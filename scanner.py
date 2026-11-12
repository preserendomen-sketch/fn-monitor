#!/usr/bin/env python3
"""Opportunity scanner for FundedNext day-trading setups.

Alerts go to the same webhook as the order monitor, with event='candidate'.
Active 08:00-21:00 Europe/Ljubljana on weekdays. Max 8 alerts per day, 2 per
symbol; 90-minute cooldown per symbol per type.

Decision rules (all of them, or no alert):

- H1 EMA 9/20 is the trend filter. Breakouts must go with it. Retest and
  pullback need a clear H1 trend. Exhaustion that fights a clear H1 trend
  is dropped.
- M15 is structure: breakout through PDH/PDL or the Asian range, retest,
  pullback to EMA, or exhaustion. A breakout on a completed M15 still needs
  RVOL >= 1.5 (rvol_min). That threshold is not lowered.
- M5 is structure. The latest completed M5 inside the M15 must close with
  the trade, beyond the M15 midpoint, with EMA 9/20 aligned (except
  exhaustion, where the M5 candle itself must reject — the EMA lags the
  turn). A breakout M5 must also close beyond the broken level.
- M1 is timing only. At least two completed M1 bars in the last five
  minutes must agree with the direction, the last one must not be a spike
  (wick against the trade >= wick_frac), and it must close beyond the
  level. One M1 bar is never enough, and an M1 spike never fires alone.

Quality: a breakout that passes is A+. Retest, pullback, and exhaustion
that pass the stricter full picture are B+ (strong B). Nothing weaker is sent.

Session-open windows (Europe/Ljubljana, clock time, end exclusive):

- london 08:45-10:00, around the 09:00 London open (London and Ljubljana
  shift together, so 09:00 local stays the London open all year).
- us_cash 15:15-16:30, around the 15:30 US cash open (NYSE 09:30). The
  US/EU DST switch differs by a week or two; the window is wide enough
  to cover that.

Inside those windows the loop sleeps boost_interval_seconds (default 60)
and evaluates every newly completed M1 and M5 bar. A breakout may fire
before the M15 bar closes when the forming M15, a completed M5 inside it
(RVOL >= 1.5 on that M5, same cushion beyond the level), and M1 timing all
agree. Retest, pullback, and exhaustion still wait for a completed M15,
because they need a finished candle; the faster loop picks that close up
within about a minute. Outside the windows the loop stays on
interval_seconds (default 300) and decides when an M15 bar completes, but
it still computes M1 and M5. Bars older than max_signal_age_minutes are
logged as missed and not sent.

--backfill and --no-send never call the webhook. Tests must not either.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import fnlib
from fnlib import (
    SCRIPT_DIR,
    FetchError,
    atr,
    day_bounds,
    ema_last,
    ema_series,
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
    sc.setdefault("pullback_vwap_atr", 1.5)
    sc.setdefault("news_window_minutes", 30)
    sc.setdefault("news_cache_seconds", 3600)
    sc.setdefault("news_url", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    # Older configs may set m5_confirm. M1 and M5 agreement is mandatory either way.
    sc.setdefault("m5_confirm", True)
    sc.setdefault("max_signal_age_minutes", 45)
    sc.setdefault("boost_interval_seconds", 60)
    # Clock windows in Europe/Ljubljana. End is exclusive. See the module docstring.
    sc.setdefault(
        "session_open_windows",
        [
            {"name": "london", "start": "08:45", "end": "10:00"},
            {"name": "us_cash", "start": "15:15", "end": "16:30"},
        ],
    )
    if not sc.get("universe"):
        raise SystemExit("config.json nima scanner.universe")
    for inst in sc["universe"]:
        inst.setdefault("id", inst.get("symbol"))
        inst.setdefault("offset", 0)
        inst["offset"] = float(inst["offset"])
    return sc


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LJ_TZ)


M15_SEC = 15 * 60
M5_SEC = 5 * 60
M1_SEC = 60

_DEFAULT_WINDOWS = (
    {"name": "london", "start": "08:45", "end": "10:00"},
    {"name": "us_cash", "start": "15:15", "end": "16:30"},
)


def _valid_hm(hm: str) -> bool:
    parts = str(hm).split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return False
    hour, minute = int(parts[0]), int(parts[1])
    return 0 <= hour <= 23 and 0 <= minute <= 59


def session_open_windows(sc: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(name, start_hm, end_hm) in Europe/Ljubljana. End is exclusive."""
    raw = sc.get("session_open_windows") or _DEFAULT_WINDOWS
    out: list[tuple[str, str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "session")
        start = str(item.get("start") or "")
        end = str(item.get("end") or "")
        if _valid_hm(start) and _valid_hm(end) and start != end:
            out.append((name, start, end))
    return out


def in_session_open_window(ts: float, sc: dict[str, Any]) -> str | None:
    """Name of the session-open window that contains ts, or None.

    This is a clock check in Europe/Ljubljana. Weekday and 08:00-21:00
    gating stays in in_active_hours; a Saturday morning is inside the
    clock window and still cannot alert.
    """
    dt = local_dt(ts)
    for name, start_hm, end_hm in session_open_windows(sc):
        start = hm_on_day(dt.date(), start_hm)
        end = hm_on_day(dt.date(), end_hm)
        if start <= dt < end:
            return name
    return None


def next_session_open_start(now: float, sc: dict[str, Any], horizon: float) -> float | None:
    """Next window start strictly after now and within horizon seconds."""
    if horizon <= 0:
        return None
    dt = local_dt(now)
    best: float | None = None
    for add in (0, 1):
        day = dt.date() + timedelta(days=add)
        for _name, start_hm, _end in session_open_windows(sc):
            start = hm_on_day(day, start_hm).timestamp()
            if now < start <= now + horizon and (best is None or start < best):
                best = start
    return best


def scanner_sleep_seconds(now: float, sc: dict[str, Any], elapsed: float) -> float:
    """Sleep after a scan that took `elapsed` seconds.

    Outside the session-open windows the base is interval_seconds (5 min).
    If a window opens before that sleep ends, wake at the open so the first
    M1/M5 of the drive is not missed. Inside a window the base is
    boost_interval_seconds (60s): completed M1 and M5 bars are evaluated
    on the next loop instead of waiting for the M15 close.
    """
    boost = in_session_open_window(now, sc) is not None
    if boost:
        base = float(sc.get("boost_interval_seconds") or 60)
    else:
        base = float(sc.get("interval_seconds") or 300)
    wait = base - max(0.0, elapsed)
    if wait < 5.0:
        wait = 5.0
    if not boost:
        nxt = next_session_open_start(now, sc, wait)
        if nxt is not None:
            until = nxt - now
            if 0 < until < wait:
                wait = max(1.0, until)
    return wait


def on_m15_boundary(close_ts: float) -> bool:
    """True when a bar close is also an M15 close (epoch-aligned)."""
    return int(round(close_ts)) % M15_SEC == 0


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


# High-impact currencies that move each instrument. Override with
# scanner.news_currencies in config.json.
DEFAULT_NEWS_CCY: dict[str, tuple[str, ...]] = {
    "EURUSD": ("USD", "EUR"),
    "GBPUSD": ("USD", "GBP"),
    "AUDUSD": ("USD", "AUD"),
    "USDJPY": ("USD", "JPY"),
    "XAUUSD": ("USD",),
    "BTCUSD": ("USD",),
    "NAS100": ("USD",),
    "US30": ("USD",),
    "USOUSD": ("USD",),
    "GER40": ("EUR", "USD"),
}

_news_cache: dict[str, Any] = {"fetched_at": 0.0, "ok": False, "events": []}


def _num(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def instrument_currencies(symbol: str, sc: dict[str, Any]) -> set[str]:
    custom = sc.get("news_currencies") or {}
    if symbol in custom:
        return {str(c).upper() for c in custom[symbol]}
    return {c.upper() for c in DEFAULT_NEWS_CCY.get(symbol.upper(), ())}


def load_news_calendar(sc: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """ForexFactory week file, cached for news_cache_seconds (default 1 hour).

    A failed fetch is cached too, so one outage does not suppress signals.
    """
    now = time.time()
    ttl = float(sc.get("news_cache_seconds") or 3600)
    if _news_cache["fetched_at"] and (now - float(_news_cache["fetched_at"])) < ttl:
        return list(_news_cache["events"]), bool(_news_cache["ok"])
    url = str(sc.get("news_url") or "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    try:
        payload = fnlib.http_get_json(url, timeout=15, retries=2)
        if not isinstance(payload, list):
            raise FetchError("koledar ni seznam")
        events = [ev for ev in payload if isinstance(ev, dict)]
        ok = True
    except Exception as exc:
        log(f"Koledar ni na voljo ({str(exc).replace(chr(10), ' ')[:160]}).")
        events = []
        ok = False
    _news_cache["fetched_at"] = now
    _news_cache["ok"] = ok
    _news_cache["events"] = events
    return events, ok


def high_impact_block(
    symbol: str,
    when_ts: float,
    events: list[dict[str, Any]],
    sc: dict[str, Any],
) -> dict[str, Any] | None:
    """Nearest high-impact event within ±news_window_minutes, or None."""
    window = float(sc.get("news_window_minutes") or 30) * 60.0
    currencies = instrument_currencies(symbol, sc)
    best: dict[str, Any] | None = None
    best_delta: float | None = None
    for ev in events:
        if str(ev.get("impact") or "").lower() != "high":
            continue
        country = str(ev.get("country") or "").upper()
        if country not in currencies:
            continue
        raw_date = ev.get("date")
        if not raw_date:
            continue
        try:
            ev_ts = datetime.fromisoformat(str(raw_date)).timestamp()
        except ValueError:
            continue
        delta = abs(when_ts - ev_ts)
        if delta <= window and (best_delta is None or delta < best_delta):
            best = ev
            best_delta = delta
    return best


def exhaustion_allowed(direction: str, trend: str) -> bool:
    """Drop exhaustion that fights a clear H1 trend.

    A sell is suppressed in a bullish trend and a buy in a bearish trend.
    Flat and unknown trends still pass.
    """
    if direction == "sell" and trend == "bullish":
        return False
    if direction == "buy" and trend == "bearish":
        return False
    return True


def touches_from_trend_side(bar: dict[str, Any], level: float | None, direction: str) -> bool:
    """Wick tags the level from the trend side and the close finishes back beyond it.

    The open has to be on the trend side of the level, so a bar that starts
    through the EMA and only closes back does not count.
    """
    if level is None:
        return False
    if direction == "buy":
        return bar["open"] >= level and bar["low"] <= level and bar["close"] > level
    return bar["open"] <= level and bar["high"] >= level and bar["close"] < level


def volume_ok(bars: list[dict[str, Any]], idx: int, rvol_min: float) -> tuple[bool, float | None]:
    """RVOL gate used for breakouts. The threshold is rvol_min (1.5) and is not lowered.

    A bar with no volume passes only when the feed usually has no volume
    (Yahoo FX, cash DAX). A hole in a feed that normally prints volume fails.
    """
    rvol = relative_volume(bars, idx, 20)
    recent = bars[max(0, idx - 40): idx + 1]
    nz = sum(1 for b in recent if (b.get("volume") or 0) > 0)
    typically_has_volume = nz >= max(5, int(len(recent) * 0.3))
    if rvol is None:
        return (not typically_has_volume), None
    return rvol >= rvol_min, rvol


def _m5_fields(confirm: bool | None, last: dict[str, Any] | None, direction: str) -> dict[str, Any]:
    if last is None:
        return {
            "m5_confirm": confirm,
            "m5_high": None,
            "m5_low": None,
            "m5_trigger": None,
            "m5_ts": None,
            "m5_open": None,
            "m5_close": None,
        }
    return {
        "m5_confirm": confirm,
        "m5_high": last["high"],
        "m5_low": last["low"],
        "m5_trigger": last["high"] if direction == "buy" else last["low"],
        "m5_ts": last["ts"],
        "m5_open": last["open"],
        "m5_close": last["close"],
    }


def m5_confirmation(
    bar: dict[str, Any],
    direction: str,
    m5: list[dict[str, Any]] | None,
    *,
    level: float | None = None,
    kind: str = "trend",
    wick_frac: float = 0.5,
) -> dict[str, Any]:
    """Latest completed M5 inside the M15 bar must agree with the M15 structure.

    None means the M5 feed is missing. False means bars were available and
    did not agree — including when none of them fall inside this M15.
    One failing M5 blocks the alert; there is no single-timeframe pass.

    Trend-following kinds (breakout, retest, pullback) need a directional
    close, a close beyond the M15 midpoint, EMA 9/20 aligned with the trade,
    and a close beyond `level` when a level is given.

    Exhaustion is the turn, so M5 EMA 9/20 is allowed to still point with
    the push that just failed. The M5 candle itself must close in the fade
    direction, through the M15 midpoint, and either wick off the extreme
    or close back through the level.
    """
    if m5 is None:
        return _m5_fields(None, None, direction)
    start = float(bar["ts"])
    end = start + M15_SEC
    inside = [(i, b) for i, b in enumerate(m5) if start - 1 <= b["ts"] < end - 1]
    if not inside:
        return _m5_fields(False, None, direction)
    i, last = inside[-1]
    closes = [b["close"] for b in m5[: i + 1]]
    mid = (bar["high"] + bar["low"]) / 2.0
    if kind == "exhaustion":
        upper, lower = wick_fractions(last)
        if direction == "sell":
            rejected = upper >= wick_frac or (level is not None and last["close"] < level)
            ok = last["close"] < last["open"] and last["close"] < mid and rejected
        else:
            rejected = lower >= wick_frac or (level is not None and last["close"] > level)
            ok = last["close"] > last["open"] and last["close"] > mid and rejected
        return _m5_fields(bool(ok), last, direction)

    e9 = ema_last(closes, 9)
    e20 = ema_last(closes, 20)
    if direction == "buy":
        ok = (
            last["close"] > mid
            and last["close"] > last["open"]
            and e9 is not None
            and e20 is not None
            and e9 > e20
        )
        if level is not None and not last["close"] > level:
            ok = False
    else:
        ok = (
            last["close"] < mid
            and last["close"] < last["open"]
            and e9 is not None
            and e20 is not None
            and e9 < e20
        )
        if level is not None and not last["close"] < level:
            ok = False
    return _m5_fields(bool(ok), last, direction)


def m1_confirmation(
    direction: str,
    m1: list[dict[str, Any]] | None,
    start_ts: float,
    end_ts: float,
    wick_frac: float,
    *,
    level: float | None = None,
) -> dict[str, Any]:
    """Timing cluster inside [start_ts, end_ts). Not a standalone signal.

    None means the M1 feed is missing. False means the bars do not agree.
    Agreement requires all of:

    - at least two completed M1 bars (a single print is an isolated spike)
    - the last bar closes in the trade direction, not a doji
    - the wick against the trade is shorter than wick_frac of the range
    - a strict majority of closes hold the trade side of the cluster's open
    - the last close itself holds that open, and holds `level` when given
    """
    empty = {"m1_confirm": None, "m1_high": None, "m1_low": None, "m1_trigger": None, "m1_ts": None}
    if m1 is None:
        return empty
    inside = [b for b in m1 if start_ts - 1 <= b["ts"] < end_ts - 1]
    if len(inside) < 2:
        last = inside[-1] if inside else None
        return {
            "m1_confirm": False,
            "m1_high": None if last is None else last["high"],
            "m1_low": None if last is None else last["low"],
            "m1_trigger": None,
            "m1_ts": None if last is None else last["ts"],
        }
    last = inside[-1]
    upper, lower = wick_fractions(last)
    anchor = inside[0]["open"]
    if direction == "buy":
        directional = last["close"] > last["open"]
        spike = upper >= wick_frac
        above = sum(1 for b in inside if b["close"] > anchor)
        below = sum(1 for b in inside if b["close"] < anchor)
        hold = last["close"] > anchor
        beyond = level is None or last["close"] > level
        trigger = last["high"]
    else:
        directional = last["close"] < last["open"]
        spike = lower >= wick_frac
        above = sum(1 for b in inside if b["close"] < anchor)
        below = sum(1 for b in inside if b["close"] > anchor)
        hold = last["close"] < anchor
        beyond = level is None or last["close"] < level
        trigger = last["low"]
    ok = directional and not spike and above > below and hold and beyond
    return {
        "m1_confirm": bool(ok),
        "m1_high": last["high"],
        "m1_low": last["low"],
        "m1_trigger": trigger,
        "m1_ts": last["ts"],
    }


def prior_week_high_low(
    daily: list[dict[str, Any]] | None,
    asof_ts: float,
) -> tuple[float | None, float | None]:
    """Prior Mon–Sun high and low from daily bars. Does not look at the current week."""
    if not daily:
        return None, None
    asof = local_dt(asof_ts).date()
    this_monday = asof - timedelta(days=asof.weekday())
    prev_monday = this_monday - timedelta(days=7)
    highs: list[float] = []
    lows: list[float] = []
    for bar in daily:
        day = datetime.fromtimestamp(bar["ts"], tz=timezone.utc).date()
        if prev_monday <= day < this_monday:
            highs.append(bar["high"])
            lows.append(bar["low"])
    if not highs:
        return None, None
    return max(highs), min(lows)


def round_step_for(inst: dict[str, Any], sc: dict[str, Any]) -> float:
    steps = sc.get("round_steps") or {}
    symbol = (inst.get("symbol") or "").upper()
    if symbol in steps:
        return float(steps[symbol])
    defaults = {
        "EURUSD": 0.005,
        "GBPUSD": 0.005,
        "AUDUSD": 0.005,
        "USDJPY": 0.5,
        "XAUUSD": 10.0,
        "BTCUSD": 1000.0,
        "NAS100": 100.0,
        "US30": 100.0,
        "GER40": 100.0,
        "USOUSD": 1.0,
    }
    return float(defaults.get(symbol, 1.0))


def nearest_round_distance(price: float, step: float) -> tuple[float, float]:
    if step <= 0:
        return 0.0, price
    level = round(price / step) * step
    return abs(price - level), level


def recent_trading_days(n: int, today=None) -> set:
    """The last n weekdays, including today when today is a weekday."""
    if n < 1:
        n = 1
    day = today or datetime.now(LJ_TZ).date()
    found = []
    while len(found) < n:
        if day.weekday() < 5:
            found.append(day)
        day -= timedelta(days=1)
    return set(found)


def _attach_mtf(
    cand: dict[str, Any],
    info5: dict[str, Any],
    info1: dict[str, Any],
    quality: str,
) -> None:
    cand["m5_confirm"] = info5["m5_confirm"]
    cand["m5_high"] = _num(info5["m5_high"])
    cand["m5_low"] = _num(info5["m5_low"])
    cand["m5_trigger"] = _num(info5["m5_trigger"])
    cand["m1_confirm"] = info1["m1_confirm"]
    cand["m1_high"] = _num(info1["m1_high"])
    cand["m1_low"] = _num(info1["m1_low"])
    cand["m1_trigger"] = _num(info1["m1_trigger"])
    cand["quality"] = quality
    cand["mtf"] = "M1+M5+M15"
    cand.setdefault("trigger_tf", "M15")


def _gate_candidates(
    inst: dict[str, Any],
    bar: dict[str, Any],
    found: list[dict[str, Any]],
    m5: list[dict[str, Any]] | None,
    sc: dict[str, Any],
    news: tuple[list[dict[str, Any]], bool] | None,
    m1: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Require M5 structure and M1 timing, then the news window.

    A candidate that exists only on M15 is dropped. Missing M1 or M5 data
    is a miss, not a pass. m5_confirm in config cannot turn this off.
    """
    wick_need = float(sc.get("wick_frac") or 0.5)
    gated: list[dict[str, Any]] = []
    for cand in found:
        kind = "exhaustion" if cand["type"] == "exhaustion" else "trend"
        level = cand.get("level")
        info5 = m5_confirmation(
            bar, cand["direction"], m5, level=level, kind=kind, wick_frac=wick_need
        )
        m5_ts = info5.get("m5_ts")
        if m5_ts is None:
            info1 = m1_confirmation(cand["direction"], m1, bar["ts"], bar["ts"], wick_need, level=level)
            if m1 is not None and info1["m1_confirm"] is None:
                info1 = {**info1, "m1_confirm": False}
        else:
            info1 = m1_confirmation(
                cand["direction"], m1, float(m5_ts), float(m5_ts) + M5_SEC, wick_need, level=level
            )
        quality = "A+" if cand["type"] == "breakout" else "B+"
        if info5["m5_confirm"] is True and info1["m1_confirm"] is True:
            _attach_mtf(cand, info5, info1, quality)
            cand["note"] = (
                f"{cand['note']} MTF {quality}: M15 struktura, M5 struktura, M1 timing."
            ).strip()
            gated.append(cand)
            continue
        why = []
        if m5 is None:
            why.append("M5 ni na voljo")
        elif info5["m5_confirm"] is not True:
            why.append("M5 se ne ujema")
        if m1 is None:
            why.append("M1 ni na voljo")
        elif info1["m1_confirm"] is not True:
            why.append("M1 se ne ujema")
        log(
            f"mtf {inst['symbol']} {cand['type']} {cand['direction']} zavrnjen "
            f"({', '.join(why) or 'ni soglasja'})."
        )

    if news is None:
        events, ok = load_news_calendar(sc)
    else:
        events, ok = news
    if not ok:
        for cand in gated:
            if "koledar ni na voljo" not in cand["note"]:
                cand["note"] = f"{cand['note']} koledar ni na voljo".strip()
        return gated

    close_ts = bar["ts"] + 15 * 60
    kept: list[dict[str, Any]] = []
    for cand in gated:
        blocked = high_impact_block(inst["symbol"], close_ts, events, sc)
        if blocked is None:
            kept.append(cand)
            continue
        log(
            f"news_block {inst['symbol']} {cand['type']} {cand['direction']} "
            f"{blocked.get('title')} {blocked.get('country')} {blocked.get('date')}"
        )
    return kept


def evaluate_bar(
    inst: dict[str, Any],
    m15: list[dict[str, Any]],
    idx: int,
    h1: list[dict[str, Any]],
    sc: dict[str, Any],
    m5: list[dict[str, Any]] | None = None,
    daily: list[dict[str, Any]] | None = None,
    news: tuple[list[dict[str, Any]], bool] | None = None,
    m1: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return zero or more candidate dicts for m15[idx] (must be completed).

    M15 structure is necessary and not sufficient. The gate keeps a candidate
    only when M5 structure and M1 timing agree with it. Callers outside the
    session-open windows use this on each completed M15 bar.
    """
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
    h1_closes = h1_closes_asof(h1, bar["ts"])
    trend = ema_state(h1_closes, 9, 20)
    h1_ema20 = ema_last(h1_closes, 20)
    m15_ema20 = ema_series(closes, 20)[idx]
    vwap = session_vwap([b for b in m15[: idx + 1] if start <= b["ts"] <= bar["ts"]])
    pwh, pwl = prior_week_high_low(daily, bar["ts"])
    round_dist, round_level = nearest_round_distance(bar["close"], round_step_for(inst, sc))
    wick_need = float(sc.get("wick_frac") or 0.5)
    rvol_min = float(sc.get("rvol_min") or 1.5)
    rsi_ob = float(sc.get("rsi_ob") or 75)
    rsi_os = float(sc.get("rsi_os") or 25)

    # Same RVOL rule as before: threshold rvol_min (1.5), no-volume pass only
    # when the feed usually has no volume. Do not lower rvol_min here.
    rvol_ok, rvol = volume_ok(m15, idx, rvol_min)

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
            "close_ts": bar["ts"] + M15_SEC,
            "trigger_tf": "M15",
            "session_open": in_session_open_window(bar["ts"] + M15_SEC, sc),
            "note": note,
            "pwh": _num(pwh),
            "pwl": _num(pwl),
            "dist_pwh": None if pwh is None else _num(abs(bar["close"] - pwh)),
            "dist_pwl": None if pwl is None else _num(abs(bar["close"] - pwl)),
            "dist_round": _num(round_dist),
            "round_level": _num(round_level),
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

    # (b2) Pullback to M15 EMA20 or H1 EMA20 in a clear H1 trend.
    pb_dir = None
    if trend == "bullish" and h1_ema20 is not None and bar["close"] > h1_ema20:
        pb_dir = "buy"
    elif trend == "bearish" and h1_ema20 is not None and bar["close"] < h1_ema20:
        pb_dir = "sell"
    vwap_limit = float(sc.get("pullback_vwap_atr") or 1.5)
    if (
        pb_dir
        and h1_atr
        and h1_atr > 0
        and vwap is not None
        and is_rejection(bar, pb_dir, wick_need)
    ):
        touched_ema: list[tuple[str, float]] = []
        m15_ema_prev = ema_series(closes, 20)[idx - 1] if idx else None
        for ema_level in (m15_ema20, m15_ema_prev):
            if touches_from_trend_side(bar, ema_level, pb_dir):
                touched_ema.append(("M15 EMA20", float(ema_level)))
                break
        if touches_from_trend_side(bar, h1_ema20, pb_dir):
            touched_ema.append(("H1 EMA20", float(h1_ema20)))
        if touched_ema:
            label, level = touched_ema[0]
            if abs(level - vwap) <= vwap_limit * h1_atr:
                found.append(
                    pack(
                        "pullback",
                        pb_dir,
                        level,
                        (
                            f"Pullback na {label} {fmt_price(level, inst)}, "
                            f"H1 EMA 9/20 {trend}, rejection sveča, "
                            f"razdalja do VWAP znotraj {vwap_limit:g}× H1 ATR."
                        ),
                    )
                )

    # (c) Exhaustion rejection at PDH/PDL, RSI extreme, wick > 50%.
    # Suppressed when the direction fights the H1 EMA 9/20 trend.
    upper, lower = wick_fractions(bar)
    if (
        pdh is not None
        and bar["high"] >= pdh
        and upper >= wick_need
        and rsi is not None
        and rsi > rsi_ob
        and exhaustion_allowed("sell", trend)
    ):
        found.append(
            pack(
                "exhaustion",
                "sell",
                pdh,
                f"Izčrpanost na PDH {fmt_price(pdh, inst)}, zgornji wick {upper:.0%}, RSI {rsi:.1f}.",
            )
        )
    if (
        pdl is not None
        and bar["low"] <= pdl
        and lower >= wick_need
        and rsi is not None
        and rsi < rsi_os
        and exhaustion_allowed("buy", trend)
    ):
        found.append(
            pack(
                "exhaustion",
                "buy",
                pdl,
                f"Izčrpanost na PDL {fmt_price(pdl, inst)}, spodnji wick {lower:.0%}, RSI {rsi:.1f}.",
            )
        )

    return _gate_candidates(inst, bar, found, m5, sc, news, m1=m1)


def crossed_level(
    price: float,
    level: float | None,
    side: str,
    need_buf: bool,
    buffer: float | None,
) -> bool:
    """Price is far enough beyond `level` to count as a break.

    FX, GER40 and WTI use the 0.15× H1 ATR cushion. A missing ATR on those
    markets does not pass. The cushion is the same one the completed-M15
    breakout uses; the early path does not shrink it.
    """
    if level is None:
        return False
    if need_buf:
        if buffer is None:
            return False
        if side == "up":
            return price >= level + buffer
        return price <= level - buffer
    if side == "up":
        return price > level
    return price < level


def _ohlc(ts: float, parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ts": ts,
        "open": parts[0]["open"],
        "high": max(b["high"] for b in parts),
        "low": min(b["low"] for b in parts),
        "close": parts[-1]["close"],
        "volume": sum((b.get("volume") or 0) for b in parts),
        "complete": False,
    }


def evaluate_early_breakout(
    inst: dict[str, Any],
    m15: list[dict[str, Any]],
    m5: list[dict[str, Any]] | None,
    m1: list[dict[str, Any]] | None,
    h1: list[dict[str, Any]],
    sc: dict[str, Any],
    decision_ts: float,
    *,
    trigger_tf: str = "M5",
    news: tuple[list[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Breakout before the M15 bar closes. Session-open windows only.

    Fires only when three views agree at `decision_ts`:

    - forming M15 (built from completed M1 bars) has closed beyond the level
      by the same cushion a finished M15 breakout would need, and the candle
      is in the trade direction
    - the latest completed M5 inside that M15 closed beyond the level by the
      same cushion, with RVOL >= rvol_min on that M5 bar, plus the usual M5
      structure (directional close, midpoint, EMA 9/20)
    - the last five minutes of M1 time the entry (see m1_confirmation)

    Retest, pullback, and exhaustion are not emitted here; they need a
    finished M15 candle. An M15 boundary is left to evaluate_bar so the same
    close is not alerted twice. Returns [] outside the boost window, outside
    active hours, or when any timeframe is missing or disagrees.
    """
    if m5 is None or m1 is None or not m15:
        return []
    window = in_session_open_window(decision_ts, sc)
    if window is None:
        return []
    if not in_active_hours(
        decision_ts, sc["active_start"], sc["active_end"], sc.get("weekdays_only", True)
    ):
        return []
    if on_m15_boundary(decision_ts):
        return []

    period = float(int(decision_ts) - (int(decision_ts) % M15_SEC))
    m1_asof = sorted(
        (b for b in m1 if b["ts"] + M1_SEC <= decision_ts + 1),
        key=lambda b: b["ts"],
    )
    m5_asof = sorted(
        (b for b in m5 if b["ts"] + M5_SEC <= decision_ts + 1),
        key=lambda b: b["ts"],
    )
    parts = [b for b in m1_asof if period - 1 <= b["ts"]]
    if not parts:
        return []
    forming = _ohlc(period, parts)
    prior = [b for b in m15 if b["ts"] + M15_SEC <= period + 1]
    if not prior:
        return []
    prev_close = prior[-1]["close"]

    today = local_dt(period).date()
    pdh, pdl, _prev = pdh_pdl(prior, today)
    asian_s = hm_on_day(today, sc["asian_start"]).timestamp()
    asian_e = hm_on_day(today, sc["asian_end"]).timestamp()
    asian_h, asian_l = window_hl(prior, asian_s, asian_e)

    h1_asof = [b for b in h1 if b["ts"] + 3600 <= decision_ts + 1]
    trend = ema_state([b["close"] for b in h1_asof], 9, 20)
    if trend not in ("bullish", "bearish"):
        return []
    direction = "buy" if trend == "bullish" else "sell"
    side = "up" if direction == "buy" else "down"
    if direction == "buy" and not forming["close"] > forming["open"]:
        return []
    if direction == "sell" and not forming["close"] < forming["open"]:
        return []

    h1_atr = atr(h1_asof, 14)
    atr_mult = float(sc.get("atr_buffer_mult") or 0.15)
    need_buf = uses_atr_buffer(inst)
    buffer = (atr_mult * h1_atr) if need_buf and h1_atr and h1_atr > 0 else (None if need_buf else 0.0)

    if direction == "buy":
        levels = [("PDH", pdh), ("Asian high", asian_h)]
    else:
        levels = [("PDL", pdl), ("Asian low", asian_l)]

    inside = [(i, b) for i, b in enumerate(m5_asof) if period - 1 <= b["ts"] < period + M15_SEC - 1]
    if not inside:
        return []
    m5_idx, m5bar = inside[-1]

    chosen: tuple[str, float] | None = None
    for name, level in levels:
        if level is None:
            continue
        was_inside = prev_close <= level if side == "up" else prev_close >= level
        if not was_inside:
            continue
        if not crossed_level(forming["close"], level, side, need_buf, buffer):
            continue
        if not crossed_level(m5bar["close"], level, side, need_buf, buffer):
            continue
        chosen = (name, float(level))
        break
    if chosen is None:
        return []
    level_name, level = chosen

    rvol_min = float(sc.get("rvol_min") or 1.5)
    rvol_pass, rvol = volume_ok(m5_asof, m5_idx, rvol_min)
    if not rvol_pass:
        return []

    wick_need = float(sc.get("wick_frac") or 0.5)
    info5 = m5_confirmation(
        forming, direction, m5_asof, level=level, kind="breakout", wick_frac=wick_need
    )
    info1 = m1_confirmation(
        direction, m1_asof, decision_ts - M5_SEC, decision_ts, wick_need, level=level
    )
    if info5["m5_confirm"] is not True or info1["m1_confirm"] is not True:
        log(
            f"mtf {inst['symbol']} early breakout zavrnjen "
            f"(M5={info5['m5_confirm']}, M1={info1['m1_confirm']})."
        )
        return []

    if news is None:
        events, ok = load_news_calendar(sc)
    else:
        events, ok = news
    note_extra = ""
    if not ok:
        note_extra = " koledar ni na voljo"
    else:
        blocked = high_impact_block(inst["symbol"], decision_ts, events, sc)
        if blocked is not None:
            log(
                f"news_block {inst['symbol']} breakout {direction} "
                f"{blocked.get('title')} {blocked.get('country')} {blocked.get('date')}"
            )
            return []

    offset = float(inst.get("offset") or 0)
    rvol_txt = "M5 RVOL ni na voljo (ni volumna)" if rvol is None else f"M5 RVOL {rvol:.2f}"
    side_txt = "nad" if direction == "buy" else "pod"
    if trigger_tf == "M1":
        bar_ts = decision_ts - M1_SEC
    else:
        bar_ts = float(m5bar["ts"])
    cand = {
        "symbol": inst["symbol"],
        "event": "candidate",
        "type": "breakout",
        "direction": direction,
        "level": round(level, 6),
        "price": forming["close"] - offset,
        "broker_price": forming["close"],
        "rvol": None if rvol is None else round(rvol, 2),
        "rsi": None,
        "ema_state": trend,
        "time_utc": datetime.fromtimestamp(decision_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bar_ts": bar_ts,
        "close_ts": decision_ts,
        "trigger_tf": trigger_tf,
        "session_open": window,
        "note": (
            f"Odprtje {window}: M5 zaprl {side_txt} {level_name} {fmt_price(level, inst)}, "
            f"{rvol_txt} (prag {rvol_min:g}), tekoča M15 je čez raven, "
            f"H1 EMA 9/20 {trend}. MTF A+: M5 struktura in M1 timing. "
            f"Zaključena M15 še vedno zahteva RVOL ≥ {rvol_min:g}.{note_extra}"
        ),
        "pwh": None,
        "pwl": None,
        "dist_pwh": None,
        "dist_pwl": None,
        "dist_round": None,
        "round_level": None,
    }
    _attach_mtf(cand, info5, info1, "A+")
    return [cand]


def iter_early_decisions(
    m5: list[dict[str, Any]] | None,
    m1: list[dict[str, Any]] | None,
    cursor_m5: float,
    cursor_m1: float,
    now: float,
    sc: dict[str, Any],
) -> tuple[list[tuple[float, str]], float, float]:
    """Completed M1 and M5 closes to evaluate on this loop.

    Outside the session-open windows the cursors advance and the list is
    empty: M1 is not polled bar-by-bar, and the M15 path still computes
    confirmation when a 15-minute bar completes. Inside a window, each new
    M1 or M5 whose close is on a weekday inside 08:00-21:00 becomes one
    decision. A close that is also an M15 close is skipped here so
    evaluate_bar owns it. When an M1 and an M5 close together the decision
    is labeled M5.
    """
    events: list[tuple[float, str]] = []

    def consider(
        bars: list[dict[str, Any]] | None,
        duration: int,
        tf: str,
        cursor: float,
    ) -> float:
        if not bars:
            return cursor
        new_cursor = cursor
        for bar in sorted(bars, key=lambda item: item["ts"]):
            if bar["ts"] <= cursor:
                continue
            close_ts = float(bar["ts"]) + duration
            if close_ts > now + 2:
                break
            new_cursor = float(bar["ts"])
            if on_m15_boundary(close_ts):
                continue
            if in_session_open_window(close_ts, sc) is None:
                continue
            if not in_active_hours(
                close_ts, sc["active_start"], sc["active_end"], sc.get("weekdays_only", True)
            ):
                continue
            events.append((close_ts, tf))
        return new_cursor

    cursor_m5 = consider(m5, M5_SEC, "M5", cursor_m5)
    cursor_m1 = consider(m1, M1_SEC, "M1", cursor_m1)
    merged: dict[float, str] = {}
    for close_ts, tf in events:
        prev = merged.get(close_ts)
        if prev is None or tf == "M5":
            merged[close_ts] = tf
    decisions = sorted(merged.items())
    return decisions, cursor_m5, cursor_m1


_WEBHOOK_OMIT = {"bar_ts", "close_ts"}


def dispatch_candidates(
    cfg: dict[str, Any],
    sc: dict[str, Any],
    state: dict[str, Any],
    inst: dict[str, Any],
    cands: list[dict[str, Any]],
    *,
    send: bool,
    now: float,
) -> list[dict[str, Any]]:
    """Apply the age limit, cooldown, and daily caps, then optionally send.

    send=False (``--no-send``) still consumes the cap, matching the previous
    scanner, and does not call the webhook. A failed send does not consume it.
    """
    max_age = float(sc.get("max_signal_age_minutes") or 45) * 60.0
    fired: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cand in cands:
        if cand["type"] in seen:
            continue
        seen.add(cand["type"])
        close_ts = float(cand["close_ts"])
        bar_ts = float(cand["bar_ts"])
        if (now - close_ts) > max_age:
            log(
                f"{inst['symbol']} {cand['type']} {cand['direction']} "
                f"zamujen (sveča starejša od {int(max_age // 60)} min)."
            )
            continue
        if not can_send(state, inst, cand["type"], bar_ts, sc):
            log(
                f"{inst['symbol']} {cand['type']} {cand['direction']} "
                f"zavrnjen (cooldown ali dnevni cap)."
            )
            continue
        if send:
            payload = {k: v for k, v in cand.items() if k not in _WEBHOOK_OMIT}
            ok, detail = send_webhook(cfg, payload)
            if detail == SKIPPED_MISSING_SECRETS:
                continue
            log(
                f"WEBHOOK candidate {inst['symbol']} {cand['type']} "
                f"{cand['direction']} {cand.get('quality')} {cand.get('trigger_tf')} "
                f"-> {'OK' if ok else 'NAPAKA'}: {detail}"
            )
            if not ok:
                continue
        mark_sent(state, inst, cand["type"], bar_ts)
        fired.append(cand)
    return fired


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


def _fetch_optional(inst: dict[str, Any], interval: str, range_: str) -> list[dict[str, Any]] | None:
    try:
        bars = completed(fetch_candles(inst, interval, range_))
    except Exception as exc:
        log(f"{inst.get('symbol', '?')} {interval}: ni podatka ({exc})")
        return None
    return bars


def fetch_instrument(
    inst: dict[str, Any],
    *,
    m1_range: str = "1d",
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]] | None,
    list[dict[str, Any]] | None,
    list[dict[str, Any]] | None,
]:
    """M15, H1, M5, M1, daily. M1 is required for timing; a failed M1 fetch is None.

    Live scans use one day of M1. Backfill asks for 5d so older session bars
    can still be confirmed. Yahoo serves about a week of 1m; Binance pages
    further only for the 5d/7d ranges.
    """
    m15 = completed(fetch_candles(inst, "15m", "1mo"))
    time.sleep(0.15)
    h1 = completed(fetch_candles(inst, "1h", "3mo"))
    time.sleep(0.15)
    m5 = _fetch_optional(inst, "5m", "1mo")
    time.sleep(0.1)
    m1 = _fetch_optional(inst, "1m", m1_range)
    time.sleep(0.1)
    daily = _fetch_optional(inst, "1d", "6mo")
    return m15, h1, m5, m1, daily


def backfill(cfg: dict[str, Any], sc: dict[str, Any], days: int = 1) -> list[dict[str, Any]]:
    """Replay M15 decisions and session-open early breakouts, then apply caps.

    `days` is the number of recent weekdays, including today. The default is
    today only. This never sends a webhook; it only reports what the live
    rules would have emitted.
    """
    wanted = recent_trading_days(days)
    now = time.time()
    fake_state: dict[str, Any] = {"day": day_key(), "sent_today": 0, "sent_today_by_symbol": {}, "last": {}}
    flagged: list[dict[str, Any]] = []
    news = load_news_calendar(sc)

    for inst in sc["universe"]:
        try:
            m15, h1, m5, m1, daily = fetch_instrument(inst, m1_range="5d")
            log(
                f"backfill {inst['symbol']}: M15={len(m15)} H1={len(h1)} "
                f"M5={'ni' if m5 is None else len(m5)} "
                f"M1={'ni' if m1 is None else len(m1)} "
                f"D1={'ni' if daily is None else len(daily)}"
            )
        except FetchError as exc:
            log(f"backfill {inst['symbol']}: ni podatka ({exc})")
            continue
        except Exception as exc:
            log(f"backfill {inst['symbol']}: napaka {exc}")
            traceback.print_exc()
            continue
        for idx, bar in enumerate(m15):
            close_ts = bar["ts"] + M15_SEC
            if close_ts > now + 2:
                continue
            if local_dt(bar["ts"]).date() not in wanted:
                continue
            if not in_active_hours(close_ts, sc["active_start"], sc["active_end"], sc["weekdays_only"]):
                continue
            cands = evaluate_bar(inst, m15, idx, h1, sc, m5=m5, daily=daily, news=news, m1=m1)
            seen = set()
            for cand in cands:
                if cand["type"] in seen:
                    continue
                seen.add(cand["type"])
                flagged.append(dict(cand))
        decisions, _c5, _c1 = iter_early_decisions(m5, m1, -1.0, -1.0, now, sc)
        for decision_ts, trigger_tf in decisions:
            if local_dt(decision_ts).date() not in wanted:
                continue
            for cand in evaluate_early_breakout(
                inst, m15, m5, m1, h1, sc, decision_ts, trigger_tf=trigger_tf, news=news
            ):
                flagged.append(dict(cand))
        time.sleep(0.2)
    # Close-time order matches a live run: an early breakout at 09:05 is
    # counted before the 09:15 M15 close, so the cooldown suppresses the echo.
    flagged.sort(key=lambda c: (c.get("close_ts") or 0, c.get("symbol") or "", c.get("type") or ""))
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


def _seed_cursor(bucket: dict[str, Any], key: str, bars: list[dict[str, Any]] | None) -> bool:
    """True when this instrument already had a cursor for these bars.

    The first time a feed is seen the cursor jumps to the latest bar and
    that history is not replayed. Later loops only see bars after it, and
    dispatch still drops anything older than max_signal_age_minutes.
    """
    if not bars:
        return False
    if key in bucket:
        return True
    bucket[key] = bars[-1]["ts"]
    return False


def scan_live(cfg: dict[str, Any], sc: dict[str, Any], state: dict[str, Any], send: bool = True) -> list[dict[str, Any]]:
    today = day_key()
    if state.get("day") != today:
        state["day"] = today
        state["sent_today"] = 0
        state["sent_today_by_symbol"] = {}
    fired: list[dict[str, Any]] = []
    last_processed = state.setdefault("last_processed", {})
    last_m5 = state.setdefault("last_m5", {})
    last_m1 = state.setdefault("last_m1", {})
    news = load_news_calendar(sc)
    now = time.time()
    boost = in_session_open_window(now, sc)

    for inst in sc["universe"]:
        try:
            m15, h1, m5, m1, daily = fetch_instrument(inst)
        except FetchError as exc:
            log(f"{inst['symbol']}: ni podatka ({exc})")
            continue
        except Exception as exc:
            log(f"{inst['symbol']}: napaka {exc}")
            traceback.print_exc()
            continue
        if not m15:
            continue
        newest = m15[-1]
        prev = last_processed.get(inst["id"])
        m5n = "ni" if m5 is None else str(len(m5))
        m1n = "ni" if m1 is None else str(len(m1))
        # First run: remember the last completed bar and do not back-send it.
        if prev is None:
            last_processed[inst["id"]] = newest["ts"]
            _seed_cursor(last_m5, inst["id"], m5)
            _seed_cursor(last_m1, inst["id"], m1)
            log(
                f"{inst['symbol']} broker={fmt_price(newest['close'], inst)} "
                f"M15={len(m15)} H1={len(h1)} M5={m5n} M1={m1n} "
                f"(začetno stanje, brez pošiljanja)"
            )
            continue
        pending = [(i, b) for i, b in enumerate(m15) if b["ts"] > float(prev)]
        pending.sort(key=lambda item: item[1]["ts"])
        for _idx, bar in pending:
            last_processed[inst["id"]] = bar["ts"]

        had_m5 = _seed_cursor(last_m5, inst["id"], m5)
        had_m1 = _seed_cursor(last_m1, inst["id"], m1)
        decisions: list[tuple[float, str]] = []
        # A cursor seeded on this loop means "first sight": do not replay the
        # M1/M5 history we just anchored. The next loop evaluates new closes.
        if had_m5 and had_m1:
            decisions, c5, c1 = iter_early_decisions(
                m5, m1, float(last_m5[inst["id"]]), float(last_m1[inst["id"]]), now, sc
            )
            last_m5[inst["id"]] = c5
            last_m1[inst["id"]] = c1

        jobs: list[tuple[float, int, str, Any]] = []
        for idx, bar in pending:
            close_ts = bar["ts"] + M15_SEC
            if in_active_hours(close_ts, sc["active_start"], sc["active_end"], sc["weekdays_only"]):
                jobs.append((close_ts, 0, "m15", idx))
        for decision_ts, trigger_tf in decisions:
            jobs.append((decision_ts, 1, "early", trigger_tf))
        jobs.sort()
        for _close_ts, _ord, kind, payload in jobs:
            if kind == "m15":
                cands = evaluate_bar(
                    inst, m15, int(payload), h1, sc, m5=m5, daily=daily, news=news, m1=m1
                )
            else:
                cands = evaluate_early_breakout(
                    inst,
                    m15,
                    m5,
                    m1,
                    h1,
                    sc,
                    float(_close_ts),
                    trigger_tf=str(payload),
                    news=news,
                )
            fired.extend(
                dispatch_candidates(cfg, sc, state, inst, cands, send=send, now=now)
            )
        if pending or decisions or boost:
            log(
                f"{inst['symbol']} broker={fmt_price(newest['close'], inst)} "
                f"M15={len(m15)} H1={len(h1)} M5={m5n} M1={m1n} "
                f"odprtje={boost or '-'}"
            )
        time.sleep(0.2)
    return fired


def main() -> int:
    parser = argparse.ArgumentParser(description="FundedNext opportunity scanner")
    parser.add_argument("--backfill", action="store_true", help="Preglej M15 in izpiši kandidate, brez webhooka.")
    parser.add_argument("--days", type=int, default=1, help="Koliko zadnjih trgovalnih dni zajame --backfill.")
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
        days = max(1, int(args.days or 1))
        log(
            f"Backfill zadnjih {days} trgovalnih dni "
            f"(okno {sc['active_start']}-{sc['active_end']} "
            f"Europe/Ljubljana, cap {sc['max_alerts_per_day']}/dan, "
            f"{sc.get('max_alerts_per_symbol_per_day', 2)}/simbol, "
            f"cooldown {sc['cooldown_minutes']} min). Brez webhooka."
        )
        flagged = backfill(cfg, sc, days=days)
        sent_like = [c for c in flagged if not c.get("suppressed")]
        suppressed = [c for c in flagged if c.get("suppressed")]
        def _by_type(rows: list[dict[str, Any]]) -> dict[str, int]:
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["type"]] = counts.get(row["type"], 0) + 1
            return dict(sorted(counts.items()))

        log(
            f"BACKFILL: {len(flagged)} zadetkov filtrov, "
            f"{len(sent_like)} bi šlo ven po cap/cooldown, "
            f"{len(suppressed)} bi bilo zadržanih. "
            f"Po tipu raw={_by_type(flagged)} po_cap={_by_type(sent_like)}."
        )
        for c in flagged:
            flag = "CAP" if c.get("suppressed") else "ALERT"
        log(
            f"  {flag} {c['time_utc']} {c['symbol']} {c['type']} {c['direction']} "
            f"{c.get('quality')} {c.get('trigger_tf')} "
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
    windows = ", ".join(f"{name} {start}-{end}" for name, start, end in session_open_windows(sc))
    log(
        f"Scanner zagnan. Instrumentov: {len(sc['universe'])}. "
        f"Interval: {sc['interval_seconds']} s, "
        f"ob odprtju {sc.get('boost_interval_seconds', 60)} s ({windows}). "
        f"MTF: M1+M5+M15, H1 trend. "
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
        wait = scanner_sleep_seconds(time.time(), sc, elapsed)
        sleep_secs(wait)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Ustavljeno (Ctrl+C).")
        sys.exit(0)
