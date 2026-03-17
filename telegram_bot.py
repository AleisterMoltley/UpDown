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
from typing import Any, Callable

import numpy as np
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

# Import market scanner module
from market_scanner import (
    format_scan_results,
    get_category_summary,
    get_top_mispriced_markets,
    scan_all_markets,
)

# Import backtest module
from backtest import (
    format_backtest_results,
    format_live_vs_backtest_comparison,
    load_backtest_results,
    run_backtest,
    save_backtest_results,
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
POSITIONS_FILE = Path("positions.json")
RISK_STATE_FILE = Path("risk_state.json")

# Conversation states
# 100% Onchain Mode: No API credentials needed, only Private Key
(
    AWAITING_SOLANA_KEY,
    AWAITING_POLYGON_KEY,
    AWAITING_TRADE_AMOUNT,
    AWAITING_MIN_BALANCE,
    AWAITING_BRIDGE_AMOUNT,
    AWAITING_CYCLE_INTERVAL,
    AWAITING_MANUAL_TRADE_MARKET,
    AWAITING_MANUAL_TRADE_SIDE,
    AWAITING_MANUAL_TRADE_AMOUNT,
    AWAITING_CONFIDENCE_THRESHOLD,
    AWAITING_RSI_PERIOD,
    AWAITING_MACD_PARAMS,
    AWAITING_RSI_THRESHOLDS,
    AWAITING_RISK_MAX_DAILY_LOSS,
    AWAITING_RISK_MAX_POSITION_SIZE,
    AWAITING_RISK_MAX_CONCURRENT_POSITIONS,
    AWAITING_RISK_CIRCUIT_BREAKER_LIMIT,
) = range(17)

# Default configuration
# 100% Onchain-Modus: Keine API-Credentials mehr benötigt, nur private_key
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
    # API-Credentials entfernt - werden automatisch aus private_key abgeleitet
    "solana_rpc_url": "https://api.mainnet-beta.solana.com",
    "polygon_rpc_url": "https://polygon-rpc.com",
    "min_poly_balance_usdc": 20.0,
    "bridge_fund_amount": 50.0,
    "bot_running": False,
    "dry_run": True,
    # Onchain-Modus Settings
    "onchain_mode": True,  # Immer True im 100% onchain-Modus
    "approvals_done": False,  # Ob USDC/CTF Approvals bereits gesetzt wurden
    # Auto MATIC Top-Up Einstellungen
    "auto_matic_topup_enabled": True,
    "auto_matic_topup_min_profit": 0.5,  # Minimum Profit in USD für Top-Up
    "auto_matic_topup_amount": 0.20,  # USDC → MATIC Swap-Betrag
    # Multi-Signal Trading Engine Settings
    "min_confidence_threshold": 68,  # Minimum confidence (0-100) to execute trades
    "rsi_period": 14,  # RSI calculation period
    "macd_fast_period": 12,  # MACD fast EMA period
    "macd_slow_period": 26,  # MACD slow EMA period
    "macd_signal_period": 9,  # MACD signal line period
    "signal_weights": {
        "ma_crossover": 30,  # Weight for MA crossover signal
        "rsi": 30,  # Weight for RSI signal
        "macd": 25,  # Weight for MACD signal
        "polymarket_delta": 15,  # Weight for Polymarket price delta
    },
    "rsi_overbought": 70,  # RSI overbought threshold
    "rsi_oversold": 30,  # RSI oversold threshold
    # Risk Management Settings
    "max_daily_loss": 25.0,  # Maximum daily loss in USD before pausing
    "max_position_size_pct": 10.0,  # Maximum position size as % of balance
    "max_concurrent_positions": 5,  # Maximum number of concurrent open positions
    "circuit_breaker_consecutive_losses": 3,  # Pause after N consecutive losing trades
    # Position Settlement Tracking Settings
    "settlement_check_interval": 1800,  # Interval in seconds to poll for market resolution (default: 30 minutes)
    "auto_redeem_enabled": True,  # Automatically redeem winning outcome tokens on settlement
}

# Solana USDC token mint address (mainnet)
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Bridge endpoint
POLYMARKET_BRIDGE_URL = "https://bridge.polymarket.com/deposit"

# Gamma API
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Sensitive keys that should not be saved to disk
# Im 100% onchain-Modus nur noch polygon_private_key und solana_private_key
SENSITIVE_KEYS = frozenset([
    "solana_private_key",
    "polygon_private_key",
])

# ---------------------------------------------------------------------------
# Polygon Onchain Contracts
# ---------------------------------------------------------------------------
# USDC auf Polygon
POLYGON_USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
# Conditional Token Framework (CTF) Contract
POLYGON_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
# Polymarket Exchange Contract
POLYGON_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
# Uniswap V3 SwapRouter (Polygon)
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
# WMATIC auf Polygon
WMATIC_ADDRESS = "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"
# Max uint256 für Approvals
MAX_UINT256 = 2**256 - 1

# Settlement interval limits (in minutes)
MIN_SETTLEMENT_INTERVAL_MINUTES = 5
MAX_SETTLEMENT_INTERVAL_MINUTES = 1440  # 24 hours

# Global state
bot_config: dict = {}
bot_thread: threading.Thread | None = None
bot_thread_lock = threading.Lock()  # Lock for thread-safe bot start/stop
stop_event = threading.Event()
cg = CoinGeckoAPI()

# Settlement tracking state
_last_settlement_check: float = 0.0  # Timestamp of last settlement check

# ---------------------------------------------------------------------------
# L2 Credentials Cache
# ---------------------------------------------------------------------------
# Cache structure: {"api_key": str, "api_secret": str, "api_passphrase": str, "derived_at": float}
_l2_credentials_cache: dict | None = None
_L2_CREDENTIALS_TTL_SECONDS = 3600  # 1 hour TTL

# ---------------------------------------------------------------------------
# Logistic Regression Model Cache
# ---------------------------------------------------------------------------
# Cache structure: {"weights": np.ndarray, "bias": float, "trained_at": float}
_logreg_model_cache: dict | None = None
_LOGREG_MODEL_TTL_SECONDS = 3600  # 1 hour TTL (re-train every hour)

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
    # API-Credentials werden im 100% onchain-Modus nicht mehr benötigt
    # Sie werden automatisch aus dem private_key abgeleitet
    if os.environ.get("POLYGON_RPC_URL"):
        bot_config["polygon_rpc_url"] = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")

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


# ---------------------------------------------------------------------------
# Positions Store
# ---------------------------------------------------------------------------


def load_positions() -> dict:
    """Load positions data from file.

    Returns:
        Dict with 'open' (list of open positions) and 'closed' (list of closed positions).
        Each position has: market_id, token_id, side, entry_price, amount, timestamp,
        and optional: exit_price, exit_timestamp, realized_pnl.
    """
    if POSITIONS_FILE.exists():
        try:
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"open": [], "closed": []}
    return {"open": [], "closed": []}


def save_positions(positions_data: dict) -> None:
    """Save positions data to file."""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions_data, f, indent=2)
    except OSError as e:
        logger.error(f"Could not save positions: {e}")


# ---------------------------------------------------------------------------
# Risk Management
# ---------------------------------------------------------------------------


def load_risk_state() -> dict:
    """Load risk management state from file.

    Returns:
        Dict with 'consecutive_losing_trades' (int) and 'circuit_breaker_paused' (bool).
    """
    if RISK_STATE_FILE.exists():
        try:
            with open(RISK_STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"consecutive_losing_trades": 0, "circuit_breaker_paused": False}
    return {"consecutive_losing_trades": 0, "circuit_breaker_paused": False}


def save_risk_state(risk_state: dict) -> None:
    """Save risk management state to file."""
    try:
        with open(RISK_STATE_FILE, "w") as f:
            json.dump(risk_state, f, indent=2)
    except OSError as e:
        logger.error(f"Could not save risk state: {e}")


def get_daily_loss() -> float:
    """Get today's total loss (negative profit).

    Returns:
        Today's loss as a positive number (0 if profitable or no trades).
    """
    pnl = load_pnl()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_profit = pnl.get("daily", {}).get(today, {}).get("profit", 0.0)
    # Return loss as positive number (0 if profitable)
    return max(0.0, -daily_profit)


def get_position_count() -> int:
    """Get the count of current open positions.

    Returns:
        Number of open positions.
    """
    positions = load_positions()
    return len(positions.get("open", []))


def get_positions_for_market(market_id: str) -> list:
    """Get all open positions for a specific market.

    Args:
        market_id: The market ID to check.

    Returns:
        List of positions for the market.
    """
    positions = load_positions()
    return [p for p in positions.get("open", []) if p.get("market_id") == market_id]


def check_risk_limits(
    trade_amount: float = 0.0,
    market_id: str = "",
    send_alert: Callable = None,
) -> dict:
    """Check all risk limits before executing a trade.

    Args:
        trade_amount: The amount to trade in USD.
        market_id: The market ID for position size check.
        send_alert: Optional callback function to send Telegram alerts.

    Returns:
        Dict with 'allowed' (bool) and 'reason' (str if blocked).
    """
    risk_state = load_risk_state()

    # 1. Check circuit breaker
    if risk_state.get("circuit_breaker_paused", False):
        consecutive = risk_state.get("consecutive_losing_trades", 0)
        limit = bot_config.get("circuit_breaker_consecutive_losses", 3)
        reason = (
            f"Circuit breaker activated after {consecutive} consecutive losing trades. "
            f"Use /risk reset to resume trading."
        )
        if send_alert:
            send_alert(
                f"⚠️ **Risk Limit Hit: Circuit Breaker**\n\n"
                f"Trading paused after {consecutive} consecutive losing trades.\n"
                f"Limit: {limit} consecutive losses\n\n"
                f"Use /risk reset to resume trading."
            )
        return {"allowed": False, "reason": reason}

    # 2. Check max daily loss
    daily_loss = get_daily_loss()
    max_daily_loss = bot_config.get("max_daily_loss", 25.0)
    if daily_loss >= max_daily_loss:
        reason = f"Daily loss limit reached: ${daily_loss:.2f} (max: ${max_daily_loss:.2f})"
        if send_alert:
            send_alert(
                f"🛑 **Risk Limit Hit: Daily Loss**\n\n"
                f"Today's loss: ${daily_loss:.2f}\n"
                f"Limit: ${max_daily_loss:.2f}\n\n"
                f"Trading paused for today."
            )
        return {"allowed": False, "reason": reason}

    # 3. Check max concurrent positions
    position_count = get_position_count()
    max_positions = bot_config.get("max_concurrent_positions", 5)
    if position_count >= max_positions:
        reason = f"Max concurrent positions reached: {position_count} (max: {max_positions})"
        if send_alert:
            send_alert(
                f"⚠️ **Risk Limit Hit: Max Positions**\n\n"
                f"Open positions: {position_count}\n"
                f"Limit: {max_positions}\n\n"
                f"Close some positions before opening new ones."
            )
        return {"allowed": False, "reason": reason}

    # 4. Check max position size per market (as % of balance)
    if trade_amount > 0:
        balance = get_polygon_balance()
        max_position_pct = bot_config.get("max_position_size_pct", 10.0)
        max_position_amount = balance * (max_position_pct / 100.0) if balance > 0 else 0

        # Calculate existing position amount for this market
        existing_position_amount = 0.0
        if market_id:
            market_positions = get_positions_for_market(market_id)
            existing_position_amount = sum(p.get("amount", 0) for p in market_positions)

        total_market_position = existing_position_amount + trade_amount

        if balance > 0 and total_market_position > max_position_amount:
            reason = (
                f"Position size limit reached for market: "
                f"${total_market_position:.2f} (max: ${max_position_amount:.2f} = {max_position_pct}% of ${balance:.2f})"
            )
            if send_alert:
                send_alert(
                    f"⚠️ **Risk Limit Hit: Position Size**\n\n"
                    f"Requested: ${trade_amount:.2f}\n"
                    f"Existing in market: ${existing_position_amount:.2f}\n"
                    f"Total would be: ${total_market_position:.2f}\n"
                    f"Max allowed: ${max_position_amount:.2f} ({max_position_pct}% of balance)\n"
                    f"Balance: ${balance:.2f}"
                )
            return {"allowed": False, "reason": reason}

    return {"allowed": True, "reason": ""}


def record_trade_result(is_win: bool, send_alert: Callable = None) -> None:
    """Record a trade result for circuit breaker tracking.

    Args:
        is_win: True if trade was profitable, False if it was a loss.
        send_alert: Optional callback function to send Telegram alerts.
    """
    risk_state = load_risk_state()

    if is_win:
        # Reset consecutive losses on a win
        risk_state["consecutive_losing_trades"] = 0
    else:
        # Increment consecutive losses
        risk_state["consecutive_losing_trades"] = risk_state.get("consecutive_losing_trades", 0) + 1

        # Check if circuit breaker should activate
        consecutive = risk_state["consecutive_losing_trades"]
        limit = bot_config.get("circuit_breaker_consecutive_losses", 3)

        if consecutive >= limit and not risk_state.get("circuit_breaker_paused", False):
            risk_state["circuit_breaker_paused"] = True
            logger.warning(f"Circuit breaker activated after {consecutive} consecutive losses")
            if send_alert:
                send_alert(
                    f"🚨 **Circuit Breaker Activated**\n\n"
                    f"Bot has been paused after {consecutive} consecutive losing trades.\n"
                    f"Limit: {limit} consecutive losses\n\n"
                    f"Use /risk reset to resume trading after reviewing strategy."
                )

    save_risk_state(risk_state)


def reset_circuit_breaker() -> dict:
    """Reset the circuit breaker and consecutive loss counter.

    Returns:
        Dict with 'success' (bool) and 'message' (str).
    """
    risk_state = load_risk_state()
    was_paused = risk_state.get("circuit_breaker_paused", False)
    consecutive = risk_state.get("consecutive_losing_trades", 0)

    risk_state["circuit_breaker_paused"] = False
    risk_state["consecutive_losing_trades"] = 0
    save_risk_state(risk_state)

    if was_paused:
        return {
            "success": True,
            "message": f"Circuit breaker reset. Was paused after {consecutive} consecutive losses.",
        }
    return {
        "success": True,
        "message": "Circuit breaker was not paused. Counter reset to 0.",
    }


def get_risk_status() -> dict:
    """Get the current risk management status.

    Returns:
        Dict with all risk-related metrics.
    """
    risk_state = load_risk_state()
    pnl = load_pnl()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_data = pnl.get("daily", {}).get(today, {"trades": 0, "profit": 0.0})

    return {
        # Configured limits
        "max_daily_loss": bot_config.get("max_daily_loss", 25.0),
        "max_position_size_pct": bot_config.get("max_position_size_pct", 10.0),
        "max_concurrent_positions": bot_config.get("max_concurrent_positions", 5),
        "circuit_breaker_limit": bot_config.get("circuit_breaker_consecutive_losses", 3),
        # Current state
        "daily_loss": get_daily_loss(),
        "daily_profit": daily_data.get("profit", 0.0),
        "daily_trades": daily_data.get("trades", 0),
        "open_positions": get_position_count(),
        "consecutive_losses": risk_state.get("consecutive_losing_trades", 0),
        "circuit_breaker_paused": risk_state.get("circuit_breaker_paused", False),
    }


def calculate_shares(amount: float, entry_price: float) -> float:
    """Calculate the number of shares from amount and entry price.

    Args:
        amount: The USDC amount spent.
        entry_price: The price per share.

    Returns:
        Number of shares (amount / entry_price), or 0 if entry_price <= 0.
    """
    if entry_price <= 0:
        return 0.0
    return amount / entry_price


def parse_position_date(timestamp: str) -> str:
    """Parse timestamp and return date string (YYYY-MM-DD).

    Args:
        timestamp: ISO format timestamp string.

    Returns:
        Date string in YYYY-MM-DD format, or "Unknown" on error.
    """
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return "Unknown"


def add_position(
    market_id: str,
    token_id: str,
    side: str,
    entry_price: float,
    amount: float,
    market_question: str = "",
) -> dict:
    """Add a new open position to the store.

    Args:
        market_id: The Polymarket market ID (condition_id).
        token_id: The token ID (YES or NO token).
        side: 'yes' or 'no'.
        entry_price: The entry price per share.
        amount: The amount in USDC spent.
        market_question: The market question string (for display).

    Returns:
        The created position dict.
    """
    positions = load_positions()
    position = {
        "market_id": market_id,
        "token_id": token_id,
        "side": side,
        "entry_price": entry_price,
        "amount": amount,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market_question": market_question,
    }
    positions["open"].append(position)
    save_positions(positions)
    logger.info(f"Added position: {side.upper()} on market {market_id[:20]}... at ${entry_price:.4f}")
    return position


def close_position(
    market_id: str,
    token_id: str,
    exit_price: float,
    resolution: str | None = None,
    send_alert: Callable = None,
) -> dict | None:
    """Close an open position and calculate realized P&L.

    Args:
        market_id: The Polymarket market ID.
        token_id: The token ID.
        exit_price: The exit/settlement price per share.
        resolution: Optional resolution outcome ('yes', 'no', or None for manual exit).
        send_alert: Optional callback function to send Telegram alerts.

    Returns:
        The closed position dict with realized_pnl, or None if not found.
    """
    positions = load_positions()

    # Find the position to close - match on both market_id and token_id
    # to avoid closing wrong position when multiple positions exist
    position_index = None
    for i, pos in enumerate(positions["open"]):
        if pos["token_id"] == token_id and pos.get("market_id", "") == market_id:
            position_index = i
            break

    # Fallback: if no exact match, try matching just token_id (backward compatibility)
    if position_index is None:
        for i, pos in enumerate(positions["open"]):
            if pos["token_id"] == token_id:
                position_index = i
                break

    if position_index is None:
        logger.warning(f"Position not found for token_id: {token_id}")
        return None

    position = positions["open"].pop(position_index)

    # Calculate realized P&L using helper function
    shares = calculate_shares(position["amount"], position["entry_price"])
    exit_value = shares * exit_price
    realized_pnl = exit_value - position["amount"]

    position["exit_price"] = exit_price
    position["exit_timestamp"] = datetime.now(timezone.utc).isoformat()
    position["realized_pnl"] = realized_pnl
    position["resolution"] = resolution

    # Move to closed positions (keep last 100)
    positions["closed"].append(position)
    positions["closed"] = positions["closed"][-100:]
    save_positions(positions)

    # Record trade result for circuit breaker tracking
    is_win = realized_pnl >= 0
    record_trade_result(is_win, send_alert)

    logger.info(
        f"Closed position: {position['side'].upper()} exit at ${exit_price:.4f}, "
        f"P&L: ${realized_pnl:.2f}"
    )
    return position


def get_open_positions() -> list:
    """Get all open positions."""
    positions = load_positions()
    return positions.get("open", [])


def record_trade(amount: float, profit: float = 0.0) -> None:
    """Record a trade in P&L history.
    
    Triggert automatisch MATIC Top-Up wenn Profit > Schwellenwert.
    """
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
    
    # Auto MATIC Top-Up nach profitablem Trade
    if (
        bot_config.get("auto_matic_topup_enabled", True)
        and profit > bot_config.get("auto_matic_topup_min_profit", 0.5)
        and not bot_config.get("dry_run", True)
    ):
        try:
            swap_amount = bot_config.get("auto_matic_topup_amount", 0.20)
            result = swap_usdc_to_matic(swap_amount)
            if result.get("success"):
                logger.info(f"Auto MATIC Top-Up: {swap_amount} USDC → MATIC, Tx: {result.get('tx')}")
            else:
                logger.warning(f"Auto MATIC Top-Up fehlgeschlagen: {result.get('error')}")
        except Exception as e:
            logger.warning(f"Auto MATIC Top-Up Fehler: {e}")


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
# Polygon Onchain Functions (100% Onchain-Modus 2026)
# ---------------------------------------------------------------------------


