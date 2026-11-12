#!/usr/bin/env python3
"""Watchdog decisions and the failure-only health webhook.

These tests must not call the network. urllib is patched. A healthy cycle
has to leave that patch unused even when the health URL is configured.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fnlib
import health

ROOT = Path(__file__).resolve().parent
WINDOWS = ROOT / "windows"
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
NOW = 1_700_000_000
STALE = 12 * 60
HEALTH_URL = "https://health.fn-monitor.test/hook"
HEALTH_KEY = "super-secret-health-key"
TRADING_URL = "https://trading.fn-monitor.test/hook"
TRADING_KEY = "trading-secret-value"


class _Resp:
    status = 200

    def read(self, _n=None):
        return b"ok"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class HealthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {}
        for key in (
            "FN_WEBHOOK_URL",
            "FN_WEBHOOK_KEY",
            "FN_HEALTH_WEBHOOK_URL",
            "FN_HEALTH_WEBHOOK_KEY",
        ):
            self._saved[key] = os.environ.pop(key, None)
        self.requests = []

        def fake_open(req, timeout=None):
            self.requests.append((req, timeout))
            return _Resp()

        self.urlopen = patch("urllib.request.urlopen", side_effect=fake_open).start()
        self.trading = patch(
            "fnlib.send_webhook",
            side_effect=AssertionError("trading webhook must not be used for health"),
        ).start()
        self.addCleanup(patch.stopall)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def decide(self, **overrides):
        params = {
            "task_exists": True,
            "task_disabled": False,
            "task_running": True,
            "task_ran_seconds": STALE + 60,
            "process_alive": True,
            "heartbeat": {"unix": NOW - 30, "pid": 42},
            "now": NOW,
            "stale_seconds": STALE,
        }
        params.update(overrides)
        return health.assess(**params)

    def configured(self) -> dict:
        os.environ["FN_HEALTH_WEBHOOK_URL"] = HEALTH_URL
        os.environ["FN_HEALTH_WEBHOOK_KEY"] = HEALTH_KEY
        os.environ["FN_WEBHOOK_URL"] = TRADING_URL
        os.environ["FN_WEBHOOK_KEY"] = TRADING_KEY
        return {"key_header": "Authorization", "key_prefix": "Bearer "}

    def body(self):
        self.assertEqual(len(self.requests), 1)
        req, timeout = self.requests[0]
        return json.loads(req.data.decode("utf-8")), req, timeout


class HealthDecisionTest(HealthTestCase):
    def test_watchdog_restarts_on_stale_heartbeat(self) -> None:
        decision = self.decide(heartbeat={"unix": NOW - 3600, "pid": 42})
        self.assertEqual(decision["action"], "restart")
        self.assertEqual(decision["reason"], health.REASON_STALE)
        self.assertTrue(decision["heartbeat_problem"])
        self.assertEqual(decision["heartbeat_age_seconds"], 3600)
        self.assertEqual(self.requests, [])

    def test_dead_process_restarts_before_the_heartbeat_is_stale(self) -> None:
        decision = self.decide(process_alive=False, task_running=False)
        self.assertEqual(decision["action"], "restart")
        self.assertEqual(decision["reason"], health.REASON_DEAD)
        self.assertFalse(decision["heartbeat_problem"])

    def test_missing_heartbeat_is_its_own_reason(self) -> None:
        decision = self.decide(process_alive=False, task_running=False, heartbeat=None)
        self.assertEqual(decision["action"], "restart")
        self.assertEqual(decision["reason"], health.REASON_MISSING)
        self.assertTrue(decision["heartbeat_problem"])

    def test_fresh_process_is_healthy(self) -> None:
        decision = self.decide()
        self.assertEqual(decision["action"], "ok")
        self.assertFalse(decision["heartbeat_problem"])

    def test_fresh_heartbeat_with_hidden_pid_counts_as_healthy(self) -> None:
        decision = self.decide(process_alive=False, task_running=True)
        self.assertEqual(decision["action"], "ok")

    def test_recent_start_is_not_restarted_for_a_missing_heartbeat(self) -> None:
        decision = self.decide(heartbeat=None, process_alive=False, task_ran_seconds=60)
        self.assertEqual(decision["action"], "starting")
        state = {"armed": False, "last_sent_unix": NOW}
        result = health.settle(decision, self._forbid_restart, {}, state, NOW)
        self.assertEqual(result["status"], "starting")
        self.assertFalse(state["armed"])
        self.assertEqual(self.requests, [])

    def test_grace_ends_at_the_stale_limit(self) -> None:
        fresh_edge = self.decide(heartbeat={"unix": NOW - STALE, "pid": 1})
        self.assertEqual(fresh_edge["action"], "ok")
        expired = self.decide(
            heartbeat={"unix": NOW - 3600, "pid": 1},
            task_ran_seconds=STALE,
        )
        self.assertEqual(expired["action"], "restart")
        self.assertEqual(expired["reason"], health.REASON_STALE)

    def test_small_clock_skew_stays_fresh(self) -> None:
        ok = self.decide(heartbeat={"unix": NOW + health.FRESH_SKEW_SECONDS, "pid": 1})
        self.assertEqual(ok["action"], "ok")
        skewed = self.decide(
            heartbeat={"unix": NOW + health.FRESH_SKEW_SECONDS + 1, "pid": 1},
            task_ran_seconds=STALE + 60,
        )
        self.assertEqual(skewed["action"], "restart")

    def test_disabled_and_missing_task_do_not_restart(self) -> None:
        disabled = self.decide(task_disabled=True, process_alive=False, heartbeat=None)
        missing = self.decide(task_exists=False, process_alive=False, heartbeat=None)
        self.assertEqual(disabled["action"], "disabled")
        self.assertEqual(missing["action"], "missing-task")
        for decision in (disabled, missing):
            result = health.settle(decision, self._forbid_restart, {}, {}, NOW)
            self.assertNotEqual(result["status"], "restart")
        self.assertEqual(self.requests, [])

    def _forbid_restart(self):
        raise AssertionError("restart")


class HealthWebhookTest(HealthTestCase):
    def test_no_health_webhook_on_a_healthy_cycle(self) -> None:
        cfg = self.configured()
        decision = self.decide()
        self.assertEqual(decision["action"], "ok")
        state = {"armed": False, "last_sent_unix": NOW - 10}
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = health.settle(decision, self._forbid_restart, cfg, state, NOW)
        self.assertEqual(result["status"], "healthy")
        self.assertFalse(result["notified"])
        self.assertIsNone(result["event"])
        self.assertTrue(state["armed"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.trading.call_count, 0)
        blob = out.getvalue() + err.getvalue()
        self.assertNotIn(HEALTH_KEY, blob)
        self.assertNotIn(HEALTH_URL, blob)
        self.assertNotIn(TRADING_KEY, blob)

    def test_health_webhook_on_restart(self) -> None:
        cfg = self.configured()
        decision = self.decide(heartbeat={"unix": NOW - 3600, "pid": 7})
        calls = []
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = health.settle(
                decision, lambda: calls.append("restart") or True, cfg, {}, NOW
            )
        self.assertEqual(calls, ["restart"])
        self.assertEqual(result["event"], "restarted")
        self.assertTrue(result["notified"])
        body, req, timeout = self.body()
        self.assertEqual(body["event"], "restarted")
        self.assertEqual(body["reason"], health.REASON_STALE)
        self.assertEqual(body["unix"], NOW)
        self.assertEqual(body["detected_unix"], NOW)
        self.assertRegex(body["utc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(body["detected_utc"], body["utc"])
        self.assertEqual(body["heartbeat_age_seconds"], 3600)
        self.assertEqual(body["source"], "fn-scanner-watchdog")
        self.assertEqual(body["task"], "FN Scanner")
        self.assertNotIn("symbol", body)
        self.assertNotIn("direction", body)
        self.assertEqual(timeout, health.HEALTH_TIMEOUT_SECONDS)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {HEALTH_KEY}")
        self.assertNotIn(HEALTH_KEY, req.data.decode("utf-8"))
        self.assertNotIn(TRADING_KEY, req.get_header("Authorization") or "")
        self.assertIn("health.fn-monitor.test", req.full_url)
        self.assertNotIn("trading.fn-monitor.test", req.full_url)
        blob = out.getvalue() + err.getvalue()
        self.assertNotIn(HEALTH_KEY, blob)
        self.assertNotIn(HEALTH_URL, blob)
        self.assertEqual(self.trading.call_count, 0)

    def test_health_webhook_on_failed_restart(self) -> None:
        cfg = self.configured()
        decision = self.decide(process_alive=False, task_running=False)
        self.assertEqual(decision["reason"], health.REASON_DEAD)
        result = health.settle(decision, lambda: False, cfg, {}, NOW)
        self.assertEqual(result["event"], "restart_failed")
        self.assertFalse(result["restart_ok"])
        body, _req, _timeout = self.body()
        self.assertEqual(body["event"], "restart_failed")
        self.assertEqual(body["reason"], health.REASON_DEAD)

    def test_stale_heartbeat_and_failed_recovery_use_heartbeat_stale(self) -> None:
        cfg = self.configured()
        decision = self.decide(process_alive=False, task_running=False, heartbeat=None)
        self.assertEqual(decision["reason"], health.REASON_MISSING)

        def boom():
            raise OSError(f"denied {HEALTH_URL} {HEALTH_KEY}")

        err = io.StringIO()
        with redirect_stderr(err):
            result = health.settle(decision, boom, cfg, {}, NOW)
        self.assertEqual(result["event"], "heartbeat_stale")
        self.assertFalse(result["restart_ok"])
        body, _req, _timeout = self.body()
        self.assertEqual(body["event"], "heartbeat_stale")
        self.assertEqual(body["reason"], health.REASON_MISSING)
        blob = json.dumps(body) + err.getvalue()
        self.assertNotIn(HEALTH_KEY, blob)
        self.assertNotIn(HEALTH_URL, blob)
        self.assertNotIn("https://", json.dumps(body))

    def test_missing_health_url_does_not_crash_or_call_trading_webhook(self) -> None:
        os.environ["FN_WEBHOOK_URL"] = TRADING_URL
        os.environ["FN_WEBHOOK_KEY"] = TRADING_KEY
        cfg = {
            "webhook_url_env": "FN_WEBHOOK_URL",
            "webhook_key_env": "FN_WEBHOOK_KEY",
            "key_header": "Authorization",
            "key_prefix": "Bearer ",
        }
        decision = self.decide(heartbeat={"unix": NOW - 3600, "pid": 1})
        result = health.settle(decision, lambda: True, cfg, {}, NOW)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["event"], "restarted")
        self.assertTrue(result["restart_ok"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.trading.call_count, 0)

    def test_config_value_is_an_env_name_not_a_url(self) -> None:
        cfg = {
            "health_webhook_url_env": HEALTH_URL,
            "health_webhook_key_env": HEALTH_KEY,
        }
        status = health.post_health(cfg, "restarted", health.REASON_STALE, NOW, "FN Scanner", 12)
        self.assertEqual(status, "disabled")
        self.assertEqual(self.requests, [])

    def test_placeholder_url_stays_disabled(self) -> None:
        os.environ["FN_HEALTH_WEBHOOK_URL"] = "https://PASTE_URL_HERE/hook"
        os.environ["FN_HEALTH_WEBHOOK_KEY"] = HEALTH_KEY
        status = health.post_health({}, "restart_failed", health.REASON_DEAD, NOW, "FN Scanner", None)
        self.assertEqual(status, "disabled")
        self.assertEqual(self.requests, [])

    def test_health_header_uses_the_trading_header_style(self) -> None:
        os.environ["FN_HEALTH_WEBHOOK_URL"] = HEALTH_URL
        os.environ["FN_HEALTH_WEBHOOK_KEY"] = HEALTH_KEY
        cfg = {"key_header": "X-Test-Key", "key_prefix": ""}
        health.post_health(cfg, "restarted", health.REASON_DEAD, NOW, "FN Scanner", None)
        _body, req, _timeout = self.body()
        self.assertEqual(req.get_header("X-test-key"), HEALTH_KEY)

    def test_http_error_redacts_secrets_and_does_not_raise(self) -> None:
        os.environ["FN_HEALTH_WEBHOOK_URL"] = HEALTH_URL
        os.environ["FN_HEALTH_WEBHOOK_KEY"] = HEALTH_KEY

        def boom(req, timeout=None):
            self.requests.append((req, timeout))
            raise urllib.error.HTTPError(
                HEALTH_URL,
                500,
                "bad",
                hdrs={},
                fp=io.BytesIO(f"echo {HEALTH_KEY} {HEALTH_URL}".encode("utf-8")),
            )

        self.urlopen.side_effect = boom
        err = io.StringIO()
        with redirect_stderr(err):
            status = health.post_health({}, "restarted", health.REASON_STALE, NOW, "FN Scanner", 90)
        self.assertEqual(status, "failed")
        logged = err.getvalue()
        self.assertNotIn(HEALTH_KEY, logged)
        self.assertNotIn(HEALTH_URL, logged)
        self.assertNotIn(HEALTH_KEY, status)

    def test_failed_delivery_is_not_retried_every_minute(self) -> None:
        cfg = self.configured()

        def boom(req, timeout=None):
            self.requests.append((req, timeout))
            raise urllib.error.URLError(f"fail {HEALTH_KEY} {HEALTH_URL}")

        self.urlopen.side_effect = boom
        decision = self.decide(heartbeat={"unix": NOW - 3600, "pid": 1})
        state = {}
        err = io.StringIO()
        with redirect_stderr(err):
            first = health.settle(decision, lambda: True, cfg, state, NOW)
            second = health.settle(decision, lambda: True, cfg, state, NOW + 60)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "suppressed")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(state["armed"])
        self.assertNotIn(HEALTH_KEY, err.getvalue())
        self.assertNotIn(HEALTH_URL, err.getvalue())

    def test_suppressed_webhook_still_restarts(self) -> None:
        cfg = self.configured()
        decision = self.decide(heartbeat={"unix": NOW - 3600, "pid": 1})
        calls = []
        state = {"armed": False, "last_sent_unix": NOW - 10}
        result = health.settle(
            decision, lambda: calls.append(1) or True, cfg, state, NOW, gap=1800
        )
        self.assertEqual(calls, [1])
        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(result["event"], "restarted")
        self.assertEqual(self.requests, [])

    def test_healthy_cycle_arms_the_next_failure(self) -> None:
        cfg = self.configured()
        stale = self.decide(heartbeat={"unix": NOW - 3600, "pid": 1})
        calls = []
        state = {}
        health.settle(stale, lambda: calls.append(1) or True, cfg, state, NOW)
        self.assertEqual(len(self.requests), 1)
        health.settle(stale, lambda: calls.append(1) or True, cfg, state, NOW + 60)
        self.assertEqual(len(self.requests), 1)
        health.settle(self.decide(), self._forbid_restart, cfg, state, NOW + 120)
        health.settle(stale, lambda: calls.append(1) or True, cfg, state, NOW + 180)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(json.loads(self.requests[-1][0].data.decode())["event"], "restarted")

    def test_reminder_after_the_gap_while_still_down(self) -> None:
        cfg = self.configured()
        decision = self.decide(heartbeat={"unix": NOW - 3600, "pid": 1})
        state = {"armed": False, "last_sent_unix": NOW - health.NOTIFY_GAP_SECONDS}
        result = health.settle(decision, lambda: True, cfg, state, NOW)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(health.NOTIFY_GAP_SECONDS, 30 * 60)
        self.assertEqual(health.HEALTH_EVENTS, ("restarted", "restart_failed", "heartbeat_stale"))

    def test_decide_cli_stale_heartbeat_requests_restart_without_network(self) -> None:
        self.configured()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scanner_heartbeat.json")
            Path(path).write_text(
                json.dumps({"unix": NOW - 3600, "pid": 7}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                rc = health.main(
                    [
                        "decide",
                        "--heartbeat",
                        path,
                        "--process-alive",
                        "1",
                        "--task-exists",
                        "1",
                        "--task-running",
                        "1",
                        "--task-ran-seconds",
                        str(STALE + 60),
                        "--stale-minutes",
                        "12",
                        "--now",
                        str(NOW),
                    ]
                )
        self.assertEqual(rc, 0)
        decision = json.loads(buf.getvalue())
        self.assertEqual(decision["action"], "restart")
        self.assertEqual(decision["reason"], health.REASON_STALE)
        self.assertEqual(self.requests, [])
        self.assertNotIn(HEALTH_KEY, buf.getvalue() + err.getvalue())

    def test_decide_cli_healthy_and_zero_stale_minutes_do_not_send(self) -> None:
        self.configured()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hb.json")
            Path(path).write_text(json.dumps({"unix": NOW - 30, "pid": 3}), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = health.main(
                    [
                        "decide",
                        "--heartbeat",
                        path,
                        "--process-alive",
                        "1",
                        "--task-running",
                        "1",
                        "--stale-minutes",
                        "0",
                        "--now",
                        str(NOW),
                    ]
                )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["action"], "ok")
        self.assertEqual(self.requests, [])
        self.assertNotIn(HEALTH_URL, buf.getvalue())

    def test_invalid_heartbeat_file_means_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scanner_heartbeat.json")
            Path(path).write_text("{", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = health.main(
                    [
                        "decide",
                        "--heartbeat",
                        path,
                        "--process-alive",
                        "0",
                        "--task-exists",
                        "1",
                        "--task-running",
                        "0",
                        "--stale-minutes",
                        "12",
                        "--now",
                        str(NOW),
                    ]
                )
        self.assertEqual(rc, 0)
        decision = json.loads(buf.getvalue())
        self.assertEqual(decision["action"], "restart")
        self.assertEqual(decision["reason"], health.REASON_MISSING)

    def test_notify_cli_restart_failed_and_repeat_is_suppressed(self) -> None:
        self.configured()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "watchdog_notify.json")
            buf = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                rc = health.main(
                    [
                        "notify",
                        "--restart-ok",
                        "0",
                        "--heartbeat-problem",
                        "0",
                        "--reason=ni živega procesa",
                        "--state",
                        state_path,
                        "--now",
                        str(NOW),
                        "--task",
                        "FN Scanner",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn("sent", buf.getvalue())
            body, _req, _timeout = self.body()
            self.assertEqual(body["event"], "restart_failed")
            self.assertEqual(body["reason"], health.REASON_DEAD)
            saved = json.loads(Path(state_path).read_text(encoding="utf-8"))
            self.assertFalse(saved["armed"])
            self.requests.clear()
            again = io.StringIO()
            with redirect_stdout(again):
                rc = health.main(
                    [
                        "notify",
                        "--restart-ok",
                        "1",
                        "--heartbeat-problem",
                        "1",
                        "--reason",
                        health.REASON_STALE,
                        "--state",
                        state_path,
                        "--now",
                        str(NOW + 60),
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn("suppressed", again.getvalue())
            self.assertEqual(self.requests, [])
            self.assertNotIn(HEALTH_KEY, buf.getvalue() + err.getvalue() + again.getvalue())

    def test_note_healthy_cli_is_local(self) -> None:
        self.configured()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "watchdog_notify.json")
            Path(state_path).write_text(
                json.dumps({"armed": False, "last_sent_unix": NOW}),
                encoding="utf-8",
            )
            rc = health.main(["note-healthy", "--state", state_path])
            saved = json.loads(Path(state_path).read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertTrue(saved["armed"])
        self.assertEqual(self.requests, [])

    def test_missing_config_still_disables_cleanly(self) -> None:
        with patch("fnlib.load_config", side_effect=SystemExit("missing")):
            cfg = health.load_health_cfg()
        self.assertEqual(cfg["health_webhook_url_env"], "FN_HEALTH_WEBHOOK_URL")
        self.assertEqual(cfg["health_webhook_key_env"], "FN_HEALTH_WEBHOOK_KEY")
        status = health.post_health(cfg, "restarted", health.REASON_DEAD, NOW, "FN Scanner", None)
        self.assertEqual(status, "disabled")
        self.assertEqual(self.requests, [])

    def _forbid_restart(self):
        raise AssertionError("restart")


class HealthInstallTest(HealthTestCase):
    def test_watchdog_script_restarts_then_notifies(self) -> None:
        text = (WINDOWS / "watchdog.ps1").read_text(encoding="utf-8-sig")
        for reason in (health.REASON_STALE, health.REASON_MISSING, health.REASON_DEAD):
            self.assertIn(reason, text)
        for flag in (
            "decide",
            "--heartbeat",
            "--process-alive",
            "--task-exists",
            "--task-disabled",
            "--task-running",
            "--stale-minutes",
            "--task-ran-seconds",
            "note-healthy",
            "--restart-ok",
            "--heartbeat-problem",
            "--heartbeat-age",
            "health.py",
            "scanner\\.py",
            "--backfill",
            "--once",
        ):
            self.assertIn(flag, text)
        self.assertEqual(text.count("note-healthy"), 1)
        self.assertEqual(text.count("--restart-ok"), 1)
        self.assertLess(text.index('State -eq "Disabled"'), text.index('"decide"'))
        self.assertLess(text.index("note-healthy"), text.index("Start-ScheduledTask"))
        self.assertLess(text.index("Start-ScheduledTask"), text.index("--restart-ok"))
        self.assertNotIn("Invoke-RestMethod", text)
        self.assertNotIn("Invoke-WebRequest", text)
        self.assertNotIn("FN_WEBHOOK", text)
        self.assertNotIn("FN_HEALTH", text)
        self.assertNotIn("https://", text)

    def test_watchdog_task_is_one_minute_and_has_no_secret(self) -> None:
        path = WINDOWS / "FN-Scanner-Watchdog.xml"
        text = path.read_text(encoding="utf-8")
        root = ET.parse(path).getroot()
        intervals = [el.text for el in root.findall(".//t:Interval", TASK_NS)]
        self.assertEqual(intervals, ["PT1M", "PT1M", "PT1M"])
        self.assertIn("-StaleMinutes 12", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("FN_WEBHOOK", text)
        self.assertNotIn("FN_HEALTH", text)

    def test_installer_copies_scripts_and_restarts_the_process(self) -> None:
        text = (WINDOWS / "install-fn-scanner.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("health.py", text)
        self.assertIn("Stop-ScheduledTask", text)
        self.assertIn("config.json, scanner_state.json in .venv ne prepisuj.", text)
        self.assertIn("FN_HEALTH_WEBHOOK_URL", text)
        self.assertIn("FN_HEALTH_WEBHOOK_KEY", text)
        self.assertIn("vsako minuto", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("FN_WEBHOOK_URL", text)
        self.assertNotIn("FN_WEBHOOK_KEY", text)

    def test_readme_describes_local_checks_and_the_opt_in_webhook(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("vsako minuto", text)
        self.assertIn("health.py", text)
        self.assertIn("FN_HEALTH_WEBHOOK_URL", text)
        self.assertIn("FN_HEALTH_WEBHOOK_KEY", text)
        self.assertIn("restarted", text)
        self.assertIn("restart_failed", text)
        self.assertIn("heartbeat_stale", text)
        self.assertIn("config.json`, `scanner_state.json` in `.venv` ne prepisuj.", text)
        self.assertIn("30 minut", text)
        self.assertIn("08:00", text)
        self.assertIn("21:00", text)

    def test_config_health_keys_are_names_and_trading_rules_stay(self) -> None:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["webhook_url_env"], "FN_WEBHOOK_URL")
        self.assertEqual(cfg["webhook_key_env"], "FN_WEBHOOK_KEY")
        self.assertEqual(cfg["health_webhook_url_env"], "FN_HEALTH_WEBHOOK_URL")
        self.assertEqual(cfg["health_webhook_key_env"], "FN_HEALTH_WEBHOOK_KEY")
        blob = json.dumps(cfg)
        self.assertNotIn("https://", blob)
        self.assertEqual(cfg["key_prefix"], "Bearer ")
        self.assertNotIn("https://", cfg["health_webhook_url_env"])
        self.assertNotIn("https://", cfg["health_webhook_key_env"])
        sc = cfg["scanner"]
        self.assertEqual(sc["max_alerts_per_day"], 12)
        self.assertEqual(sc["max_alerts_per_symbol_per_day"], 2)
        self.assertEqual(sc["cooldown_minutes"], 90)
        self.assertEqual(sc["rvol_min"], 1.5)
        self.assertEqual(sc["active_start"], "08:00")
        self.assertEqual(sc["active_end"], "21:00")
        self.assertIs(sc["weekdays_only"], True)

    def test_scanner_does_not_send_health_events(self) -> None:
        text = (ROOT / "scanner.py").read_text(encoding="utf-8")
        self.assertNotIn("health.py", text)
        self.assertNotIn("FN_HEALTH", text)
        self.assertNotIn("heartbeat_stale", text)
        self.assertNotIn("restart_failed", text)

    def test_old_config_without_health_keys_stays_disabled(self) -> None:
        os.environ["FN_WEBHOOK_URL"] = TRADING_URL
        os.environ["FN_WEBHOOK_KEY"] = TRADING_KEY
        cfg = fnlib.load_config()
        # Simulate the file that is already on the VPS: no health keys.
        cfg.pop("health_webhook_url_env", None)
        cfg.pop("health_webhook_key_env", None)
        status = health.post_health(cfg, "restarted", health.REASON_STALE, NOW, "FN Scanner", 400)
        self.assertEqual(status, "disabled")
        self.assertEqual(self.requests, [])
        self.assertEqual(fnlib.health_webhook_url(cfg), "")
        self.assertEqual(fnlib.health_webhook_key(cfg), "")


if __name__ == "__main__":
    unittest.main()
