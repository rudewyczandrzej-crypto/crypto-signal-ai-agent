import os
import re
import json
import html
import time
import math
import logging
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import feedparser
from dotenv import load_dotenv
from supabase import create_client, Client
from groq import Groq

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# =========================
# CONFIG
# =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip()

BINANCE_FUTURES_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").strip().rstrip("/")

SIGNAL_SCAN_INTERVAL_SECONDS = int(os.getenv("SIGNAL_SCAN_INTERVAL_SECONDS", "900"))
SIGNAL_MANAGE_INTERVAL_SECONDS = int(os.getenv("SIGNAL_MANAGE_INTERVAL_SECONDS", "300"))

MIN_AI_SIGNAL_SCORE = int(os.getenv("MIN_AI_SIGNAL_SCORE", "7"))
MAX_CANDIDATES_PER_SCAN = int(os.getenv("MAX_CANDIDATES_PER_SCAN", "20"))
MAX_SIGNALS_PER_SCAN = int(os.getenv("MAX_SIGNALS_PER_SCAN", "3"))

SIGNAL_COOLDOWN_HOURS = int(os.getenv("SIGNAL_COOLDOWN_HOURS", "6"))
MAX_OPEN_SIGNALS = int(os.getenv("MAX_OPEN_SIGNALS", "5"))
MAX_OPEN_SIGNALS_PER_SYMBOL = int(os.getenv("MAX_OPEN_SIGNALS_PER_SYMBOL", "1"))

MAX_LEVERAGE_CAP = int(os.getenv("MAX_LEVERAGE_CAP", "5"))
DEFAULT_MAX_HOLD_MINUTES = int(os.getenv("DEFAULT_MAX_HOLD_MINUTES", "240"))

MIN_24H_QUOTE_VOLUME = float(os.getenv("MIN_24H_QUOTE_VOLUME", "50000000"))
MIN_VOLUME_SPIKE = float(os.getenv("MIN_VOLUME_SPIKE", "1.25"))

AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.15"))

DISCOVERY_NEWS_ENABLED = os.getenv("DISCOVERY_NEWS_ENABLED", "true").lower() in ["1", "true", "yes", "y"]

NEWS_FEEDS = [
    feed.strip()
    for feed in os.getenv(
        "NEWS_FEEDS",
        "https://cointelegraph.com/rss,"
        "https://www.coindesk.com/arc/outboundfeeds/rss/,"
        "https://decrypt.co/feed"
    ).split(",")
    if feed.strip()
]