def get_web3_client():
    """Erstellt einen Web3-Client für Polygon.
    
    Returns:
        Web3-Instanz oder None bei Fehler.
    """
    try:
        from web3 import Web3
        rpc_url = bot_config.get("polygon_rpc_url", "https://polygon-rpc.com")
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            logger.warning(f"Konnte nicht mit Polygon RPC verbinden: {rpc_url}")
            return None
        return w3
    except ImportError:
        logger.warning("web3 nicht installiert. Bitte 'pip install web3' ausführen.")
        return None
    except Exception as e:
        logger.error(f"Web3 Client Fehler: {e}")
        return None


def get_matic_balance() -> float:
    """Holt die MATIC-Balance der Polygon-Wallet.
    
    Returns:
        MATIC-Balance als float, oder 0.0 bei Fehler.
    """
    try:
        w3 = get_web3_client()
        if w3 is None:
            return 0.0
        
        address = get_polygon_address()
        if not address:
            return 0.0
        
        balance_wei = w3.eth.get_balance(address)
        return float(w3.from_wei(balance_wei, "ether"))
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der MATIC-Balance: {e}")
        return 0.0


def check_approvals() -> dict:
    """Prüft ob USDC und CTF Approvals für Exchange gesetzt sind.
    
    Returns:
        Dict mit 'usdc_approved' und 'ctf_approved' booleans.
    """
    try:
        w3 = get_web3_client()
        if w3 is None:
            return {"usdc_approved": False, "ctf_approved": False, "error": "Web3 nicht verfügbar"}
        
        address = get_polygon_address()
        if not address:
            return {"usdc_approved": False, "ctf_approved": False, "error": "Keine Polygon-Adresse"}
        
        # ERC20 ABI für allowance
        erc20_abi = [
            {
                "constant": True,
                "inputs": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"}
                ],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            }
        ]
        
        # ERC1155 ABI für isApprovedForAll
        erc1155_abi = [
            {
                "constant": True,
                "inputs": [
                    {"name": "account", "type": "address"},
                    {"name": "operator", "type": "address"}
                ],
                "name": "isApprovedForAll",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            }
        ]
        
        # USDC Allowance prüfen
        usdc_contract = w3.eth.contract(
            address=w3.to_checksum_address(POLYGON_USDC_ADDRESS),
            abi=erc20_abi
        )
        usdc_allowance = usdc_contract.functions.allowance(
            w3.to_checksum_address(address),
            w3.to_checksum_address(POLYGON_EXCHANGE_ADDRESS)
        ).call()
        
        # CTF isApprovedForAll prüfen
        ctf_contract = w3.eth.contract(
            address=w3.to_checksum_address(POLYGON_CTF_ADDRESS),
            abi=erc1155_abi
        )
        ctf_approved = ctf_contract.functions.isApprovedForAll(
            w3.to_checksum_address(address),
            w3.to_checksum_address(POLYGON_EXCHANGE_ADDRESS)
        ).call()
        
        # USDC ist approved wenn Allowance >= 1M USDC (praktisch unbegrenzt)
        usdc_approved = usdc_allowance >= 1_000_000 * 10**6
        
        return {
            "usdc_approved": usdc_approved,
            "ctf_approved": ctf_approved,
            "usdc_allowance": usdc_allowance,
        }
    except Exception as e:
        logger.error(f"Fehler beim Prüfen der Approvals: {e}")
        return {"usdc_approved": False, "ctf_approved": False, "error": str(e)}


def setup_approvals() -> dict:
    """Setzt USDC.approve() und CTF.setApprovalForAll() für den Exchange.
    
    ⚠️ WARNUNG: Dies sind onchain-Transaktionen die MATIC kosten!
    
    Returns:
        Dict mit 'success', 'usdc_tx', 'ctf_tx', und eventuell 'error'.
    """
    try:
        w3 = get_web3_client()
        if w3 is None:
            return {"success": False, "error": "Web3 nicht verfügbar"}
        
        key = bot_config.get("polygon_private_key", "")
        if not key:
            return {"success": False, "error": "Kein Polygon Private Key konfiguriert"}
        
        from eth_account import Account
        key_with_prefix = key if key.startswith("0x") else f"0x{key}"
        account = Account.from_key(key_with_prefix)
        address = account.address
        
        # Check MATIC balance für Gas
        matic_balance = get_matic_balance()
        if matic_balance < 0.01:
            return {
                "success": False,
                "error": f"Nicht genug MATIC für Gas! Balance: {matic_balance:.4f} MATIC. Mindestens 0.01 MATIC benötigt."
            }
        
        results = {"success": True, "usdc_tx": None, "ctf_tx": None}
        
        # Prüfe aktuelle Approvals
        current = check_approvals()
        
        # ERC20 ABI für approve
        erc20_abi = [
            {
                "inputs": [
                    {"name": "spender", "type": "address"},
                    {"name": "amount", "type": "uint256"}
                ],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            }
        ]
        
        # ERC1155 ABI für setApprovalForAll
        erc1155_abi = [
            {
                "inputs": [
                    {"name": "operator", "type": "address"},
                    {"name": "approved", "type": "bool"}
                ],
                "name": "setApprovalForAll",
                "outputs": [],
                "type": "function"
            }
        ]
        
        nonce = w3.eth.get_transaction_count(address)
        chain_id = bot_config.get("chain_id", 137)
        
        # USDC Approve falls nötig
        if not current.get("usdc_approved"):
            logger.info("Setze USDC Approval...")
            usdc_contract = w3.eth.contract(
                address=w3.to_checksum_address(POLYGON_USDC_ADDRESS),
                abi=erc20_abi
            )
            
            tx = usdc_contract.functions.approve(
                w3.to_checksum_address(POLYGON_EXCHANGE_ADDRESS),
                MAX_UINT256
            ).build_transaction({
                "from": address,
                "nonce": nonce,
                "gas": 100000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
                "chainId": chain_id,
            })
            
            signed_tx = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            results["usdc_tx"] = tx_hash.hex()
            logger.info(f"USDC Approval Tx: {results['usdc_tx']}")
            nonce += 1
            
            # Warte auf Bestätigung
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        else:
            logger.info("USDC bereits approved")
        
        # CTF setApprovalForAll falls nötig
        if not current.get("ctf_approved"):
            logger.info("Setze CTF Approval...")
            ctf_contract = w3.eth.contract(
                address=w3.to_checksum_address(POLYGON_CTF_ADDRESS),
                abi=erc1155_abi
            )
            
            tx = ctf_contract.functions.setApprovalForAll(
                w3.to_checksum_address(POLYGON_EXCHANGE_ADDRESS),
                True
            ).build_transaction({
                "from": address,
                "nonce": nonce,
                "gas": 100000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
                "chainId": chain_id,
            })
            
            signed_tx = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            results["ctf_tx"] = tx_hash.hex()
            logger.info(f"CTF Approval Tx: {results['ctf_tx']}")
            
            # Warte auf Bestätigung
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        else:
            logger.info("CTF bereits approved")
        
        # Update config
        bot_config["approvals_done"] = True
        save_config()
        
        return results
        
    except Exception as e:
        logger.error(f"Fehler beim Setzen der Approvals: {e}")
        return {"success": False, "error": str(e)}


def swap_usdc_to_matic(amount_usdc: float) -> dict:
    """Swapped USDC zu MATIC via Uniswap V3 Router.
    
    Args:
        amount_usdc: Betrag in USDC (z.B. 0.20 für 20 Cent)
    
    Returns:
        Dict mit 'success', 'tx', und eventuell 'error'.
    """
    try:
        w3 = get_web3_client()
        if w3 is None:
            return {"success": False, "error": "Web3 nicht verfügbar"}
        
        key = bot_config.get("polygon_private_key", "")
        if not key:
            return {"success": False, "error": "Kein Polygon Private Key konfiguriert"}
        
        from eth_account import Account
        key_with_prefix = key if key.startswith("0x") else f"0x{key}"
        account = Account.from_key(key_with_prefix)
        address = account.address
        
        # 30 Sekunden Safety-Delay (nicht blockierend in sync context)
        logger.info(f"⚠️ USDC → MATIC Swap: {amount_usdc} USDC. 30s Safety-Delay...")
        time.sleep(30)
        
        # Uniswap V3 SwapRouter ABI (ExactInputSingle)
        swap_router_abi = [
            {
                "inputs": [
                    {
                        "components": [
                            {"name": "tokenIn", "type": "address"},
                            {"name": "tokenOut", "type": "address"},
                            {"name": "fee", "type": "uint24"},
                            {"name": "recipient", "type": "address"},
                            {"name": "deadline", "type": "uint256"},
                            {"name": "amountIn", "type": "uint256"},
                            {"name": "amountOutMinimum", "type": "uint256"},
                            {"name": "sqrtPriceLimitX96", "type": "uint160"}
                        ],
                        "name": "params",
                        "type": "tuple"
                    }
                ],
                "name": "exactInputSingle",
                "outputs": [{"name": "amountOut", "type": "uint256"}],
                "type": "function",
                "stateMutability": "payable"
            }
        ]
        
        # Amount in USDC wei (6 decimals)
        amount_in = int(amount_usdc * 10**6)
        
        # Deadline: 5 Minuten in der Zukunft
        deadline = int(time.time()) + 300
        
        swap_router = w3.eth.contract(
            address=w3.to_checksum_address(UNISWAP_V3_ROUTER),
            abi=swap_router_abi
        )
        
        # Slippage-Schutz: Erwarte mindestens 80% des Nominalwerts
        # Bei 0.20 USDC und MATIC ~$0.50, erwarten wir ~0.4 MATIC
        # Wir akzeptieren 80% davon als Minimum = 0.32 MATIC
        # Für kleine Beträge ist das ein vernünftiger Schutz
        # MATIC hat 18 decimals, also: 0.20 USD / 0.50 USD/MATIC * 0.8 = 0.32 MATIC
        # In Wei: 0.32 * 10^18 = 320000000000000000
        # Vereinfacht: Für $0.20 erwarten wir min. 0.1 MATIC (konservativ)
        amount_out_min = int(0.1 * 10**18) if amount_usdc >= 0.20 else 0
        
        # ExactInputSingle Parameter
        # Fee: 3000 = 0.3% Pool (üblich für USDC/WMATIC)
        params = (
            w3.to_checksum_address(POLYGON_USDC_ADDRESS),  # tokenIn
            w3.to_checksum_address(WMATIC_ADDRESS),         # tokenOut
            3000,                                            # fee
            w3.to_checksum_address(address),                # recipient
            deadline,                                        # deadline
            amount_in,                                       # amountIn
            amount_out_min,                                  # amountOutMinimum (Slippage-Schutz)
            0                                                # sqrtPriceLimitX96
        )
        
        nonce = w3.eth.get_transaction_count(address)
        chain_id = bot_config.get("chain_id", 137)
        
        tx = swap_router.functions.exactInputSingle(params).build_transaction({
            "from": address,
            "nonce": nonce,
            "gas": 200000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            "chainId": chain_id,
            "value": 0,
        })
        
        signed_tx = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        logger.info(f"USDC → MATIC Swap Tx: {tx_hash.hex()}")
        
        return {"success": True, "tx": tx_hash.hex()}
        
    except Exception as e:
        logger.error(f"Fehler beim USDC → MATIC Swap: {e}")
        return {"success": False, "error": str(e)}


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


def _is_l2_credentials_valid() -> bool:
    """Check if cached L2 credentials are still valid (not expired).
    
    Returns:
        True if credentials exist and are within TTL, False otherwise.
    """
    global _l2_credentials_cache
    if _l2_credentials_cache is None:
        return False
    
    derived_at = _l2_credentials_cache.get("derived_at", 0)
    elapsed = time.time() - derived_at
    return elapsed < _L2_CREDENTIALS_TTL_SECONDS


def invalidate_l2_credentials_cache() -> None:
    """Invalidate the L2 credentials cache.
    
    Call this function when a 401/403 response is received from the CLOB API
    to force re-derivation of credentials on the next client build.
    """
    global _l2_credentials_cache
    _l2_credentials_cache = None
    logger.info("L2-Credentials-Cache invalidiert")


def _is_auth_error(exception: Exception) -> bool:
    """Check if an exception indicates a 401/403 authentication error.
    
    Args:
        exception: The exception to check.
        
    Returns:
        True if the exception indicates an authentication/authorization error.
    """
    error_str = str(exception).lower()
    # Check for common HTTP auth error indicators
    if "401" in error_str or "403" in error_str:
        return True
    if "unauthorized" in error_str or "forbidden" in error_str:
        return True
    if "authentication" in error_str or "invalid api" in error_str:
        return True
    
    # Check if exception has response attribute (like requests.HTTPError)
    if hasattr(exception, "response") and exception.response is not None:
        status_code = getattr(exception.response, "status_code", None)
        if status_code in (401, 403):
            return True
    
    return False


def _build_clob_client():
    """Construct a ClobClient im 100% onchain-Modus.
    
    Verwendet nur private_key (EOA signature_type=0).
    L2-Credentials werden aus dem Cache verwendet oder automatisch mit 
    derive_api_key() abgeleitet und für 1 Stunde gecached.
    Keine gespeicherten API-Creds mehr nötig!
    """
    global _l2_credentials_cache
    key = bot_config.get("polygon_private_key", "")
    
    if not key:
        logger.warning("Kein Polygon Private Key konfiguriert")
        return None
    
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        
        # Key mit 0x Prefix normalisieren
        key_with_prefix = key if key.startswith("0x") else f"0x{key}"
        
        # Check if we have valid cached credentials
        if _is_l2_credentials_valid():
            logger.info("Verwende gecachte L2-API-Credentials")
            return ClobClient(
                host=bot_config.get("polymarket_host", "https://clob.polymarket.com"),
                chain_id=bot_config.get("chain_id", 137),
                key=key_with_prefix,
                signature_type=0,
                creds=ApiCreds(
                    api_key=_l2_credentials_cache["api_key"],
                    api_secret=_l2_credentials_cache["api_secret"],
                    api_passphrase=_l2_credentials_cache["api_passphrase"],
                ),
            )
        
        # ClobClient im EOA-Modus erstellen (signature_type=0)
        client = ClobClient(
            host=bot_config.get("polymarket_host", "https://clob.polymarket.com"),
            chain_id=bot_config.get("chain_id", 137),
            key=key_with_prefix,
            signature_type=0,  # EOA signature
        )
        
        # L2-Credentials automatisch ableiten und cachen
        try:
            derived_creds = client.derive_api_key()
            logger.info("L2-API-Credentials erfolgreich abgeleitet und gecached")
            
            # Cache the derived credentials
            _l2_credentials_cache = {
                "api_key": derived_creds.get("apiKey", ""),
                "api_secret": derived_creds.get("secret", ""),
                "api_passphrase": derived_creds.get("passphrase", ""),
                "derived_at": time.time(),
            }
            
            # Client mit abgeleiteten Credentials neu erstellen
            client = ClobClient(
                host=bot_config.get("polymarket_host", "https://clob.polymarket.com"),
                chain_id=bot_config.get("chain_id", 137),
                key=key_with_prefix,
                signature_type=0,
                creds=ApiCreds(
                    api_key=_l2_credentials_cache["api_key"],
                    api_secret=_l2_credentials_cache["api_secret"],
                    api_passphrase=_l2_credentials_cache["api_passphrase"],
                ),
            )
        except Exception as e:
            # Fallback: Versuche ohne abgeleitete Credentials
            logger.warning(f"Konnte L2-Credentials nicht ableiten: {e}")
            logger.info("Verwende Client ohne abgeleitete Credentials")
        
        return client
        
    except ImportError:
        logger.warning("py-clob-client ist nicht installiert. Bitte 'pip install py-clob-client' ausführen.")
        return None
    except Exception as e:
        logger.error(f"Fehler beim Erstellen des ClobClient: {e}")
        return None


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
        # Invalidate cache on authentication errors (401/403)
        if _is_auth_error(e):
            invalidate_l2_credentials_cache()
        logger.error(f"Error fetching Polygon balance: {e}")
        return 0.0


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


# ---------------------------------------------------------------------------
# Multi-Signal Trading Engine
# ---------------------------------------------------------------------------

# Signal calculation constants
RSI_NEUTRAL_ZONE_DIVISOR = 20  # Divisor for neutral zone strength calculation (distance from 50)
RSI_NEUTRAL_MAX_STRENGTH = 30  # Maximum strength in neutral RSI zone (0-100)
MACD_HISTOGRAM_NORMALIZE_FACTOR = 10000  # Normalizes histogram relative to price
MACD_STRENGTH_MULTIPLIER = 10  # Multiplier for MACD strength calculation
POLYMARKET_DELTA_MAX_STRENGTH_FACTOR = 500  # Max strength at 20% price difference
POLYMARKET_DELTA_THRESHOLD = 0.02  # Minimum delta to signal a direction
AGREEMENT_BONUS_PER_SIGNAL = 10  # Confidence bonus per agreeing signal

# Logistic Regression constants
LOGREG_TRAINING_DAYS = 7  # Days of historical data for training
LOGREG_FEATURE_WINDOW = 14  # Minimum data points needed for feature calculation
LOGREG_MIN_TRAINING_SAMPLES = 20  # Minimum samples required for reliable model training
LOGREG_AGREEMENT_BONUS_CAP = 5  # Maximum bonus (%) for LogReg agreement with main prediction
# Confidence bonus formula: (logreg_confidence - 50) / 10
# - 50 is the neutral point (50% probability = no directional confidence)
# - Division by 10 scales the bonus to 0-5% range (e.g., 100% confidence → 5% bonus)

# Polymarket deviation scaling
POLYMARKET_DEVIATION_STRENGTH_SCALE = 5  # Scaling factor: 20% deviation = 100% strength

# Kelly Criterion constants
KELLY_MAX_FRACTION = 0.15  # Maximum 15% of balance per trade
KELLY_MIN_TRADE_USD = 3.0  # Minimum $3 per trade
KELLY_HOUSE_EDGE_BUFFER = 0.03  # Polymarket house edge buffer


def kelly_position_size(confidence: float, balance: float, deviation_pct: float = 0.0) -> float:
    """Calculate optimal position size using Kelly Criterion.
    
    The Kelly Criterion determines the optimal fraction of bankroll to bet
    to maximize long-term growth while minimizing risk of ruin.
    
    Formula:
        edge = abs(deviation_pct / 100) + 0.03 (Polymarket house edge buffer)
        fraction = (confidence/100 * 2 - 1) * edge
    
    Constraints:
        - Maximum 15% of balance per trade
        - Minimum $3 per trade
    
    Args:
        confidence: Confidence level (0-100) from multi-signal engine
        balance: Current USDC balance
        deviation_pct: Market price deviation percentage from scanner (default 0)
    
    Returns:
        Optimal trade amount in USD, constrained to [min_trade, max_position]
    """
    # Calculate edge: deviation percentage + house edge buffer
    edge = abs(deviation_pct / 100) + KELLY_HOUSE_EDGE_BUFFER
    
    # Kelly formula: fraction = (win_prob * 2 - 1) * edge
    # Where win_prob is confidence/100
    win_prob = confidence / 100.0
    kelly_fraction = (win_prob * 2 - 1) * edge
    
    # Kelly can be negative if confidence < 50% (indicating a losing bet)
    # In that case, we should not bet at all
    if kelly_fraction <= 0:
        return KELLY_MIN_TRADE_USD  # Return minimum trade size
    
    # Cap at maximum fraction (15% of balance)
    kelly_fraction = min(kelly_fraction, KELLY_MAX_FRACTION)
    
    # Calculate position size
    position_size = balance * kelly_fraction
    
    # Apply constraints: min $3, max 15% of balance
    max_position = balance * KELLY_MAX_FRACTION
    position_size = max(KELLY_MIN_TRADE_USD, min(position_size, max_position))
    
    return round(position_size, 2)


def sigmoid(x: float) -> float:
    """Compute sigmoid function with overflow protection.
    
    Args:
        x: Input value.
    
    Returns:
        Sigmoid of x, clamped between 0 and 1.
    """
    # Clip to avoid overflow in exp
    x = np.clip(x, -500, 500)
    return 1.0 / (1.0 + np.exp(-x))


