#!/usr/bin/env python3
"""THSR reserved-seat availability watcher using public TDX APIs.

No booking, captcha automation, or payment. Python 3.9+, stdlib only.
The TDX same-day seat data generally updates every ~10 minutes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
TDX_ROOT = "https://tdx.transportdata.tw/api/basic/v2/Rail/THSR"
TDX_AUTH = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
BOOK_URL = "https://irs.thsrc.com.tw/IMINT/"
WAIT_URL = "https://www.thsrc.com.tw/t/nrwaittime"
STATUSES = {"O": "尚有座位", "L": "座位有限", "X": "已售完"}


class MonitorError(Exception):
    pass


class HTTPStatusError(MonitorError):
    def __init__(self, code: int, host: str, detail: str = ""):
        self.code = code
        super().__init__(f"HTTP {code} from {host}" + (f" — {detail}" if detail else ""))


def telegram_description(payload: Any, token: str) -> str:
    description = payload.get("description") if isinstance(payload, dict) else None
    if not isinstance(description, str):
        return ""
    return " ".join(description.replace(token, "[redacted]").split())[:500]


def tdx_error_detail(path: str, payload: Any, secrets: tuple[str, ...]) -> str:
    if path == parse.urlsplit(TDX_AUTH).path:
        stage = "TDX 取得存取權杖"
    elif "/DailyTimetable/" in path:
        stage = "TDX 查詢時刻表"
    elif "/AvailableSeatStatus/" in path:
        stage = "TDX 查詢餘位"
    else:
        stage = "TDX API"
    if not isinstance(payload, dict):
        return stage
    fields = ("error", "error_description", "message", "Message", "detail", "title", "description")
    details = [payload[key] for key in fields if isinstance(payload.get(key), str) and payload[key]]
    message = " | ".join(dict.fromkeys(details))
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        for form in {secret, parse.quote(secret, safe=""), parse.quote_plus(secret)}:
            message = message.replace(form, "[redacted]")
    message = " ".join(message.split())[:500]
    return stage + (f" — {message}" if message else "")


def load_dotenv(filepath: Path) -> None:
    """Read simple KEY=VALUE lines, without overriding existing environment vars."""
    if not filepath.exists():
        return
    for raw in filepath.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key.isidentifier():
            raise MonitorError(f"Invalid environment variable name: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def http_json(url: str, *, bearer: str | None = None,
              data: dict[str, str] | None = None) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "thsr-personal-seat-monitor/1.1 (public TDX API)",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    body = None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = parse.urlencode(data).encode("utf-8")
    req = request.Request(url, data=body, headers=headers)
    endpoint = parse.urlsplit(url)
    host = endpoint.hostname or "remote host"
    try:
        with request.urlopen(req, timeout=20) as response:
            return json.load(response)
    except error.HTTPError as exc:
        detail = ""
        with exc:
            if host in ("api.telegram.org", "tdx.transportdata.tw"):
                try:
                    payload = json.loads(exc.read(8192))
                except (ValueError, OSError):
                    payload = None
                if host == "api.telegram.org":
                    token = endpoint.path.split("/")[1][3:]
                    detail = telegram_description(payload, token)
                else:
                    form = data or {}
                    detail = tdx_error_detail(
                        endpoint.path, payload,
                        (form.get("client_id", ""), form.get("client_secret", ""), bearer or ""),
                    )
        raise HTTPStatusError(exc.code, host, detail) from None
    except error.URLError as exc:
        reason = type(exc.reason).__name__ if host == "api.telegram.org" else exc.reason
        raise MonitorError(f"Connection failure to {host}: {reason}") from None
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise MonitorError(f"Invalid JSON from {parse.urlsplit(url).hostname}: {type(exc).__name__}") from None


def unwrap_rows(payload: Any) -> list[dict[str, Any]]:
    """Accept historical arrays and TDX 2026 AvailableSeats wrapper."""
    if isinstance(payload, list):
        if not all(isinstance(item, dict) for item in payload):
            raise MonitorError("Unexpected list element in TDX response")
        return payload
    if isinstance(payload, dict):
        for field in ("AvailableSeats", "AvailableSeatStatus", "DailyTimetables", "Items", "Data", "data", "value"):
            if field in payload:
                return unwrap_rows(payload[field])
    raise MonitorError(f"Unknown TDX response structure; keys={list(payload)[:12] if isinstance(payload, dict) else type(payload).__name__}")


def train_no(item: dict[str, Any]) -> str:
    nested = item.get("DailyTrainInfo") or {}
    return str(item.get("TrainNo") or nested.get("TrainNo") or "").strip()


def normalize_status(raw: Any) -> str:
    status = str(raw or "").strip().upper()
    return {"AVAILABLE": "O", "LIMITED": "L", "FULL": "X"}.get(status, status if status in STATUSES else "?")


def parse_timetable(payload: Any) -> dict[str, tuple[str, str]]:
    results: dict[str, tuple[str, str]] = {}
    for row in unwrap_rows(payload):
        number = train_no(row)
        origin = row.get("OriginStopTime") or {}
        dest = row.get("DestinationStopTime") or {}
        departure = str(origin.get("DepartureTime") or "")[:5]
        arrival = str(dest.get("ArrivalTime") or "")[:5]
        if number and is_hhmm(departure) and is_hhmm(arrival):
            results[number] = departure, arrival
    if not results:
        raise MonitorError("Timetable returned no usable train records")
    return results


def parse_seats(payload: Any) -> dict[str, str]:
    results: dict[str, str] = {}
    for row in unwrap_rows(payload):
        number = train_no(row)
        if not number:
            continue
        raw = row.get("StandardSeatStatus", row.get("StdSeatStatus"))
        results[number] = normalize_status(raw)
    if not results:
        raise MonitorError("Seat API returned no usable train numbers")
    return results


def is_hhmm(text: str) -> bool:
    try:
        dt_time.fromisoformat(text)
        return len(text) == 5
    except ValueError:
        return False


def parse_source_update(payload: Any) -> datetime | None:
    """Return source collection timestamp if supplied. Do not assume fetch time."""
    if not isinstance(payload, dict):
        return None
    for field in ("SrcUpdateTime", "SrcRecTime"):
        raw = payload.get(field)
        if isinstance(raw, str):
            try:
                out = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return out if out.tzinfo else out.replace(tzinfo=TZ)
            except ValueError:
                pass
    return None


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    travel_date: date
    arrival_at_station: dt_time
    origin_station: str = "1030"   # 新竹
    destination_station: str = "1000"  # 台北
    boarding_buffer_min: int = 7
    poll_seconds: int = 300
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_lag_minutes: int = 30

    @classmethod
    def from_env(cls) -> "Config":
        try:
            travel_date = date.fromisoformat(os.getenv("TRAVEL_DATE", "2026-11-22"))
            arrival = dt_time.fromisoformat(os.getenv("ARRIVAL_AT_STATION", "19:45"))
            boarding = int(os.getenv("BOARDING_BUFFER_MINUTES", "7"))
            poll = int(os.getenv("POLL_SECONDS", "300"))
        except (ValueError, TypeError) as exc:
            raise MonitorError(f"Invalid date, time or interval in .env: {exc}") from None
        if poll < 60:
            raise MonitorError("POLL_SECONDS must be >= 60; TDX data only changes about every 10 min")
        if not 0 <= boarding <= 60:
            raise MonitorError("BOARDING_BUFFER_MINUTES must be between 0 and 60")
        return cls(
            client_id=os.getenv("TDX_CLIENT_ID", ""),
            client_secret=os.getenv("TDX_CLIENT_SECRET", ""),
            travel_date=travel_date,
            arrival_at_station=arrival,
            origin_station=os.getenv("ORIGIN_STATION_ID", "1030"),
            destination_station=os.getenv("DESTINATION_STATION_ID", "1000"),
            boarding_buffer_min=boarding,
            poll_seconds=poll,
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )


class TDX:
    def __init__(self, cfg: Config, fetch: Callable[..., Any] = http_json):
        self.cfg = cfg
        self.fetch = fetch
        self.access_token = ""
        self.valid_until = 0.0

    def token(self, *, force: bool = False) -> str:
        if not force and time.monotonic() < self.valid_until and self.access_token:
            return self.access_token
        if not self.cfg.client_id or not self.cfg.client_secret:
            raise MonitorError("Please configure TDX_CLIENT_ID and TDX_CLIENT_SECRET in .env")
        payload = self.fetch(TDX_AUTH, data={
            "grant_type": "client_credentials",
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
        })
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise MonitorError("TDX token response missing access_token")
        self.access_token = str(payload["access_token"])
        self.valid_until = time.monotonic() + max(60, int(payload.get("expires_in", 3600)) - 120)
        return self.access_token

    def get(self, path: str) -> Any:
        endpoint = f"{TDX_ROOT}/{path}"
        token = self.token()
        try:
            return self.fetch(endpoint, bearer=token)
        except HTTPStatusError as exc:
            if exc.code != 401:
                raise
            return self.fetch(endpoint, bearer=self.token(force=True))

    def timetable(self) -> dict[str, tuple[str, str]]:
        cfg = self.cfg
        path = f"DailyTimetable/OD/{cfg.origin_station}/to/{cfg.destination_station}/{cfg.travel_date}"
        return parse_timetable(self.get(path))

    def seats(self) -> tuple[dict[str, str], datetime | None]:
        cfg = self.cfg
        path = ("AvailableSeatStatus/Train/OD/"
                f"{cfg.origin_station}/to/{cfg.destination_station}/TrainDate/{cfg.travel_date}")
        payload = self.get(path)
        return parse_seats(payload), parse_source_update(payload)


def eligible_trains(cfg: Config, timetable: dict[str, tuple[str, str]],
                    seat_states: dict[str, str], now: datetime) -> list[tuple[str, str, str, str]]:
    """Skip services too soon to board, based on station arrival and local time."""
    ready = datetime.combine(cfg.travel_date, cfg.arrival_at_station, TZ) + timedelta(minutes=cfg.boarding_buffer_min)
    minimum = max(ready, now + timedelta(minutes=5))
    trains: list[tuple[str, str, str, str]] = []
    for number, (departure, arrival) in timetable.items():
        dep_dt = datetime.combine(cfg.travel_date, dt_time.fromisoformat(departure), TZ)
        if dep_dt < minimum:
            continue
        trains.append((number, departure, arrival, seat_states.get(number, "?")))
    trains.sort(key=lambda t: (t[1], t[0]))
    return trains


class State:
    def __init__(self, path: Path, cfg: Config):
        self.path = path
        self.key = f"{cfg.travel_date}:{cfg.origin_station}:{cfg.destination_station}"
        self.status: dict[str, str] = {}
        self.reminders: list[str] = []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if loaded.get("key") == self.key and isinstance(loaded.get("status"), dict):
                self.status = {k: v for k, v in loaded["status"].items() if v in STATUSES}
                self.reminders = list(loaded.get("reminders") or [])
        except FileNotFoundError:
            pass
        except (ValueError, OSError, AttributeError, TypeError) as exc:
            print(f"[WARN] State file not readable; starting fresh: {exc}", flush=True)

    def save(self) -> None:
        data = {"key": self.key, "status": self.status, "reminders": self.reminders}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


def telegram_setting(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def telegram_request(token: str, method: str, fetch: Callable[..., Any] = http_json,
                     *, data: dict[str, str] | None = None) -> Any:
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise MonitorError("TELEGRAM_BOT_TOKEN 格式錯誤，請填入 BotFather 提供的完整 Token（數字:金鑰）。")
    options = {"data": data} if data is not None else {}
    result = fetch(f"https://api.telegram.org/bot{token}/{method}", **options)
    if not isinstance(result, dict) or result.get("ok") is not True:
        detail = telegram_description(result, token) or "回應未確認成功"
        raise MonitorError(f"Telegram {method} — {detail}")
    return result.get("result")


def telegram_send(cfg: Config, message: str, fetch: Callable[..., Any] = http_json) -> None:
    token = telegram_setting(cfg.telegram_bot_token)
    chat_id = telegram_setting(cfg.telegram_chat_id)
    if not (token and chat_id):
        print("\n🔔 " + message + "\n", flush=True)
        print("[WARN] Telegram not configured; notification printed locally only.", flush=True)
        return
    if not re.fullmatch(r"(?:-?[1-9][0-9]*|@[A-Za-z0-9_]+)", chat_id):
        raise MonitorError("TELEGRAM_CHAT_ID 格式錯誤，請填入 --show-chat-id 顯示的完整數字（保留負號），或頻道的 @username。")
    telegram_request(
        token, "sendMessage", fetch,
        data={"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"},
    )
    print("\n🔔 " + message + "\n", flush=True)


def evaluate_changes(trains: list[tuple[str, str, str, str]], state: State) -> list[str]:
    """Notify on initial O/L or a transition from known sold-out X to O/L."""
    changes = []
    for number, departure, arrival, code in trains:
        if code not in STATUSES:  # Missing/unknown is not 'sold out'.
            continue
        if code in ("O", "L") and state.status.get(number) not in ("O", "L"):
            changes.append(f"🚄 {number} 次｜新竹 {departure} → 台北 {arrival}｜{STATUSES[code]}")
    return changes


def update_statuses(trains: list[tuple[str, str, str, str]], state: State) -> None:
    for number, _departure, _arrival, code in trains:
        if code in STATUSES:
            state.status[number] = code


def run_check(cfg: Config, tdx: TDX, state: State,
              timetable: dict[str, tuple[str, str]], now: datetime,
              notify: Callable[[str], None]) -> list[tuple[str, str, str, str]]:
    seats, source_update = tdx.seats()
    trains = eligible_trains(cfg, timetable, seats, now)
    if trains:
        print(f"[{now:%Y-%m-%d %H:%M:%S}] " + " | ".join(
            f"{no} {dep} {STATUSES.get(code, '未知')}" for no, dep, _arrival, code in trains), flush=True)
    else:
        print(f"[{now:%Y-%m-%d %H:%M:%S}] 沒有尚可搭乘的符合條件車次", flush=True)

    if source_update is not None:
        age = now - source_update.astimezone(TZ)
        if age > timedelta(minutes=cfg.alert_lag_minutes):
            print(f"[WARN] Seat feed may be stale (source timestamp {source_update.isoformat()}); skip alerts", flush=True)
            return trains

    changes = evaluate_changes(trains, state)
    if changes:
        notify("【高鐵對號座可能釋出】1 人，標準車廂\n"
               + "\n".join(changes)
               + "\n請立刻用 T-EX App 或官網確認並付款；TDX 非即時訂位保證。"
               + f"\n官方訂票：{BOOK_URL}")
    update_statuses(trains, state)
    state.save()
    return trains


def send_free_seat_reminder(cfg: Config, state: State, now: datetime,
                            notify: Callable[[str], None]) -> None:
    """Official wait page reminder only. The page has no documented public data API."""
    for label, hhmm, until in (
        ("18:30", dt_time(18, 30), dt_time(19, 0)),
        ("19:30", dt_time(19, 30), dt_time(19, 45)),
    ):
        if label in state.reminders or now.date() != cfg.travel_date:
            continue
        if now.time() < hhmm or now.time() > until:
            continue
        notify(f"【高鐵自由座備案】現在 {now:%H:%M}。如果對號座仍買不到，"
               "可查看高鐵官方『當日自由座等候時間預估』，新竹站、北上。\n"
               f"{WAIT_URL}\n"
               "注意：這只是查看提醒，程式無法自動讀取該頁的即時燈號。")
        state.reminders.append(label)
        state.save()


def chat_id_helper(cfg: Config) -> None:
    token = telegram_setting(cfg.telegram_bot_token)
    updates = telegram_request(token, "getUpdates")
    if not isinstance(updates, list):
        raise MonitorError("Telegram getUpdates 回應缺少訊息清單")
    chats = set()
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat_id = message.get("chat", {}).get("id")
        if isinstance(chat_id, int):
            chats.add(str(chat_id))
    if not chats:
        print("尚無 Chat ID：請先在 Telegram 對你的 Bot 發送 /start，再執行一次。")
    else:
        print("在 .env 設定 TELEGRAM_CHAT_ID 為以下其中一個值：", ", ".join(sorted(chats)))


def demo() -> None:
    cfg = Config("demo", "demo", date(2026, 11, 22), dt_time(19, 45))
    now = datetime(2026, 11, 22, 18, 20, tzinfo=TZ)
    sample_timetable = {
        "676": ("19:57", "20:36"),
        "850": ("20:08", "20:42"),
        "678": ("20:25", "21:02"),
    }
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as tmp:
        state = State(Path(tmp) / "state.json", cfg)
        for i, statuses in enumerate((
            {"676": "X", "850": "X", "678": "X"},
            {"676": "X", "850": "L", "678": "X"},
            {"676": "X", "850": "L", "678": "X"},
        )):
            trains = eligible_trains(cfg, sample_timetable, statuses, now)
            changes = evaluate_changes(trains, state)
            print(f"[DEMO {i+1}] " + " | ".join(f"{t[0]}:{t[3]}" for t in trains))
            if changes:
                print("[DEMO ALERT] " + "; ".join(changes))
            update_statuses(trains, state)
            state.save()
    print("Demo finished; no requests or notifications sent.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="新竹→台北 高鐵對號座監控（TDX 每 5 分鐘輪詢）")
    parser.add_argument("--once", action="store_true", help="只檢查一次")
    parser.add_argument("--check-tdx", action="store_true", help="測試 TDX 認證、時刻表與餘位，不發送通知")
    parser.add_argument("--test-telegram", action="store_true", help="發送 Telegram 測試訊息")
    parser.add_argument("--show-chat-id", action="store_true", help="列出已對 Bot 傳訊的 Telegram Chat ID")
    parser.add_argument("--demo", action="store_true", help="不需金鑰的離線示範")
    args = parser.parse_args(argv)
    if args.demo:
        demo()
        return 0
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    cfg = Config.from_env()
    if args.show_chat_id:
        chat_id_helper(cfg)
        return 0
    if args.test_telegram:
        if not (telegram_setting(cfg.telegram_bot_token) and telegram_setting(cfg.telegram_chat_id)):
            raise MonitorError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
        telegram_send(cfg, "✅ 高鐵監控測試通知成功。")
        return 0
    if not cfg.client_id or not cfg.client_secret:
        raise MonitorError("Set TDX_CLIENT_ID and TDX_CLIENT_SECRET in .env first")
    if args.check_tdx:
        tdx = TDX(cfg)
        tdx.token()
        print("TDX 認證成功", flush=True)
        print(f"TDX 時刻表查詢成功，共 {len(tdx.timetable())} 班", flush=True)
        seats, _ = tdx.seats()
        print(f"TDX 餘位查詢成功，共 {len(seats)} 班", flush=True)
        return 0
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        print("[WARN] Telegram not configured; only terminal logs will be shown.", flush=True)
    print(f"TDX 監控 {cfg.travel_date}，新竹 {cfg.arrival_at_station:%H:%M} 抵達車站後，"
          f"預留 {cfg.boarding_buffer_min} 分鐘，1 人標準對號座；{cfg.poll_seconds} 秒輪詢。", flush=True)
    print("TDX 當日餘位資料約每 10 分鐘更新；本工具不會自動購票。", flush=True)
    if datetime.now(TZ).date() > cfg.travel_date:
        print("搭乘日期已過，停止。", flush=True)
        return 0

    state = State(root / ".state.json", cfg)
    tdx = TDX(cfg)
    timetable: dict[str, tuple[str, str]] = {}
    timetable_updated = 0.0
    failures = 0
    while True:
        now = datetime.now(TZ)
        if now.date() > cfg.travel_date:
            print("搭乘日期已過，停止。", flush=True)
            return 0
        next_delay = cfg.poll_seconds
        try:
            if not timetable or time.monotonic() - timetable_updated > 1800:
                timetable = tdx.timetable()
                timetable_updated = time.monotonic()
            notifier = lambda message: telegram_send(cfg, message)
            if not args.once:
                send_free_seat_reminder(cfg, state, now, notifier)
            run_check(cfg, tdx, state, timetable, now, notifier)
            failures = 0
        except (MonitorError, OSError, KeyError, ValueError) as exc:
            failures += 1
            next_delay = min(3600, cfg.poll_seconds * (2 ** min(failures, 4)))
            print(f"[ERROR {now:%H:%M:%S}] {exc}; retry in {next_delay}s", file=sys.stderr, flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        try:
            time.sleep(next_delay)
        except KeyboardInterrupt:
            print("使用者停止監控。")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MonitorError, KeyboardInterrupt) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

