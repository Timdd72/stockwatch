"""Bewusst ausgelöste Capability-Prüfung mit sparsamer Fallback-Reihenfolge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from market_data.alpha_vantage import AlphaVantageProvider
from market_data.finnhub import EndpointResult, FinnhubProvider
from market_data.usage import ApiLimitExceeded

from .market_updates import MarketUpdateService
from .provider_settings import ProviderSettingsService


class ProviderCapabilityService:
    def __init__(
        self,
        settings: ProviderSettingsService,
        updates: MarketUpdateService,
        alpha_factory: Callable[[], AlphaVantageProvider],
        finnhub_factory: Callable[[], FinnhubProvider],
    ) -> None:
        self._settings = settings
        self._updates = updates
        self._alpha_factory = alpha_factory
        self._finnhub_factory = finnhub_factory

    def estimate_requests(self, stock_id: int) -> int:
        support = self._updates.support_for_stock(stock_id)
        count = 0
        for setting in self._settings.get_settings(stock_id):
            for provider in (setting.primary_provider, setting.fallback_provider):
                if provider == "finnhub" and support.finnhub_symbol:
                    count += 1
                elif (
                    provider == "alpha_vantage"
                    and support.alpha_vantage_symbol
                    and setting.data_type in ("quote", "history", "fundamentals")
                ):
                    count += 1
        return count

    def check_stock(self, stock_id: int) -> int:
        support = self._updates.support_for_stock(stock_id)
        calls_before = 0
        for setting in self._settings.get_settings(stock_id):
            known_primary = self._settings.capability_status(
                stock_id, setting.primary_provider, setting.data_type
            )
            if known_primary == "premium_required":
                primary_status, used = known_primary, 0
            else:
                primary_status, used = self._check(
                    setting.primary_provider,
                    setting.data_type,
                    support.alpha_vantage_symbol,
                    support.finnhub_symbol,
                )
            calls_before += used
            self._settings.save_capability(
                stock_id, setting.primary_provider, setting.data_type, primary_status
            )
            if primary_status == "available":
                continue
            fallback = setting.fallback_provider
            if fallback and fallback != "none":
                known_fallback = self._settings.capability_status(stock_id, fallback, setting.data_type)
                if known_fallback == "premium_required":
                    fallback_status, used = known_fallback, 0
                else:
                    fallback_status, used = self._check(
                        fallback, setting.data_type, support.alpha_vantage_symbol,
                        support.finnhub_symbol,
                    )
                calls_before += used
                self._settings.save_capability(
                    stock_id, fallback, setting.data_type, fallback_status
                )
        return calls_before

    def _check(
        self,
        provider: str,
        data_type: str,
        alpha_symbol: str | None,
        finnhub_symbol: str | None,
    ) -> tuple[str, int]:
        if provider in ("none", "yahoo"):
            return "unavailable", 0
        try:
            if provider == "alpha_vantage":
                if not alpha_symbol or data_type not in (
                    "quote",
                    "history",
                    "fundamentals",
                ):
                    return "unavailable", 0
                alpha = self._alpha_factory()
                if data_type in ("quote", "history"):
                    alpha.get_daily(alpha_symbol)
                else:
                    alpha.get_overview(alpha_symbol)
                return "available", 1
            if provider == "finnhub":
                if not finnhub_symbol:
                    return "unavailable", 0
                result = self._check_finnhub(
                    self._finnhub_factory(), finnhub_symbol, data_type
                )
                return self._status(result), 1
            return "unavailable", 0
        except ApiLimitExceeded:
            raise
        except Exception:
            return "error", 1

    @staticmethod
    def _check_finnhub(
        provider: FinnhubProvider, symbol: str, data_type: str
    ) -> EndpointResult:
        now = datetime.now(timezone.utc)
        mapping = {
            "quote": ("Kurs", "quote", {}),
            "history": (
                "Historie",
                "stock/candle",
                {
                    "resolution": "D",
                    "from": int((now - timedelta(days=10)).timestamp()),
                    "to": int(now.timestamp()),
                },
            ),
            "fundamentals": ("Fundamentaldaten", "stock/metric", {"metric": "all"}),
            "news": (
                "News",
                "company-news",
                {
                    "from": (now.date() - timedelta(days=7)).isoformat(),
                    "to": now.date().isoformat(),
                },
            ),
            "analyst": ("Analysten", "stock/recommendation", {}),
            "earnings": ("Earnings", "stock/earnings", {"limit": 1}),
        }
        area, endpoint, params = mapping[data_type]
        return provider.request(area, endpoint, symbol=symbol, **params)

    @staticmethod
    def _status(result: EndpointResult) -> str:
        if result.availability == "verfügbar":
            return "available"
        if result.availability == "Premium-Zugang erforderlich":
            return "premium_required"
        if result.availability == "leer":
            return "unavailable"
        return "error"
