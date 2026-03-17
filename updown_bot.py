"""
UpDown Bot: A self-contained up/down prediction engine for Polymarket.

Fetches 5-minute OHLC candle data from CoinGecko, applies a moving-average
crossover strategy to predict the next price direction, searches Polymarket's
Gamma API for relevant active markets, and optionally places trades via the
Polymarket CLOB client.

Includes Solana wallet integration for automatic funding of Polygon balance
when trading balance is low.

100% Onchain-Modus:
    - Keine API-Credentials mehr benötigt
    - L2-Credentials werden automatisch abgeleitet
    - Nur noch POLYMARKET_PRIVATE_KEY erforderlich

Setup:
    pip install -r requirements.txt

    Set the following environment variables (see .env.example):
        POLYMARKET_PRIVATE_KEY

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

# Import market scanner module for Scanner-First logic
from market_scanner import get_top_mispriced_markets

# ---------------------------------------------------------------------------
# Configuration – loaded from environment variables so secrets are never
# hard-coded in source.
# ---------------------------------------------------------------------------
POLYMARKET_HOST = os.environ.get("POLYMARKET_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))  # Polygon Mainnet
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
# API-Credentials werden im 100% onchain-Modus nicht mehr benötigt
# Sie werden automatisch aus dem Private Key abgeleitet

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

# Kelly Criterion constants (backtest-derived)
KELLY_AVG_WIN_PCT = 0.07  # 7% average win from backtest
KELLY_AVG_LOSS_PCT = 0.04  # 4% average loss from backtest
KELLY_MAX_FRACTION = 0.25  # Maximum 25% Kelly fraction
KELLY_MIN_TRADE_USD = 3.0  # Minimum $3 per trade
KELLY_SLIPPAGE_BUFFER = 0.005  # 0.5% slippage buffer for edge calculation

# Orderbook imbalance constants
ORDERBOOK_IMBALANCE_THRESHOLD = 0.15  # Min imbalance to apply price edge
ORDERBOOK_PRICE_EDGE = 0.995  # Multiply midpoint by this when imbalance favorable

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
cg = CoinGeckoAPI()

# ---------------------------------------------------------------------------
# L2 Credentials Cache
# ---------------------------------------------------------------------------
# Cache structure: {"api_key": str, "api_secret": str, "api_passphrase": str, "derived_at": float}
_l2_credentials_cache: dict | None = None
_L2_CREDENTIALS_TTL_SECONDS = 3600  # 1 hour TTL


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
    print("L2-Credentials-Cache invalidiert")


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
    if not PRIVATE_KEY:
        print("Warning: POLYMARKET_PRIVATE_KEY not set. Trading will be disabled.")
        return None
    
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        
        # Key mit 0x Prefix normalisieren
        key = PRIVATE_KEY if PRIVATE_KEY.startswith("0x") else f"0x{PRIVATE_KEY}"
        
        # Check if we have valid cached credentials
        if _is_l2_credentials_valid():
            print("Verwende gecachte L2-API-Credentials")
            return ClobClient(
                host=POLYMARKET_HOST,
                chain_id=CHAIN_ID,
                key=key,
                signature_type=0,
                creds=ApiCreds(
                    api_key=_l2_credentials_cache["api_key"],
                    api_secret=_l2_credentials_cache["api_secret"],
                    api_passphrase=_l2_credentials_cache["api_passphrase"],
                ),
            )
        
        # ClobClient im EOA-Modus erstellen (signature_type=0)
        client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=CHAIN_ID,
            key=key,
            signature_type=0,  # EOA signature
        )
        
        # L2-Credentials automatisch ableiten und cachen
        try:
            derived_creds = client.derive_api_key()
            print("L2-API-Credentials erfolgreich abgeleitet und gecached")
            
            # Cache the derived credentials
            _l2_credentials_cache = {
                "api_key": derived_creds.get("apiKey", ""),
                "api_secret": derived_creds.get("secret", ""),
                "api_passphrase": derived_creds.get("passphrase", ""),
                "derived_at": time.time(),
            }
            
            # Client mit abgeleiteten Credentials neu erstellen
            client = ClobClient(
                host=POLYMARKET_HOST,
                chain_id=CHAIN_ID,
                key=key,
                signature_type=0,
                creds=ApiCreds(
                    api_key=_l2_credentials_cache["api_key"],
                    api_secret=_l2_credentials_cache["api_secret"],
                    api_passphrase=_l2_credentials_cache["api_passphrase"],
                ),
            )
        except Exception as e:
            # Fallback: Versuche ohne abgeleitete Credentials
            print(f"Warning: Konnte L2-Credentials nicht ableiten: {e}")
            print("Verwende Client ohne abgeleitete Credentials")
        
        return client
        
    except ImportError:
        print(
            "Warning: py-clob-client is not installed. "
            "Trading will be disabled. Run: pip install py-clob-client"
        )
        return None
    except Exception as e:
        print(f"Error beim Erstellen des ClobClient: {e}")
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
                    raise ValueError(  # noqa: TRY301
                        "No USDC token account found for wallet. "
                        "Ensure your Solana wallet has a USDC token account initialized."
                    )

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
            print("⚠️  Note: SOL fallback uses a fixed 0.1 SOL amount (~$15-20 USD estimate).")
            print("   For precise amounts, ensure sufficient USDC balance on Solana.")
            try:
                # Use a conservative fixed SOL amount for fallback
                # This is intentionally conservative - users should fund with USDC for precise amounts
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
            # Invalidate cache on authentication errors (401/403)
            if _is_auth_error(exc):
                invalidate_l2_credentials_cache()
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
        print("✅ Solana funded → Polygon USDC ready")
        print("=" * 60)
        print("⏳ Note: Bridge transfers may take a few minutes to complete.")
        print("   The bot will continue with the current balance.\n")
    else:
        print("\n❌ Bridge transfer failed. Please fund manually.")
        print("   The bot will continue with the current balance.\n")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def get_polygon_balance(clob) -> float:
    """Get current Polygon USDC balance from CLOB client.
    
    Args:
        clob: The ClobClient instance (may be None).
    
    Returns:
        Current USDC balance, or 100.0 as default if unavailable.
    """
    if clob is None:
        return 100.0  # Default balance for dry run
    
    try:
        balance_info = clob.get_balance()
        if isinstance(balance_info, dict):
            return float(balance_info.get("balance", 0) or 0)
        return float(balance_info or 0)
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            invalidate_l2_credentials_cache()
        print(f"Warning: Could not fetch Polygon balance: {exc}")
        return 100.0  # Default fallback


def fetch_5min_data(
    crypto_id: str = "bitcoin",
    vs_currency: str = "usd",
    max_retries: int = 3,
) -> list:
    """Fetch the last 24 hours of 5-minute OHLC candles from CoinGecko.

    Returns a list of [timestamp, open, high, low, close] lists.
    CoinGecko returns the finest granularity (≈5 min) when *days* is set to 1.

    Args:
        crypto_id: CoinGecko asset ID (e.g., 'bitcoin', 'ethereum').
        vs_currency: Currency for price conversion (e.g., 'usd').
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
            ohlc = cg.get_coin_ohlc_by_id(
                id=crypto_id, vs_currency=vs_currency, days="1"
            )
            if ohlc is None or not isinstance(ohlc, list):
                raise ValueError(f"Invalid response from CoinGecko API: {ohlc}")
            return ohlc
        except requests.exceptions.ConnectionError as e:
            last_error = e
            print(
                f"CoinGecko API connection failed (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                time.sleep(wait_time)
        except requests.exceptions.Timeout as e:
            last_error = e
            print(
                f"CoinGecko API timeout (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
        except Exception as e:
            # For other errors, don't retry - could be rate limit or invalid request
            print(f"CoinGecko API error: {e}")
            raise

    # All retries exhausted
    error_msg = (
        f"Failed to connect to CoinGecko API after {max_retries} attempts. "
        "This may be caused by: network issues, firewall blocking api.coingecko.com, "
        "or CoinGecko service unavailability."
    )
    print(error_msg)
    raise ConnectionError(error_msg) from last_error


# ---------------------------------------------------------------------------
# Up/Down prediction engine
# ---------------------------------------------------------------------------

def predict_up_down(
    closes: list,
    short_window: int = 0,
    long_window: int = 0,
) -> dict:
    """Predict the next price direction using a moving-average crossover.

    Args:
        closes: Ordered list of closing prices (oldest first).
        short_window: Look-back period for the fast moving average.
                      Defaults to SHORT_WINDOW env var or 5. Pass 0 to use default.
        long_window: Look-back period for the slow moving average.
                     Defaults to LONG_WINDOW env var or 20. Pass 0 to use default.

    Returns:
        Dictionary with:
            - direction: 'up', 'down', or 'hold'
            - confidence: Confidence score (50-100) based on MA crossover strength
    """
    # Use module-level defaults if 0 is passed (sentinel value for "use default")
    if short_window <= 0:
        short_window = SHORT_WINDOW
    if long_window <= 0:
        long_window = LONG_WINDOW

    if len(closes) < long_window:
        return {"direction": "hold", "confidence": 0}

    ma_short = sum(closes[-short_window:]) / short_window
    ma_long = sum(closes[-long_window:]) / long_window
    
    direction = "up" if ma_short > ma_long else "down"
    
    # Calculate confidence based on MA crossover strength
    # Strength = percentage difference between short and long MA
    if ma_long > 0:
        crossover_strength = abs(ma_short - ma_long) / ma_long * 100
        # Scale to confidence: 0% diff = 50% conf, 2%+ diff = 100% conf
        confidence = min(100.0, 50.0 + crossover_strength * 25.0)
    else:
        confidence = 50.0
    
    return {"direction": direction, "confidence": round(confidence, 1)}


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


def calc_kelly_size(
    confidence: float,
    balance: float,
    base_trade_amount: float = 5.0,
) -> dict:
    """Calculate Kelly-optimized position size using backtest-derived edge.
    
    Uses the Kelly Criterion with actual backtest results:
    - edge = (confidence/100 * 0.07) - ((1 - confidence/100) * 0.04)
    - kelly_fraction = edge / 0.07 (capped at 0.25)
    - final_size = min(balance * kelly_fraction, base_trade_amount * 3)
    
    This formula is derived from actual backtest results:
    - 7% average win percentage
    - 4% average loss percentage
    
    Args:
        confidence: Confidence level (0-100) from multi-signal engine.
        balance: Current USDC balance.
        base_trade_amount: Base trade amount from config (default $5).
    
    Returns:
        Dictionary with:
            - size: Final bet size in USD
            - edge: Calculated edge percentage
            - kelly_fraction: Kelly fraction (before capping)
    """
    # Calculate edge using backtest-derived average win/loss percentages
    # edge = (win_prob * avg_win) - (loss_prob * avg_loss) - slippage_buffer
    win_prob = confidence / 100.0
    loss_prob = 1.0 - win_prob
    
    edge = (win_prob * KELLY_AVG_WIN_PCT) - (loss_prob * KELLY_AVG_LOSS_PCT) - KELLY_SLIPPAGE_BUFFER
    edge_pct = edge * 100  # Convert to percentage for logging
    
    # Kelly fraction = edge / avg_win (capped at max 25%)
    if edge <= 0:
        # Negative edge means losing bet, use minimum size
        kelly_fraction = 0.0
        final_size = KELLY_MIN_TRADE_USD
    else:
        kelly_fraction = edge / KELLY_AVG_WIN_PCT
        kelly_fraction = min(kelly_fraction, KELLY_MAX_FRACTION)
        
        # Final bet size: min(balance * kelly_fraction, base_trade_amount * 3)
        kelly_size = balance * kelly_fraction
        max_size = base_trade_amount * 3.0
        final_size = min(kelly_size, max_size)
        
        # Ensure minimum trade size
        final_size = max(KELLY_MIN_TRADE_USD, final_size)
    
    # Log Kelly bet size calculation
    print(f"Kelly bet size: ${final_size:.2f} (edge {edge_pct:.2f}%)")
    
    return {
        "size": round(final_size, 2),
        "edge": round(edge_pct, 2),
        "kelly_fraction": round(kelly_fraction, 4),
    }


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
        # Fetch order book to calculate imbalance for better entry price
        order_book = clob.get_order_book(token_id)
        
        # Calculate midpoint from order book
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        
        # Get best bid and ask prices for midpoint calculation
        best_bid = float(bids[0].get("price", 0)) if bids else 0.0
        best_ask = float(asks[0].get("price", 1)) if asks else 1.0
        mid = (best_bid + best_ask) / 2 if (best_bid > 0 and best_ask > 0) else 0.5
        
        # Calculate total bid and ask volumes
        bid_volume = sum(float(b.get("size", 0)) for b in bids)
        ask_volume = sum(float(a.get("size", 0)) for a in asks)
        total_volume = bid_volume + ask_volume
        
        # Calculate imbalance: (bid_volume - ask_volume) / (bid + ask)
        imbalance = (bid_volume - ask_volume) / total_volume if total_volume > 0 else 0.0
        
        # Apply price edge when imbalance is favorable for YES buys
        # Positive imbalance = more bids than asks = bullish
        price = mid
        if imbalance > ORDERBOOK_IMBALANCE_THRESHOLD and outcome.lower() == "yes":
            price = mid * ORDERBOOK_PRICE_EDGE
            edge_pct = (1 - ORDERBOOK_PRICE_EDGE) * 100
            print(f"Orderbook edge: +{edge_pct:.1f}% (imbalance: {imbalance:.3f})")

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
        # Invalidate cache on authentication errors (401/403)
        if _is_auth_error(exc):
            invalidate_l2_credentials_cache()
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
    """Run the prediction and (optionally) trading loop using Scanner-First logic.

    Scanner-First Logic:
    1. Call get_top_mispriced_markets(count=8, min_deviation_pct=12, prioritize_politics=True)
    2. For each market, calculate signal confidence and filter where signal direction matches deviation direction
    3. Select top-1 with highest (deviation_pct * confidence)
    4. Trade only when combined_score > 75

    Args:
        crypto_id: CoinGecko asset ID (e.g. ``'bitcoin'``).
        query_terms: Deprecated - kept for backward compatibility but not used in scanner-first mode.
        trade_amount: USDC amount per trade.
        dry_run: When *True*, market data and predictions are logged but no
                 orders are submitted.
    """
    clob = None if dry_run else _build_clob_client()

    print("UpDown bot started.")
    print("  Scanner Mode active – searching mispriced")
    print(f"  Asset         : {crypto_id}")
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
        print("Running bot cycle (Scanner-First mode)…")

        # Step 0 – Check and fund Polygon balance if needed (Solana auto-funding)
        check_and_fund_polygon(clob, dry_run=dry_run)

        # Step 1 – Fetch OHLC data for signal calculation
        try:
            ohlc = fetch_5min_data(crypto_id=crypto_id)
        except Exception as exc:  # noqa: BLE001
            print(f"Error fetching price data: {exc}")
            time.sleep(CYCLE_INTERVAL)
            continue

        closes = [candle[4] for candle in ohlc]
        print(f"Fetched {len(closes)} candles. Latest close: {closes[-1] if closes else 'n/a'}")

        # Step 2 – Scanner-First: Get top mispriced markets
        print("Scanner Mode active – searching mispriced")
        try:
            mispriced_markets = get_top_mispriced_markets(
                count=8,
                min_deviation_pct=12,
                prioritize_politics=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Error fetching mispriced markets: {exc}")
            time.sleep(CYCLE_INTERVAL)
            continue

        if not mispriced_markets:
            print("No mispriced markets found.")
            time.sleep(CYCLE_INTERVAL)
            continue

        print(f"Found {len(mispriced_markets)} mispriced market(s).")

        # Step 3 – Calculate signal direction using MA crossover
        prediction_result = predict_up_down(closes)
        signal_direction = prediction_result["direction"]
        base_confidence = prediction_result["confidence"]
        print(f"Base signal: {signal_direction.upper()} (confidence: {base_confidence:.1f}%)")

        if signal_direction == "hold":
            print("Not enough data – skipping trade.")
            time.sleep(CYCLE_INTERVAL)
            continue

        # Step 4 – Filter markets where signal direction matches deviation direction
        # and calculate combined_score = deviation_pct * confidence
        candidates = []
        for market in mispriced_markets:
            price_deviation = market.get("price_deviation", {})
            deviation_pct = abs(price_deviation.get("deviation_pct", 0))
            deviation_direction = price_deviation.get("direction", "unknown")
            
            # Skip markets with unknown deviation direction
            if deviation_direction == "unknown":
                print(
                    f"  ✗ Market: {market.get('question', 'N/A')[:50]}... "
                    f"SKIPPED: Unknown deviation direction"
                )
                continue
            
            # Map deviation direction to expected signal direction:
            # - "underpriced" means price is below historical mean → expect price to go UP → signal should be "up"
            # - "overpriced" means price is above historical mean → expect price to go DOWN → signal should be "down"
            expected_signal_direction = "up" if deviation_direction == "underpriced" else "down"
            
            # Only include markets where signal direction matches expected direction from deviation
            if signal_direction == expected_signal_direction:
                # Calculate combined_score = deviation_pct * confidence
                combined_score = deviation_pct * base_confidence
                candidates.append({
                    "market": market,
                    "deviation_pct": deviation_pct,
                    "deviation_direction": deviation_direction,
                    "signal_direction": signal_direction,
                    "confidence": base_confidence,
                    "combined_score": combined_score,
                })
                print(
                    f"  ✓ Market: {market.get('question', 'N/A')[:50]}... "
                    f"(deviation: {deviation_pct:.1f}%, combined_score: {combined_score:.1f})"
                )
            else:
                print(
                    f"  ✗ Market: {market.get('question', 'N/A')[:50]}... "
                    f"SKIPPED: Signal ({signal_direction}) != expected ({expected_signal_direction})"
                )

        if not candidates:
            print("No markets with matching signal/deviation direction found.")
            time.sleep(CYCLE_INTERVAL)
            continue

        # Step 5 – Select top-1 with highest combined_score
        candidates.sort(key=lambda x: x["combined_score"], reverse=True)
        best_candidate = candidates[0]
        
        market = best_candidate["market"]
        deviation_pct = best_candidate["deviation_pct"]
        deviation_direction = best_candidate["deviation_direction"]
        confidence = best_candidate["confidence"]
        combined_score = best_candidate["combined_score"]

        print(
            f"\nSelected best market: {market.get('question', 'N/A')[:60]}..."
            f"\n  Combined Score: {combined_score:.1f}"
        )

        # Step 6 – Trade only when combined_score > 75
        if combined_score <= 75:
            print(f"Combined score {combined_score:.1f} <= 75 – skipping trade.")
            time.sleep(CYCLE_INTERVAL)
            continue

        # Get market details
        price_deviation = market.get("price_deviation", {})
        current_price = price_deviation.get("current_price", 0.5)
        historical_mean = price_deviation.get("historical_mean", 0.5)
        category = market.get("category", "other")

        print(f"  Category: {category}")
        print(f"  Signal: {signal_direction.upper()}")
        print(f"  Deviation: {deviation_pct:.1f}% ({deviation_direction})")
        print(f"  Confidence: {confidence:.1f}%")
        print(f"  Current Price: {current_price:.2%}")
        print(f"  Historical Mean: {historical_mean:.2%}")

        # Calculate Kelly-optimized position size and execute trade
        current_balance = get_polygon_balance(clob)
        kelly_result = calc_kelly_size(
            confidence=confidence,
            balance=current_balance,
            base_trade_amount=trade_amount,
        )
        kelly_size = kelly_result["size"]
        kelly_edge = kelly_result["edge"]
        
        if dry_run:
            print(
                f"  [Dry run] Would buy {'YES' if signal_direction == 'up' else 'NO'} "
                f"for ${kelly_size:.2f} USDC (edge {kelly_edge:.2f}%)"
            )
        else:
            outcome = "yes" if signal_direction == "up" else "no"
            place_trade(clob, market, outcome=outcome, amount=kelly_size)

        print(f"\nSleeping {CYCLE_INTERVAL}s until next cycle…\n")
        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    # Run in dry-run mode unless wallet private key is configured.
    # 100% Onchain Mode: Only POLYMARKET_PRIVATE_KEY is required,
    # L2 credentials are automatically derived using derive_api_key().
    _dry = not PRIVATE_KEY
    if _dry:
        print(
            "Note: POLYMARKET_PRIVATE_KEY not set. "
            "Running in dry-run mode (no orders will be placed).\n"
        )
    run_bot(dry_run=_dry)
