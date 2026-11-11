#!/usr/bin/env python3
"""Shared helpers for the FundedNext monitor and opportunity scanner."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

LJ_TZ = ZoneInfo("Europe/Ljubljana")

_cffi_session = None
_cffi_available: bool | None = None


class FetchError(Exception):
    """A single market-data request failed; retry on the next loop."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def local_now() -> datetime:
    return datetime.now(LJ_TZ)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"Manjka config.json: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("key_header", "Authorization")
    cfg.setdefault("key_prefix", "Bearer ")
    cfg.setdefault("webhook_url_env", "FN_WEBHOOK_URL")
    cfg.setdefault("webhook_key_env", "FN_WEBHOOK_KEY")
    cfg.setdefault("poll_seconds", 60)
    cfg.setdefault("cooldown_seconds", 600)
    return cfg


def _env_value(cfg: dict[str, Any], cfg_key: str, default_name: str) -> str:
    env_name = (cfg.get(cfg_key) or default_name).strip() or default_name
    return (os.environ.get(env_name) or "").strip()


def webhook_key(cfg: dict[str, Any]) -> str:
    """Key from FN_WEBHOOK_KEY (or webhook_key_env). Never log the return value.

    The value in config.json is ignored so a secret cannot be committed.
    """
    return _env_value(cfg, "webhook_key_env", "FN_WEBHOOK_KEY")


def webhook_url(cfg: dict[str, Any]) -> str:
    """URL from FN_WEBHOOK_URL (or webhook_url_env). Never log the return value.

    The value in config.json is ignored so a secret cannot be committed.
    """
    return _env_value(cfg, "webhook_url_env", "FN_WEBHOOK_URL")


def webhook_is_placeholder(url: str) -> bool:
    if not url:
        return True
    upper = url.upper()
    return "PASTE_" in upper or "EXAMPLE" in upper or "HERE" in upper


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _have_curl_cffi() -> bool:
    global _cffi_available
    if _cffi_available is None:
        try:
            import curl_cffi  # noqa: F401

            _cffi_available = True
        except Exception:
            _cffi_available = False
    return _cffi_available


def _cffi_get_json(url: str, timeout: float) -> Any:
    global _cffi_session
    from curl_cffi import requests as creq

    if _cffi_session is None:
        _cffi_session = creq.Session(impersonate="chrome")
    try:
        resp = _cffi_session.get(
            url,
            timeout=timeout,
            headers={"Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"},
        )
    except Exception as exc:
        raise FetchError(f"omrežna napaka: {exc}") from exc
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code} za {url}: {resp.text[:200]}")
    try:
        return resp.json()
    except Exception as exc:
        raise FetchError(f"JSON ni veljaven za {url}: {exc}") from exc


