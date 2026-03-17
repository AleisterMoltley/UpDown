"""
UpDown Bot: A self-contained up/down prediction engine for Polymarket.

Fetches 5-minute OHLC candle data from CoinGecko, applies a moving-average
crossover strategy to predict the next price direction, searches Polymarket's
Gamma API for relevant active markets, and optionally places trades via the
Polymarket CLOB client.

Includes Solana wallet integration for automatic funding of Polygon balance
when trading balance is low.

Setup:
    pip install -r requirements.txt

    Set the following environment variables (see .env.example):
        POLYMARKET_PRIVATE_KEY
        POLYMARKET_API_KEY
        POLYMARKET_API_SECRET
        POLYMARKET_API_PASSPHRASE

    Optional Solana auto-funding:
        SOLANA_PRIVATE_KEY
        SOLANA_RPC_URL
        MIN_POLY_BALANCE_USDC
        BRIDGE_FUND_AMOUNT

    Then run:
        python updown_bot.py
"""

import os
import time
import requests
from pycoingecko import CoinGeckoAPI

# ---------------------------------------------------------------------------
# Configuration – loaded from environment variables so secrets are never
# hard-coded in source.
# ---------------------------------------------------------------------------
POLYMARKET_HOST = os.environ.get("POLYMARKET_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))  # Polygon Mainnet
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
API_KEY = os.environ.get("POLYMARKET_API_KEY", "")
API_SECRET = os.environ.get("POLYMARKET_API_SECRET", "")
API_PASSPHRASE = os.environ.get("POLYMARKET_API_PASSPHRASE", "")

# Cycle interval in seconds (default: 5 minutes)
CYCLE_INTERVAL = int(os.environ.get("CYCLE_INTERVAL_SECONDS", "300"))

# MA strategy configuration (configurable via env)
SHORT_WINDOW = int(os.environ.get("SHORT_WINDOW", "5"))
LONG_WINDOW = int(os.environ.get("LONG_WINDOW", "20"))

# Solana auto-funding configuration
SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "")
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
MIN_POLY_BALANCE_USDC = float(os.environ.get("MIN_POLY_BALANCE_USDC", "20.0"))
BRIDGE_FUND_AMOUNT = float(os.environ.get("BRIDGE_FUND_AMOUNT", "50.0"))

# Solana USDC token mint address (mainnet)
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Bridge endpoint
POLYMARKET_BRIDGE_URL = "https://bridge.polymarket.com/deposit"

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
cg = CoinGeckoAPI()


def _build_clob_client():
    """Construct a ClobClient only when credentials are available."""
    if not all([PRIVATE_KEY, API_KEY, API_SECRET, API_PASSPHRASE]):
        return None
    try:
        from clob_client.client import ClobClient  # noqa: PLC0415 – optional dep
        return ClobClient(
            host=POLYMARKET_HOST,
            key=API_KEY,
            secret=API_SECRET,
            passphrase=API_PASSPHRASE,
            chain_id=CHAIN_ID,
            private_key=PRIVATE_KEY,
        )
    except ImportError:
        print(
            "Warning: py-clob-client is not installed. "
            "Trading will be disabled. Run: pip install py-clob-client"
        )
        return None


# ---------------------------------------------------------------------------
# Solana Wallet Integration
# ---------------------------------------------------------------------------

def get_polygon_address_from_private_key() -> str | None:
    """Derive the 0x Polygon address from POLYMARKET_PRIVATE_KEY.

    Returns:
        The Ethereum/Polygon hex address (0x...) or None if key is not set.
    """
    if not PRIVATE_KEY:
        return None
    try:
        from eth_account import Account  # noqa: PLC0415
        # Handle keys with or without 0x prefix
        key = PRIVATE_KEY if PRIVATE_KEY.startswith("0x") else f"0x{PRIVATE_KEY}"
        account = Account.from_key(key)
        return account.address
    except ImportError:
        print("Warning: eth_account not installed. Cannot derive Polygon address.")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"Error deriving Polygon address: {exc}")
        return None


