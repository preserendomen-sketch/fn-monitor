#!/usr/bin/env python3
"""FundedNext pending-order monitor.

Polls free public feeds every minute, compares them to pending orders in
config.json, and POSTs a webhook when a setup event fires. The webhook URL
and key are read from FN_WEBHOOK_URL and FN_WEBHOOK_KEY. State is stored in
state.json so a restart does not resend alerts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from typing import Any

import fnlib
from fnlib import (
    CONFIG_PATH,
    SCRIPT_DIR,
    FetchError,
    fmt_price,
    last_price_snapshot,
    load_config,
    install_stop_signals,
    load_json,
    log,
    save_json,
    send_webhook,
    SKIPPED_MISSING_SECRETS,
    sleep_secs,
    stop_requested,
    utc_now_iso,
    webhook_key,
    webhook_url,
)

STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")

ONESHOT_EVENTS = {
    "triggered",
    "m15_close_against",
    "halfway_to_tp",
    "sl_hit",
    "tp_hit",
}
COOLDOWN_EVENTS = {"near_entry", "near_sl"}

NEAR_ENTRY_FRAC = 0.25
M15_AGAINST_FRAC = 0.30
NEAR_SL_FRAC = 0.70
HALFWAY_TP_FRAC = 0.50

EVENT_NOTES = {
    "near_entry": "Cena je blizu vstopa (zadnjih 25 % razdalje do vstopa).",
    "triggered": "Limit je verjetno sprožen: cena je dosegla vstopno raven.",
    "m15_close_against": (
        "POZOR, predčasen izhod: zadnja zaključena M15 sveča je zaprla "
        "čez vstop v nasprotno smer (več kot 30 % razdalje do SL)."
    ),
    "near_sl": "Cena je naredila 70 % poti do SL.",
    "sl_hit": "Cena je dosegla SL.",
    "tp_hit": "Cena je dosegla TP.",
    "halfway_to_tp": "Cena je na polovici poti do TP.",
}


def prepare_orders(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    orders = cfg.get("orders") or []
    if not orders:
        raise SystemExit("config.json nima nobenega naloga v polju 'orders'.")
    for order in orders:
        order.setdefault("id", order.get("symbol"))
        order.setdefault("offset", 0)
        order["side"] = str(order.get("side", "sell")).strip().lower()
        if order["side"] not in ("sell", "buy"):
            raise SystemExit(f"Neveljavna stran (side) pri {order['id']}: {order['side']}")
        for field in ("entry", "sl", "tp", "offset"):
            order[field] = float(order[field])
    return orders


def risk_distance(order: dict[str, Any]) -> float:
    dist = abs(float(order["sl"]) - float(order["entry"]))
    if dist <= 0:
        raise ValueError(f"SL in vstop sta enaka pri {order.get('id')}")
    return dist


def detect_events(order: dict[str, Any], market: dict[str, Any], state: dict[str, Any]) -> list[str]:
    side = order["side"]
    entry = float(order["entry"])
    sl = float(order["sl"])
    tp = float(order["tp"])
    dist = risk_distance(order)
    price = market["price"]
    high = market["recent_high"]
    low = market["recent_low"]
    m15_close = market["m15_close"]
    already_triggered = bool(state.get("triggered"))

    events: list[str] = []

    if side == "sell":
        triggered_now = high >= entry
        near_entry = (not already_triggered) and (not triggered_now) and (
            price >= entry - NEAR_ENTRY_FRAC * dist
        )
        sl_hit = high >= sl
        tp_hit = already_triggered and (low <= tp)
        near_sl = already_triggered and (not sl_hit) and (price >= entry + NEAR_SL_FRAC * dist)
        halfway = already_triggered and (not tp_hit) and (
            price <= entry - HALFWAY_TP_FRAC * abs(entry - tp)
        )
        m15_against = (
            already_triggered
            and m15_close is not None
            and m15_close > entry + M15_AGAINST_FRAC * dist
        )
    else:
        triggered_now = low <= entry
        near_entry = (not already_triggered) and (not triggered_now) and (
            price <= entry + NEAR_ENTRY_FRAC * dist
        )
        sl_hit = low <= sl
        tp_hit = already_triggered and (high >= tp)
        near_sl = already_triggered and (not sl_hit) and (price <= entry - NEAR_SL_FRAC * dist)
        halfway = already_triggered and (not tp_hit) and (
            price >= entry + HALFWAY_TP_FRAC * abs(tp - entry)
        )
        m15_against = (
            already_triggered
            and m15_close is not None
            and m15_close < entry - M15_AGAINST_FRAC * dist
        )

    if near_entry:
        events.append("near_entry")
    if triggered_now and not already_triggered:
        events.append("triggered")
    if m15_against:
        events.append("m15_close_against")
    if halfway:
        events.append("halfway_to_tp")
    if near_sl:
        events.append("near_sl")
    if sl_hit and already_triggered:
        events.append("sl_hit")
    if tp_hit:
        events.append("tp_hit")
    if sl_hit and triggered_now and not already_triggered:
        if "sl_hit" not in events:
            events.append("sl_hit")
    return events


def order_fingerprint(order: dict[str, Any]) -> str:
    return json.dumps(
        {
            "side": order["side"],
            "entry": order["entry"],
            "sl": order["sl"],
            "tp": order["tp"],
            "offset": order.get("offset", 0),
            "ticker": order.get("ticker"),
        },
        sort_keys=True,
    )


def get_order_state(all_state: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    oid = order["id"]
    st = all_state.get(oid) or {}
    fp = order_fingerprint(order)
    if st.get("fingerprint") != fp:
        st = {"fingerprint": fp, "triggered": False, "last_sent": {}, "sent_once": {}}
    return st


def may_send(event: str, st: dict[str, Any], cooldown: int, now_ts: float) -> bool:
    if event in ONESHOT_EVENTS and st.get("sent_once", {}).get(event):
        return False
    last = (st.get("last_sent") or {}).get(event)
    if last is not None and (now_ts - float(last)) < cooldown:
        return False
    return True


def mark_sent(st: dict[str, Any], event: str, now_ts: float) -> None:
    st.setdefault("last_sent", {})[event] = now_ts
    if event in ONESHOT_EVENTS:
        st.setdefault("sent_once", {})[event] = True
    if event == "triggered":
        st["triggered"] = True


def build_payload(order: dict[str, Any], event: str, market: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": order["symbol"],
        "event": event,
        "side": order["side"],
        "entry": order["entry"],
        "sl": order["sl"],
        "tp": order["tp"],
        "price": market["raw_price"],
        "broker_price": market["broker_price"],
        "time_utc": utc_now_iso(),
        "note": EVENT_NOTES.get(event, event),
    }


def evaluate_order(
    cfg: dict[str, Any],
    order: dict[str, Any],
    market: dict[str, Any],
    all_state: dict[str, Any],
) -> list[str]:
    st = get_order_state(all_state, order)
    events = detect_events(order, market, st)
    cooldown = int(cfg.get("cooldown_seconds") or 600)
    now_ts = time.time()
    fired: list[str] = []

    if "triggered" in events or st.get("triggered"):
        st["triggered"] = True

    for event in events:
        if not may_send(event, st, cooldown, now_ts):
            continue
        payload = build_payload(order, event, market)
        ok, detail = send_webhook(cfg, payload)
        if detail == SKIPPED_MISSING_SECRETS:
            # Do not consume one-shot events or cooldowns when nothing was sent.
            continue
        log(f"WEBHOOK {order['symbol']} {event} -> {'OK' if ok else 'NAPAKA'}: {detail}")
        mark_sent(st, event, now_ts)
        fired.append(event)

    st["fingerprint"] = order_fingerprint(order)
    st["last_price"] = market["broker_price"]
    st["updated_at"] = utc_now_iso()
    all_state[order["id"]] = st
    return fired


def status_line(snapshots: list[tuple[dict[str, Any], dict[str, Any] | None, str]]) -> str:
    parts = []
    for order, market, err in snapshots:
        if err or market is None:
            parts.append(f"{order['symbol']} ERR")
            continue
        parts.append(
            f"{order['symbol']} {fmt_price(market['broker_price'], order)} "
            f"(lim {fmt_price(order['entry'], order)})"
        )
    return " | ".join(parts)


def poll_once(cfg: dict[str, Any], all_state: dict[str, Any]) -> None:
    snapshots: list[tuple[dict[str, Any], dict[str, Any] | None, str]] = []
    for order in prepare_orders(cfg):
        try:
            market = last_price_snapshot(order)
            evaluate_order(cfg, order, market, all_state)
            snapshots.append((order, market, ""))
            time.sleep(0.15)
        except FetchError as exc:
            log(f"{order.get('symbol', '?')}: ni podatka ({exc})")
            snapshots.append((order, None, str(exc)))
        except Exception as exc:
            log(f"{order.get('symbol', '?')}: nepričakovana napaka: {exc}")
            traceback.print_exc()
            snapshots.append((order, None, str(exc)))
    log(status_line(snapshots))
    try:
        save_json(STATE_PATH, all_state)
    except OSError as exc:
        log(f"state.json se ni shranil: {exc}")


def run_test(cfg: dict[str, Any]) -> int:
    if not webhook_key(cfg) or not webhook_url(cfg):
        log("Opozorilo: FN_WEBHOOK_URL ali FN_WEBHOOK_KEY manjka.")
    orders = prepare_orders(cfg)
    order = orders[0]
    try:
        market = last_price_snapshot(order)
        log(
            f"Testni vir v redu: {order['symbol']} "
            f"broker={fmt_price(market['broker_price'], order)} "
            f"raw={fmt_price(market['raw_price'], order)}"
        )
    except FetchError as exc:
        log(f"Testni vir ni uspel ({exc}), pošiljam vseeno s ceno 0.")
        market = {
            "price": 0.0,
            "broker_price": 0.0,
            "raw_price": 0.0,
            "high": 0.0,
            "low": 0.0,
            "recent_high": 0.0,
            "recent_low": 0.0,
            "m15_close": None,
            "m15_ts": None,
        }
    payload = build_payload(order, "test", market)
    payload["event"] = "test"
    payload["note"] = "Testni alarm iz monitor.py --test. Ni pravi tržni dogodek."
    ok, detail = send_webhook(cfg, payload)
    if detail == SKIPPED_MISSING_SECRETS:
        return 0
    log(f"WEBHOOK TEST {order['symbol']} -> {'OK' if ok else 'NAPAKA'}: {detail}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="FundedNext price monitor")
    parser.add_argument("--test", action="store_true", help="Pošlji en testni dogodek in končaj.")
    parser.add_argument("--once", action="store_true", help="Opravi en krog preverjanja in končaj.")
    args = parser.parse_args()

    install_stop_signals()
    try:
        cfg = load_config()
        prepare_orders(cfg)
    except (OSError, json.JSONDecodeError, SystemExit) as exc:
        log(f"config.json ni veljaven: {exc}")
        return 2

    if args.test:
        return run_test(cfg)

    all_state = load_json(STATE_PATH, {})
    if not isinstance(all_state, dict):
        all_state = {}

    if not (webhook_url(cfg) and webhook_key(cfg)):
        log("Opozorilo: FN_WEBHOOK_URL ali FN_WEBHOOK_KEY manjka. Pošiljanje bo preskočeno.")
    log(
        f"Monitor zagnan. Nalogov: {len(cfg['orders'])}. "
        f"Interval: {cfg.get('poll_seconds', 60)} s. "
        f"cffi={'da' if fnlib._have_curl_cffi() else 'ne'}. "
        f"Webhook: {'da' if webhook_url(cfg) and webhook_key(cfg) else 'ne'}. "
        f"Stanje: {STATE_PATH}"
    )
    while True:
        if stop_requested():
            return 0
        started = time.time()
        try:
            cfg = load_config()
            poll_once(cfg, all_state)
        except SystemExit as exc:
            log(str(exc))
        except Exception as exc:
            log(f"Krog ni uspel, poskusim znova čez minuto: {exc}")
            traceback.print_exc()
        if args.once or stop_requested():
            return 0
        elapsed = time.time() - started
        wait = max(1.0, float(cfg.get("poll_seconds") or 60) - elapsed)
        sleep_secs(wait)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Ustavljeno (Ctrl+C).")
        sys.exit(0)
