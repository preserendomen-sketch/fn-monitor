#!/usr/bin/env python3
"""MTF agreement and session-open windows.

These tests never call the live webhook. send_webhook is replaced before any
scanner path that could send, and the webhook environment variables are cleared.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta

import scanner
from fnlib import LJ_TZ
from scanner import (
    backfill,
    day_key,
    dispatch_candidates,
    evaluate_bar,
    evaluate_early_breakout,
    in_session_open_window,
    iter_early_decisions,
    m1_confirmation,
    m5_confirmation,
    scanner_cfg,
    scanner_sleep_seconds,
)

PDH = 100.0


def _refuse_webhook(*_args, **_kwargs):
    raise AssertionError("research/tests must not fire the webhook")


def _at(day, hour: int, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LJ_TZ).timestamp()


def _bar(ts: float, open_: float, high: float, low: float, close: float, volume: float = 100.0) -> dict:
    return {
        "ts": float(ts),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "complete": True,
    }


def _previous_weekday(day):
    prev = day - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def _inst(symbol: str = "NAS100") -> dict:
    ticker = "NQ=F" if symbol == "NAS100" else f"{symbol}=X"
    return {"id": symbol, "symbol": symbol, "source": "yahoo", "ticker": ticker, "offset": 0}


def _sc(inst: dict) -> dict:
    return scanner_cfg(
        {
            "scanner": {
                "universe": [inst],
                "rvol_min": 1.5,
                "weekdays_only": True,
                "active_start": "08:00",
                "active_end": "21:00",
                "wick_frac": 0.5,
                "atr_buffer_mult": 0.15,
                "cooldown_minutes": 90,
                "max_alerts_per_day": 8,
                "max_alerts_per_symbol_per_day": 2,
            }
        }
    )


class Market:
    """One bullish breakout through yesterday's high, with M1/M5 around it."""

    def __init__(
        self,
        day,
        *,
        hour: int = 9,
        minute: int = 0,
        symbol: str = "NAS100",
        break_close: float = 101.0,
        m15_volume: float = 200.0,
        pre_high: float = 99.8,
        lead_m5_volume: float = 200.0,
        last_m1: str = "agree",
    ):
        self.day = day
        self.inst = _inst(symbol)
        self.sc = _sc(self.inst)
        self.signal = _at(day, hour, minute)
        self.break_close = break_close
        self.m15: list[dict] = []
        self.h1: list[dict] = []
        self.m5: list[dict] = []
        self.m1: list[dict] = []
        self._build(m15_volume, lead_m5_volume, last_m1, pre_high)

    def _build(self, m15_volume: float, lead_m5_volume: float, last_m1: str, pre_high: float) -> None:
        prev = _previous_weekday(self.day)
        # Yesterday establishes PDH at 100. Today's pre-signal bars stay under it.
        ts = _at(prev, 8, 0)
        end = _at(prev, 20, 0)
        while ts < end:
            high, close = (PDH, 99.0) if ts == _at(prev, 12, 0) else (99.5, 99.0)
            self.m15.append(_bar(ts, 98.8, high, 98.4, close, 100))
            ts += 15 * 60
        ts = _at(self.day, 0, 0)
        while ts < self.signal:
            self.m15.append(_bar(ts, 99.4, pre_high, 99.2, 99.6, 100))
            ts += 15 * 60
        m15_high = self.break_close + 0.05
        self.m15.append(_bar(self.signal, 99.7, m15_high, 99.5, self.break_close, m15_volume))

        # Steady H1 uptrend. True range is 1.0 so the ATR cushion is 0.15 on FX.
        price = 80.0
        ts = self.signal - 48 * 3600
        for _ in range(60):
            close = price
            self.h1.append(_bar(ts, close - 0.2, close + 0.4, close - 0.6, close, 100))
            price += 0.2
            ts += 3600

        # M5 history, then the three M5 bars inside the signal M15.
        ts = self.signal - 40 * 300
        step = 0.05
        price = 97.0
        while ts < self.signal:
            close = price
            self.m5.append(_bar(ts, close - 0.04, close + 0.02, close - 0.06, close, 100))
            price += step
            ts += 300
        lead_close = self.break_close
        self.m5.append(_bar(self.signal, 99.7, lead_close + 0.05, 99.6, lead_close, lead_m5_volume))
        self.m5.append(_bar(self.signal + 300, lead_close - 0.3, lead_close - 0.1, lead_close - 0.4, lead_close - 0.15, 100))
        self.m5.append(
            _bar(
                self.signal + 600,
                self.break_close - 0.3,
                m15_high,
                self.break_close - 0.4,
                self.break_close,
                100,
            )
        )

        self.m1.extend(self._climb(self.signal, lead_close, "agree"))
        self.m1.extend(self._climb(self.signal + 300, lead_close - 0.15, "agree"))
        self.m1.extend(self._climb(self.signal + 600, self.break_close, last_m1))

    def _climb(self, start: float, final: float, mode: str) -> list[dict]:
        if mode == "single":
            return [_bar(start + 4 * 60, final - 0.1, final + 0.02, final - 0.12, final, 10)]
        bars = []
        for i in range(5):
            close = 99.7 + (final - 99.7) * ((i + 1) / 5.0)
            open_ = 99.7 + (final - 99.7) * (i / 5.0)
            if i == 4 and mode == "spike":
                open_ = final
                close = final - 0.15
                bars.append(_bar(start + i * 60, open_, final + 2.0, close - 0.05, close, 10))
                continue
            if i == 4 and mode == "red":
                bars.append(_bar(start + i * 60, close + 0.2, close + 0.25, close - 0.05, close, 10))
                continue
            high = max(open_, close) + 0.02
            low = min(open_, close) - 0.02
            bars.append(_bar(start + i * 60, open_, high, low, close, 10))
        return bars


class MtfTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ("FN_WEBHOOK_URL", "FN_WEBHOOK_KEY")}
        os.environ.pop("FN_WEBHOOK_URL", None)
        os.environ.pop("FN_WEBHOOK_KEY", None)
        self._webhook = scanner.send_webhook
        self._fetch_orig = scanner.fetch_instrument
        self._news_orig = scanner.load_news_calendar
        scanner.send_webhook = _refuse_webhook

    def tearDown(self) -> None:
        scanner.send_webhook = self._webhook
        scanner.fetch_instrument = self._fetch_orig
        scanner.load_news_calendar = self._news_orig
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_windows_and_sleep(self) -> None:
        # Thursday 12 Nov 2026 is winter time (CET, UTC+1). 09:00 Ljubljana is
        # the London open; 15:30 Ljubljana is the usual NYSE cash open.
        day = datetime(2026, 11, 12).date()
        sc = _sc(_inst())
        self.assertEqual(in_session_open_window(_at(day, 8, 44), sc), None)
        self.assertEqual(in_session_open_window(_at(day, 8, 45), sc), "london")
        self.assertEqual(in_session_open_window(_at(day, 9, 0), sc), "london")
        self.assertEqual(in_session_open_window(_at(day, 9, 59), sc), "london")
        self.assertEqual(in_session_open_window(_at(day, 10, 0), sc), None)
        self.assertEqual(in_session_open_window(_at(day, 12, 0), sc), None)
        self.assertEqual(in_session_open_window(_at(day, 15, 14), sc), None)
        self.assertEqual(in_session_open_window(_at(day, 15, 15), sc), "us_cash")
        self.assertEqual(in_session_open_window(_at(day, 15, 30), sc), "us_cash")
        self.assertEqual(in_session_open_window(_at(day, 16, 29), sc), "us_cash")
        self.assertEqual(in_session_open_window(_at(day, 16, 30), sc), None)
        # The clock window is not a weekday check. Saturday morning is inside
        # the London window and still cannot alert (in_active_hours).
        saturday = day + timedelta(days=2)
        self.assertEqual(saturday.weekday(), 5)
        self.assertEqual(in_session_open_window(_at(saturday, 9, 0), sc), "london")

        self.assertAlmostEqual(scanner_sleep_seconds(_at(day, 8, 42), sc, 0), 180, delta=1)
        self.assertAlmostEqual(scanner_sleep_seconds(_at(day, 8, 44) + 50, sc, 0), 10, delta=1)
        self.assertAlmostEqual(scanner_sleep_seconds(_at(day, 9, 0), sc, 10), 50, delta=1)
        self.assertAlmostEqual(scanner_sleep_seconds(_at(day, 9, 0), sc, 0), 60, delta=1)
        self.assertAlmostEqual(scanner_sleep_seconds(_at(day, 12, 0), sc, 0), 300, delta=1)
        self.assertAlmostEqual(scanner_sleep_seconds(_at(day, 15, 12), sc, 0), 180, delta=1)

    def test_when_m1_and_m5_are_evaluated(self) -> None:
        day = datetime(2026, 11, 12).date()
        sc = _sc(_inst())
        m1 = []
        m5 = []
        ts = _at(day, 8, 30)
        while ts <= _at(day, 10, 5):
            m1.append(_bar(ts, 1, 1, 1, 1))
            ts += 60
        ts = _at(day, 8, 30)
        while ts <= _at(day, 10, 0):
            m5.append(_bar(ts, 1, 1, 1, 1))
            ts += 300
        # US cash window, so a later M1 is a decision too.
        m1.append(_bar(_at(day, 15, 19), 1, 1, 1, 1))
        now = _at(day, 16, 0)
        decisions, c5, c1 = iter_early_decisions(m5, m1, -1, -1, now, sc)
        labeled = {(int(ts), tf) for ts, tf in decisions}

        def close(hour: int, minute: int) -> int:
            return int(_at(day, hour, minute))

        # 08:44 is before the window. 08:45 is inside it, but it is also an
        # M15 close, so the early path leaves it to evaluate_bar. The next
        # M1, 08:46, is the first early decision.
        self.assertNotIn((close(8, 44), "M1"), labeled)
        self.assertNotIn((close(8, 45), "M1"), labeled)
        self.assertIn((close(8, 46), "M1"), labeled)
        # An M5 that closes inside the window is its own decision.
        self.assertIn((close(8, 50), "M5"), labeled)
        self.assertIn((close(9, 5), "M5"), labeled)
        # 09:15 is also an M15 close, so the early path leaves it to evaluate_bar.
        self.assertNotIn((close(9, 15), "M5"), labeled)
        self.assertNotIn((close(9, 15), "M1"), labeled)
        # 10:00 is the exclusive end.
        self.assertNotIn((close(10, 0), "M1"), labeled)
        self.assertIn((close(9, 59), "M1"), labeled)
        self.assertNotIn((close(12, 0), "M1"), labeled)
        self.assertIn((close(15, 20), "M1"), labeled)
        # Cursors move past bars that were not decisions, so the next loop
        # does not replay them.
        self.assertGreater(c5, 0)
        self.assertGreater(c1, 0)
        again, _, _ = iter_early_decisions(m5, m1, c5, c1, now, sc)
        self.assertEqual(again, [])

        # Weekend clock-window bars are not decisions: alerts stay on weekdays.
        sat = day + timedelta(days=2)
        sat_m1 = [_bar(_at(sat, 9, 0), 1, 1, 1, 1), _bar(_at(sat, 9, 1), 1, 1, 1, 1)]
        sat_dec, _, _ = iter_early_decisions(None, sat_m1, -1, -1, _at(sat, 9, 30), sc)
        self.assertEqual(sat_dec, [])

    def test_m1_timing_rules(self) -> None:
        start = 1_700_000_000.0
        agree = []
        for i in range(5):
            open_ = 100 + i * 0.1
            close = open_ + 0.08
            agree.append(_bar(start + i * 60, open_, close + 0.01, open_ - 0.01, close))
        ok = m1_confirmation("buy", agree, start, start + 300, 0.5, level=100)
        self.assertTrue(ok["m1_confirm"])

        one = m1_confirmation("buy", agree[:1], start, start + 300, 0.5, level=100)
        self.assertFalse(one["m1_confirm"])

        missing = m1_confirmation("buy", None, start, start + 300, 0.5, level=100)
        self.assertIsNone(missing["m1_confirm"])

        spike = list(agree)
        last = spike[-1]
        spike[-1] = _bar(last["ts"], last["open"], last["open"] + 3, last["open"] - 0.05, last["open"] - 0.1)
        self.assertFalse(m1_confirmation("buy", spike, start, start + 300, 0.5, level=100)["m1_confirm"])

        red = [_bar(start + i * 60, 101, 101.1, 100.4, 100.5) for i in range(4)]
        self.assertFalse(m1_confirmation("buy", red, start, start + 300, 0.5, level=100)["m1_confirm"])

        under = list(agree)
        under[-1] = _bar(under[-1]["ts"], 99.4, 99.7, 99.3, 99.6)
        self.assertFalse(m1_confirmation("buy", under, start, start + 300, 0.5, level=100)["m1_confirm"])

    def test_m5_structure_rules(self) -> None:
        start = _at(datetime(2026, 11, 12).date(), 9, 0)
        m15 = _bar(start, 99.5, 101.2, 99.4, 101.0)
        m5 = []
        price = 97.0
        ts = start - 40 * 300
        while ts < start + 600:
            close = price
            m5.append(_bar(ts, close - 0.05, close + 0.02, close - 0.08, close))
            price += 0.08
            ts += 300
        m5.append(_bar(start + 600, 100.5, 101.15, 100.4, 101.0))
        trend = m5_confirmation(m15, "buy", m5, level=PDH, kind="breakout")
        self.assertTrue(trend["m5_confirm"])

        short = list(m5)
        short[-1] = _bar(start + 600, 100.8, 100.9, 99.0, 99.2)
        self.assertFalse(m5_confirmation(m15, "buy", short, level=PDH, kind="breakout")["m5_confirm"])
        self.assertIsNone(m5_confirmation(m15, "buy", None, level=PDH)["m5_confirm"])
        self.assertFalse(m5_confirmation(m15, "buy", [], level=PDH)["m5_confirm"])

        # Exhaustion may keep a bullish M5 EMA. The candle has to reject.
        fade = list(m5)
        fade[-1] = _bar(start + 600, 101.0, 102.5, 99.9, 100.0)
        self.assertTrue(
            m5_confirmation(m15, "sell", fade, level=101.0, kind="exhaustion", wick_frac=0.5)["m5_confirm"]
        )
        held = list(m5)
        held[-1] = _bar(start + 600, 100.6, 101.1, 100.5, 101.0)
        self.assertFalse(
            m5_confirmation(m15, "sell", held, level=101.0, kind="exhaustion", wick_frac=0.5)["m5_confirm"]
        )

    def _market(self, **kwargs) -> Market:
        day = max(scanner.recent_trading_days(1))
        return Market(day, **kwargs)

    def test_breakout_needs_all_three_timeframes(self) -> None:
        market = self._market()
        idx = len(market.m15) - 1
        news = ([], True)
        agreed = evaluate_bar(
            market.inst, market.m15, idx, market.h1, market.sc,
            m5=market.m5, news=news, m1=market.m1,
        )
        self.assertEqual([c["type"] for c in agreed], ["breakout"])
        self.assertEqual(agreed[0]["quality"], "A+")
        self.assertEqual(agreed[0]["mtf"], "M1+M5+M15")
        self.assertEqual(agreed[0]["trigger_tf"], "M15")
        self.assertTrue(agreed[0]["m5_confirm"])
        self.assertTrue(agreed[0]["m1_confirm"])
        self.assertEqual(agreed[0]["direction"], "buy")

        self.assertEqual(
            evaluate_bar(
                market.inst, market.m15, idx, market.h1, market.sc,
                m5=market.m5, news=news, m1=None,
            ),
            [],
        )
        self.assertEqual(
            evaluate_bar(
                market.inst, market.m15, idx, market.h1, market.sc,
                m5=None, news=news, m1=market.m1,
            ),
            [],
        )
        spiked = self._market(last_m1="spike")
        self.assertEqual(
            evaluate_bar(
                spiked.inst, spiked.m15, len(spiked.m15) - 1, spiked.h1, spiked.sc,
                m5=spiked.m5, news=news, m1=spiked.m1,
            ),
            [],
        )
        single = self._market(last_m1="single")
        self.assertEqual(
            evaluate_bar(
                single.inst, single.m15, len(single.m15) - 1, single.h1, single.sc,
                m5=single.m5, news=news, m1=single.m1,
            ),
            [],
        )

    def test_m15_breakout_rvol_stays_at_1_5(self) -> None:
        news = ([], True)
        weak = self._market(m15_volume=140)  # 140/100 = 1.4, under the standard
        self.assertEqual(
            evaluate_bar(
                weak.inst, weak.m15, len(weak.m15) - 1, weak.h1, weak.sc,
                m5=weak.m5, news=news, m1=weak.m1,
            ),
            [],
        )
        strong = self._market(m15_volume=200)
        got = evaluate_bar(
            strong.inst, strong.m15, len(strong.m15) - 1, strong.h1, strong.sc,
            m5=strong.m5, news=news, m1=strong.m1,
        )
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(got[0]["rvol"], 1.5)

    def test_fx_buffer_is_not_loosened(self) -> None:
        news = ([], True)
        # EURUSD uses the 0.15× H1 ATR cushion. The fixture's H1 true range is 1,
        # so the close must clear PDH by 0.15. 100.10 does not; 100.20 does.
        # Pre-signal highs sit above the test closes, so Asian high is not the
        # level being broken. Only PDH (100) is in play, and it needs the cushion.
        shy = self._market(symbol="EURUSD", break_close=100.10, m15_volume=200, pre_high=101.0)
        self.assertEqual(
            evaluate_bar(
                shy.inst, shy.m15, len(shy.m15) - 1, shy.h1, shy.sc,
                m5=shy.m5, news=news, m1=shy.m1,
            ),
            [],
        )
        clear = self._market(symbol="EURUSD", break_close=100.20, m15_volume=200, pre_high=101.0)
        got = evaluate_bar(
            clear.inst, clear.m15, len(clear.m15) - 1, clear.h1, clear.sc,
            m5=clear.m5, news=news, m1=clear.m1,
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["quality"], "A+")
        self.assertEqual(got[0]["level"], PDH)
        # The same cushion applies before the M15 bar closes.
        self.assertEqual(
            evaluate_early_breakout(
                shy.inst, shy.m15, shy.m5, shy.m1, shy.h1, shy.sc,
                shy.signal + 300, news=news,
            ),
            [],
        )
        early = evaluate_early_breakout(
            clear.inst, clear.m15, clear.m5, clear.m1, clear.h1, clear.sc,
            clear.signal + 300, news=news,
        )
        self.assertEqual(len(early), 1)
        self.assertEqual(early[0]["level"], PDH)
        self.assertGreaterEqual(early[0]["rvol"], 1.5)

    def test_early_breakout_only_inside_the_window_and_not_from_m1_alone(self) -> None:
        news = ([], True)
        market = self._market(last_m1="spike")
        early = evaluate_early_breakout(
            market.inst, market.m15, market.m5, market.m1, market.h1, market.sc,
            market.signal + 300, trigger_tf="M5", news=news,
        )
        self.assertEqual(len(early), 1)
        self.assertEqual(early[0]["quality"], "A+")
        self.assertEqual(early[0]["trigger_tf"], "M5")
        self.assertEqual(early[0]["session_open"], "london")
        self.assertIn("M5 RVOL", early[0]["note"])
        # The completed M15 is the same market, but its last M1 is a spike.
        self.assertEqual(
            evaluate_bar(
                market.inst, market.m15, len(market.m15) - 1, market.h1, market.sc,
                m5=market.m5, news=news, m1=market.m1,
            ),
            [],
        )
        # Noon has the same microstructure and is outside both windows.
        noon = self._market(hour=12, minute=0)
        self.assertEqual(
            evaluate_early_breakout(
                noon.inst, noon.m15, noon.m5, noon.m1, noon.h1, noon.sc,
                noon.signal + 300, news=news,
            ),
            [],
        )
        # M5 RVOL 1.2 does not sneak through just because the M15 bar is still open.
        thin = self._market(lead_m5_volume=120)
        self.assertEqual(
            evaluate_early_breakout(
                thin.inst, thin.m15, thin.m5, thin.m1, thin.h1, thin.sc,
                thin.signal + 300, news=news,
            ),
            [],
        )
        # A Saturday open is inside the clock window and outside active hours.
        sat = market.day + timedelta(days=(5 - market.day.weekday()) % 7)
        if sat == market.day:
            sat = market.day + timedelta(days=7)
        while sat.weekday() != 5:
            sat += timedelta(days=1)
        weekend = Market(sat)
        self.assertEqual(
            evaluate_early_breakout(
                weekend.inst, weekend.m15, weekend.m5, weekend.m1, weekend.h1, weekend.sc,
                weekend.signal + 300, news=news,
            ),
            [],
        )

    def test_retest_is_strong_b_only_with_mtf(self) -> None:
        market = self._market()
        bar = market.m15[-1]
        cand = {
            "symbol": market.inst["symbol"],
            "type": "retest",
            "direction": "buy",
            "level": PDH,
            "note": "Retest test.",
        }
        kept = scanner._gate_candidates(
            market.inst, bar, [dict(cand)], market.m5, market.sc, ([], True), m1=market.m1
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["quality"], "B+")
        self.assertTrue(kept[0]["m1_confirm"] and kept[0]["m5_confirm"])
        self.assertEqual(
            scanner._gate_candidates(
                market.inst, bar, [dict(cand)], market.m5, market.sc, ([], True), m1=None
            ),
            [],
        )
        self.assertEqual(
            scanner._gate_candidates(
                market.inst, bar, [dict(cand)], None, market.sc, ([], True), m1=market.m1
            ),
            [],
        )

    def test_dispatch_respects_caps_and_does_not_send_when_disabled(self) -> None:
        market = self._market()
        now = market.signal + 300
        cand = {
            "symbol": "NAS100",
            "type": "breakout",
            "direction": "buy",
            "close_ts": now,
            "bar_ts": market.signal,
            "quality": "A+",
            "trigger_tf": "M5",
            "note": "test",
        }
        state = {"day": day_key(now), "sent_today": 0, "sent_today_by_symbol": {}, "last": {}}
        fired = dispatch_candidates(
            {}, market.sc, state, market.inst, [cand], send=False, now=now
        )
        self.assertEqual(len(fired), 1)
        self.assertEqual(state["sent_today"], 1)
        again = dispatch_candidates(
            {}, market.sc, state, market.inst, [dict(cand, bar_ts=now)], send=False, now=now + 60
        )
        self.assertEqual(again, [])

        sent = []

        def _record(cfg, payload):
            sent.append(payload)
            return True, "ok"

        scanner.send_webhook = _record
        state2 = {"day": day_key(now), "sent_today": 0, "sent_today_by_symbol": {}, "last": {}}
        dispatch_candidates({}, market.sc, state2, market.inst, [dict(cand)], send=True, now=now)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("bar_ts", sent[0])
        self.assertNotIn("close_ts", sent[0])
        self.assertEqual(sent[0]["quality"], "A+")
        stale = dict(cand, close_ts=now - 46 * 60, bar_ts=now - 46 * 60, type="retest")
        state3 = {"day": day_key(now), "sent_today": 0, "sent_today_by_symbol": {}, "last": {}}
        self.assertEqual(
            dispatch_candidates({}, market.sc, state3, market.inst, [stale], send=True, now=now),
            [],
        )
        self.assertEqual(len(sent), 1)

    def test_backfill_replays_early_breakout_without_webhook(self) -> None:
        market = self._market(last_m1="spike")

        def _fetch(inst, m1_range="1d"):
            self.assertEqual(m1_range, "5d")
            return market.m15, market.h1, market.m5, market.m1, None

        scanner.fetch_instrument = _fetch
        scanner.load_news_calendar = lambda sc: ([], True)
        try:
            flagged = backfill({}, market.sc, days=1)
        finally:
            scanner.fetch_instrument = self._fetch_orig
            scanner.load_news_calendar = self._news_orig
        alerts = [c for c in flagged if not c.get("suppressed")]
        self.assertTrue(alerts)
        self.assertTrue(all(c["type"] == "breakout" and c["quality"] == "A+" for c in alerts))
        self.assertTrue(all(c["trigger_tf"] in {"M1", "M5"} for c in alerts))
        self.assertTrue(all(c.get("m1_confirm") and c.get("m5_confirm") for c in alerts))


if __name__ == "__main__":
    unittest.main()
