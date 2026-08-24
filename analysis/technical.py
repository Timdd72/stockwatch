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