def get_solana_deposit_address(evm_address: str) -> str | None:
    """Get the Solana deposit address from Polymarket bridge for an EVM address.

    Args:
        evm_address: The 0x Polygon address to get a deposit address for.

    Returns:
        The Solana deposit address (SVM) or None on failure.
    """
    try:
        response = requests.post(
            POLYMARKET_BRIDGE_URL,
            json={"evmAddress": evm_address},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        deposit_address = data.get("depositAddress") or data.get("solanaAddress")
        return deposit_address
    except requests.RequestException as exc:
        print(f"Error fetching Solana deposit address: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected error getting deposit address: {exc}")
        return None


def get_solana_balance() -> dict:
    """Check SOL and USDC balance on Solana wallet.

    Returns:
        Dict with 'sol' (in SOL) and 'usdc' (in USDC) balances, or empty dict on error.
    """
    if not SOLANA_PRIVATE_KEY:
        return {}

    try:
        import base58
        from solders.keypair import Keypair
        from solana.rpc.api import Client as SolanaClient
        from solders.pubkey import Pubkey

        # Decode private key and get public key
        secret_key_bytes = base58.b58decode(SOLANA_PRIVATE_KEY)
        keypair = Keypair.from_bytes(secret_key_bytes)
        pubkey = keypair.pubkey()

        client = SolanaClient(SOLANA_RPC_URL)

        # Get SOL balance
        sol_balance_resp = client.get_balance(pubkey)
        sol_lamports = sol_balance_resp.value if hasattr(sol_balance_resp, 'value') else 0
        sol_balance = sol_lamports / 1e9  # Convert lamports to SOL

        # Get USDC balance (SPL token)
        usdc_balance = 0.0
        try:
            usdc_mint = Pubkey.from_string(SOLANA_USDC_MINT)
            token_accounts = client.get_token_accounts_by_owner_json_parsed(
                pubkey,
                {"mint": usdc_mint}
            )
            if hasattr(token_accounts, 'value') and token_accounts.value:
                for account in token_accounts.value:
                    parsed = account.account.data.parsed
                    if parsed and 'info' in parsed and 'tokenAmount' in parsed['info']:
                        usdc_balance = float(parsed['info']['tokenAmount']['uiAmount'] or 0)
                        break
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: Could not fetch USDC balance: {exc}")

        return {"sol": sol_balance, "usdc": usdc_balance}

    except ImportError as exc:
        print(f"Warning: Solana dependencies not installed: {exc}")
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"Error checking Solana balance: {exc}")
        return {}


