"""
UpDown Bot: A self-contained up/down prediction engine for Polymarket.

Fetches 5-minute OHLC candle data from CoinGecko, applies a moving-average
crossover strategy to predict the next price direction, searches Polymarket's
Gamma API for relevant active markets, and optionally places trades via the
Polymarket CLOB client.

Setup:
    pip install -r requirements.txt

    Set the following environment variables (see .env.example):
        POLYMARKET_PRIVATE_KEY
        POLYMARKET_API_KEY
        POLYMARKET_API_SECRET
        POLYMARKET_API_PASSPHRASE

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
    short_window: int = 5,
    long_window: int = 20,
) -> str:
    """Predict the next price direction using a moving-average crossover.

    Args:
        closes: Ordered list of closing prices (oldest first).
        short_window: Look-back period for the fast moving average.
        long_window: Look-back period for the slow moving average.

    Returns:
        'up'   – short MA is above long MA (bullish signal)
        'down' – short MA is at or below long MA (bearish signal)
        'hold' – not enough data to compute the long MA
    """
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
    print(f"  Asset       : {crypto_id}")
    print(f"  Query terms : {query_terms}")
    print(f"  Trade amount: ${trade_amount} USDC")
    print(f"  Dry run     : {dry_run}")
    print(f"  Cycle       : {CYCLE_INTERVAL}s\n")

    while True:
        print("=" * 60)
        print("Running bot cycle…")

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
