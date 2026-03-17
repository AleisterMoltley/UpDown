# UpDown

A self-contained up/down prediction engine and trading bot for [Polymarket](https://polymarket.com), built without relying on third-party prediction services. Price data is sourced from the [CoinGecko](https://www.coingecko.com/) free API; market discovery uses Polymarket's public [Gamma API](https://gamma-api.polymarket.com); and order placement uses the official [py-clob-client](https://github.com/Polymarket/py-clob-client) SDK.

**🔗 100% Onchain Mode (2026)**: Only your wallet private key is needed! L2 API credentials are automatically derived - no more manual API key setup.

**🎰 100% Telegram Control**: Control everything via Telegram commands - set wallets, start/stop trading, view balances, and configure settings without ever touching the server.

**⚡ Solana Auto-Funding**: Automatically bridge USDC from your Solana wallet to Polygon when your trading balance is low.

> **Disclaimer:** This is not financial advice. Prediction markets and crypto trading carry significant risk. Use at your own risk.

---

## 🤠 THE GOOD OL' COUNTRY BOY GUIDE: How To Get This Bot Runnin' (For Total Beginners)

Alright y'all, listen here. I'm gonna walk ya through this whole dang thing from the very start. No fancy computer words, just plain ol' English like we're sittin' on the porch talkin' 'bout tractors. Let's do this thang.

### PART 1: WHAT THE HECK IS THIS BOT AND WHAT DO I NEED?

This here bot is like a little robot friend who looks at Bitcoin prices and tries to guess if they gonna go up or down. Then it can bet on Polymarket for ya. Fancy, huh?

**What ya need before we start:**
- A computer (duh)
- Internet connection (double duh)
- A phone with Telegram app (it's free, get it from your app store)
- A brain (optional, but helps)
- About 30 minutes of your time
- Maybe a cold beer 🍺

---

### PART 2: GETTIN' TELEGRAM ALL SET UP

Alright, first thing's first. We gotta make ourselves a Telegram bot. Don't worry, it's easier than bakin' a pie.

#### Step 2.1: Create Your Bot (Talk to the BotFather)

1. **Open Telegram** on your phone or computer
2. **Search for `@BotFather`** in the search bar at the top
   - He's the big daddy of all Telegram bots
   - Make sure it's got the blue checkmark (verified) so you don't get scammed by some imposter
3. **Tap on BotFather** and hit that START button
4. **Send him this message:** `/newbot`
5. **He'll ask you for a name** - type whatever you want, like `My Awesome Trading Bot` or `Billy Bob's Money Maker`
6. **Then he'll ask for a username** - this gotta end in `bot`, like `mybillybobbot` or `moneymachine_bot`
   - If it says "taken", just add some numbers like `mymoneymachine123_bot`
7. **BOOM! He'll give you a TOKEN** - it looks like a buncha random letters and numbers like this:
   ```
   5547382916:AAH2yKvZ_blahblahblah_moreRandomStuff
   ```
8. **WRITE THAT TOKEN DOWN SOMEWHERE SAFE!!!** 
   - Put it in a text file, write it on paper, tattoo it on your arm, I don't care
   - But DON'T share it with nobody else, ya hear?

#### Step 2.2: Get Your Chat ID

Now we gotta find your personal Chat ID. It's like your Telegram phone number.

1. **Search for `@userinfobot`** in Telegram
2. **Start a chat with him** (click START)
3. **He'll immediately tell you your ID** - it's just a number, like `123456789`
4. **Write that number down too!**

**Great job partner! You now got:**
- ✅ A Bot Token (that long string of letters and numbers)
- ✅ Your Chat ID (just a number)

---

### PART 3: PUTTIN' THE BOT ON THE INTERNET (Railway Deployment)

Now here's the fun part. We gonna put this bot up in the cloud so it runs 24/7 without you liftin' a finger. We're using a thing called Railway - it's free to start!

#### Step 3.1: Make a GitHub Account (If Ya Don't Got One)

1. **Go to [github.com](https://github.com)**
2. **Click "Sign Up"**
3. **Put in your email, make a password, pick a username**
   - Username can be anything, like `billybob_coder_2024`
4. **Do that robot verification thing** (prove you ain't a robot)
5. **Check your email and click the verification link**

Done! You're now officially a coder! 👨‍💻

#### Step 3.2: Get This Bot Code Into Your GitHub

1. **Go to this page** (the UpDown repo where you found this guide)
2. **Click the "Fork" button** at the top right
   - This makes your own copy of the code
3. **Click "Create Fork"**
   - Now you got your own copy! Yeehaw!

#### Step 3.3: Sign Up for Railway

1. **Go to [railway.app](https://railway.app)**
2. **Click "Login" or "Start a New Project"**
3. **Click "Login with GitHub"**
   - This connects Railway to your GitHub (makes life easier)
4. **Let Railway access your GitHub** when it asks

#### Step 3.4: Create Your Project on Railway

1. **Click "New Project"** (big purple button)
2. **Click "Deploy from GitHub repo"**
3. **Find and select your forked UpDown repo** from the list
   - If you don't see it, click "Configure GitHub App" and give Railway permission
4. **Wait for it to start deploying** (it'll probably fail the first time - THAT'S OKAY!)
   - It's gonna fail cause we ain't told it our secrets yet

#### Step 3.5: Add Your Secret Stuff (Environment Variables)

This is where we tell Railway about your bot token and chat ID.

1. **Click on your project** in Railway (the one that probably says "failed")
2. **Click on the "Variables" tab**
3. **Click "New Variable"** and add these TWO things:

   **First variable:**
   - Name: `TELEGRAM_BOT_TOKEN`
   - Value: *paste that long token from BotFather*

   **Second variable:**
   - Name: `TELEGRAM_CHAT_ID`
   - Value: *paste your Chat ID number*

4. **That's it for the required stuff!** Railway will automatically redeploy

#### Step 3.6: Make Sure It's Actually Runnin'

1. **Click on "Deployments" tab** in your Railway project
2. **You should see a green checkmark** and "Success" or "Active"
3. **Click on the deployment** to see the logs
4. **You should see something like:**
   ```
   🚀 UpDown Telegram Bot starting...
   ✅ Bot is running! Send /start to your bot.
   ```

**HOT DANG, YOUR BOT IS ALIVE! 🎉**

---

### PART 4: TALKIN' TO YOUR BOT

Now the fun part! Let's actually use this thing.

1. **Go back to Telegram**
2. **Find your bot** (search for the username you gave it, like `@mymoneymachine123_bot`)
3. **Click START**
4. **You should see a welcome message** with some fancy buttons!

#### Basic Commands Ya Can Use:

| What to Type | What It Does |
|--------------|--------------|
| `/start` | Shows the main menu with buttons |
| `/help` | Shows all the commands |
| `/status` | Shows if the bot is runnin' and what it's doin' |
| `/predict` | Gets a prediction (is Bitcoin goin' up or down?) |
| `/balance` | Shows your wallet balances |
| `/toggle_dry_run` | Switches between fake trading and real trading |

**⚠️ IMPORTANT:** The bot starts in "dry run" mode, which means it's just PRETENDING to trade. No real money involved till you switch it!

---

### PART 5: SETTIN' UP FOR REAL TRADING (Optional - Only If You Want to Bet Real Money)

Alright now, if you just wanna watch the predictions, you're done! But if you wanna actually trade on Polymarket, read on...

**🎉 GOOD NEWS:** Since 2026, UpDown uses **100% Onchain Mode** - that means you ONLY need your wallet private key! No more messin' around with API keys, secrets, and passphrases. The bot figures all that out automatically. How cool is that?

#### Step 5.1: Get Polymarket Set Up

1. **Go to [polymarket.com](https://polymarket.com)**
2. **Create an account** and set up your wallet
3. **Get some USDC** (that's the cryptocurrency you trade with on Polygon)
4. **Make sure you got a little MATIC too** (for gas fees - the bot will remind ya if you're low)

#### Step 5.2: Tell Your Bot Your Wallet Key

You can do this right in Telegram! Just message your bot:

1. **Send `/set_polygon_key`** - then paste your Polygon wallet private key

**That's it! Just ONE key!** The bot automatically derives all the API credentials it needs from your wallet. Magic! ✨

**The bot will delete your message right after reading it, so your secrets stay secret!**

#### Step 5.3: Set Up Contract Approvals (First Time Only)

Before your first trade, you need to approve the Polymarket contracts to use your USDC:

1. **Send `/setup_approvals`** - this sets up the required token approvals
2. **Wait for confirmation** - the bot will tell you when it's done

You only gotta do this once! After that, you're good to go.

#### Step 5.4: Start Actual Trading

1. **Send `/toggle_dry_run`** to turn OFF dry run mode (this enables REAL trading!)
2. **Send `/set_trade_amount`** to set how much you wanna bet each time
3. **Send `/gas_status`** to make sure you got enough MATIC for gas
4. **Send `/start_bot`** to start the automated trading loop

**That's it! Your bot will now:**
- Check Bitcoin prices every 5 minutes
- Make predictions (up or down)
- Find matching markets on Polymarket
- Place bets automatically (100% onchain - no middleman!)

---

### PART 6: FANCY EXTRAS (Solana Auto-Funding)

If you want the bot to automatically add more money when you're runnin' low, you can set up Solana auto-funding:

1. **Get some USDC in a Solana wallet** (like Phantom)
2. **Send `/set_solana_key`** to your bot and paste your Solana private key
3. **Send `/set_min_balance`** to set when it should add more money (like when you get below $20)
4. **Send `/set_bridge_amount`** to set how much to add each time

The bot will automatically move money from Solana to Polygon when ya need it. Pretty slick, huh?

---

### PART 7: TROUBLESHOOTIN' (When Stuff Don't Work)

**Bot ain't respondin'?**
- Check Railway to make sure it's still runnin' (Deployments tab, look for green)
- Make sure your Chat ID is right
- Restart the deployment in Railway

**Railway keeps failin'?**
- Check your environment variables are spelled exactly right
- Make sure there ain't no extra spaces in your token or ID

**Can't find your bot in Telegram?**
- Search for the exact username you gave it (with @)
- Make sure you spelled it right

**Bot says "unauthorized"?**
- Your Chat ID ain't matchin' - double check it with @userinfobot

**Still stuck?**
- Take a break, have a beer, come back later
- It's always somethin' simple you missed

---

### PART 8: THE QUICK RECAP (TL;DR for the Lazy Folk)

1. **Telegram stuff:**
   - Talk to @BotFather, make a bot, get a TOKEN
   - Talk to @userinfobot, get your CHAT ID

2. **Railway stuff:**
   - Sign up with GitHub
   - Deploy this repo
   - Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as variables

3. **Use it:**
   - Message your bot on Telegram
   - Send `/start`
   - Have fun!

---

### 🎸 CONGRATULATIONS, PARTNER!

You done did it! You got yourself a fancy trading bot runnin' in the cloud, controlled from your phone. Your grandpappy would be proud.

Now go tell all your friends that you're a "blockchain developer" or whatever the kids call it these days.

Happy tradin', and may the odds be ever in your favor! 🤠🚀

---

*If this guide helped ya out, give the repo a ⭐ star. It makes us feel all warm and fuzzy inside.*

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
| `/set_trade_amount` | Configure trade size |
| `/set_min_balance` | Set minimum Polygon balance for auto-funding |
| `/set_bridge_amount` | Set amount to bridge when auto-funding |
| `/set_interval` | Set cycle interval |
| `/setup_approvals` | Set up USDC/CTF token approvals for trading |
| `/gas_status` | Check MATIC balance for gas fees |
| `/toggle_onchain` | Toggle 100% onchain trading mode |
| `/help` | Show all commands |

### Security Features

- **Authorized User Only**: Only your `TELEGRAM_CHAT_ID` can control the bot
- **Secure Key Input**: Private keys are deleted from chat history immediately
- **Memory-Only Storage**: Sensitive credentials are stored in memory only, not on disk
- **Dry Run Default**: Bot starts in dry-run mode - must explicitly enable live trading
- **100% Onchain**: All trades execute directly on Polygon - no centralized API dependencies

---

## 📊 How It Works

1. **Data Fetching** – Pulls the last 24 h of 5-minute OHLC candles for Bitcoin (or any CoinGecko asset) via `pycoingecko`.
2. **Up/Down Engine** – A custom moving-average comparator:
   - Computes a 5-period (fast) and 20-period (slow) simple moving average over recent closing prices (configurable via env vars).
   - Returns `"up"` when the current fast MA is above the current slow MA (bullish), `"down"` otherwise (bearish).
   - Returns `"hold"` when there is insufficient data.
3. **Market Discovery** – Queries Polymarket's Gamma API for active, unclosed markets whose `question` contains your search terms (e.g. current date + "btc").
4. **Trade Execution** – Places trades 100% onchain via the CLOB client. L2 API credentials are automatically derived from your wallet private key (no manual API setup needed!). Automatically skips trading when credentials are absent (dry-run mode).
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
| `POLYMARKET_PRIVATE_KEY` | Your Polygon wallet private key (only key needed!) |
| `POLYMARKET_HOST` | CLOB endpoint (default: `https://clob.polymarket.com`) |
| `POLYMARKET_CHAIN_ID` | Polygon chain ID (default: `137`) |
| `POLYGON_RPC_URL` | Polygon RPC endpoint (default: `https://polygon-rpc.com`) |
| `CYCLE_INTERVAL_SECONDS` | Seconds between cycles (default: `300`) |
| `SHORT_WINDOW` | Fast MA period (default: `5`) |
| `LONG_WINDOW` | Slow MA period (default: `20`) |

> **💡 100% Onchain Mode:** Since 2026, you only need the `POLYMARKET_PRIVATE_KEY`! The bot automatically derives all required L2 API credentials from your wallet using `signature_type=0` (EOA mode). No more separate API key, secret, and passphrase needed!

Fund your wallet with USDC on Polygon for trading and a small amount of MATIC for gas fees.

### 3. Run the bot

```bash
# Load .env (if using a shell without automatic dotenv support)
export $(grep -v '^#' .env | xargs)

python updown_bot.py
```

If `POLYMARKET_PRIVATE_KEY` is missing, the bot runs in **dry-run mode** – it logs predictions and matched markets but never submits orders.

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
- **Multi-Signal Engine** – The bot uses a weighted signal combination:
  - MA Crossover (30% weight)
  - RSI (14) with Overbought/Oversold detection (30% weight)
  - MACD (12, 26, 9) (25% weight)
  - Polymarket price deviation (15% weight)
- **Logistic Regression Fallback** – A simple numpy-based LogReg model trained on-the-fly using 7 days of CoinGecko historical data provides additional confirmation signals.
- **Confidence Threshold** – Set minimum confidence (default: 68%) via `/set_confidence_threshold`. Trades are only executed when confidence ≥ threshold.

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