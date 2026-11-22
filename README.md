# 高鐵座位監控（新竹 → 台北）

**預設行程：2026/11/22（日），1 人，19:45 才能抵達高鐵新竹站；監控可搭乘的北上標準對號座，Telegram 主動通知。**

本專案用 Python 3.9+ 標準函式庫，不需 `pip install`，適合 Raspberry Pi / Debian / WSL / Linux。啟動立即檢查一次，每 5 分鐘輪詢。`Ctrl+C` 可停止。日期結束後自動退出。

> ⚠️ **功能邊界**：使用 TDX 公開 API，反映的是上游約每 10 分鐘更新的餘位狀態，**無法在旅客退票的那一秒偵測，也無法保證訂得到票**。軟體不會自動訂位、付款或繞過高鐵購票機制；真正要買票仍需使用官方 T-EX App 或官網。自由座燈號**沒有在此專案中自動解析**：程式會在當日 18:30、19:30 推播**官方自由座預估等候時間頁面連結提醒**，不是自由座即時數值通知。

## 1. 啟動（幾分鐘內完成）

```bash
# 在解壓縮後的 thsr-monitor 資料夾
cd thsr-monitor
cp .env.example .env
nano .env
```

**取得 TDX 金鑰**：前往 https://tdx.transportdata.tw/register 註冊，從會員中心取得 `Client ID` / `Client Secret`，填入 `.env`。

```env
TDX_CLIENT_ID=你的ID
TDX_CLIENT_SECRET=你的Secret
```

沒有 Telegram 也能用，只會在終端機顯示變化；但不會有手機推播。

**Telegram 手機推播（建議）**：

1. 在 Telegram 搜尋 `@BotFather`，輸入 `/newbot` 建立 Bot，取得 Token，填入 `.env` 的 `TELEGRAM_BOT_TOKEN`。
2. 在 Telegram 搜尋你建立的 Bot，對它發送 `/start`。
3. 執行 `python3 thsr_monitor.py --show-chat-id`，將顯示的數字填入 `TELEGRAM_CHAT_ID`。
4. 執行 `python3 thsr_monitor.py --test-telegram`，確認手機收到測試訊息。

第一次測試：

```bash
python3 thsr_monitor.py --demo    # 不連網、用模擬資料測試事件判斷
python3 thsr_monitor.py --once    # 真的呼叫 TDX，只檢查一次
./start.sh                      # 持續監控（一直執行到今日結束）
```

成功時，終端機會顯示符合條件車次與 `尚有座位 / 座位有限 / 已售完 / 未知`。**首次查到有位**以及**已售完後重新查到有位**都會通知；連續有位不會一直重複提醒。

## 2. Raspberry Pi 斷電後自動恢復：systemd

```bash
# 確保 .env 已經填好且可跑 --once
chmod +x start.sh install_service.sh
./install_service.sh

# 看服務狀態與輸出
sudo systemctl status thsr-monitor-$(whoami).service
journalctl -u thsr-monitor-$(whoami).service -f

# 提前停止監控
sudo systemctl disable --now thsr-monitor-$(whoami).service
```

`install_service.sh` 需要 `sudo`，僅安裝本專案服務，不依賴你原本的監控專案。服務以目前一般使用者身分執行；程式正常結束（日期已過）就不會自動重啟。

不需要 systemd 時，可以在 Linux/WSL 用 `./start.sh` 維持前景運行（終端機關閉會中止）。

## 3. 可自行調整的參數

| `.env` 參數 | 預設 | 說明 |
|---|---|---|
| `TRAVEL_DATE` | `2026-11-22` | 搭乘日期（台灣時間） |
| `ARRIVAL_AT_STATION` | `19:45` | 最早到達高鐵新竹站的時間 |
| `BOARDING_BUFFER_MINUTES` | `7` | 到站後預留 7 分鐘買票、過閘門、上月台，因此最早提醒 19:52 後車次 |
| `POLL_SECONDS` | `300` | 5 分鐘，建議不要更頻繁；TDX 約 10 分鐘更新一次 |
| `ORIGIN_STATION_ID` | `1030` | 新竹 |
| `DESTINATION_STATION_ID` | `1000` | 台北 |

除此之外，程式**自動忽略已發車或發車前不足 5 分鐘的車次**；這只是保守提醒邏輯，不代表購票通路的正式截止規則。表定時間不是即時誤點資料。

## 4. 穩定性、限制與驗證

- OAuth2 自動取得並快取 TDX Token；HTTP 401 會重取一次；遇到錯誤最多退避至 1 小時，避免不斷打 API。
- 時刻表每 30 分鐘重新讀取；TDX 資料結構錯誤時會顯示 ERROR，不把未知值誤判成有座。
- 2026-07 起 TDX 可能回傳 `AvailableSeats` wrapper；也支援舊版 `Items`、陣列格式。
- 將各車次最後狀態保存在 `.state.json`，Raspberry Pi 重開機後不會因相同餘位重複推播。
- 可記錄 source timestamp 時，若資料超過 30 分鐘沒更新就**不推播可能過期的餘位**。
- `.env`、`.state.json` 已列入 `.gitignore`；`install_service.sh` 會把 `.env` 權限設為 600。不要把 Token 貼到公開的 GitHub repository。
- 內建測試：`python3 -m unittest discover -s tests -v`。
- **已執行離線測試；尚未持有你的 TDX API 金鑰，無法實際測試當日 API 回傳或 Telegram 實際送達。** 若官方格式變動，可用 `--once` 的報錯排查。

## 5. 自由座備案

當日約 18:30 與 19:30 會提醒你檢查官方自由座等候預估頁：
https://www.thsrc.com.tw/t/nrwaittime

程式不會宣稱自動判斷自由座「能否上車」；即使官方預估等候時間低也不能保證上車。對號座買不到，仍可在到站後依現場規定購買自由座並排隊。

## 參考的官方資料

- TDX 服務與金鑰：https://tdx.transportdata.tw/about/service
- TDX 高鐵時刻表與餘位 API：https://tdx.transportdata.tw/data-service/basic
- 餘位資料更新頻率：https://motc-ptx.gitbook.io/tdx-zi-liao-shi-yong-kui-hua-bao-dian/data_notice/public_transportation_data/rail_data
- 高鐵自由座等候時間：https://www.thsrc.com.tw/t/nrwaittime
- 官方訂票：https://irs.thsrc.com.tw/IMINT/
