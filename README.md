# UpDown

A self-contained up/down prediction engine and trading bot for [Polymarket](https://polymarket.com), built without relying on third-party prediction services. Price data is sourced from the [CoinGecko](https://www.coingecko.com/) free API; market discovery uses Polymarket's public [Gamma API](https://gamma-api.polymarket.com); and order placement uses the official [py-clob-client](https://github.com/Polymarket/py-clob-client) SDK.

> **Disclaimer:** This is not financial advice. Prediction markets and crypto trading carry significant risk. Use at your own risk.

---

## How It Works

1. **Data Fetching** – Pulls the last 24 h of 5-minute OHLC candles for Bitcoin (or any CoinGecko asset) via `pycoingecko`.
2. **Up/Down Engine** – A custom moving-average comparator:
   - Computes a 5-period (fast) and 20-period (slow) simple moving average over recent closing prices.
   - Returns `"up"` when the current fast MA is above the current slow MA (bullish), `"down"` otherwise (bearish).
   - Returns `"hold"` when there is insufficient data.
3. **Market Discovery** – Queries Polymarket's Gamma API for active, unclosed markets whose `question` contains your search terms (e.g. current date + "btc").
4. **Trade Execution** – Optionally places a limit order (yes/no) on each matched market via the CLOB client. Automatically skips trading when credentials are absent (dry-run mode).
5. **Loop** – Repeats every 5 minutes (configurable via `CYCLE_INTERVAL_SECONDS`).

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

> `py-clob-client` is only required if you intend to place live trades. The bot runs fine without it in dry-run mode.

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `POLYMARKET_PRIVATE_KEY` | Your Polygon wallet private key |
| `POLYMARKET_API_KEY` | Polymarket CLOB API key |
| `POLYMARKET_API_SECRET` | Polymarket CLOB API secret |
| `POLYMARKET_API_PASSPHRASE` | Polymarket CLOB API passphrase |
| `POLYMARKET_HOST` | CLOB endpoint (default: `https://clob.polymarket.com`) |
| `POLYMARKET_CHAIN_ID` | Polygon chain ID (default: `137`) |
| `CYCLE_INTERVAL_SECONDS` | Seconds between cycles (default: `300`) |

Follow [Polymarket's quickstart docs](https://docs.polymarket.com/) to generate API credentials and fund your wallet with USDC on Polygon.

### 3. Run the bot

```bash
# Load .env (if using a shell without automatic dotenv support)
export $(grep -v '^#' .env | xargs)

python updown_bot.py
```

If any credential variable is missing the bot runs in **dry-run mode** – it logs predictions and matched markets but never submits orders.

---

## Customisation

- **Prediction windows** – Adjust `short_window` / `long_window` in `predict_up_down()`.
- **Asset** – Pass a different `crypto_id` to `run_bot()` (any CoinGecko ID, e.g. `"ethereum"`).
- **Market query** – Pass custom `query_terms` to `run_bot()` to match different Polymarket questions.
- **Trade size** – Set `trade_amount` in `run_bot()` (USDC).
- **Advanced ML** – Replace `predict_up_down()` with a logistic-regression or neural-network model trained on historical data from `cg.get_coin_market_chart_by_id()`.

---

## File Structure

```
updown_bot.py      Main bot: prediction engine + market discovery + trading loop
requirements.txt   Python dependencies
.env.example       Template for environment variables (copy to .env)
.gitignore         Excludes secrets, venvs, and build artefacts
```