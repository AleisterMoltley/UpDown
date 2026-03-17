"""
Market Scanner Module for UpDown Bot.

This module expands market discovery beyond BTC daily price markets by:
1. Fetching ALL active Polymarket markets with volume > $10k
2. Categorizing them (crypto, politics, sports, etc.)
3. Identifying markets where current price deviates significantly from historical mean

Usage:
    from market_scanner import scan_all_markets, get_top_mispriced_markets
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Gamma API endpoint
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Minimum volume threshold in USD
MIN_VOLUME_USD = 10000

# Maximum offset for paginated market fetching (safety limit)
MAX_FETCH_OFFSET = 5000

# Neutral probability baseline when no historical data is available
# 0.5 represents a 50% probability - the neutral expectation for a binary market
NEUTRAL_PROBABILITY_BASELINE = 0.5

# Market categories based on keywords
CATEGORY_KEYWORDS = {
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "crypto", "blockchain",
        "token", "solana", "sol", "xrp", "dogecoin", "doge", "cardano",
        "ada", "binance", "bnb", "usdc", "usdt", "stablecoin", "defi",
    ],
    "politics": [
        "president", "election", "congress", "senate", "trump", "biden",
        "democrat", "republican", "vote", "poll", "governor", "mayor",
        "political", "government", "legislation", "impeach", "primary",
    ],
    "sports": [
        "nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
        "baseball", "hockey", "tennis", "golf", "mma", "ufc", "boxing",
        "super bowl", "world cup", "olympics", "championship", "playoffs",
    ],
    "entertainment": [
        "oscar", "grammy", "emmy", "movie", "film", "tv", "show",
        "celebrity", "music", "concert", "award", "streaming", "netflix",
    ],
    "economics": [
        "fed", "interest rate", "inflation", "gdp", "unemployment",
        "stock", "s&p", "nasdaq", "dow", "recession", "economy",
        "treasury", "bond", "yield", "cpi", "fomc",
    ],
    "tech": [
        "ai", "artificial intelligence", "chatgpt", "openai", "google",
        "apple", "microsoft", "meta", "tesla", "tech", "startup",
        "ipo", "acquisition", "silicon valley",
    ],
    "world": [
        "war", "ukraine", "russia", "china", "nato", "un", "treaty",
        "summit", "international", "diplomatic", "military",
    ],
}


def categorize_market(question: str) -> str:
    """Categorize a market based on its question text.

    Uses word boundary matching to avoid false positives (e.g., 'nfl' in 'inflation').

    Args:
        question: The market question text.

    Returns:
        Category string (crypto, politics, sports, etc.) or 'other'.
    """
    question_lower = question.lower()

    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            # Use word boundary regex for short keywords (<=3 chars) to avoid substring matches
            if len(kw) <= 3:
                pattern = r'\b' + re.escape(kw) + r'\b'
                if re.search(pattern, question_lower):
                    return category
            else:
                if kw in question_lower:
                    return category

    return "other"


def fetch_all_active_markets(min_volume: float = MIN_VOLUME_USD) -> list[dict]:
    """Fetch all active Polymarket markets above volume threshold.

    Uses pagination to get all markets, not just first page.

    Args:
        min_volume: Minimum volume in USD to include market.

    Returns:
        List of market dictionaries meeting volume criteria.
    """
    all_markets = []
    offset = 0
    limit = 100  # Max per page

    while True:
        try:
            url = f"{GAMMA_API_BASE}/markets"
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            }

            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            markets = response.json()

            if not markets:
                break

            all_markets.extend(markets)
            offset += limit

            # Safety limit to avoid infinite loops
            if offset > MAX_FETCH_OFFSET:
                logger.warning("Reached maximum offset limit for market fetch")
                break

        except requests.RequestException as e:
            logger.error(f"Error fetching markets at offset {offset}: {e}")
            break

    # Filter by volume
    filtered = []
    for market in all_markets:
        volume = _get_market_volume(market)
        if volume >= min_volume:
            filtered.append(market)

    logger.info(f"Found {len(filtered)} markets with volume >= ${min_volume:,.0f}")
    return filtered


def _get_market_volume(market: dict) -> float:
    """Extract trading volume from market data.

    Args:
        market: Market dictionary from Gamma API.

    Returns:
        Total volume in USD.
    """
    # Try different field names for volume
    volume = market.get("volume", 0)
    if not volume:
        volume = market.get("volumeNum", 0)
    if not volume:
        volume = market.get("volume24hr", 0)
    if not volume:
        # Sum token volumes if available
        tokens = market.get("tokens", [])
        volume = sum(float(t.get("volume", 0)) for t in tokens)

    return float(volume) if volume else 0.0


def _get_market_volume_24h(market: dict) -> float:
    """Extract 24-hour trading volume from market data.

    Args:
        market: Market dictionary from Gamma API.

    Returns:
        24-hour volume in USD.
    """
    # Try different field names for 24h volume
    volume_24h = market.get("volume24hr", 0)
    if not volume_24h:
        volume_24h = market.get("volume24h", 0)
    if not volume_24h:
        volume_24h = market.get("volumeNum24hr", 0)
    if not volume_24h:
        # Fallback: sum token 24h volumes if available
        tokens = market.get("tokens", [])
        volume_24h = sum(float(t.get("volume24hr", 0) or t.get("volume24h", 0) or 0) for t in tokens)

    return float(volume_24h) if volume_24h else 0.0


def _get_market_end_date(market: dict) -> datetime | None:
    """Extract market end/resolution date.

    Args:
        market: Market dictionary from Gamma API.

    Returns:
        End date as datetime or None if unavailable.
    """
    # Try different field names for end date
    end_date_str = market.get("endDate") or market.get("end_date") or market.get("endDateIso")
    if not end_date_str:
        end_date_str = market.get("resolutionDate") or market.get("resolution_date")

    if not end_date_str:
        return None

    try:
        # Handle ISO format with or without timezone
        if isinstance(end_date_str, str):
            # Remove 'Z' suffix and parse
            end_date_str = end_date_str.replace("Z", "+00:00")
            return datetime.fromisoformat(end_date_str)
        elif isinstance(end_date_str, (int, float)):
            # Unix timestamp
            return datetime.fromtimestamp(end_date_str, tz=timezone.utc)
    except (ValueError, TypeError):
        pass

    return None


def _get_current_price(market: dict) -> float | None:
    """Get the current YES token price for a market.

    Args:
        market: Market dictionary from Gamma API.

    Returns:
        Current YES price (0-1) or None if unavailable.
    """
    tokens = market.get("tokens", [])
    for token in tokens:
        outcome = token.get("outcome", "").lower()
        if outcome == "yes":
            price = token.get("price")
            if price is not None:
                return float(price)

    # Fallback: check for outcomePrices field
    outcome_prices = market.get("outcomePrices")
    if outcome_prices and isinstance(outcome_prices, list) and len(outcome_prices) > 0:
        return float(outcome_prices[0])

    return None


def _get_price_history(market: dict) -> list[float]:
    """Get historical price data for a market.

    The Gamma API may provide price history in different formats.
    We try to extract as much historical data as available.

    Args:
        market: Market dictionary from Gamma API.

    Returns:
        List of historical prices (oldest to newest).
    """
    prices = []

    # Check for priceHistory field
    price_history = market.get("priceHistory", [])
    if price_history:
        for entry in price_history:
            if isinstance(entry, dict):
                price = entry.get("price") or entry.get("yes")
                if price is not None:
                    prices.append(float(price))
            elif isinstance(entry, (int, float)):
                prices.append(float(entry))

    # If no history, check for price changes
    if not prices:
        price_changes = market.get("priceChanges", [])
        if price_changes:
            for change in price_changes:
                if isinstance(change, dict):
                    price = change.get("price")
                    if price is not None:
                        prices.append(float(price))

    return prices


def calculate_price_deviation(market: dict) -> dict[str, Any]:
    """Calculate how much current price deviates from historical mean.

    Args:
        market: Market dictionary from Gamma API.

    Returns:
        Dictionary with:
        - current_price: Current YES price
        - historical_mean: Mean of historical prices
        - deviation: Absolute deviation from mean
        - deviation_pct: Percentage deviation from mean
        - direction: 'underpriced' or 'overpriced'
    """
    current_price = _get_current_price(market)
    if current_price is None:
        return {
            "current_price": None,
            "historical_mean": None,
            "deviation": 0,
            "deviation_pct": 0,
            "direction": "unknown",
        }

    price_history = _get_price_history(market)

    # Use neutral baseline when no historical data is available
    if not price_history:
        historical_mean = NEUTRAL_PROBABILITY_BASELINE
    else:
        historical_mean = sum(price_history) / len(price_history)

    deviation = current_price - historical_mean
    if historical_mean > 0:
        deviation_pct = (deviation / historical_mean) * 100
    else:
        deviation_pct = 0

    direction = "underpriced" if deviation < 0 else "overpriced"

    return {
        "current_price": current_price,
        "historical_mean": historical_mean,
        "deviation": deviation,
        "deviation_pct": deviation_pct,
        "direction": direction,
    }


def scan_all_markets(min_volume: float = MIN_VOLUME_USD) -> list[dict]:
    """Scan all active markets and add category and deviation data.

    Args:
        min_volume: Minimum volume threshold in USD.

    Returns:
        List of market dictionaries with added fields:
        - category: Market category
        - price_deviation: Deviation analysis dict
    """
    markets = fetch_all_active_markets(min_volume)
    scanned = []

    for market in markets:
        question = market.get("question", "")
        category = categorize_market(question)
        deviation = calculate_price_deviation(market)

        scanned_market = {
            **market,
            "category": category,
            "price_deviation": deviation,
        }
        scanned.append(scanned_market)

    return scanned


def get_top_mispriced_markets(
    count: int = 5,
    min_volume: float = MIN_VOLUME_USD,
    min_deviation_pct: float = 10.0,
    min_volume_24h: float | None = None,
    categories: list[str] | None = None,
    max_hours_to_settlement: float | None = None,
    prioritize_politics: bool = False,
) -> list[dict]:
    """Get the top mispriced markets sorted by deviation.

    Args:
        count: Number of top markets to return.
        min_volume: Minimum volume threshold in USD.
        min_deviation_pct: Minimum absolute deviation percentage to consider.
        min_volume_24h: Minimum 24h volume in USD (optional filter).
        categories: List of allowed categories (e.g., ["crypto", "politics", "economics"]).
        max_hours_to_settlement: Maximum hours until market settlement (optional filter).
        prioritize_politics: If True, sort politics markets with >8% deviation higher.

    Returns:
        List of top mispriced markets sorted by absolute deviation.
    """
    markets = scan_all_markets(min_volume)
    now = datetime.now(timezone.utc)

    # Filter markets with significant deviation
    mispriced = []
    for m in markets:
        # Check deviation threshold
        deviation_pct = abs(m["price_deviation"].get("deviation_pct", 0))
        if deviation_pct < min_deviation_pct:
            continue
        if m["price_deviation"].get("current_price") is None:
            continue

        # Filter by 24h volume if specified
        if min_volume_24h is not None:
            volume_24h = _get_market_volume_24h(m)
            if volume_24h < min_volume_24h:
                continue

        # Filter by categories if specified
        if categories is not None:
            if m.get("category", "other") not in categories:
                continue

        # Filter by time to settlement if specified
        if max_hours_to_settlement is not None:
            end_date = _get_market_end_date(m)
            if end_date is not None:
                hours_to_settlement = (end_date - now).total_seconds() / 3600
                if hours_to_settlement < 0 or hours_to_settlement > max_hours_to_settlement:
                    continue
            # If no end_date available, exclude if filter is active
            else:
                continue

        mispriced.append(m)

    # Sort by absolute deviation percentage (highest first)
    # If prioritize_politics is True, give politics markets with >8% deviation a boost
    def sort_key(m: dict) -> tuple:
        deviation = abs(m["price_deviation"].get("deviation_pct", 0))
        is_politics_high_dev = (
            prioritize_politics
            and m.get("category") == "politics"
            and deviation > 8.0
        )
        # Return tuple: (is_priority, deviation) - priority items first, then by deviation
        return (not is_politics_high_dev, -deviation)

    mispriced.sort(key=sort_key)

    return mispriced[:count]


def format_scan_results(markets: list[dict]) -> str:
    """Format scan results for Telegram display.

    Args:
        markets: List of scanned market dictionaries.

    Returns:
        Formatted string for Telegram message.
    """
    if not markets:
        return "No mispriced markets found with current criteria."

    lines = ["🔎 **Top Mispriced Markets**\n"]

    for i, market in enumerate(markets, 1):
        question = market.get("question", "Unknown")[:60]
        category = market.get("category", "other")
        deviation = market.get("price_deviation", {})

        current = deviation.get("current_price", 0)
        mean = deviation.get("historical_mean", 0.5)
        dev_pct = deviation.get("deviation_pct", 0)
        direction = deviation.get("direction", "unknown")
        volume = _get_market_volume(market)

        # Emoji based on direction
        emoji = "📉" if direction == "underpriced" else "📈"

        lines.append(f"**{i}. {question}...**")
        lines.append(f"   {emoji} {direction.upper()} by {abs(dev_pct):.1f}%")
        lines.append(f"   📊 Price: {current:.2%} (Mean: {mean:.2%})")
        lines.append(f"   🏷️ Category: {category.capitalize()}")
        lines.append(f"   💰 Volume: ${volume:,.0f}")
        lines.append("")

    lines.append(f"_Scanned at {datetime.now(timezone.utc).strftime('%H:%M UTC')}_")

    return "\n".join(lines)


def get_category_summary(markets: list[dict]) -> dict[str, int]:
    """Get count of markets by category.

    Args:
        markets: List of scanned market dictionaries.

    Returns:
        Dictionary mapping category to count.
    """
    summary: dict[str, int] = {}
    for market in markets:
        category = market.get("category", "other")
        summary[category] = summary.get(category, 0) + 1
    return summary
