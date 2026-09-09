"""Deterministische Handlungseinschätzung aus bereits lokal vorhandenen Daten.

Die Engine ist bewusst frei von Provider-, HTTP- und KI-Aufrufen. Sie wertet
einen strukturierten lokalen Datenvertrag aus und liefert eine nachvollziehbare
Entscheidung mit Datenqualitäts- und Freshness-Hinweisen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Mapping


class DecisionAction(str, Enum):
    IMMEDIATE_SELL = "IMMEDIATE_SELL"
    SELL = "SELL"
    HOLD = "HOLD"
    BUY = "BUY"
    IMMEDIATE_BUY = "IMMEDIATE_BUY"


class Signal(str, Enum):
    STRONG_BEARISH = "STRONG_BEARISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    BULLISH = "BULLISH"
    STRONG_BULLISH = "STRONG_BULLISH"


class DecisionPerspective(str, Enum):
    POSITION = "POSITION"
    NEW_ENTRY = "NEW_ENTRY"


@dataclass(frozen=True, slots=True)
class DecisionInput:
    """Lokaler, optionaler Eingangsvertrag für die Decision Engine."""

    current_price: float | None = None
    currency: str | None = None
    generated_at: datetime | None = None
    data_timestamp: datetime | None = None
    technical_context_timestamp: datetime | None = None
    technical_context: Mapping[str, Any] | None = None
    has_position: bool = False
    purchase_price: float | None = None
    profit_loss: float | None = None
    profit_loss_percent: float | None = None
    trend: str | None = None
    performance_5d: float | None = None
    performance_20d: float | None = None
    performance_60d: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    adx14: float | None = None
    trix15: float | None = None
    atr14: float | None = None
    bollinger_lower: float | None = None
    bollinger_upper: float | None = None
    supports: tuple[float, ...] = ()
    resistances: tuple[float, ...] = ()
    swing_lows: tuple[float, ...] = ()
    swing_highs: tuple[float, ...] = ()
    fibonacci_levels: tuple[float, ...] = ()
    mkr_frameworks: tuple[Mapping[str, Any], ...] = ()
    mkr_confidence: float | None = None
    mkr_data_gaps: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()
    data_quality: str | None = None
    price_type: str | None = None
    verified_event: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DecisionInput":
        """Erlaubt Tests/Adapter ohne eine zweite Berechnungslogik."""
        data = dict(value)
        for key in ("supports", "resistances", "swing_lows", "swing_highs", "fibonacci_levels", "mkr_frameworks", "mkr_data_gaps", "data_gaps"):
            if data.get(key) is None:
                data[key] = ()
            elif not isinstance(data[key], tuple):
                data[key] = tuple(data[key])
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass(frozen=True, slots=True)
class DecisionResult:
    action: DecisionAction
    confidence: int
    current_price: float | None
    currency: str | None
    generated_at: datetime
    data_timestamp: datetime | None
    has_position: bool
    technical_context_timestamp: datetime | None = None
    buy_zone_low: float | None = None
    buy_zone_high: float | None = None
    sell_zone_low: float | None = None
    sell_zone_high: float | None = None
    warning_level: float | None = None
    invalidation_level: float | None = None
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    supporting_signals: tuple[str, ...] = ()
    opposing_signals: tuple[str, ...] = ()
    data_quality: str = "INSUFFICIENT"
    data_gaps: tuple[str, ...] = ()
    signal: Signal = Signal.NEUTRAL
    position_action: DecisionAction = DecisionAction.HOLD
    entry_action: DecisionAction = DecisionAction.HOLD
    position_decision: "PerspectiveResult | None" = None
    entry_decision: "PerspectiveResult | None" = None


@dataclass(frozen=True, slots=True)
class PerspectiveResult:
    perspective: DecisionPerspective
    action: DecisionAction
    confidence: int
    supporting_signals: tuple[str, ...] = ()
    opposing_signals: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    buy_zone_low: float | None = None
    buy_zone_high: float | None = None
    sell_zone_low: float | None = None
    sell_zone_high: float | None = None
    warning_level: float | None = None
    invalidation_level: float | None = None


@dataclass(frozen=True, slots=True)
class DecisionWeights:
    trend: int = 2
    performance: int = 1
    moving_average: int = 1
    momentum: int = 1
    mkr: int = 2


class DecisionEngine:
    """Erzeugt eine erklärbare Entscheidung ohne Seiteneffekte."""

    def __init__(self, weights: DecisionWeights | None = None, now=None) -> None:
        self.weights = weights or DecisionWeights()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def evaluate(self, value: DecisionInput | Mapping[str, Any]) -> DecisionResult:
        data = value if isinstance(value, DecisionInput) else DecisionInput.from_mapping(value)
        data = self._with_technical_context(data)
        score = 0
        supporting: list[str] = []
        opposing: list[str] = []
        reasons: list[str] = []

        trend = (data.trend or "").upper()
        if trend == "POSITIV":
            score += self.weights.trend
            supporting.append("Trend ist positiv")
        elif trend == "NEGATIV":
            score -= self.weights.trend
            opposing.append("Trend ist negativ")

        for name, value_ in (("5-Tage-Performance", data.performance_5d), ("20-Tage-Performance", data.performance_20d), ("60-Tage-Performance", data.performance_60d)):
            if _number(value_):
                if value_ > 0:
                    score += self.weights.performance
                    supporting.append(f"{name} positiv ({value_:.2f} %)")
                elif value_ < 0:
                    score -= self.weights.performance
                    opposing.append(f"{name} negativ ({value_:.2f} %)")

        for name, price, average in (("SMA20", data.current_price, data.sma20), ("SMA50", data.current_price, data.sma50)):
            if _number(price) and _number(average):
                if price >= average:
                    score += self.weights.moving_average
                    supporting.append(f"Kurs liegt über {name}")
                else:
                    score -= self.weights.moving_average
                    opposing.append(f"Kurs liegt unter {name}")

        if _number(data.macd_histogram):
            if data.macd_histogram > 0:
                score += self.weights.momentum
                supporting.append("MACD-Histogramm ist positiv")
            elif data.macd_histogram < 0:
                score -= self.weights.momentum
                opposing.append("MACD-Histogramm ist negativ")
        if _number(data.rsi14):
            if data.rsi14 >= 75:
                score -= self.weights.momentum
                opposing.append(f"RSI ist überhitzt ({data.rsi14:.2f})")
            elif data.rsi14 <= 25:
                score += self.weights.momentum
                supporting.append(f"RSI ist stark überverkauft ({data.rsi14:.2f})")

        mkr_score = self._mkr_score(data.mkr_frameworks)
        score += mkr_score
        if mkr_score > 0:
            supporting.append("belastbare MKR-Signale sind überwiegend positiv")
        elif mkr_score < 0:
            opposing.append("belastbare MKR-Signale sind überwiegend negativ")

        confidence, quality, gaps, warnings = self._confidence(data, score, supporting, opposing)
        reasons.extend((supporting + opposing)[:6])
        entry_reasons = tuple(reasons)
        if data.has_position and _number(data.profit_loss_percent):
            reasons.append(f"Positionsergebnis: {data.profit_loss_percent:.2f} %")
        if data.price_type == "DAILY_CLOSE":
            warnings.append("Nur Daily-Schlusskurs vorhanden; kein aktueller Intraday-Kurs.")
        if data.price_type == "MANUAL":
            warnings.append("Kurs wurde manuell eingetragen; technische Historie kann älter sein.")
        buy_low, buy_high, sell_low, sell_high = self._zones(data)
        invalidation = self._invalidation(data)
        signal = self._signal(score)
        entry_action = (DecisionAction.BUY if signal in (Signal.BULLISH, Signal.STRONG_BULLISH)
                        else DecisionAction.SELL if signal in (Signal.BEARISH, Signal.STRONG_BEARISH)
                        else DecisionAction.HOLD)
        entry_warnings = tuple(warnings)
        entry = PerspectiveResult(
            DecisionPerspective.NEW_ENTRY, entry_action, confidence,
            tuple(supporting), tuple(opposing), entry_reasons, entry_warnings,
            buy_low, buy_high, None, None, sell_low, invalidation,
        )
        position = None
        if data.has_position:
            # Ein Bestand kann nicht "gekauft" werden; Aufstockung ist NEW_ENTRY.
            position_action = DecisionAction.SELL if signal in (Signal.BEARISH, Signal.STRONG_BEARISH) else DecisionAction.HOLD
            position_confidence = min(100, confidence + (6 if data.purchase_price is not None else 0))
            position_reasons = list(reasons)
            if data.purchase_price is not None:
                position_reasons.append(f"Kaufpreis vorhanden ({data.purchase_price:.2f})")
            position = PerspectiveResult(
                DecisionPerspective.POSITION, position_action, position_confidence,
                tuple(supporting), tuple(opposing), tuple(position_reasons), tuple(warnings),
                None, None, sell_low, sell_high, sell_low, invalidation,
            )
        position_action = position.action if position else DecisionAction.HOLD
        action = position.action if position else entry.action
        if not data.has_position and action == DecisionAction.SELL:
            action = DecisionAction.HOLD
        warning = sell_low if data.has_position else None
        return DecisionResult(
            action=action, confidence=confidence, current_price=data.current_price,
            currency=data.currency, generated_at=data.generated_at or self._now(),
            data_timestamp=data.data_timestamp, technical_context_timestamp=data.technical_context_timestamp,
            has_position=data.has_position,
            buy_zone_low=buy_low, buy_zone_high=buy_high,
            sell_zone_low=sell_low, sell_zone_high=sell_high,
            warning_level=warning, invalidation_level=invalidation,
            reasons=tuple(reasons), warnings=tuple(warnings),
            supporting_signals=tuple(supporting), opposing_signals=tuple(opposing),
            data_quality=quality, data_gaps=tuple(gaps), signal=signal,
            position_action=position_action, entry_action=entry_action,
            position_decision=position, entry_decision=entry,
        )

    def _mkr_score(self, frameworks: tuple[Mapping[str, Any], ...]) -> int:
        score = 0
        for framework in frameworks:
            get = framework.get if isinstance(framework, Mapping) else lambda key, default=None: getattr(framework, key, default)
            quality = str(get("data_quality", "INSUFFICIENT")).upper()
            if quality in {"INSUFFICIENT", "NOT_AVAILABLE"}:
                continue
            confidence = float(get("confidence", 0) or 0)
            factor = 1.0 if quality in {"GOOD", "FULL"} else 0.5
            factor *= min(max(confidence, 0.0), 100.0) / 100.0
            signal = str(get("signal", "NEUTRAL")).upper()
            if signal == "BULLISH":
                score += round(self.weights.mkr * factor)
            elif signal == "BEARISH":
                score -= round(self.weights.mkr * factor)
        return score

    @staticmethod
    def _signal(score: int) -> Signal:
        if score >= 5:
            return Signal.STRONG_BULLISH
        if score >= 2:
            return Signal.BULLISH
        if score <= -5:
            return Signal.STRONG_BEARISH
        if score <= -2:
            return Signal.BEARISH
        return Signal.NEUTRAL

    def _confidence(self, data, score, supporting, opposing):
        supplied = [data.current_price, data.trend, data.performance_20d, data.performance_60d, data.sma20, data.sma50, data.rsi14, data.macd_histogram]
        present = sum(item is not None for item in supplied)
        quality = ("GOOD" if present >= 6 else "LIMITED" if present >= 3 else "INSUFFICIENT")
        gaps = list(data.data_gaps) + list(data.mkr_data_gaps)
        warnings: list[str] = []
        context_timestamp = data.technical_context_timestamp or data.data_timestamp
        if context_timestamp is not None:
            timestamp = _aware(context_timestamp)
            age_hours = (self._now() - timestamp).total_seconds() / 3600
            if age_hours > 24:
                warnings.append("Technischer Kontext ist älter als 24 Stunden.")
                gaps.append("veraltete Daten")
        if not supporting and not opposing:
            gaps.append("keine belastbaren Signale")
        if supporting and opposing:
            warnings.append("Signale widersprechen sich.")
        confidence = 35 + present * 7 + min(abs(score) * 4, 20) - len(gaps) * 6 - (12 if supporting and opposing else 0)
        return max(0, min(100, int(confidence))), quality, tuple(dict.fromkeys(gaps)), warnings

    @staticmethod
    def _with_technical_context(data: DecisionInput) -> DecisionInput:
        """Übernimmt nur vorhandene technische Felder aus einem älteren Kontext.

        Quote-Felder (Preis, Kursart und Quote-Zeitpunkt) bleiben ausdrücklich
        unangetastet. Es findet keine neue Indikatorberechnung statt.
        """
        if not data.technical_context:
            return data
        values = {name: getattr(data, name) for name in data.__dataclass_fields__}
        technical_fields = (
            "trend", "performance_5d", "performance_20d", "performance_60d",
            "sma20", "sma50", "sma200", "ema20", "ema50", "ema200",
            "rsi14", "macd", "macd_signal", "macd_histogram", "adx14",
            "trix15", "atr14", "bollinger_lower", "bollinger_upper",
            "supports", "resistances", "swing_lows", "swing_highs",
            "fibonacci_levels", "data_gaps", "data_quality",
        )
        for name in technical_fields:
            context_value = data.technical_context.get(name)
            if context_value is not None:
                values[name] = tuple(context_value) if name in {"supports", "resistances", "swing_lows", "swing_highs", "fibonacci_levels"} else context_value
        return DecisionInput(**values)

    @staticmethod
    def _zones(data):
        supports = sorted(set(x for x in data.supports + data.swing_lows + data.fibonacci_levels if _number(x) and (data.current_price is None or x <= data.current_price)))
        resistances = sorted(set(x for x in data.resistances + data.swing_highs + data.fibonacci_levels if _number(x) and (data.current_price is None or x >= data.current_price)))
        buy = supports[-2:] if supports else ()
        sell = resistances[:2] if resistances else ()
        return (min(buy) if buy else None, max(buy) if buy else None, min(sell) if sell else None, max(sell) if sell else None)

    @staticmethod
    def _invalidation(data):
        lows = [x for x in data.supports + data.swing_lows if _number(x)]
        return min(lows) if lows else None


def _number(value: Any) -> bool:
    try:
        return value is not None and isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
