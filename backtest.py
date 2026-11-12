#!/usr/bin/env python3
"""Offline backtest of scanner candidates.

This file is not imported by monitor.py or scanner.py and is not started by
the GitHub Actions workflow. It only reads the scanner rules.

Prices are the offset-adjusted Yahoo/Binance series the scanner already uses.
P&L is in R, then 50 USD per 1R, minus 0.1R of spread and commission per
closed trade. A buy/sell stop that is not filled within 60 minutes is not a
trade. A position still open at 21:00 Europe/Ljubljana is closed at that
M5 bar's close and counted as expired.

This replays the hybrid on this branch: completed M15 decisions, plus
session-open early breakouts that do not share the breakout cooldown.
On 5–11 Nov 2026 that hybrid was +1.71R capped at 2R, behind the M15
baseline at +4.71R. It does not send a webhook.
"""

from __future__ import annotations

import bisect
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fnlib import LJ_TZ, atr, load_config
from scanner import (
    can_send,
    day_key,
    evaluate_bar,
    evaluate_early_breakout,
    fetch_instrument,
    hm_on_day,
    in_active_hours,
    iter_early_decisions,
    load_news_calendar,
    local_dt,
    log,
    mark_sent,
    recent_trading_days,
    scanner_cfg,
)

RISK_USD = 50.0
COST_R = 0.1
MAX_OPEN = 3
STOP_VALID_SEC = 60 * 60
SESSION_END = "21:00"


@dataclass
class Signal:
    symbol: str
    ctype: str
    direction: str
    close_ts: float
    close: float
    m15_low: float
    m15_high: float
    m5_low: float | None
    m5_high: float | None
    m5_trigger: float | None
    m5_confirm: bool | None
    atr: float
    bars: list[dict[str, Any]]
    index: int
    early: bool = False
    anchor_ts: float = 0.0


@dataclass
class Working:
    signal: Signal
    status: str  # pending | open
    stop: float | None
    entry: float | None = None
    sl: float = 0.0
    tp: float = 0.0
    risk: float = 0.0
    i: int = 0
    expire_ts: float = 0.0
    session_end: float = 0.0
    active_from: float = 0.0
    last_close: float | None = None
    entry_mode: str = ""
    exit_ts: float = 0.0


@dataclass
class Closed:
    signal: Signal
    reason: str  # tp | sl | session
    entry: float
    exit: float
    exit_ts: float
    net_r: float
    usd: float
    entry_mode: str


@dataclass
class RunResult:
    trades: list[Closed] = field(default_factory=list)
    cancelled: int = 0
    skipped_concurrency: int = 0
    skipped_data: int = 0
    close_entries: int = 0
    stop_entries: int = 0


def trade_levels(
    entry: float,
    direction: str,
    m15_low: float,
    m15_high: float,
    m5_low: float | None,
    m5_high: float | None,
    atr_value: float,
    tp_r: float,
) -> tuple[float, float, float]:
    """Structural stop, widened to at least 0.5× M15 ATR, then a TP multiple."""
    if direction == "buy":
        structure = m15_low if m5_low is None else min(m15_low, m5_low)
        sl = structure - 0.1 * atr_value
        risk = entry - sl
        if risk < 0.5 * atr_value:
            risk = 0.5 * atr_value
            sl = entry - risk
        tp = entry + tp_r * risk
    else:
        structure = m15_high if m5_high is None else max(m15_high, m5_high)
        sl = structure + 0.1 * atr_value
        risk = sl - entry
        if risk < 0.5 * atr_value:
            risk = 0.5 * atr_value
            sl = entry + risk
        tp = entry - tp_r * risk
    return sl, tp, risk


def try_fill(bar: dict[str, Any], direction: str, stop: float) -> float | None:
    if direction == "buy":
        if bar["high"] < stop:
            return None
        if bar["open"] >= stop:
            return float(bar["open"])
        return stop
    if bar["low"] > stop:
        return None
    if bar["open"] <= stop:
        return float(bar["open"])
    return stop


def exit_on_bar(
    direction: str,
    sl: float,
    tp: float,
    bar: dict[str, Any],
) -> tuple[str, float] | None:
    """SL wins when the same M5 bar contains both SL and TP."""
    if direction == "buy":
        if bar["open"] <= sl:
            return "sl", float(bar["open"])
        if bar["open"] >= tp:
            return "tp", float(bar["open"])
        hit_sl = bar["low"] <= sl
        hit_tp = bar["high"] >= tp
    else:
        if bar["open"] >= sl:
            return "sl", float(bar["open"])
        if bar["open"] <= tp:
            return "tp", float(bar["open"])
        hit_sl = bar["high"] >= sl
        hit_tp = bar["low"] <= tp
    if hit_sl:
        return "sl", sl
    if hit_tp:
        return "tp", tp
    return None


