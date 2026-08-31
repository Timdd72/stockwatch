"""Persistente Providerwahl und Capability-Ergebnisse pro Aktie."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from database.models import ProviderCapability, Stock, StockProviderSetting


DATA_TYPES = ("quote", "history", "fundamentals", "news", "analyst", "earnings")
PROVIDERS = ("alpha_vantage", "finnhub", "yahoo", "none")


class ProviderSettingsService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def ensure_defaults(self, stock_id: int) -> list[StockProviderSetting]:
        with self._session_factory.begin() as session:
            stock = session.get(Stock, stock_id)
            if stock is None:
                raise LookupError("Die Aktie wurde nicht gefunden.")
            existing = {
                item.data_type: item
                for item in session.scalars(
                    select(StockProviderSetting).where(
                        StockProviderSetting.stock_id == stock_id
                    )
                )
            }
            defaults = self._defaults(stock)
            for data_type, (primary, fallback) in defaults.items():
                if data_type not in existing:
                    item = StockProviderSetting(
                        stock_id=stock_id,
                        data_type=data_type,
                        primary_provider=primary,
                        fallback_provider=fallback,
                        auto_select=False,
                    )
                    session.add(item)
                    existing[data_type] = item
            session.flush()
            return [existing[data_type] for data_type in DATA_TYPES]

    def ensure_all_stocks(self) -> None:
        with self._session_factory() as session:
            ids = list(session.scalars(select(Stock.id)))
        for stock_id in ids:
            self.ensure_defaults(stock_id)

    def get_settings(self, stock_id: int) -> list[StockProviderSetting]:
        self.ensure_defaults(stock_id)
        with self._session_factory() as session:
            values = list(
                session.scalars(
                    select(StockProviderSetting).where(
                        StockProviderSetting.stock_id == stock_id
                    )
                )
            )
            by_type = {item.data_type: item for item in values}
            return [by_type[data_type] for data_type in DATA_TYPES]

    def get_setting(self, stock_id: int, data_type: str) -> StockProviderSetting:
        if data_type not in DATA_TYPES:
            raise ValueError("Unbekannter Datentyp.")
        return next(
            item for item in self.get_settings(stock_id) if item.data_type == data_type
        )

    def update_settings(
        self, stock_id: int, values: dict[str, tuple[str, str | None, bool]]
    ) -> None:
        self.ensure_defaults(stock_id)
        with self._session_factory.begin() as session:
            settings = {
                item.data_type: item
                for item in session.scalars(
                    select(StockProviderSetting).where(
                        StockProviderSetting.stock_id == stock_id
                    )
                )
            }
            for data_type, (primary, fallback, auto_select) in values.items():
                if data_type not in DATA_TYPES or primary not in PROVIDERS:
                    raise ValueError("Ungültige Provider-Konfiguration.")
                if fallback not in PROVIDERS and fallback is not None:
                    raise ValueError("Ungültiger Fallback-Provider.")
                item = settings[data_type]
                item.primary_provider = primary
                item.fallback_provider = None if fallback in (None, "none") else fallback
                item.auto_select = auto_select

    def record_success(self, stock_id: int, data_type: str, provider: str) -> None:
        with self._session_factory.begin() as session:
            item = session.scalar(
                select(StockProviderSetting).where(
                    StockProviderSetting.stock_id == stock_id,
                    StockProviderSetting.data_type == data_type,
                )
            )
            if item is not None:
                item.last_success_provider = provider
                item.last_checked_at = datetime.now(timezone.utc)

    def capabilities(self, stock_id: int) -> list[ProviderCapability]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(ProviderCapability).where(
                        ProviderCapability.stock_id == stock_id
                    )
                )
            )

    def capability_status(
        self, stock_id: int, provider: str, data_type: str
    ) -> str | None:
        with self._session_factory() as session:
            return session.scalar(
                select(ProviderCapability.status).where(
                    ProviderCapability.stock_id == stock_id,
                    ProviderCapability.provider == provider,
                    ProviderCapability.data_type == data_type,
                )
            )

    def save_capability(
        self, stock_id: int, provider: str, data_type: str, status: str
    ) -> None:
        with self._session_factory.begin() as session:
            item = session.scalar(
                select(ProviderCapability).where(
                    ProviderCapability.stock_id == stock_id,
                    ProviderCapability.provider == provider,
                    ProviderCapability.data_type == data_type,
                )
            )
            if item is None:
                session.add(
                    ProviderCapability(
                        stock_id=stock_id,
                        provider=provider,
                        data_type=data_type,
                        status=status,
                        last_checked_at=datetime.now(timezone.utc),
                    )
                )
            else:
                item.status = status
                item.last_checked_at = datetime.now(timezone.utc)

    def invalidate_capabilities(self,stock_id:int,provider:str)->int:
        """Entfernt nur Capability-Ergebnisse der betroffenen Symbolgrundlage."""
        with self._session_factory.begin() as session:
            rows=list(session.scalars(select(ProviderCapability).where(
                ProviderCapability.stock_id==stock_id,
                ProviderCapability.provider==provider)))
            for row in rows:session.delete(row)
            return len(rows)

    @staticmethod
    def _defaults(stock: Stock) -> dict[str, tuple[str, str | None]]:
        is_us = stock.currency.upper() == "USD" or "NASDAQ" in stock.exchange.upper()
        if is_us:
            return {
                "quote": ("finnhub", "alpha_vantage"),
                "history": ("finnhub", "alpha_vantage"),
                "fundamentals": ("finnhub", "alpha_vantage"),
                "news": ("finnhub", None),
                "analyst": ("finnhub", None),
                "earnings": ("finnhub", None),
            }
        return {
            "quote": ("finnhub", "alpha_vantage"),
            "history": ("finnhub", "alpha_vantage"),
            "fundamentals": ("finnhub", None),
            "news": ("finnhub", None),
            "analyst": ("finnhub", None),
            "earnings": ("finnhub", None),
        }