def _urllib_get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if resp.status != 200:
                raise FetchError(f"HTTP {resp.status} za {url}")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise FetchError(f"HTTP {exc.code} za {url}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise FetchError(f"Napaka pri branju {url}: {exc}") from exc


def http_get_json(url: str, timeout: float = 20.0, retries: int = 3) -> Any:
    """GET JSON. Prefer curl_cffi (Yahoo often returns 429 to plain urllib)."""
    last_err: Exception | None = None
    prefer_cffi = _have_curl_cffi()
    for attempt in range(retries):
        try:
            if prefer_cffi:
                return _cffi_get_json(url, timeout)
            return _urllib_get_json(url, timeout)
        except FetchError as exc:
            last_err = exc
            msg = str(exc)
            # If cffi failed unusually, try urllib once (and vice versa).
            if prefer_cffi and "HTTP 429" not in msg and attempt == 0:
                try:
                    return _urllib_get_json(url, timeout)
                except FetchError as exc2:
                    last_err = exc2
            if "HTTP 429" in msg or "HTTP 5" in msg or "omrežna" in msg:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def apply_offset(candles: list[dict[str, Any]], offset: float) -> list[dict[str, Any]]:
    if not offset:
        return candles
    out = []
    for c in candles:
        d = dict(c)
        for k in ("open", "high", "low", "close"):
            d[k] = c[k] + offset
        out.append(d)
    return out


def _snap_ts(ts: float, interval_seconds: int) -> float:
    return float(int(ts) - (int(ts) % interval_seconds))


def parse_yahoo_chart(payload: Any, interval_seconds: int) -> list[dict[str, Any]]:
    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        err = None
        try:
            err = payload.get("chart", {}).get("error")
        except Exception:
            pass
        raise FetchError(f"Yahoo odgovor ni veljaven: {err or exc}") from exc

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    candles: list[dict[str, Any]] = []
    now_ts = time.time()
    for i, ts in enumerate(timestamps):
        if i >= len(closes):
            break
        c = closes[i]
        h = highs[i] if i < len(highs) else None
        l = lows[i] if i < len(lows) else None
        o = opens[i] if i < len(opens) else None
        if c is None or h is None or l is None:
            continue
        if o is None:
            o = c
        vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
        bar_start = _snap_ts(float(ts), interval_seconds)
        complete = (bar_start + interval_seconds) <= (now_ts + 2)
        candles.append(
            {
                "ts": bar_start,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(vol or 0),
                "complete": complete,
            }
        )
    if not candles:
        raise FetchError("Yahoo ni vrnil nobene veljavne sveče.")
    return candles


def parse_binance_klines(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise FetchError("Binance ni vrnil sveč.")
    now_ms = time.time() * 1000
    candles: list[dict[str, Any]] = []
    for row in payload:
        try:
            open_ms = float(row[0])
            close_ms = float(row[6])
            candles.append(
                {
                    "ts": open_ms / 1000.0,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "complete": close_ms <= (now_ms + 2000),
                }
            )
        except (TypeError, ValueError, IndexError) as exc:
            raise FetchError(f"Binance sveča ni veljavna: {exc}") from exc
    return candles


def yahoo_chart_url(ticker: str, interval: str, range_: str, host: str = "query1") -> str:
    enc = urllib.parse.quote(ticker, safe="=.-")
    return (
        f"https://{host}.finance.yahoo.com/v8/finance/chart/{enc}"
        f"?interval={interval}&range={range_}&includePrePost=true"
    )


def interval_seconds(interval: str) -> int:
    return {"1m": 60, "5m": 300, "15m": 15 * 60, "1h": 3600, "60m": 3600, "1d": 86400}[interval]


def _short_err(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:180]


def fetch_yahoo(ticker: str, interval: str, range_: str) -> list[dict[str, Any]]:
    """Load a Yahoo chart.

    curl_cffi (Chrome impersonation) is tried first. GitHub-hosted runner IPs
    are often blocked or rate-limited, so a failure falls through to plain
    urllib on the same host and then to query2.finance.yahoo.com (again
    curl_cffi, then urllib). The first response that parses wins.
    """
    seconds = interval_seconds(interval)
    last_err: Exception | None = None
    for host in ("query1", "query2"):
        url = yahoo_chart_url(ticker, interval, range_, host)
        attempts: list[tuple[str, Any]] = []
        if _have_curl_cffi():
            attempts.append(("curl_cffi", _cffi_get_json))
        attempts.append(("urllib", _urllib_get_json))
        for name, fetch in attempts:
            try:
                payload = fetch(url, 20.0)
                candles = parse_yahoo_chart(payload, seconds)
            except FetchError as exc:
                last_err = exc
                log(f"Yahoo {ticker} {interval} {host} prek {name} ni uspel: {_short_err(exc)}")
                continue
            if not (host == "query1" and name == attempts[0][0]):
                log(f"Yahoo {ticker} {interval}: uspelo prek {host}/{name}")
            return candles
    assert last_err is not None
    raise last_err


def fetch_binance(
    ticker: str,
    interval: str,
    limit: int = 500,
    end_time_ms: int | None = None,
) -> list[dict[str, Any]]:
    """BTC klines. api.binance.com first, then the public vision mirror.

    GitHub-hosted runners often get HTTP 451 from api.binance.com. The vision
    host serves the same spot klines and is tried only after the primary fails.
    """
    last_err: Exception | None = None
    hosts = ("api.binance.com", "data-api.binance.vision")
    for host in hosts:
        url = f"https://{host}/api/v3/klines?symbol={ticker}&interval={interval}&limit={limit}"
        if end_time_ms is not None:
            url += f"&endTime={int(end_time_ms)}"
        try:
            candles = parse_binance_klines(http_get_json(url))
        except FetchError as exc:
            last_err = exc
            log(f"Binance {ticker} {interval} prek {host} ni uspel: {_short_err(exc)}")
            continue
        if host != hosts[0]:
            log(f"Binance {ticker} {interval}: uspelo prek {host}")
        return candles
    assert last_err is not None
    raise last_err


def fetch_candles(instrument: dict[str, Any], interval: str, range_: str = "5d") -> list[dict[str, Any]]:
    source = (instrument.get("source") or "yahoo").lower()
    ticker = instrument["ticker"]
    offset = float(instrument.get("offset") or 0)
    if source == "binance":
        binv = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "60m": "1h", "1d": "1d"}.get(interval, interval)
        limit = 1000 if interval in ("5m", "15m", "1h", "60m", "1d") else 120
        candles = fetch_binance(ticker, binv, limit=limit)
        # 1000 M5 bars cover ~3.5 days. One older page reaches the 5-day test window.
        if interval == "5m" and candles:
            try:
                older = fetch_binance(ticker, binv, limit=1000, end_time_ms=int(candles[0]["ts"] * 1000) - 1)
            except FetchError:
                older = []
            merged: dict[float, dict[str, Any]] = {}
            for bar in older + candles:
                merged[bar["ts"]] = bar
            candles = [merged[k] for k in sorted(merged)]
    else:
        candles = fetch_yahoo(ticker, interval, range_)
    return apply_offset(candles, offset)


def last_price_snapshot(instrument: dict[str, Any]) -> dict[str, Any]:
    """1m + 15m snapshot used by the order monitor."""
    source = (instrument.get("source") or "yahoo").lower()
    offset = float(instrument.get("offset") or 0)
    if source == "binance":
        c1 = apply_offset(fetch_binance(instrument["ticker"], "1m", limit=20), offset)
        c15 = apply_offset(fetch_binance(instrument["ticker"], "15m", limit=20), offset)
    else:
        c1 = apply_offset(fetch_yahoo(instrument["ticker"], "1m", "1d"), offset)
        c15 = apply_offset(fetch_yahoo(instrument["ticker"], "15m", "5d"), offset)

    last = c1[-1]
    completed_15 = [c for c in c15 if c["complete"]]
    last_closed_15 = completed_15[-1] if completed_15 else None
    recent = c1[-5:] if len(c1) >= 5 else c1
    return {
        "price": last["close"],
        "broker_price": last["close"],
        "raw_price": last["close"] - offset,
        "high": last["high"],
        "low": last["low"],
        "recent_high": max(c["high"] for c in recent),
        "recent_low": min(c["low"] for c in recent),
        "m15_close": last_closed_15["close"] if last_closed_15 else None,
        "m15_ts": last_closed_15["ts"] if last_closed_15 else None,
    }


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def ema_last(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    for v in reversed(series):
        if v is not None:
            return v
    return None


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains = []
    losses = []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    def _rsi(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi(avg_g, avg_l)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi(avg_g, avg_l)
    return out


def rsi_at(values: list[float], period: int = 14) -> float | None:
    series = rsi_series(values, period)
    for v in reversed(series):
        if v is not None:
            return v
    return None


def atr(bars: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def wick_fractions(bar: dict[str, Any]) -> tuple[float, float]:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.0, 0.0
    body_top = max(bar["open"], bar["close"])
    body_bot = min(bar["open"], bar["close"])
    upper = bar["high"] - body_top
    lower = body_bot - bar["low"]
    return upper / rng, lower / rng


def ema_state(closes: list[float], fast: int = 9, slow: int = 20) -> str:
    e9 = ema_last(closes, fast)
    e20 = ema_last(closes, slow)
    if e9 is None or e20 is None:
        return "unknown"
    if e9 > e20:
        return "bullish"
    if e9 < e20:
        return "bearish"
    return "flat"


def session_vwap(bars: list[dict[str, Any]]) -> float | None:
    num = 0.0
    den = 0.0
    used_volume = False
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        vol = b.get("volume") or 0.0
        if vol > 0:
            num += tp * vol
            den += vol
            used_volume = True
        else:
            # Equal-weight fallback when the feed has no volume (typical for FX).
            if not used_volume:
                num += tp
                den += 1.0
    if den <= 0:
        return None
    return num / den


def relative_volume(bars: list[dict[str, Any]], idx: int, lookback: int = 20) -> float | None:
    if idx < 1 or idx >= len(bars):
        return None
    vol = bars[idx].get("volume") or 0.0
    if vol <= 0:
        return None
    tod = datetime.fromtimestamp(bars[idx]["ts"], tz=timezone.utc).astimezone(LJ_TZ).strftime("%H:%M")
    same: list[float] = []
    for i in range(idx):
        v = bars[i].get("volume") or 0.0
        if v <= 0:
            continue
        if datetime.fromtimestamp(bars[i]["ts"], tz=timezone.utc).astimezone(LJ_TZ).strftime("%H:%M") == tod:
            same.append(v)
    if len(same) >= 5:
        window = same[-lookback:]
        avg = sum(window) / len(window)
    else:
        window = [
            bars[i].get("volume") or 0.0
            for i in range(max(0, idx - lookback), idx)
            if (bars[i].get("volume") or 0.0) > 0
        ]
        if len(window) < 5:
            return None
        avg = sum(window) / len(window)
    if avg <= 0:
        return None
    return vol / avg


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------

def day_bounds(day, tz=LJ_TZ) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def hm_on_day(day, hm: str, tz=LJ_TZ) -> datetime:
    hour, minute = (int(x) for x in hm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def in_active_hours(ts: float, start_hm: str = "08:00", end_hm: str = "21:00", weekdays_only: bool = True) -> bool:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LJ_TZ)
    if weekdays_only and dt.weekday() >= 5:
        return False
    start = hm_on_day(dt.date(), start_hm)
    end = hm_on_day(dt.date(), end_hm)
    return start <= dt < end


# ---------------------------------------------------------------------------
# Stop signal (session runner sends SIGTERM at the end of the window)
# ---------------------------------------------------------------------------

_stop_requested = False


def request_stop(signum: int, _frame: Any) -> None:
    global _stop_requested
    if _stop_requested:
        raise KeyboardInterrupt
    _stop_requested = True
    log("Prejet signal za ustavitev, končam po tekočem krogu.")


def install_stop_signals() -> None:
    import signal

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def stop_requested() -> bool:
    return _stop_requested


def sleep_secs(seconds: float) -> None:
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if _stop_requested:
            return
        time.sleep(min(1.0, end - time.time()))


# ---------------------------------------------------------------------------
# Webhook (never logs the key or the URL)
# ---------------------------------------------------------------------------

_warned_missing_secrets = False
SKIPPED_MISSING_SECRETS = "skipped-missing-secrets"


def _redact(text: str, *secrets: str) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def send_webhook(cfg: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
    global _warned_missing_secrets
    url = webhook_url(cfg)
    key = webhook_key(cfg)
    if webhook_is_placeholder(url) or not key:
        if not _warned_missing_secrets:
            log(
                "Opozorilo: FN_WEBHOOK_URL ali FN_WEBHOOK_KEY manjka. "
                "Pošiljanje je preskočeno."
            )
            _warned_missing_secrets = True
        return False, SKIPPED_MISSING_SECRETS

    body = json.dumps(payload).encode("utf-8")
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")[:200]
            return True, _redact(f"HTTP {resp.status} {raw}", url, key)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return False, _redact(f"HTTP {exc.code} {raw}", url, key)
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, _redact(f"omrežna napaka: {exc}", url, key)


def fmt_price(value: float | None, instrument: dict[str, Any] | None = None) -> str:
    if value is None:
        return "-"
    symbol = (instrument or {}).get("symbol", "")
    if any(x in symbol for x in ("BTC", "NAS", "US30", "GER", "NDX", "DAX")):
        return f"{value:.1f}"
    ticker = (instrument or {}).get("ticker") or ""
    if any(x in symbol for x in ("XAU", "GOLD")) or ticker == "GC=F":
        return f"{value:.2f}"
    # WTI is quoted in cents. Keep two decimals even if the price is under 50.
    if symbol in {"USOUSD", "CL"} or ticker == "CL=F" or "WTI" in symbol:
        return f"{value:.2f}"
    if value is not None and abs(value) >= 50:
        return f"{value:.2f}"
    return f"{value:.5f}"