def _session_end(close_ts: float) -> float:
    day = local_dt(close_ts).date()
    return hm_on_day(day, SESSION_END).timestamp()


def _on_bar(item: Working, bar: dict[str, Any], tp_r: float) -> tuple[str, float | None] | None:
    if bar["ts"] < item.active_from:
        return None
    if item.status == "pending":
        if bar["ts"] >= item.expire_ts or bar["ts"] >= item.session_end:
            return ("cancel", None, bar["ts"])
        assert item.stop is not None
        fill = try_fill(bar, item.signal.direction, item.stop)
        if fill is None:
            item.last_close = float(bar["close"])
            if bar["ts"] + 300 >= min(item.expire_ts, item.session_end):
                return ("cancel", None, bar["ts"] + 300)
            return None
        sl, tp, risk = trade_levels(
            fill,
            item.signal.direction,
            item.signal.m15_low,
            item.signal.m15_high,
            item.signal.m5_low,
            item.signal.m5_high,
            item.signal.atr,
            tp_r,
        )
        item.status = "open"
        item.entry = fill
        item.sl = sl
        item.tp = tp
        item.risk = risk
        item.last_close = float(bar["close"])
    if item.status == "open" and item.entry is not None:
        hit = exit_on_bar(item.signal.direction, item.sl, item.tp, bar)
        if hit:
            return (hit[0], hit[1], bar["ts"] + 300)
        if bar["ts"] < item.session_end <= bar["ts"] + 300:
            return ("session", float(bar["close"]), bar["ts"] + 300)
        item.last_close = float(bar["close"])
    return None


def _finalize(item: Working, outcome: tuple[str, float | None, float], result: RunResult) -> None:
    reason, price, exit_ts = outcome
    item.exit_ts = exit_ts
    if reason == "cancel" or price is None or item.entry is None or item.risk <= 0:
        result.cancelled += 1
        return
    if item.signal.direction == "buy":
        raw_r = (price - item.entry) / item.risk
    else:
        raw_r = (item.entry - price) / item.risk
    net_r = raw_r - COST_R
    result.trades.append(
        Closed(
            signal=item.signal,
            reason=reason,
            entry=item.entry,
            exit=price,
            exit_ts=exit_ts,
            net_r=net_r,
            usd=net_r * RISK_USD,
            entry_mode=item.entry_mode,
        )
    )


def _place(sig: Signal, tp_r: float, result: RunResult) -> Working | None:
    if sig.atr <= 0 or not sig.bars:
        result.skipped_data += 1
        return None
    use_stop = sig.m5_confirm is True and sig.m5_trigger is not None
    session_end = _session_end(sig.close_ts)
    if use_stop:
        result.stop_entries += 1
        item = Working(
            signal=sig,
            status="pending",
            stop=float(sig.m5_trigger),
            i=sig.index,
            expire_ts=sig.close_ts + STOP_VALID_SEC,
            session_end=session_end,
            active_from=sig.close_ts,
            entry_mode="stop",
        )
        return item
    result.close_entries += 1
    sl, tp, risk = trade_levels(
        sig.close,
        sig.direction,
        sig.m15_low,
        sig.m15_high,
        sig.m5_low,
        sig.m5_high,
        sig.atr,
        tp_r,
    )
    return Working(
        signal=sig,
        status="open",
        stop=None,
        entry=sig.close,
        sl=sl,
        tp=tp,
        risk=risk,
        i=sig.index,
        expire_ts=sig.close_ts + STOP_VALID_SEC,
        session_end=session_end,
        active_from=sig.close_ts,
        last_close=sig.close,
        entry_mode="m15_close",
    )


def _advance(active: list[Working], t1: float, tp_r: float, result: RunResult) -> None:
    while True:
        nxt: tuple[float, Working] | None = None
        for item in active:
            if item.i < len(item.signal.bars) and item.signal.bars[item.i]["ts"] < t1:
                ts = item.signal.bars[item.i]["ts"]
                if nxt is None or ts < nxt[0]:
                    nxt = (ts, item)
        if nxt is None:
            break
        item = nxt[1]
        bar = item.signal.bars[item.i]
        item.i += 1
        outcome = _on_bar(item, bar, tp_r)
        if outcome:
            active.remove(item)
            _finalize(item, outcome, result)
    for item in list(active):
        deadline = item.session_end if item.status == "open" else min(item.expire_ts, item.session_end)
        if deadline > t1:
            continue
        active.remove(item)
        if item.status == "open" and item.last_close is not None:
            _finalize(item, ("session", item.last_close, item.session_end), result)
        else:
            _finalize(item, ("cancel", None, deadline), result)


