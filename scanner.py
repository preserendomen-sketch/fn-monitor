#!/usr/bin/env python3
"""Opportunity scanner for FundedNext day-trading setups.

Every 5 minutes, on completed M15 candles (H1 for trend), flags a candidate
only when one of the strict filters holds. Alerts go to the same webhook as
the order monitor, with event='candidate'. Active 08:00-21:00 Europe/Ljubljana
on weekdays. Max 8 alerts per day, 2 per symbol; 90-minute cooldown per symbol per type.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

import fnlib
from fnlib import (
    SCRIPT_DIR,
    FetchError,
    atr,
    day_bounds,
    ema_state,
    fetch_candles,
    fmt_price,
    hm_on_day,
    in_active_hours,
    load_config,
    install_stop_signals,
    load_json,
    log,
    relative_volume,
    rsi_series,
    save_json,
    send_webhook,
    session_vwap,
    SKIPPED_MISSING_SECRETS,
    sleep_secs,
    stop_requested,
    utc_now_iso,
    wick_fractions,
    LJ_TZ,
)

STATE_PATH = os.path.join(SCRIPT_DIR, "scanner_state.json")


def scanner_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    sc = dict(cfg.get("scanner") or {})
    sc.setdefault("interval_seconds", 300)
    sc.setdefault("active_start", "08:00")
    sc.setdefault("active_end", "21:00")
    sc.setdefault("weekdays_only", True)
    sc.setdefault("asian_start", "00:00")
    sc.setdefault("asian_end", "08:00")
    sc.setdefault("cooldown_minutes", 90)
    sc.setdefault("max_alerts_per_day", 8)
    sc.setdefault("max_alerts_per_symbol_per_day", 2)
    sc.setdefault("atr_buffer_mult", 0.15)
    sc.setdefault("rvol_min", 1.5)
    sc.setdefault("rsi_ob", 75)
    sc.setdefault("rsi_os", 25)
    sc.setdefault("wick_frac", 0.5)
    if not sc.get("universe"):
        raise SystemExit("config.json nima scanner.universe")
    for inst in sc["universe"]:
        inst.setdefault("id", inst.get("symbol"))
        inst.setdefault("offset", 0)
        inst["offset"] = float(inst["offset"])
    return sc


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LJ_TZ)


def completed(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in bars if b.get("complete")]


def bars_on_day(bars: list[dict[str, Any]], day) -> list[dict[str, Any]]:
    start, end = day_bounds(day)
    return [b for b in bars if start <= b["ts"] < end]


def previous_trading_day(bars: list[dict[str, Any]], today) -> Any | None:
    prev_dates = []
    for b in bars:
        d = local_dt(b["ts"]).date()
        if d < today:
            prev_dates.append(d)
    if not prev_dates:
        return None
    return max(prev_dates)


def pdh_pdl(bars: list[dict[str, Any]], today) -> tuple[float | None, float | None, Any | None]:
    prev = previous_trading_day(bars, today)
    if prev is None:
        return None, None, None
    day_bars = bars_on_day(bars, prev)
    if not day_bars:
        return None, None, prev
    return max(b["high"] for b in day_bars), min(b["low"] for b in day_bars), prev


def window_hl(bars: list[dict[str, Any]], start_ts: float, end_ts: float) -> tuple[float | None, float | None]:
    sel = [b for b in bars if start_ts <= b["ts"] < end_ts]
    if not sel:
        return None, None
    return max(b["high"] for b in sel), min(b["low"] for b in sel)


def h1_closes_asof(h1: list[dict[str, Any]], asof_ts: float) -> list[float]:
    """Completed H1 closes at or before the M15 bar close (asof_ts = m15 start)."""
    out = []
    for b in h1:
        # Use H1 bars whose period has ended by the close of this M15 bar.
        if b["ts"] + 3600 <= asof_ts + 15 * 60 + 1:
            out.append(b["close"])
    return out


def uses_atr_buffer(inst: dict[str, Any]) -> bool:
    """Breakouts on these markets must close 0.15× H1 ATR beyond the level.

    FX and cash DAX have no usable volume. WTI (USOUSD / CL=F) does, and it
    still uses the same cushion so a one-tick poke through the level is not
    a breakout.
    """
    symbol = (inst.get("symbol") or inst.get("id") or "").upper()
    ticker = inst.get("ticker") or ""
    if symbol in {"EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "GER40", "USOUSD"}:
        return True
    if ticker == "CL=F":
        return True
    return ticker.endswith("=X")


def levels_broken_before(
    m15: list[dict[str, Any]],
    idx: int,
    pdh,
    pdl,
    asian_h,
    asian_l,
    buffer: float = 0.0,
) -> list[tuple[str, float, str]]:
    """PDH/PDL or Asian range levels that were closed beyond on an earlier bar today.

    Returns list of (name, price, break_dir) where break_dir is 'up' or 'down'.
    `buffer` is 0.15× H1 ATR for FX, GER40 and WTI, otherwise 0.
    """
    today = local_dt(m15[idx]["ts"]).date()
    start, _ = day_bounds(today)
    broken: dict[str, tuple[str, float, str]] = {}
    for i in range(idx):
        b = m15[i]
        if b["ts"] < start:
            continue
        candidates = [
            ("PDH", pdh, "up"),
            ("PDL", pdl, "down"),
            ("Asian high", asian_h, "up"),
            ("Asian low", asian_l, "down"),
        ]
        for name, level, direction in candidates:
            if level is None:
                continue
            key = f"{name}:{round(level, 6)}:{direction}"
            if key in broken:
                continue
            if direction == "up" and b["close"] > level + buffer:
                broken[key] = (name, float(level), "up")
            elif direction == "down" and b["close"] < level - buffer:
                broken[key] = (name, float(level), "down")
    return list(broken.values())


def is_rejection(bar: dict[str, Any], direction: str, wick_frac: float) -> bool:
    upper, lower = wick_fractions(bar)
    mid = (bar["high"] + bar["low"]) / 2.0
    if direction == "buy":
        return lower >= wick_frac and bar["close"] >= mid
    return upper >= wick_frac and bar["close"] <= mid


def evaluate_bar(
    inst: dict[str, Any],
    m15: list[dict[str, Any]],
    idx: int,
    h1: list[dict[str, Any]],
    sc: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return zero or more candidate dicts for m15[idx] (must be completed)."""
    bar = m15[idx]
    today = local_dt(bar["ts"]).date()
    start, _ = day_bounds(today)
    asian_s = hm_on_day(today, sc["asian_start"]).timestamp()
    asian_e = hm_on_day(today, sc["asian_end"]).timestamp()

    pdh, pdl, _prev = pdh_pdl(m15[: idx + 1], today)
    asian_h, asian_l = window_hl(m15[:idx], asian_s, asian_e)

    closes = [b["close"] for b in m15[: idx + 1]]
    rsi_s = rsi_series(closes, 14)
    rsi = rsi_s[idx]
    rvol = relative_volume(m15, idx, 20)
    h1_closes = h1_closes_asof(h1, bar["ts"])
    trend = ema_state(h1_closes, 9, 20)
    vwap = session_vwap([b for b in m15[: idx + 1] if start <= b["ts"] <= bar["ts"]])
    wick_need = float(sc.get("wick_frac") or 0.5)
    rvol_min = float(sc.get("rvol_min") or 1.5)
    rsi_ob = float(sc.get("rsi_ob") or 75)
    rsi_os = float(sc.get("rsi_os") or 25)

    # Skip RVOL only when the instrument typically has no volume (Yahoo FX, cash DAX).
    # If the feed usually has volume but this bar does not, do not treat it as a pass.
    recent = m15[max(0, idx - 40): idx + 1]
    nz = sum(1 for b in recent if (b.get("volume") or 0) > 0)
    typically_has_volume = nz >= max(5, int(len(recent) * 0.3))
    if rvol is None:
        rvol_ok = not typically_has_volume
    else:
        rvol_ok = rvol >= rvol_min

    found: list[dict[str, Any]] = []

    def pack(ctype: str, direction: str, level: float | None, note: str) -> dict[str, Any]:
        offset = float(inst.get("offset") or 0)
        return {
            "symbol": inst["symbol"],
            "event": "candidate",
            "type": ctype,
            "direction": direction,
            "level": None if level is None else round(level, 6),
            "price": bar["close"] - offset,
            "broker_price": bar["close"],
            "rvol": None if rvol is None else round(rvol, 2),
            "rsi": None if rsi is None else round(rsi, 1),
            "ema_state": trend,
            "time_utc": datetime.fromtimestamp(bar["ts"] + 15 * 60, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "bar_ts": bar["ts"],
            "note": note,
        }

    # H1 ATR as of this M15 close, for the close-beyond-level cushion.
    h1_asof = [b for b in h1 if b["ts"] + 3600 <= bar["ts"] + 15 * 60 + 1]
    h1_atr = atr(h1_asof, 14)
    atr_mult = float(sc.get("atr_buffer_mult") or 0.15)
    need_buf = uses_atr_buffer(inst)
    if need_buf:
        buffer = (atr_mult * h1_atr) if h1_atr and h1_atr > 0 else None
    else:
        buffer = 0.0

    # (a) M15 close beyond PDH/PDL or Asian range, RVOL, H1 EMA aligned.
    breakout_levels_up = [
        ("PDH", pdh),
        ("Asian high", asian_h),
    ]
    breakout_levels_down = [
        ("PDL", pdl),
        ("Asian low", asian_l),
    ]
    prev_close = m15[idx - 1]["close"] if idx > 0 else None

    def fresh_beyond(level, side: str) -> bool:
        if level is None:
            return False
        if need_buf and buffer is None:
            return False
        buf = buffer or 0.0
        if side == "up":
            crossed = bar["close"] >= level + buf if need_buf else bar["close"] > level
            was_inside = prev_close is None or prev_close <= level
        else:
            crossed = bar["close"] <= level - buf if need_buf else bar["close"] < level
            was_inside = prev_close is None or prev_close >= level
        return crossed and was_inside

    if rvol_ok and trend in ("bullish", "bearish"):
        if trend == "bullish":
            hits = [(n, lv) for n, lv in breakout_levels_up if fresh_beyond(lv, "up")]
            if hits:
                # Prefer PDH, then Asian high/low.
                name, lv = hits[0]
                rvol_txt = "RVOL ni na voljo (ni volumna)" if rvol is None else f"RVOL {rvol:.2f}"
                found.append(
                    pack(
                        "breakout",
                        "buy",
                        lv,
                        f"M15 zaprl nad {name} {fmt_price(lv, inst)}, H1 EMA 9/20 bullish, {rvol_txt}.",
                    )
                )
        elif trend == "bearish":
            hits = [(n, lv) for n, lv in breakout_levels_down if fresh_beyond(lv, "down")]
            if hits:
                name, lv = hits[0]
                rvol_txt = "RVOL ni na voljo (ni volumna)" if rvol is None else f"RVOL {rvol:.2f}"
                found.append(
                    pack(
                        "breakout",
                        "sell",
                        lv,
                        f"M15 zaprl pod {name} {fmt_price(lv, inst)}, H1 EMA 9/20 bearish, {rvol_txt}.",
                    )
                )

    # (b) Retest of a level broken earlier today, or of session VWAP,
    # in the H1 trend direction with a rejection candle.
    if trend in ("bullish", "bearish"):
        direction = "buy" if trend == "bullish" else "sell"
        brk_buf = buffer if buffer is not None else (1e18 if need_buf else 0.0)
        broken = levels_broken_before(m15, idx, pdh, pdl, asian_h, asian_l, buffer=brk_buf)
        touched: list[tuple[str, float]] = []
        want = "up" if direction == "buy" else "down"
        for name, level, brk in broken:
            if brk != want:
                continue
            if bar["low"] <= level <= bar["high"]:
                touched.append((name, level))
        if vwap is not None and bar["low"] <= vwap <= bar["high"]:
            touched.append(("VWAP", vwap))
        if touched and is_rejection(bar, direction, wick_need):
            name, lv = touched[0]
            found.append(
                pack(
                    "retest",
                    direction,
                    lv,
                    f"Retest {name} {fmt_price(lv, inst)} z rejection svečo, H1 {trend}.",
                )
            )

    # (c) Exhaustion rejection at PDH/PDL, RSI extreme, wick > 50%.
    upper, lower = wick_fractions(bar)
    if pdh is not None and bar["high"] >= pdh and upper >= wick_need and rsi is not None and rsi > rsi_ob:
        found.append(
            pack(
                "exhaustion",
                "sell",
                pdh,
                f"Izčrpanost na PDH {fmt_price(pdh, inst)}, zgornji wick {upper:.0%}, RSI {rsi:.1f}.",
            )
        )
    if pdl is not None and bar["low"] <= pdl and lower >= wick_need and rsi is not None and rsi < rsi_os:
        found.append(
            pack(
                "exhaustion",
                "buy",
                pdl,
                f"Izčrpanost na PDL {fmt_price(pdl, inst)}, spodnji wick {lower:.0%}, RSI {rsi:.1f}.",
            )
        )

    return found


def day_key(ts: float | None = None) -> str:
    dt = local_dt(ts) if ts else datetime.now(LJ_TZ)
    return dt.strftime("%Y-%m-%d")


def can_send(state: dict[str, Any], inst: dict[str, Any], ctype: str, bar_ts: float, sc: dict[str, Any]) -> bool:
    today = day_key(bar_ts)
    cooldown = float(sc["cooldown_minutes"]) * 60
    key = f"{inst['id']}:{ctype}"
    last = (state.get("last") or {}).get(key)
    if last is not None and (bar_ts - float(last)) < cooldown:
        return False
    if state.get("day") != today:
        return True
    if int(state.get("sent_today") or 0) >= int(sc["max_alerts_per_day"]):
        return False
    per = int((state.get("sent_today_by_symbol") or {}).get(inst["id"]) or 0)
    if per >= int(sc.get("max_alerts_per_symbol_per_day") or 2):
        return False
    return True


def mark_sent(state: dict[str, Any], inst: dict[str, Any], ctype: str, bar_ts: float) -> None:
    today = day_key(bar_ts)
    if state.get("day") != today:
        state["day"] = today
        state["sent_today"] = 0
        state["sent_today_by_symbol"] = {}
        # Keep last timestamps; cooldown can span midnight but the cap resets.
    state["sent_today"] = int(state.get("sent_today") or 0) + 1
    by = state.setdefault("sent_today_by_symbol", {})
    by[inst["id"]] = int(by.get(inst["id"]) or 0) + 1
    state.setdefault("last", {})[f"{inst['id']}:{ctype}"] = bar_ts


def fetch_instrument(inst: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    m15 = completed(fetch_candles(inst, "15m", "1mo"))
    time.sleep(0.15)
    h1 = completed(fetch_candles(inst, "1h", "3mo"))
    return m15, h1


def backfill(cfg: dict[str, Any], sc: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk today's completed M15 bars in the active window and apply caps."""
    today = datetime.now(LJ_TZ).date()
    start_active = hm_on_day(today, sc["active_start"]).timestamp()
    end_active = hm_on_day(today, sc["active_end"]).timestamp()
    now = time.time()
    end = min(end_active, now)
    fake_state: dict[str, Any] = {"day": day_key(), "sent_today": 0, "sent_today_by_symbol": {}, "last": {}}
    flagged: list[dict[str, Any]] = []

    for inst in sc["universe"]:
        try:
            m15, h1 = fetch_instrument(inst)
            log(f"backfill {inst['symbol']}: M15={len(m15)} H1={len(h1)}")
        except FetchError as exc:
            log(f"backfill {inst['symbol']}: ni podatka ({exc})")
            continue
        except Exception as exc:
            log(f"backfill {inst['symbol']}: napaka {exc}")
            traceback.print_exc()
            continue
        for idx, bar in enumerate(m15):
            close_ts = bar["ts"] + 15 * 60
            if close_ts < start_active or bar["ts"] >= end:
                continue
            if not in_active_hours(close_ts, sc["active_start"], sc["active_end"], sc["weekdays_only"]):
                continue
            cands = evaluate_bar(inst, m15, idx, h1, sc)
            seen = set()
            for cand in cands:
                if cand["type"] in seen:
                    continue
                seen.add(cand["type"])
                flagged.append(dict(cand))
        time.sleep(0.2)
    # Apply caps in time order, as a live run from 08:00 would.
    flagged.sort(key=lambda c: (c.get("bar_ts") or 0, c.get("symbol") or "", c.get("type") or ""))
    applied: list[dict[str, Any]] = []
    for cand in flagged:
        inst = {"id": cand["symbol"], "symbol": cand["symbol"]}
        if not can_send(fake_state, inst, cand["type"], cand["bar_ts"], sc):
            cand = dict(cand)
            cand["suppressed"] = True
            applied.append(cand)
            continue
        mark_sent(fake_state, inst, cand["type"], cand["bar_ts"])
        applied.append(cand)
    return applied


def scan_live(cfg: dict[str, Any], sc: dict[str, Any], state: dict[str, Any], send: bool = True) -> list[dict[str, Any]]:
    today = day_key()
    if state.get("day") != today:
        state["day"] = today
        state["sent_today"] = 0
        state["sent_today_by_symbol"] = {}
    fired: list[dict[str, Any]] = []
    last_processed = state.setdefault("last_processed", {})

    for inst in sc["universe"]:
        try:
            m15, h1 = fetch_instrument(inst)
        except FetchError as exc:
            log(f"{inst['symbol']}: ni podatka ({exc})")
            continue
        except Exception as exc:
            log(f"{inst['symbol']}: napaka {exc}")
            traceback.print_exc()
            continue
        if not m15:
            continue
        bar = m15[-1]
        idx = len(m15) - 1
        close_ts = bar["ts"] + 15 * 60
        prev = last_processed.get(inst["id"])
        # First run: remember the last completed bar and do not back-send it.
        if prev is None:
            last_processed[inst["id"]] = bar["ts"]
            log(
                f"{inst['symbol']} broker={fmt_price(bar['close'], inst)} "
                f"M15={len(m15)} H1={len(h1)} (začetno stanje, brez pošiljanja)"
            )
            continue
        last_processed[inst["id"]] = bar["ts"]
        if bar["ts"] <= float(prev):
            continue
        if not in_active_hours(close_ts, sc["active_start"], sc["active_end"], sc["weekdays_only"]):
            continue
        cands = evaluate_bar(inst, m15, idx, h1, sc)
        seen = set()
        for cand in cands:
            if cand["type"] in seen:
                continue
            seen.add(cand["type"])
            if not can_send(state, inst, cand["type"], bar["ts"], sc):
                log(
                    f"{inst['symbol']} {cand['type']} {cand['direction']} "
                    f"zavrnjen (cooldown ali dnevni cap)."
                )
                continue
            if send:
                ok, detail = send_webhook(cfg, {k: v for k, v in cand.items() if k != "bar_ts"})
                if detail == SKIPPED_MISSING_SECRETS:
                    continue
                log(
                    f"WEBHOOK candidate {inst['symbol']} {cand['type']} "
                    f"{cand['direction']} -> {'OK' if ok else 'NAPAKA'}: {detail}"
                )
                if not ok:
                    continue
            mark_sent(state, inst, cand["type"], bar["ts"])
            fired.append(cand)
        last = m15[-1]
        log(
            f"{inst['symbol']} broker={fmt_price(last['close'], inst)} "
            f"M15={len(m15)} H1={len(h1)}"
        )
        time.sleep(0.2)
    return fired


def main() -> int:
    parser = argparse.ArgumentParser(description="FundedNext opportunity scanner")
    parser.add_argument("--backfill", action="store_true", help="Preglej današnje M15 in izpiši kandidate, brez webhooka.")
    parser.add_argument("--once", action="store_true", help="En krog in končaj (pošlje samo nove sveče).")
    parser.add_argument("--no-send", action="store_true", help="Ne pošiljaj webhookov.")
    args = parser.parse_args()

    install_stop_signals()
    try:
        cfg = load_config()
        sc = scanner_cfg(cfg)
    except (OSError, json.JSONDecodeError, SystemExit) as exc:
        log(f"config.json ni veljaven: {exc}")
        return 2

    if args.backfill:
        log(
            f"Backfill današnjih M15 (okno {sc['active_start']}-{sc['active_end']} "
            f"Europe/Ljubljana, cap {sc['max_alerts_per_day']}/dan, "
            f"{sc.get('max_alerts_per_symbol_per_day', 2)}/simbol, "
            f"cooldown {sc['cooldown_minutes']} min)."
        )
        flagged = backfill(cfg, sc)
        sent_like = [c for c in flagged if not c.get("suppressed")]
        suppressed = [c for c in flagged if c.get("suppressed")]
        log(
            f"BACKFILL: {len(flagged)} zadetkov filtrov, "
            f"{len(sent_like)} bi šlo ven po cap/cooldown, "
            f"{len(suppressed)} bi bilo zadržanih."
        )
        for c in flagged:
            flag = "CAP" if c.get("suppressed") else "ALERT"
            log(
                f"  {flag} {c['time_utc']} {c['symbol']} {c['type']} {c['direction']} "
                f"lvl={c['level']} px={c['broker_price']} rvol={c['rvol']} "
                f"rsi={c['rsi']} ema={c['ema_state']} | {c['note']}"
            )
        report_path = os.path.join(SCRIPT_DIR, "backfill_report.json")
        save_json(report_path, {"flagged": flagged, "would_send": sent_like, "suppressed": suppressed})
        log(f"Poročilo: {report_path}")
        return 0

    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}

    if not (fnlib.webhook_url(cfg) and fnlib.webhook_key(cfg)):
        log("Opozorilo: FN_WEBHOOK_URL ali FN_WEBHOOK_KEY manjka. Pošiljanje bo preskočeno.")
    log(
        f"Scanner zagnan. Instrumentov: {len(sc['universe'])}. "
        f"Interval: {sc['interval_seconds']} s. "
        f"Okno {sc['active_start']}-{sc['active_end']} Europe/Ljubljana, delavniki. "
        f"Cap {sc['max_alerts_per_day']}/dan, "
        f"{sc.get('max_alerts_per_symbol_per_day', 2)}/simbol. "
        f"cffi={'da' if fnlib._have_curl_cffi() else 'ne'}. "
        f"Webhook: {'da' if fnlib.webhook_url(cfg) and fnlib.webhook_key(cfg) else 'ne'}."
    )

    while True:
        if stop_requested():
            return 0
        started = time.time()
        try:
            cfg = load_config()
            sc = scanner_cfg(cfg)
            fired = scan_live(cfg, sc, state, send=not args.no_send)
            if not fired:
                log("Ni novega kandidata.")
            try:
                save_json(STATE_PATH, state)
            except OSError as exc:
                log(f"scanner_state.json se ni shranil: {exc}")
        except SystemExit as exc:
            log(str(exc))
        except Exception as exc:
            log(f"Krog ni uspel: {exc}")
            traceback.print_exc()
        if args.once or stop_requested():
            return 0
        elapsed = time.time() - started
        wait = max(5.0, float(sc.get("interval_seconds") or 300) - elapsed)
        sleep_secs(wait)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Ustavljeno (Ctrl+C).")
        sys.exit(0)