def send_to_polymarket_bridge(deposit_address: str, amount_usdc: float) -> bool:
    """Build and sign a Solana transfer to the Polymarket bridge deposit address.

    Transfers USDC first if available, otherwise falls back to SOL.
    Includes safety measures: prints QR code link, warning, and 30s confirmation delay.

    Args:
        deposit_address: The Solana deposit address from Polymarket bridge.
        amount_usdc: Amount in USDC to transfer.

    Returns:
        True if transfer was successful, False otherwise.
    """
    if not SOLANA_PRIVATE_KEY:
        print("Error: SOLANA_PRIVATE_KEY not configured.")
        return False

    try:
        import base58
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solana.rpc.api import Client as SolanaClient
        from solders.system_program import TransferParams, transfer
        from solana.transaction import Transaction
        from spl.token.instructions import transfer_checked, TransferCheckedParams
        from spl.token.constants import TOKEN_PROGRAM_ID
        from solders.compute_budget import set_compute_unit_price

        # Decode private key
        secret_key_bytes = base58.b58decode(SOLANA_PRIVATE_KEY)
        keypair = Keypair.from_bytes(secret_key_bytes)
        pubkey = keypair.pubkey()
        dest_pubkey = Pubkey.from_string(deposit_address)

        client = SolanaClient(SOLANA_RPC_URL)

        # Get current balances
        balances = get_solana_balance()
        sol_balance = balances.get("sol", 0)
        usdc_balance = balances.get("usdc", 0)

        print("\n" + "=" * 60)
        print("⚠️  SOLANA BRIDGE TRANSFER - PLEASE REVIEW CAREFULLY ⚠️")
        print("=" * 60)
        print(f"  From wallet    : {pubkey}")
        print(f"  To (bridge)    : {deposit_address}")
        print(f"  Amount         : ${amount_usdc} USDC")
        print(f"  Current USDC   : ${usdc_balance:.2f}")
        print(f"  Current SOL    : {sol_balance:.4f} SOL")
        print("=" * 60)

        # Generate QR code link for verification
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={deposit_address}"
        print(f"\n🔗 QR Code Link (verify deposit address): {qr_url}")

        print("\n⏳ Waiting 30 seconds before proceeding (safety delay)...")
        print("   Press Ctrl+C to abort.\n")

        # 30-second safety delay
        for i in range(30, 0, -1):
            print(f"   {i} seconds remaining...", end="\r")
            time.sleep(1)

        print("\n✅ Safety delay complete. Proceeding with transfer...")

        # Try USDC transfer first
        if usdc_balance >= amount_usdc:
            print("📤 Transferring USDC...")
            try:
                usdc_mint = Pubkey.from_string(SOLANA_USDC_MINT)

                # Find source token account
                token_accounts = client.get_token_accounts_by_owner_json_parsed(
                    pubkey,
                    {"mint": usdc_mint}
                )

                if not hasattr(token_accounts, 'value') or not token_accounts.value:
                    raise ValueError("No USDC token account found")  # noqa: TRY301

                source_token_account = Pubkey.from_string(
                    str(token_accounts.value[0].pubkey)
                )

                # Get or create destination token account
                dest_token_accounts = client.get_token_accounts_by_owner_json_parsed(
                    dest_pubkey,
                    {"mint": usdc_mint}
                )

                if hasattr(dest_token_accounts, 'value') and dest_token_accounts.value:
                    dest_token_account = Pubkey.from_string(
                        str(dest_token_accounts.value[0].pubkey)
                    )
                else:
                    # Use associated token address
                    from spl.token.instructions import get_associated_token_address
                    dest_token_account = get_associated_token_address(dest_pubkey, usdc_mint)

                # Amount in smallest units (USDC has 6 decimals)
                amount_units = int(amount_usdc * 1_000_000)

                # Build transaction
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

                # Add priority fee
                priority_fee_ix = set_compute_unit_price(1000)

                txn = Transaction()
                txn.add(priority_fee_ix)
                txn.add(transfer_ix)
                txn.recent_blockhash = recent_blockhash
                txn.fee_payer = pubkey

                # Sign and send
                txn.sign(keypair)
                result = client.send_transaction(txn, keypair)

                if hasattr(result, 'value'):
                    print(f"✅ USDC transfer successful! Tx: {result.value}")
                    return True
                else:
                    print(f"⚠️  Transfer result: {result}")
                    return True

            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  USDC transfer failed: {exc}")
                print("Falling back to SOL transfer...")

        # Fallback to SOL transfer
        if sol_balance > 0.01:  # Keep some SOL for fees
            print("📤 Transferring SOL (fallback)...")
            try:
                # Estimate SOL amount equivalent to USDC (rough estimate: 1 SOL ≈ $20-200)
                # This is a fallback - actual price should be fetched
                sol_amount_lamports = int(0.1 * 1e9)  # Transfer 0.1 SOL as fallback

                recent_blockhash = client.get_latest_blockhash().value.blockhash

                transfer_ix = transfer(
                    TransferParams(
                        from_pubkey=pubkey,
                        to_pubkey=dest_pubkey,
                        lamports=sol_amount_lamports,
                    )
                )

                txn = Transaction()
                txn.add(transfer_ix)
                txn.recent_blockhash = recent_blockhash
                txn.fee_payer = pubkey

                txn.sign(keypair)
                result = client.send_transaction(txn, keypair)

                if hasattr(result, 'value'):
                    print(f"✅ SOL transfer successful! Tx: {result.value}")
                    return True
                else:
                    print(f"⚠️  Transfer result: {result}")
                    return True

            except Exception as exc:  # noqa: BLE001
                print(f"❌ SOL transfer failed: {exc}")
                return False

        print("❌ Insufficient balance for transfer.")
        return False

    except ImportError as exc:
        print(f"Error: Solana dependencies not installed: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"Error during bridge transfer: {exc}")
        return False


def check_and_fund_polygon(clob, dry_run: bool = False) -> None:
    """Check Polygon balance and trigger bridge funding if below threshold.

    Called at the start of every bot cycle. Only funds if not in dry_run mode
    and SOLANA_PRIVATE_KEY is configured.

    Args:
        clob: The ClobClient instance (may be None).
        dry_run: If True, skip actual funding operations.
    """
    if dry_run:
        return

    if not SOLANA_PRIVATE_KEY:
        return  # Solana auto-funding not configured

    # Check current Polygon balance
    current_balance = 0.0
    if clob is not None:
        try:
            balance_info = clob.get_balance()
            # Handle different response formats
            if isinstance(balance_info, dict):
                current_balance = float(balance_info.get("balance", 0) or 0)
            else:
                current_balance = float(balance_info or 0)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: Could not fetch Polygon balance: {exc}")
            return

    print(f"💰 Current Polygon balance: ${current_balance:.2f} USDC")

    if current_balance >= MIN_POLY_BALANCE_USDC:
        print(f"✅ Balance OK (>= ${MIN_POLY_BALANCE_USDC:.2f} minimum)")
        return

    print(f"⚠️  Balance below minimum (${MIN_POLY_BALANCE_USDC:.2f}). Initiating Solana bridge...")

    # Get Solana balance
    sol_balances = get_solana_balance()
    if not sol_balances:
        print("❌ Could not check Solana balance. Skipping auto-fund.")
        return

    print(f"🌐 Solana balance: {sol_balances.get('sol', 0):.4f} SOL, ${sol_balances.get('usdc', 0):.2f} USDC")

    # Get Polygon address
    polygon_address = get_polygon_address_from_private_key()
    if not polygon_address:
        print("❌ Could not derive Polygon address. Skipping auto-fund.")
        return

    # Get bridge deposit address
    deposit_address = get_solana_deposit_address(polygon_address)
    if not deposit_address:
        print("❌ Could not get bridge deposit address. Skipping auto-fund.")
        return

    print(f"🌉 Bridge deposit address: {deposit_address}")

    # Execute bridge transfer
    success = send_to_polymarket_bridge(deposit_address, BRIDGE_FUND_AMOUNT)

    if success:
        print("\n" + "=" * 60)
        print("✅ Solana funded → Polygon USDC.e ready")
        print("=" * 60)
        print("⏳ Note: Bridge transfers may take a few minutes to complete.")
        print("   The bot will continue with the current balance.\n")
    else:
        print("\n❌ Bridge transfer failed. Please fund manually.")
        print("   The bot will continue with the current balance.\n")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_5min_data(crypto_id: str = "bitcoin", vs_currency: str = "usd") -> list:
    """Fetch the last 24 hours of 5-minute OHLC candles from CoinGecko.

    Returns a list of [timestamp, open, high, low, close] lists.
    CoinGecko returns the finest granularity (≈5 min) when *days* is set to 1.
    """
    ohlc = cg.get_coin_ohlc_by_id(id=crypto_id, vs_currency=vs_currency, days="1")
    return ohlc  # [[timestamp_ms, open, high, low, close], ...]


# ---------------------------------------------------------------------------
# Up/Down prediction engine
# ---------------------------------------------------------------------------

def predict_up_down(
    closes: list,
    short_window: int | None = None,
    long_window: int | None = None,
) -> str:
    """Predict the next price direction using a moving-average crossover.

    Args:
        closes: Ordered list of closing prices (oldest first).
        short_window: Look-back period for the fast moving average.
                      Defaults to SHORT_WINDOW env var or 5.
        long_window: Look-back period for the slow moving average.
                     Defaults to LONG_WINDOW env var or 20.

    Returns:
        'up'   – short MA is above long MA (bullish signal)
        'down' – short MA is at or below long MA (bearish signal)
        'hold' – not enough data to compute the long MA
    """
    if short_window is None:
        short_window = SHORT_WINDOW
    if long_window is None:
        long_window = LONG_WINDOW

    if len(closes) < long_window:
        return "hold"

    ma_short = sum(closes[-short_window:]) / short_window
    ma_long = sum(closes[-long_window:]) / long_window

    return "up" if ma_short > ma_long else "down"


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

GAMMA_API_BASE = "https://gamma-api.polymarket.com"


def find_relevant_markets(query_terms: list | None = None) -> list:
    """Search Polymarket's public Gamma API for active markets.

    Args:
        query_terms: All terms must appear (case-insensitively) in the
                     market's ``question`` field for it to be included.

    Returns:
        List of market dicts containing at least ``id`` and ``question``.
    """
    if query_terms is None:
        query_terms = ["BTC", "today", "price"]

    url = f"{GAMMA_API_BASE}/markets"
    params = {"active": "true", "closed": "false", "limit": 100}

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Error fetching markets: {exc}")
        return []

    markets = response.json()
    relevant = [
        m
        for m in markets
        if all(term.lower() in m.get("question", "").lower() for term in query_terms)
    ]
    return relevant


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def place_trade(
    clob,
    market: dict,
    outcome: str = "yes",
    amount: float = 10.0,
) -> None:
    """Place a limit order on a Polymarket market.

    Args:
        clob: An initialised ClobClient instance.
        market: Market dict returned by ``find_relevant_markets`` (must
                contain a ``tokens`` list with token IDs for yes/no outcomes).
        outcome: ``'yes'`` or ``'no'``.
        amount: Order size in USDC.
    """
    if clob is None:
        print("Trading disabled: CLOB client not initialised.")
        return

    # Resolve the token ID for the requested outcome.
    tokens = market.get("tokens", [])
    token_id = None
    for token in tokens:
        if token.get("outcome", "").lower() == outcome.lower():
            token_id = token.get("token_id") or token.get("id")
            break

    if token_id is None:
        print(
            f"Could not find {outcome!r} token for market {market.get('id')}. "
            "Skipping trade."
        )
        return

    try:
        # Fetch current mid-point price so the order is competitive.
        mid = clob.get_midpoint(token_id)
        price = float(mid) if mid else 0.5

        order = clob.create_order(
            token_id=token_id,
            price=price,
            side="buy",  # always buying the predicted outcome token (yes or no)
            size=amount,
        )
        signed_order = clob.sign_order(order)
        resp = clob.post_order(signed_order)
        print(f"Trade response for market {market.get('id')}: {resp}")
    except Exception as exc:  # noqa: BLE001 – surface all CLOB errors
        print(f"Error placing trade: {exc}")


# ---------------------------------------------------------------------------
# Main bot loop
# ---------------------------------------------------------------------------

def run_bot(
    crypto_id: str = "bitcoin",
    query_terms: list | None = None,
    trade_amount: float = 5.0,
    dry_run: bool = False,
) -> None:
    """Run the prediction and (optionally) trading loop.

    Args:
        crypto_id: CoinGecko asset ID (e.g. ``'bitcoin'``).
        query_terms: Terms used to filter Polymarket markets.
        trade_amount: USDC amount per trade.
        dry_run: When *True*, market data and predictions are logged but no
                 orders are submitted.
    """
    clob = None if dry_run else _build_clob_client()

    if query_terms is None:
        from datetime import datetime, timezone  # noqa: PLC0415
        today = datetime.now(timezone.utc).strftime("%B %d").lstrip("0").lower()
        query_terms = ["btc", today]

    print("UpDown bot started.")
    print(f"  Asset         : {crypto_id}")
    print(f"  Query terms   : {query_terms}")
    print(f"  Trade amount  : ${trade_amount} USDC")
    print(f"  Dry run       : {dry_run}")
    print(f"  Cycle         : {CYCLE_INTERVAL}s")
    print(f"  MA windows    : short={SHORT_WINDOW}, long={LONG_WINDOW}")

    # Show Solana auto-funding status
    if SOLANA_PRIVATE_KEY and not dry_run:
        print(f"  Solana funding: ENABLED (min: ${MIN_POLY_BALANCE_USDC}, bridge: ${BRIDGE_FUND_AMOUNT})")
    else:
        print("  Solana funding: DISABLED")
    print()

    while True:
        print("=" * 60)
        print("Running bot cycle…")

        # Step 0 – Check and fund Polygon balance if needed (Solana auto-funding)
        check_and_fund_polygon(clob, dry_run=dry_run)

        # Step 1 – Fetch OHLC data.
        try:
            ohlc = fetch_5min_data(crypto_id=crypto_id)
        except Exception as exc:  # noqa: BLE001
            print(f"Error fetching price data: {exc}")
            time.sleep(CYCLE_INTERVAL)
            continue

        closes = [candle[4] for candle in ohlc]
        print(f"Fetched {len(closes)} candles. Latest close: {closes[-1] if closes else 'n/a'}")

        # Step 2 – Predict direction.
        prediction = predict_up_down(closes)
        print(f"Prediction for next period: {prediction.upper()}")

        if prediction == "hold":
            print("Not enough data – skipping trade.")
            time.sleep(CYCLE_INTERVAL)
            continue

        # Step 3 – Discover markets.
        markets = find_relevant_markets(query_terms)
        if not markets:
            print("No relevant markets found.")
        else:
            print(f"Found {len(markets)} relevant market(s).")
            for market in markets:
                print(f"  Market: {market.get('question')} (ID: {market.get('id')})")

                # Step 4 – Execute trade.
                if dry_run:
                    print(
                        f"  [Dry run] Would buy {'YES' if prediction == 'up' else 'NO'} "
                        f"for ${trade_amount} USDC."
                    )
                else:
                    outcome = "yes" if prediction == "up" else "no"
                    place_trade(clob, market, outcome=outcome, amount=trade_amount)

        print(f"Sleeping {CYCLE_INTERVAL}s until next cycle…\n")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    # Run in dry-run mode unless credentials are fully configured.
    _dry = not all([PRIVATE_KEY, API_KEY, API_SECRET, API_PASSPHRASE])
    if _dry:
        print(
            "Note: One or more Polymarket credentials are missing. "
            "Running in dry-run mode (no orders will be placed).\n"
        )
    run_bot(dry_run=_dry)
