# Crypto Signal AI — fixed Bybit patch

Replace main.py.

Optional:
- replace .env.example
- replace README.md

Railway variables:
Remove:
BINANCE_FUTURES_BASE_URL

Add:
BYBIT_BASE_URL=https://api.bybit.com
BYBIT_CATEGORY=linear

Quick check inside main.py:
You should see:
APP_VERSION = "crypto-signal-ai-bybit-v1"
BYBIT_BASE_URL
bybit_get
/v5/market/tickers

You should NOT see:
BINANCE_FUTURES_BASE_URL
def binance_get
