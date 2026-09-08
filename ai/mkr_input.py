"""Read-only Sammlung lokaler StockWatch-Daten für MKR-14."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any, Callable

import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from analysis import (
    MkrTechnicalIndicators, aggregate_ohlcv, calculate_mkr_technical_indicators,
    calculate_technical_indicators,
)
from database.models import (
    AnalystRecommendation, CompanyData, EarningsResult, NewsItem, Position,
    ProviderCache, Stock, StockProviderSymbol,
)
from database.repository import StockWatchRepository
from database.service import calculate_position_metrics
from .mkr_prompt import MKR_PROMPT_VERSION
from .mkr_schemas import (
    MkrCoverage, MkrDataQuality, MkrFrameworkAvailability, MkrInputData,
)


HistoryLoader = Callable[[int], Any]


@dataclass(frozen=True, slots=True)
class LoadedHistory:
    history: pd.DataFrame
    provider: str | None
    provider_symbol: str | None
    fetched_at: datetime | None
    freshness: str

FRAMEWORK_NAMES = {
    1: "Elliott-Wellen", 2: "Fibonacci", 3: "Chartmuster", 4: "RSI",
    5: "Fair Value Gaps", 6: "Order Flow & Volumen",
    7: "MA-Struktur + Support/Resistance", 8: "Momentum", 9: "Options Flow",
    10: "Katalysatoren & Fundamentals", 11: "Multi-Zeitrahmen", 12: "UNI Score",
    13: "Technische Indikator-Bestätigung", 14: "Kapitalrotations-Disziplin",
}


class MkrInputAssembler:
    """Liest Daten ohne Persistenzänderung und schätzt keine fehlenden Werte."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        history_loader: HistoryLoader | None = None,
        history_max_age: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._history_loader = history_loader
        self._history_max_age = history_max_age
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        stock_id: int,
        *,
        history: pd.DataFrame | None = None,
        history_provider: str | None = None,
        history_provider_symbol: str | None = None,
    ) -> MkrInputData:
        loaded_history = LoadedHistory(
            history, history_provider, history_provider_symbol, None, "UNKNOWN",
        ) if history is not None else None
        with self._sessions() as session:
            stock = session.get(Stock, stock_id)
            if stock is None:
                raise LookupError("Die Aktie wurde nicht gefunden.")
            repository = StockWatchRepository(session)
            quote = repository.get_latest_snapshot(stock_id)
            technical_snapshot = repository.get_latest_technical_snapshot(stock_id)
            position = repository.get_active_position(stock_id)
            company = session.scalar(select(CompanyData).where(
                CompanyData.stock_id == stock_id).order_by(CompanyData.updated_at.desc()).limit(1))
            analyst = session.scalar(select(AnalystRecommendation).where(
                AnalystRecommendation.stock_id == stock_id).order_by(
                    AnalystRecommendation.period.desc()).limit(1))
            earnings = list(session.scalars(select(EarningsResult).where(
                EarningsResult.stock_id == stock_id).order_by(
                    EarningsResult.event_date.desc()).limit(6)))
            news = list(session.scalars(select(NewsItem).where(
                NewsItem.stock_id == stock_id).order_by(NewsItem.datetime.desc()).limit(10)))
            provider_symbols = list(session.scalars(select(StockProviderSymbol).where(
                StockProviderSymbol.stock_id == stock_id)))
            if loaded_history is None:
                cached = self._cached_history(session, stock, provider_symbols)
                if cached is not None:
                    loaded_history = cached

        if loaded_history is None and self._history_loader is not None:
            loaded = self._history_loader(stock_id)
            if loaded is not None:
                loaded_history = self._coerce_loaded_history(loaded)

        history = loaded_history.history if loaded_history else None
        history_provider = loaded_history.provider if loaded_history else None
        extended = calculate_mkr_technical_indicators(history) if history is not None else None
        metrics = calculate_position_metrics(position, quote.price if quote else None) if position else None
        technical_data = self._technical_data(technical_snapshot, extended, loaded_history)
        fundamental_data = self._fundamentals(company)
        if history is not None:
            local_high, local_low = _week_52(history, "High"), _week_52(history, "Low")
            if local_high is not None and local_low is not None:
                fundamental_data = fundamental_data or {}
                fundamental_data["week_52_high"] = local_high
                fundamental_data["week_52_low"] = local_low
                fundamental_data["week_52_source"] = "local_daily_ohlcv"
        analyst_data = self._analyst(analyst)
        sources = self._sources(quote, technical_snapshot, company, analyst, earnings, news,
                                history_provider, extended)
        availability = self._framework_availability(
            extended, technical_snapshot, fundamental_data, analyst_data, earnings, news, metrics,
        )
        return MkrInputData(
            prompt_version=MKR_PROMPT_VERSION,
            stock={"id": stock.id, "name": stock.name, "symbol": stock.symbol,
                   "exchange": stock.exchange, "currency": stock.currency},
            price=None if quote is None else {
                "value": quote.price, "price_type": quote.price_type,
                "market_timestamp": _iso(quote.market_timestamp),
                "fetched_at": _iso(quote.fetched_at or quote.timestamp),
                "provider": quote.provider,
                "manual_entered_at": _iso(quote.manual_entered_at),
                "manual_entered_by": quote.manual_entered_by,
            },
            position=None if metrics is None else {
                "purchase_price": float(metrics.position.purchase_price),
                "quantity": float(metrics.position.quantity),
                "purchase_date": metrics.position.purchase_date.isoformat(),
                "invested_amount": _decimal(metrics.invested_amount),
                "current_value": _decimal(metrics.current_value),
                "profit_loss": _decimal(metrics.profit_loss),
                "profit_loss_percent": _decimal(metrics.profit_loss_percent),
            },
            technical_data=technical_data,
            fundamental_data=fundamental_data,
            analyst_data=analyst_data,
            earnings_data=[{
                "event_date": item.event_date.isoformat(), "estimated_eps": item.estimated_eps,
                "actual_eps": item.actual_eps, "surprise_percent": item.surprise_percent,
                "provider": item.provider,
            } for item in earnings],
            news_data=[{
                "id": item.id, "published_at": _iso(item.datetime), "headline": item.headline,
                "summary": item.summary, "source": item.source, "url": item.url,
                "provider": item.provider, "untrusted_external_data": True,
            } for item in news],
            data_sources=sources,
            framework_availability=availability,
        )

    def _cached_history(
        self, session: Session, stock: Stock, provider_symbols: list[StockProviderSymbol],
    ) -> LoadedHistory | None:
        symbols = {stock.symbol, *(item.symbol for item in provider_symbols if item.symbol)}
        row = session.scalar(select(ProviderCache).where(
            ProviderCache.symbol.in_(symbols),
            ProviderCache.data_type.in_(("mkr_history", "scanner_history")),
            ProviderCache.fetched_at >= self._clock() - self._history_max_age,
        ).order_by(ProviderCache.fetched_at.desc()).limit(1))
        if row is None:
            return None
        try:
            records = json.loads(row.payload).get("records")
            frame = pd.DataFrame(records)
            if "Date" in frame and frame["Date"].notna().all():
                frame.index = pd.to_datetime(frame.pop("Date"), utc=True)
                frame.index.name = "Date"
            elif "Date" in frame:
                frame = frame.drop(columns=["Date"])
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return LoadedHistory(frame, row.provider, row.symbol, _aware(row.fetched_at), "FRESH") \
            if not frame.empty else None

    def _coerce_loaded_history(self, value: Any) -> LoadedHistory:
        if hasattr(value, "history") and hasattr(value, "provider"):
            fetched_at = getattr(value, "fetched_at", None)
            fresh = fetched_at is not None and _aware(fetched_at) >= self._clock() - self._history_max_age
            return LoadedHistory(value.history, value.provider,
                                 getattr(value, "provider_symbol", None), fetched_at,
                                 "FRESH" if fresh else "STALE" if fetched_at else "UNKNOWN")
        if isinstance(value, tuple) and len(value) >= 2:
            return LoadedHistory(value[0], value[1], value[2] if len(value) > 2 else None,
                                 value[3] if len(value) > 3 else None, "UNKNOWN")
        raise TypeError("History-Loader muss AnalysisHistory oder ein History/Provider-Tupel liefern.")

    @staticmethod
    def _technical_data(snapshot, extended: MkrTechnicalIndicators | None,
                        loaded: LoadedHistory | None) -> dict | None:
        if snapshot is None and extended is None:
            return None
        base = extended.base if extended else None
        result = {
            "history_metadata": _history_metadata(loaded, extended),
            "history_provider": loaded.provider if loaded else None,
            "history_start": _iso(extended.history_start) if extended else None,
            "history_end": _iso(extended.history_end) if extended else None,
            "trading_days": extended.trading_days if extended else None,
            "change_1d_percent": base.previous_day_change_pct if base else getattr(snapshot, "change_1d_percent", None),
            "performance_5d": base.performance_5d_pct if base else getattr(snapshot, "performance_5d", None),
            "performance_20d": base.performance_20d_pct if base else getattr(snapshot, "performance_20d", None),
            "performance_60d": base.performance_60d_pct if base else getattr(snapshot, "performance_60d", None),
            "sma20": base.sma20 if base else getattr(snapshot, "sma20", None),
            "sma50": base.sma50 if base else getattr(snapshot, "sma50", None),
            "sma200": base.sma200 if base else getattr(snapshot, "sma200", None),
            "rsi14": base.rsi14 if base else getattr(snapshot, "rsi14", None),
            "volatility_20d": base.volatility_20d_pct if base else getattr(snapshot, "volatility_20d", None),
            "distance_sma20_percent": base.distance_sma20_pct if base else getattr(snapshot, "distance_sma20_percent", None),
            "distance_sma50_percent": base.distance_sma50_pct if base else getattr(snapshot, "distance_sma50_percent", None),
            "trend": base.trend if base else getattr(snapshot, "trend", None),
        }
        if extended:
            result.update({
                "ema20": extended.ema20, "ema50": extended.ema50, "ema200": extended.ema200,
                "macd": extended.macd, "macd_signal": extended.macd_signal,
                "macd_histogram": extended.macd_histogram, "atr14": extended.atr14,
                "bollinger_middle": extended.bollinger_middle,
                "bollinger_upper": extended.bollinger_upper,
                "bollinger_lower": extended.bollinger_lower,
                "volume_latest": extended.volume_latest,
                "volume_average_20d": extended.volume_average_20d,
                "volume_vs_average_20d_percent": extended.volume_vs_average_20d_percent,
                "obv": extended.obv, "adx14": extended.adx14, "trix15": extended.trix15,
                "daily_fair_value_gaps": [_gap(item) for item in extended.fair_value_gaps],
                "swing_points": [_swing(item) for item in extended.swing_points],
                "fibonacci": _fibonacci(extended),
                "daily_ohlcv": _bars(loaded.history, 520),
                "timeframes": _timeframes(loaded.history, extended),
                "week_52_high": _week_52(loaded.history, "High"),
                "week_52_low": _week_52(loaded.history, "Low"),
            })
        return result

    @staticmethod
    def _fundamentals(company: CompanyData | None) -> dict | None:
        if company is None:
            return None
        return {
            "name": company.name, "country": company.country, "currency": company.currency,
            "exchange": company.exchange, "industry": company.industry,
            "market_cap": company.market_cap, "pe_ratio": company.pe_ratio, "eps": company.eps,
            "revenue": company.revenue, "revenue_growth": company.revenue_growth,
            "profit_growth": None, "roe": company.roe, "debt_to_equity": company.debt_to_equity,
            "week_52_low": None, "week_52_high": None, "peg": None,
            "insider_transactions": None, "analyst_target_low": None,
            "analyst_target_average": None, "analyst_target_high": None,
            "provider": company.provider, "updated_at": _iso(company.updated_at),
        }

    @staticmethod
    def _analyst(item: AnalystRecommendation | None) -> dict | None:
        if item is None:
            return None
        return {"period": item.period.isoformat(), "strong_buy": item.strong_buy,
                "buy": item.buy, "hold": item.hold, "sell": item.sell,
                "strong_sell": item.strong_sell, "provider": item.provider}

    @staticmethod
    def _sources(quote, technical, company, analyst, earnings, news,
                 history_provider, extended) -> list[dict]:
        values: list[dict] = []
        if quote:
            values.append({"data_type": "quote", "provider": quote.provider,
                           "timestamp": _iso(quote.market_timestamp or quote.fetched_at)})
        if technical:
            values.append({"data_type": "technical_snapshot", "provider": technical.provider,
                           "timestamp": _iso(technical.timestamp)})
        if extended:
            values.append({"data_type": "daily_ohlcv", "provider": history_provider,
                           "timestamp": _iso(extended.history_end)})
        if company:
            values.append({"data_type": "fundamentals", "provider": company.provider,
                           "timestamp": _iso(company.updated_at)})
        if analyst:
            values.append({"data_type": "analyst", "provider": analyst.provider,
                           "timestamp": analyst.period.isoformat()})
        values.extend({"data_type": "earnings", "provider": item.provider,
                       "timestamp": item.event_date.isoformat()} for item in earnings[:1])
        values.extend({"data_type": "news", "provider": item.provider,
                       "timestamp": _iso(item.datetime), "url": item.url} for item in news)
        return values

    @staticmethod
    def _framework_availability(extended, snapshot, fundamentals, analyst, earnings, news,
                                position) -> list[MkrFrameworkAvailability]:
        has_daily = extended is not None
        days = extended.trading_days if extended else 0
        has_volume = bool(extended and extended.volume_average_20d is not None and extended.obv is not None)
        has_momentum = bool(extended and extended.macd_histogram is not None and
                            extended.atr14 is not None and extended.adx14 is not None)
        has_long = bool(extended and extended.ema200 is not None)
        has_fundamentals = bool(fundamentals and any(
            fundamentals.get(key) is not None for key in
            ("market_cap", "pe_ratio", "eps", "revenue", "revenue_growth", "roe", "debt_to_equity")))
        has_rsi = bool((extended and extended.base.rsi14 is not None) or
                       (snapshot and snapshot.rsi14 is not None))
        specifications = {
            1: _availability(days >= 60, days >= 200, "Daily-OHLC liefert die Wellenbasis; die Zählung bleibt interpretativ."),
            2: _availability(bool(extended and extended.fibonacci), bool(extended and extended.fibonacci and days >= 100), "Belastbare lokale Swings und Fibonacci-Level erforderlich."),
            3: _availability(days >= 60, days >= 100, "Daily-OHLC erlaubt eine belastbare Musterprüfung."),
            4: _availability(has_rsi, False,
                             "Daily-/Weekly-RSI sind auswertbar; ohne belastbare Divergenzanalyse bleibt die Abdeckung LIMITED."),
            5: _availability(bool(extended and days >= 3), False, "Daily FVG ist berechenbar; echte 4H-Daten fehlen."),
            6: _availability(has_volume, False,
                             "Volumen und OBV sind lokal; ohne echten Order Flow, Block-Trades und Dark-Pool-Daten bleibt die Abdeckung LIMITED."),
            7: _availability(bool(extended and extended.ema50 is not None), has_long,
                             "Vollständige MA-Struktur benötigt mindestens 200 Handelstage."),
            8: _availability(has_momentum, has_momentum and bool(extended and extended.bollinger_upper),
                             "MACD, ATR, ADX und Bollinger benötigen ausreichend Daily-OHLC."),
            9: _availability(False, False, "Keine strukturierten Optionsdaten vorhanden."),
            10: _availability(has_fundamentals or bool(earnings or news), False,
                              "PEG, Insidertransaktionen und Analystenziele fehlen derzeit."),
            11: _availability(days >= 100, days >= 504,
                              "Weekly wird aus Daily aggregiert; eine belastbare Monatsstruktur benötigt etwa zwei Jahre."),
            12: _availability(has_fundamentals, False,
                              "Qualitative Monopol-/IP- und Analysten-Upside-Daten fehlen."),
            13: _availability(bool(extended and extended.trix15 is not None and has_rsi),
                              bool(extended and has_long and has_momentum and extended.trix15 is not None),
                              "Volle Bestätigung benötigt EMA200, RSI, MACD und TRIX."),
            14: _availability(bool(extended or snapshot), False,
                              "Preis-/ATR-Level sind nutzbar; Portfoliovermögen und Cashquote werden nicht gespeichert."),
        }
        return [MkrFrameworkAvailability(number=number, name=FRAMEWORK_NAMES[number],
                    data_quality=quality, coverage=coverage, reason=reason)
                for number, (quality, coverage, reason) in specifications.items()]