def simulate(signals: list[Signal], tp_r: float) -> RunResult:
    """Max 3 working-or-open positions, and one per symbol.

    A resting stop occupies a slot until it fills or expires, so a later
    signal cannot stack a fourth position behind it.
    """
    result = RunResult()
    active: list[Working] = []
    ordered = sorted(signals, key=lambda s: (s.close_ts, s.symbol, s.ctype))
    i = 0
    while i < len(ordered):
        ts = ordered[i].close_ts
        group = []
        while i < len(ordered) and ordered[i].close_ts == ts:
            group.append(ordered[i])
            i += 1
        _advance(active, ts, tp_r, result)
        for sig in group:
            if any(item.signal.symbol == sig.symbol for item in active) or len(active) >= MAX_OPEN:
                result.skipped_concurrency += 1
                continue
            item = _place(sig, tp_r, result)
            if item is not None:
                active.append(item)
    if signals:
        horizon = max(_session_end(s.close_ts) for s in signals) + 300
        _advance(active, horizon, tp_r, result)
    return result


def collect(days: list) -> tuple[list[Signal], list[Signal]]:
    cfg = load_config()
    sc = scanner_cfg(cfg)
    wanted = set(days)
    news = load_news_calendar(sc)
    raw: list[Signal] = []
    for inst in sc["universe"]:
        m15, h1, m5, m1, daily = fetch_instrument(inst, m1_range="5d")
        if not m5:
            log(f"{inst['symbol']}: ni M5, kandidati tega instrumenta v backtestu nimajo izstopa.")
            m5_bars: list[dict[str, Any]] = []
        else:
            m5_bars = m5
        if not m1:
            log(f"{inst['symbol']}: ni M1, zgodnji breakout tega instrumenta ni mogoč.")
        starts = [b["ts"] for b in m5_bars]
        for idx, bar in enumerate(m15):
            close_ts = bar["ts"] + 15 * 60
            if local_dt(bar["ts"]).date() not in wanted:
                continue
            if not in_active_hours(close_ts, sc["active_start"], sc["active_end"], sc["weekdays_only"]):
                continue
            atr_value = atr(m15[: idx + 1], 14)
            if not atr_value or atr_value <= 0:
                continue
            seen: set[str] = set()
            for cand in evaluate_bar(inst, m15, idx, h1, sc, m5=m5, daily=daily, news=news, m1=m1):
                if cand["type"] in seen:
                    continue
                seen.add(cand["type"])
                raw.append(
                    Signal(
                        symbol=cand["symbol"],
                        ctype=cand["type"],
                        direction=cand["direction"],
                        close_ts=close_ts,
                        close=float(cand["broker_price"]),
                        m15_low=float(bar["low"]),
                        m15_high=float(bar["high"]),
                        m5_low=cand.get("m5_low"),
                        m5_high=cand.get("m5_high"),
                        m5_trigger=cand.get("m5_trigger"),
                        m5_confirm=cand.get("m5_confirm"),
                        atr=float(atr_value),
                        bars=m5_bars,
                        index=bisect.bisect_left(starts, close_ts),
                    )
                )
        if m5 and m1:
            decisions, _, _ = iter_early_decisions(m5, m1, -1.0, -1.0, time.time(), sc)
            for decision_ts, trigger_tf in decisions:
                if local_dt(decision_ts).date() not in wanted:
                    continue
                period = float(int(decision_ts) - (int(decision_ts) % (15 * 60)))
                prior = [b for b in m15 if b["ts"] + 15 * 60 <= period + 1]
                atr_value = atr(prior, 14) if prior else None
                if not atr_value or atr_value <= 0:
                    continue
                parts = [b for b in m1 if period - 1 <= b["ts"] and b["ts"] + 60 <= decision_ts + 1]
                for cand in evaluate_early_breakout(
                    inst, m15, m5, m1, h1, sc, decision_ts, trigger_tf=trigger_tf, news=news
                ):
                    if parts:
                        low = min(b["low"] for b in parts)
                        high = max(b["high"] for b in parts)
                    else:
                        low = float(cand.get("m5_low") or cand["broker_price"])
                        high = float(cand.get("m5_high") or cand["broker_price"])
                    raw.append(
                        Signal(
                            symbol=cand["symbol"],
                            ctype=cand["type"],
                            direction=cand["direction"],
                            close_ts=decision_ts,
                            close=float(cand["broker_price"]),
                            m15_low=low,
                            m15_high=high,
                            m5_low=cand.get("m5_low"),
                            m5_high=cand.get("m5_high"),
                            m5_trigger=cand.get("m5_trigger"),
                            m5_confirm=cand.get("m5_confirm"),
                            atr=float(atr_value),
                            bars=m5_bars,
                            index=bisect.bisect_left(starts, decision_ts),
                            early=True,
                            anchor_ts=float(cand["bar_ts"]),
                        )
                    )
    raw.sort(key=lambda s: (s.close_ts, s.symbol, s.ctype))
    state: dict[str, Any] = {"day": day_key(), "sent_today": 0, "sent_today_by_symbol": {}, "last": {}}
    capped: list[Signal] = []
    for sig in raw:
        inst = {"id": sig.symbol, "symbol": sig.symbol}
        anchor = sig.anchor_ts or (sig.close_ts - 15 * 60)
        ctype = "breakout_early" if sig.early else sig.ctype
        if not can_send(state, inst, ctype, anchor, sc):
            continue
        mark_sent(state, inst, ctype, anchor)
        capped.append(sig)
    return capped, raw