HEALTH_ALERT_COOLDOWN_SECONDS = int(os.getenv("HEALTH_ALERT_COOLDOWN_SECONDS", "1800"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("crypto-signal-ai")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("Missing GROQ_API_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

http = requests.Session()
http.headers.update({
    "User-Agent": "CryptoSignalAI/1.0 paper trading research bot",
    "Accept": "application/json,text/plain,*/*",
})

LAST_HEALTH_ALERT_TS = 0.0


# =========================
# HELPERS
# =========================

def escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def pct(value: Optional[float]) -> str:
    if value is None:
        return "н/д"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def money(value: Optional[float]) -> str:
    if value is None:
        return "н/д"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}"


def safe_json_loads(text: str) -> Dict[str, Any]:
    text = (text or "").strip()

    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def normalize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (symbol or "").upper())


def clamp(x: float, low: float, high: float) -> float:
    return max(low, min(high, x))


# =========================
# DATABASE
# =========================

def ensure_user(telegram_id: str) -> None:
    existing = supabase.table("users").select("id").eq("telegram_id", telegram_id).execute()

    if existing.data:
        return

    supabase.table("users").insert({
        "telegram_id": telegram_id,
        "signals_enabled": True,
    }).execute()


def get_all_users() -> List[Dict[str, Any]]:
    result = supabase.table("users").select("*").execute()
    return result.data or []


def set_signals_enabled(telegram_id: str, enabled: bool) -> None:
    ensure_user(telegram_id)
    supabase.table("users").update({"signals_enabled": enabled}).eq("telegram_id", telegram_id).execute()


def get_signals_enabled_users() -> List[Dict[str, Any]]:
    result = supabase.table("users").select("*").eq("signals_enabled", True).execute()
    return result.data or []


def count_open_signals(telegram_id: str) -> int:
    result = (
        supabase.table("signals")
        .select("id")
        .eq("telegram_id", telegram_id)
        .eq("status", "open")
        .execute()
    )
    return len(result.data or [])


def has_open_signal_for_symbol(telegram_id: str, symbol: str) -> bool:
    result = (
        supabase.table("signals")
        .select("id")
        .eq("telegram_id", telegram_id)
        .eq("symbol", symbol)
        .eq("status", "open")
        .limit(1)
        .execute()
    )
    return bool(result.data)


def recently_sent_signal(telegram_id: str, symbol: str, side: str) -> bool:
    cutoff = iso(now_utc() - timedelta(hours=SIGNAL_COOLDOWN_HOURS))

    result = (
        supabase.table("signals")
        .select("id")
        .eq("telegram_id", telegram_id)
        .eq("symbol", symbol)
        .eq("side", side)
        .gte("created_at", cutoff)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def save_signal(telegram_id: str, signal: Dict[str, Any]) -> Dict[str, Any]:
    expires_at = now_utc() + timedelta(minutes=int(signal["max_hold_minutes"]))

    result = supabase.table("signals").insert({
        "telegram_id": telegram_id,
        "symbol": signal["symbol"],
        "side": signal["side"],
        "entry_low": signal["entry_low"],
        "entry_high": signal["entry_high"],
        "stop_loss": signal["stop_loss"],
        "tp1": signal["tp1"],
        "tp2": signal["tp2"],
        "tp3": signal["tp3"],
        "leverage": signal["leverage"],
        "max_hold_minutes": signal["max_hold_minutes"],
        "confidence": signal["confidence"],
        "risk_score": signal["risk_score"],
        "status": "open",
        "expires_at": iso(expires_at),
        "raw": signal,
    }).execute()

    return result.data[0]


def get_open_signals(telegram_id: Optional[str] = None) -> List[Dict[str, Any]]:
    query = supabase.table("signals").select("*").eq("status", "open")

    if telegram_id:
        query = query.eq("telegram_id", telegram_id)

    result = query.order("created_at", desc=False).execute()
    return result.data or []


def update_signal_status(signal_id: int, status: str, exit_price: Optional[float], note: str) -> None:
    supabase.table("signals").update({
        "status": status,
        "exit_price": exit_price,
        "closed_at": iso(now_utc()),
        "close_note": note,
    }).eq("id", signal_id).execute()


# =========================
# BINANCE API
# =========================

class MarketFetchError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def binance_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{BINANCE_FUTURES_BASE_URL}{path}"

    try:
        response = http.get(url, params=params or {}, timeout=30)
    except Exception as e:
        raise MarketFetchError("network_error", str(e))

    if response.status_code in [401, 403, 418, 429]:
        raise MarketFetchError("binance_limited", f"Binance HTTP {response.status_code}: {response.text[:300]}")

    if response.status_code >= 500:
        raise MarketFetchError("binance_server_error", f"Binance server error {response.status_code}: {response.text[:300]}")

    if response.status_code >= 400:
        raise MarketFetchError("binance_http_error", f"Binance HTTP {response.status_code}: {response.text[:300]}")

    try:
        return response.json()
    except Exception:
        raise MarketFetchError("bad_json", f"Binance returned non-JSON: {response.text[:300]}")


_SYMBOL_CACHE: Optional[set] = None
_SYMBOL_CACHE_TS = 0.0


def get_usdt_perp_symbols() -> set:
    global _SYMBOL_CACHE, _SYMBOL_CACHE_TS

    now = time.time()
    if _SYMBOL_CACHE and now - _SYMBOL_CACHE_TS < 3600:
        return _SYMBOL_CACHE

    data = binance_get("/fapi/v1/exchangeInfo")
    symbols = set()

    for s in data.get("symbols", []):
        if (
            s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        ):
            symbols.add(s.get("symbol"))

    _SYMBOL_CACHE = symbols
    _SYMBOL_CACHE_TS = now
    return symbols


def fetch_24h_tickers() -> List[Dict[str, Any]]:
    data = binance_get("/fapi/v1/ticker/24hr")
    if not isinstance(data, list):
        raise MarketFetchError("api_changed", "Binance ticker returned unexpected format")
    return data


def fetch_klines(symbol: str, interval: str, limit: int = 150) -> List[List[Any]]:
    data = binance_get("/fapi/v1/klines", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    })

    if not isinstance(data, list):
        raise MarketFetchError("api_changed", "Binance klines returned unexpected format")

    return data


def fetch_current_price(symbol: str) -> Optional[float]:
    data = binance_get("/fapi/v1/ticker/price", {"symbol": symbol})
    return safe_float(data.get("price")) if isinstance(data, dict) else None


# =========================
# INDICATORS
# =========================

def kline_to_candles(klines: List[List[Any]]) -> List[Dict[str, float]]:
    candles = []
    for row in klines:
        candles.append({
            "open_time": float(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "close_time": float(row[6]),
        })
    return candles


def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) <= period:
        return None

    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period


def avg(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def analyze_symbol(symbol: str, ticker: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kl_15m = fetch_klines(symbol, "15m", 120)
    kl_5m = fetch_klines(symbol, "5m", 120)

    candles15 = kline_to_candles(kl_15m)
    candles5 = kline_to_candles(kl_5m)

    if len(candles15) < 60 or len(candles5) < 60:
        return None

    closes15 = [c["close"] for c in candles15]
    volumes15 = [c["volume"] for c in candles15]

    e20 = ema(closes15, 20)[-1]
    e50 = ema(closes15, 50)[-1]
    r = rsi(closes15, 14)
    a = atr(candles15, 14)

    if r is None or a is None:
        return None

    last = candles15[-1]
    close = last["close"]
    volume = last["volume"]
    avg_vol20 = avg(volumes15[-21:-1]) or volume
    volume_spike = volume / avg_vol20 if avg_vol20 else 1.0

    recent_high = max(c["high"] for c in candles15[-25:-1])
    recent_low = min(c["low"] for c in candles15[-25:-1])

    atr_pct = (a / close) * 100 if close else 0

    price_change_24h = safe_float(ticker.get("priceChangePercent"))
    quote_volume = safe_float(ticker.get("quoteVolume"))

    trend = "neutral"
    if close > e20 > e50:
        trend = "up"
    elif close < e20 < e50:
        trend = "down"

    # Setup scoring before AI. AI only validates; code calculates levels.
    long_breakout = trend == "up" and close >= recent_high * 0.997 and volume_spike >= MIN_VOLUME_SPIKE and 48 <= r <= 76
    short_breakdown = trend == "down" and close <= recent_low * 1.003 and volume_spike >= MIN_VOLUME_SPIKE and 24 <= r <= 52

    # Pullback continuation setups.
    long_pullback = trend == "up" and e20 * 0.995 <= close <= e20 * 1.012 and 43 <= r <= 62 and volume_spike >= 0.85
    short_pullback = trend == "down" and e20 * 0.988 <= close <= e20 * 1.005 and 38 <= r <= 58 and volume_spike >= 0.85

    side = None
    setup_type = None

    if long_breakout:
        side = "LONG"
        setup_type = "breakout"
    elif short_breakdown:
        side = "SHORT"
        setup_type = "breakdown"
    elif long_pullback:
        side = "LONG"
        setup_type = "pullback_continuation"
    elif short_pullback:
        side = "SHORT"
        setup_type = "pullback_continuation"

    if not side:
        return None

    # Avoid absurdly volatile charts for simple daytrading.
    if atr_pct > 8:
        return None

    # Entry / stop / TP
    if side == "LONG":
        entry_low = close - 0.15 * a
        entry_high = close + 0.10 * a
        stop_loss = min(close - 1.20 * a, recent_low)
        risk = entry_high - stop_loss
        if risk <= 0:
            return None
        tp1 = entry_high + 1.0 * risk
        tp2 = entry_high + 1.6 * risk
        tp3 = entry_high + 2.2 * risk
    else:
        entry_low = close - 0.10 * a
        entry_high = close + 0.15 * a
        stop_loss = max(close + 1.20 * a, recent_high)
        risk = stop_loss - entry_low
        if risk <= 0:
            return None
        tp1 = entry_low - 1.0 * risk
        tp2 = entry_low - 1.6 * risk
        tp3 = entry_low - 2.2 * risk

    # Leverage recommendation for paper trading only.
    # Higher volatility -> lower leverage.
    if atr_pct >= 5:
        leverage = 2
    elif atr_pct >= 3:
        leverage = 3
    elif atr_pct >= 1.5:
        leverage = 4
    else:
        leverage = 5

    leverage = min(leverage, MAX_LEVERAGE_CAP)

    # Max hold time: intraday only.
    if setup_type in ["breakout", "breakdown"]:
        max_hold_minutes = min(DEFAULT_MAX_HOLD_MINUTES, 180)
    else:
        max_hold_minutes = min(DEFAULT_MAX_HOLD_MINUTES, 240)

    local_score = 0
    local_score += 2 if trend in ["up", "down"] else 0
    local_score += 2 if volume_spike >= 1.5 else 1 if volume_spike >= 1.15 else 0
    local_score += 2 if setup_type in ["breakout", "breakdown"] else 1
    local_score += 1 if 1 <= atr_pct <= 5 else 0
    local_score += 1 if price_change_24h is not None and abs(price_change_24h) <= 18 else 0

    return {
        "symbol": symbol,
        "side": side,
        "setup_type": setup_type,
        "current_price": close,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "leverage": leverage,
        "max_hold_minutes": max_hold_minutes,
        "confidence": local_score,
        "risk_score": int(clamp(round(atr_pct * 1.5), 1, 10)),
        "atr": a,
        "atr_pct": atr_pct,
        "rsi_15m": r,
        "ema20_15m": e20,
        "ema50_15m": e50,
        "volume_spike": volume_spike,
        "price_change_24h": price_change_24h,
        "quote_volume": quote_volume,
        "recent_high_15m": recent_high,
        "recent_low_15m": recent_low,
        "trend": trend,
    }


# =========================
# NEWS
# =========================

def fetch_news_entries(limit_per_feed: int = 8) -> List[Dict[str, str]]:
    if not DISCOVERY_NEWS_ENABLED:
        return []

    entries = []

    for feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:limit_per_feed]:
                title = str(getattr(entry, "title", "") or "")
                summary = str(getattr(entry, "summary", "") or "")
                link = str(getattr(entry, "link", "") or "")

                if title:
                    entries.append({
                        "title": title,
                        "summary": re.sub(r"<[^>]+>", "", summary)[:400],
                        "link": link,
                        "source": feed_url,
                    })

        except Exception as e:
            logger.warning("RSS fetch failed %s: %s", feed_url, e)

    return entries


def news_for_symbol(news: List[Dict[str, str]], symbol: str) -> List[Dict[str, str]]:
    sym = symbol.replace("USDT", "")
    results = []

    for item in news:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        if re.search(rf"(?<![a-z0-9]){re.escape(sym.lower())}(?![a-z0-9])", text):
            results.append(item)

    return results[:3]


# =========================
# AI VALIDATION
# =========================

def validate_signal_with_ai(candidate: Dict[str, Any], news: List[Dict[str, str]]) -> Dict[str, Any]:
    prompt = f"""
Ти AI-валідатор тестових crypto daytrading сигналів.

ВАЖЛИВО:
- Це тільки paper trading / TradingView test.
- Не давай фінансову пораду.
- Не схвалюй слабкі сигнали.
- Сигнал має бути intraday: максимум кілька годин.
- Відхиляй FOMO, якщо рух уже занадто пізній.
- Відхиляй, якщо немає достатньої конвергенції: тренд + обʼєм + setup + нормальна волатильність.
- Рівні входу/SL/TP уже пораховані кодом. Ти тільки вирішуєш approve/reject і пояснюєш ризики.

Кандидат:
{json.dumps(candidate, ensure_ascii=False, indent=2)}

Новини по символу, якщо є:
{json.dumps(news, ensure_ascii=False, indent=2)}

Поверни тільки JSON без markdown:
{{
  "approve": true/false,
  "score": 0-10,
  "signal_quality": "strong/normal/weak/reject",
  "reason_uk": "коротко українською, чому approve/reject",
  "risks": ["..."],
  "fomo_warning": "low/medium/high",
  "what_must_happen": ["умови, які мають підтвердити сигнал"],
  "cancel_if": ["умови, коли сигнал не брати"],
  "confidence": 0-10
}}
"""

    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a strict crypto paper-trading signal validator. Return valid JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=AI_TEMPERATURE,
        )

        content = completion.choices[0].message.content or "{}"
        data = safe_json_loads(content)

        return {
            "approve": bool(data.get("approve", False)),
            "score": int(data.get("score", 0)),
            "signal_quality": str(data.get("signal_quality", "reject")),
            "reason_uk": str(data.get("reason_uk", "")),
            "risks": data.get("risks", []),
            "fomo_warning": str(data.get("fomo_warning", "medium")),
            "what_must_happen": data.get("what_must_happen", []),
            "cancel_if": data.get("cancel_if", []),
            "confidence": int(data.get("confidence", 0)),
        }

    except Exception as e:
        logger.error("Groq signal validation failed: %s", e)
        return {
            "approve": False,
            "score": 0,
            "signal_quality": "reject",
            "reason_uk": "AI validation failed.",
            "risks": [],
            "fomo_warning": "medium",
            "what_must_happen": [],
            "cancel_if": [],
            "confidence": 0,
        }


# =========================
# CANDIDATE SCAN
# =========================

def pick_market_candidates() -> List[Dict[str, Any]]:
    allowed = get_usdt_perp_symbols()
    tickers = fetch_24h_tickers()

    candidates = []

    for t in tickers:
        symbol = t.get("symbol")
        if symbol not in allowed:
            continue

        quote_volume = safe_float(t.get("quoteVolume")) or 0
        price_change = safe_float(t.get("priceChangePercent")) or 0

        if quote_volume < MIN_24H_QUOTE_VOLUME:
            continue

        # Exclude too extreme 24h moves for a controlled daytrading bot.
        if abs(price_change) > 35:
            continue

        # Candidate score: volume + movement but not too crazy.
        score = 0
        score += min(5, quote_volume / 100_000_000)
        score += min(5, abs(price_change) / 4)

        candidates.append({
            "symbol": symbol,
            "quoteVolume": quote_volume,
            "priceChangePercent": price_change,
            "candidate_score": score,
            "ticker": t,
        })

    candidates.sort(key=lambda x: x["candidate_score"], reverse=True)
    return candidates[:MAX_CANDIDATES_PER_SCAN]


def scan_for_signals() -> List[Dict[str, Any]]:
    market_candidates = pick_market_candidates()
    news = fetch_news_entries()

    valid_signals = []

    for c in market_candidates:
        symbol = c["symbol"]

        try:
            raw_signal = analyze_symbol(symbol, c["ticker"])
            if not raw_signal:
                continue

            related_news = news_for_symbol(news, symbol)
            ai = validate_signal_with_ai(raw_signal, related_news)

            if not ai.get("approve"):
                logger.info("AI rejected %s: %s", symbol, ai.get("reason_uk"))
                continue

            if int(ai.get("score", 0)) < MIN_AI_SIGNAL_SCORE:
                logger.info("Low AI score %s: %s", symbol, ai.get("score"))
                continue

            raw_signal["ai"] = ai
            raw_signal["news"] = related_news
            raw_signal["confidence"] = int(ai.get("score", raw_signal.get("confidence", 0)))
            raw_signal["risk_score"] = max(raw_signal.get("risk_score", 1), 10 - int(ai.get("confidence", 5)))

            valid_signals.append(raw_signal)

            if len(valid_signals) >= MAX_SIGNALS_PER_SCAN:
                break

            time.sleep(0.4)

        except Exception as e:
            logger.warning("Signal analysis failed for %s: %s", symbol, e)

    return valid_signals


# =========================
# TELEGRAM FORMAT
# =========================

def format_signal_message(signal: Dict[str, Any]) -> str:
    ai = signal.get("ai", {})
    side_emoji = "🟢" if signal["side"] == "LONG" else "🔴"

    risks = ai.get("risks") or []
    what = ai.get("what_must_happen") or []
    cancel = ai.get("cancel_if") or []

    risks_text = "\n".join([f"• {escape(x)}" for x in risks[:5]]) or "н/д"
    what_text = "\n".join([f"• {escape(x)}" for x in what[:5]]) or "н/д"
    cancel_text = "\n".join([f"• {escape(x)}" for x in cancel[:5]]) or "н/д"

    msg = f"""
{side_emoji} <b>TEST SIGNAL — {escape(signal['symbol'])} {escape(signal['side'])}</b>

⚠️ <b>Paper trading only</b>

<b>Setup:</b> {escape(signal.get("setup_type"))}
<b>AI score:</b> {escape(ai.get("score"))}/10
<b>Quality:</b> {escape(ai.get("signal_quality"))}
<b>FOMO:</b> {escape(ai.get("fomo_warning"))}
<b>Max hold:</b> {escape(signal.get("max_hold_minutes"))} хв
<b>Leverage:</b> до x{escape(signal.get("leverage"))}

<b>Entry zone:</b>
{escape(money(signal.get("entry_low")))} — {escape(money(signal.get("entry_high")))}

<b>Stop Loss:</b>
{escape(money(signal.get("stop_loss")))}

<b>Take Profits:</b>
TP1: {escape(money(signal.get("tp1")))}
TP2: {escape(money(signal.get("tp2")))}
TP3: {escape(money(signal.get("tp3")))}

<b>Market:</b>
Price: {escape(money(signal.get("current_price")))}
24h: {escape(pct(signal.get("price_change_24h")))}
RSI 15m: {escape(round(signal.get("rsi_15m", 0), 2))}
ATR: {escape(round(signal.get("atr_pct", 0), 2))}%
Volume spike: x{escape(round(signal.get("volume_spike", 0), 2))}

<b>AI reason:</b>
{escape(ai.get("reason_uk"))}

<b>Ризики:</b>
{risks_text}

<b>Що має підтвердити сигнал:</b>
{what_text}

<b>Не брати / скасувати якщо:</b>
{cancel_text}

<i>Це тестовий сигнал для paper trading, не фінансова порада.</i>
"""
    return msg.strip()


def format_signal_update(signal: Dict[str, Any], status: str, price: Optional[float], note: str) -> str:
    emoji = "✅" if status in ["tp1", "tp2", "tp3"] else "🛑" if status == "stopped" else "⏱"

    return f"""
{emoji} <b>Signal update — {escape(signal['symbol'])}</b>

<b>Status:</b> {escape(status)}
<b>Side:</b> {escape(signal['side'])}
<b>Current/Exit price:</b> {escape(money(price))}
<b>Note:</b> {escape(note)}

<i>Paper trading log.</i>
""".strip()


# =========================
# HEALTH ALERTS
# =========================

async def send_health_alert(application: Application, title: str, details: str) -> None:
    global LAST_HEALTH_ALERT_TS

    now = time.time()
    if now - LAST_HEALTH_ALERT_TS < HEALTH_ALERT_COOLDOWN_SECONDS:
        return

    LAST_HEALTH_ALERT_TS = now

    users = get_all_users()
    if not users:
        return

    text = f"""
⚠️ <b>Crypto Signal AI health alert</b>

<b>{escape(title)}</b>

{escape(details)}
"""

    for u in users:
        try:
            await application.bot.send_message(
                chat_id=str(u["telegram_id"]),
                text=text.strip(),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error("Could not send health alert: %s", e)


# =========================
# AGENT JOBS
# =========================

async def send_signals_to_users(application: Application, signals: List[Dict[str, Any]], manual_telegram_id: Optional[str] = None) -> int:
    if manual_telegram_id:
        users = [{"telegram_id": manual_telegram_id, "signals_enabled": True}]
    else:
        users = get_signals_enabled_users()

    sent = 0

    for user in users:
        telegram_id = str(user["telegram_id"])

        if count_open_signals(telegram_id) >= MAX_OPEN_SIGNALS:
            logger.info("Max open signals reached for %s", telegram_id)
            continue

        for signal in signals:
            symbol = signal["symbol"]
            side = signal["side"]

            if has_open_signal_for_symbol(telegram_id, symbol):
                continue

            if recently_sent_signal(telegram_id, symbol, side):
                continue

            saved = save_signal(telegram_id, signal)
            msg = format_signal_message(signal)

            await application.bot.send_message(
                chat_id=telegram_id,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

            sent += 1
            time.sleep(0.4)

            if count_open_signals(telegram_id) >= MAX_OPEN_SIGNALS:
                break

    return sent


async def scheduled_signal_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Scheduled signal scan started.")

    try:
        signals = scan_for_signals()

        if not signals:
            logger.info("No valid signals found.")
            return

        sent = await send_signals_to_users(context.application, signals)
        logger.info("Scheduled signal scan finished. Sent: %s", sent)

    except MarketFetchError as e:
        logger.error("Market fetch error: %s %s", e.kind, e.message)
        await send_health_alert(context.application, f"Market API problem: {e.kind}", e.message)
    except Exception as e:
        logger.error("Signal scan error: %s", e)
        logger.error(traceback.format_exc())
        await send_health_alert(context.application, "Unexpected signal scan error", str(e))


async def scheduled_signal_manage(context: ContextTypes.DEFAULT_TYPE) -> None:
    signals = get_open_signals()

    if not signals:
        return

    for signal in signals:
        try:
            symbol = signal["symbol"]
            side = signal["side"]
            price = fetch_current_price(symbol)

            if price is None:
                continue

            status = None
            note = ""

            if side == "LONG":
                if price <= float(signal["stop_loss"]):
                    status = "stopped"
                    note = "Price touched stop loss."
                elif price >= float(signal["tp3"]):
                    status = "tp3"
                    note = "Price reached TP3."
                elif price >= float(signal["tp2"]):
                    status = "tp2"
                    note = "Price reached TP2."
                elif price >= float(signal["tp1"]):
                    status = "tp1"
                    note = "Price reached TP1."
            else:
                if price >= float(signal["stop_loss"]):
                    status = "stopped"
                    note = "Price touched stop loss."
                elif price <= float(signal["tp3"]):
                    status = "tp3"
                    note = "Price reached TP3."
                elif price <= float(signal["tp2"]):
                    status = "tp2"
                    note = "Price reached TP2."
                elif price <= float(signal["tp1"]):
                    status = "tp1"
                    note = "Price reached TP1."

            expires_at_raw = signal.get("expires_at")
            if not status and expires_at_raw:
                try:
                    expires_at = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
                    if now_utc() >= expires_at:
                        status = "expired"
                        note = "Max hold time reached."
                except Exception:
                    pass

            if status:
                update_signal_status(int(signal["id"]), status, price, note)

                msg = format_signal_update(signal, status, price, note)
                await context.application.bot.send_message(
                    chat_id=str(signal["telegram_id"]),
                    text=msg,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

            time.sleep(0.2)

        except Exception as e:
            logger.warning("Could not manage signal %s: %s", signal.get("id"), e)


# =========================
# COMMANDS
# =========================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return

    telegram_id = str(chat.id)
    ensure_user(telegram_id)

    text = f"""
👋 Привіт! Я <b>Crypto Signal AI</b>.

Я шукаю тестові intraday-сигнали для <b>paper trading</b>:
• сам вибираю монети з Binance Futures
• рахую тренд / обʼєм / RSI / ATR / рівні
• Groq AI фільтрує слабкі і FOMO-сигнали
• даю entry zone, SL, TP, leverage, max hold time

⚠️ <b>Це не фінансова порада і не live trading.</b>

<b>Команди:</b>
/scan — знайти сигнали зараз
/open — відкриті тестові сигнали
/stats — статистика
/signals_on — увімкнути авто-сигнали
/signals_off — вимкнути авто-сигнали
/status — налаштування
/help
"""
    await update.message.reply_text(text.strip(), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = """
<b>Crypto Signal AI — help</b>

/scan — ручний пошук тестових сигналів
/open — відкриті сигнали
/stats — статистика по закритих сигналах
/signals_on — увімкнути авто-скан
/signals_off — вимкнути авто-скан
/status — показати налаштування

<b>Як працює:</b>
1. Бот бере Binance USDT perpetuals.
2. Вибирає монети з обʼємом і рухом.
3. Рахує 15m/5m структуру, EMA, RSI, ATR, volume spike.
4. Формує кандидат-сигнал.
5. AI або схвалює, або відхиляє.
6. Якщо score достатній — надсилає сигнал.

<b>Сигнал містить:</b>
Entry zone, Stop Loss, TP1/TP2/TP3, leverage, max hold time.

<i>Paper trading only. Не фінансова порада.</i>
"""
    await update.message.reply_text(text.strip(), parse_mode=ParseMode.HTML)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return

    telegram_id = str(chat.id)
    ensure_user(telegram_id)

    await update.message.reply_text("🔍 Сканую Binance Futures і шукаю тестові intraday-сигнали...")

    try:
        signals = scan_for_signals()
        if not signals:
            await update.message.reply_text("Поки немає достатньо якісних сигналів. Краще без угоди, ніж погана угода.")
            return

        sent = await send_signals_to_users(context.application, signals, manual_telegram_id=telegram_id)
        await update.message.reply_text(f"✅ Готово. Надіслано тестових сигналів: {sent}")

    except Exception as e:
        await update.message.reply_text(f"Помилка scan: {e}")


async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return

    telegram_id = str(chat.id)
    signals = get_open_signals(telegram_id)

    if not signals:
        await update.message.reply_text("Немає відкритих тестових сигналів.")
        return

    lines = ["📋 <b>Відкриті тестові сигнали:</b>\n"]

    for s in signals:
        lines.append(
            f"#{s['id']} — <b>{escape(s['symbol'])}</b> {escape(s['side'])}\n"
            f"Entry: {escape(money(s.get('entry_low')))} — {escape(money(s.get('entry_high')))}\n"
            f"SL: {escape(money(s.get('stop_loss')))} | TP1: {escape(money(s.get('tp1')))}\n"
            f"Leverage: x{escape(s.get('leverage'))} | expires: {escape(s.get('expires_at'))}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return

    telegram_id = str(chat.id)

    result = (
        supabase.table("signals")
        .select("*")
        .eq("telegram_id", telegram_id)
        .neq("status", "open")
        .execute()
    )

    rows = result.data or []
    if not rows:
        await update.message.reply_text("Ще немає закритих сигналів для статистики.")
        return

    total = len(rows)
    wins = len([r for r in rows if str(r.get("status", "")).startswith("tp")])
    losses = len([r for r in rows if r.get("status") == "stopped"])
    expired = len([r for r in rows if r.get("status") == "expired"])

    winrate = (wins / total) * 100 if total else 0

    text = f"""
📊 <b>Paper signal stats</b>

Total closed: {total}
Wins TP: {wins}
Stopped: {losses}
Expired: {expired}

Winrate by TP touch: {winrate:.2f}%

<i>Це груба статистика торкання TP/SL, не PnL.</i>
"""
    await update.message.reply_text(text.strip(), parse_mode=ParseMode.HTML)


async def signals_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return

    telegram_id = str(chat.id)
    set_signals_enabled(telegram_id, True)
    await update.message.reply_text("✅ Авто-сигнали увімкнено.")


async def signals_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not update.message:
        return

    telegram_id = str(chat.id)
    set_signals_enabled(telegram_id, False)
    await update.message.reply_text("⏸ Авто-сигнали вимкнено. /scan все ще працює вручну.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = f"""
⚙️ <b>Crypto Signal AI status</b>

<b>Market:</b> Binance Futures public API
<b>AI:</b> Groq — {escape(GROQ_MODEL)}

<b>Scan interval:</b> {SIGNAL_SCAN_INTERVAL_SECONDS} sec
<b>Manage interval:</b> {SIGNAL_MANAGE_INTERVAL_SECONDS} sec

<b>MIN_AI_SIGNAL_SCORE:</b> {MIN_AI_SIGNAL_SCORE}/10
<b>MAX_CANDIDATES_PER_SCAN:</b> {MAX_CANDIDATES_PER_SCAN}
<b>MAX_SIGNALS_PER_SCAN:</b> {MAX_SIGNALS_PER_SCAN}

<b>MAX_LEVERAGE_CAP:</b> x{MAX_LEVERAGE_CAP}
<b>DEFAULT_MAX_HOLD_MINUTES:</b> {DEFAULT_MAX_HOLD_MINUTES}
<b>SIGNAL_COOLDOWN_HOURS:</b> {SIGNAL_COOLDOWN_HOURS}

<b>MIN_24H_QUOTE_VOLUME:</b> {MIN_24H_QUOTE_VOLUME}

<i>Paper trading only.</i>
"""
    await update.message.reply_text(text.strip(), parse_mode=ParseMode.HTML)


# =========================
# MAIN
# =========================

def main() -> None:
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CommandHandler("open", open_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("signals_on", signals_on_command))
    application.add_handler(CommandHandler("signals_off", signals_off_command))
    application.add_handler(CommandHandler("status", status_command))

    application.job_queue.run_repeating(
        scheduled_signal_scan,
        interval=SIGNAL_SCAN_INTERVAL_SECONDS,
        first=90,
        name="scheduled_signal_scan",
    )

    application.job_queue.run_repeating(
        scheduled_signal_manage,
        interval=SIGNAL_MANAGE_INTERVAL_SECONDS,
        first=120,
        name="scheduled_signal_manage",
    )

    logger.info("Crypto Signal AI started.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
