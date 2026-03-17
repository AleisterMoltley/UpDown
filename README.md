# UpDown

A self-contained up/down prediction engine and trading bot for [Polymarket](https://polymarket.com), built without relying on third-party prediction services. Price data is sourced from the [CoinGecko](https://www.coingecko.com/) free API; market discovery uses Polymarket's public [Gamma API](https://gamma-api.polymarket.com); and order placement uses the official [py-clob-client](https://github.com/Polymarket/py-clob-client) SDK.

**Now with Solana auto-funding**: The bot can automatically bridge USDC from your Solana wallet to Polygon when your trading balance is low.

> **Disclaimer:** This is not financial advice. Prediction markets and crypto trading carry significant risk. Use at your own risk.

---

## How It Works

1. **Data Fetching** – Pulls the last 24 h of 5-minute OHLC candles for Bitcoin (or any CoinGecko asset) via `pycoingecko`.
2. **Up/Down Engine** – A custom moving-average comparator:
   - Computes a 5-period (fast) and 20-period (slow) simple moving average over recent closing prices (configurable via env vars).
   - Returns `"up"` when the current fast MA is above the current slow MA (bullish), `"down"` otherwise (bearish).
   - Returns `"hold"` when there is insufficient data.
3. **Market Discovery** – Queries Polymarket's Gamma API for active, unclosed markets whose `question` contains your search terms (e.g. current date + "btc").
4. **Trade Execution** – Optionally places a limit order (yes/no) on each matched market via the CLOB client. Automatically skips trading when credentials are absent (dry-run mode).
5. **Solana Auto-Funding** – Before each cycle, checks your Polygon balance and automatically bridges USDC from Solana if below threshold.
6. **Loop** – Repeats every 5 minutes (configurable via `CYCLE_INTERVAL_SECONDS`).

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
| `SHORT_WINDOW` | Fast MA period (default: `5`) |
| `LONG_WINDOW` | Slow MA period (default: `20`) |

Follow [Polymarket's quickstart docs](https://docs.polymarket.com/) to generate API credentials and fund your wallet with USDC on Polygon.

### 3. Run the bot

```bash
# Load .env (if using a shell without automatic dotenv support)
export $(grep -v '^#' .env | xargs)

python updown_bot.py
```

If any credential variable is missing the bot runs in **dry-run mode** – it logs predictions and matched markets but never submits orders.

---

## Solana Auto-Funding

The bot can automatically bridge USDC from your Solana wallet to your Polygon address when your trading balance falls below a configurable threshold. This means you can fund your Solana wallet once and let the bot handle the rest.

### Configuration

| Variable | Description | Default |
|---|---|---|
| `SOLANA_PRIVATE_KEY` | Your Solana wallet private key (base58 encoded) | *Required for auto-funding* |
| `SOLANA_RPC_URL` | Solana RPC endpoint | `https://api.mainnet-beta.solana.com` |
| `MIN_POLY_BALANCE_USDC` | Minimum Polygon balance before triggering bridge | `20.0` |
| `BRIDGE_FUND_AMOUNT` | Amount (USDC) to bridge when triggered | `50.0` |

### How It Works

1. **Balance Check** – At the start of each bot cycle, the bot checks your Polygon USDC balance via the CLOB client.
2. **Threshold Detection** – If balance < `MIN_POLY_BALANCE_USDC`, auto-funding is triggered.
3. **Bridge Setup** – The bot derives your Polygon address and requests a deposit address from the Polymarket bridge.
4. **Safety Confirmation** – Before any transfer:
   - A detailed summary is printed showing source, destination, and amounts
   - A QR code link is generated for address verification
   - A **30-second safety delay** allows you to abort (Ctrl+C)
5. **Transfer** – USDC is transferred first (if available), otherwise SOL as fallback.
6. **Confirmation** – Success/failure is logged with transaction details.

### Example Usage

```bash
# Fund your Solana wallet with USDC once
# The bot will automatically bridge to Polygon when needed

# Set up your .env with Solana credentials
SOLANA_PRIVATE_KEY=your_base58_solana_private_key_here
MIN_POLY_BALANCE_USDC=20.0
BRIDGE_FUND_AMOUNT=50.0

# Run the bot - it will auto-fund when balance is low
python updown_bot.py
```

> ⚠️ **SECURITY WARNING**
>
> **Private keys grant full control over your funds.**
>
> - **NEVER** share your private keys with anyone
> - **NEVER** commit `.env` or private keys to source control
> - Store keys securely (hardware wallet, encrypted vault, etc.)
> - The `SOLANA_PRIVATE_KEY` should be a base58-encoded secret key
> - The `POLYMARKET_PRIVATE_KEY` should be a hex-encoded Ethereum/Polygon private key
> - Consider using a dedicated trading wallet with limited funds
> - The 30-second safety delay before transfers allows you to verify and abort if needed

---

## Customisation

- **Prediction windows** – Set `SHORT_WINDOW` / `LONG_WINDOW` env vars (or pass to `predict_up_down()`).
- **Asset** – Pass a different `crypto_id` to `run_bot()` (any CoinGecko ID, e.g. `"ethereum"`).
- **Market query** – Pass custom `query_terms` to `run_bot()` to match different Polymarket questions.
- **Trade size** – Set `trade_amount` in `run_bot()` (USDC).
- **Auto-funding** – Configure `MIN_POLY_BALANCE_USDC` and `BRIDGE_FUND_AMOUNT` to control when and how much to bridge.
- **Advanced ML** – Replace `predict_up_down()` with a logistic-regression or neural-network model trained on historical data from `cg.get_coin_market_chart_by_id()`.

---

## File Structure

```
updown_bot.py      Main bot: prediction engine + market discovery + trading loop + Solana funding
requirements.txt   Python dependencies
.env.example       Template for environment variables (copy to .env)
.gitignore         Excludes secrets, venvs, and build artefacts
```