"""
Backtest module for UpDown Telegram Bot.

This module provides historical backtesting capabilities for the multi-signal trading engine.
It fetches 30 days of 5-minute CoinGecko data and simulates 1000 trades to calculate:
- Winrate
- Profit-Factor
- Max-Drawdown
- Sharpe-Ratio

Results are saved to daily_pnl.json and displayed in Telegram via /backtest and /status commands.
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

# File path for storing backtest results (same as PNL file for integration)
BACKTEST_RESULTS_FILE = Path("daily_pnl.json")

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


def calculate_confidence_backtest(closes: list, config: dict | None = None) -> dict:
    """Calculate confidence score for backtesting (Multi-Signal Engine).
    
    This is a standalone version of the multi-signal engine for backtesting,
    without Polymarket API dependencies.
    
    Args:
        closes: List of closing prices.
        config: Optional configuration dict with signal weights and thresholds.
    
    Returns:
        Dictionary with direction, confidence_score, and signal details.
    """
    if config is None:
        config = {}
    
    # Signal weights
    weights = config.get("signal_weights", {
        "ma_crossover": 30,
        "rsi": 30,
        "macd": 25,
        "momentum": 15,  # Use momentum instead of Polymarket delta for backtest
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
    
    # Aggregate scores
    up_score = 0
    down_score = 0
    total_weight = 0
    
    for signal_name, signal in [
        ("ma_crossover", ma_signal),
        ("rsi", rsi_signal),
        ("macd", macd_signal),
        ("momentum", momentum_signal),
    ]:
        weight = weights.get(signal_name, 25)
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
    
    return {
        "direction": direction,
        "confidence_score": round(confidence, 1),
        "signals": {
            "ma_crossover": ma_signal,
            "rsi": rsi_signal,
            "macd": macd_signal,
            "momentum": momentum_signal,
        },
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
        return {
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


def run_backtest(
    crypto_id: str = "bitcoin",
    max_trades: int = BACKTEST_TRADES,
    min_confidence: int = MIN_CONFIDENCE_THRESHOLD,
    trade_size_pct: float = BACKTEST_TRADE_SIZE_PCT,
    config: dict | None = None,
) -> BacktestResult | None:
    """Run a backtest simulation using the multi-signal engine.
    
    Args:
        crypto_id: CoinGecko cryptocurrency ID to backtest.
        max_trades: Maximum number of trades to simulate.
        min_confidence: Minimum confidence threshold to execute trade.
        trade_size_pct: Trade size as percentage of balance.
        config: Optional configuration for signal engine.
    
    Returns:
        BacktestResult object with all metrics, or None on failure.
    """
    logger.info(f"Starting backtest for {crypto_id} with max {max_trades} trades")
    
    # Fetch historical data
    hourly_prices = fetch_30day_data(crypto_id)
    if hourly_prices is None:
        logger.error("Failed to fetch historical data for backtest")
        return None
    
    # Interpolate to 5-minute intervals for more trading opportunities
    prices_5min = interpolate_to_5min(hourly_prices)
    logger.info(f"Interpolated to {len(prices_5min)} 5-minute data points")
    
    # Extract close prices
    closes = [p[1] for p in prices_5min]
    
    # Initialize result
    result = BacktestResult()
    result.crypto_id = crypto_id
    result.data_points = len(prices_5min)
    result.run_timestamp = datetime.now(timezone.utc).isoformat()
    
    # Initialize simulation state
    balance = BACKTEST_INITIAL_BALANCE
    equity_curve = [balance]
    peak_balance = balance
    max_drawdown = 0.0
    returns = []
    trades_count = 0
    
    # Minimum data window needed for signals (MACD needs 35 points: 26 + 9)
    min_window = 40
    
    # Step through data simulating trades
    # Divide by 2 to account for lookahead periods and spacing between trades
    # This ensures we have room for trade entry, holding period, and gap to next trade
    step_size = max(1, (len(closes) - min_window) // (max_trades * 2))
    
    i = min_window
    while i < len(closes) and trades_count < max_trades:
        # Get historical window
        window = closes[max(0, i - 100):i]
        
        if len(window) < min_window:
            i += step_size
            continue
        
        # Calculate confidence
        conf_result = calculate_confidence_backtest(window, config)
        direction = conf_result["direction"]
        confidence = conf_result["confidence_score"]
        
        # Skip if below threshold or hold signal
        if direction == "hold" or confidence < min_confidence:
            i += step_size
            continue
        
        # Execute trade
        current_price = closes[i]
        
        # Look ahead to determine outcome (holding period defined by constant)
        lookahead = min(TRADE_HOLDING_PERIOD_CANDLES, len(closes) - i - 1)
        if lookahead <= 0:
            break
        
        future_price = closes[i + lookahead]
        
        # Calculate trade outcome
        price_change_pct = ((future_price - current_price) / current_price) * 100
        
        # Trade size based on balance
        trade_size = balance * trade_size_pct
        
        # Determine if trade was profitable
        if direction == "up":
            profit = trade_size * (price_change_pct / 100)
        else:  # direction == "down"
            profit = trade_size * (-price_change_pct / 100)
        
        # Update statistics
        if profit > 0:
            result.winning_trades += 1
            result.total_profit += profit
        else:
            result.losing_trades += 1
            result.total_loss += profit  # profit is negative here
        
        # Update balance
        balance += profit
        equity_curve.append(balance)
        
        # Track return for Sharpe ratio
        if equity_curve[-2] > 0:
            trade_return = (balance - equity_curve[-2]) / equity_curve[-2]
            returns.append(trade_return)
        
        # Track drawdown
        if balance > peak_balance:
            peak_balance = balance
        current_drawdown = ((peak_balance - balance) / peak_balance) * 100 if peak_balance > 0 else 0
        max_drawdown = max(max_drawdown, current_drawdown)
        
        # Record trade
        result.trades.append({
            "index": i,
            "direction": direction,
            "confidence": confidence,
            "entry_price": current_price,
            "exit_price": future_price,
            "profit": round(profit, 2),
            "balance_after": round(balance, 2),
        })
        
        trades_count += 1
        
        # Move forward (skip the lookahead period to avoid overlapping trades)
        i += lookahead + step_size
    
    # Finalize results
    result.final_balance = balance
    result.equity_curve = equity_curve
    result.max_drawdown = max_drawdown
    
    # Calculate Sharpe Ratio
    if len(returns) > 1:
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        if std_return > 0:
            # Annualize using hourly annualization factor (~8760 trades per year)
            result.sharpe_ratio = (mean_return / std_return) * SHARPE_ANNUALIZATION_FACTOR
        else:
            result.sharpe_ratio = 0.0
    else:
        result.sharpe_ratio = 0.0
    
    logger.info(
        f"Backtest completed: {result.total_trades} trades, "
        f"Winrate: {result.winrate:.1f}%, "
        f"Profit Factor: {result.profit_factor:.2f}, "
        f"Max Drawdown: {result.max_drawdown:.1f}%"
    )
    
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
    
    return f"""
📈 **Backtest Results**

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