def _availability(available: bool, complete: bool, reason: str):
    if complete:
        return MkrDataQuality.GOOD, MkrCoverage.FULL, reason
    if available:
        return MkrDataQuality.LIMITED, MkrCoverage.LIMITED, reason
    return MkrDataQuality.INSUFFICIENT, MkrCoverage.NOT_AVAILABLE, reason


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _decimal(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _gap(item) -> dict:
    return {"direction": item.direction, "formed_at": _iso(item.formed_at),
            "position": item.position, "low": item.low, "high": item.high,
            "status": "UNFILLED"}


def _swing(item) -> dict:
    return {"kind": item.kind, "timestamp": _iso(item.timestamp),
            "position": item.position, "price": item.price}


def _fibonacci(extended: MkrTechnicalIndicators) -> dict | None:
    item = extended.fibonacci
    if item is None:
        return None
    return {"direction": item.direction, "swing_low": item.swing_low,
            "swing_high": item.swing_high, "swing_low_at": _iso(item.swing_low_at),
            "swing_high_at": _iso(item.swing_high_at),
            "retracements": item.retracements, "extensions": item.extensions}


def _timeframes(history: pd.DataFrame, extended: MkrTechnicalIndicators) -> dict:
    result: dict[str, Any] = {
        "daily": {"trend": extended.base.trend, "bars": _bars(history, 260)},
        "weekly": None,
        "monthly": None,
    }
    if not isinstance(history.index, pd.DatetimeIndex) or "Open" not in history:
        return result
    for name, limit in (("weekly", 104), ("monthly", 36)):
        aggregated = aggregate_ohlcv(history, name)
        if aggregated.empty:
            continue
        indicators = calculate_technical_indicators(aggregated)
        result[name] = {
            "number_of_rows": len(aggregated), "first_date": _iso(aggregated.index[0]),
            "last_date": _iso(aggregated.index[-1]), "trend": indicators.trend,
            "performance_5_periods": indicators.performance_5d_pct,
            "performance_20_periods": indicators.performance_20d_pct,
            "sma20": indicators.sma20, "sma50": indicators.sma50,
            "rsi14": indicators.rsi14, "bars": _bars(aggregated, limit),
        }
    return result


def _history_metadata(
    loaded: LoadedHistory | None, extended: MkrTechnicalIndicators | None,
) -> dict | None:
    if loaded is None or extended is None:
        return None
    frame = loaded.history
    columns = set(frame.columns)
    has_ohlc = {"Open", "High", "Low", "Close"}.issubset(columns)
    has_volume = "Volume" in columns and bool(frame["Volume"].notna().all())
    return {
        "provider": loaded.provider,
        "provider_symbol": loaded.provider_symbol,
        "first_date": _iso(extended.history_start),
        "last_date": _iso(extended.history_end),
        "number_of_rows": extended.trading_days,
        "has_ohlc": has_ohlc,
        "has_volume": has_volume,
        "fetched_at": _iso(loaded.fetched_at),
        "freshness": loaded.freshness,
        "sufficient_for_ema200": extended.trading_days >= 200,
        "sufficient_for_weekly": has_ohlc and extended.trading_days >= 75,
        "sufficient_for_monthly": has_ohlc and extended.trading_days >= 504,
    }


def _week_52(history: pd.DataFrame, column: str) -> float | None:
    if len(history) < 252 or column not in history:
        return None
    values = pd.to_numeric(history[column], errors="coerce").dropna().iloc[-252:]
    if len(values) < 252:
        return None
    return float(values.max() if column == "High" else values.min())


def _bars(history: pd.DataFrame, limit: int) -> list[dict]:
    values: list[dict] = []
    for index, row in history.sort_index().iloc[-limit:].iterrows():
        item: dict[str, Any] = {"date": _iso(index) if isinstance(
            history.index, pd.DatetimeIndex,
        ) else None}
        for column in ("Open", "High", "Low", "Close", "Volume"):
            value = row.get(column)
            item[column.lower()] = None if value is None or pd.isna(value) else float(value)
        values.append(item)
    return values


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