def _stats(result: RunResult) -> dict[str, Any]:
    trades = sorted(result.trades, key=lambda t: (t.exit_ts, t.signal.symbol, t.signal.ctype))
    wins = sum(1 for t in trades if t.reason == "tp")
    losses = sum(1 for t in trades if t.reason == "sl")
    expired = sum(1 for t in trades if t.reason == "session")
    n = len(trades)
    net_r = sum(t.net_r for t in trades)
    usd = sum(t.usd for t in trades)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    by_day: dict[str, float] = defaultdict(float)
    by_type: dict[str, list[Closed]] = defaultdict(list)
    by_symbol: dict[str, list[Closed]] = defaultdict(list)
    for t in trades:
        equity += t.usd
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        by_day[local_dt(t.exit_ts).strftime("%Y-%m-%d")] += t.usd
        by_type[t.signal.ctype].append(t)
        by_symbol[t.signal.symbol].append(t)
    if by_day:
        best = max(by_day.items(), key=lambda kv: kv[1])
        worst = min(by_day.items(), key=lambda kv: kv[1])
    else:
        best = ("-", 0.0)
        worst = ("-", 0.0)

    def pack(rows: list[Closed]) -> dict[str, Any]:
        w = sum(1 for t in rows if t.reason == "tp")
        return {
            "trades": len(rows),
            "wins": w,
            "net_r": sum(t.net_r for t in rows),
            "usd": sum(t.usd for t in rows),
            "win_rate": (w / len(rows)) if rows else None,
        }

    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "cancelled": result.cancelled,
        "skipped_concurrency": result.skipped_concurrency,
        "skipped_data": result.skipped_data,
        "close_entries": result.close_entries,
        "stop_entries": result.stop_entries,
        "win_rate": (wins / n) if n else None,
        "net_r": net_r,
        "usd": usd,
        "max_dd": max_dd,
        "best_day": best,
        "worst_day": worst,
        "by_type": {k: pack(v) for k, v in sorted(by_type.items())},
        "by_symbol": {k: pack(v) for k, v in sorted(by_symbol.items())},
    }


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{100 * value:.1f}%"


def _fmt_day(pair: tuple[str, float]) -> str:
    return f"{pair[0]} {pair[1]:+.0f}"


def print_report(rows: list[tuple[str, str, dict[str, Any]]]) -> None:
    header = (
        f"{'TP':>4} {'set':<9} {'sig':>4} {'tr':>4} {'W':>3} {'L':>3} {'exp':>4} "
        f"{'canc':>4} {'skip':>4} {'win':>6} {'R':>8} {'USD':>8} {'maxDD':>7} "
        f"{'best':<16} {'worst':<16} {'stop':>4} {'m15':>4}"
    )
    print(header)
    print("-" * len(header))
    for tp, name, st in rows:
        print(
            f"{tp:>4} {name:<9} {st['signals']:>4} {st['trades']:>4} {st['wins']:>3} "
            f"{st['losses']:>3} {st['expired']:>4} {st['cancelled']:>4} "
            f"{st['skipped_concurrency']:>4} {_fmt_pct(st['win_rate']):>6} "
            f"{st['net_r']:>8.2f} {st['usd']:>8.0f} {st['max_dd']:>7.0f} "
            f"{_fmt_day(st['best_day']):<16} {_fmt_day(st['worst_day']):<16} "
            f"{st['stop_entries']:>4} {st['close_entries']:>4}"
        )