def fetch_7day_historical_data(crypto_id: str = "bitcoin") -> list | None:
    """Fetch 7 days of price data from CoinGecko for model training.
    
    Note: CoinGecko's market_chart endpoint returns only price data, not true OHLC.
    We simulate OHLC format for compatibility, with all values being the same price.
    
    Args:
        crypto_id: Cryptocurrency ID for CoinGecko API.
    
    Returns:
        List of [timestamp, price, price, price, price] (simulated OHLC) or None on error.
    """
    try:
        # CoinGecko returns hourly data when days <= 90
        data = cg.get_coin_market_chart_by_id(
            id=crypto_id,
            vs_currency="usd",
            days=LOGREG_TRAINING_DAYS,
        )
        
        if not data or "prices" not in data:
            return None
        
        # Convert to OHLC-like format (CoinGecko market_chart only provides prices, not true OHLC)
        # All OHLC values are identical as we only have the spot price at each timestamp
        prices = data.get("prices", [])
        return [[p[0], p[1], p[1], p[1], p[1]] for p in prices]
    except Exception as e:
        logger.error(f"Error fetching 7-day historical data: {e}")
        return None


def prepare_logreg_features(closes: list) -> np.ndarray | None:
    """Prepare feature vector for logistic regression from price data.
    
    Features:
    - Normalized RSI (scaled 0-1)
    - Normalized MACD histogram (scaled -1 to 1)
    - MA crossover signal (-1, 0, or 1)
    - Price momentum (% change over last N periods)
    
    Args:
        closes: List of closing prices.
    
    Returns:
        numpy array of features or None if insufficient data.
    """
    if len(closes) < LOGREG_FEATURE_WINDOW:
        return None
    
    try:
        # RSI (normalize to 0-1)
        rsi = calculate_rsi(closes) / 100.0
        
        # MACD histogram (normalize relative to price)
        macd_data = calculate_macd(closes)
        if macd_data.get("valid"):
            hist = macd_data["histogram"]
            # Use mean of recent non-zero prices as fallback for normalization
            recent_prices = [p for p in closes[-20:] if p > 0]
            if recent_prices:
                current_price = recent_prices[-1]
            else:
                # No valid prices - return None to signal insufficient data
                return None
            # Normalize to roughly -1 to 1 range relative to 1% of price
            normalized_hist = np.clip(hist / (current_price * 0.01), -1, 1)
        else:
            normalized_hist = 0.0
        
        # MA crossover signal
        short_window = bot_config.get("short_window", 5)
        long_window = bot_config.get("long_window", 20)
        if len(closes) >= long_window:
            ma_short = sum(closes[-short_window:]) / short_window
            ma_long = sum(closes[-long_window:]) / long_window
            ma_signal = 1.0 if ma_short > ma_long else -1.0
        else:
            ma_signal = 0.0
        
        # Price momentum (% change over last 5 periods)
        if len(closes) >= 6 and closes[-6] > 0:
            momentum = (closes[-1] - closes[-6]) / closes[-6]
            momentum = np.clip(momentum, -0.1, 0.1) * 10  # Scale to roughly -1 to 1
        else:
            momentum = 0.0
        
        return np.array([rsi, normalized_hist, ma_signal, momentum])
    except Exception as e:
        logger.error(f"Error preparing LogReg features: {e}")
        return None


def create_training_labels(closes: list, lookahead: int = 1) -> np.ndarray:
    """Create binary labels for training (1 = price went up, 0 = price went down).
    
    Args:
        closes: List of closing prices.
        lookahead: Number of periods to look ahead for labeling.
    
    Returns:
        numpy array of binary labels.
    """
    labels = []
    for i in range(len(closes) - lookahead):
        if closes[i + lookahead] > closes[i]:
            labels.append(1)
        else:
            labels.append(0)
    return np.array(labels)


def train_logreg_model(X: np.ndarray, y: np.ndarray, learning_rate: float = 0.1, iterations: int = 100) -> tuple:
    """Train a simple logistic regression model using gradient descent.
    
    Args:
        X: Feature matrix (n_samples, n_features).
        y: Binary labels (n_samples,).
        learning_rate: Gradient descent learning rate.
        iterations: Number of training iterations.
    
    Returns:
        Tuple of (weights, bias).
    """
    n_samples, n_features = X.shape
    weights = np.zeros(n_features)
    bias = 0.0
    
    for _ in range(iterations):
        # Forward pass
        linear = np.dot(X, weights) + bias
        predictions = sigmoid(linear)
        
        # Gradient descent
        dw = (1 / n_samples) * np.dot(X.T, (predictions - y))
        db = (1 / n_samples) * np.sum(predictions - y)
        
        weights -= learning_rate * dw
        bias -= learning_rate * db
    
    return weights, bias


def get_logreg_model() -> dict | None:
    """Get or train the logistic regression model.
    
    Uses cached model if available and not expired, otherwise trains a new one.
    
    Returns:
        Dict with 'weights' and 'bias', or None if training failed.
    """
    global _logreg_model_cache
    
    # Check cache
    if _logreg_model_cache is not None:
        trained_at = _logreg_model_cache.get("trained_at", 0)
        if time.time() - trained_at < _LOGREG_MODEL_TTL_SECONDS:
            return _logreg_model_cache
    
    # Train new model
    try:
        crypto_id = bot_config.get("crypto_id", "bitcoin")
        historical_data = fetch_7day_historical_data(crypto_id)
        
        if not historical_data or len(historical_data) < LOGREG_FEATURE_WINDOW * 2:
            logger.warning("Insufficient historical data for LogReg training")
            return None
        
        closes = [d[4] for d in historical_data]  # Extract closing prices
        
        # Create feature matrix and labels
        X_list = []
        y_list = []
        
        for i in range(LOGREG_FEATURE_WINDOW, len(closes) - 1):
            features = prepare_logreg_features(closes[:i+1])
            if features is not None:
                X_list.append(features)
                # Label: 1 if next price is higher, 0 otherwise
                y_list.append(1 if closes[i + 1] > closes[i] else 0)
        
        if len(X_list) < LOGREG_MIN_TRAINING_SAMPLES:
            logger.warning(f"Not enough training samples for LogReg (need {LOGREG_MIN_TRAINING_SAMPLES}, have {len(X_list)})")
            return None
        
        X = np.array(X_list)
        y = np.array(y_list)
        
        # Train model
        weights, bias = train_logreg_model(X, y)
        
        _logreg_model_cache = {
            "weights": weights,
            "bias": bias,
            "trained_at": time.time(),
            "samples": len(X_list),
        }
        
        logger.info(f"LogReg model trained on {len(X_list)} samples")
        return _logreg_model_cache
        
    except Exception as e:
        logger.error(f"Error training LogReg model: {e}")
        return None


def predict_with_logreg(closes: list) -> dict | None:
    """Make prediction using the logistic regression model.
    
    Args:
        closes: List of closing prices.
    
    Returns:
        Dict with 'direction' and 'probability', or None if prediction failed.
    """
    model = get_logreg_model()
    if model is None:
        return None
    
    features = prepare_logreg_features(closes)
    if features is None:
        return None
    
    try:
        weights = model["weights"]
        bias = model["bias"]
        
        linear = np.dot(features, weights) + bias
        probability = sigmoid(linear)
        
        direction = "up" if probability > 0.5 else "down"
        confidence = abs(probability - 0.5) * 2 * 100  # Convert to 0-100 scale
        
        return {
            "direction": direction,
            "probability": probability,
            "confidence": confidence,
        }
    except Exception as e:
        logger.error(f"Error in LogReg prediction: {e}")
        return None


def calculate_ema(prices: list, period: int) -> list:
    """Calculate Exponential Moving Average (EMA).
    
    Args:
        prices: List of price values.
        period: EMA period.
    
    Returns:
        List of EMA values (same length as prices, with NaN for early values).
    """
    if len(prices) < period:
        return []
    
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]  # Start with SMA
    
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    
    return ema


def calculate_rsi(closes: list, period: int | None = None) -> float:
    """Calculate Relative Strength Index (RSI).
    
    Args:
        closes: List of closing prices.
        period: RSI period (default from config, typically 14).
    
    Returns:
        RSI value (0-100), or 50 if insufficient data.
    """
    if period is None:
        period = bot_config.get("rsi_period", 14)
    
    if len(closes) < period + 1:
        return 50.0  # Neutral value if insufficient data
    
    # Calculate price changes
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    
    # Separate gains and losses
    gains = [max(0, change) for change in changes]
    losses = [max(0, -change) for change in changes]
    
    # Calculate average gain and loss using EMA
    recent_gains = gains[-(period):]
    recent_losses = losses[-(period):]
    
    avg_gain = sum(recent_gains) / period
    avg_loss = sum(recent_losses) / period
    
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def calculate_macd(closes: list) -> dict:
    """Calculate MACD (Moving Average Convergence Divergence).
    
    Returns:
        Dictionary with:
        - macd_line: MACD line value
        - signal_line: Signal line value  
        - histogram: MACD histogram (macd_line - signal_line)
        - valid: Whether calculation was successful
    """
    fast_period = bot_config.get("macd_fast_period", 12)
    slow_period = bot_config.get("macd_slow_period", 26)
    signal_period = bot_config.get("macd_signal_period", 9)
    
    if len(closes) < slow_period + signal_period:
        return {"macd_line": 0, "signal_line": 0, "histogram": 0, "valid": False}
    
    # Calculate fast and slow EMAs
    fast_ema = calculate_ema(closes, fast_period)
    slow_ema = calculate_ema(closes, slow_period)
    
    if not fast_ema or not slow_ema:
        return {"macd_line": 0, "signal_line": 0, "histogram": 0, "valid": False}
    
    # Align EMAs (slow_ema starts later)
    offset = slow_period - fast_period
    fast_ema_aligned = fast_ema[offset:]
    
    # MACD line = Fast EMA - Slow EMA
    macd_line_values = [f - s for f, s in zip(fast_ema_aligned, slow_ema)]
    
    if len(macd_line_values) < signal_period:
        return {"macd_line": 0, "signal_line": 0, "histogram": 0, "valid": False}
    
    # Signal line = EMA of MACD line
    signal_ema = calculate_ema(macd_line_values, signal_period)
    
    if not signal_ema:
        return {"macd_line": macd_line_values[-1], "signal_line": 0, "histogram": 0, "valid": False}
    
    macd_line = macd_line_values[-1]
    signal_line = signal_ema[-1]
    histogram = macd_line - signal_line
    
    return {
        "macd_line": macd_line,
        "signal_line": signal_line,
        "histogram": histogram,
        "valid": True,
    }


def get_polymarket_price_delta(market: dict | None = None) -> dict:
    """Calculate price delta between market price and fair value.
    
    Compares current Polymarket YES token price against the fair value
    implied by our technical signals. Uses mispriced markets from scanner.
    
    Args:
        market: Optional market dict. If None, searches for top mispriced market.
    
    Returns:
        Dictionary with:
        - market_price: Current YES token price (0-1)
        - fair_value: Our estimated fair value (0-1)
        - delta: Difference (positive = undervalued for YES)
        - valid: Whether calculation was successful
    """
    try:
        if market is None:
            markets = find_relevant_markets(count=8, min_deviation_pct=12.0)
            if not markets:
                return {"market_price": 0.5, "fair_value": 0.5, "delta": 0, "valid": False}
            market = markets[0]
        
        # Get current market price for YES token
        tokens = market.get("tokens", [])
        yes_price = 0.5
        for token in tokens:
            if token.get("outcome", "").lower() == "yes":
                yes_price = float(token.get("price", 0.5))
                break
        
        # Use price_deviation data from scanner if available
        price_deviation = market.get("price_deviation", {})
        historical_mean = price_deviation.get("historical_mean", 0.5)
        
        # Fair value is the historical mean from the scanner
        return {
            "market_price": yes_price,
            "fair_value": historical_mean,
            "delta": yes_price - historical_mean,
            "valid": True,
        }
    except Exception as e:
        logger.error(f"Error getting Polymarket price delta: {e}")
        return {"market_price": 0.5, "fair_value": 0.5, "delta": 0, "valid": False}


def calculate_ma_crossover_signal(closes: list) -> dict:
    """Calculate MA crossover signal strength.
    
    Returns:
        Dictionary with:
        - direction: 'up', 'down', or 'neutral'
        - strength: Signal strength (0-100)
        - ma_short: Short MA value
        - ma_long: Long MA value
    """
    short_window = bot_config.get("short_window", 5)
    long_window = bot_config.get("long_window", 20)
    
    if len(closes) < long_window:
        return {"direction": "neutral", "strength": 0, "ma_short": 0, "ma_long": 0}
    
    ma_short = sum(closes[-short_window:]) / short_window
    ma_long = sum(closes[-long_window:]) / long_window
    
    # Calculate percentage difference
    if ma_long == 0:
        return {"direction": "neutral", "strength": 0, "ma_short": ma_short, "ma_long": ma_long}
    
    pct_diff = ((ma_short - ma_long) / ma_long) * 100
    
    # Convert to strength (0-100), with max strength at +/- 2% difference
    strength = min(100, abs(pct_diff) * 50)
    direction = "up" if ma_short > ma_long else "down"
    
    return {
        "direction": direction,
        "strength": strength,
        "ma_short": ma_short,
        "ma_long": ma_long,
    }


def calculate_rsi_signal(closes: list) -> dict:
    """Calculate RSI-based signal strength.
    
    Returns:
        Dictionary with:
        - direction: 'up' (oversold), 'down' (overbought), or 'neutral'
        - strength: Signal strength (0-100)
        - rsi: Raw RSI value
    """
    rsi = calculate_rsi(closes)
    overbought = bot_config.get("rsi_overbought", 70)
    oversold = bot_config.get("rsi_oversold", 30)
    
    if rsi <= oversold:
        # Oversold = bullish signal
        direction = "up"
        # Strength increases as RSI approaches 0
        strength = ((oversold - rsi) / oversold) * 100
    elif rsi >= overbought:
        # Overbought = bearish signal
        direction = "down"
        # Strength increases as RSI approaches 100
        strength = ((rsi - overbought) / (100 - overbought)) * 100
    else:
        # Neutral zone - small strength based on distance from 50
        direction = "neutral"
        strength = abs(rsi - 50) / RSI_NEUTRAL_ZONE_DIVISOR * 100
        strength = min(RSI_NEUTRAL_MAX_STRENGTH, strength)
    
    return {
        "direction": direction,
        "strength": min(100, strength),
        "rsi": rsi,
    }


def calculate_macd_signal(closes: list) -> dict:
    """Calculate MACD-based signal strength.
    
    Returns:
        Dictionary with:
        - direction: 'up', 'down', or 'neutral'
        - strength: Signal strength (0-100)
        - histogram: MACD histogram value
        - macd_line: MACD line value
        - signal_line: Signal line value
    """
    macd = calculate_macd(closes)
    
    if not macd.get("valid"):
        return {
            "direction": "neutral",
            "strength": 0,
            "histogram": 0,
            "macd_line": 0,
            "signal_line": 0,
        }
    
    # Handle empty closes list - return neutral signal
    if not closes:
        return {
            "direction": "neutral",
            "strength": 0,
            "histogram": 0,
            "macd_line": 0,
            "signal_line": 0,
        }
    
    histogram = macd["histogram"]
    macd_line = macd["macd_line"]
    signal_line = macd["signal_line"]
    
    # Direction based on histogram
    if histogram > 0:
        direction = "up"
    elif histogram < 0:
        direction = "down"
    else:
        direction = "neutral"
    
    # Strength based on histogram magnitude, normalized relative to price
    current_price = closes[-1]
    if current_price <= 0:
        current_price = 1  # Fallback for invalid price
    normalized_histogram = abs(histogram) / current_price * MACD_HISTOGRAM_NORMALIZE_FACTOR
    strength = min(100, normalized_histogram * MACD_STRENGTH_MULTIPLIER)
    
    return {
        "direction": direction,
        "strength": strength,
        "histogram": histogram,
        "macd_line": macd_line,
        "signal_line": signal_line,
    }


def calculate_polymarket_delta_signal(market_price: float, fair_value: float) -> dict:
    """Calculate signal from Polymarket price vs fair value.
    
    Args:
        market_price: Current YES token price (0-1)
        fair_value: Our estimated fair value (0-1)
    
    Returns:
        Dictionary with:
        - direction: 'up' if undervalued, 'down' if overvalued
        - strength: Signal strength (0-100)
        - delta: Price difference
    """
    delta = fair_value - market_price
    
    # Direction: positive delta means market is undervalued (buy YES)
    if delta > POLYMARKET_DELTA_THRESHOLD:
        direction = "up"
    elif delta < -POLYMARKET_DELTA_THRESHOLD:
        direction = "down"
    else:
        direction = "neutral"
    
    # Strength based on delta magnitude (max strength at 20% difference)
    strength = min(100, abs(delta) * POLYMARKET_DELTA_MAX_STRENGTH_FACTOR)
    
    return {
        "direction": direction,
        "strength": strength,
        "delta": delta,
    }


