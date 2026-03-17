"""
Backtest module for UpDown Telegram Bot.

This module provides historical backtesting capabilities for the multi-signal trading engine.
It uses Walk-Forward Optimization to avoid lookahead bias:
- Splits 30 days into 5 training windows + 1 test window
- Optimizes SHORT_WINDOW/LONG_WINDOW + RSI thresholds per window with brute-force
- Includes volume_momentum signal (10% weight)

Results are saved to daily_pnl.json and displayed in Telegram via /backtest and /status commands.
Optimized parameters are saved to bot_config.json.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pycoingecko import CoinGeckoAPI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKTEST_DAYS = 30  # Fetch 30 days of data
BACKTEST_TRADES = 1000  # Simulate 1000 trades
BACKTEST_INITIAL_BALANCE = 1000.0  # Starting balance for simulation
BACKTEST_TRADE_SIZE_PCT = 0.05  # 5% of balance per trade
MIN_CONFIDENCE_THRESHOLD = 68  # Minimum confidence to execute trade

# Trade holding period: 12 candles of 5-min each = ~1 hour
TRADE_HOLDING_PERIOD_CANDLES = 12

# Annualization factor for Sharpe ratio: sqrt(trades per year)
# With ~1 hour per trade and 24 hours/day, ~8760 trading opportunities per year
# Using sqrt(8760) ≈ 93.6 for hourly annualization
SHARPE_ANNUALIZATION_FACTOR = 93.6

# Kelly Criterion constants (backtest-derived)
KELLY_AVG_WIN_PCT = 0.07  # 7% average win from backtest
KELLY_AVG_LOSS_PCT = 0.04  # 4% average loss from backtest
KELLY_MAX_FRACTION = 0.25  # Maximum 25% Kelly fraction
KELLY_MIN_TRADE_USD = 3.0  # Minimum $3 per trade

# Walk-Forward Optimization constants
WFO_NUM_TRAINING_WINDOWS = 5  # 5 training windows
WFO_PARAM_RANGE_MIN = 5  # Minimum value for parameter optimization
WFO_PARAM_RANGE_MAX = 20  # Maximum value for parameter optimization
VOLUME_MOMENTUM_WEIGHT = 10  # 10% weight for volume_momentum signal
VOLUME_MOMENTUM_BONUS = 15  # +15 confidence when volume & price both rise

# File paths
BACKTEST_RESULTS_FILE = Path("daily_pnl.json")
CONFIG_FILE = Path("bot_config.json")

logger = logging.getLogger(__name__)

# CoinGecko API
cg = CoinGeckoAPI()


# ---------------------------------------------------------------------------
# Technical Indicator Functions (standalone versions for backtest)
# ---------------------------------------------------------------------------


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


def calculate_rsi(closes: list, period: int = 14) -> float:
    """Calculate Relative Strength Index (RSI).
    
    Args:
        closes: List of closing prices.
        period: RSI period (default 14).
    
    Returns:
        RSI value (0-100), or 50 if insufficient data.
    """
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


def calculate_macd(closes: list, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> dict:
    """Calculate MACD (Moving Average Convergence Divergence).
    
    Returns:
        Dictionary with macd_line, signal_line, histogram, and valid flag.
    """
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


# ---------------------------------------------------------------------------
# Signal Calculation Functions
# ---------------------------------------------------------------------------


def calculate_ma_crossover_signal(closes: list, short_window: int = 5, long_window: int = 20) -> dict:
    """Calculate MA crossover signal.
    
    Returns:
        Dictionary with direction, strength, ma_short, ma_long.
    """
    if len(closes) < long_window:
        return {"direction": "neutral", "strength": 0, "ma_short": 0, "ma_long": 0}
    
    ma_short = sum(closes[-short_window:]) / short_window
    ma_long = sum(closes[-long_window:]) / long_window
    
    if ma_long == 0:
        return {"direction": "neutral", "strength": 0, "ma_short": ma_short, "ma_long": ma_long}
    
    pct_diff = ((ma_short - ma_long) / ma_long) * 100
    strength = min(100, abs(pct_diff) * 50)
    direction = "up" if ma_short > ma_long else "down"
    
    return {
        "direction": direction,
        "strength": strength,
        "ma_short": ma_short,
        "ma_long": ma_long,
    }


def calculate_rsi_signal(closes: list, overbought: int = 70, oversold: int = 30) -> dict:
    """Calculate RSI-based signal.
    
    Returns:
        Dictionary with direction, strength, rsi.
    """
    rsi = calculate_rsi(closes)
    
    if rsi <= oversold:
        direction = "up"
        strength = ((oversold - rsi) / oversold) * 100
    elif rsi >= overbought:
        direction = "down"
        strength = ((rsi - overbought) / (100 - overbought)) * 100
    else:
        direction = "neutral"
        strength = abs(rsi - 50) / 20 * 100
        strength = min(30, strength)
    
    return {
        "direction": direction,
        "strength": min(100, strength),
        "rsi": rsi,
    }


def calculate_macd_signal(closes: list) -> dict:
    """Calculate MACD-based signal.
    
    Returns:
        Dictionary with direction, strength, histogram, macd_line, signal_line.
    """
    macd = calculate_macd(closes)
    
    if not macd.get("valid") or not closes:
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
    
    if histogram > 0:
        direction = "up"
    elif histogram < 0:
        direction = "down"
    else:
        direction = "neutral"
    
    current_price = closes[-1] if closes[-1] > 0 else 1
    normalized_histogram = abs(histogram) / current_price * 10000
    strength = min(100, normalized_histogram * 10)
    
    return {
        "direction": direction,
        "strength": strength,
        "histogram": histogram,
        "macd_line": macd_line,
        "signal_line": signal_line,
    }


def calculate_confidence_backtest(
    closes: list,
    config: dict | None = None,
    volumes: list | None = None,
) -> dict:
    """Calculate confidence score for backtesting (Multi-Signal Engine).
    
    This is a standalone version of the multi-signal engine for backtesting,
    without Polymarket API dependencies. Includes volume_momentum signal.
    
    Args:
        closes: List of closing prices.
        config: Optional configuration dict with signal weights and thresholds.
        volumes: Optional list of volume values for volume_momentum signal.
    
    Returns:
        Dictionary with direction, confidence_score, and signal details.
    """
    if config is None:
        config = {}
    
    # Signal weights - now includes volume_momentum at 10%
    # Adjusted from original: ma_crossover=30, rsi=30, macd=25, momentum=15
    # New: ma_crossover=27, rsi=27, macd=23, momentum=13, volume_momentum=10
    weights = config.get("signal_weights", {
        "ma_crossover": 27,
        "rsi": 27,
        "macd": 23,
        "momentum": 13,
        "volume_momentum": VOLUME_MOMENTUM_WEIGHT,  # 10% weight
    })
    
    short_window = config.get("short_window", 5)
    long_window = config.get("long_window", 20)
    rsi_overbought = config.get("rsi_overbought", 70)
    rsi_oversold = config.get("rsi_oversold", 30)
    
    # Calculate individual signals
    ma_signal = calculate_ma_crossover_signal(closes, short_window, long_window)
    rsi_signal = calculate_rsi_signal(closes, rsi_overbought, rsi_oversold)
    macd_signal = calculate_macd_signal(closes)
    
    # Calculate momentum signal (replaces Polymarket delta for backtest)
    if len(closes) >= 6 and closes[-6] > 0:
        momentum_pct = ((closes[-1] - closes[-6]) / closes[-6]) * 100
        momentum_direction = "up" if momentum_pct > 0.1 else "down" if momentum_pct < -0.1 else "neutral"
        momentum_strength = min(100, abs(momentum_pct) * 20)
    else:
        momentum_direction = "neutral"
        momentum_pct = 0
        momentum_strength = 0
    
    momentum_signal = {
        "direction": momentum_direction,
        "strength": momentum_strength,
        "momentum_pct": momentum_pct,
    }
    
    # Calculate volume momentum signal (new)
    if volumes is not None and len(volumes) >= 20:
        volume_signal = calculate_volume_momentum_signal(closes, volumes)
    else:
        volume_signal = {
            "direction": "neutral",
            "strength": 0,
            "volume_rising": False,
            "price_rising": False,
            "bonus": 0,
        }
    
    # Aggregate scores
    up_score = 0
    down_score = 0
    total_weight = 0
    
    for signal_name, signal in [
        ("ma_crossover", ma_signal),
        ("rsi", rsi_signal),
        ("macd", macd_signal),
        ("momentum", momentum_signal),
        ("volume_momentum", volume_signal),
    ]:
        weight = weights.get(signal_name, 0)
        total_weight += weight
        
        if signal["direction"] == "up":
            up_score += weight * (signal["strength"] / 100)
        elif signal["direction"] == "down":
            down_score += weight * (signal["strength"] / 100)
    
    if total_weight <= 0:
        return {
            "direction": "hold",
            "confidence_score": 0,
            "signals": {
                "ma_crossover": ma_signal,
                "rsi": rsi_signal,
                "macd": macd_signal,
                "momentum": momentum_signal,
                "volume_momentum": volume_signal,
            },
        }
    
    # Determine direction and confidence
    if up_score > down_score:
        direction = "up"
        confidence = (up_score / total_weight) * 100
    elif down_score > up_score:
        direction = "down"
        confidence = (down_score / total_weight) * 100
    else:
        direction = "hold"
        confidence = 0
    
    # Bonus for signal agreement
    directions = [ma_signal["direction"], rsi_signal["direction"], macd_signal["direction"]]
    agreement_count = sum(1 for d in directions if d == direction)
    
    if agreement_count >= 2:
        agreement_bonus = (agreement_count - 1) * 10
        confidence = min(100, confidence + agreement_bonus)
    
    # Volume momentum bonus: +15 confidence when volume rises AND price rises
    if volume_signal.get("bonus", 0) > 0 and direction == "up":
        confidence = min(100, confidence + volume_signal["bonus"])
    
    return {
        "direction": direction,
        "confidence_score": round(confidence, 1),
        "signals": {
            "ma_crossover": ma_signal,
            "rsi": rsi_signal,
            "macd": macd_signal,
            "momentum": momentum_signal,
            "volume_momentum": volume_signal,
        },
    }


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
    # edge = (win_prob * avg_win) - (loss_prob * avg_loss)
    win_prob = confidence / 100.0
    loss_prob = 1.0 - win_prob
    
    edge = (win_prob * KELLY_AVG_WIN_PCT) - (loss_prob * KELLY_AVG_LOSS_PCT)
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
    logger.debug(f"Kelly bet size: ${final_size:.2f} (edge {edge_pct:.2f}%)")
    
    return {
        "size": round(final_size, 2),
        "edge": round(edge_pct, 2),
        "kelly_fraction": round(kelly_fraction, 4),
    }


# ---------------------------------------------------------------------------
# Data Fetching
# ---------------------------------------------------------------------------


def fetch_30day_data(crypto_id: str = "bitcoin", max_retries: int = 3) -> list | None:
    """Fetch 30 days of price data from CoinGecko for backtesting.
    
    Note: CoinGecko returns hourly data when days <= 90. We simulate
    5-minute intervals by interpolating between hourly points.
    
    Args:
        crypto_id: Cryptocurrency ID for CoinGecko API (e.g., 'bitcoin').
        max_retries: Number of retry attempts for transient failures.
    
    Returns:
        List of [timestamp, close_price] tuples, or None on error.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            # CoinGecko returns hourly data when days <= 90
            data = cg.get_coin_market_chart_by_id(
                id=crypto_id,
                vs_currency="usd",
                days=BACKTEST_DAYS,
            )
            
            if not data or "prices" not in data:
                logger.warning(f"No price data returned from CoinGecko for {crypto_id}")
                return None
            
            prices = data.get("prices", [])
            if len(prices) < 24:  # Need at least 1 day of hourly data
                logger.warning(f"Insufficient data from CoinGecko: {len(prices)} points")
                return None
            
            logger.info(f"Fetched {len(prices)} hourly data points for {crypto_id}")
            return prices
            
        except Exception as e:
            last_error = e
            logger.warning(f"CoinGecko API error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
    
    logger.error(f"Failed to fetch 30-day data after {max_retries} attempts: {last_error}")
    return None


def fetch_30day_data_with_volume(crypto_id: str = "bitcoin", max_retries: int = 3) -> dict | None:
    """Fetch 30 days of price AND volume data from CoinGecko for backtesting.
    
    Uses get_coin_market_chart_by_id to get both prices and total_volumes.
    
    Args:
        crypto_id: Cryptocurrency ID for CoinGecko API (e.g., 'bitcoin').
        max_retries: Number of retry attempts for transient failures.
    
    Returns:
        Dictionary with 'prices' and 'volumes' lists, or None on error.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            # CoinGecko returns hourly data when days <= 90
            data = cg.get_coin_market_chart_by_id(
                id=crypto_id,
                vs_currency="usd",
                days=BACKTEST_DAYS,
            )
            
            if not data or "prices" not in data:
                logger.warning(f"No price data returned from CoinGecko for {crypto_id}")
                return None
            
            prices = data.get("prices", [])
            volumes = data.get("total_volumes", [])
            
            if len(prices) < 24:  # Need at least 1 day of hourly data
                logger.warning(f"Insufficient data from CoinGecko: {len(prices)} points")
                return None
            
            logger.info(f"Fetched {len(prices)} hourly data points with volume for {crypto_id}")
            return {"prices": prices, "volumes": volumes}
            
        except Exception as e:
            last_error = e
            logger.warning(f"CoinGecko API error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
    
    logger.error(f"Failed to fetch 30-day data with volume after {max_retries} attempts: {last_error}")
    return None


def interpolate_to_5min(hourly_prices: list) -> list:
    """Interpolate hourly price data to approximate 5-minute intervals.
    
    This creates more data points for backtesting by linear interpolation.
    
    Args:
        hourly_prices: List of [timestamp, price] from CoinGecko.
    
    Returns:
        List of [timestamp, price] at 5-minute intervals.
    """
    if len(hourly_prices) < 2:
        return hourly_prices
    
    result = []
    for i in range(len(hourly_prices) - 1):
        t1, p1 = hourly_prices[i]
        t2, p2 = hourly_prices[i + 1]
        
        # Add 12 points per hour (5-minute intervals)
        for j in range(12):
            fraction = j / 12
            timestamp = t1 + (t2 - t1) * fraction
            price = p1 + (p2 - p1) * fraction
            result.append([timestamp, price])
    
    # Add final hourly price point only if it wasn't already included
    # (The last iteration covers j=0..11, ending at 11/12 of the final interval)
    if hourly_prices:
        result.append(hourly_prices[-1])
    
    return result


def interpolate_volumes_to_5min(hourly_volumes: list) -> list:
    """Interpolate hourly volume data to approximate 5-minute intervals.
    
    Args:
        hourly_volumes: List of [timestamp, volume] from CoinGecko.
    
    Returns:
        List of [timestamp, volume] at 5-minute intervals.
    """
    if len(hourly_volumes) < 2:
        return hourly_volumes
    
    result = []
    for i in range(len(hourly_volumes) - 1):
        t1, v1 = hourly_volumes[i]
        t2, v2 = hourly_volumes[i + 1]
        
        # Add 12 points per hour (5-minute intervals)
        # Distribute volume evenly across the 5-min intervals
        for j in range(12):
            fraction = j / 12
            timestamp = t1 + (t2 - t1) * fraction
            # Linear interpolation for volume
            volume = v1 + (v2 - v1) * fraction
            result.append([timestamp, volume])
    
    if hourly_volumes:
        result.append(hourly_volumes[-1])
    
    return result


def calculate_volume_momentum_signal(
    closes: list,
    volumes: list,
    short_period: int = 5,
    long_period: int = 20,
) -> dict:
    """Calculate volume momentum signal.
    
    Compares 5-period volume MA vs 20-period volume MA.
    If volume rises AND price rises, returns bullish signal with +15 bonus.
    
    Args:
        closes: List of closing prices.
        volumes: List of volume values (same length as closes).
        short_period: Short MA period for volume (default 5).
        long_period: Long MA period for volume (default 20).
    
    Returns:
        Dictionary with direction, strength, volume_rising, price_rising, bonus.
    """
    if len(volumes) < long_period or len(closes) < long_period:
        return {
            "direction": "neutral",
            "strength": 0,
            "volume_rising": False,
            "price_rising": False,
            "bonus": 0,
        }
    
    # Calculate volume MAs
    vol_short_ma = sum(volumes[-short_period:]) / short_period
    vol_long_ma = sum(volumes[-long_period:]) / long_period
    
    # Calculate price change (recent vs earlier)
    price_short_avg = sum(closes[-short_period:]) / short_period
    price_long_avg = sum(closes[-long_period:]) / long_period
    
    volume_rising = vol_short_ma > vol_long_ma
    price_rising = price_short_avg > price_long_avg
    
    # Determine direction and strength
    if volume_rising and price_rising:
        direction = "up"
        strength = min(100, ((vol_short_ma / vol_long_ma) - 1) * 200 + 50)
        bonus = VOLUME_MOMENTUM_BONUS  # +15 confidence bonus
    elif volume_rising and not price_rising:
        direction = "down"  # Volume rising but price falling = potential reversal
        strength = min(100, ((vol_short_ma / vol_long_ma) - 1) * 150 + 30)
        bonus = 0
    elif not volume_rising and price_rising:
        direction = "neutral"  # Price rising without volume = weak signal
        strength = 20
        bonus = 0
    else:
        direction = "neutral"
        strength = 0
        bonus = 0
    
    return {
        "direction": direction,
        "strength": min(100, max(0, strength)),
        "volume_rising": volume_rising,
        "price_rising": price_rising,
        "bonus": bonus,
    }


# ---------------------------------------------------------------------------
# Backtesting Engine
# ---------------------------------------------------------------------------


class BacktestResult:
    """Container for backtest results and metrics."""
    
    def __init__(self):
        self.trades: list = []
        self.equity_curve: list = []
        self.initial_balance: float = BACKTEST_INITIAL_BALANCE
        self.final_balance: float = BACKTEST_INITIAL_BALANCE
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.total_profit: float = 0.0
        self.total_loss: float = 0.0
        self.max_drawdown: float = 0.0
        self.sharpe_ratio: float = 0.0
        self.run_timestamp: str = ""
        self.crypto_id: str = "bitcoin"
        self.data_points: int = 0
        # Walk-Forward Optimization results
        self.optimized_params: dict = {}
        self.wfo_windows: list = []
        self.is_walk_forward: bool = False
    
    @property
    def winrate(self) -> float:
        """Calculate win rate percentage."""
        total = self.winning_trades + self.losing_trades
        if total == 0:
            return 0.0
        return (self.winning_trades / total) * 100
    
    @property
    def profit_factor(self) -> float:
        """Calculate profit factor (gross profit / gross loss)."""
        if self.total_loss == 0:
            return float("inf") if self.total_profit > 0 else 0.0
        return self.total_profit / abs(self.total_loss)
    
    @property
    def total_trades(self) -> int:
        """Total number of trades executed."""
        return self.winning_trades + self.losing_trades
    
    def to_dict(self) -> dict:
        """Convert results to dictionary for JSON serialization."""
        result = {
            "run_timestamp": self.run_timestamp,
            "crypto_id": self.crypto_id,
            "data_points": self.data_points,
            "initial_balance": self.initial_balance,
            "final_balance": round(self.final_balance, 2),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "winrate": round(self.winrate, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor != float("inf") else "∞",
            "total_profit": round(self.total_profit, 2),
            "total_loss": round(self.total_loss, 2),
            "net_profit": round(self.total_profit + self.total_loss, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
        }
        if self.is_walk_forward:
            result["optimized_params"] = self.optimized_params
            result["wfo_windows"] = self.wfo_windows
            result["is_walk_forward"] = True
        return result


# ---------------------------------------------------------------------------
# Walk-Forward Optimization Functions
# ---------------------------------------------------------------------------


def optimize_params_brute_force(
    closes: list,
    volumes: list,
    min_confidence: int,
    trade_size_pct: float,
    param_range: tuple = (WFO_PARAM_RANGE_MIN, WFO_PARAM_RANGE_MAX),
) -> dict:
    """Brute-force optimization of SHORT_WINDOW, LONG_WINDOW, RSI thresholds.
    
    Tests all combinations in the parameter range and returns the best params
    based on profit factor.
    
    Args:
        closes: List of closing prices for the training window.
        volumes: List of volume values for the training window.
        min_confidence: Minimum confidence threshold.
        trade_size_pct: Trade size as percentage of balance.
        param_range: Tuple of (min, max) for parameter range.
    
    Returns:
        Dictionary with best parameters and their performance metrics.
    """
    min_val, max_val = param_range
    best_profit_factor = 0.0
    best_params = {
        "short_window": 5,
        "long_window": 20,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
    }
    best_metrics = {"winrate": 0, "profit_factor": 0, "trades": 0}
    
    # Brute-force search through parameter combinations
    for short_window in range(min_val, max_val + 1):
        for long_window in range(short_window + 2, max_val + 1):  # long > short + 1
            for rsi_ob in range(65, 81, 5):  # RSI overbought: 65, 70, 75, 80
                for rsi_os in range(20, 36, 5):  # RSI oversold: 20, 25, 30, 35
                    config = {
                        "short_window": short_window,
                        "long_window": long_window,
                        "rsi_overbought": rsi_ob,
                        "rsi_oversold": rsi_os,
                    }
                    
                    # Run mini backtest on training data
                    metrics = _run_mini_backtest(
                        closes, volumes, config, min_confidence, trade_size_pct
                    )
                    
                    # Select based on profit factor (with minimum trades requirement)
                    if metrics["trades"] >= 5 and metrics["profit_factor"] > best_profit_factor:
                        best_profit_factor = metrics["profit_factor"]
                        best_params = config.copy()
                        best_metrics = metrics.copy()
    
    logger.debug(
        f"Optimization result: short={best_params['short_window']}, "
        f"long={best_params['long_window']}, PF={best_profit_factor:.2f}"
    )
    
    return {
        "params": best_params,
        "metrics": best_metrics,
    }


def _run_mini_backtest(
    closes: list,
    volumes: list,
    config: dict,
    min_confidence: int,
    trade_size_pct: float,
) -> dict:
    """Run a mini backtest for parameter optimization.
    
    Args:
        closes: Price data.
        volumes: Volume data.
        config: Signal engine configuration.
        min_confidence: Minimum confidence threshold.
        trade_size_pct: Trade size percentage.
    
    Returns:
        Dictionary with winrate, profit_factor, trades count.
    """
    min_window = 40
    if len(closes) < min_window + TRADE_HOLDING_PERIOD_CANDLES:
        return {"winrate": 0, "profit_factor": 0, "trades": 0}
    
    winning = 0
    losing = 0
    total_profit = 0.0
    total_loss = 0.0
    balance = 1000.0
    
    # Step through data
    max_trades = 100  # Limit for optimization speed
    step_size = max(1, (len(closes) - min_window) // (max_trades * 2))
    
    i = min_window
    trades_count = 0
    while i < len(closes) - TRADE_HOLDING_PERIOD_CANDLES and trades_count < max_trades:
        window_closes = closes[max(0, i - 100):i]
        window_volumes = volumes[max(0, i - 100):i] if volumes else None
        
        if len(window_closes) < min_window:
            i += step_size
            continue
        
        conf_result = calculate_confidence_backtest(window_closes, config, window_volumes)
        direction = conf_result["direction"]
        confidence = conf_result["confidence_score"]
        
        if direction == "hold" or confidence < min_confidence:
            i += step_size
            continue
        
        current_price = closes[i]
        future_price = closes[i + TRADE_HOLDING_PERIOD_CANDLES]
        
        price_change_pct = ((future_price - current_price) / current_price) * 100
        trade_size = balance * trade_size_pct
        
        if direction == "up":
            profit = trade_size * (price_change_pct / 100)
        else:
            profit = trade_size * (-price_change_pct / 100)
        
        if profit > 0:
            winning += 1
            total_profit += profit
        else:
            losing += 1
            total_loss += profit
        
        balance += profit
        trades_count += 1
        i += TRADE_HOLDING_PERIOD_CANDLES + step_size
    
    total_trades = winning + losing
    winrate = (winning / total_trades * 100) if total_trades > 0 else 0
    profit_factor = (total_profit / abs(total_loss)) if total_loss != 0 else (float("inf") if total_profit > 0 else 0)
    
    return {
        "winrate": winrate,
        "profit_factor": profit_factor if profit_factor != float("inf") else 999.0,
        "trades": total_trades,
    }


def save_optimized_params(params: dict) -> None:
    """Save optimized parameters to bot_config.json.
    
    Args:
        params: Dictionary with optimized parameters.
    """
    try:
        # Load existing config
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
        else:
            config = {}
        
        # Update with optimized params
        config["short_window"] = params.get("short_window", 5)
        config["long_window"] = params.get("long_window", 20)
        config["rsi_overbought"] = params.get("rsi_overbought", 70)
        config["rsi_oversold"] = params.get("rsi_oversold", 30)
        config["wfo_last_optimized"] = datetime.now(timezone.utc).isoformat()
        
        # Update signal weights to include volume_momentum
        if "signal_weights" not in config:
            config["signal_weights"] = {}
        config["signal_weights"]["ma_crossover"] = 27
        config["signal_weights"]["rsi"] = 27
        config["signal_weights"]["macd"] = 23
        config["signal_weights"]["momentum"] = 13
        config["signal_weights"]["volume_momentum"] = VOLUME_MOMENTUM_WEIGHT
        
        # Save config
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        
        logger.info(f"Optimized params saved to {CONFIG_FILE}: {params}")
    except Exception as e:
        logger.error(f"Failed to save optimized params: {e}")


def run_backtest(
    crypto_id: str = "bitcoin",
    max_trades: int = BACKTEST_TRADES,
    min_confidence: int = MIN_CONFIDENCE_THRESHOLD,
    trade_size_pct: float = BACKTEST_TRADE_SIZE_PCT,
    config: dict | None = None,
) -> BacktestResult | None:
    """Run Walk-Forward Optimization backtest.
    
    Splits 30 days into 5 training windows + 1 test window.
    Optimizes SHORT_WINDOW/LONG_WINDOW + RSI thresholds per training window.
    Tests on the subsequent window to avoid lookahead bias.
    
    Args:
        crypto_id: CoinGecko cryptocurrency ID to backtest.
        max_trades: Maximum number of trades to simulate.
        min_confidence: Minimum confidence threshold to execute trade.
        trade_size_pct: Trade size as percentage of balance.
        config: Optional configuration for signal engine.
    
    Returns:
        BacktestResult object with all metrics, or None on failure.
    """
    logger.info(f"Starting Walk-Forward Optimization backtest for {crypto_id}")
    
    # Fetch historical data with volume
    data = fetch_30day_data_with_volume(crypto_id)
    if data is None:
        logger.error("Failed to fetch historical data for backtest")
        return None
    
    hourly_prices = data["prices"]
    hourly_volumes = data["volumes"]
    
    # Interpolate to 5-minute intervals
    prices_5min = interpolate_to_5min(hourly_prices)
    volumes_5min = interpolate_volumes_to_5min(hourly_volumes)
    logger.info(f"Interpolated to {len(prices_5min)} 5-minute data points with volume")
    
    # Extract close prices and volumes
    closes = [p[1] for p in prices_5min]
    volumes = [v[1] for v in volumes_5min]
    
    # Ensure volumes list matches closes length
    if len(volumes) < len(closes):
        volumes.extend([volumes[-1]] * (len(closes) - len(volumes)))
    elif len(volumes) > len(closes):
        volumes = volumes[:len(closes)]
    
    # Initialize result
    result = BacktestResult()
    result.crypto_id = crypto_id
    result.data_points = len(prices_5min)
    result.run_timestamp = datetime.now(timezone.utc).isoformat()
    result.is_walk_forward = True
    
    # Walk-Forward Optimization: Split into 5 training + 1 test window (6 windows total)
    total_windows = WFO_NUM_TRAINING_WINDOWS + 1  # 5 training + 1 test = 6
    window_size = len(closes) // total_windows
    
    if window_size < 100:
        logger.warning("Insufficient data for Walk-Forward Optimization, falling back to simple backtest")
        return _run_simple_backtest(closes, volumes, min_confidence, trade_size_pct, config)
    
    logger.info(f"WFO: {total_windows} windows, {window_size} data points each")
    
    # Initialize simulation state
    balance = BACKTEST_INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance
    max_drawdown = 0.0
    returns = []
    wfo_windows = []
    final_best_params = {}
    
    # Walk through windows: optimize on training, test on next window
    for window_idx in range(WFO_NUM_TRAINING_WINDOWS):
        train_start = window_idx * window_size
        train_end = train_start + window_size
        test_start = train_end
        test_end = min(test_start + window_size, len(closes))
        
        if test_end <= test_start:
            break
        
        train_closes = closes[train_start:train_end]
        train_volumes = volumes[train_start:train_end]
        test_closes = closes[test_start:test_end]
        test_volumes = volumes[test_start:test_end]
        
        logger.info(f"WFO Window {window_idx + 1}: Train[{train_start}:{train_end}], Test[{test_start}:{test_end}]")
        
        # Optimize parameters on training window
        opt_result = optimize_params_brute_force(
            train_closes, train_volumes, min_confidence, trade_size_pct
        )
        best_params = opt_result["params"]
        opt_metrics = opt_result["metrics"]
        
        logger.info(
            f"Window {window_idx + 1} optimal params: "
            f"short={best_params['short_window']}, long={best_params['long_window']}, "
            f"rsi_ob={best_params['rsi_overbought']}, rsi_os={best_params['rsi_oversold']}, "
            f"train_PF={opt_metrics['profit_factor']:.2f}"
        )
        
        # Test on the next window using optimized parameters
        window_result = _test_on_window(
            test_closes, test_volumes, best_params, min_confidence, trade_size_pct, balance
        )
        
        # Update cumulative results
        result.winning_trades += window_result["winning"]
        result.losing_trades += window_result["losing"]
        result.total_profit += window_result["profit"]
        result.total_loss += window_result["loss"]
        result.trades.extend(window_result["trades"])
        
        # Update balance and equity curve
        balance = window_result["final_balance"]
        equity_curve.extend(window_result["equity_curve"])
        
        # Track returns
        returns.extend(window_result["returns"])
        
        # Track drawdown
        for eq in window_result["equity_curve"]:
            if eq > peak_balance:
                peak_balance = eq
            current_dd = ((peak_balance - eq) / peak_balance) * 100 if peak_balance > 0 else 0
            max_drawdown = max(max_drawdown, current_dd)
        
        # Record window results
        wfo_windows.append({
            "window": window_idx + 1,
            "params": best_params,
            "train_metrics": opt_metrics,
            "test_trades": window_result["trades_count"],
            "test_winrate": window_result["winrate"],
        })
        
        # Keep track of best params (from last window for final use)
        final_best_params = best_params
    
    # Finalize results
    result.final_balance = balance
    result.equity_curve = equity_curve
    result.max_drawdown = max_drawdown
    result.optimized_params = final_best_params
    result.wfo_windows = wfo_windows
    
    # Calculate Sharpe Ratio
    if len(returns) > 1:
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        if std_return > 0:
            result.sharpe_ratio = (mean_return / std_return) * SHARPE_ANNUALIZATION_FACTOR
        else:
            result.sharpe_ratio = 0.0
    else:
        result.sharpe_ratio = 0.0
    
    # Save optimized parameters to bot_config.json
    if final_best_params:
        save_optimized_params(final_best_params)
    
    logger.info(
        f"Walk-Forward backtest completed: {result.total_trades} trades, "
        f"Winrate: {result.winrate:.1f}%, "
        f"Profit Factor: {result.profit_factor:.2f}, "
        f"Max Drawdown: {result.max_drawdown:.1f}%"
    )
    
    return result


def _test_on_window(
    closes: list,
    volumes: list,
    params: dict,
    min_confidence: int,
    trade_size_pct: float,
    starting_balance: float,
) -> dict:
    """Test trading on a window using specified parameters.
    
    Args:
        closes: Price data for the test window.
        volumes: Volume data for the test window.
        params: Optimized parameters to use.
        min_confidence: Minimum confidence threshold.
        trade_size_pct: Trade size percentage.
        starting_balance: Starting balance for this window.
    
    Returns:
        Dictionary with test results.
    """
    min_window = 40
    
    winning = 0
    losing = 0
    total_profit = 0.0
    total_loss = 0.0
    balance = starting_balance
    equity_curve = []
    returns = []
    trades = []
    
    # Configure for this window
    config = {
        "short_window": params["short_window"],
        "long_window": params["long_window"],
        "rsi_overbought": params["rsi_overbought"],
        "rsi_oversold": params["rsi_oversold"],
    }
    
    step_size = max(1, (len(closes) - min_window) // 50)
    
    i = min_window
    while i < len(closes) - TRADE_HOLDING_PERIOD_CANDLES:
        window_closes = closes[max(0, i - 100):i]
        window_volumes = volumes[max(0, i - 100):i] if volumes else None
        
        if len(window_closes) < min_window:
            i += step_size
            continue
        
        conf_result = calculate_confidence_backtest(window_closes, config, window_volumes)
        direction = conf_result["direction"]
        confidence = conf_result["confidence_score"]
        
        if direction == "hold" or confidence < min_confidence:
            i += step_size
            continue
        
        current_price = closes[i]
        future_price = closes[i + TRADE_HOLDING_PERIOD_CANDLES]
        
        price_change_pct = ((future_price - current_price) / current_price) * 100
        trade_size = balance * trade_size_pct
        
        if direction == "up":
            profit = trade_size * (price_change_pct / 100)
        else:
            profit = trade_size * (-price_change_pct / 100)
        
        if profit > 0:
            winning += 1
            total_profit += profit
        else:
            losing += 1
            total_loss += profit
        
        prev_balance = balance
        balance += profit
        equity_curve.append(balance)
        
        if prev_balance > 0:
            returns.append((balance - prev_balance) / prev_balance)
        
        trades.append({
            "index": i,
            "direction": direction,
            "confidence": confidence,
            "entry_price": current_price,
            "exit_price": future_price,
            "profit": round(profit, 2),
            "balance_after": round(balance, 2),
        })
        
        i += TRADE_HOLDING_PERIOD_CANDLES + step_size
    
    total_trades = winning + losing
    winrate = (winning / total_trades * 100) if total_trades > 0 else 0
    
    return {
        "winning": winning,
        "losing": losing,
        "profit": total_profit,
        "loss": total_loss,
        "final_balance": balance,
        "equity_curve": equity_curve,
        "returns": returns,
        "trades": trades,
        "trades_count": total_trades,
        "winrate": winrate,
    }


def _run_simple_backtest(
    closes: list,
    volumes: list,
    min_confidence: int,
    trade_size_pct: float,
    config: dict | None,
) -> BacktestResult:
    """Fallback simple backtest when data is insufficient for WFO.
    
    Args:
        closes: Price data.
        volumes: Volume data.
        min_confidence: Minimum confidence threshold.
        trade_size_pct: Trade size percentage.
        config: Optional configuration.
    
    Returns:
        BacktestResult object.
    """
    result = BacktestResult()
    result.run_timestamp = datetime.now(timezone.utc).isoformat()
    result.data_points = len(closes)
    
    balance = BACKTEST_INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance
    max_drawdown = 0.0
    returns = []
    
    min_window = 40
    step_size = max(1, (len(closes) - min_window) // 200)
    
    i = min_window
    while i < len(closes) - TRADE_HOLDING_PERIOD_CANDLES:
        window_closes = closes[max(0, i - 100):i]
        window_volumes = volumes[max(0, i - 100):i] if volumes else None
        
        if len(window_closes) < min_window:
            i += step_size
            continue
        
        conf_result = calculate_confidence_backtest(window_closes, config, window_volumes)
        direction = conf_result["direction"]
        confidence = conf_result["confidence_score"]
        
        if direction == "hold" or confidence < min_confidence:
            i += step_size
            continue
        
        current_price = closes[i]
        future_price = closes[i + TRADE_HOLDING_PERIOD_CANDLES]
        
        price_change_pct = ((future_price - current_price) / current_price) * 100
        trade_size = balance * trade_size_pct
        
        if direction == "up":
            profit = trade_size * (price_change_pct / 100)
        else:
            profit = trade_size * (-price_change_pct / 100)
        
        if profit > 0:
            result.winning_trades += 1
            result.total_profit += profit
        else:
            result.losing_trades += 1
            result.total_loss += profit
        
        prev_balance = balance
        balance += profit
        equity_curve.append(balance)
        
        if prev_balance > 0:
            returns.append((balance - prev_balance) / prev_balance)
        
        if balance > peak_balance:
            peak_balance = balance
        current_dd = ((peak_balance - balance) / peak_balance) * 100 if peak_balance > 0 else 0
        max_drawdown = max(max_drawdown, current_dd)
        
        result.trades.append({
            "index": i,
            "direction": direction,
            "confidence": confidence,
            "entry_price": current_price,
            "exit_price": future_price,
            "profit": round(profit, 2),
            "balance_after": round(balance, 2),
        })
        
        i += TRADE_HOLDING_PERIOD_CANDLES + step_size
    
    result.final_balance = balance
    result.equity_curve = equity_curve
    result.max_drawdown = max_drawdown
    
    if len(returns) > 1:
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        if std_return > 0:
            result.sharpe_ratio = (mean_return / std_return) * SHARPE_ANNUALIZATION_FACTOR
    
    return result


# ---------------------------------------------------------------------------
# Results Storage
# ---------------------------------------------------------------------------


def save_backtest_results(result: BacktestResult) -> None:
    """Save backtest results to daily_pnl.json file.
    
    Integrates with existing PNL structure by adding a 'backtest' key.
    
    Args:
        result: BacktestResult object to save.
    """
    try:
        # Load existing PNL data
        if BACKTEST_RESULTS_FILE.exists():
            with open(BACKTEST_RESULTS_FILE, "r") as f:
                pnl_data = json.load(f)
        else:
            pnl_data = {"daily": {}, "total": 0.0, "trades": []}
        
        # Add backtest results
        pnl_data["backtest"] = result.to_dict()
        
        # Save updated data
        with open(BACKTEST_RESULTS_FILE, "w") as f:
            json.dump(pnl_data, f, indent=2)
        
        logger.info(f"Backtest results saved to {BACKTEST_RESULTS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save backtest results: {e}")


def load_backtest_results() -> dict | None:
    """Load the most recent backtest results from daily_pnl.json.
    
    Returns:
        Dictionary with backtest results, or None if not found.
    """
    try:
        if not BACKTEST_RESULTS_FILE.exists():
            return None
        
        with open(BACKTEST_RESULTS_FILE, "r") as f:
            pnl_data = json.load(f)
        
        return pnl_data.get("backtest")
    except Exception as e:
        logger.error(f"Failed to load backtest results: {e}")
        return None


# ---------------------------------------------------------------------------
# Formatting Functions
# ---------------------------------------------------------------------------


def format_backtest_results(result: BacktestResult | dict) -> str:
    """Format backtest results for Telegram display.
    
    Args:
        result: BacktestResult object or dictionary from to_dict().
    
    Returns:
        Formatted string for Telegram message.
    """
    if isinstance(result, BacktestResult):
        data = result.to_dict()
    else:
        data = result
    
    # Format profit factor (handle Inf)
    pf = data.get("profit_factor", 0)
    pf_str = f"{pf:.2f}" if isinstance(pf, (int, float)) else str(pf)
    
    # Winrate emoji
    winrate = data.get("winrate", 0)
    if winrate >= 55:
        wr_emoji = "✅"
    elif winrate >= 50:
        wr_emoji = "📊"
    else:
        wr_emoji = "⚠️"
    
    # Sharpe ratio emoji
    sharpe = data.get("sharpe_ratio", 0)
    if sharpe >= 1.5:
        sr_emoji = "🌟"
    elif sharpe >= 1.0:
        sr_emoji = "✅"
    elif sharpe >= 0.5:
        sr_emoji = "📊"
    else:
        sr_emoji = "⚠️"
    
    # Max drawdown emoji
    mdd = data.get("max_drawdown", 0)
    if mdd <= 10:
        dd_emoji = "✅"
    elif mdd <= 20:
        dd_emoji = "📊"
    else:
        dd_emoji = "⚠️"
    
    # Base results
    text = f"""
📈 **Walk-Forward Backtest Results**

**Asset:** {data.get('crypto_id', 'bitcoin').upper()}
**Data Points:** {data.get('data_points', 0):,}
**Run:** {data.get('run_timestamp', 'N/A')[:19]}

💰 **Performance:**
Initial Balance: ${data.get('initial_balance', 0):,.2f}
Final Balance: ${data.get('final_balance', 0):,.2f}
Net Profit: ${data.get('net_profit', 0):,.2f}

📊 **Metrics:**
{wr_emoji} Winrate: {winrate:.1f}% ({data.get('winning_trades', 0)}W / {data.get('losing_trades', 0)}L)
📈 Profit Factor: {pf_str}
{dd_emoji} Max Drawdown: {mdd:.1f}%
{sr_emoji} Sharpe Ratio: {sharpe:.2f}

**Trade Stats:**
Total Trades: {data.get('total_trades', 0)}
Gross Profit: ${data.get('total_profit', 0):,.2f}
Gross Loss: ${data.get('total_loss', 0):,.2f}
"""
    
    # Add WFO optimized parameters if available
    if data.get("is_walk_forward") and data.get("optimized_params"):
        params = data["optimized_params"]
        text += f"""
🔧 **Optimierte Parameter:**
• Short Window: {params.get('short_window', 5)}
• Long Window: {params.get('long_window', 20)}
• RSI Overbought: {params.get('rsi_overbought', 70)}
• RSI Oversold: {params.get('rsi_oversold', 30)}
• Volume Momentum: {VOLUME_MOMENTUM_WEIGHT}% Gewicht

_Parameter wurden in bot_config.json gespeichert._
"""
    
    return text


def format_live_vs_backtest_comparison(live_pnl: dict, backtest: dict | None) -> str:
    """Format Live vs Backtest comparison for /status command.
    
    Args:
        live_pnl: Live trading PNL data.
        backtest: Backtest results dictionary or None.
    
    Returns:
        Formatted comparison string for Telegram.
    """
    if backtest is None:
        return "\n📊 **Live vs Backtest:** Kein Backtest verfügbar. Nutze /backtest"
    
    # Calculate live stats
    live_trades = live_pnl.get("trades", [])
    live_winning = sum(1 for t in live_trades if t.get("profit", 0) > 0)
    live_losing = sum(1 for t in live_trades if t.get("profit", 0) < 0)
    live_total = live_winning + live_losing
    
    if live_total > 0:
        live_winrate = (live_winning / live_total) * 100
    else:
        live_winrate = 0.0
    
    live_profit = sum(t.get("profit", 0) for t in live_trades if t.get("profit", 0) > 0)
    live_loss = sum(t.get("profit", 0) for t in live_trades if t.get("profit", 0) < 0)
    
    if live_loss != 0:
        live_pf = live_profit / abs(live_loss)
    else:
        live_pf = float("inf") if live_profit > 0 else 0.0
    
    # Backtest stats
    bt_winrate = backtest.get("winrate", 0)
    bt_pf = backtest.get("profit_factor", 0)
    bt_mdd = backtest.get("max_drawdown", 0)
    bt_sharpe = backtest.get("sharpe_ratio", 0)
    
    # Format profit factors (use consistent infinity symbol)
    live_pf_str = f"{live_pf:.2f}" if live_pf != float("inf") else "∞"
    bt_pf_str = f"{bt_pf:.2f}" if isinstance(bt_pf, (int, float)) else "∞" if bt_pf == "∞" else str(bt_pf)
    
    # Comparison indicators
    wr_diff = live_winrate - bt_winrate
    wr_indicator = "📈" if wr_diff > 2 else "📉" if wr_diff < -2 else "➡️"
    
    # Use simple line-by-line format (Telegram doesn't support markdown tables)
    return f"""
📊 **Live vs Backtest Vergleich:**

**Winrate:**
  Live: {live_winrate:.1f}% | Backtest: {bt_winrate:.1f}% {wr_indicator}
**Profit Factor:**
  Live: {live_pf_str} | Backtest: {bt_pf_str}
**Trades:**
  Live: {live_total} | Backtest: {backtest.get('total_trades', 0)}

**Backtest Referenz:**
Max Drawdown: {bt_mdd:.1f}%
Sharpe Ratio: {bt_sharpe:.2f}
"""


# ---------------------------------------------------------------------------
# Main entry point for direct execution
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    
    print("Running UpDown Backtest...")
    result = run_backtest(crypto_id="bitcoin")
    
    if result:
        print(format_backtest_results(result))
        save_backtest_results(result)
        print("\nResults saved to daily_pnl.json")
    else:
        print("Backtest failed. Check logs for details.")
