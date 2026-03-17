# UpDown

A self-contained up/down prediction engine and trading bot for [Polymarket](https://polymarket.com), built without relying on third-party prediction services. Price data is sourced from the [CoinGecko](https://www.coingecko.com/) free API; market discovery uses Polymarket's public [Gamma API](https://gamma-api.polymarket.com); and order placement uses the official [py-clob-client](https://github.com/Polymarket/py-clob-client) SDK.

**🎰 Now with 100% Telegram Control**: Control everything via Telegram commands - set wallets, start/stop trading, view balances, and configure settings without ever touching the server.

**⚡ Solana Auto-Funding**: Automatically bridge USDC from your Solana wallet to Polygon when your trading balance is low.

> **Disclaimer:** This is not financial advice. Prediction markets and crypto trading carry significant risk. Use at your own risk.

---

## 🆕 Telegram Bot Mode (Recommended)

Control the entire bot via Telegram - no server access needed after initial setup!

### Quick Start

1. **Create a Telegram Bot**
   - Message [@BotFather](https://t.me/BotFather) on Telegram
   - Send `/newbot` and follow the prompts
   - Save the bot token

2. **Get Your Chat ID**
   - Message [@userinfobot](https://t.me/userinfobot) on Telegram
   - Send `/start` to get your chat ID

3. **Configure Environment**
   ```bash
   cp .env.example .env
   # Edit .env and set:
   # TELEGRAM_BOT_TOKEN=your_bot_token
   # TELEGRAM_CHAT_ID=your_chat_id
   ```

4. **Run the Telegram Bot**
   ```bash
   pip install -r requirements.txt
   python telegram_bot.py
   ```

5. **Control via Telegram**
   - Send `/start` to your bot to see the main menu
   - Use `/help` for all available commands

### Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu with inline buttons |
| `/status` | View bot status, balances, and predictions |
| `/balance` | View Solana and Polygon wallet balances |
| `/predict` | Get current price prediction |
| `/markets` | Find relevant Polymarket markets |
| `/start_bot` | Start the automated trading loop |
| `/stop_bot` | Stop the trading loop |
| `/trade` | Execute a manual trade |
| `/bridge` | Manually bridge Solana → Polygon |
| `/pnl` | View P&L history |
| `/config` | View/edit bot settings |
| `/toggle_dry_run` | Switch between dry run and live trading |
| `/set_solana_key` | Set Solana wallet (securely via DM) |
| `/set_polygon_key` | Set Polygon wallet (securely via DM) |
| `/set_polymarket_api` | Set Polymarket API credentials |
| `/set_trade_amount` | Configure trade size |
| `/set_min_balance` | Set minimum Polygon balance for auto-funding |
| `/set_bridge_amount` | Set amount to bridge when auto-funding |
| `/set_interval` | Set cycle interval |
| `/help` | Show all commands |

### Security Features

- **Authorized User Only**: Only your `TELEGRAM_CHAT_ID` can control the bot
- **Secure Key Input**: Private keys are deleted from chat history immediately
- **Memory-Only Storage**: Sensitive credentials are stored in memory only, not on disk
- **Dry Run Default**: Bot starts in dry-run mode - must explicitly enable live trading

---

## 📊 How It Works

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

## 🖥️ Classic Mode (CLI)

If you prefer running the bot directly without Telegram:

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

## 🌉 Solana Auto-Funding

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
4. **Transfer** – USDC is transferred to the bridge deposit address.
5. **Notification** – In Telegram mode, you receive a notification with transaction details.

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

---

## ⚙️ Customisation

- **Prediction windows** – Set `SHORT_WINDOW` / `LONG_WINDOW` env vars (or use `/config` in Telegram).
- **Asset** – Configure `crypto_id` (any CoinGecko ID, e.g. `"ethereum"`).
- **Market query** – Customize `query_terms` to match different Polymarket questions.
- **Trade size** – Use `/set_trade_amount` in Telegram or set in config.
- **Auto-funding** – Configure `MIN_POLY_BALANCE_USDC` and `BRIDGE_FUND_AMOUNT` via Telegram or env vars.
- **Advanced ML** – Replace `predict_up_down()` with a logistic-regression or neural-network model trained on historical data from `cg.get_coin_market_chart_by_id()`.

---

## 📁 File Structure

```
telegram_bot.py    NEW: Full Telegram-controlled bot with all features
updown_bot.py      Classic CLI bot: prediction engine + trading loop + Solana funding
requirements.txt   Python dependencies (includes python-telegram-bot)
.env.example       Template for environment variables (copy to .env)
.gitignore         Excludes secrets, venvs, and build artefacts
bot_config.json    Auto-generated: Non-sensitive bot configuration (gitignored)
daily_pnl.json     Auto-generated: P&L tracking data
```

---

## 🚀 Deployment (Railway)

For cloud deployment on Railway:

1. Create a new Railway project
2. Connect your GitHub repository
3. Add environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Deploy! The bot will run continuously and you control everything via Telegram.

Optional: Add `railway.json` for custom build settings:
```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "python telegram_bot.py",
    "restartPolicyType": "ON_FAILURE"
  }
}
```