def calculate_confidence(closes: list, market_price_deviation: float | None = None, market: dict | None = None) -> dict:
    """Calculate overall confidence score combining all signals (Multi-Signal Engine).
    
    This is the main prediction engine that combines:
    - MA Crossover signal (30% weight)
    - RSI (14) with Overbought/Oversold detection (30% weight)
    - MACD (12, 26, 9) (25% weight)
    - Polymarket deviation_pct from scanner (15% weight)
    
    Also includes a logistic regression fallback trained on 7 days of historical data.
    
    Args:
        closes: List of closing prices.
        market_price_deviation: Optional Polymarket price deviation percentage from scanner.
                                Positive = underpriced (bullish), Negative = overpriced (bearish).
        market: Optional Polymarket market for additional price delta info.
    
    Returns:
        Dictionary with:
        - direction: 'up' or 'down' (or 'hold' if insufficient data)
        - confidence_score: Overall confidence score (0-100)
        - signals: Individual signal details
        - fair_value: Estimated fair value for YES token
        - logreg_prediction: Logistic regression prediction (if available)
    """
    # Signal weights as specified in requirements
    weights = bot_config.get("signal_weights", {
        "ma_crossover": 30,
        "rsi": 30,
        "macd": 25,
        "polymarket_delta": 15,
    })
    
    # Calculate individual signals
    ma_signal = calculate_ma_crossover_signal(closes)
    rsi_signal = calculate_rsi_signal(closes)
    macd_signal = calculate_macd_signal(closes)
    
    # Determine overall direction from weighted votes
    up_score = 0
    down_score = 0
    total_weight = 0
    
    for signal_name, signal in [("ma_crossover", ma_signal), ("rsi", rsi_signal), ("macd", macd_signal)]:
        weight = weights.get(signal_name, 25)
        total_weight += weight
        
        if signal["direction"] == "up":
            up_score += weight * (signal["strength"] / 100)
        elif signal["direction"] == "down":
            down_score += weight * (signal["strength"] / 100)
    
    # Calculate fair value for Polymarket (0-1 scale)
    # Higher up_score = higher fair value for YES
    if total_weight > 0:
        technical_fair_value = 0.5 + (up_score - down_score) / (total_weight * 2)
    else:
        technical_fair_value = 0.5
    technical_fair_value = max(0.1, min(0.9, technical_fair_value))
    
    # Handle Polymarket deviation from scanner (if provided)
    if market_price_deviation is not None:
        # Convert deviation_pct to a signal
        # Positive deviation = market underpriced = bullish
        # Negative deviation = market overpriced = bearish
        pm_direction = "up" if market_price_deviation > 0 else "down" if market_price_deviation < 0 else "neutral"
        # Scale strength: using POLYMARKET_DEVIATION_STRENGTH_SCALE (5 = 20% deviation → 100% strength)
        pm_strength = min(100, abs(market_price_deviation) * POLYMARKET_DEVIATION_STRENGTH_SCALE)
        polymarket_signal = {
            "direction": pm_direction,
            "strength": pm_strength,
            "delta": market_price_deviation / 100,  # Convert to decimal
            "deviation_pct": market_price_deviation,
        }
        # Store actual market_price for return value
        pm_market_price = 0.5 + (market_price_deviation / 200)  # Estimate: 50% ± deviation/2
    else:
        # Fallback: get Polymarket price delta from market data
        polymarket_data = get_polymarket_price_delta(market)
        polymarket_data["fair_value"] = technical_fair_value
        polymarket_data["delta"] = technical_fair_value - polymarket_data["market_price"]
        pm_market_price = polymarket_data["market_price"]
        
        polymarket_signal = calculate_polymarket_delta_signal(
            polymarket_data["market_price"],
            technical_fair_value
        )
    
    # Add Polymarket delta to scores
    polymarket_weight = weights.get("polymarket_delta", 15)
    total_weight += polymarket_weight
    
    if polymarket_signal["direction"] == "up":
        up_score += polymarket_weight * (polymarket_signal["strength"] / 100)
    elif polymarket_signal["direction"] == "down":
        down_score += polymarket_weight * (polymarket_signal["strength"] / 100)
    
    # Get logistic regression prediction (fallback/confirmation)
    logreg_pred = predict_with_logreg(closes)
    logreg_signal = None
    if logreg_pred is not None:
        logreg_signal = {
            "direction": logreg_pred["direction"],
            "confidence": logreg_pred["confidence"],
            "probability": logreg_pred["probability"],
        }
        # If LogReg agrees with main prediction, add small confidence bonus
        # This serves as a confirmation signal
        # Formula: bonus = (confidence - 50) / 10, capped at LOGREG_AGREEMENT_BONUS_CAP
        # - 50 is neutral (50% probability = no directional confidence)
        # - Division by 10 scales to 0-5% range (e.g., 100% confidence → 5% bonus)
        if logreg_pred["confidence"] > 60:  # Only if LogReg is confident
            if (logreg_pred["direction"] == "up" and up_score > down_score) or \
               (logreg_pred["direction"] == "down" and down_score > up_score):
                agreement_factor = min(LOGREG_AGREEMENT_BONUS_CAP, (logreg_pred["confidence"] - 50) / 10)
            else:
                agreement_factor = 0
        else:
            agreement_factor = 0
    else:
        agreement_factor = 0
    
    # Determine final prediction
    if total_weight <= 0:
        return {
            "direction": "hold",
            "confidence_score": 0,
            "signals": {
                "ma_crossover": ma_signal,
                "rsi": rsi_signal,
                "macd": macd_signal,
                "polymarket_delta": polymarket_signal,
            },
            "fair_value": 0.5,
            "polymarket_price": 0.5,
            "up_score": 0,
            "down_score": 0,
            "logreg_prediction": logreg_signal,
        }
    
    if up_score > down_score:
        direction = "up"
        confidence = (up_score / total_weight) * 100
    elif down_score > up_score:
        direction = "down"
        confidence = (down_score / total_weight) * 100
    else:
        direction = "hold"
        confidence = 0
    
    # Adjust confidence based on signal agreement
    directions = [ma_signal["direction"], rsi_signal["direction"], macd_signal["direction"]]
    agreement_count = sum(1 for d in directions if d == direction)
    
    # Bonus for signal agreement (up to 20% bonus for all signals agreeing)
    if agreement_count >= 2:
        agreement_bonus = (agreement_count - 1) * AGREEMENT_BONUS_PER_SIGNAL
        confidence = min(100, confidence + agreement_bonus)
    
    # Add LogReg agreement bonus
    confidence = min(100, confidence + agreement_factor)
    
    # Build result - return both 'confidence_score' (new) and 'confidence' (backward compat)
    return {
        "direction": direction,
        "confidence_score": round(confidence, 1),
        "confidence": round(confidence, 1),  # Backward compatibility
        "prediction": direction,  # Backward compatibility
        "signals": {
            "ma_crossover": ma_signal,
            "rsi": rsi_signal,
            "macd": macd_signal,
            "polymarket_delta": polymarket_signal,
        },
        "fair_value": technical_fair_value,
        "polymarket_price": pm_market_price,
        "up_score": up_score,
        "down_score": down_score,
        "logreg_prediction": logreg_signal,
    }


def calculate_confidence_score(closes: list, market: dict | None = None) -> dict:
    """Legacy wrapper for calculate_confidence() for backward compatibility.
    
    Note: This is deprecated. Use calculate_confidence() directly.
    
    Args:
        closes: List of closing prices.
        market: Optional Polymarket market for price delta.
    
    Returns:
        Dictionary with prediction info (same as calculate_confidence).
    """
    return calculate_confidence(closes, market_price_deviation=None, market=market)


def predict_up_down(closes: list) -> str:
    """Predict the next price direction.
    
    Note: This is a legacy function for backward compatibility.
    Use calculate_confidence() for the new multi-signal engine with confidence scores.
    
    Args:
        closes: List of closing prices.
    
    Returns:
        'up', 'down', or 'hold'
    """
    result = calculate_confidence(closes)
    return result.get("direction", "hold")


