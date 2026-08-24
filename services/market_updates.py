"""Wiederverwendbare manuelle Marktdaten-Aktualisierungen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import re
from typing import Callable

import pandas as pd

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from analysis import TechnicalIndicators, calculate_technical_indicators
from database.api_usage import ApiUsageService
from database.market_data_store import MarketDataStore
from database.models import NewsItem, ProviderCache, SecurityCatalog, Stock
from database.service import StockWatchService
from market_data.alpha_vantage import AlphaVantageError, AlphaVantageProvider
from market_data.finnhub import EndpointResult, FinnhubError, FinnhubProvider
from market_data.usage import ApiLimitExceeded
from .provider_settings import ProviderSettingsService


class MarketUpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderSupport:
    alpha_vantage_symbol: str | None
    finnhub_symbol: str | None


class MarketUpdateService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        usage: ApiUsageService,
        alpha_factory: Callable[[], AlphaVantageProvider] | None = None,
        finnhub_factory: Callable[[], FinnhubProvider] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._usage = usage
        self._stock_service = StockWatchService(session_factory)
        self._settings = ProviderSettingsService(session_factory)
        self._data_store = MarketDataStore(session_factory)
        self._alpha_factory = alpha_factory or (
            lambda: AlphaVantageProvider.from_env(usage_recorder=usage)
        )
        self._finnhub_factory = finnhub_factory or (
            lambda: FinnhubProvider.from_env(usage_recorder=usage)
        )

    def support_for_stock(self, stock_id: int) -> ProviderSupport:
        with self._session_factory() as session:
            stock = session.get(Stock, stock_id)
            if stock is None:
                raise LookupError("Die Aktie wurde nicht gefunden.")
            catalog = session.scalar(
                select(SecurityCatalog)
                .where(
                    or_(
                        SecurityCatalog.symbol == stock.symbol,
                        SecurityCatalog.provider_symbol_alpha_vantage == stock.symbol,
                        SecurityCatalog.provider_symbol_finnhub == stock.symbol,
                    )
                )
                .limit(1)
            )
            alpha = catalog.provider_symbol_alpha_vantage if catalog else None
            finnhub = catalog.provider_symbol_finnhub if catalog else None
            if finnhub is None and (
                "NASDAQ" in stock.exchange.upper()
                and stock.currency.upper() == "USD"
                and re.fullmatch(r"[A-Z][A-Z0-9]{0,5}", stock.symbol)
            ):
                finnhub = stock.symbol
            return ProviderSupport(alpha, finnhub)

    def support_map(self, stock_ids: list[int]) -> dict[int, ProviderSupport]:
        return {stock_id: self.support_for_stock(stock_id) for stock_id in stock_ids}

    def update_alpha_vantage(self, stock_id: int) -> int:
        support = self.support_for_stock(stock_id)
        if support.alpha_vantage_symbol is None:
            raise MarketUpdateError("Für diese Aktie ist kein Alpha-Vantage-Symbol hinterlegt.")
        history = self._alpha_factory().get_daily(support.alpha_vantage_symbol)
        indicators = calculate_technical_indicators(history)
        stock = self._get_stock(stock_id)
        market_timestamp = _daily_market_timestamp(history.index[-1])
        snapshot = self._stock_service.save_analysis(stock.symbol, indicators,
            price_type="DAILY_CLOSE", market_timestamp=market_timestamp,
            provider="alpha_vantage")
        return snapshot.id

    def update_finnhub_quote(self, stock_id: int) -> int:
        if self._settings.capability_status(stock_id,"finnhub","quote") == "premium_required":
            raise MarketUpdateError("Finnhub-Quote ist laut Capability-Prüfung nur mit Premium verfügbar.")
        support = self.support_for_stock(stock_id)
        if support.finnhub_symbol is None:
            raise MarketUpdateError("Für diese Aktie ist kein Finnhub-Symbol verfügbar.")
        result = self._finnhub_factory().request(
            "Kursdaten", "quote", symbol=support.finnhub_symbol
        )
        data = self._require_data(result)
        price = data.get("c") if isinstance(data, dict) else None
        if not isinstance(price, (int, float)) or price <= 0:
            raise MarketUpdateError("Finnhub lieferte keinen gültigen aktuellen Kurs.")
        stock = self._get_stock(stock_id)
        indicators = TechnicalIndicators(
            current_close=float(price),
            previous_day_change_pct=None,
            performance_5d_pct=None,
            performance_20d_pct=None,
            performance_60d_pct=None,
            sma20=None,
            sma50=None,
            sma200=None,
            rsi14=None,
            volatility_20d_pct=None,
            distance_sma20_pct=None,
            distance_sma50_pct=None,
            trend="NEUTRAL",
        )
        market_timestamp = _epoch_timestamp(data.get("t")) if isinstance(data, dict) else None
        return self._stock_service.save_analysis(stock.symbol, indicators,
            price_type="REALTIME", market_timestamp=market_timestamp,
            provider="finnhub").id

    def update_quote(self, stock_id: int) -> int:
        """Aktualisiert den Kurs gemäß persistierter Provider-Reihenfolge."""
        return self._with_fallback(stock_id, "quote", self._quote_with_provider)

    def update_history(self, stock_id: int) -> int:
        """Aktualisiert Historie und technische Analyse gemäß Konfiguration."""
        return self._with_fallback(stock_id, "history", self._history_with_provider)

    def load_history(self, provider: str, symbol: str) -> pd.DataFrame:
        """Lädt Historie über die vorhandenen Providerclients, ohne Stock anzulegen."""
        if provider == "alpha_vantage":
            # Der kostenlose Zugang liefert compact (bis 100 Handelstage);
            # full ist bei aktuellen Alpha-Vantage-Tarifen Premium.
            return self._alpha_factory().get_daily(symbol)
        if provider == "finnhub":
            now = datetime.now(timezone.utc)
            result = self._finnhub_factory().request(
                "Historie", "stock/candle", symbol=symbol, resolution="D",
                **{"from": int((now - timedelta(days=400)).timestamp()), "to": int(now.timestamp())},
            )
            data = self._require_data(result)
            if not isinstance(data, dict) or not data.get("c"):
                raise MarketUpdateError("Finnhub lieferte keine Historie.")
            history = pd.DataFrame({
                "Close": data["c"],
                "High": data.get("h", data["c"]),
                "Low": data.get("l", data["c"]),
            })
            if data.get("t"):
                history.index = pd.to_datetime(data["t"], unit="s", utc=True)
            return history.sort_index()
        raise MarketUpdateError("Provider unterstützt keine Historie.")

    def update_company_data(self, stock_id: int) -> tuple[int, int]:
        """Aktualisiert langlebigere Datentypen; frische Cachewerte werden geschont."""
        fetched = cached = 0
        ages = {
            "fundamentals": timedelta(hours=24),
            "analyst": timedelta(hours=24),
            "earnings": timedelta(hours=24),
            "news": timedelta(hours=4),
        }
        for data_type, max_age in ages.items():
            setting = self._settings.get_setting(stock_id, data_type)
            support = self.support_for_stock(stock_id)
            symbol = self._symbol_for(setting.primary_provider, support)
            if symbol and self._cache_is_fresh(
                setting.primary_provider, symbol, data_type, max_age
            ):
                cached += 1
                continue
            self._with_fallback(stock_id, data_type, self._company_with_provider)
            fetched += 1
        return fetched, cached

    def update_fundamentals(self, stock_id: int) -> None:
        self._with_fallback(stock_id, "fundamentals", self._company_with_provider)

    def update_news(self, stock_id: int) -> None:
        self._with_fallback(stock_id, "news", self._company_with_provider)

    def update_analyst(self, stock_id: int) -> None:
        self._with_fallback(stock_id, "analyst", self._company_with_provider)

    def update_earnings(self, stock_id: int) -> None:
        self._with_fallback(stock_id, "earnings", self._company_with_provider)

    def _with_fallback(self, stock_id: int, data_type: str, operation):
        setting = self._settings.get_setting(stock_id, data_type)
        providers = [setting.primary_provider]
        if setting.fallback_provider and setting.fallback_provider not in providers:
            providers.append(setting.fallback_provider)
        last_error: Exception | None = None
        for provider in providers:
            if provider in ("none", "yahoo"):
                last_error = MarketUpdateError(f"Provider {provider} ist deaktiviert.")
                continue
            known_status = self._settings.capability_status(
                stock_id, provider, data_type
            )
            if known_status in (
                "unavailable",
                "premium_required",
            ):
                last_error = MarketUpdateError(
                    f"{provider} ist laut Capability-Prüfung nicht verfügbar."
                )
                continue
            try:
                value = operation(stock_id, provider, data_type)
                self._settings.record_success(stock_id, data_type, provider)
                return value
            except ApiLimitExceeded:
                # Ein lokales Limit darf nicht durch einen anderen Provider umgangen werden.
                raise
            except MarketUpdateError as exc:
                last_error = exc
            except (AlphaVantageError, FinnhubError) as exc:
                last_error = MarketUpdateError(str(exc))
        raise last_error or MarketUpdateError("Kein geeigneter Provider konfiguriert.")

    def _quote_with_provider(self, stock_id: int, provider: str, _data_type: str) -> int:
        support = self.support_for_stock(stock_id)
        if provider == "alpha_vantage":
            symbol = self._symbol_for(provider, support)
            if not symbol:
                raise MarketUpdateError("Kein Alpha-Vantage-Symbol hinterlegt.")
            history = self._alpha_factory().get_daily(symbol)
            if history.empty:
                raise MarketUpdateError("Alpha Vantage lieferte keinen Kurs.")
            return self._save_price(stock_id, float(history["Close"].iloc[-1]),
                price_type="DAILY_CLOSE", market_timestamp=_daily_market_timestamp(history.index[-1]),
                provider="alpha_vantage")
        if provider == "finnhub":
            symbol = self._symbol_for(provider, support)
            if not symbol:
                raise MarketUpdateError("Kein Finnhub-Symbol hinterlegt.")
            data = self._require_data(
                self._finnhub_factory().request("Kurs", "quote", symbol=symbol)
            )
            price = data.get("c") if isinstance(data, dict) else None
            if not isinstance(price, (int, float)) or price <= 0:
                raise MarketUpdateError("Finnhub lieferte keinen gültigen Kurs.")
            return self._save_price(stock_id, float(price), price_type="REALTIME",
                market_timestamp=_epoch_timestamp(data.get("t")), provider="finnhub")
        raise MarketUpdateError("Provider unterstützt keine Kursaktualisierung.")

    def _history_with_provider(self, stock_id: int, provider: str, _data_type: str) -> int:
        support = self.support_for_stock(stock_id)
        symbol = self._symbol_for(provider, support)
        if not symbol:
            raise MarketUpdateError(f"Kein Symbol für {provider} hinterlegt.")
        history = self.load_history(provider, symbol)
        indicators = calculate_technical_indicators(history)
        stock = self._get_stock(stock_id)
        return self._stock_service.save_analysis(stock.symbol, indicators,
            price_type="DAILY_CLOSE", market_timestamp=_daily_market_timestamp(history.index[-1]),
            provider=provider).id

    def _company_with_provider(self, stock_id: int, provider: str, data_type: str) -> None:
        support = self.support_for_stock(stock_id)
        symbol = self._symbol_for(provider, support)
        if not symbol:
            raise MarketUpdateError(f"Kein Symbol für {provider} hinterlegt.")
        if provider == "alpha_vantage":
            if data_type != "fundamentals":
                raise MarketUpdateError("Alpha Vantage ist für diesen Datentyp nicht eingerichtet.")
            data = self._alpha_factory().get_overview(symbol)
            result = EndpointResult(
                "Fundamentaldaten", "verfügbar" if data else "leer", data
            )
            self._require_data(result)
            self._data_store.save_company(stock_id, provider, {}, data)
        elif provider == "finnhub":
            today = date.today()
            if data_type == "fundamentals":
                profile_result = self._finnhub_factory().request(
                    "Unternehmensprofil", "stock/profile2", symbol=symbol
                )
                profile = self._require_data(profile_result)
                result = self._finnhub_factory().request(
                    "Fundamentaldaten", "stock/metric", symbol=symbol, metric="all"
                )
                metrics = self._require_data(result)
                self._data_store.save_company(
                    stock_id, provider,
                    profile if isinstance(profile, dict) else {},
                    metrics if isinstance(metrics, dict) else {},
                )
                return
            mapping = {
                "analyst": ("stock/recommendation", {}),
                "earnings": ("stock/earnings", {"limit": 4}),
                "news": ("company-news", {"from": self._news_from_date(stock_id).isoformat(), "to": today.isoformat()}),
            }
            endpoint, params = mapping[data_type]
            result = self._finnhub_factory().request(data_type, endpoint, symbol=symbol, **params)
            data = self._require_data(result)
            rows = data if isinstance(data, list) else []
            if data_type == "news":
                self._data_store.save_news(stock_id, provider, rows[:10])
            elif data_type == "analyst":
                self._data_store.save_analyst(stock_id, provider, rows)
            elif data_type == "earnings":
                self._data_store.save_earnings(stock_id, provider, rows)
        else:
            raise MarketUpdateError("Provider unterstützt diesen Datentyp nicht.")
        self._store_cache(provider, symbol, data_type, result)

    def _news_from_date(self, stock_id: int) -> date:
        earliest = date.today() - timedelta(days=30)
        with self._session_factory() as session:
            latest = session.scalar(
                select(func.max(NewsItem.datetime)).where(NewsItem.stock_id == stock_id)
            )
        return max(earliest, latest.date()) if latest else earliest

    def _save_price(self, stock_id: int, price: float, *, price_type: str,
                    market_timestamp: datetime | None, provider: str) -> int:
        stock = self._get_stock(stock_id)
        indicators = TechnicalIndicators(price, None, None, None, None, None, None, None,
                                          None, None, None, None, "NEUTRAL")
        return self._stock_service.save_analysis(stock.symbol, indicators,
            price_type=price_type, market_timestamp=market_timestamp, provider=provider).id

    @staticmethod
    def _symbol_for(provider: str, support: ProviderSupport) -> str | None:
        return support.alpha_vantage_symbol if provider == "alpha_vantage" else (
            support.finnhub_symbol if provider == "finnhub" else None
        )

    def update_finnhub_company_data(self, stock_id: int) -> tuple[int, int]:
        support = self.support_for_stock(stock_id)
        if support.finnhub_symbol is None:
            raise MarketUpdateError("Für diese Aktie ist kein Finnhub-Symbol verfügbar.")
        provider = self._finnhub_factory()
        symbol = support.finnhub_symbol
        today = date.today()
        requests = (
            ("profile", timedelta(hours=24), "Unternehmensprofil", "stock/profile2", {}),
            ("fundamentals", timedelta(hours=24), "Fundamentaldaten", "stock/metric", {"metric": "all"}),
            ("analysts", timedelta(hours=24), "Analysten", "stock/recommendation", {}),
            (
                "earnings_calendar",
                timedelta(hours=24),
                "Earnings-Kalender",
                "calendar/earnings",
                {"from": today.isoformat(), "to": (today + timedelta(days=180)).isoformat()},
            ),
            ("earnings", timedelta(hours=24), "Earnings", "stock/earnings", {"limit": 4}),
            (
                "news",
                timedelta(hours=4),
                "News",
                "company-news",
                {"from": (today - timedelta(days=30)).isoformat(), "to": today.isoformat()},
            ),
        )
        fetched = cached = 0
        for data_type, max_age, area, endpoint, params in requests:
            if self._cache_is_fresh("finnhub", symbol, data_type, max_age):
                cached += 1
                continue
            result = provider.request(area, endpoint, symbol=symbol, **params)
            if result.availability == "API-Fehler":
                raise MarketUpdateError(result.message or f"Finnhub-Fehler bei {area}.")
            self._store_cache("finnhub", symbol, data_type, result)
            fetched += 1
        return fetched, cached

    def _get_stock(self, stock_id: int) -> Stock:
        with self._session_factory() as session:
            stock = session.get(Stock, stock_id)
            if stock is None:
                raise LookupError("Die Aktie wurde nicht gefunden.")
            return stock

    @staticmethod
    def _require_data(result: EndpointResult) -> object:
        if result.availability not in ("verfügbar", "leer"):
            raise MarketUpdateError(result.message or f"Finnhub: {result.availability}")
        return result.data

    def _cache_is_fresh(
        self, provider: str, symbol: str, data_type: str, max_age: timedelta
    ) -> bool:
        with self._session_factory() as session:
            item = session.scalar(
                select(ProviderCache).where(
                    ProviderCache.provider == provider,
                    ProviderCache.symbol == symbol,
                    ProviderCache.data_type == data_type,
                )
            )
            if item is None:
                return False
            fetched_at = item.fetched_at
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            return fetched_at >= datetime.now(timezone.utc) - max_age

    def _store_cache(
        self, provider: str, symbol: str, data_type: str, result: EndpointResult
    ) -> None:
        payload = json.dumps(
            {"availability": result.availability, "data": result.data},
            ensure_ascii=False,
            default=str,
        )
        with self._session_factory.begin() as session:
            item = session.scalar(
                select(ProviderCache).where(
                    ProviderCache.provider == provider,
                    ProviderCache.symbol == symbol,
                    ProviderCache.data_type == data_type,
                )
            )
            if item is None:
                session.add(
                    ProviderCache(
                        provider=provider,
                        symbol=symbol,
                        data_type=data_type,
                        fetched_at=datetime.now(timezone.utc),
                        payload=payload,
                    )
                )
            else:
                item.fetched_at = datetime.now(timezone.utc)
                item.payload = payload


def _daily_market_timestamp(value: object) -> datetime | None:
    """Bewahrt den Börsentag eines Daily-Candles ohne erfundene Uhrzeit."""
    if isinstance(value, (int, float)):
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    # Daily-Daten enthalten nur ein Datum; Mitternacht UTC dient als Datums-Träger.
    return datetime(stamp.year, stamp.month, stamp.day, tzinfo=timezone.utc)


def _epoch_timestamp(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc) if value else None
    except (TypeError, ValueError, OSError):
        return None
