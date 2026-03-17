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
POSITIONS_FILE = Path("positions.json")

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
) = range(9)

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

# Global state
bot_config: dict = {}
bot_thread: threading.Thread | None = None
bot_thread_lock = threading.Lock()  # Lock for thread-safe bot start/stop
stop_event = threading.Event()
cg = CoinGeckoAPI()

# ---------------------------------------------------------------------------
# L2 Credentials Cache
# ---------------------------------------------------------------------------
# Cache structure: {"api_key": str, "api_secret": str, "api_passphrase": str, "derived_at": float}
_l2_credentials_cache: dict | None = None
_L2_CREDENTIALS_TTL_SECONDS = 3600  # 1 hour TTL

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
) -> dict | None:
    """Close an open position and calculate realized P&L.

    Args:
        market_id: The Polymarket market ID.
        token_id: The token ID.
        exit_price: The exit/settlement price per share.
        resolution: Optional resolution outcome ('yes', 'no', or None for manual exit).

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
            "error": (
                "Unable to connect to CoinGecko API. "
                "Verify network connectivity and ensure api.coingecko.com is not blocked by firewall rules."
            ),
        }
    except Exception as e:
        logger.error(f"Error getting prediction: {e}")
        return {
            "prediction": "error",
            "price": 0,
            "candles": 0,
            "crypto": bot_config.get("crypto_id", "unknown"),
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

        # Record the position for tracking
        market_id = market.get("id") or market.get("condition_id") or ""
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


def check_resolved_markets() -> list:
    """Check open positions for resolved markets and calculate realized P&L.

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


def sell_position(position: dict) -> dict:
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

            # Check for resolved markets and auto-calculate P&L
            if not bot_config.get("dry_run", True):
                try:
                    resolved = check_resolved_markets()
                    for result in resolved:
                        pos = result.get("position", {})
                        send_notification(
                            f"📊 **Market Resolved**\n\n"
                            f"Market: {result.get('market_question', 'N/A')[:50]}...\n"
                            f"Resolution: {result.get('resolution', 'N/A').upper()}\n"
                            f"Your Side: {pos.get('side', 'N/A').upper()}\n"
                            f"Entry: ${pos.get('entry_price', 0):.4f}\n"
                            f"Settlement: ${pos.get('exit_price', 0):.4f}\n"
                            f"Realized P&L: ${pos.get('realized_pnl', 0):.2f}"
                        )
                except Exception as e:
                    logger.error(f"Error checking resolved markets: {e}")

            # Get prediction
            pred = get_current_prediction()
            prediction = pred.get("prediction", "hold")

            if prediction == "hold":
                # Skip the cycle but don't update previous_prediction
                # This way, "hold" states are transparent to flip detection
                logger.info("Not enough data - skipping trade")
                wait_with_check(bot_config.get("cycle_interval_seconds", 300))
                continue

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
    onchain = "⛓️ 100% Onchain"

    await update.message.reply_text(
        f"🎰 **UpDown Trading Bot**\n\n"
        f"Status: {status}\n"
        f"Modus: {mode} | {onchain}\n\n"
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
/predict - Aktuelle Vorhersage
/markets - Relevante Märkte finden
/trade - Manueller Trade
/bridge - Solana→Polygon Bridge

**Position Tracking:**
/positions - Offene Positionen anzeigen 📊
  (Auto-Exit bei Prediction-Flip, Auto-P&L bei Resolution)

**Configuration:**
/config - Einstellungen anzeigen/ändern
/set_trade_amount - Trade-Größe setzen
/set_min_balance - Min. Polygon-Balance setzen
/set_bridge_amount - Bridge-Betrag setzen
/set_interval - Zyklus-Intervall setzen
/toggle_dry_run - Dry Run Modus umschalten
/toggle_onchain - Onchain-Modus umschalten

**Info:**
/pnl - P&L Historie anzeigen
/help - Diese Hilfe

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

    text = f"""
📊 **Bot Status (100% Onchain)**

**State:** {status}
**Mode:** {mode} | {onchain}

**Wallets:**
Solana: {sol_configured} {f"${sol_bal.get('usdc', 0):.2f} USDC | {sol_bal.get('sol', 0):.4f} SOL" if sol_bal else "Nicht konfiguriert"}
Polygon: {poly_configured} {f"${poly_bal:.2f} USDC" if poly_bal else "Nicht konfiguriert"}
MATIC: ⛽ {matic_bal:.4f} MATIC {matic_warning}
Approvals: {approvals}

**Aktuelle Vorhersage:**
Asset: {pred.get('crypto', 'N/A').upper()}
Preis: ${pred.get('price', 0):,.2f}
Richtung: {pred.get('prediction', 'N/A').upper()}

**Heutiges P&L:**
Trades: {daily_pnl.get('trades', 0)}
Profit: ${daily_pnl.get('profit', 0):.2f}
Total: ${pnl.get('total', 0):.2f}

**Config:**
Trade Amount: ${bot_config.get('trade_amount', 5.0)}
Cycle: {bot_config.get('cycle_interval_seconds', 300)}s
Auto MATIC: {'✅' if bot_config.get('auto_matic_topup_enabled', True) else '❌'}
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

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("predict", predict_command))
    application.add_handler(CommandHandler("markets", markets_command))
    application.add_handler(CommandHandler("pnl", pnl_command))
    application.add_handler(CommandHandler("positions", positions_command))
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

    # Add conversation handlers
    application.add_handler(solana_conv)
    application.add_handler(polygon_conv)
    application.add_handler(trade_amount_conv)
    application.add_handler(min_balance_conv)
    application.add_handler(bridge_amount_conv)
    application.add_handler(interval_conv)

    # Add callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))

    # Start the bot
    print("Bot is running. Press Ctrl+C to stop.")
    print("100% Onchain-Modus aktiviert - Keine API-Credentials benötigt!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