def get_current_prediction() -> dict:
    """Get current prediction with price data using multi-signal engine.

    Returns:
        Dictionary with prediction info:
        - prediction: 'up', 'down', 'hold', 'error', or 'unavailable'
        - confidence: Confidence score (0-100)
        - price: Current price (0 on error)
        - candles: Number of candles fetched
        - crypto: Crypto ID
        - signals: Individual signal details (ma_crossover, rsi, macd, polymarket_delta)
        - fair_value: Estimated fair value for YES token
        - meets_threshold: Whether confidence meets minimum threshold for trading
        - logreg_prediction: Logistic regression prediction (if available)
        - error: Error message (only present on failure)
    """
    try:
        crypto_id = bot_config.get("crypto_id", "bitcoin")
        ohlc = fetch_5min_data(crypto_id=crypto_id)
        closes = [candle[4] for candle in ohlc]
        current_price = closes[-1] if closes else 0
        
        # Use new multi-signal engine with logistic regression fallback
        confidence_result = calculate_confidence(closes)
        
        # Use new default threshold of 68
        min_threshold = bot_config.get("min_confidence_threshold", 68)
        confidence_score = confidence_result.get("confidence_score", confidence_result.get("confidence", 0))
        meets_threshold = confidence_score >= min_threshold
        
        return {
            "prediction": confidence_result.get("direction", confidence_result.get("prediction", "hold")),
            "confidence": confidence_score,
            "confidence_score": confidence_score,
            "price": current_price,
            "candles": len(closes),
            "crypto": crypto_id,
            "signals": confidence_result.get("signals", {}),
            "fair_value": confidence_result.get("fair_value", 0.5),
            "polymarket_price": confidence_result.get("polymarket_price", 0.5),
            "meets_threshold": meets_threshold,
            "min_threshold": min_threshold,
            "logreg_prediction": confidence_result.get("logreg_prediction"),
        }
    except ConnectionError as e:
        logger.error(f"Connection error getting prediction: {e}")
        return {
            "prediction": "unavailable",
            "confidence": 0,
            "confidence_score": 0,
            "price": 0,
            "candles": 0,
            "crypto": bot_config.get("crypto_id", "unknown"),
            "signals": {},
            "meets_threshold": False,
            "logreg_prediction": None,
            "error": (
                "Unable to connect to CoinGecko API. "
                "Verify network connectivity and ensure api.coingecko.com is not blocked by firewall rules."
            ),
        }
    except Exception as e:
        logger.error(f"Error getting prediction: {e}")
        return {
            "prediction": "error",
            "confidence": 0,
            "confidence_score": 0,
            "price": 0,
            "candles": 0,
            "crypto": bot_config.get("crypto_id", "unknown"),
            "signals": {},
            "meets_threshold": False,
            "logreg_prediction": None,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Market Discovery & Trading
# ---------------------------------------------------------------------------

# Target categories for auto-trading: crypto, politics, economics
TARGET_CATEGORIES = ["crypto", "politics", "economics"]


def get_deviation_emoji(direction: str) -> str:
    """Get emoji for price deviation direction.
    
    Args:
        direction: 'underpriced', 'overpriced', or 'unknown'
        
    Returns:
        Appropriate emoji: 📉 for underpriced, 📈 for overpriced, ⚖️ for unknown
    """
    if direction == "underpriced":
        return "📉"
    elif direction == "overpriced":
        return "📈"
    else:
        return "⚖️"


def find_relevant_markets(
    query_terms: list | None = None,
    count: int = 8,
    min_deviation_pct: float = 12.0,
    categories: list | None = None,
) -> list:
    """Find top mispriced markets using the market scanner.

    This function replaces the old BTC-daily-only market discovery with
    a scanner that finds mispriced markets across multiple categories.

    Args:
        query_terms: Deprecated parameter, kept for backward compatibility.
        count: Number of top mispriced markets to fetch.
        min_deviation_pct: Minimum price deviation percentage to consider.
        categories: List of categories to filter by (default: crypto, politics, economics).

    Returns:
        List of top mispriced market dictionaries with deviation data.
    """
    if categories is None:
        categories = TARGET_CATEGORIES

    try:
        # Get top mispriced markets from the scanner
        all_mispriced = get_top_mispriced_markets(
            count=count,
            min_deviation_pct=min_deviation_pct,
        )

        # Filter by target categories if specified
        if categories:
            filtered = [
                m for m in all_mispriced
                if m.get("category", "other") in categories
            ]
            # If filtering resulted in empty, return all mispriced markets
            if not filtered:
                logger.info(f"No markets found in categories {categories}, using all mispriced")
                return all_mispriced
            return filtered

        return all_mispriced

    except Exception as e:
        logger.error(f"Error finding mispriced markets: {e}")
        return []


def place_trade(
    market: dict,
    outcome: str = "yes",
    amount: float = 10.0,
    send_alert: Callable = None,
    skip_risk_check: bool = False,
) -> dict:
    """Place a limit order on a Polymarket market.

    Args:
        market: The market dict with tokens.
        outcome: 'yes' or 'no'.
        amount: The amount in USDC to trade.
        send_alert: Optional callback function to send Telegram alerts.
        skip_risk_check: If True, skip risk limit checks (for internal use).

    Returns:
        Dict with 'success' (bool), 'error' (str on failure), and order details on success.
    """
    # Get market ID for risk check
    market_id = market.get("id") or market.get("condition_id") or ""

    # Check risk limits before trading (unless explicitly skipped)
    if not skip_risk_check:
        risk_check = check_risk_limits(
            trade_amount=amount,
            market_id=market_id,
            send_alert=send_alert,
        )
        if not risk_check.get("allowed", True):
            return {"success": False, "error": risk_check.get("reason", "Risk limit reached")}

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

        # Record the position for tracking
        market_question = market.get("question", "")
        add_position(
            market_id=market_id,
            token_id=token_id,
            side=outcome.lower(),
            entry_price=price,
            amount=amount,
            market_question=market_question,
        )

        return {"success": True, "response": resp, "price": price, "token_id": token_id}
    except Exception as e:
        # Invalidate cache on authentication errors (401/403)
        if _is_auth_error(e):
            invalidate_l2_credentials_cache()
        return {"success": False, "error": str(e)}


def get_market_by_id(market_id: str) -> dict | None:
    """Fetch a specific market by ID from Gamma API.

    Args:
        market_id: The Polymarket market ID (condition_id).

    Returns:
        Market dict or None if not found.
    """
    try:
        url = f"{GAMMA_API_BASE}/markets/{market_id}"
        response = requests.get(url, timeout=15)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Error fetching market {market_id}: {e}")
        return None


def check_resolved_markets(send_alert: Callable = None) -> list:
    """Check open positions for resolved markets and calculate realized P&L.

    Args:
        send_alert: Optional callback function to send Telegram alerts.

    Returns:
        List of closed position results with P&L info.
    """
    open_positions = get_open_positions()
    if not open_positions:
        return []

    closed_results = []

    for position in open_positions:
        market_id = position.get("market_id", "")
        if not market_id:
            continue

        market = get_market_by_id(market_id)
        if market is None:
            continue

        # Check if market is resolved (closed == True or end_date_iso in the past)
        is_closed = market.get("closed", False)
        # Gamma API uses camelCase, but check both for compatibility
        resolution_outcome = market.get("resolvedOutcome") or market.get("resolved_outcome")

        if is_closed and resolution_outcome:
            # Market resolved - determine settlement price
            side = position.get("side", "").lower()
            token_id = position.get("token_id", "")

            # Settlement price: 1.0 if position side wins, 0.0 if it loses
            if resolution_outcome.lower() == side:
                exit_price = 1.0
            else:
                exit_price = 0.0

            # Close the position
            closed = close_position(
                market_id=market_id,
                token_id=token_id,
                exit_price=exit_price,
                resolution=resolution_outcome,
                send_alert=send_alert,
            )

            if closed:
                # Record the P&L in daily totals
                record_trade(0, closed.get("realized_pnl", 0))
                closed_results.append({
                    "position": closed,
                    "market_question": position.get("market_question", ""),
                    "resolution": resolution_outcome,
                })

    return closed_results


def redeem_ctf_tokens(condition_id: str, outcome_index: int, amount: int) -> dict:
    """Redeem winning outcome tokens from a resolved market via CTF contract.
    
    After a market resolves, holders of winning outcome tokens can redeem them
    for USDC (1 token = 1 USDC if the outcome won).
    
    Args:
        condition_id: The market condition ID (bytes32 in hex format).
        outcome_index: The winning outcome index (0 for Yes, 1 for No typically).
        amount: Amount of tokens to redeem (in token units, not USDC).
    
    Returns:
        Dict with 'success', 'tx', and optionally 'error'.
    """
    try:
        w3 = get_web3_client()
        if w3 is None:
            return {"success": False, "error": "Web3 nicht verfügbar"}
        
        key = bot_config.get("polygon_private_key", "")
        if not key:
            return {"success": False, "error": "Kein Polygon Private Key konfiguriert"}
        
        from eth_account import Account
        key_with_prefix = key if key.startswith("0x") else f"0x{key}"
        account = Account.from_key(key_with_prefix)
        address = account.address
        
        # Check MATIC balance for gas
        matic_balance = get_matic_balance()
        if matic_balance < 0.001:
            return {
                "success": False, 
                "error": f"Nicht genug MATIC für Gas! Balance: {matic_balance:.6f} MATIC"
            }
        
        # CTF redeemPositions ABI
        # Function: redeemPositions(IERC20 collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint[] indexSets)
        ctf_redeem_abi = [
            {
                "inputs": [
                    {"name": "collateralToken", "type": "address"},
                    {"name": "parentCollectionId", "type": "bytes32"},
                    {"name": "conditionId", "type": "bytes32"},
                    {"name": "indexSets", "type": "uint256[]"}
                ],
                "name": "redeemPositions",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            }
        ]
        
        ctf_contract = w3.eth.contract(
            address=w3.to_checksum_address(POLYGON_CTF_ADDRESS),
            abi=ctf_redeem_abi
        )
        
        # Convert condition_id to bytes32
        if not condition_id.startswith("0x"):
            condition_id = f"0x{condition_id}"
        condition_bytes = bytes.fromhex(condition_id[2:].zfill(64))
        
        # Parent collection ID is typically 0x0 for Polymarket markets
        parent_collection_id = bytes(32)
        
        # Index sets: for binary markets, [1] for Yes (2^0), [2] for No (2^1)
        # For winning outcome, we redeem that index set
        index_set = 1 << outcome_index  # 2^outcome_index
        
        # Build the transaction
        nonce = w3.eth.get_transaction_count(address)
        gas_price = w3.eth.gas_price
        
        tx = ctf_contract.functions.redeemPositions(
            w3.to_checksum_address(POLYGON_USDC_ADDRESS),  # collateral token (USDC)
            parent_collection_id,
            condition_bytes,
            [index_set]  # indexSets array
        ).build_transaction({
            "from": address,
            "nonce": nonce,
            "gas": 200000,  # Estimate
            "gasPrice": gas_price,
        })
        
        # Sign and send
        signed_tx = w3.eth.account.sign_transaction(tx, key_with_prefix)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        
        logger.info(f"CTF Redemption Tx sent: {tx_hash.hex()}")
        
        # Wait for confirmation
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt["status"] == 1:
            return {"success": True, "tx": tx_hash.hex()}
        return {"success": False, "error": "Transaction reverted", "tx": tx_hash.hex()}
        
    except Exception as e:
        logger.error(f"CTF Redemption Fehler: {e}")
        return {"success": False, "error": str(e)}


def get_ctf_token_balance(token_id: str) -> int:
    """Get the CTF token balance for a specific token ID.
    
    Args:
        token_id: The token ID (position ID) to check balance for.
    
    Returns:
        Token balance as integer (0 if error or no balance).
    """
    try:
        w3 = get_web3_client()
        if w3 is None:
            return 0
        
        address = get_polygon_address()
        if not address:
            return 0
        
        # ERC1155 balanceOf ABI
        erc1155_abi = [
            {
                "constant": True,
                "inputs": [
                    {"name": "account", "type": "address"},
                    {"name": "id", "type": "uint256"}
                ],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            }
        ]
        
        ctf_contract = w3.eth.contract(
            address=w3.to_checksum_address(POLYGON_CTF_ADDRESS),
            abi=erc1155_abi
        )
        
        # Convert token_id to uint256
        token_id_int = int(token_id) if isinstance(token_id, str) else token_id
        
        balance = ctf_contract.functions.balanceOf(
            w3.to_checksum_address(address),
            token_id_int
        ).call()
        
        return balance
        
    except Exception as e:
        logger.error(f"Error getting CTF token balance: {e}")
        return 0


def check_and_settle_positions(send_alert: Callable = None) -> list:
    """Check for resolved markets and settle positions with optional token redemption.
    
    This is the main settlement tracking function that:
    1. Polls Gamma API for market resolution status
    2. Calculates P&L for resolved positions
    3. Optionally redeems winning outcome tokens via CTF contract
    4. Sends Telegram notifications with results
    5. Updates P&L tracking
    
    Args:
        send_alert: Optional callback function to send Telegram alerts.
    
    Returns:
        List of settlement results with P&L and redemption info.
    """
    global _last_settlement_check
    
    # Check if enough time has passed since last check
    current_time = time.time()
    settlement_interval = bot_config.get("settlement_check_interval", 1800)  # Default 30 min
    
    if current_time - _last_settlement_check < settlement_interval:
        # Not enough time has passed, skip this check
        return []
    
    _last_settlement_check = current_time
    logger.info("Running scheduled settlement check...")
    
    open_positions = get_open_positions()
    if not open_positions:
        logger.info("No open positions to check for settlement")
        return []
    
    settlement_results = []
    auto_redeem = bot_config.get("auto_redeem_enabled", True)
    
    for position in open_positions:
        market_id = position.get("market_id", "")
        if not market_id:
            continue
        
        market = get_market_by_id(market_id)
        if market is None:
            logger.warning(f"Could not fetch market {market_id} for settlement check")
            continue
        
        # Check if market is resolved
        is_closed = market.get("closed", False)
        resolution_outcome = market.get("resolvedOutcome") or market.get("resolved_outcome")
        
        if not (is_closed and resolution_outcome):
            continue  # Market not yet resolved
        
        # Market resolved - determine settlement price and outcome
        side = position.get("side", "").lower()
        token_id = position.get("token_id", "")
        entry_price = position.get("entry_price", 0)
        amount = position.get("amount", 0)
        market_question = position.get("market_question", "")
        
        # Calculate shares (calculate_shares handles entry_price <= 0 by returning 0)
        shares = calculate_shares(amount, entry_price)
        
        # Determine if this position won
        is_winner = resolution_outcome.lower() == side
        exit_price = 1.0 if is_winner else 0.0
        
        # Calculate P&L
        exit_value = shares * exit_price
        realized_pnl = exit_value - amount
        
        # Close the position
        closed = close_position(
            market_id=market_id,
            token_id=token_id,
            exit_price=exit_price,
            resolution=resolution_outcome,
            send_alert=send_alert,
        )
        
        result = {
            "position": closed,
            "market_question": market_question,
            "resolution": resolution_outcome,
            "is_winner": is_winner,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "amount_invested": amount,
            "shares": shares,
            "realized_pnl": realized_pnl,
            "redemption_attempted": False,
            "redemption_success": False,
            "redemption_tx": None,
        }
        
        if closed:
            # Record the P&L in daily totals
            record_trade(0, realized_pnl)
        
        # Attempt CTF token redemption for winning positions
        if is_winner and auto_redeem and not bot_config.get("dry_run", True):
            logger.info(f"Attempting to redeem winning tokens for market {market_id[:20]}...")
            
            # Get CTF token balance to see if there are tokens to redeem
            token_balance = get_ctf_token_balance(token_id)
            
            if token_balance > 0:
                result["redemption_attempted"] = True
                
                # Determine outcome index (0 for Yes, 1 for No typically)
                outcome_index = 0 if side == "yes" else 1
                
                # Get the condition_id from market data (may differ from market_id)
                # Gamma API returns 'conditionId' or 'condition_id', fallback to market_id
                condition_id = (
                    market.get("conditionId") or 
                    market.get("condition_id") or 
                    market_id
                )
                
                redemption_result = redeem_ctf_tokens(
                    condition_id=condition_id,
                    outcome_index=outcome_index,
                    amount=token_balance
                )
                
                if redemption_result.get("success"):
                    result["redemption_success"] = True
                    result["redemption_tx"] = redemption_result.get("tx")
                    logger.info(f"Successfully redeemed tokens: {redemption_result.get('tx')}")
                else:
                    logger.warning(f"Token redemption failed: {redemption_result.get('error')}")
                    result["redemption_error"] = redemption_result.get("error")
            else:
                logger.info(f"No CTF tokens to redeem for position {token_id}")
        
        # Send detailed notification
        if send_alert:
            emoji = "🎉" if is_winner else "📉"
            pnl_emoji = "📈" if realized_pnl >= 0 else "📉"
            
            msg = (
                f"{emoji} **Position Settled**\n\n"
                f"**Market:** {market_question[:80]}{'...' if len(market_question) > 80 else ''}\n\n"
                f"**Resolution:** {resolution_outcome.upper()}\n"
                f"**Your Side:** {side.upper()}\n"
                f"**Result:** {'✅ WIN' if is_winner else '❌ LOSS'}\n\n"
                f"💰 **P&L Details:**\n"
                f"• Entry Price: ${entry_price:.4f}\n"
                f"• Settlement: ${exit_price:.2f}\n"
                f"• Invested: ${amount:.2f}\n"
                f"• Shares: {shares:.4f}\n"
                f"• {pnl_emoji} Realized P&L: ${realized_pnl:+.2f}\n"
            )
            
            if result.get("redemption_attempted"):
                if result.get("redemption_success"):
                    msg += f"\n🔄 **Token Redemption:** ✅ Success\n"
                    msg += f"Tx: `{result.get('redemption_tx', 'N/A')[:20]}...`"
                else:
                    msg += f"\n🔄 **Token Redemption:** ❌ Failed\n"
                    msg += f"Error: {result.get('redemption_error', 'Unknown')[:50]}"
            
            send_alert(msg)
        
        settlement_results.append(result)
    
    if settlement_results:
        total_pnl = sum(r.get("realized_pnl", 0) for r in settlement_results)
        wins = sum(1 for r in settlement_results if r.get("is_winner"))
        losses = len(settlement_results) - wins
        
        logger.info(
            f"Settlement check complete: {len(settlement_results)} positions settled. "
            f"Wins: {wins}, Losses: {losses}, Total P&L: ${total_pnl:+.2f}"
        )
    
    return settlement_results


def force_settlement_check(send_alert: Callable = None) -> list:
    """Force an immediate settlement check regardless of the interval.
    
    This bypasses the settlement interval check and runs immediately.
    Useful for manual triggering via Telegram command.
    
    Args:
        send_alert: Optional callback function to send Telegram alerts.
    
    Returns:
        List of settlement results with P&L info.
    """
    global _last_settlement_check
    
    # Reset the last check time to force an immediate check
    _last_settlement_check = 0
    
    return check_and_settle_positions(send_alert=send_alert)



    """Sell/exit a position by posting a sell order at market midpoint.

    Args:
        position: The position dict from the positions store.

    Returns:
        Dict with success status and order details.
    """
    clob = _build_clob_client()
    if clob is None:
        return {"success": False, "error": "CLOB client not initialized"}

    token_id = position.get("token_id", "")
    if not token_id:
        return {"success": False, "error": "No token_id in position"}

    try:
        # Get current midpoint price
        mid = clob.get_midpoint(token_id)
        exit_price = float(mid) if mid else 0.5

        # Calculate shares to sell using helper function
        entry_price = position.get("entry_price", 0)
        amount = position.get("amount", 0)
        shares = calculate_shares(amount, entry_price)

        if shares <= 0:
            return {"success": False, "error": "No shares to sell"}

        # Create sell order at midpoint
        order = clob.create_order(
            token_id=token_id,
            price=exit_price,
            side="sell",
            size=shares,
        )
        signed_order = clob.sign_order(order)
        resp = clob.post_order(signed_order)

        # Check if order was accepted
        # The CLOB API typically returns a dict with orderID on success
        order_success = False
        if resp is not None:
            if isinstance(resp, dict):
                # Check for common success indicators
                order_success = "orderID" in resp or "id" in resp or resp.get("success", False)
            else:
                # Non-dict response, assume success if not None
                order_success = True

        if not order_success:
            logger.warning(f"Sell order may not have been accepted for token {token_id}: {resp}")

        # Close the position in our store
        market_id = position.get("market_id", "")
        closed = close_position(
            market_id=market_id,
            token_id=token_id,
            exit_price=exit_price,
            resolution=None,  # Manual exit, not market resolution
        )

        if closed:
            record_trade(0, closed.get("realized_pnl", 0))

        return {
            "success": True,
            "response": resp,
            "exit_price": exit_price,
            "shares_sold": shares,
            "realized_pnl": closed.get("realized_pnl", 0) if closed else 0,
            "order_posted": order_success,
        }

    except Exception as e:
        # Invalidate cache on authentication errors (401/403)
        if _is_auth_error(e):
            invalidate_l2_credentials_cache()
        return {"success": False, "error": str(e)}


def check_prediction_flip_and_exit(current_prediction: str, send_notification) -> list:
    """Check if prediction has flipped and exit positions accordingly.

    If the bot's prediction changes direction (e.g., from 'up' to 'down'),
    positions that are now against the prediction should be sold.

    Args:
        current_prediction: Current prediction ('up' or 'down').
        send_notification: Callback to send Telegram notification.

    Returns:
        List of closed position results.
    """
    open_positions = get_open_positions()
    if not open_positions:
        return []

    closed_results = []

    for position in open_positions:
        side = position.get("side", "").lower()

        # Determine if position is against current prediction
        # 'up' prediction means YES is good, NO should be exited
        # 'down' prediction means NO is good, YES should be exited
        should_exit = False
        if current_prediction == "up" and side == "no":
            should_exit = True
        elif current_prediction == "down" and side == "yes":
            should_exit = True

        if should_exit:
            logger.info(
                f"Prediction flipped to {current_prediction}, "
                f"exiting {side.upper()} position"
            )

            result = sell_position(position)

            if result.get("success"):
                closed_results.append({
                    "position": position,
                    "exit_price": result.get("exit_price"),
                    "realized_pnl": result.get("realized_pnl"),
                    "reason": "prediction_flip",
                })
                send_notification(
                    f"🔄 **Position Exited (Prediction Flip)**\n\n"
                    f"Market: {position.get('market_question', 'N/A')[:50]}...\n"
                    f"Side: {side.upper()}\n"
                    f"Exit Price: ${result.get('exit_price', 0):.4f}\n"
                    f"P&L: ${result.get('realized_pnl', 0):.2f}"
                )
            else:
                logger.error(f"Failed to exit position: {result.get('error')}")

    return closed_results


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

    # Track previous prediction for flip detection
    previous_prediction = None

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

            # Automatic position settlement tracking (every 30 minutes by default)
            # Polls Gamma API for market resolution, calculates P&L, and optionally redeems tokens
            try:
                check_and_settle_positions(send_alert=send_notification)
            except Exception as e:
                logger.error(f"Error in settlement check: {e}")

            # Get prediction with multi-signal confidence
            pred = get_current_prediction()
            prediction = pred.get("prediction", "hold")
            confidence = pred.get("confidence", 0)
            meets_threshold = pred.get("meets_threshold", False)
            min_threshold = pred.get("min_threshold", 68)

            if prediction == "hold":
                # Skip the cycle but don't update previous_prediction
                # This way, "hold" states are transparent to flip detection
                logger.info("Not enough data - skipping trade")
                wait_with_check(bot_config.get("cycle_interval_seconds", 300))
                continue

            # Log signal details
            signals = pred.get("signals", {})
            logger.info(
                f"Prediction: {prediction.upper()} | Confidence: {confidence:.1f}% | "
                f"Threshold: {min_threshold}% | Trade: {'YES' if meets_threshold else 'NO'}"
            )

            # Check for prediction flip and exit positions if needed
            # Note: Because "hold" states are skipped above, previous_prediction
            # always holds the last directional prediction ("up" or "down").
            # Transitions like "up" → "hold" → "down" trigger exits when "down" arrives.
            if not bot_config.get("dry_run", True):
                if previous_prediction is not None and previous_prediction != prediction:
                    logger.info(f"Prediction flipped: {previous_prediction} → {prediction}")
                    try:
                        check_prediction_flip_and_exit(prediction, send_notification)
                    except Exception as e:
                        logger.error(f"Error during prediction flip exit: {e}")

            previous_prediction = prediction

            # Check if confidence meets threshold for trading
            if not meets_threshold:
                logger.info(
                    f"Confidence {confidence:.1f}% below threshold {min_threshold}% - skipping trade"
                )
                # Send notification about skipped trade
                logreg_info = pred.get("logreg_prediction")
                logreg_text = ""
                if logreg_info:
                    logreg_text = f"\n• LogReg: {logreg_info.get('direction', 'N/A').upper()} ({logreg_info.get('confidence', 0):.0f}%)"
                send_notification(
                    f"⏸️ **Trade Skipped - Low Confidence**\n\n"
                    f"Prediction: {prediction.upper()}\n"
                    f"Confidence: {confidence:.1f}%\n"
                    f"Required: ≥{min_threshold}%\n\n"
                    f"📊 **Signals:**\n"
                    f"• MA: {signals.get('ma_crossover', {}).get('direction', 'N/A').upper()}\n"
                    f"• RSI: {signals.get('rsi', {}).get('rsi', 0):.1f}\n"
                    f"• MACD: {signals.get('macd', {}).get('direction', 'N/A').upper()}"
                    f"{logreg_text}"
                )
                wait_with_check(bot_config.get("cycle_interval_seconds", 300))
                continue

            # Check risk limits before looking for markets (for live trading only)
            if not bot_config.get("dry_run", True):
                trade_amount = bot_config.get("trade_amount", 5.0)
                risk_check = check_risk_limits(
                    trade_amount=trade_amount,
                    send_alert=send_notification,
                )
                if not risk_check.get("allowed", True):
                    logger.info(f"Risk limit hit: {risk_check.get('reason', 'Unknown')}")
                    wait_with_check(bot_config.get("cycle_interval_seconds", 300))
                    continue

            # Find top 3 mispriced markets (crypto, politics, economics)
            markets = find_relevant_markets(count=8, min_deviation_pct=12.0)[:3]

            if not markets:
                logger.info("No mispriced markets found")
                wait_with_check(bot_config.get("cycle_interval_seconds", 300))
                continue

            # Execute trades with confidence + deviation_pct bonus
            for market in markets:
                # Get deviation data for extra confidence
                price_deviation = market.get("price_deviation", {})
                deviation_pct = abs(price_deviation.get("deviation_pct", 0))
                direction = price_deviation.get("direction", "unknown")
                category = market.get("category", "other")
                current_price = price_deviation.get("current_price", 0.5)
                historical_mean = price_deviation.get("historical_mean", 0.5)

                # Calculate total confidence (base confidence + deviation bonus)
                # Formula: deviation_pct / 2.0, capped at 15%
                # Example: 30% deviation → 15% bonus (max), 20% deviation → 10% bonus
                # This rewards trading in highly mispriced markets while limiting risk
                deviation_bonus = min(15.0, deviation_pct / 2.0)
                total_confidence = min(100.0, confidence + deviation_bonus)

                # Build LogReg info string
                logreg_info = pred.get("logreg_prediction")
                logreg_text = ""
                if logreg_info:
                    logreg_text = f"\n• LogReg: {logreg_info.get('direction', 'N/A').upper()} ({logreg_info.get('confidence', 0):.0f}%)"

                if bot_config.get("dry_run", True):
                    outcome = "YES" if prediction == "up" else "NO"
                    # Calculate Kelly size for dry run display (no config persistence)
                    current_balance = get_polygon_balance()
                    kelly_size = kelly_position_size(
                        confidence=total_confidence,
                        balance=current_balance,
                        deviation_pct=deviation_pct,
                    )
                    
                    logger.info(f"[Dry run] Would buy {outcome} on {market.get('question')}")
                    send_notification(
                        f"📊 **Dry Run Trade**\n"
                        f"Market: {market.get('question', 'N/A')[:50]}...\n"
                        f"🏷️ Category: {category.capitalize()}\n"
                        f"Prediction: {prediction.upper()}\n"
                        f"Base Confidence: {confidence:.1f}%\n"
                        f"📈 Deviation: {deviation_pct:.1f}% ({direction})\n"
                        f"🎯 Total Confidence: {total_confidence:.1f}%\n"
                        f"Would buy: {outcome}\n"
                        f"💰 Kelly Size: ${kelly_size:.2f} (Balance: ${current_balance:.2f})\n\n"
                        f"📊 **Market Data:**\n"
                        f"• Current Price: {current_price:.2%}\n"
                        f"• Historical Mean: {historical_mean:.2%}\n\n"
                        f"📊 **Signals:**\n"
                        f"• MA: {signals.get('ma_crossover', {}).get('direction', 'N/A').upper()} "
                        f"({signals.get('ma_crossover', {}).get('strength', 0):.0f}%)\n"
                        f"• RSI: {signals.get('rsi', {}).get('rsi', 0):.1f} "
                        f"({signals.get('rsi', {}).get('direction', 'N/A').upper()})\n"
                        f"• MACD: {signals.get('macd', {}).get('direction', 'N/A').upper()} "
                        f"(hist: {signals.get('macd', {}).get('histogram', 0):.2f})\n"
                        f"• PM Delta: {signals.get('polymarket_delta', {}).get('delta', 0):.3f}"
                        f"{logreg_text}"
                    )
                else:
                    outcome = "yes" if prediction == "up" else "no"
                    
                    # Calculate Kelly-optimized position size
                    current_balance = get_polygon_balance()
                    kelly_size = kelly_position_size(
                        confidence=total_confidence,
                        balance=current_balance,
                        deviation_pct=deviation_pct,
                    )
                    # Store Kelly-calculated size in config (overrides fixed value)
                    bot_config["trade_amount"] = kelly_size
                    
                    result = place_trade(
                        market,
                        outcome=outcome,
                        amount=kelly_size,
                        send_alert=send_notification,
                    )

                    if result.get("success"):
                        send_notification(
                            f"✅ **Trade Executed**\n"
                            f"Market: {market.get('question', 'N/A')[:50]}...\n"
                            f"🏷️ Category: {category.capitalize()}\n"
                            f"Side: {outcome.upper()}\n"
                            f"Base Confidence: {confidence:.1f}%\n"
                            f"📈 Deviation: {deviation_pct:.1f}% ({direction})\n"
                            f"🎯 Total Confidence: {total_confidence:.1f}%\n"
                            f"💰 Kelly Size: ${kelly_size:.2f} (Balance: ${current_balance:.2f})\n"
                            f"Price: {result.get('price', 'N/A')}"
                            f"{logreg_text}"
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
            InlineKeyboardButton("⛓️ Approvals", callback_data="setup_approvals"),
            InlineKeyboardButton("⛽ Gas", callback_data="gas_status"),
        ],
        [
            InlineKeyboardButton("📜 P&L", callback_data="pnl"),
            InlineKeyboardButton("📊 Positions", callback_data="positions"),
        ],
        [
            InlineKeyboardButton("🛡️ Risk", callback_data="risk"),
            InlineKeyboardButton("❓ Hilfe", callback_data="help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    status = "🟢 Läuft" if bot_config.get("bot_running", False) else "🔴 Gestoppt"
    mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live"
    onchain = "⛓️ 100% Onchain"

    # Check risk status
    risk_state = load_risk_state()
    risk_status = "🔴 PAUSED" if risk_state.get("circuit_breaker_paused") else "🟢 OK"

    await update.message.reply_text(
        f"🎰 **UpDown Trading Bot**\n\n"
        f"Status: {status}\n"
        f"Modus: {mode} | {onchain}\n"
        f"Risk: {risk_status}\n\n"
        f"Steuere deinen Polymarket Prediction Bot komplett via Telegram.\n\n"
        f"Wähle eine Option:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    help_text = """
🎰 **UpDown Bot Commands (100% Onchain-Modus)**

**Bot Control:**
/start - Hauptmenü
/status - Bot-Status anzeigen
/start_bot - Trading-Loop starten
/stop_bot - Trading-Loop stoppen

**Wallet Setup:**
/set_solana_key - Solana Private Key setzen
/set_polygon_key - Polygon Private Key setzen

**Onchain Setup (NEU):**
/setup_approvals - USDC/CTF Approvals setzen ⛓️
/gas_status - MATIC-Balance anzeigen ⛽

**Trading:**
/balance - Alle Balances anzeigen
/predict - Multi-Signal Vorhersage anzeigen 📈
/markets - Top mispriced Märkte finden 📈
/scan - Alle Märkte scannen 🔎 (Top 8 Mispriced, ≥12% Deviation)
/politics_scan - Politics/Economics Scan 🏛️ (vol24h>$150k, <48h Settlement)
/trade - Manueller Trade (Top 3 Mispriced)
/bridge - Solana→Polygon Bridge

**Position Tracking:**
/positions - Offene Positionen anzeigen 📊
  (Auto-Exit bei Prediction-Flip, Auto-P&L bei Resolution)

**Settlement Tracking:** 📊
/settlement - Settlement-Tracking Status
/settlement status - Detaillierter Status
/settlement check - Sofortiger Check auf resolved Markets
/settlement interval <min> - Check-Intervall setzen (Standard: 30 Min)
/settlement redeem on|off - Auto Token-Redemption

**Risk Management:** 🛡️
/risk - Risiko-Status anzeigen
/risk reset - Circuit Breaker zurücksetzen
/risk set max_daily_loss <USD> - Max. Tagesverlust
/risk set max_position_size <PCT> - Max. Position (% Balance)
/risk set max_positions <N> - Max. gleichzeitige Positionen
/risk set circuit_breaker <N> - Pause nach N Verlusten
/risk help - Risiko-Hilfe

**Configuration:**
/config - Einstellungen anzeigen/ändern
/set_trade_amount - Trade-Größe setzen
/set_min_balance - Min. Polygon-Balance setzen
/set_bridge_amount - Bridge-Betrag setzen
/set_interval - Zyklus-Intervall setzen
/toggle_dry_run - Dry Run Modus umschalten
/toggle_onchain - Onchain-Modus umschalten

**Multi-Signal Settings:**
/set_confidence_threshold - Min. Konfidenz setzen (0-100)
/set_rsi_params - RSI Parameter (period overbought oversold)
/set_macd_params - MACD Parameter (fast slow signal)

**Backtesting:**
/backtest - 30-Tage Backtest mit 1000 Trades 📈
  (Zeigt: Winrate, Profit-Faktor, Max-Drawdown, Sharpe-Ratio)

**Info:**
/pnl - P&L Historie anzeigen
/help - Diese Hilfe

📊 **Multi-Signal Trading Engine:**
Trades werden nur ausgeführt wenn Konfidenz ≥ Schwellenwert.
Signale: MA Crossover + RSI(14) + MACD + Polymarket Delta

⚙️ **Auto Settlement:**
Alle 30 Min werden Positionen auf Resolution geprüft.
Bei Resolution: P&L berechnet, Token eingelöst, Notification gesendet.

⚠️ **Sicherheitshinweise:**
- Private Keys werden nur im Speicher gehalten
- Niemals Bot-Token oder Keys teilen
- Nutze eine dedizierte Trading-Wallet
- Onchain-Transaktionen kosten MATIC!
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
    matic_bal = get_matic_balance()

    # Get prediction
    pred = get_current_prediction()

    # Get P&L
    pnl = load_pnl()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_pnl = pnl.get("daily", {}).get(today, {"trades": 0, "profit": 0.0})

    status = "🟢 Running" if bot_config.get("bot_running", False) else "🔴 Stopped"
    mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live Trading"
    onchain = "⛓️ Onchain" if bot_config.get("onchain_mode", True) else "🔌 API"

    sol_configured = "✅" if bot_config.get("solana_private_key") else "❌"
    poly_configured = "✅" if bot_config.get("polygon_private_key") else "❌"
    approvals = "✅" if bot_config.get("approvals_done", False) else "❌"

    # MATIC-Warnung wenn niedrig
    matic_warning = "⚠️ NIEDRIG!" if matic_bal < 0.1 else ""
    
    # Get multi-signal info
    confidence = pred.get("confidence", 0)
    meets_threshold = pred.get("meets_threshold", False)
    min_threshold = bot_config.get("min_confidence_threshold", 68)
    
    confidence_status = "✅ WILL TRADE" if meets_threshold else f"⏸️ Below {min_threshold}%"

    text = f"""
📊 **Bot Status (100% Onchain)**

**State:** {status}
**Mode:** {mode} | {onchain}

**Wallets:**
Solana: {sol_configured} {f"${sol_bal.get('usdc', 0):.2f} USDC | {sol_bal.get('sol', 0):.4f} SOL" if sol_bal else "Nicht konfiguriert"}
Polygon: {poly_configured} {f"${poly_bal:.2f} USDC" if poly_bal else "Nicht konfiguriert"}
MATIC: ⛽ {matic_bal:.4f} MATIC {matic_warning}
Approvals: {approvals}

**Multi-Signal Prediction:**
Asset: {pred.get('crypto', 'N/A').upper()}
Preis: ${pred.get('price', 0):,.2f}
Richtung: {pred.get('prediction', 'N/A').upper()}
Konfidenz: {confidence:.1f}% ({confidence_status})

**Heutiges P&L:**
Trades: {daily_pnl.get('trades', 0)}
Profit: ${daily_pnl.get('profit', 0):.2f}
Total: ${pnl.get('total', 0):.2f}

**Config:**
Trade Amount: ${bot_config.get('trade_amount', 5.0)}
Cycle: {bot_config.get('cycle_interval_seconds', 300)}s
Min Confidence: {min_threshold}%
Auto MATIC: {'✅' if bot_config.get('auto_matic_topup_enabled', True) else '❌'}
"""
    
    # Add Live vs Backtest comparison
    backtest_results = load_backtest_results()
    comparison_text = format_live_vs_backtest_comparison(pnl, backtest_results)
    text += comparison_text
    
    await update.message.reply_text(text, parse_mode="Markdown")


async def backtest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /backtest command.
    
    Runs a backtest simulation using 30 days of CoinGecko data and 1000 simulated trades.
    Displays: Winrate, Profit-Factor, Max-Drawdown, Sharpe-Ratio.
    Results are saved to daily_pnl.json for comparison in /status.
    """
    if not is_authorized(update):
        await unauthorized_response(update)
        return
    
    await update.message.reply_text(
        "📊 **Backtest gestartet...**\n\n"
        "⏳ Lade 30 Tage CoinGecko Daten...\n"
        "🔄 Simuliere 1000 Trades mit Multi-Signal-Engine...\n"
        "Dies kann einige Sekunden dauern.",
        parse_mode="Markdown"
    )
    
    try:
        # Get crypto ID from config
        crypto_id = bot_config.get("crypto_id", "bitcoin")
        min_confidence = bot_config.get("min_confidence_threshold", 68)
        
        # Build config for backtest engine
        backtest_config = {
            "signal_weights": bot_config.get("signal_weights", {
                "ma_crossover": 30,
                "rsi": 30,
                "macd": 25,
                "momentum": 15,
            }),
            "short_window": bot_config.get("short_window", 5),
            "long_window": bot_config.get("long_window", 20),
            "rsi_overbought": bot_config.get("rsi_overbought", 70),
            "rsi_oversold": bot_config.get("rsi_oversold", 30),
        }
        
        # Run backtest
        result = run_backtest(
            crypto_id=crypto_id,
            max_trades=1000,
            min_confidence=min_confidence,
            config=backtest_config,
        )
        
        if result is None:
            await update.message.reply_text(
                "❌ **Backtest fehlgeschlagen**\n\n"
                "Konnte keine Daten von CoinGecko abrufen.\n"
                "Bitte später erneut versuchen.",
                parse_mode="Markdown"
            )
            return
        
        # Save results to daily_pnl.json
        save_backtest_results(result)
        
        # Format and send results
        result_text = format_backtest_results(result)
        
        # Add recommendation based on metrics
        recommendations = []
        if result.winrate >= 55:
            recommendations.append("✅ Winrate > 55%: Strategie zeigt positive Trefferquote")
        elif result.winrate < 50:
            recommendations.append("⚠️ Winrate < 50%: Strategie benötigt Optimierung")
        
        if result.profit_factor >= 1.5:
            recommendations.append("✅ Profit Factor > 1.5: Gutes Gewinn/Verlust-Verhältnis")
        elif result.profit_factor < 1.0:
            recommendations.append("⚠️ Profit Factor < 1.0: Verluste überwiegen")
        
        if result.max_drawdown <= 15:
            recommendations.append("✅ Max Drawdown ≤ 15%: Akzeptables Risiko")
        elif result.max_drawdown > 25:
            recommendations.append("⚠️ Max Drawdown > 25%: Hohes Risiko")
        
        if result.sharpe_ratio >= 1.0:
            recommendations.append("✅ Sharpe > 1.0: Gute risikoadjustierte Rendite")
        elif result.sharpe_ratio < 0.5:
            recommendations.append("⚠️ Sharpe < 0.5: Geringe risikoadjustierte Rendite")
        
        if recommendations:
            result_text += "\n**Empfehlungen:**\n" + "\n".join(recommendations)
        
        result_text += "\n\n_Ergebnisse gespeichert. Siehe /status für Live vs Backtest Vergleich._"
        
        await update.message.reply_text(result_text, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        await update.message.reply_text(
            f"❌ **Backtest Fehler**\n\n"
            f"Ein Fehler ist aufgetreten: {str(e)[:200]}",
            parse_mode="Markdown"
        )


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

    await update.message.reply_text("🔮 Analyzing market with multi-signal engine...")

    pred = get_current_prediction()

    emoji = "📈" if pred.get("prediction") == "up" else "📉" if pred.get("prediction") == "down" else "⏸️"
    confidence = pred.get("confidence", 0)
    meets_threshold = pred.get("meets_threshold", False)
    min_threshold = pred.get("min_threshold", 68)
    
    # Get signal details
    signals = pred.get("signals", {})
    ma_signal = signals.get("ma_crossover", {})
    rsi_signal = signals.get("rsi", {})
    macd_signal = signals.get("macd", {})
    pm_signal = signals.get("polymarket_delta", {})
    logreg_pred = pred.get("logreg_prediction")
    
    # Confidence bar visualization
    confidence_bars = "█" * int(confidence / 10) + "░" * (10 - int(confidence / 10))
    threshold_marker = "▼" if meets_threshold else "▽"
    
    trade_status = "✅ WILL TRADE" if meets_threshold else "⏸️ NO TRADE (low confidence)"
    
    # Build LogReg section
    logreg_text = ""
    if logreg_pred:
        logreg_text = f"""
• **LogReg Fallback** (7-day trained)
  Prediction: {logreg_pred.get('direction', 'N/A').upper()}
  Confidence: {logreg_pred.get('confidence', 0):.0f}%
  Probability: {logreg_pred.get('probability', 0.5):.2%}
"""

    text = f"""
{emoji} **Multi-Signal Prediction**

**Asset:** {pred.get('crypto', 'N/A').upper()}
**Current Price:** ${pred.get('price', 0):,.2f}
**Prediction:** {pred.get('prediction', 'N/A').upper()}
**Data Points:** {pred.get('candles', 0)} candles

📊 **Confidence Score**
[{confidence_bars}] {confidence:.1f}%
Threshold: {min_threshold}% {threshold_marker}
Status: {trade_status}

📈 **Signal Details:**

• **MA Crossover** ({bot_config.get('short_window', 5)}/{bot_config.get('long_window', 20)}) - 30%
  Direction: {ma_signal.get('direction', 'N/A').upper()}
  Strength: {ma_signal.get('strength', 0):.0f}%
  Short MA: ${ma_signal.get('ma_short', 0):,.2f}
  Long MA: ${ma_signal.get('ma_long', 0):,.2f}

• **RSI** (Period: {bot_config.get('rsi_period', 14)}) - 30%
  Value: {rsi_signal.get('rsi', 0):.1f}
  Direction: {rsi_signal.get('direction', 'N/A').upper()}
  Strength: {rsi_signal.get('strength', 0):.0f}%
  (Overbought: >{bot_config.get('rsi_overbought', 70)}, Oversold: <{bot_config.get('rsi_oversold', 30)})

• **MACD** ({bot_config.get('macd_fast_period', 12)}/{bot_config.get('macd_slow_period', 26)}/{bot_config.get('macd_signal_period', 9)}) - 25%
  Histogram: {macd_signal.get('histogram', 0):.2f}
  Direction: {macd_signal.get('direction', 'N/A').upper()}
  Strength: {macd_signal.get('strength', 0):.0f}%

• **Polymarket Delta** - 15%
  Market Price: {pred.get('polymarket_price', 0.5):.2%}
  Fair Value: {pred.get('fair_value', 0.5):.2%}
  Delta: {pm_signal.get('delta', 0):.3f}
  Direction: {pm_signal.get('direction', 'N/A').upper()}
{logreg_text}"""
    await update.message.reply_text(text, parse_mode="Markdown")


async def markets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /markets command - Show top mispriced markets."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    await update.message.reply_text("🔍 Scanning for top mispriced markets...")

    markets = find_relevant_markets(count=8, min_deviation_pct=12.0)

    if not markets:
        await update.message.reply_text(
            "No mispriced markets found with ≥12% deviation.\n"
            "Try /scan for a broader market overview."
        )
        return

    text = f"📈 **Top {len(markets)} Mispriced Market(s)**\n"
    text += f"_(Categories: crypto, politics, economics)_\n\n"

    for i, market in enumerate(markets[:5], 1):
        question = market.get('question', 'N/A')[:60]
        category = market.get('category', 'other')
        deviation = market.get('price_deviation', {})
        dev_pct = deviation.get('deviation_pct', 0)
        direction = deviation.get('direction', 'unknown')
        current_price = deviation.get('current_price', 0.5)

        emoji = get_deviation_emoji(direction)

        text += f"**{i}. {question}...**\n"
        text += f"   {emoji} {direction.upper()} by {abs(dev_pct):.1f}%\n"
        text += f"   📊 Price: {current_price:.2%} | 🏷️ {category.capitalize()}\n"
        text += f"   ID: `{market.get('id', 'N/A')[:20]}...`\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /scan command - Scan ALL active markets for mispriced opportunities.

    This command fetches all active Polymarket markets with volume > $10k,
    categorizes them, and identifies the top 8 markets where current price
    deviates ≥12% from historical mean.
    """
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    await update.message.reply_text("🔍 Scanning all active markets... This may take a moment.")

    try:
        # Scan all markets once and reuse for both mispriced detection and summary
        all_markets = scan_all_markets()
        summary = get_category_summary(all_markets)

        # Get top 8 mispriced markets with >= 12% deviation using scanner
        mispriced = get_top_mispriced_markets(count=8, min_deviation_pct=12.0)

        if not mispriced:
            # If no mispriced markets found, show category summary
            text = "📊 **Market Scan Complete**\n\n"
            text += "No significantly mispriced markets found (≥12% deviation).\n\n"
            text += "**Markets by Category:**\n"
            for cat, count in sorted(summary.items(), key=lambda x: -x[1]):
                text += f"• {cat.capitalize()}: {count}\n"
            text += f"\n**Total:** {len(all_markets)} active markets (volume > $10k)"

            await update.message.reply_text(text, parse_mode="Markdown")
            return

        # Format and send results
        result_text = format_scan_results(mispriced)

        # Add category summary at the end (reuse already scanned data)
        result_text += "\n\n**📊 All Markets by Category:**\n"
        for cat, count in sorted(summary.items(), key=lambda x: -x[1]):
            result_text += f"• {cat.capitalize()}: {count}\n"
        result_text += f"\n**Total:** {len(all_markets)} active markets"

        await update.message.reply_text(result_text, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error in scan_command: {e}")
        await update.message.reply_text(
            f"❌ Error scanning markets: {str(e)[:100]}",
            parse_mode="Markdown",
        )


async def politics_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /politics_scan command - Scan for high-value politics/economics markets.

    This command focuses on politics, crypto, and economics markets with:
    - 24h volume > $150k
    - Settlement within 48 hours (fast settlement = less risk)
    - Prioritizes politics markets with >8% deviation (often irrational Trump/Biden/News moves)
    """
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    await update.message.reply_text(
        "🏛️ Scanning politics/economics markets...\n"
        "Filters: volume24h > $150k, settlement < 48h, prioritizing politics"
    )

    try:
        # Get markets with strict filters for high-value fast-settling opportunities
        mispriced = get_top_mispriced_markets(
            count=10,
            min_deviation_pct=5.0,  # Lower threshold for high-volume markets
            min_volume_24h=150_000,  # Only markets with > $150k 24h volume
            categories=["crypto", "politics", "economics"],
            max_hours_to_settlement=48,  # Fast settlement within 48h
            prioritize_politics=True,  # Politics with >8% deviation first
        )

        if not mispriced:
            await update.message.reply_text(
                "🏛️ **Politics/Economics Scan Complete**\n\n"
                "No markets found matching criteria:\n"
                "• Categories: crypto, politics, economics\n"
                "• 24h volume > $150k\n"
                "• Settlement within 48h\n\n"
                "Try /scan for a broader market overview.",
                parse_mode="Markdown",
            )
            return

        # Format results with special politics indicator
        lines = ["🏛️ **High-Value Politics/Economics Markets**\n"]
        lines.append("_Filters: vol24h > $150k, settlement < 48h_\n")

        for i, market in enumerate(mispriced, 1):
            question = market.get("question", "Unknown")[:55]
            category = market.get("category", "other")
            deviation = market.get("price_deviation", {})

            current = deviation.get("current_price", 0)
            dev_pct = deviation.get("deviation_pct", 0)
            direction = deviation.get("direction", "unknown")

            # Highlight politics markets with high deviation
            is_priority = category == "politics" and abs(dev_pct) > 8.0
            priority_marker = "🔥" if is_priority else ""

            emoji = "📉" if direction == "underpriced" else "📈"
            cat_emoji = {"politics": "🏛️", "economics": "📊", "crypto": "₿"}.get(category, "📌")

            lines.append(f"**{i}. {question}...** {priority_marker}")
            lines.append(f"   {emoji} {direction.upper()} by {abs(dev_pct):.1f}%")
            lines.append(f"   📊 Price: {current:.2%}")
            lines.append(f"   {cat_emoji} {category.capitalize()}")
            lines.append("")

        lines.append("_🔥 = Politics high-deviation (often irrational)_")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error in politics_scan_command: {e}")
        await update.message.reply_text(
            f"❌ Error scanning markets: {str(e)[:100]}",
            parse_mode="Markdown",
        )


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


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /positions command - Show all open positions."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    positions_data = load_positions()
    open_positions = positions_data.get("open", [])
    closed_positions = positions_data.get("closed", [])

    if not open_positions:
        # Show summary even if no open positions
        total_closed = len(closed_positions)
        total_pnl = sum(p.get("realized_pnl", 0) for p in closed_positions)
        await update.message.reply_text(
            f"📊 **Positions**\n\n"
            f"No open positions.\n\n"
            f"**History:**\n"
            f"Closed positions: {total_closed}\n"
            f"Total realized P&L: ${total_pnl:.2f}",
            parse_mode="Markdown",
        )
        return

    text = f"📊 **Open Positions ({len(open_positions)})**\n\n"

    clob = _build_clob_client()

    for i, pos in enumerate(open_positions, 1):
        market_question = pos.get("market_question", "Unknown")[:40]
        side = pos.get("side", "?").upper()
        entry_price = pos.get("entry_price", 0)
        amount = pos.get("amount", 0)
        timestamp = parse_position_date(pos.get("timestamp", ""))

        # Try to get current price for P&L calculation
        current_price = entry_price
        unrealized_pnl = 0.0
        if clob:
            try:
                mid = clob.get_midpoint(pos.get("token_id", ""))
                if mid:
                    current_price = float(mid)
                    shares = calculate_shares(amount, entry_price)
                    unrealized_pnl = (current_price - entry_price) * shares
            except Exception:
                pass

        text += f"**{i}. {side}** - {market_question}...\n"
        text += f"   Entry: ${entry_price:.4f} | Current: ${current_price:.4f}\n"
        text += f"   Amount: ${amount:.2f} | Unrealized P&L: ${unrealized_pnl:.2f}\n"
        text += f"   Opened: {timestamp}\n\n"

    # Add summary
    total_invested = sum(p.get("amount", 0) for p in open_positions)
    text += f"**Total Invested:** ${total_invested:.2f}\n"

    # Recent closed positions
    recent_closed = closed_positions[-3:] if closed_positions else []
    if recent_closed:
        text += f"\n**Recent Closed:**\n"
        for pos in reversed(recent_closed):
            side = pos.get("side", "?").upper()
            pnl = pos.get("realized_pnl", 0)
            emoji = "✅" if pnl >= 0 else "❌"
            text += f"{emoji} {side}: ${pnl:.2f}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def settlement_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settlement command - Check and settle resolved positions."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    args = context.args if context.args else []

    # Handle subcommands
    if args and args[0].lower() == "status":
        # Show settlement tracking status
        interval = bot_config.get("settlement_check_interval", 1800)
        auto_redeem = bot_config.get("auto_redeem_enabled", True)
        dry_run = bot_config.get("dry_run", True)
        
        # Calculate time since last check
        time_since_check = time.time() - _last_settlement_check
        next_check_in = max(0, interval - time_since_check)
        
        open_positions = get_open_positions()
        
        await update.message.reply_text(
            f"⚙️ **Settlement Tracking Status**\n\n"
            f"**Configuration:**\n"
            f"• Check Interval: {interval // 60} minutes\n"
            f"• Auto Redeem Tokens: {'✅ Enabled' if auto_redeem else '❌ Disabled'}\n"
            f"• Mode: {'🔴 Dry Run' if dry_run else '🟢 Live'}\n\n"
            f"**Current State:**\n"
            f"• Open Positions: {len(open_positions)}\n"
            f"• Last Check: {int(time_since_check // 60)}m {int(time_since_check % 60)}s ago\n"
            f"• Next Check In: {int(next_check_in // 60)}m {int(next_check_in % 60)}s\n\n"
            f"**Commands:**\n"
            f"`/settlement check` - Force immediate check\n"
            f"`/settlement interval <minutes>` - Set check interval\n"
            f"`/settlement redeem on|off` - Toggle auto redemption",
            parse_mode="Markdown",
        )
        return

    if args and args[0].lower() == "check":
        # Force immediate settlement check
        await update.message.reply_text(
            "🔍 **Checking for resolved markets...**\n\n"
            "This may take a moment.",
            parse_mode="Markdown",
        )

        # Create a notification callback for this user
        async def send_update(text: str):
            try:
                await update.message.reply_text(text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send settlement update: {e}")

        # Run settlement check - use synchronous call since we're in async context
        # The notification callback will schedule async updates
        loop = asyncio.get_event_loop()
        
        def sync_alert(text: str):
            """Sync wrapper for async notification with error handling."""
            future = asyncio.run_coroutine_threadsafe(send_update(text), loop)
            try:
                # Wait for result with timeout to catch errors
                future.result(timeout=10)
            except Exception as e:
                logger.error(f"Error sending settlement notification: {e}")

        try:
            results = force_settlement_check(send_alert=sync_alert)
            
            if not results:
                await update.message.reply_text(
                    "✅ **Settlement Check Complete**\n\n"
                    "No markets have resolved since last check.\n"
                    "All open positions are still active.",
                    parse_mode="Markdown",
                )
            else:
                total_pnl = sum(r.get("realized_pnl", 0) for r in results)
                wins = sum(1 for r in results if r.get("is_winner"))
                losses = len(results) - wins
                
                await update.message.reply_text(
                    f"✅ **Settlement Check Complete**\n\n"
                    f"**Settled:** {len(results)} position(s)\n"
                    f"**Wins:** {wins} | **Losses:** {losses}\n"
                    f"**Total P&L:** ${total_pnl:+.2f}",
                    parse_mode="Markdown",
                )
        except Exception as e:
            await update.message.reply_text(
                f"❌ **Settlement Check Failed**\n\n"
                f"Error: {str(e)[:200]}",
                parse_mode="Markdown",
            )
        return

    if args and args[0].lower() == "interval" and len(args) >= 2:
        # Set settlement check interval
        try:
            minutes = int(args[1])
            if minutes < MIN_SETTLEMENT_INTERVAL_MINUTES or minutes > MAX_SETTLEMENT_INTERVAL_MINUTES:
                await update.message.reply_text(
                    f"❌ Interval must be between {MIN_SETTLEMENT_INTERVAL_MINUTES} and {MAX_SETTLEMENT_INTERVAL_MINUTES} minutes.",
                    parse_mode="Markdown",
                )
                return
            
            bot_config["settlement_check_interval"] = minutes * 60
            save_config()
            
            await update.message.reply_text(
                f"✅ **Settlement interval set to {minutes} minutes**\n\n"
                f"The bot will check for resolved markets every {minutes} minutes.",
                parse_mode="Markdown",
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid interval. Please provide a number in minutes.\n"
                "Example: `/settlement interval 30`",
                parse_mode="Markdown",
            )
        return

    if args and args[0].lower() == "redeem" and len(args) >= 2:
        # Toggle auto redemption
        value = args[1].lower()
        if value in ("on", "true", "yes", "1"):
            bot_config["auto_redeem_enabled"] = True
            save_config()
            await update.message.reply_text(
                "✅ **Auto token redemption enabled**\n\n"
                "Winning outcome tokens will be automatically redeemed when markets resolve.",
                parse_mode="Markdown",
            )
        elif value in ("off", "false", "no", "0"):
            bot_config["auto_redeem_enabled"] = False
            save_config()
            await update.message.reply_text(
                "✅ **Auto token redemption disabled**\n\n"
                "Winning outcome tokens will NOT be automatically redeemed.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ Invalid value. Use `on` or `off`.\n"
                "Example: `/settlement redeem on`",
                parse_mode="Markdown",
            )
        return

    # Default: show help
    await update.message.reply_text(
        "📊 **Settlement Tracking**\n\n"
        "Automatically polls Gamma API for market resolution status every 30 minutes.\n"
        "When a market resolves, calculates P&L and sends notifications.\n\n"
        "**Commands:**\n"
        "`/settlement status` - Show tracking status\n"
        "`/settlement check` - Force immediate check\n"
        "`/settlement interval <min>` - Set check interval\n"
        "`/settlement redeem on|off` - Toggle auto redemption\n\n"
        "**Configuration:**\n"
        f"• Current Interval: {bot_config.get('settlement_check_interval', 1800) // 60} minutes\n"
        f"• Auto Redeem: {'Enabled' if bot_config.get('auto_redeem_enabled', True) else 'Disabled'}",
        parse_mode="Markdown",
    )



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
            InlineKeyboardButton("🎯 Confidence", callback_data="cfg_confidence"),
            InlineKeyboardButton("📊 RSI", callback_data="cfg_rsi"),
        ],
        [
            InlineKeyboardButton("📈 MACD", callback_data="cfg_macd"),
            InlineKeyboardButton("🧪 Toggle Dry Run", callback_data="toggle_dry_run"),
        ],
        [
            InlineKeyboardButton("🛡️ Risk Settings", callback_data="risk"),
        ],
        [InlineKeyboardButton("« Back", callback_data="back_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live Trading"
    
    # Get signal weights
    weights = bot_config.get("signal_weights", {})

    text = f"""
⚙️ **Bot Configuration**

**Trading:**
Mode: {mode}
Trade Amount: ${bot_config.get('trade_amount', 5.0)}
Cycle Interval: {bot_config.get('cycle_interval_seconds', 300)}s
Asset: {bot_config.get('crypto_id', 'bitcoin')}

**Multi-Signal Engine:**
Min Confidence: {bot_config.get('min_confidence_threshold', 68)}%

**Signal Weights:**
• MA Crossover: {weights.get('ma_crossover', 30)}%
• RSI: {weights.get('rsi', 30)}%
• MACD: {weights.get('macd', 25)}%
• PM Delta: {weights.get('polymarket_delta', 15)}%

**MA Settings:**
Short Window: {bot_config.get('short_window', 5)}
Long Window: {bot_config.get('long_window', 20)}

**RSI Settings:**
Period: {bot_config.get('rsi_period', 14)}
Overbought: {bot_config.get('rsi_overbought', 70)}
Oversold: {bot_config.get('rsi_oversold', 30)}

**MACD Settings:**
Fast: {bot_config.get('macd_fast_period', 12)}
Slow: {bot_config.get('macd_slow_period', 26)}
Signal: {bot_config.get('macd_signal_period', 9)}

**Risk Management:** 🛡️
Max Daily Loss: ${bot_config.get('max_daily_loss', 25.0)}
Max Position Size: {bot_config.get('max_position_size_pct', 10.0)}% of balance
Max Concurrent Positions: {bot_config.get('max_concurrent_positions', 5)}
Circuit Breaker: {bot_config.get('circuit_breaker_consecutive_losses', 3)} consecutive losses

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
# Multi-Signal Configuration Commands
# ---------------------------------------------------------------------------


async def set_confidence_threshold_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting confidence threshold."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        f"🎯 **Set Confidence Threshold**\n\n"
        f"Current: {bot_config.get('min_confidence_threshold', 68)}%\n\n"
        f"Trades are only executed when confidence ≥ this threshold.\n"
        f"Send a value between 0 and 100.\n\n"
        f"Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_CONFIDENCE_THRESHOLD


async def set_confidence_threshold_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive confidence threshold."""
    try:
        threshold = int(update.message.text.strip())
        if threshold < 0 or threshold > 100:
            raise ValueError("Threshold must be between 0 and 100")

        bot_config["min_confidence_threshold"] = threshold
        save_config()

        await update.message.reply_text(
            f"✅ Confidence threshold set to {threshold}%\n\n"
            f"Bot will only trade when confidence ≥ {threshold}%",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid threshold: {e}")
        return AWAITING_CONFIDENCE_THRESHOLD

    return ConversationHandler.END


async def set_rsi_params_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting RSI parameters."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        f"📊 **Set RSI Parameters**\n\n"
        f"Current Settings:\n"
        f"• Period: {bot_config.get('rsi_period', 14)}\n"
        f"• Overbought: {bot_config.get('rsi_overbought', 70)}\n"
        f"• Oversold: {bot_config.get('rsi_oversold', 30)}\n\n"
        f"Send values in format: `period overbought oversold`\n"
        f"Example: `14 70 30`\n\n"
        f"Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_RSI_PERIOD


async def set_rsi_params_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive RSI parameters."""
    try:
        parts = update.message.text.strip().split()
        if len(parts) != 3:
            raise ValueError("Please provide exactly 3 values: period overbought oversold")
        
        period = int(parts[0])
        overbought = int(parts[1])
        oversold = int(parts[2])
        
        if period < 2 or period > 50:
            raise ValueError("Period must be between 2 and 50")
        if overbought < 50 or overbought > 100:
            raise ValueError("Overbought must be between 50 and 100")
        if oversold < 0 or oversold > 50:
            raise ValueError("Oversold must be between 0 and 50")
        if oversold >= overbought:
            raise ValueError("Oversold must be less than overbought")

        bot_config["rsi_period"] = period
        bot_config["rsi_overbought"] = overbought
        bot_config["rsi_oversold"] = oversold
        save_config()

        await update.message.reply_text(
            f"✅ RSI parameters updated:\n"
            f"• Period: {period}\n"
            f"• Overbought: {overbought}\n"
            f"• Oversold: {oversold}",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid parameters: {e}")
        return AWAITING_RSI_PERIOD

    return ConversationHandler.END


async def set_macd_params_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start setting MACD parameters."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return ConversationHandler.END

    await update.message.reply_text(
        f"📈 **Set MACD Parameters**\n\n"
        f"Current Settings:\n"
        f"• Fast Period: {bot_config.get('macd_fast_period', 12)}\n"
        f"• Slow Period: {bot_config.get('macd_slow_period', 26)}\n"
        f"• Signal Period: {bot_config.get('macd_signal_period', 9)}\n\n"
        f"Send values in format: `fast slow signal`\n"
        f"Example: `12 26 9`\n\n"
        f"Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return AWAITING_MACD_PARAMS


async def set_macd_params_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive MACD parameters."""
    try:
        parts = update.message.text.strip().split()
        if len(parts) != 3:
            raise ValueError("Please provide exactly 3 values: fast slow signal")
        
        fast = int(parts[0])
        slow = int(parts[1])
        signal = int(parts[2])
        
        if fast < 2 or fast > 50:
            raise ValueError("Fast period must be between 2 and 50")
        if slow < 5 or slow > 100:
            raise ValueError("Slow period must be between 5 and 100")
        if signal < 2 or signal > 50:
            raise ValueError("Signal period must be between 2 and 50")
        if fast >= slow:
            raise ValueError("Fast period must be less than slow period")

        bot_config["macd_fast_period"] = fast
        bot_config["macd_slow_period"] = slow
        bot_config["macd_signal_period"] = signal
        save_config()

        await update.message.reply_text(
            f"✅ MACD parameters updated:\n"
            f"• Fast Period: {fast}\n"
            f"• Slow Period: {slow}\n"
            f"• Signal Period: {signal}",
            parse_mode="Markdown",
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid parameters: {e}")
        return AWAITING_MACD_PARAMS

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

    # Check if we have credentials to enable live trading (100% onchain-Modus)
    if current:  # Trying to enable live trading
        if not bot_config.get("polygon_private_key"):
            await update.message.reply_text(
                "❌ Kann Live Trading nicht aktivieren.\n\n"
                "Bitte zuerst Polygon Private Key setzen mit /set_polygon_key",
            )
            return
        # Im 100% onchain-Modus keine API-Credentials mehr nötig
        # Prüfe aber ob Approvals gesetzt sind
        if not bot_config.get("approvals_done", False):
            await update.message.reply_text(
                "⚠️ **Warnung:** Approvals noch nicht gesetzt!\n\n"
                "Für Live Trading empfohlen:\n"
                "1. /setup_approvals - Setzt USDC/CTF Approvals\n"
                "2. /gas_status - Prüfe MATIC-Balance\n\n"
                "Live Trading wird trotzdem aktiviert.",
            )

    bot_config["dry_run"] = not current
    save_config()

    new_mode = "🧪 Dry Run" if bot_config["dry_run"] else "💰 Live Trading"

    await update.message.reply_text(
        f"✅ Modus geändert zu: {new_mode}\n\n"
        f"{'Keine echten Trades werden ausgeführt.' if bot_config['dry_run'] else '⚠️ ECHTE TRADES werden ausgeführt!'}",
        parse_mode="Markdown",
    )


async def setup_approvals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setup_approvals command - Setzt USDC und CTF Approvals onchain."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    if not bot_config.get("polygon_private_key"):
        await update.message.reply_text(
            "❌ Polygon Private Key nicht konfiguriert.\n"
            "Bitte zuerst /set_polygon_key verwenden."
        )
        return

    # Prüfe aktuelle Approvals
    current = check_approvals()
    
    if current.get("error"):
        await update.message.reply_text(f"❌ Fehler: {current.get('error')}")
        return
    
    if current.get("usdc_approved") and current.get("ctf_approved"):
        await update.message.reply_text(
            "✅ **Approvals bereits gesetzt!**\n\n"
            "USDC: ✅ Approved\n"
            "CTF: ✅ Approved\n\n"
            "Du kannst direkt mit /toggle_dry_run Live Trading aktivieren.",
            parse_mode="Markdown",
        )
        bot_config["approvals_done"] = True
        save_config()
        return

    # Zeige Warnung
    matic_balance = get_matic_balance()
    await update.message.reply_text(
        f"⚠️ **ONCHAIN APPROVAL TRANSAKTION**\n\n"
        f"Dies setzt:\n"
        f"• USDC.approve(Exchange, maxUint256)\n"
        f"• CTF.setApprovalForAll(Exchange, true)\n\n"
        f"**Contracts (Polygon 2026):**\n"
        f"• USDC: `{POLYGON_USDC_ADDRESS[:10]}...`\n"
        f"• CTF: `{POLYGON_CTF_ADDRESS[:10]}...`\n"
        f"• Exchange: `{POLYGON_EXCHANGE_ADDRESS[:10]}...`\n\n"
        f"**Aktuelle MATIC-Balance:** {matic_balance:.4f} MATIC\n\n"
        f"⛽ **KOSTET MATIC FÜR GAS!**\n"
        f"Mindestens 0.01 MATIC benötigt.\n\n"
        f"Setze Approvals...",
        parse_mode="Markdown",
    )

    # Führe Approvals durch
    result = setup_approvals()
    
    if result.get("success"):
        msg = "✅ **Approvals erfolgreich gesetzt!**\n\n"
        if result.get("usdc_tx"):
            msg += f"USDC Tx: `{result.get('usdc_tx')[:20]}...`\n"
        else:
            msg += "USDC: Bereits approved\n"
        if result.get("ctf_tx"):
            msg += f"CTF Tx: `{result.get('ctf_tx')[:20]}...`\n"
        else:
            msg += "CTF: Bereits approved\n"
        msg += "\n🎉 Du kannst jetzt Live Trading aktivieren mit /toggle_dry_run"
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"❌ **Approval fehlgeschlagen**\n\n"
            f"Fehler: {result.get('error', 'Unbekannt')}\n\n"
            f"Stelle sicher dass du genug MATIC für Gas hast!",
            parse_mode="Markdown",
        )


async def gas_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /gas_status command - Zeigt MATIC-Balance für Gas."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    if not bot_config.get("polygon_private_key"):
        await update.message.reply_text(
            "❌ Polygon Private Key nicht konfiguriert.\n"
            "Bitte zuerst /set_polygon_key verwenden."
        )
        return

    address = get_polygon_address()
    matic_balance = get_matic_balance()
    
    # Status-Emoji basierend auf Balance
    if matic_balance >= 1.0:
        status = "🟢 Gut"
        warning = ""
    elif matic_balance >= 0.1:
        status = "🟡 OK"
        warning = "Bald auffüllen empfohlen."
    elif matic_balance >= 0.01:
        status = "🟠 Niedrig"
        warning = "⚠️ Bald kein Gas mehr! Bitte MATIC auffüllen."
    else:
        status = "🔴 Kritisch"
        warning = "❌ NICHT GENUG GAS! Transaktionen werden fehlschlagen."

    # Prüfe Auto MATIC Top-Up Status
    auto_topup = "✅ Aktiviert" if bot_config.get("auto_matic_topup_enabled", True) else "❌ Deaktiviert"
    topup_threshold = bot_config.get("auto_matic_topup_min_profit", 0.5)
    topup_amount = bot_config.get("auto_matic_topup_amount", 0.20)

    # Adresse formatieren
    address_display = f"`{address[:20]}...`" if address else "Nicht verfügbar"

    text = f"""
⛽ **Gas Status (MATIC)**

**Adresse:** {address_display}
**MATIC-Balance:** {matic_balance:.4f} MATIC
**Status:** {status}
{warning}

**Auto MATIC Top-Up:**
Status: {auto_topup}
Trigger: Bei Profit > ${topup_threshold:.2f}
Betrag: ${topup_amount:.2f} USDC → MATIC

**Tipps:**
• Mindestens 0.1 MATIC für stabilen Betrieb
• Auto Top-Up swapped USDC→MATIC nach Profit
• Manuell MATIC senden an deine Polygon-Adresse
"""
    await update.message.reply_text(text, parse_mode="Markdown")


async def toggle_onchain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /toggle_onchain command - Schaltet Onchain-Modus um."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    current = bot_config.get("onchain_mode", True)
    
    # Im 100% Onchain-Modus ist dies immer True
    # Aber wir zeigen Info darüber
    await update.message.reply_text(
        f"⛓️ **100% Onchain-Modus**\n\n"
        f"Aktueller Status: {'✅ Aktiviert' if current else '❌ Deaktiviert'}\n\n"
        f"**Was bedeutet das?**\n"
        f"• Keine API-Credentials (Key/Secret/Passphrase) nötig\n"
        f"• L2-Credentials werden automatisch abgeleitet\n"
        f"• Nur dein Private Key wird benötigt\n"
        f"• Alle Trades sind 100% onchain\n\n"
        f"**Vorteile:**\n"
        f"✓ Einfacheres Setup\n"
        f"✓ Keine API-Keys zu verwalten\n"
        f"✓ Maximale Sicherheit\n\n"
        f"Hinweis: Der Onchain-Modus ist in dieser Version immer aktiv.",
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
    """Handle /trade command for manual trading with top mispriced markets."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    if bot_config.get("dry_run", True):
        await update.message.reply_text(
            "⚠️ Bot is in dry run mode.\n"
            "Use /toggle_dry_run to enable live trading first.",
        )
        return

    # Find top 3 mispriced markets (crypto, politics, economics)
    markets = find_relevant_markets(count=8, min_deviation_pct=12.0)[:3]

    if not markets:
        await update.message.reply_text(
            "No mispriced markets found with ≥12% deviation.\n"
            "Try again later or use /scan to see all markets."
        )
        return

    # Get current prediction and calculate confidence with deviation bonus
    pred = get_current_prediction()
    prediction = pred.get("prediction", "hold")
    base_confidence = pred.get("confidence", 0)
    outcome = "YES" if prediction == "up" else "NO"

    # Get best market (first one has highest deviation)
    best_market = markets[0]
    price_deviation = best_market.get("price_deviation", {})
    deviation_pct = abs(price_deviation.get("deviation_pct", 0))
    direction = price_deviation.get("direction", "unknown")
    category = best_market.get("category", "other")
    current_price = price_deviation.get("current_price", 0.5)
    historical_mean = price_deviation.get("historical_mean", 0.5)

    # Calculate total confidence with deviation bonus
    # Formula: deviation_pct / 2.0, capped at 15% (same as bot_loop)
    # Example: 30% deviation → 15% bonus (max), 20% deviation → 10% bonus
    deviation_bonus = min(15.0, deviation_pct / 2.0)
    total_confidence = min(100.0, base_confidence + deviation_bonus)

    # Calculate Kelly-optimized position size
    current_balance = get_polygon_balance()
    kelly_size = kelly_position_size(
        confidence=total_confidence,
        balance=current_balance,
        deviation_pct=deviation_pct,
    )
    # Store Kelly size in user_data for use when trade is confirmed (not in global config)
    context.user_data["kelly_trade_amount"] = kelly_size

    keyboard = [
        [
            InlineKeyboardButton("Buy YES", callback_data="trade_yes"),
            InlineKeyboardButton("Buy NO", callback_data="trade_no"),
        ],
        [InlineKeyboardButton("Cancel", callback_data="trade_cancel")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    context.user_data["trade_market"] = best_market

    emoji = get_deviation_emoji(direction)

    await update.message.reply_text(
        f"📈 **Manual Trade**\n\n"
        f"**Market:** {best_market.get('question', 'N/A')[:70]}...\n"
        f"🏷️ **Category:** {category.capitalize()}\n\n"
        f"**Price Analysis:**\n"
        f"• Current Price: {current_price:.2%}\n"
        f"• Historical Mean: {historical_mean:.2%}\n"
        f"• {emoji} {direction.upper()} by {deviation_pct:.1f}%\n\n"
        f"**Confidence:**\n"
        f"• Base: {base_confidence:.1f}%\n"
        f"• Deviation Bonus: +{deviation_bonus:.1f}%\n"
        f"• 🎯 Total: {total_confidence:.1f}%\n\n"
        f"**Trade:**\n"
        f"• Prediction suggests: {outcome}\n"
        f"• 💰 Kelly Size: ${kelly_size:.2f} (Balance: ${current_balance:.2f})\n\n"
        f"Select your trade:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Risk Management Command Handler
# ---------------------------------------------------------------------------


async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /risk command for viewing and configuring risk management."""
    if not is_authorized(update):
        await unauthorized_response(update)
        return

    args = context.args if context.args else []

    # Handle subcommands
    if args:
        subcommand = args[0].lower()

        if subcommand == "reset":
            # Reset circuit breaker
            result = reset_circuit_breaker()
            await update.message.reply_text(
                f"🔄 **Circuit Breaker Reset**\n\n{result.get('message', 'Reset complete.')}",
                parse_mode="Markdown",
            )
            return

        elif subcommand == "set" and len(args) >= 3:
            # Set a risk parameter: /risk set <param> <value>
            param = args[1].lower()
            try:
                value = float(args[2])
            except ValueError:
                await update.message.reply_text(
                    "❌ Invalid value. Please provide a number.",
                    parse_mode="Markdown",
                )
                return

            if param in ("max_daily_loss", "daily_loss"):
                if value < 0:
                    await update.message.reply_text("❌ Value must be positive.")
                    return
                bot_config["max_daily_loss"] = value
                save_config()
                await update.message.reply_text(
                    f"✅ Max daily loss set to: ${value:.2f}",
                    parse_mode="Markdown",
                )
                return

            elif param in ("max_position_size", "position_size", "position_pct"):
                if value < 0 or value > 100:
                    await update.message.reply_text("❌ Value must be between 0 and 100.")
                    return
                bot_config["max_position_size_pct"] = value
                save_config()
                await update.message.reply_text(
                    f"✅ Max position size set to: {value:.1f}% of balance",
                    parse_mode="Markdown",
                )
                return

            elif param in ("max_positions", "max_concurrent", "concurrent"):
                if value < 1:
                    await update.message.reply_text("❌ Value must be at least 1.")
                    return
                bot_config["max_concurrent_positions"] = int(value)
                save_config()
                await update.message.reply_text(
                    f"✅ Max concurrent positions set to: {int(value)}",
                    parse_mode="Markdown",
                )
                return

            elif param in ("circuit_breaker", "consecutive_losses", "cb_limit"):
                if value < 1:
                    await update.message.reply_text("❌ Value must be at least 1.")
                    return
                bot_config["circuit_breaker_consecutive_losses"] = int(value)
                save_config()
                await update.message.reply_text(
                    f"✅ Circuit breaker limit set to: {int(value)} consecutive losses",
                    parse_mode="Markdown",
                )
                return

            else:
                await update.message.reply_text(
                    "❌ Unknown parameter. Valid parameters:\n"
                    "• max_daily_loss\n"
                    "• max_position_size\n"
                    "• max_positions\n"
                    "• circuit_breaker",
                    parse_mode="Markdown",
                )
                return

        elif subcommand == "help":
            await update.message.reply_text(
                "🛡️ **Risk Management Help**\n\n"
                "**View Status:**\n"
                "`/risk` - Show current risk status\n\n"
                "**Reset Circuit Breaker:**\n"
                "`/risk reset` - Reset and resume trading\n\n"
                "**Configure Limits:**\n"
                "`/risk set max_daily_loss <amount>` - Set max daily loss in USD\n"
                "`/risk set max_position_size <pct>` - Set max position size (% of balance)\n"
                "`/risk set max_positions <count>` - Set max concurrent positions\n"
                "`/risk set circuit_breaker <count>` - Set consecutive losses before pause\n\n"
                "**Examples:**\n"
                "`/risk set max_daily_loss 50`\n"
                "`/risk set max_position_size 15`\n"
                "`/risk set max_positions 3`\n"
                "`/risk set circuit_breaker 5`",
                parse_mode="Markdown",
            )
            return

        else:
            await update.message.reply_text(
                "❌ Unknown subcommand. Use `/risk help` for available commands.",
                parse_mode="Markdown",
            )
            return

    # Default: show risk status
    status = get_risk_status()

    # Format status indicators
    cb_status = "🔴 PAUSED" if status["circuit_breaker_paused"] else "🟢 Active"
    daily_loss_status = "🔴" if status["daily_loss"] >= status["max_daily_loss"] else "🟢"
    positions_status = "🔴" if status["open_positions"] >= status["max_concurrent_positions"] else "🟢"

    # Calculate position size limit
    balance = get_polygon_balance()
    max_position_amount = balance * (status["max_position_size_pct"] / 100.0) if balance > 0 else 0

    text = f"""
🛡️ **Risk Management Status**

**Circuit Breaker:** {cb_status}
Consecutive Losses: {status['consecutive_losses']} / {status['circuit_breaker_limit']}

**Daily Loss Limit:** {daily_loss_status}
Today's Loss: ${status['daily_loss']:.2f} / ${status['max_daily_loss']:.2f}
Today's P&L: ${status['daily_profit']:.2f} ({status['daily_trades']} trades)

**Position Limits:** {positions_status}
Open Positions: {status['open_positions']} / {status['max_concurrent_positions']}
Max Position Size: {status['max_position_size_pct']:.1f}% (${max_position_amount:.2f})

**Configure:**
`/risk set max_daily_loss <amount>`
`/risk set max_position_size <pct>`
`/risk set max_positions <count>`
`/risk set circuit_breaker <count>`
`/risk reset` - Reset circuit breaker
`/risk help` - Show help
"""
    await update.message.reply_text(text, parse_mode="Markdown")


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
        await query.edit_message_text("🔍 Scanning for mispriced markets...")
        markets = find_relevant_markets(count=8, min_deviation_pct=12.0)

        if not markets:
            text = "No mispriced markets found (≥12% deviation)."
        else:
            text = f"📈 **Top {len(markets)} Mispriced Market(s)**\n"
            text += f"_(crypto, politics, economics)_\n\n"
            for i, m in enumerate(markets[:3], 1):
                question = m.get('question', 'N/A')[:50]
                category = m.get('category', 'other')
                deviation = m.get('price_deviation', {})
                dev_pct = deviation.get('deviation_pct', 0)
                direction = deviation.get('direction', 'unknown')
                emoji = get_deviation_emoji(direction)
                text += f"**{i}. {question}...**\n"
                text += f"   {emoji} {direction.upper()} {abs(dev_pct):.1f}%\n"
                text += f"   🏷️ {category.capitalize()}\n\n"

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
        approvals_set = "✅" if bot_config.get("approvals_done", False) else "❌"
        matic_bal = get_matic_balance()

        text = (
            f"🔑 **Wallets (100% Onchain)**\n\n"
            f"Solana Key: {sol_set}\n"
            f"Polygon Key: {poly_set}\n"
            f"Approvals: {approvals_set}\n"
            f"MATIC: ⛽ {matic_bal:.4f}\n\n"
            f"**Setup-Befehle:**\n"
            f"/set_solana_key - Solana Key\n"
            f"/set_polygon_key - Polygon Key\n"
            f"/setup_approvals - Onchain Approvals ⛓️\n"
            f"/gas_status - MATIC-Balance ⛽"
        )

        keyboard = [[InlineKeyboardButton("« Zurück", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "setup_approvals":
        # Zeige Approvals-Status und Option zum Setzen
        if not bot_config.get("polygon_private_key"):
            await query.edit_message_text(
                "❌ Polygon Private Key nicht konfiguriert.\n"
                "Bitte zuerst /set_polygon_key verwenden."
            )
            return
        
        current = check_approvals()
        matic_bal = get_matic_balance()
        
        usdc_status = "✅" if current.get("usdc_approved") else "❌"
        ctf_status = "✅" if current.get("ctf_approved") else "❌"
        
        text = (
            f"⛓️ **Onchain Approvals**\n\n"
            f"USDC: {usdc_status}\n"
            f"CTF: {ctf_status}\n"
            f"MATIC: ⛽ {matic_bal:.4f}\n\n"
            f"Zum Setzen verwende:\n"
            f"/setup_approvals"
        )
        
        keyboard = [[InlineKeyboardButton("« Zurück", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "gas_status":
        # Zeige MATIC-Balance
        if not bot_config.get("polygon_private_key"):
            await query.edit_message_text(
                "❌ Polygon Private Key nicht konfiguriert.\n"
                "Bitte zuerst /set_polygon_key verwenden."
            )
            return
        
        matic_bal = get_matic_balance()
        
        if matic_bal >= 1.0:
            status = "🟢 Gut"
        elif matic_bal >= 0.1:
            status = "🟡 OK"
        else:
            status = "🔴 Niedrig"
        
        text = (
            f"⛽ **Gas Status**\n\n"
            f"MATIC: {matic_bal:.4f}\n"
            f"Status: {status}\n\n"
            f"Für Details:\n"
            f"/gas_status"
        )
        
        keyboard = [[InlineKeyboardButton("« Zurück", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "pnl":
        pnl = load_pnl()
        text = f"📜 **P&L**\n\nTotal: ${pnl.get('total', 0):.2f}"

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "positions":
        positions_data = load_positions()
        open_positions = positions_data.get("open", [])
        closed_positions = positions_data.get("closed", [])

        if not open_positions:
            total_closed = len(closed_positions)
            total_pnl = sum(p.get("realized_pnl", 0) for p in closed_positions)
            text = (
                f"📊 **Positions**\n\n"
                f"No open positions.\n\n"
                f"Closed: {total_closed}\n"
                f"Total P&L: ${total_pnl:.2f}"
            )
        else:
            text = f"📊 **Open Positions ({len(open_positions)})**\n\n"
            for i, pos in enumerate(open_positions[:3], 1):
                side = pos.get("side", "?").upper()
                entry = pos.get("entry_price", 0)
                amount = pos.get("amount", 0)
                text += f"{i}. {side}: ${amount:.2f} @ {entry:.4f}\n"
            if len(open_positions) > 3:
                text += f"...and {len(open_positions) - 3} more\n"
            text += f"\nUse /positions for details."

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "help":
        text = "❓ **Help**\n\nUse /help for full command list."

        keyboard = [[InlineKeyboardButton("« Back", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "risk":
        # Show risk management status
        status = get_risk_status()

        # Format status indicators
        cb_status = "🔴 PAUSED" if status["circuit_breaker_paused"] else "🟢 Active"
        daily_loss_status = "🔴" if status["daily_loss"] >= status["max_daily_loss"] else "🟢"
        positions_status = "🔴" if status["open_positions"] >= status["max_concurrent_positions"] else "🟢"

        # Calculate position size limit
        balance = get_polygon_balance()
        max_position_amount = balance * (status["max_position_size_pct"] / 100.0) if balance > 0 else 0

        text = (
            f"🛡️ **Risk Management**\n\n"
            f"**Circuit Breaker:** {cb_status}\n"
            f"Consecutive Losses: {status['consecutive_losses']} / {status['circuit_breaker_limit']}\n\n"
            f"**Daily Loss:** {daily_loss_status}\n"
            f"Today: ${status['daily_loss']:.2f} / ${status['max_daily_loss']:.2f}\n\n"
            f"**Positions:** {positions_status}\n"
            f"Open: {status['open_positions']} / {status['max_concurrent_positions']}\n"
            f"Max Size: {status['max_position_size_pct']:.0f}% (${max_position_amount:.2f})\n\n"
            f"Use /risk for configuration."
        )

        keyboard = [
            [InlineKeyboardButton("🔄 Reset CB", callback_data="risk_reset")],
            [InlineKeyboardButton("« Back", callback_data="back_main")],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "risk_reset":
        result = reset_circuit_breaker()
        text = f"🔄 **Circuit Breaker Reset**\n\n{result.get('message', 'Reset complete.')}"

        keyboard = [[InlineKeyboardButton("« Back", callback_data="risk")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "toggle_dry_run":
        current = bot_config.get("dry_run", True)

        if current:  # Trying to enable live trading
            # Im 100% onchain-Modus nur Private Key nötig, keine API-Credentials
            if not bot_config.get("polygon_private_key"):
                await query.edit_message_text(
                    "❌ Kann Live Trading nicht aktivieren.\n\n"
                    "Bitte zuerst Polygon Private Key setzen.",
                )
                return

        bot_config["dry_run"] = not current
        save_config()

        new_mode = "🧪 Dry Run" if bot_config["dry_run"] else "💰 Live Trading"
        text = f"✅ Modus: {new_mode}"

        keyboard = [[InlineKeyboardButton("« Zurück", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "trade_yes":
        market = context.user_data.get("trade_market")
        if market:
            # Use Kelly-calculated amount if available, fallback to config
            amount = context.user_data.get("kelly_trade_amount", bot_config.get("trade_amount", 5.0))
            result = place_trade(market, "yes", amount)
            if result.get("success"):
                text = f"✅ Bought YES for ${amount:.2f}"
            else:
                text = f"❌ Trade failed: {result.get('error')}"
            # Clean up user data
            context.user_data.pop("kelly_trade_amount", None)
        else:
            text = "❌ No market selected"
        await query.edit_message_text(text)

    elif data == "trade_no":
        market = context.user_data.get("trade_market")
        if market:
            # Use Kelly-calculated amount if available, fallback to config
            amount = context.user_data.get("kelly_trade_amount", bot_config.get("trade_amount", 5.0))
            result = place_trade(market, "no", amount)
            if result.get("success"):
                text = f"✅ Bought NO for ${amount:.2f}"
            else:
                text = f"❌ Trade failed: {result.get('error')}"
            # Clean up user data
            context.user_data.pop("kelly_trade_amount", None)
        else:
            text = "❌ No market selected"
        await query.edit_message_text(text)

    elif data == "trade_cancel":
        context.user_data.pop("trade_market", None)
        context.user_data.pop("kelly_trade_amount", None)
        await query.edit_message_text("❌ Trade cancelled")

    elif data == "cfg_confidence":
        text = (
            f"🎯 **Confidence Threshold**\n\n"
            f"Current: {bot_config.get('min_confidence_threshold', 68)}%\n\n"
            f"Trades only execute when confidence ≥ this value.\n\n"
            f"Use `/set_confidence_threshold` to change."
        )
        keyboard = [[InlineKeyboardButton("« Back", callback_data="config")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cfg_rsi":
        text = (
            f"📊 **RSI Settings**\n\n"
            f"Period: {bot_config.get('rsi_period', 14)}\n"
            f"Overbought: {bot_config.get('rsi_overbought', 70)}\n"
            f"Oversold: {bot_config.get('rsi_oversold', 30)}\n\n"
            f"Use `/set_rsi_params` to change."
        )
        keyboard = [[InlineKeyboardButton("« Back", callback_data="config")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cfg_macd":
        text = (
            f"📈 **MACD Settings**\n\n"
            f"Fast Period: {bot_config.get('macd_fast_period', 12)}\n"
            f"Slow Period: {bot_config.get('macd_slow_period', 26)}\n"
            f"Signal Period: {bot_config.get('macd_signal_period', 9)}\n\n"
            f"Use `/set_macd_params` to change."
        )
        keyboard = [[InlineKeyboardButton("« Back", callback_data="config")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

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
                InlineKeyboardButton("⛓️ Approvals", callback_data="setup_approvals"),
                InlineKeyboardButton("⛽ Gas", callback_data="gas_status"),
            ],
            [
                InlineKeyboardButton("📜 P&L", callback_data="pnl"),
                InlineKeyboardButton("📊 Positions", callback_data="positions"),
            ],
            [InlineKeyboardButton("❓ Hilfe", callback_data="help")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        status = "🟢 Läuft" if bot_config.get("bot_running", False) else "🔴 Gestoppt"
        mode = "🧪 Dry Run" if bot_config.get("dry_run", True) else "💰 Live"
        onchain = "⛓️ Onchain"

        await query.edit_message_text(
            f"🎰 **UpDown Bot**\n\n"
            f"Status: {status} | Modus: {mode} | {onchain}\n\n"
            f"Wähle eine Option:",
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

    # Multi-signal configuration conversation handlers
    confidence_conv = ConversationHandler(
        entry_points=[CommandHandler("set_confidence_threshold", set_confidence_threshold_start)],
        states={
            AWAITING_CONFIDENCE_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_confidence_threshold_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    rsi_conv = ConversationHandler(
        entry_points=[CommandHandler("set_rsi_params", set_rsi_params_start)],
        states={
            AWAITING_RSI_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rsi_params_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    macd_conv = ConversationHandler(
        entry_points=[CommandHandler("set_macd_params", set_macd_params_start)],
        states={
            AWAITING_MACD_PARAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_macd_params_receive)],
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
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CommandHandler("politics_scan", politics_scan_command))
    application.add_handler(CommandHandler("pnl", pnl_command))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("settlement", settlement_command))
    application.add_handler(CommandHandler("config", config_command))
    application.add_handler(CommandHandler("start_bot", start_bot_command))
    application.add_handler(CommandHandler("stop_bot", stop_bot_command))
    application.add_handler(CommandHandler("toggle_dry_run", toggle_dry_run_command))
    application.add_handler(CommandHandler("bridge", bridge_command))
    application.add_handler(CommandHandler("trade", trade_command))
    # Neue Onchain-Commands (100% Onchain-Modus 2026)
    application.add_handler(CommandHandler("setup_approvals", setup_approvals_command))
    application.add_handler(CommandHandler("gas_status", gas_status_command))
    application.add_handler(CommandHandler("toggle_onchain", toggle_onchain_command))
    # Risk management command
    application.add_handler(CommandHandler("risk", risk_command))
    # Backtest command
    application.add_handler(CommandHandler("backtest", backtest_command))

    # Add conversation handlers
    application.add_handler(solana_conv)
    application.add_handler(polygon_conv)
    application.add_handler(trade_amount_conv)
    application.add_handler(min_balance_conv)
    application.add_handler(bridge_amount_conv)
    application.add_handler(interval_conv)
    # Multi-signal configuration handlers
    application.add_handler(confidence_conv)
    application.add_handler(rsi_conv)
    application.add_handler(macd_conv)

    # Add callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))

    # Start the bot
    print("Bot is running. Press Ctrl+C to stop.")
    print("100% Onchain-Modus aktiviert - Keine API-Credentials benötigt!")
    print("Multi-Signal Trading Engine aktiv (RSI, MACD, MA, Polymarket Delta)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
