# Crypto Signal AI

A Telegram AI agent for **paper trading only**.

It scans Bybit USDT perpetuals, finds intraday setups, calculates:
- Entry zone
- Stop Loss
- Take Profits
- Max leverage
- Max hold time

Then Groq AI validates whether the signal is worth sending.

It is NOT financial advice and it does NOT place trades.

## Commands

```text
/start
/help
/scan
/open
/stats
/signals_on
/signals_off
/status
```

## Setup

### 1. Supabase

Create a Supabase project.

Run `database.sql` in SQL Editor.

### 2. Telegram

Create bot with BotFather.

### 3. Groq

Create Groq API key.

### 4. GitHub

Upload:

```text
main.py
requirements.txt
database.sql
.env.example
README.md
Procfile
```

### 5. Railway

Deploy from GitHub.

Add variables from `.env.example`.

Recommended test settings:

```env
SIGNAL_SCAN_INTERVAL_SECONDS=900
SIGNAL_MANAGE_INTERVAL_SECONDS=300
MIN_AI_SIGNAL_SCORE=7
MAX_CANDIDATES_PER_SCAN=20
MAX_SIGNALS_PER_SCAN=3
MAX_LEVERAGE_CAP=5
DEFAULT_MAX_HOLD_MINUTES=240
```

## How it works

1. Fetches Bybit USDT perpetual symbols.
2. Filters by liquidity and 24h movement.
3. Pulls 15m and 5m candles.
4. Calculates EMA20/EMA50, RSI, ATR, volume spike.
5. Builds candidate signal from deterministic levels.
6. Groq AI approves/rejects.
7. Sends test signal to Telegram.
8. Tracks open signal until TP/SL/expiry.

## Important

This is for TradingView paper trading / testing only.
Crypto futures are high risk.
The bot can be wrong.


## Bybit patch

This version uses Bybit V5 public market API:
- /v5/market/instruments-info
- /v5/market/tickers
- /v5/market/kline

Railway variables:

```env
BYBIT_BASE_URL=https://api.bybit.com
BYBIT_CATEGORY=linear
```
