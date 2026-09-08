"""Lokale Berechnung technischer Kennzahlen aus Tageskursen."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

import pandas as pd


Trend = Literal["POSITIV", "NEGATIV", "NEUTRAL"]


@dataclass(frozen=True, slots=True)
class TechnicalIndicators:
    """Technische Kennzahlen für den jüngsten verfügbaren Handelstag."""

    current_close: float
    previous_day_change_pct: float | None
    performance_5d_pct: float | None
    performance_20d_pct: float | None
    performance_60d_pct: float | None
    sma20: float | None
    sma50: float | None
    sma200: float | None
    rsi14: float | None
    volatility_20d_pct: float | None
    distance_sma20_pct: float | None
    distance_sma50_pct: float | None
    trend: Trend


@dataclass(frozen=True, slots=True)
class FairValueGap:
    direction: Literal["BULLISH", "BEARISH"]
    formed_at: pd.Timestamp | None
    position: int
    low: float
    high: float


@dataclass(frozen=True, slots=True)
class SwingPoint:
    kind: Literal["HIGH", "LOW"]
    timestamp: pd.Timestamp | None
    position: int
    price: float


@dataclass(frozen=True, slots=True)
class FibonacciLevels:
    direction: Literal["UP", "DOWN"]
    swing_low: float
    swing_high: float
    swing_low_at: pd.Timestamp | None
    swing_high_at: pd.Timestamp | None
    retracements: dict[str, float]
    extensions: dict[str, float]


@dataclass(frozen=True, slots=True)
class MkrTechnicalIndicators:
    """Erweiterte, rein deterministische MKR-Kennzahlen aus Daily-OHLCV."""

    base: TechnicalIndicators
    trading_days: int
    history_start: pd.Timestamp | None
    history_end: pd.Timestamp | None
    ema20: float | None
    ema50: float | None
    ema200: float | None
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None
    atr14: float | None
    bollinger_middle: float | None
    bollinger_upper: float | None
    bollinger_lower: float | None
    volume_latest: float | None
    volume_average_20d: float | None
    volume_vs_average_20d_percent: float | None
    obv: float | None
    adx14: float | None
    trix15: float | None
    fair_value_gaps: tuple[FairValueGap, ...]
    swing_points: tuple[SwingPoint, ...]
    fibonacci: FibonacciLevels | None


class TechnicalAnalysisError(ValueError):
    """Die übergebene Kursreihe kann nicht ausgewertet werden."""


def calculate_technical_indicators(history: pd.DataFrame) -> TechnicalIndicators:
    """Berechnet technische Kennzahlen ohne externe Indikator-Dienste.

    Performance über N Tage vergleicht den aktuellen Schlusskurs mit dem
    Schlusskurs vor N Handelstagen. Die Volatilität wird aus 20 täglichen
    Renditen berechnet und mit sqrt(252) annualisiert.
    """

    closes = _clean_closes(history)
    current = float(closes.iloc[-1])

    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)

    if sma20 is not None and sma50 is not None:
        if current > sma20 and sma20 > sma50:
            trend: Trend = "POSITIV"
        elif current < sma20 and sma20 < sma50:
            trend = "NEGATIV"
        else:
            trend = "NEUTRAL"
    else:
        trend = "NEUTRAL"

    return TechnicalIndicators(
        current_close=current,
        previous_day_change_pct=_performance(closes, 1),
        performance_5d_pct=_performance(closes, 5),
        performance_20d_pct=_performance(closes, 20),
        performance_60d_pct=_performance(closes, 60),
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        rsi14=_rsi14(closes),
        volatility_20d_pct=_volatility20(closes),
        distance_sma20_pct=_distance_pct(current, sma20),
        distance_sma50_pct=_distance_pct(current, sma50),
        trend=trend,
    )


def calculate_mkr_technical_indicators(history: pd.DataFrame) -> MkrTechnicalIndicators:
    """Erweitert die bestehende Analyse ohne deren Berechnungen zu duplizieren."""

    frame = _clean_ohlcv(history)
    closes = frame["Close"]
    base = calculate_technical_indicators(frame)
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_series = ema12 - ema26
    signal_series = macd_series.ewm(span=9, adjust=False, min_periods=9).mean()
    middle = closes.rolling(20, min_periods=20).mean()
    deviation = closes.rolling(20, min_periods=20).std(ddof=0)
    volume = frame.get("Volume")
    volume_average = volume.rolling(20, min_periods=20).mean() if volume is not None else None
    latest_volume = _last(volume)
    latest_average = _last(volume_average)
    swings = _swing_points(frame)

    return MkrTechnicalIndicators(
        base=base,
        trading_days=len(frame),
        history_start=_index_timestamp(frame.index, 0),
        history_end=_index_timestamp(frame.index, len(frame) - 1),
        ema20=_last(_ema_series(closes, 20)),
        ema50=_last(_ema_series(closes, 50)),
        ema200=_last(_ema_series(closes, 200)),
        macd=_last(macd_series),
        macd_signal=_last(signal_series),
        macd_histogram=_last(macd_series - signal_series),
        atr14=_atr14(frame),
        bollinger_middle=_last(middle),
        bollinger_upper=_last(middle + 2 * deviation),
        bollinger_lower=_last(middle - 2 * deviation),
        volume_latest=latest_volume,
        volume_average_20d=latest_average,
        volume_vs_average_20d_percent=(
            (latest_volume / latest_average - 1) * 100
            if latest_volume is not None and latest_average not in (None, 0)
            else None
        ),
        obv=_obv(frame),
        adx14=_adx14(frame),
        trix15=_trix(closes, 15),
        fair_value_gaps=_fair_value_gaps(frame),
        swing_points=swings,
        fibonacci=_fibonacci(swings),
    )


def aggregate_ohlcv(
    history: pd.DataFrame, timeframe: Literal["weekly", "monthly"],
) -> pd.DataFrame:
    """Aggregiert echte Daily-OHLCV-Bars deterministisch in höhere Zeitrahmen."""

    frame = _clean_ohlcv(history)
    if "Open" not in frame:
        raise TechnicalAnalysisError("Für die OHLC-Aggregation fehlt die Spalte 'Open'.")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TechnicalAnalysisError("Für die OHLC-Aggregation ist ein Datumsindex erforderlich.")
    frequency = "W-FRI" if timeframe == "weekly" else "ME"
    aggregation: dict[str, str] = {
        "Open": "first", "High": "max", "Low": "min", "Close": "last",
    }
    if "Volume" in frame:
        aggregation["Volume"] = "sum"
    return frame.resample(frequency).agg(aggregation).dropna(subset=["Open", "High", "Low", "Close"])


def _clean_closes(history: pd.DataFrame) -> pd.Series:
    if not isinstance(history, pd.DataFrame):
        raise TechnicalAnalysisError("history muss ein pandas.DataFrame sein.")
    if "Close" not in history.columns:
        raise TechnicalAnalysisError("Die Spalte 'Close' fehlt.")

    closes = pd.to_numeric(history["Close"], errors="coerce").dropna()
    if closes.empty:
        raise TechnicalAnalysisError("Es sind keine gültigen Schlusskurse vorhanden.")
    if (closes <= 0).any():
        raise TechnicalAnalysisError("Schlusskurse müssen größer als null sein.")
    return closes.sort_index()


def _performance(closes: pd.Series, days: int) -> float | None:
    if len(closes) <= days:
        return None
    return float((closes.iloc[-1] / closes.iloc[-days - 1] - 1.0) * 100.0)


def _sma(closes: pd.Series, days: int) -> float | None:
    if len(closes) < days:
        return None
    return float(closes.iloc[-days:].mean())


def _rsi14(closes: pd.Series) -> float | None:
    if len(closes) < 15:
        return None

    changes = closes.diff()
    gains = changes.clip(lower=0.0)
    losses = -changes.clip(upper=0.0)
    average_gain = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1]
    average_loss = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1]

    if average_gain == 0 and average_loss == 0:
        return 50.0
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return float(100.0 - 100.0 / (1.0 + relative_strength))


def _volatility20(closes: pd.Series) -> float | None:
    if len(closes) < 21:
        return None
    returns = closes.pct_change().dropna().iloc[-20:]
    return float(returns.std(ddof=1) * math.sqrt(252) * 100.0)


def _distance_pct(current: float, average: float | None) -> float | None:
    if average is None:
        return None
    return (current / average - 1.0) * 100.0


def _clean_ohlcv(history: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(history, pd.DataFrame):
        raise TechnicalAnalysisError("history muss ein pandas.DataFrame sein.")
    required = {"High", "Low", "Close"}
    missing = required.difference(history.columns)
    if missing:
        raise TechnicalAnalysisError(
            "Für die MKR-Analyse fehlen OHLCV-Spalten: " + ", ".join(sorted(missing))
        )
    columns = [column for column in ("Open", "High", "Low", "Close", "Volume") if column in history]
    frame = history.loc[:, columns].copy()
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["High", "Low", "Close"]).sort_index()
    if frame.empty or (frame[["High", "Low", "Close"]] <= 0).any().any():
        raise TechnicalAnalysisError("Es sind keine gültigen positiven OHLC-Kurse vorhanden.")
    if (frame["High"] < frame["Low"]).any():
        raise TechnicalAnalysisError("Ein Tageshoch liegt unter dem Tagestief.")
    if "Volume" in frame:
        frame.loc[frame["Volume"] < 0, "Volume"] = math.nan
    return frame


def _ema_series(values: pd.Series, days: int) -> pd.Series:
    return values.ewm(span=days, adjust=False, min_periods=days).mean()


def _last(values: pd.Series | None) -> float | None:
    if values is None or values.empty or pd.isna(values.iloc[-1]):
        return None
    return float(values.iloc[-1])


def _true_range(frame: pd.DataFrame) -> pd.Series:
    previous = frame["Close"].shift(1)
    return pd.concat(
        [frame["High"] - frame["Low"], (frame["High"] - previous).abs(),
         (frame["Low"] - previous).abs()], axis=1,
    ).max(axis=1)


def _atr14(frame: pd.DataFrame) -> float | None:
    return _last(_true_range(frame).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())


def _obv(frame: pd.DataFrame) -> float | None:
    if "Volume" not in frame or frame["Volume"].isna().any():
        return None
    direction = frame["Close"].diff().apply(lambda value: 1 if value > 0 else -1 if value < 0 else 0)
    return float((direction * frame["Volume"]).fillna(0).cumsum().iloc[-1])


def _adx14(frame: pd.DataFrame) -> float | None:
    if len(frame) < 28:
        return None
    up = frame["High"].diff()
    down = -frame["Low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr = _true_range(frame).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr
    denominator = (plus_di + minus_di).replace(0, math.nan)
    dx = 100 * (plus_di - minus_di).abs() / denominator
    return _last(dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())


def _trix(closes: pd.Series, days: int) -> float | None:
    first = _ema_series(closes, days)
    second = _ema_series(first.dropna(), days).reindex(closes.index)
    third = _ema_series(second.dropna(), days).reindex(closes.index)
    return _last(third.pct_change(fill_method=None) * 100)


def _fair_value_gaps(frame: pd.DataFrame) -> tuple[FairValueGap, ...]:
    gaps: list[FairValueGap] = []
    for index in range(2, len(frame)):
        earlier_high = float(frame["High"].iloc[index - 2])
        earlier_low = float(frame["Low"].iloc[index - 2])
        current_low = float(frame["Low"].iloc[index])
        current_high = float(frame["High"].iloc[index])
        later = frame.iloc[index + 1 :]
        if current_low > earlier_high:
            fully_filled = not later.empty and bool((later["Low"] <= earlier_high).any())
            if not fully_filled:
                gaps.append(FairValueGap("BULLISH", _index_timestamp(frame.index, index), index,
                                         earlier_high, current_low))
        elif current_high < earlier_low:
            fully_filled = not later.empty and bool((later["High"] >= earlier_low).any())
            if not fully_filled:
                gaps.append(FairValueGap("BEARISH", _index_timestamp(frame.index, index), index,
                                         current_high, earlier_low))
    return tuple(gaps[-5:])


def _swing_points(frame: pd.DataFrame, window: int = 3) -> tuple[SwingPoint, ...]:
    points: list[SwingPoint] = []
    for index in range(window, len(frame) - window):
        high = float(frame["High"].iloc[index])
        low = float(frame["Low"].iloc[index])
        high_window = frame["High"].iloc[index - window : index + window + 1]
        low_window = frame["Low"].iloc[index - window : index + window + 1]
        if high == float(high_window.max()) and int((high_window == high).sum()) == 1:
            points.append(SwingPoint("HIGH", _index_timestamp(frame.index, index), index, high))
        if low == float(low_window.min()) and int((low_window == low).sum()) == 1:
            points.append(SwingPoint("LOW", _index_timestamp(frame.index, index), index, low))
    return tuple(points[-10:])


def _fibonacci(swings: tuple[SwingPoint, ...]) -> FibonacciLevels | None:
    if len(swings) < 2:
        return None
    last = swings[-1]
    opposite = next((item for item in reversed(swings[:-1]) if item.kind != last.kind), None)
    if opposite is None:
        return None
    low_point = last if last.kind == "LOW" else opposite
    high_point = last if last.kind == "HIGH" else opposite
    if high_point.price <= low_point.price:
        return None
    size = high_point.price - low_point.price
    direction: Literal["UP", "DOWN"] = "UP" if low_point.position < high_point.position else "DOWN"
    ratios = (0.236, 0.382, 0.5, 0.618, 0.65, 0.786)
    extensions = (1.272, 1.618, 2.618)
    if direction == "UP":
        retracement_values = {f"{ratio:.3f}": high_point.price - size * ratio for ratio in ratios}
        extension_values = {f"{ratio:.3f}": low_point.price + size * ratio for ratio in extensions}
    else:
        retracement_values = {f"{ratio:.3f}": low_point.price + size * ratio for ratio in ratios}
        extension_values = {f"{ratio:.3f}": high_point.price - size * ratio for ratio in extensions}
    return FibonacciLevels(
        direction, low_point.price, high_point.price, low_point.timestamp, high_point.timestamp,
        retracement_values, extension_values,
    )


def _index_timestamp(index: pd.Index, position: int) -> pd.Timestamp | None:
    if not isinstance(index, pd.DatetimeIndex):
        return None
    value = pd.Timestamp(index[position])
    return None if pd.isna(value) else value
