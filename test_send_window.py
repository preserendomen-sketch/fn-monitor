#!/usr/bin/env python3
"""Prove entry alerts stay inside 08:00-21:00 Europe/Ljubljana weekdays.

These tests must not call the network or the real webhook. scan_live is fed
synthetic candles and a fake send function. urllib and the HTTP helper are
patched to fail the test if anything still tries to leave the machine.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import fnlib
import scanner
from fnlib import LJ_TZ

ROOT = Path(__file__).resolve().parent
WINDOWS = ROOT / "windows"
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
UTC = timezone.utc


def first_weekday(year: int, month: int, weekday: int) -> datetime:
    day = datetime(year, month, 1, tzinfo=LJ_TZ)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day


def at_local(day: datetime, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LJ_TZ)


def candle(close_local: datetime) -> dict:
    start = close_local - timedelta(minutes=15)
    return {
        "ts": start.timestamp(),
        "open": 1.0,
        "high": 1.2,
        "low": 0.8,
        "close": 1.0,
        "volume": 10.0,
        "complete": True,
    }


def live_rules() -> dict:
    return {
        "active_start": "08:00",
        "active_end": "21:00",
        "weekdays_only": True,
        "cooldown_minutes": 90,
        "max_alerts_per_day": 12,
        "max_alerts_per_symbol_per_day": 2,
        "max_signal_age_minutes": 45,
    }


class SendWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {}
        for key in ("FN_WEBHOOK_URL", "FN_WEBHOOK_KEY"):
            self._saved_env[key] = os.environ.pop(key, None)
        self.urlopen = patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("real webhook must not be called"),
        ).start()
        self.http = patch(
            "fnlib.http_get_json",
            side_effect=AssertionError("network must not be called"),
        ).start()
        self.addCleanup(patch.stopall)
        fnlib._stop_requested = False
        if hasattr(scanner.note_send_window, "was_open"):
            del scanner.note_send_window.was_open

    def tearDown(self) -> None:
        fnlib._stop_requested = False
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.assertEqual(self.urlopen.call_count, 0)
        self.assertEqual(self.http.call_count, 0)

    def _scan(self, closes, now, *, send=True, prev=None, gate=None, sent_today=0, sent_by=None, day=None):
        bars = [candle(close) for close in closes]
        bars.sort(key=lambda bar: bar["ts"])
        if prev is None:
            prev = bars[0]["ts"] - 60
        inst = {
            "id": "BTCUSD",
            "symbol": "BTCUSD",
            "source": "binance",
            "ticker": "BTCUSDT",
            "offset": 0,
        }
        sc = live_rules()
        sc["universe"] = [inst]
        state = {
            "day": day or datetime.now(LJ_TZ).strftime("%Y-%m-%d"),
            "sent_today": sent_today,
            "sent_today_by_symbol": dict(sent_by or {}),
            "last": {},
            "last_processed": {inst["id"]: prev},
        }
        sent = []
        evaluated = []
        news_calls = {"n": 0}

        def fake_fetch(_inst):
            return list(bars), [], None, None

        def fake_eval(instrument, m15, idx, h1, sc_, m5=None, daily=None, news=None):
            evaluated.append(m15[idx]["ts"])
            return [
                {
                    "symbol": instrument["symbol"],
                    "event": "candidate",
                    "type": "breakout",
                    "direction": "buy",
                    "level": 1.0,
                    "price": 1.0,
                    "broker_price": 1.0,
                    "bar_ts": m15[idx]["ts"],
                    "note": "test",
                    "time_utc": "2026-01-01T00:00:00Z",
                }
            ]

        def fake_send(_cfg, payload):
            sent.append(payload)
            return True, "test-sink"

        def fake_news(_sc):
            news_calls["n"] += 1
            return [], True

        patches = [
            patch("scanner.fetch_instrument", fake_fetch),
            patch("scanner.evaluate_bar", fake_eval),
            patch("scanner.load_news_calendar", fake_news),
            patch("scanner.send_webhook", fake_send),
            patch("scanner.time.sleep", return_value=None),
        ]
        if gate is not None:
            patches.append(patch("scanner.entry_alert_allowed", gate))
        for item in patches:
            item.start()
        try:
            fired = scanner.scan_live({}, sc, state, send=send, now=now.timestamp())
        finally:
            for item in reversed(patches):
                item.stop()
        return sent, state, evaluated, news_calls["n"], fired

    def test_window_edges_and_weekend(self) -> None:
        sc = live_rules()
        thursday = first_weekday(2026, 11, 3)
        friday = first_weekday(2026, 11, 4)
        saturday = first_weekday(2026, 11, 5)
        sunday = first_weekday(2026, 11, 6)
        monday = first_weekday(2026, 11, 0)

        def allowed(day, hour, minute):
            return scanner.entry_alert_allowed(at_local(day, hour, minute).timestamp(), sc)

        self.assertFalse(allowed(thursday, 0, 15))
        self.assertFalse(allowed(thursday, 3, 0))
        self.assertFalse(allowed(thursday, 7, 59))
        self.assertTrue(allowed(thursday, 8, 0))
        self.assertTrue(allowed(thursday, 12, 0))
        self.assertTrue(allowed(thursday, 20, 59))
        self.assertFalse(allowed(thursday, 21, 0))
        self.assertFalse(allowed(thursday, 23, 45))
        self.assertTrue(allowed(friday, 20, 59))
        self.assertFalse(allowed(friday, 21, 0))
        self.assertTrue(allowed(monday, 8, 0))
        self.assertFalse(allowed(monday, 7, 59))
        self.assertFalse(allowed(saturday, 10, 0))
        self.assertFalse(allowed(saturday, 20, 0))
        self.assertFalse(allowed(sunday, 12, 0))

    def test_window_follows_ljubljana_across_dst(self) -> None:
        sc = live_rules()
        winter = first_weekday(2026, 1, 2)
        summer = first_weekday(2026, 7, 2)
        self.assertEqual(at_local(winter, 8, 0).utcoffset(), timedelta(hours=1))
        self.assertEqual(at_local(summer, 8, 0).utcoffset(), timedelta(hours=2))
        self.assertTrue(scanner.entry_alert_allowed(at_local(winter, 8, 0).timestamp(), sc))
        self.assertFalse(scanner.entry_alert_allowed(at_local(winter, 7, 0).timestamp(), sc))
        self.assertTrue(scanner.entry_alert_allowed(at_local(summer, 8, 0).timestamp(), sc))
        self.assertFalse(scanner.entry_alert_allowed(at_local(summer, 7, 0).timestamp(), sc))
        # 06:00 UTC is 07:00 in January (closed) and 08:00 in July (open).
        winter_utc = datetime(winter.year, winter.month, winter.day, 6, 0, tzinfo=UTC)
        summer_utc = datetime(summer.year, summer.month, summer.day, 6, 0, tzinfo=UTC)
        self.assertFalse(scanner.entry_alert_allowed(winter_utc.timestamp(), sc))
        self.assertTrue(scanner.entry_alert_allowed(summer_utc.timestamp(), sc))

    def test_overnight_and_weekend_bars_do_not_send(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        saturday = first_weekday(2026, 11, 5)
        cases = [
            at_local(thursday, 3, 0),
            at_local(thursday, 7, 59),
            at_local(thursday, 21, 0),
            at_local(thursday, 23, 30),
            at_local(saturday, 12, 0),
        ]
        for close in cases:
            with self.subTest(close=close.isoformat()):
                sent, state, evaluated, news, fired = self._scan(
                    [close], close + timedelta(minutes=1)
                )
                self.assertEqual(sent, [])
                self.assertEqual(fired, [])
                self.assertEqual(evaluated, [])
                self.assertEqual(news, 0)
                self.assertEqual(state["sent_today"], 0)
                self.assertEqual(state["last"], {})
                self.assertEqual(state["last_processed"]["BTCUSD"], candle(close)["ts"])

    def test_overnight_run_keeps_the_latest_bar_without_sending(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        closes = [
            at_local(thursday, 2, 0),
            at_local(thursday, 2, 15),
            at_local(thursday, 2, 30),
            at_local(thursday, 3, 0),
        ]
        sent, state, evaluated, news, fired = self._scan(closes, closes[-1] + timedelta(minutes=5))
        self.assertEqual(sent, [])
        self.assertEqual(fired, [])
        self.assertEqual(evaluated, [])
        self.assertEqual(news, 0)
        self.assertEqual(state["last_processed"]["BTCUSD"], candle(closes[-1])["ts"])

    def test_weekday_inside_window_sends_once(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        close = at_local(thursday, 10, 0)
        sent, state, evaluated, news, fired = self._scan([close], close + timedelta(minutes=1))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["event"], "candidate")
        self.assertEqual(sent[0]["symbol"], "BTCUSD")
        self.assertEqual(sent[0]["type"], "breakout")
        self.assertNotIn("bar_ts", sent[0])
        self.assertNotIn("http", json.dumps(sent).lower())
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(evaluated), 1)
        self.assertEqual(news, 1)
        self.assertEqual(state["sent_today"], 1)

    def test_open_and_close_boundaries_go_through_scan_live(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        opened = at_local(thursday, 8, 0)
        sent, _state, evaluated, _news, _fired = self._scan([opened], opened + timedelta(minutes=1))
        self.assertEqual(len(sent), 1)
        self.assertEqual(evaluated, [candle(opened)["ts"]])

        closed = at_local(thursday, 21, 0)
        sent, state, evaluated, news, fired = self._scan([closed], closed + timedelta(minutes=1))
        self.assertEqual(sent, [])
        self.assertEqual(fired, [])
        self.assertEqual(evaluated, [])
        self.assertEqual(news, 0)
        self.assertEqual(state["sent_today"], 0)

    def test_in_window_close_can_still_be_delivered_for_45_minutes(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        close = at_local(thursday, 20, 50)
        sent, _state, _evaluated, _news, _fired = self._scan(
            [close], close + timedelta(minutes=30)
        )
        self.assertEqual(len(sent), 1)
        sent, _state, _evaluated, _news, _fired = self._scan(
            [close], close + timedelta(minutes=70)
        )
        self.assertEqual(sent, [])

    def test_no_send_flag_does_not_call_webhook_inside_the_window(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        close = at_local(thursday, 10, 0)
        sent, state, _evaluated, _news, fired = self._scan(
            [close], close + timedelta(minutes=1), send=False
        )
        self.assertEqual(sent, [])
        self.assertEqual(len(fired), 1)
        self.assertEqual(state["sent_today"], 1)

    def test_send_gate_can_block_an_otherwise_valid_bar(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        close = at_local(thursday, 10, 0)
        sent, state, evaluated, news, fired = self._scan(
            [close],
            close + timedelta(minutes=1),
            gate=lambda *_args, **_kwargs: False,
        )
        self.assertEqual(sent, [])
        self.assertEqual(fired, [])
        self.assertEqual(evaluated, [])
        self.assertEqual(news, 0)
        self.assertEqual(state["sent_today"], 0)

    def test_morning_open_sends_only_the_in_window_bar(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        overnight = at_local(thursday, 3, 0)
        before_open = at_local(thursday, 7, 45)
        opened = at_local(thursday, 8, 0)
        sent, state, evaluated, news, fired = self._scan(
            [overnight, before_open, opened],
            opened + timedelta(minutes=1),
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(fired), 1)
        self.assertEqual(evaluated, [candle(opened)["ts"]])
        self.assertEqual(news, 1)
        self.assertEqual(state["last_processed"]["BTCUSD"], candle(opened)["ts"])

    def test_backfill_never_calls_webhook(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        night = at_local(thursday, 3, 0)
        morning = at_local(thursday, 10, 0)
        bars = [candle(night), candle(morning)]
        evaluated = []
        inst = {"id": "BTCUSD", "symbol": "BTCUSD", "offset": 0}

        def fake_fetch(_inst):
            return list(bars), [], None, None

        def fake_eval(instrument, m15, idx, h1, sc_, m5=None, daily=None, news=None):
            evaluated.append(m15[idx]["ts"])
            return [
                {
                    "symbol": instrument["symbol"],
                    "event": "candidate",
                    "type": "breakout",
                    "direction": "buy",
                    "bar_ts": m15[idx]["ts"],
                    "level": 1,
                    "broker_price": 1,
                    "note": "test",
                    "time_utc": "t",
                    "rvol": 2,
                    "rsi": 50,
                    "ema_state": "bullish",
                }
            ]

        def fake_send(*_args, **_kwargs):
            raise AssertionError("backfill must not send")

        sc = live_rules()
        sc["universe"] = [inst]
        with patch("scanner.fetch_instrument", fake_fetch), patch(
            "scanner.evaluate_bar", fake_eval
        ), patch("scanner.load_news_calendar", return_value=([], True)), patch(
            "scanner.send_webhook", fake_send
        ), patch("fnlib.send_webhook", fake_send), patch(
            "scanner.time.sleep", return_value=None
        ), patch(
            "scanner.time.time", return_value=morning.timestamp() + 120
        ), patch(
            "scanner.recent_trading_days", return_value={thursday.date()}
        ):
            flagged = scanner.backfill({}, sc, days=1)
        self.assertEqual(evaluated, [candle(morning)["ts"]])
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["symbol"], "BTCUSD")
        self.assertNotIn("http", json.dumps(flagged).lower())

    def test_heartbeat_is_a_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scanner_heartbeat.json")
            with patch("scanner.HEARTBEAT_PATH", path):
                scanner.write_heartbeat(
                    live_rules(),
                    {
                        "day": "2026-11-12",
                        "sent_today": 0,
                        "last_processed": {"BTCUSD": 1_700_000_000},
                    },
                    send_window_open=False,
                    cycle_ok=True,
                )
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertFalse(data["send_window_open"])
        self.assertEqual(data["mode"], "always_on")
        self.assertEqual(data["active_start"], "08:00")
        self.assertEqual(data["active_end"], "21:00")
        self.assertIn("BTCUSD", data["last_processed_utc"])
        self.assertIn("pid", data)
        blob = json.dumps(data).lower()
        self.assertNotIn("webhook", blob)
        self.assertNotIn("http", blob)

    def test_continuous_loop_writes_heartbeat_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = os.path.join(tmp, "hb.json")
            state_path = os.path.join(tmp, "state.json")

            def fake_scan(*_args, **_kwargs):
                fnlib._stop_requested = True
                return []

            with patch("scanner.HEARTBEAT_PATH", heartbeat), patch(
                "scanner.STATE_PATH", state_path
            ), patch("scanner.scan_live", fake_scan), patch(
                "scanner.send_webhook", side_effect=AssertionError("webhook")
            ), patch.object(sys, "argv", ["scanner.py"]):
                rc = scanner.main()
            data = json.loads(Path(heartbeat).read_text(encoding="utf-8"))
            self.assertTrue(Path(state_path).is_file())
        self.assertEqual(rc, 0)
        self.assertIn("send_window_open", data)
        self.assertEqual(data["mode"], "always_on")

    def test_once_does_not_overwrite_the_service_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = os.path.join(tmp, "hb.json")
            state_path = os.path.join(tmp, "state.json")
            with patch("scanner.HEARTBEAT_PATH", heartbeat), patch(
                "scanner.STATE_PATH", state_path
            ), patch("scanner.scan_live", return_value=[]), patch(
                "scanner.send_webhook", side_effect=AssertionError("webhook")
            ), patch.object(sys, "argv", ["scanner.py", "--once", "--no-send"]):
                rc = scanner.main()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(heartbeat))

    def test_live_rules_in_config_are_unchanged(self) -> None:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        sc = cfg["scanner"]
        self.assertEqual(sc["active_start"], "08:00")
        self.assertEqual(sc["active_end"], "21:00")
        self.assertIs(sc["weekdays_only"], True)
        self.assertEqual(sc["cooldown_minutes"], 90)
        self.assertEqual(sc["max_alerts_per_day"], 12)
        self.assertEqual(sc["max_alerts_per_symbol_per_day"], 2)
        self.assertEqual(sc["rvol_min"], 1.5)
        self.assertEqual(sc["interval_seconds"], 300)
        self.assertEqual(
            [row["symbol"] for row in sc["universe"]],
            [
                "EURUSD",
                "GBPUSD",
                "AUDUSD",
                "USDJPY",
                "XAUUSD",
                "USOUSD",
                "BTCUSD",
                "NAS100",
                "GER40",
                "US30",
            ],
        )

    def test_caps_and_cooldown_stay_at_12_2_and_90(self) -> None:
        sc = live_rules()
        thursday = first_weekday(2026, 11, 3)
        ts = at_local(thursday, 10, 0).timestamp()
        day = scanner.day_key(ts)
        eurusd = {"id": "EURUSD", "symbol": "EURUSD"}
        gbpusd = {"id": "GBPUSD", "symbol": "GBPUSD"}
        state = {"day": day, "sent_today": 0, "sent_today_by_symbol": {}, "last": {}}
        self.assertTrue(scanner.can_send(state, eurusd, "breakout", ts, sc))
        scanner.mark_sent(state, eurusd, "breakout", ts)
        self.assertFalse(scanner.can_send(state, eurusd, "breakout", ts + 89 * 60, sc))
        self.assertTrue(scanner.can_send(state, eurusd, "breakout", ts + 90 * 60, sc))
        self.assertTrue(scanner.can_send(state, eurusd, "retest", ts, sc))
        scanner.mark_sent(state, eurusd, "retest", ts)
        self.assertEqual(state["sent_today"], 2)
        self.assertEqual(state["sent_today_by_symbol"]["EURUSD"], 2)
        self.assertFalse(scanner.can_send(state, eurusd, "exhaustion", ts + 90 * 60, sc))
        self.assertTrue(scanner.can_send(state, gbpusd, "breakout", ts, sc))
        capped = {"day": day, "sent_today": 12, "sent_today_by_symbol": {}, "last": {}}
        self.assertFalse(scanner.can_send(capped, gbpusd, "breakout", ts, sc))
        almost = {"day": day, "sent_today": 11, "sent_today_by_symbol": {"GBPUSD": 1}, "last": {}}
        self.assertTrue(scanner.can_send(almost, gbpusd, "breakout", ts, sc))

    def test_scan_live_stops_at_the_daily_and_symbol_caps(self) -> None:
        thursday = first_weekday(2026, 11, 3)
        close = at_local(thursday, 10, 0)
        fixed = scanner.day_key(close.timestamp())
        with patch("scanner.day_key", return_value=fixed):
            sent, state, _evaluated, _news, fired = self._scan(
                [close],
                close + timedelta(minutes=1),
                sent_today=12,
                day=fixed,
            )
        self.assertEqual(sent, [])
        self.assertEqual(fired, [])
        self.assertEqual(state["sent_today"], 12)
        with patch("scanner.day_key", return_value=fixed):
            sent, state, _evaluated, _news, fired = self._scan(
                [close],
                close + timedelta(minutes=1),
                sent_today=1,
                sent_by={"BTCUSD": 2},
                day=fixed,
            )
        self.assertEqual(sent, [])
        self.assertEqual(fired, [])
        self.assertEqual(state["sent_today_by_symbol"]["BTCUSD"], 2)


class WindowsAlwaysOnTest(unittest.TestCase):
    def test_readme_documents_the_vps_install(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(r"C:\fn-monitor", text)
        self.assertIn(r".venv\Scripts\python.exe", text)
        self.assertIn("install-fn-scanner.ps1", text)
        self.assertIn("scanner_heartbeat.json", text)
        self.assertIn("--no-send", text)
        self.assertIn("08:00", text)
        self.assertIn("21:00", text)
        self.assertIn("FN Scanner Watchdog", text)

    def test_scanner_task_runs_at_boot_without_logon(self) -> None:
        path = WINDOWS / "FN-Scanner.xml"
        root = ET.parse(path).getroot()
        text = path.read_text(encoding="utf-8")
        self.assertIsNotNone(root.find("t:Triggers/t:BootTrigger", TASK_NS))
        self.assertEqual(
            root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=TASK_NS),
            "Password",
        )
        self.assertEqual(
            root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=TASK_NS),
            "PT0S",
        )
        self.assertEqual(
            root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=TASK_NS),
            "IgnoreNew",
        )
        self.assertEqual(
            root.findtext("t:Settings/t:DisallowStartIfOnBatteries", namespaces=TASK_NS),
            "false",
        )
        self.assertEqual(
            root.findtext("t:Settings/t:StopIfGoingOnBatteries", namespaces=TASK_NS),
            "false",
        )
        self.assertEqual(
            root.findtext("t:Settings/t:StartWhenAvailable", namespaces=TASK_NS),
            "true",
        )
        self.assertEqual(
            root.findtext("t:Settings/t:RestartOnFailure/t:Interval", namespaces=TASK_NS),
            "PT1M",
        )
        self.assertIn(r"__INSTALL_DIR__\windows\start-scanner.cmd", text)
        self.assertIn("__RUN_AS__", text)
        self.assertNotIn("AtLogon", text)
        self.assertNotIn("FN_WEBHOOK", text)
        self.assertNotIn("https://", text)

    def test_watchdog_task_checks_every_minute(self) -> None:
        path = WINDOWS / "FN-Scanner-Watchdog.xml"
        root = ET.parse(path).getroot()
        text = path.read_text(encoding="utf-8")
        self.assertIsNotNone(root.find("t:Triggers/t:BootTrigger", TASK_NS))
        self.assertIsNotNone(root.find("t:Triggers/t:CalendarTrigger", TASK_NS))
        intervals = [el.text for el in root.findall(".//t:Interval", TASK_NS)]
        self.assertTrue(intervals)
        self.assertTrue(all(item == "PT1M" for item in intervals))
        self.assertNotIn("PT5M", text)
        self.assertIn("watchdog.ps1", text)
        self.assertIn("powershell.exe", text)
        self.assertIn("-StaleMinutes 12", text)
        self.assertEqual(
            root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=TASK_NS),
            "PT2M",
        )
        self.assertEqual(
            root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=TASK_NS),
            "Password",
        )
        self.assertNotIn("FN_WEBHOOK", text)
        self.assertNotIn("FN_HEALTH", text)
        self.assertNotIn("https://", text)

    def test_starter_uses_the_venv_python(self) -> None:
        text = (WINDOWS / "start-scanner.cmd").read_text(encoding="utf-8")
        self.assertIn(r".venv\Scripts\python.exe", text)
        self.assertIn("scanner.py", text)
        self.assertNotIn("--backfill", text)
        self.assertNotIn("FN_WEBHOOK", text)

    def test_scripts_do_not_call_a_webhook(self) -> None:
        for path in WINDOWS.iterdir():
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("fn_webhook", text, path.name)
            self.assertNotIn("invoke-webrequest", text, path.name)
            self.assertNotIn("invoke-restmethod", text, path.name)
            self.assertNotIn("curl ", text, path.name)
            self.assertNotIn("urllib", text, path.name)

    def test_installer_registers_both_tasks_for_the_venv(self) -> None:
        text = (WINDOWS / "install-fn-scanner.ps1").read_text(encoding="utf-8")
        self.assertIn("FN Scanner", text)
        self.assertIn("FN Scanner Watchdog", text)
        self.assertIn("Register-ScheduledTask", text)
        self.assertIn("Read-Host", text)
        self.assertIn(r".venv\Scripts\python.exe", text)
        self.assertIn("$Uninstall", text)
        self.assertNotIn("FN_WEBHOOK", text)


if __name__ == "__main__":
    unittest.main()
