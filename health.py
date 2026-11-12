#!/usr/bin/env python3
"""Local scanner health check and failure-only webhook.

The watchdog calls `decide` on every pass. That command only reads the
heartbeat file and prints a JSON decision. It never opens a network
connection.

`notify` runs only after a restart attempt. It posts a small JSON body when
FN_HEALTH_WEBHOOK_URL and FN_HEALTH_WEBHOOK_KEY are both set. Until then it
prints `disabled` and does not call the trading webhook. A missing URL does
not raise.

Healthy passes call `note-healthy`, which only updates a local file so the
next real failure can notify again. The same failure is not posted every
minute.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import fnlib

HEALTH_EVENTS = ("restarted", "restart_failed", "heartbeat_stale")
REASON_STALE = "heartbeat je prestar"
REASON_MISSING = "heartbeat manjka"
REASON_DEAD = "ni živega procesa"
DEFAULT_STALE_MINUTES = 12
NOTIFY_GAP_SECONDS = 30 * 60
FRESH_SKEW_SECONDS = 120
HEALTH_TIMEOUT_SECONDS = 10
TASK_NAME = "FN Scanner"

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


def sanitize(text: str, *secrets: str) -> str:
    """Drop URLs, bearer tokens, and any secret that was passed in."""
    cleaned = (text or "").replace("\r", " ").replace("\n", " ")
    cleaned = _URL_RE.sub("[url]", cleaned)
    cleaned = _BEARER_RE.sub("Bearer [redacted]", cleaned)
    for secret in secrets:
        if secret and len(secret) >= 8:
            cleaned = cleaned.replace(secret, "[redacted]")
    return " ".join(cleaned.split())


def _log_failure(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def heartbeat_age_seconds(heartbeat: dict[str, Any] | None, now: float) -> float | None:
    if not isinstance(heartbeat, dict):
        return None
    raw = heartbeat.get("unix")
    if raw is None or raw == "":
        return None
    try:
        return float(now) - float(raw)
    except (TypeError, ValueError):
        return None


def load_heartbeat(path: str) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        data = fnlib.load_json(path, None)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def stale_seconds_from_minutes(minutes: float) -> float:
    if minutes <= 0:
        minutes = float(DEFAULT_STALE_MINUTES)
    return float(minutes) * 60.0


def _ran_seconds(value: float | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def assess(
    *,
    task_exists: bool,
    task_disabled: bool = False,
    task_running: bool = False,
    task_ran_seconds: float | None = None,
    process_alive: bool = False,
    heartbeat: dict[str, Any] | None = None,
    now: float,
    stale_seconds: float,
) -> dict[str, Any]:
    """Decide what the watchdog should do. No files, no network.

    `starting` is the grace period after a task start, before a heartbeat
    exists. A disabled task is left alone.
    """
    age = heartbeat_age_seconds(heartbeat, now)
    age_out = None if age is None else int(age)
    fresh = age is not None and -FRESH_SKEW_SECONDS <= age <= stale_seconds
    problem = not fresh
    ran = _ran_seconds(task_ran_seconds)

    def decision(action: str, reason: str, detail: str = "") -> dict[str, Any]:
        return {
            "action": action,
            "reason": reason,
            "detail": detail,
            "heartbeat_problem": problem if action != "disabled" and action != "missing-task" else False,
            "heartbeat_age_seconds": age_out,
        }

    if not task_exists:
        return decision("missing-task", "opravilo ne obstaja")
    if task_disabled:
        return decision("disabled", "")
    if process_alive and fresh:
        return decision("ok", "", "process")
    if fresh and task_running:
        return decision("ok", "", "task running")
    if (not fresh) and task_running and ran is not None and 0 <= ran < stale_seconds:
        return decision("starting", "")
    if not fresh and heartbeat is None:
        reason = REASON_MISSING
    elif not fresh:
        reason = REASON_STALE
    else:
        reason = REASON_DEAD
    return decision("restart", reason)


def classify(restart_ok: bool, heartbeat_problem: bool) -> str:
    if restart_ok:
        return "restarted"
    if heartbeat_problem:
        return "heartbeat_stale"
    return "restart_failed"


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(
    event: str,
    reason: str,
    *,
    now: float,
    task: str = TASK_NAME,
    age: int | None = None,
) -> dict[str, Any]:
    stamp = iso_utc(now)
    unix = int(now)
    safe_reason = sanitize(reason)[:300] or "neznano"
    safe_task = sanitize(task)[:80] or TASK_NAME
    return {
        "event": event,
        "reason": safe_reason,
        "utc": stamp,
        "unix": unix,
        "detected_utc": stamp,
        "detected_unix": unix,
        "heartbeat_age_seconds": age,
        "source": "fn-scanner-watchdog",
        "task": safe_task,
    }


def load_health_cfg() -> dict[str, Any]:
    try:
        cfg = fnlib.load_config()
    except (Exception, SystemExit):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("key_header", "Authorization")
    cfg.setdefault("key_prefix", "Bearer ")
    cfg.setdefault("health_webhook_url_env", "FN_HEALTH_WEBHOOK_URL")
    cfg.setdefault("health_webhook_key_env", "FN_HEALTH_WEBHOOK_KEY")
    return cfg


def post_health(
    cfg: dict[str, Any],
    event: str,
    reason: str,
    now: float,
    task: str,
    age: int | None,
) -> str:
    """Post one health event. Returns sent, failed, or disabled.

    Never raises. Never logs the URL or the key. Does not call the trading
    webhook when the health URL is unset.
    """
    url = fnlib.health_webhook_url(cfg)
    key = fnlib.health_webhook_key(cfg)
    if event not in HEALTH_EVENTS:
        return "failed"
    if fnlib.webhook_is_placeholder(url) or not key:
        return "disabled"

    payload = build_payload(event, sanitize(reason, url, key), now=now, task=task, age=age)
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "fn-monitor/1.0",
        "Accept": "application/json",
    }
    header_name = (cfg.get("key_header") or "Authorization").strip() or "Authorization"
    prefix = cfg.get("key_prefix")
    if prefix is None:
        prefix = "Bearer "
    headers[header_name] = f"{prefix}{key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_SECONDS) as resp:
            resp.read(256)
        return "sent"
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            raw = ""
        _log_failure(sanitize(f"HTTP {exc.code} {raw}", url, key))
        return "failed"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log_failure(sanitize(f"omrežna napaka: {exc}", url, key))
        return "failed"


def notify_allowed(state: dict[str, Any], now: float, gap: float) -> bool:
    if state.get("armed", True):
        return True
    try:
        last = float(state.get("last_sent_unix") or 0)
    except (TypeError, ValueError):
        return True
    return (float(now) - last) >= float(gap)


def mark_sent(state: dict[str, Any], event: str, reason: str, now: float) -> None:
    state["armed"] = False
    state["last_sent_unix"] = float(now)
    state["last_event"] = event
    state["last_reason"] = sanitize(reason)[:300]


def mark_recovered(state: dict[str, Any]) -> None:
    state["armed"] = True


def load_notify_state(path: str) -> dict[str, Any]:
    try:
        data = fnlib.load_json(path, {})
    except (OSError, json.JSONDecodeError, ValueError):
        return {"armed": True}
    if not isinstance(data, dict):
        return {"armed": True}
    data.setdefault("armed", True)
    return data


def deliver(
    cfg: dict[str, Any],
    state: dict[str, Any],
    event: str,
    reason: str,
    now: float,
    *,
    gap: float = NOTIFY_GAP_SECONDS,
    task: str = TASK_NAME,
    age: int | None = None,
) -> str:
    """Send unless this incident was already reported. Does not restart."""
    if event not in HEALTH_EVENTS:
        return "failed"
    if not notify_allowed(state, now, gap):
        return "suppressed"
    status = post_health(cfg, event, reason, now, task, age)
    if status != "disabled":
        mark_sent(state, event, reason, now)
    return status


def settle(
    decision: dict[str, Any],
    restart,
    cfg: dict[str, Any],
    state: dict[str, Any],
    now: float,
    *,
    gap: float = NOTIFY_GAP_SECONDS,
    task: str = TASK_NAME,
) -> dict[str, Any]:
    """Apply one watchdog decision.

    A healthy reading does not call `restart` and does not touch the network.
    A restart is attempted even when the webhook will be suppressed.
    """
    action = decision.get("action")
    if action == "ok":
        mark_recovered(state)
        return {"status": "healthy", "notified": False, "event": None, "restart_ok": None}
    if action != "restart":
        return {"status": action, "notified": False, "event": None, "restart_ok": None}
    restart_ok = False
    try:
        restart_ok = bool(restart())
    except Exception:
        restart_ok = False
    event = classify(restart_ok, bool(decision.get("heartbeat_problem")))
    age = decision.get("heartbeat_age_seconds")
    if age is not None:
        try:
            age = int(age)
        except (TypeError, ValueError):
            age = None
    status = deliver(
        cfg,
        state,
        event,
        str(decision.get("reason") or ""),
        now,
        gap=gap,
        task=task,
        age=age,
    )
    return {
        "status": status,
        "notified": status == "sent",
        "event": event,
        "restart_ok": restart_ok,
    }


def _flag(value: str) -> bool:
    return value == "1"


def _optional_float(value: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _optional_age(value: str) -> int | None:
    number = _optional_float(value)
    if number is None:
        return None
    return int(number)


def _cmd_decide(args: argparse.Namespace) -> int:
    heartbeat = load_heartbeat(args.heartbeat) if args.heartbeat else None
    now = time.time() if args.now is None else float(args.now)
    decision = assess(
        task_exists=_flag(args.task_exists),
        task_disabled=_flag(args.task_disabled),
        task_running=_flag(args.task_running),
        task_ran_seconds=_optional_float(args.task_ran_seconds),
        process_alive=_flag(args.process_alive),
        heartbeat=heartbeat,
        now=now,
        stale_seconds=stale_seconds_from_minutes(float(args.stale_minutes)),
    )
    sys.stdout.write(json.dumps(decision, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


def _cmd_note_healthy(args: argparse.Namespace) -> int:
    state = load_notify_state(args.state)
    mark_recovered(state)
    try:
        fnlib.save_json(args.state, state)
    except OSError:
        _log_failure("stanje obvestil se ni shranilo")
    return 0


def _cmd_notify(args: argparse.Namespace) -> int:
    state = load_notify_state(args.state)
    now = time.time() if args.now is None else float(args.now)
    event = classify(_flag(args.restart_ok), _flag(args.heartbeat_problem))
    status = deliver(
        load_health_cfg(),
        state,
        event,
        args.reason or "",
        now,
        gap=float(args.gap_seconds),
        task=args.task or TASK_NAME,
        age=_optional_age(args.heartbeat_age),
    )
    try:
        fnlib.save_json(args.state, state)
    except OSError:
        _log_failure("stanje obvestil se ni shranilo")
    sys.stdout.write(status + "\n")
    sys.stdout.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lokalni nadzor skenerja. Webhook samo ob napaki.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    decide = sub.add_parser("decide", help="Lokalna odločitev, brez omrežja.")
    decide.add_argument("--heartbeat", default="")
    decide.add_argument("--process-alive", choices=("0", "1"), required=True)
    decide.add_argument("--task-exists", choices=("0", "1"), default="1")
    decide.add_argument("--task-disabled", choices=("0", "1"), default="0")
    decide.add_argument("--task-running", choices=("0", "1"), default="0")
    decide.add_argument("--task-ran-seconds", default="")
    decide.add_argument("--stale-minutes", type=float, default=DEFAULT_STALE_MINUTES)
    decide.add_argument("--now", type=float, default=None)
    decide.set_defaults(func=_cmd_decide)

    note = sub.add_parser("note-healthy", help="Lokalno zabeleži zdrav krog.")
    note.add_argument("--state", required=True)
    note.set_defaults(func=_cmd_note_healthy)

    notify = sub.add_parser("notify", help="Webhook samo po poskusu zagona.")
    notify.add_argument("--restart-ok", choices=("0", "1"), required=True)
    notify.add_argument("--heartbeat-problem", choices=("0", "1"), required=True)
    notify.add_argument("--reason", default="")
    notify.add_argument("--state", required=True)
    notify.add_argument("--task", default=TASK_NAME)
    notify.add_argument("--now", type=float, default=None)
    notify.add_argument("--gap-seconds", type=float, default=NOTIFY_GAP_SECONDS)
    notify.add_argument("--heartbeat-age", default="")
    notify.set_defaults(func=_cmd_notify)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception:
        _log_failure("ukaz ni uspel")
        return 1


if __name__ == "__main__":
    sys.exit(main())
