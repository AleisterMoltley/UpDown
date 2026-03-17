"""
UpDown Telegram Bot: A 100% Telegram-controlled prediction trading bot for Polymarket.

This bot allows full control via Telegram commands:
- Set Solana/Polygon wallets dynamically
- Start/stop trading bot
- View balances, predictions, and P&L
- Configure all settings without server access

Security: Only the TELEGRAM_CHAT_ID user can control the bot.

Setup:
    pip install -r requirements.txt
    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
    python telegram_bot.py
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from pycoingecko import CoinGeckoAPI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Load environment variables from .env file
load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# File paths for persistent storage
CONFIG_FILE = Path("bot_config.json")
PNL_FILE = Path("daily_pnl.json")

# Conversation states
(
    AWAITING_SOLANA_KEY,
    AWAITING_POLYGON_KEY,
    AWAITING_API_KEY,
    AWAITING_API_SECRET,
    AWAITING_API_PASSPHRASE,
    AWAITING_TRADE_AMOUNT,
    AWAITING_MIN_BALANCE,
    AWAITING_BRIDGE_AMOUNT,
    AWAITING_CYCLE_INTERVAL,
    AWAITING_MANUAL_TRADE_MARKET,
    AWAITING_MANUAL_TRADE_SIDE,
    AWAITING_MANUAL_TRADE_AMOUNT,
) = range(12)

# Default configuration
DEFAULT_CONFIG = {
    "polymarket_host": "https://clob.polymarket.com",
    "chain_id": 137,
    "cycle_interval_seconds": 300,
    "short_window": 5,
    "long_window": 20,
    "trade_amount": 5.0,
    "crypto_id": "bitcoin",
    "query_terms": ["btc"],
    "solana_private_key": "",
    "polygon_private_key": "",
    "polymarket_api_key": "",
    "polymarket_api_secret": "",
    "polymarket_api_passphrase": "",
    "solana_rpc_url": "https://api.mainnet-beta.solana.com",
    "min_poly_balance_usdc": 20.0,
    "bridge_fund_amount": 50.0,
    "bot_running": False,
    "dry_run": True,
}

# Solana USDC token mint address (mainnet)
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Bridge endpoint
POLYMARKET_BRIDGE_URL = "https://bridge.polymarket.com/deposit"

# Gamma API
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Sensitive keys that should not be saved to disk
SENSITIVE_KEYS = frozenset([
    "solana_private_key",
    "polygon_private_key",
    "polymarket_api_key",
    "polymarket_api_secret",
    "polymarket_api_passphrase",
])

# Global state
bot_config: dict = {}
bot_thread: threading.Thread | None = None
bot_thread_lock = threading.Lock()  # Lock for thread-safe bot start/stop
stop_event = threading.Event()
cg = CoinGeckoAPI()

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load configuration from file or return defaults."""
    global bot_config
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                saved = json.load(f)
                # Merge with defaults to handle new keys
                bot_config = {**DEFAULT_CONFIG, **saved}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not load config: {e}")
            bot_config = DEFAULT_CONFIG.copy()
    else:
        bot_config = DEFAULT_CONFIG.copy()

    # Override with environment variables if set
    if os.environ.get("SOLANA_PRIVATE_KEY"):
        bot_config["solana_private_key"] = os.environ.get("SOLANA_PRIVATE_KEY", "")
    if os.environ.get("POLYMARKET_PRIVATE_KEY"):
        bot_config["polygon_private_key"] = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if os.environ.get("POLYMARKET_API_KEY"):
        bot_config["polymarket_api_key"] = os.environ.get("POLYMARKET_API_KEY", "")
    if os.environ.get("POLYMARKET_API_SECRET"):
        bot_config["polymarket_api_secret"] = os.environ.get("POLYMARKET_API_SECRET", "")
    if os.environ.get("POLYMARKET_API_PASSPHRASE"):
        bot_config["polymarket_api_passphrase"] = os.environ.get("POLYMARKET_API_PASSPHRASE", "")

    return bot_config


def save_config() -> None:
    """Save current configuration to file."""
    try:
        # Don't save sensitive keys to file (keep them in memory only)
        safe_config = {k: v for k, v in bot_config.items() if k not in SENSITIVE_KEYS}
        with open(CONFIG_FILE, "w") as f:
            json.dump(safe_config, f, indent=2)
    except OSError as e:
        logger.error(f"Could not save config: {e}")


def load_pnl() -> dict:
    """Load P&L data from file."""
    if PNL_FILE.exists():
        try:
            with open(PNL_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"daily": {}, "total": 0.0, "trades": []}
    return {"daily": {}, "total": 0.0, "trades": []}


def save_pnl(pnl_data: dict) -> None:
    """Save P&L data to file."""
    try:
        with open(PNL_FILE, "w") as f:
            json.dump(pnl_data, f, indent=2)
    except OSError as e:
        logger.error(f"Could not save P&L: {e}")


def record_trade(amount: float, profit: float = 0.0) -> None:
    """Record a trade in P&L history."""
    pnl = load_pnl()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in pnl["daily"]:
        pnl["daily"][today] = {"trades": 0, "profit": 0.0}
    pnl["daily"][today]["trades"] += 1
    pnl["daily"][today]["profit"] += profit
    pnl["total"] += profit
    pnl["trades"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "amount": amount,
        "profit": profit,
    })
    # Keep only last 100 trades
    pnl["trades"] = pnl["trades"][-100:]
    save_pnl(pnl)


# ---------------------------------------------------------------------------
# Security check
# ---------------------------------------------------------------------------


def is_authorized(update: Update) -> bool:
    """Check if the user is authorized to use the bot."""
    if not TELEGRAM_CHAT_ID:
        return False
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return user_id == TELEGRAM_CHAT_ID