def print_breakdown(title: str, st: dict[str, Any]) -> None:
    print(f"\n{title}")
    print(f"{'bucket':<10} {'tr':>4} {'W':>3} {'win':>6} {'R':>8} {'USD':>8}")
    for label, rows in (("type", st["by_type"]), ("symbol", st["by_symbol"])):
        for key, rec in rows.items():
            print(
                f"{key:<10} {rec['trades']:>4} {rec['wins']:>3} {_fmt_pct(rec['win_rate']):>6} "
                f"{rec['net_r']:>8.2f} {rec['usd']:>8.0f}"
            )
        if label == "type":
            print()


def self_check() -> None:
    """Tiny deterministic paths so the fill and R math do not depend on Yahoo."""
    base = datetime(2026, 11, 11, 8, 0, tzinfo=LJ_TZ).timestamp()

    def m5(i: int, o: float, h: float, l: float, c: float) -> dict[str, Any]:
        ts = base + i * 300
        return {"ts": ts, "open": o, "high": h, "low": l, "close": c}

    bars = [m5(i, 100, 101, 99, 100) for i in range(1, 8)]
    bars.append(m5(8, 100, 112, 99.5, 111))
    sig = Signal(
        symbol="TEST",
        ctype="pullback",
        direction="buy",
        close_ts=base,
        close=100,
        m15_low=98,
        m15_high=101,
        m5_low=None,
        m5_high=None,
        m5_trigger=None,
        m5_confirm=None,
        atr=10,
        bars=bars,
        index=0,
    )
    # Structural SL 98-1=97 is only 3 away, so it widens to 0.5×ATR = 5. TP 2R = 110.
    sl, tp, risk = trade_levels(100, "buy", 98, 101, None, None, 10, 2)
    assert abs(risk - 5) < 1e-9 and abs(sl - 95) < 1e-9 and abs(tp - 110) < 1e-9
    out = simulate([sig], 2)
    assert len(out.trades) == 1 and out.trades[0].reason == "tp"
    assert abs(out.trades[0].net_r - 1.9) < 1e-9

    both = [m5(1, 100, 112, 90, 100)]
    sig_sl = Signal(
        "TEST", "breakout", "buy", base, 100, 98, 101, None, None, None, None, 10, both, 0
    )
    out_sl = simulate([sig_sl], 2)
    assert out_sl.trades[0].reason == "sl" and abs(out_sl.trades[0].net_r - (-1.1)) < 1e-9

    quiet = [m5(i, 100, 100.2, 99.8, 100) for i in range(1, 4)]
    stop_sig = Signal(
        "TEST", "breakout", "buy", base, 100, 98, 101, 99, 100.5, 100.5, True, 10, quiet, 0
    )
    out_stop = simulate([stop_sig], 2)
    assert out_stop.trades == [] and out_stop.cancelled == 1

    session_bars = [m5(i, 100, 100.4, 99.6, 100.1) for i in range(1, 200)]
    # 08:00 LJ + many bars crosses 21:00.
    sig_flat = Signal(
        "TEST", "retest", "buy", base, 100, 98, 101, None, None, None, None, 10, session_bars, 0
    )
    out_flat = simulate([sig_flat], 2)
    assert len(out_flat.trades) == 1 and out_flat.trades[0].reason == "session"

    burst = []
    for n, sym in enumerate(("AAA", "BBB", "CCC", "DDD")):
        burst.append(
            Signal(sym, "breakout", "buy", base, 100, 98, 101, None, None, None, None, 10, [m5(1, 100, 100, 100, 100)], 0)
        )
    out_burst = simulate(burst, 2)
    assert out_burst.skipped_concurrency == 1
    assert out_burst.close_entries == 3
    log("backtest self-check ok")


def main() -> None:
    self_check()
    days = sorted(recent_trading_days(5))
    log("Backtest dni: " + ", ".join(d.isoformat() for d in days))
    capped, raw = collect(days)
    log(f"Signali: capped={len(capped)} uncapped={len(raw)}")
    summary: list[tuple[str, str, dict[str, Any]]] = []
    detailed: dict[tuple[str, str], dict[str, Any]] = {}
    for tp_r in (1.5, 2.0, 3.0):
        for name, signals in (("capped", capped), ("uncapped", raw)):
            result = simulate(signals, tp_r)
            stats = _stats(result)
            stats["signals"] = len(signals)
            label = f"{tp_r:g}"
            summary.append((label, name, stats))
            detailed[(label, name)] = stats
    print_report(summary)
    print_breakdown("TP 2R capped", detailed[("2", "capped")])
    print_breakdown("TP 2R uncapped", detailed[("2", "uncapped")])


if __name__ == "__main__":
    main()
