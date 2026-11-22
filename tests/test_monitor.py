import sys
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import thsr_monitor as m


class TestTDXPayloads(unittest.TestCase):
    def test_2026_available_seats_wrapper(self):
        payload = {"AvailableSeats": [
            {"TrainNo": "0676", "StandardSeatStatus": "L"},
            {"TrainNo": "0850", "StandardSeatStatus": "X"},
        ], "SrcUpdateTime": "2026-11-22T18:50:00+08:00"}
        self.assertEqual(m.parse_seats(payload), {"0676": "L", "0850": "X"})
        self.assertEqual(m.parse_source_update(payload).hour, 18)

    def test_legacy_items_and_aliases(self):
        payload = {"Items": [{"TrainNo": "676", "StdSeatStatus": "Available"}]}
        self.assertEqual(m.parse_seats(payload), {"676": "O"})

    def test_timetable_and_stops(self):
        payload = [{"DailyTrainInfo": {"TrainNo": "0676"},
                    "OriginStopTime": {"DepartureTime": "19:57:00"},
                    "DestinationStopTime": {"ArrivalTime": "20:36:00"}},
                   {"DailyTrainInfo": {"TrainNo": "0088"},
                    "OriginStopTime": {"DepartureTime": "10:00"},
                    "DestinationStopTime": {"ArrivalTime": "10:30"}}]
        self.assertEqual(m.parse_timetable(payload)["0676"], ("19:57", "20:36"))

    def test_malformed_fails_closed(self):
        with self.assertRaises(m.MonitorError):
            m.parse_seats({"garbage": []})
        with self.assertRaises(m.MonitorError):
            m.parse_seats({"AvailableSeats": []})
        self.assertEqual(m.normalize_status("unknown"), "?")


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self.cfg = m.Config("id", "secret", date(2026, 11, 22), time(19, 45))
        self.now = datetime(2026, 11, 22, 18, 59, tzinfo=m.TZ)
        self.timetable = {"a": ("19:50", "20:20"), "b": ("19:57", "20:30"), "c": ("20:08", "20:42")}
        self.temp = tempfile.TemporaryDirectory()
        self.state = m.State(Path(self.temp.name) / ".state.json", self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def test_arrival_buffer_and_current_time(self):
        trains = m.eligible_trains(self.cfg, self.timetable, {"a": "O", "b": "L", "c": "X"}, self.now)
        self.assertEqual([x[0] for x in trains], ["b", "c"])
        self.now = datetime(2026, 11, 22, 20, 1, tzinfo=m.TZ)
        trains = m.eligible_trains(self.cfg, self.timetable, {}, self.now)
        self.assertEqual([x[0] for x in trains], ["c"])

    def test_initial_and_transitions_and_restart(self):
        initial = [("b", "19:57", "20:30", "L"), ("c", "20:08", "20:42", "X")]
        self.assertEqual(len(m.evaluate_changes(initial, self.state)), 1)
        m.update_statuses(initial, self.state)
        self.state.save()
        restarted = m.State(self.state.path, self.cfg)
        self.assertFalse(m.evaluate_changes(initial, restarted))
        next_t = [("b", "19:57", "20:30", "X"), ("c", "20:08", "20:42", "O")]
        self.assertEqual(len(m.evaluate_changes(next_t, restarted)), 1)
        m.update_statuses(next_t, restarted)
        self.assertFalse(m.evaluate_changes(next_t, restarted))
        unknown = [("b", "19:57", "20:30", "?"), ("c", "20:08", "20:42", "?")]
        m.update_statuses(unknown, restarted)
        self.assertEqual(restarted.status["b"], "X")

    def test_check_once_alert_and_persistence(self):
        class FakeTDX:
            def seats(self):
                return {"b": "O", "c": "X"}, None
        messages = []
        trains = m.run_check(self.cfg, FakeTDX(), self.state, self.timetable, self.now, messages.append)
        self.assertEqual(len(trains), 2)
        self.assertEqual(len(messages), 1)
        self.assertIn("19:57", messages[0])
        self.assertEqual(m.State(self.state.path, self.cfg).status["b"], "O")
        m.run_check(self.cfg, FakeTDX(), self.state, self.timetable, self.now, messages.append)
        self.assertEqual(len(messages), 1)

    def test_stale_timestamp_disables_alert(self):
        class FakeTDX:
            def seats(self):
                return {"b": "O"}, datetime(2026, 11, 22, 18, 0, tzinfo=m.TZ)
        messages = []
        m.run_check(self.cfg, FakeTDX(), self.state, self.timetable, self.now, messages.append)
        self.assertEqual(messages, [])
        self.assertEqual(self.state.status, {})

    def test_reminders_one_time(self):
        messages = []
        m.send_free_seat_reminder(self.cfg, self.state, self.now, messages.append)
        m.send_free_seat_reminder(self.cfg, self.state, self.now, messages.append)
        self.assertEqual(len(messages), 1)
        self.assertIn("自由座", messages[0])


if __name__ == "__main__":
    unittest.main()