async def unauthorized_response(update: Update) -> None:
    """Send unauthorized access message."""
    await update.message.reply_text(
        "⛔ **Unauthorized Access**\n\n"
        "You are not authorized to control this bot.\n"
        "This bot is configured for a specific user only.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Wallet & Trading Functions
# ---------------------------------------------------------------------------


def get_polygon_address() -> str | None:
    """Derive the 0x Polygon address from polygon_private_key."""
    key = bot_config.get("polygon_private_key", "")
    if not key:
        return None
    try:
        from eth_account import Account
        key = key if key.startswith("0x") else f"0x{key}"
        account = Account.from_key(key)
        return account.address
    except ImportError:
        logger.warning("eth_account not installed")
        return None
    except Exception as e:
        logger.error(f"Error deriving Polygon address: {e}")
        return None


def get_solana_pubkey() -> str | None:
    """Get the public key from Solana private key."""
    key = bot_config.get("solana_private_key", "")
    if not key:
        return None
    try:
        import base58
        from solders.keypair import Keypair
        secret_key_bytes = base58.b58decode(key)
        keypair = Keypair.from_bytes(secret_key_bytes)
        return str(keypair.pubkey())
    except ImportError:
        logger.warning("Solana dependencies not installed")
        return None
    except Exception as e:
        logger.error(f"Error getting Solana pubkey: {e}")
        return None


def get_solana_balance() -> dict:
    """Check SOL and USDC balance on Solana wallet."""
    key = bot_config.get("solana_private_key", "")
    if not key:
        return {}

    try:
        import base58
        from solana.rpc.api import Client as SolanaClient
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey

        secret_key_bytes = base58.b58decode(key)
        keypair = Keypair.from_bytes(secret_key_bytes)
        pubkey = keypair.pubkey()

        client = SolanaClient(bot_config.get("solana_rpc_url", "https://api.mainnet-beta.solana.com"))

        # Get SOL balance
        sol_balance_resp = client.get_balance(pubkey)
        sol_lamports = sol_balance_resp.value if hasattr(sol_balance_resp, "value") else 0
        sol_balance = sol_lamports / 1e9

        # Get USDC balance
        usdc_balance = 0.0
        try:
            usdc_mint = Pubkey.from_string(SOLANA_USDC_MINT)
            token_accounts = client.get_token_accounts_by_owner_json_parsed(
                pubkey, {"mint": usdc_mint}
            )
            if hasattr(token_accounts, "value") and token_accounts.value:
                for account in token_accounts.value:
                    parsed = account.account.data.parsed
                    if parsed and "info" in parsed and "tokenAmount" in parsed["info"]:
                        usdc_balance = float(parsed["info"]["tokenAmount"]["uiAmount"] or 0)
                        break
        except Exception as e:
            logger.warning(f"Could not fetch USDC balance: {e}")

        return {"sol": sol_balance, "usdc": usdc_balance}

    except ImportError as e:
        logger.warning(f"Solana dependencies not installed: {e}")
        return {}
    except Exception as e:
        logger.error(f"Error checking Solana balance: {e}")
        return {}


def get_polygon_balance() -> float:
    """Get Polygon USDC balance via CLOB client."""
    clob = _build_clob_client()
    if clob is None:
        return 0.0
    try:
        balance_info = clob.get_balance()
        if isinstance(balance_info, dict):
            return float(balance_info.get("balance", 0) or 0)
        return float(balance_info or 0)
    except Exception as e:
        logger.error(f"Error fetching Polygon balance: {e}")
        return 0.0


def _build_clob_client():
    """Construct a ClobClient only when credentials are available."""
    key = bot_config.get("polygon_private_key", "")
    api_key = bot_config.get("polymarket_api_key", "")
    api_secret = bot_config.get("polymarket_api_secret", "")
    api_passphrase = bot_config.get("polymarket_api_passphrase", "")

    if not all([key, api_key, api_secret, api_passphrase]):
        return None
    try:
        from clob_client.client import ClobClient
        return ClobClient(
            host=bot_config.get("polymarket_host", "https://clob.polymarket.com"),
            key=api_key,
            secret=api_secret,
            passphrase=api_passphrase,
            chain_id=bot_config.get("chain_id", 137),
            private_key=key,
        )
    except ImportError:
        logger.warning("py-clob-client is not installed")
        return None


# ---------------------------------------------------------------------------
# Prediction Engine
# ---------------------------------------------------------------------------


def fetch_5min_data(crypto_id: str = "bitcoin", max_retries: int = 3) -> list:
    """Fetch the last 24 hours of 5-minute OHLC candles from CoinGecko.

    Args:
        crypto_id: CoinGecko asset ID (e.g., 'bitcoin', 'ethereum').
        max_retries: Number of retry attempts for transient failures.

    Returns:
        List of OHLC candles [[timestamp, open, high, low, close], ...].

    Raises:
        ConnectionError: When unable to connect to CoinGecko API after retries.
        ValueError: When API returns invalid data.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            ohlc = cg.get_coin_ohlc_by_id(id=crypto_id, vs_currency="usd", days="1")
            if ohlc is None or not isinstance(ohlc, list):
                raise ValueError(f"Invalid response from CoinGecko API: {ohlc}")
            return ohlc
        except requests.exceptions.ConnectionError as e:
            last_error = e
            logger.warning(
                f"CoinGecko API connection failed (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                time.sleep(wait_time)
        except requests.exceptions.Timeout as e:
            last_error = e
            logger.warning(
                f"CoinGecko API timeout (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
        except Exception as e:
            # For other errors, don't retry - could be rate limit or invalid request
            logger.error(f"CoinGecko API error: {e}")
            raise

    # All retries exhausted
    error_msg = (
        f"Failed to connect to CoinGecko API after {max_retries} attempts. "
        "This may be caused by: network issues, firewall blocking api.coingecko.com, "
        "or CoinGecko service unavailability."
    )
    logger.error(error_msg)
    raise ConnectionError(error_msg) from last_error


def predict_up_down(closes: list) -> str:
    """Predict the next price direction using a moving-average crossover."""
    short_window = bot_config.get("short_window", 5)
    long_window = bot_config.get("long_window", 20)

    if len(closes) < long_window:
        return "hold"

    ma_short = sum(closes[-short_window:]) / short_window
    ma_long = sum(closes[-long_window:]) / long_window

    return "up" if ma_short > ma_long else "down"


def get_current_prediction() -> dict:
    """Get current prediction with price data.

    Returns:
        Dictionary with prediction info:
        - prediction: 'up', 'down', 'hold', 'error', or 'unavailable'
        - price: Current price (0 on error)
        - candles: Number of candles fetched
        - crypto: Crypto ID
        - error: Error message (only present on failure)
    """
    try:
        crypto_id = bot_config.get("crypto_id", "bitcoin")
        ohlc = fetch_5min_data(crypto_id=crypto_id)
        closes = [candle[4] for candle in ohlc]
        prediction = predict_up_down(closes)
        current_price = closes[-1] if closes else 0
        return {
            "prediction": prediction,
            "price": current_price,
            "candles": len(closes),
            "crypto": crypto_id,
        }
    except ConnectionError as e:
        logger.error(f"Connection error getting prediction: {e}")
        return {
            "prediction": "unavailable",
            "price": 0,
            "candles": 0,
            "crypto": bot_config.get("crypto_id", "unknown"),
            "error": "Unable to connect to CoinGecko API. Check network/firewall.",
        }
    except Exception as e:
        logger.error(f"Error getting prediction: {e}")
        return {
            "prediction": "error",
            "price": 0,
            "candles": 0,
            "crypto": "unknown",
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Market Discovery & Trading
# ---------------------------------------------------------------------------


def find_relevant_markets(query_terms: list | None = None) -> list:
    """Search Polymarket's public Gamma API for active markets."""
    if query_terms is None:
        query_terms = bot_config.get("query_terms", ["BTC"])
        today = datetime.now(timezone.utc).strftime("%B %d").lower()
        query_terms = query_terms + [today]

    url = f"{GAMMA_API_BASE}/markets"
    params = {"active": "true", "closed": "false", "limit": 100}

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Error fetching markets: {e}")
        return []

    markets = response.json()
    relevant = [
        m for m in markets
        if all(term.lower() in m.get("question", "").lower() for term in query_terms)
    ]
    return relevant


def place_trade(market: dict, outcome: str = "yes", amount: float = 10.0) -> dict:
    """Place a limit order on a Polymarket market."""
    clob = _build_clob_client()
    if clob is None:
        return {"success": False, "error": "CLOB client not initialized"}

    tokens = market.get("tokens", [])
    token_id = None
    for token in tokens:
        if token.get("outcome", "").lower() == outcome.lower():
            token_id = token.get("token_id") or token.get("id")
            break

    if token_id is None:
        return {"success": False, "error": f"Could not find {outcome} token"}

    try:
        mid = clob.get_midpoint(token_id)
        price = float(mid) if mid else 0.5

        order = clob.create_order(
            token_id=token_id,
            price=price,
            side="buy",
            size=amount,
        )
        signed_order = clob.sign_order(order)
        resp = clob.post_order(signed_order)
        record_trade(amount)
        return {"success": True, "response": resp, "price": price}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Solana Bridge
# ---------------------------------------------------------------------------


def get_solana_deposit_address(evm_address: str) -> str | None:
    """Get the Solana deposit address from Polymarket bridge."""
    try:
        response = requests.post(
            POLYMARKET_BRIDGE_URL,
            json={"evmAddress": evm_address},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("depositAddress") or data.get("solanaAddress")
    except Exception as e:
        logger.error(f"Error fetching deposit address: {e}")
        return None


def send_to_polymarket_bridge(deposit_address: str, amount_usdc: float) -> dict:
    """Transfer USDC to the Polymarket bridge deposit address."""
    key = bot_config.get("solana_private_key", "")
    if not key:
        return {"success": False, "error": "SOLANA_PRIVATE_KEY not configured"}

    try:
        import base58
        from solana.rpc.api import Client as SolanaClient
        from solana.transaction import Transaction
        from solders.compute_budget import set_compute_unit_price
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from spl.token.constants import TOKEN_PROGRAM_ID
        from spl.token.instructions import TransferCheckedParams, transfer_checked

        secret_key_bytes = base58.b58decode(key)
        keypair = Keypair.from_bytes(secret_key_bytes)
        pubkey = keypair.pubkey()
        dest_pubkey = Pubkey.from_string(deposit_address)

        client = SolanaClient(bot_config.get("solana_rpc_url", "https://api.mainnet-beta.solana.com"))

        # Get balances
        balances = get_solana_balance()
        usdc_balance = balances.get("usdc", 0)

        if usdc_balance < amount_usdc:
            return {"success": False, "error": f"Insufficient USDC balance: ${usdc_balance:.2f}"}

        usdc_mint = Pubkey.from_string(SOLANA_USDC_MINT)

        # Find source token account
        token_accounts = client.get_token_accounts_by_owner_json_parsed(
            pubkey, {"mint": usdc_mint}
        )

        if not hasattr(token_accounts, "value") or not token_accounts.value:
            return {"success": False, "error": "No USDC token account found"}

        source_token_account = Pubkey.from_string(str(token_accounts.value[0].pubkey))

        # Get destination token account
        dest_token_accounts = client.get_token_accounts_by_owner_json_parsed(
            dest_pubkey, {"mint": usdc_mint}
        )

        if hasattr(dest_token_accounts, "value") and dest_token_accounts.value:
            dest_token_account = Pubkey.from_string(str(dest_token_accounts.value[0].pubkey))
        else:
            from spl.token.instructions import get_associated_token_address
            dest_token_account = get_associated_token_address(dest_pubkey, usdc_mint)

        amount_units = int(amount_usdc * 1_000_000)
        recent_blockhash = client.get_latest_blockhash().value.blockhash

        transfer_ix = transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=source_token_account,
                mint=usdc_mint,
                dest=dest_token_account,
                owner=pubkey,
                amount=amount_units,
                decimals=6,
            )
        )

        priority_fee_ix = set_compute_unit_price(1000)

        txn = Transaction()
        txn.add(priority_fee_ix)
        txn.add(transfer_ix)
        txn.recent_blockhash = recent_blockhash
        txn.fee_payer = pubkey

        txn.sign(keypair)
        result = client.send_transaction(txn, keypair)

        if hasattr(result, "value"):
            return {"success": True, "tx": str(result.value)}
        return {"success": True, "tx": str(result)}

    except ImportError as e:
        return {"success": False, "error": f"Solana dependencies not installed: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def check_and_fund_polygon() -> dict:
    """Check Polygon balance and trigger bridge funding if below threshold."""
    if bot_config.get("dry_run", True):
        return {"funded": False, "reason": "dry_run"}

    if not bot_config.get("solana_private_key"):
        return {"funded": False, "reason": "solana_not_configured"}

    current_balance = get_polygon_balance()
    min_balance = bot_config.get("min_poly_balance_usdc", 20.0)

    if current_balance >= min_balance:
        return {"funded": False, "reason": "balance_ok", "balance": current_balance}

    polygon_address = get_polygon_address()
    if not polygon_address:
        return {"funded": False, "reason": "polygon_address_error"}

    deposit_address = get_solana_deposit_address(polygon_address)
    if not deposit_address:
        return {"funded": False, "reason": "deposit_address_error"}

    bridge_amount = bot_config.get("bridge_fund_amount", 50.0)
    result = send_to_polymarket_bridge(deposit_address, bridge_amount)

    if result.get("success"):
        return {"funded": True, "amount": bridge_amount, "tx": result.get("tx")}
    return {"funded": False, "reason": result.get("error")}


# ---------------------------------------------------------------------------
# Bot Loop
# ---------------------------------------------------------------------------


def bot_loop(send_notification):
    """Main bot trading loop (runs in separate thread)."""
    global stop_event

    logger.info("Bot loop started")

    while not stop_event.is_set():
        try:
            # Check and fund Polygon if needed
            fund_result = check_and_fund_polygon()
            if fund_result.get("funded"):
                send_notification(
                    f"🌉 **Auto-funded Polygon**\n"
                    f"Amount: ${fund_result.get('amount', 0):.2f}\n"
                    f"Tx: `{fund_result.get('tx', 'N/A')}`"
                )

            # Get prediction
            pred = get_current_prediction()
            prediction = pred.get("prediction", "hold")

            if prediction == "hold":
                logger.info("Not enough data - skipping trade")
                wait_with_check(bot_config.get("cycle_interval_seconds", 300))
                continue

            # Find markets
            markets = find_relevant_markets()

            if not markets:
                logger.info("No relevant markets found")
                wait_with_check(bot_config.get("cycle_interval_seconds", 300))
                continue

            # Execute trades
            for market in markets:
                if bot_config.get("dry_run", True):
                    outcome = "YES" if prediction == "up" else "NO"
                    logger.info(f"[Dry run] Would buy {outcome} on {market.get('question')}")
                    send_notification(
                        f"📊 **Dry Run Trade**\n"
                        f"Market: {market.get('question', 'N/A')[:50]}...\n"
                        f"Prediction: {prediction.upper()}\n"
                        f"Would buy: {outcome}"
                    )
                else:
                    outcome = "yes" if prediction == "up" else "no"
                    trade_amount = bot_config.get("trade_amount", 5.0)
                    result = place_trade(market, outcome=outcome, amount=trade_amount)

                    if result.get("success"):
                        send_notification(
                            f"✅ **Trade Executed**\n"
                            f"Market: {market.get('question', 'N/A')[:50]}...\n"
                            f"Side: {outcome.upper()}\n"
                            f"Amount: ${trade_amount}\n"
                            f"Price: {result.get('price', 'N/A')}"
                        )
                    else:
                        send_notification(
                            f"❌ **Trade Failed**\n"
                            f"Market: {market.get('question', 'N/A')[:50]}...\n"
                            f"Error: {result.get('error', 'Unknown')}"
                        )

            # Wait for next cycle
            wait_with_check(bot_config.get("cycle_interval_seconds", 300))

        except Exception as e:
            logger.error(f"Error in bot loop: {e}")
            send_notification(f"⚠️ **Bot Error**\n{str(e)[:200]}")
            wait_with_check(60)  # Wait 1 min on error

    logger.info("Bot loop stopped")


def wait_with_check(seconds: int) -> None:
    """Wait for specified seconds while checking stop_event."""
    for _ in range(seconds):
        if stop_event.is_set():
            break
        time.sleep(1)


# ---------------------------------------------------------------------------
# Telegram Command Handlers
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    keyboard = [
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("💰 Balance", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("🔮 Predict", callback_data="predict"),
            InlineKeyboardButton("📈 Markets", callback_data="markets"),
        ],
        [
            InlineKeyboardButton("▶️ Start Bot", callback_data="start_bot"),
            InlineKeyboardButton("⏹️ Stop Bot", callback_data="stop_bot"),
        ],
        [
            InlineKeyboardButton("⚙️ Config", callback_data="config"),
            InlineKeyboardButton("🔑 Wallets", callback_data="wallets"),
        ],
        [
            InlineKeyboardButton("📜 P&L History", callback_data="pnl"),
            InlineKeyboardButton("❓ Help", callback_data="help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    status = "🟢 Running" if bot_config.get("bot_running", False) else "🔴 Stopped"
    mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live Trading"

    await update.message.reply_text(
        f"🎰 **UpDown Trading Bot**\n\n"
        f"Status: {status}\n"
        f"Mode: {mode}\n\n"
        f"Control your Polymarket prediction bot entirely via Telegram.\n\n"
        f"Select an option below:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    help_text = """
🎰 **UpDown Bot Commands**

**Bot Control:**
/start - Main menu
/status - View bot status
/start_bot - Start trading loop
/stop_bot - Stop trading loop

**Wallet Setup:**
/set_solana_key - Set Solana private key
/set_polygon_key - Set Polygon private key
/set_polymarket_api - Set Polymarket API credentials

**Trading:**
/balance - View all balances
/predict - Get current prediction
/markets - Find relevant markets
/trade - Execute manual trade
/bridge - Manual Solana→Polygon bridge

**Configuration:**
/config - View/edit settings
/set_trade_amount - Set trade size
/set_min_balance - Set minimum Polygon balance
/set_bridge_amount - Set bridge amount
/set_interval - Set cycle interval
/toggle_dry_run - Toggle dry run mode

**Info:**
/pnl - View P&L history
/help - This help message

⚠️ **Security Note:**
- Private keys are stored in memory only
- Never share your bot token or keys
- Use a dedicated trading wallet
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    # Get balances
    sol_bal = get_solana_balance()
    poly_bal = get_polygon_balance()

    # Get prediction
    pred = get_current_prediction()

    # Get P&L
    pnl = load_pnl()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = pnl.get("daily", {}).get(today, {"trades": 0, "profit": 0.0})

    status = "🟢 Running" if bot_config.get("bot_running", False) else "🔴 Stopped"
    mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live Trading"

    sol_configured = "✅" if bot_config.get("solana_private_key") else "❌"
    poly_configured = "✅" if bot_config.get("polygon_private_key") else "❌"
    api_configured = "✅" if bot_config.get("polymarket_api_key") else "❌"

    text = f"""
📊 **Bot Status**

**State:** {status}
**Mode:** {mode}

**Wallets:**
Solana: {sol_configured} {f"${sol_bal.get('usdc', 0):.2f} USDC | {sol_bal.get('sol', 0):.4f} SOL" if sol_bal else "Not configured"}
Polygon: {poly_configured} {f"${poly_bal:.2f} USDC" if poly_bal else "Not configured"}
API: {api_configured}

**Current Prediction:**
Asset: {pred.get('crypto', 'N/A').upper()}
Price: ${pred.get('price', 0):,.2f}
Direction: {pred.get('prediction', 'N/A').upper()}

**Today's P&L:**
Trades: {daily_pnl.get('trades', 0)}
Profit: ${daily_pnl.get('profit', 0):.2f}
Total: ${pnl.get('total', 0):.2f}

**Config:**
Trade Amount: ${bot_config.get('trade_amount', 5.0)}
Cycle: {bot_config.get('cycle_interval_seconds', 300)}s
MA: {bot_config.get('short_window', 5)}/{bot_config.get('long_window', 20)}
"""
    await update.message.reply_text(text, parse_mode="Markdown")


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /balance command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    await update.message.reply_text("⏳ Fetching balances...")

    sol_bal = get_solana_balance()
    poly_bal = get_polygon_balance()

    sol_addr = get_solana_pubkey()
    poly_addr = get_polygon_address()

    # Format addresses safely (only truncate if we have a real address)
    sol_addr_display = f"`{sol_addr[:20]}...`" if sol_addr else "Not configured"
    poly_addr_display = f"`{poly_addr[:20]}...`" if poly_addr else "Not configured"

    text = f"""
💰 **Wallet Balances**

**Solana Wallet:**
Address: {sol_addr_display}
USDC: ${sol_bal.get('usdc', 0):.2f}
SOL: {sol_bal.get('sol', 0):.4f}

**Polygon Wallet:**
Address: {poly_addr_display}
USDC: ${poly_bal:.2f}

**Auto-Fund Settings:**
Min Balance: ${bot_config.get('min_poly_balance_usdc', 20.0)}
Bridge Amount: ${bot_config.get('bridge_fund_amount', 50.0)}
"""
    await update.message.reply_text(text, parse_mode="Markdown")


async def predict_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /predict command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    await update.message.reply_text("🔮 Analyzing market data...")

    pred = get_current_prediction()

    emoji = "📈" if pred.get("prediction") == "up" else "📉" if pred.get("prediction") == "down" else "⏸️"

    text = f"""
{emoji} **Price Prediction**

**Asset:** {pred.get('crypto', 'N/A').upper()}
**Current Price:** ${pred.get('price', 0):,.2f}
**Prediction:** {pred.get('prediction', 'N/A').upper()}
**Data Points:** {pred.get('candles', 0)} candles

**Strategy:**
MA Crossover ({bot_config.get('short_window', 5)}/{bot_config.get('long_window', 20)})
"""
    await update.message.reply_text(text, parse_mode="Markdown")


async def markets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /markets command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    await update.message.reply_text("🔍 Searching for markets...")

    markets = find_relevant_markets()

    if not markets:
        await update.message.reply_text("No relevant markets found for today.")
        return

    text = f"📈 **Found {len(markets)} Market(s)**\n\n"
    for i, market in enumerate(markets[:5], 1):
        text += f"{i}. {market.get('question', 'N/A')[:80]}\n"
        text += f"   ID: `{market.get('id', 'N/A')[:20]}...`\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pnl command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    pnl = load_pnl()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    text = f"""
📜 **P&L History**

**Total Profit:** ${pnl.get('total', 0):.2f}

**Last 7 Days:**
"""
    daily = pnl.get("daily", {})
    for i in range(7):
        date = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        day_data = daily.get(date, {"trades": 0, "profit": 0.0})
        marker = "📍" if date == today else "  "
        text += f"{marker} {date}: {day_data.get('trades', 0)} trades, ${day_data.get('profit', 0):.2f}\n"

    text += f"\n**Recent Trades:** {len(pnl.get('trades', []))}"

    await update.message.reply_text(text, parse_mode="Markdown")


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /config command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    keyboard = [
        [
            InlineKeyboardButton("💵 Trade Amount", callback_data="cfg_trade_amount"),
            InlineKeyboardButton("⏱️ Interval", callback_data="cfg_interval"),
        ],
        [
            InlineKeyboardButton("💰 Min Balance", callback_data="cfg_min_balance"),
            InlineKeyboardButton("🌉 Bridge Amount", callback_data="cfg_bridge_amount"),
        ],
        [
            InlineKeyboardButton("🧪 Toggle Dry Run", callback_data="toggle_dry_run"),
        ],
        [InlineKeyboardButton("« Back", callback_data="back_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live Trading"

    text = f"""
⚙️ **Bot Configuration**

**Trading:**
Mode: {mode}
Trade Amount: ${bot_config.get('trade_amount', 5.0)}
Cycle Interval: {bot_config.get('cycle_interval_seconds', 300)}s
Asset: {bot_config.get('crypto_id', 'bitcoin')}

**Strategy:**
Short MA: {bot_config.get('short_window', 5)}
Long MA: {bot_config.get('long_window', 20)}

**Auto-Fund:**
Min Polygon Balance: ${bot_config.get('min_poly_balance_usdc', 20.0)}
Bridge Amount: ${bot_config.get('bridge_fund_amount', 50.0)}

Select a setting to modify:
"""
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Wallet Setup Conversations
# ---------------------------------------------------------------------------


async def set_solana_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting Solana private key."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        "🔐 **Set Solana Private Key**\n\n"
        "Send your base58-encoded Solana private key.\n\n"
        "⚠️ **Security:**\n"
        "- Key is stored in memory only\n"
        "- Key is NOT saved to disk\n"
        "- Use a dedicated trading wallet\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_SOLANA_KEY


async def set_solana_key_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate Solana private key."""
    key = update.message.text.strip()

    # Delete the message containing the key for security
    try:
        await update.message.delete()
    except Exception:
        pass

    # Validate key
    try:
        import base58
        from solders.keypair import Keypair
        secret_key_bytes = base58.b58decode(key)
        keypair = Keypair.from_bytes(secret_key_bytes)
        pubkey = str(keypair.pubkey())

        bot_config["solana_private_key"] = key
        save_config()

        await update.message.reply_text(
            f"✅ **Solana Key Set**\n\n"
            f"Public Address:\n`{pubkey}`\n\n"
            f"Your key has been stored securely in memory.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Invalid Key**\n\n"
            f"Error: {str(e)[:100]}\n\n"
            f"Please provide a valid base58-encoded Solana private key.",
            parse_mode="Markdown",
        )
        return AWAITING_SOLANA_KEY

    return ConversationHandler.END


async def set_polygon_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting Polygon private key."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        "🔐 **Set Polygon Private Key**\n\n"
        "Send your hex-encoded Polygon/Ethereum private key.\n"
        "(With or without 0x prefix)\n\n"
        "⚠️ **Security:**\n"
        "- Key is stored in memory only\n"
        "- Key is NOT saved to disk\n"
        "- Use a dedicated trading wallet\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_POLYGON_KEY


async def set_polygon_key_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate Polygon private key."""
    key = update.message.text.strip()

    # Delete the message containing the key for security
    try:
        await update.message.delete()
    except Exception:
        pass

    # Validate key
    try:
        from eth_account import Account
        key_with_prefix = key if key.startswith("0x") else f"0x{key}"
        account = Account.from_key(key_with_prefix)
        address = account.address

        bot_config["polygon_private_key"] = key
        save_config()

        await update.message.reply_text(
            f"✅ **Polygon Key Set**\n\n"
            f"Address:\n`{address}`\n\n"
            f"Your key has been stored securely in memory.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Invalid Key**\n\n"
            f"Error: {str(e)[:100]}\n\n"
            f"Please provide a valid hex-encoded private key.",
            parse_mode="Markdown",
        )
        return AWAITING_POLYGON_KEY

    return ConversationHandler.END


async def set_api_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting Polymarket API credentials."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        "🔐 **Set Polymarket API Credentials**\n\n"
        "Step 1/3: Send your **API Key**\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_API_KEY


async def set_api_key_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive API key."""
    key = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    context.user_data["api_key"] = key

    await update.message.reply_text(
        "✅ API Key received.\n\n"
        "Step 2/3: Now send your **API Secret**",
        parse_mode="Markdown",
    )
    return AWAITING_API_SECRET


async def set_api_secret_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive API secret."""
    secret = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    context.user_data["api_secret"] = secret

    await update.message.reply_text(
        "✅ API Secret received.\n\n"
        "Step 3/3: Now send your **API Passphrase**",
        parse_mode="Markdown",
    )
    return AWAITING_API_PASSPHRASE


async def set_api_passphrase_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive API passphrase and save all credentials."""
    passphrase = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    bot_config["polymarket_api_key"] = context.user_data.get("api_key", "")
    bot_config["polymarket_api_secret"] = context.user_data.get("api_secret", "")
    bot_config["polymarket_api_passphrase"] = passphrase
    save_config()

    # Clear user data
    context.user_data.clear()

    await update.message.reply_text(
        "✅ **Polymarket API Credentials Set**\n\n"
        "All credentials have been stored securely in memory.\n\n"
        "You can now enable live trading with /toggle_dry_run",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current conversation."""
    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Config Setting Conversations
# ---------------------------------------------------------------------------


async def set_trade_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting trade amount."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        f"💵 **Set Trade Amount**\n\n"
        f"Current: ${bot_config.get('trade_amount', 5.0)}\n\n"
        f"Send the new trade amount in USDC (e.g., 10.0)\n\n"
        f"Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_TRADE_AMOUNT


async def set_trade_amount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive trade amount."""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError("Amount must be positive")

        bot_config["trade_amount"] = amount
        save_config()

        await update.message.reply_text(
            f"✅ Trade amount set to ${amount:.2f}",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Invalid amount: {e}\nPlease enter a positive number.",
        )
        return AWAITING_TRADE_AMOUNT

    return ConversationHandler.END


async def set_min_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting minimum balance."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        f"💰 **Set Minimum Polygon Balance**\n\n"
        f"Current: ${bot_config.get('min_poly_balance_usdc', 20.0)}\n\n"
        f"When balance falls below this, auto-funding is triggered.\n"
        f"Send the new minimum balance in USDC.\n\n"
        f"Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_MIN_BALANCE


async def set_min_balance_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive minimum balance."""
    try:
        amount = float(update.message.text.strip())
        if amount < 0:
            raise ValueError("Amount cannot be negative")

        bot_config["min_poly_balance_usdc"] = amount
        save_config()

        await update.message.reply_text(
            f"✅ Minimum balance set to ${amount:.2f}",
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid amount: {e}")
        return AWAITING_MIN_BALANCE

    return ConversationHandler.END


async def set_bridge_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting bridge amount."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        f"🌉 **Set Bridge Amount**\n\n"
        f"Current: ${bot_config.get('bridge_fund_amount', 50.0)}\n\n"
        f"Amount to bridge from Solana when auto-funding.\n"
        f"Send the new bridge amount in USDC.\n\n"
        f"Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_BRIDGE_AMOUNT


async def set_bridge_amount_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive bridge amount."""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError("Amount must be positive")

        bot_config["bridge_fund_amount"] = amount
        save_config()

        await update.message.reply_text(
            f"✅ Bridge amount set to ${amount:.2f}",
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid amount: {e}")
        return AWAITING_BRIDGE_AMOUNT

    return ConversationHandler.END


async def set_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting cycle interval."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        f"⏱️ **Set Cycle Interval**\n\n"
        f"Current: {bot_config.get('cycle_interval_seconds', 300)} seconds\n\n"
        f"Time between trading cycles.\n"
        f"Send the new interval in seconds (min 60).\n\n"
        f"Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_CYCLE_INTERVAL


async def set_interval_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive cycle interval."""
    try:
        interval = int(update.message.text.strip())
        if interval < 60:
            raise ValueError("Interval must be at least 60 seconds")

        bot_config["cycle_interval_seconds"] = interval
        save_config()

        await update.message.reply_text(
            f"✅ Cycle interval set to {interval} seconds",
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid interval: {e}")
        return AWAITING_CYCLE_INTERVAL

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Bot Control Commands
# ---------------------------------------------------------------------------


async def start_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start_bot command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    global bot_thread

    with bot_thread_lock:
        if bot_config.get("bot_running", False):
            await update.message.reply_text("⚠️ Bot is already running!")
            return

        # Get the current event loop to schedule notifications from the bot thread
        loop = asyncio.get_event_loop()
        bot_instance = context.bot

        def send_notification(text: str):
            """Send notification from bot thread to Telegram (thread-safe)."""
            async def _send():
                try:
                    await bot_instance.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=text,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error(f"Failed to send notification: {e}")

            # Schedule coroutine on the main event loop (thread-safe)
            asyncio.run_coroutine_threadsafe(_send(), loop)

        stop_event.clear()
        bot_config["bot_running"] = True
        save_config()

        bot_thread = threading.Thread(target=bot_loop, args=(send_notification,), daemon=True)
        bot_thread.start()

    mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live Trading"

    await update.message.reply_text(
        f"▶️ **Bot Started**\n\n"
        f"Mode: {mode}\n"
        f"Interval: {bot_config.get('cycle_interval_seconds', 300)}s\n"
        f"Trade Amount: ${bot_config.get('trade_amount', 5.0)}\n\n"
        f"Use /stop_bot to stop.",
        parse_mode="Markdown",
    )


async def stop_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop_bot command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    with bot_thread_lock:
        if not bot_config.get("bot_running", False):
            await update.message.reply_text("⚠️ Bot is not running!")
            return

        stop_event.set()
        bot_config["bot_running"] = False
        save_config()

    await update.message.reply_text(
        "⏹️ **Bot Stopped**\n\n"
        "Trading loop has been stopped.\n"
        "Use /start_bot to restart.",
        parse_mode="Markdown",
    )


async def toggle_dry_run_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /toggle_dry_run command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    current = bot_config.get("dry_run", True)

    # Check if we have credentials to enable live trading
    if current:  # Trying to enable live trading
        if not bot_config.get("polygon_private_key"):
            await update.message.reply_text(
                "❌ Cannot enable live trading.\n\n"
                "Please set your Polygon private key first with /set_polygon_key",
            )
            return
        if not bot_config.get("polymarket_api_key"):
            await update.message.reply_text(
                "❌ Cannot enable live trading.\n\n"
                "Please set your Polymarket API credentials first with /set_polymarket_api",
            )
            return

    bot_config["dry_run"] = not current
    save_config()

    new_mode = "🧪 Dry Run" if bot_config["dry_run"] else "💰 Live Trading"

    await update.message.reply_text(
        f"✅ Mode changed to: {new_mode}\n\n"
        f"{'No real trades will be executed.' if bot_config['dry_run'] else '⚠️ Real trades will be executed!'}",
        parse_mode="Markdown",
    )


async def bridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /bridge command for manual bridging."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    if not bot_config.get("solana_private_key"):
        await update.message.reply_text("❌ Solana wallet not configured. Use /set_solana_key first.")
        return

    if not bot_config.get("polygon_private_key"):
        await update.message.reply_text("❌ Polygon wallet not configured. Use /set_polygon_key first.")
        return

    await update.message.reply_text("🌉 Initiating Solana → Polygon bridge...")

    polygon_address = get_polygon_address()
    if not polygon_address:
        await update.message.reply_text("❌ Could not derive Polygon address.")
        return

    deposit_address = get_solana_deposit_address(polygon_address)
    if not deposit_address:
        await update.message.reply_text("❌ Could not get bridge deposit address.")
        return

    bridge_amount = bot_config.get("bridge_fund_amount", 50.0)
    result = send_to_polymarket_bridge(deposit_address, bridge_amount)

    if result.get("success"):
        await update.message.reply_text(
            f"✅ **Bridge Transfer Initiated**\n\n"
            f"Amount: ${bridge_amount:.2f} USDC\n"
            f"Tx: `{result.get('tx', 'N/A')}`\n\n"
            f"⏳ Bridge may take a few minutes to complete.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ **Bridge Failed**\n\n"
            f"Error: {result.get('error', 'Unknown')}",
            parse_mode="Markdown",
        )


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /trade command for manual trading."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    if bot_config.get("dry_run", True):
        await update.message.reply_text(
            "⚠️ Bot is in dry run mode.\n"
            "Use /toggle_dry_run to enable live trading first.",
        )
        return

    # Find markets first
    markets = find_relevant_markets()

    if not markets:
        await update.message.reply_text("No relevant markets found for today.")
        return

    # Get current prediction
    pred = get_current_prediction()
    prediction = pred.get("prediction", "hold")
    outcome = "YES" if prediction == "up" else "NO"

    keyboard = [
        [
            InlineKeyboardButton("Buy YES", callback_data="trade_yes"),
            InlineKeyboardButton("Buy NO", callback_data="trade_no"),
        ],
        [InlineKeyboardButton("Cancel", callback_data="trade_cancel")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    context.user_data["trade_market"] = markets[0]

    await update.message.reply_text(
        f"📈 **Manual Trade**\n\n"
        f"Market: {markets[0].get('question', 'N/A')[:80]}\n"
        f"Prediction suggests: {outcome}\n"
        f"Amount: ${bot_config.get('trade_amount', 5.0)}\n\n"
        f"Select your trade:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Callback Query Handler
# ---------------------------------------------------------------------------


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks."""
    query = update.callback_query
    await query.answer()

    if not is_authorized(update):
        await query.edit_message_text("⛔ Unauthorized")
        return

    data = query.data

    if data == "status":
        await query.edit_message_text("Loading status...")
        # Simulate status command
        sol_bal = get_solana_balance()
        poly_bal = get_polygon_balance()
        pred = get_current_prediction()
        pnl = load_pnl()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = pnl.get("daily", {}).get(today, {"trades": 0, "profit": 0.0})
        status = "🟢 Running" if bot_config.get("bot_running", False) else "🔴 Stopped"
        mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live"

        text = (
            f"📊 **Status**\n\n"
            f"State: {status} | Mode: {mode}\n"
            f"Solana: ${sol_bal.get('usdc', 0):.2f} USDC\n"
            f"Polygon: ${poly_bal:.2f} USDC\n"
            f"Prediction: {pred.get('prediction', 'N/A').upper()}\n"
            f"Today: {daily_pnl.get('trades', 0)} trades, ${daily_pnl.get('profit', 0):.2f}"
        )

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "balance":
        await query.edit_message_text("💰 Fetching balances...")
        sol_bal = get_solana_balance()
        poly_bal = get_polygon_balance()

        text = (
            f"💰 **Balances**\n\n"
            f"**Solana:** ${sol_bal.get('usdc', 0):.2f} USDC | {sol_bal.get('sol', 0):.4f} SOL\n"
            f"**Polygon:** ${poly_bal:.2f} USDC"
        )

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "predict":
        await query.edit_message_text("🔮 Analyzing...")
        pred = get_current_prediction()
        emoji = "📈" if pred.get("prediction") == "up" else "📉" if pred.get("prediction") == "down" else "⏸️"

        text = (
            f"{emoji} **Prediction**\n\n"
            f"Asset: {pred.get('crypto', 'N/A').upper()}\n"
            f"Price: ${pred.get('price', 0):,.2f}\n"
            f"Direction: **{pred.get('prediction', 'N/A').upper()}**"
        )

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "markets":
        await query.edit_message_text("🔍 Searching markets...")
        markets = find_relevant_markets()

        if not markets:
            text = "No relevant markets found."
        else:
            text = f"📈 **{len(markets)} Market(s)**\n\n"
            for i, m in enumerate(markets[:3], 1):
                text += f"{i}. {m.get('question', 'N/A')[:60]}...\n"

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "start_bot":
        global bot_thread
        with bot_thread_lock:
            if bot_config.get("bot_running", False):
                await query.edit_message_text("⚠️ Bot is already running!")
                return

            # Get the current event loop to schedule notifications from the bot thread
            loop = asyncio.get_event_loop()
            bot_instance = context.bot

            def send_notification(text: str):
                """Send notification from bot thread to Telegram (thread-safe)."""
                async def _send():
                    try:
                        await bot_instance.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=text,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.error(f"Failed to send notification: {e}")

                asyncio.run_coroutine_threadsafe(_send(), loop)

            stop_event.clear()
            bot_config["bot_running"] = True
            save_config()

            bot_thread = threading.Thread(target=bot_loop, args=(send_notification,), daemon=True)
            bot_thread.start()

        mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live"
        text = f"▶️ **Bot Started**\n\nMode: {mode}\nUse /stop_bot to stop."

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "stop_bot":
        with bot_thread_lock:
            stop_event.set()
            bot_config["bot_running"] = False
            save_config()

        text = "⏹️ **Bot Stopped**"

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "config":
        mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live"
        text = (
            f"⚙️ **Config**\n\n"
            f"Mode: {mode}\n"
            f"Trade: ${bot_config.get('trade_amount', 5.0)}\n"
            f"Interval: {bot_config.get('cycle_interval_seconds', 300)}s"
        )

        keyboard = [
            [InlineKeyboardButton("🧪 Toggle Dry Run", callback_data="toggle_dry_run")],
            [InlineKeyboardButton("« Back", callback_data="back_main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "wallets":
        sol_set = "✅" if bot_config.get("solana_private_key") else "❌"
        poly_set = "✅" if bot_config.get("polygon_private_key") else "❌"
        api_set = "✅" if bot_config.get("polymarket_api_key") else "❌"

        text = (
            f"🔑 **Wallets**\n\n"
            f"Solana Key: {sol_set}\n"
            f"Polygon Key: {poly_set}\n"
            f"Polymarket API: {api_set}\n\n"
            f"Use commands to set:\n"
            f"/set_solana_key\n"
            f"/set_polygon_key\n"
            f"/set_polymarket_api"
        )

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "pnl":
        pnl = load_pnl()
        text = f"📜 **P&L**\n\nTotal: ${pnl.get('total', 0):.2f}"

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "help":
        text = "❓ **Help**\n\nUse /help for full command list."

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "toggle_dry_run":
        current = bot_config.get("dry_run", True)

        if current:  # Trying to enable live trading
            if not bot_config.get("polygon_private_key") or not bot_config.get("polymarket_api_key"):
                await query.edit_message_text(
                    "❌ Cannot enable live trading.\n\n"
                    "Set wallet and API credentials first.",
                )
                return

        bot_config["dry_run"] = not current
        save_config()

        new_mode = "🧪 Dry Run" if bot_config["dry_run"] else "💰 Live Trading"
        text = f"✅ Mode: {new_mode}"

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "trade_yes":
        market = context.user_data.get("trade_market")
        if market:
            amount = bot_config.get("trade_amount", 5.0)
            result = place_trade(market, "yes", amount)
            if result.get("success"):
                text = f"✅ Bought YES for ${amount}"
            else:
                text = f"❌ Trade failed: {result.get('error')}"
        else:
            text = "❌ No market selected"
        await query.edit_message_text(text)

    elif data == "trade_no":
        market = context.user_data.get("trade_market")
        if market:
            amount = bot_config.get("trade_amount", 5.0)
            result = place_trade(market, "no", amount)
            if result.get("success"):
                text = f"✅ Bought NO for ${amount}"
            else:
                text = f"❌ Trade failed: {result.get('error')}"
        else:
            text = "❌ No market selected"
        await query.edit_message_text(text)

    elif data == "trade_cancel":
        context.user_data.pop("trade_market", None)
        await query.edit_message_text("❌ Trade cancelled")

    elif data == "back_main":
        # Return to main menu
        keyboard = [
            [
                InlineKeyboardButton("📊 Status", callback_data="status"),
                InlineKeyboardButton("💰 Balance", callback_data="balance"),
            ],
            [
                InlineKeyboardButton("🔮 Predict", callback_data="predict"),
                InlineKeyboardButton("📈 Markets", callback_data="markets"),
            ],
            [
                InlineKeyboardButton("▶️ Start Bot", callback_data="start_bot"),
                InlineKeyboardButton("⏹️ Stop Bot", callback_data="stop_bot"),
            ],
            [
                InlineKeyboardButton("⚙️ Config", callback_data="config"),
                InlineKeyboardButton("🔑 Wallets", callback_data="wallets"),
            ],
            [
                InlineKeyboardButton("📜 P&L", callback_data="pnl"),
                InlineKeyboardButton("❓ Help", callback_data="help"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        status = "🟢 Running" if bot_config.get("bot_running", False) else "🔴 Stopped"
        mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live"

        await query.edit_message_text(
            f"🎰 **UpDown Bot**\n\n"
            f"Status: {status} | Mode: {mode}\n\n"
            f"Select an option:",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the Telegram bot."""
    global bot_config

    # Load configuration
    bot_config = load_config()

    # Validate required env vars
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in environment")
        sys.exit(1)

    if not TELEGRAM_CHAT_ID:
        print("Error: TELEGRAM_CHAT_ID not set in environment")
        print("Send /start to @userinfobot to get your chat ID")
        sys.exit(1)

    print(f"Starting UpDown Telegram Bot...")
    print(f"Authorized user: {TELEGRAM_CHAT_ID}")

    # Build application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation handlers for wallet setup
    solana_conv = ConversationHandler(
        entry_points=[CommandHandler("set_solana_key", set_solana_key_start)],
        states={
            AWAITING_SOLANA_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_solana_key_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    polygon_conv = ConversationHandler(
        entry_points=[CommandHandler("set_polygon_key", set_polygon_key_start)],
        states={
            AWAITING_POLYGON_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_polygon_key_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    api_conv = ConversationHandler(
        entry_points=[CommandHandler("set_polymarket_api", set_api_start)],
        states={
            AWAITING_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_api_key_receive)],
            AWAITING_API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_api_secret_receive)],
            AWAITING_API_PASSPHRASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_api_passphrase_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    trade_amount_conv = ConversationHandler(
        entry_points=[CommandHandler("set_trade_amount", set_trade_amount_start)],
        states={
            AWAITING_TRADE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_trade_amount_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    min_balance_conv = ConversationHandler(
        entry_points=[CommandHandler("set_min_balance", set_min_balance_start)],
        states={
            AWAITING_MIN_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_min_balance_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    bridge_amount_conv = ConversationHandler(
        entry_points=[CommandHandler("set_bridge_amount", set_bridge_amount_start)],
        states={
            AWAITING_BRIDGE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_bridge_amount_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    interval_conv = ConversationHandler(
        entry_points=[CommandHandler("set_interval", set_interval_start)],
        states={
            AWAITING_CYCLE_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_interval_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("predict", predict_command))
    application.add_handler(CommandHandler("markets", markets_command))
    application.add_handler(CommandHandler("pnl", pnl_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("start_bot", start_bot_command))
    application.add_handler(CommandHandler("stop_bot", stop_bot_command))
    application.add_handler(CommandHandler("toggle_dry_run", toggle_dry_run_command))
    application.add_handler(CommandHandler("bridge", bridge_command))
    application.add_handler(CommandHandler("trade", trade_command))

    # Add conversation handlers
    application.add_handler(solana_conv)
    application.add_handler(polygon_conv)
    application.add_handler(api_conv)
    application.add_handler(trade_amount_conv)
    application.add_handler(min_balance_conv)
    application.add_handler(bridge_amount_conv)
    application.add_handler(interval_conv)

    # Add callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))

    # Start the bot
    print("Bot is running. Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
