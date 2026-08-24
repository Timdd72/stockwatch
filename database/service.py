"""Service-Schicht für fachliche StockWatch-Datenbankabläufe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session, sessionmaker

from analysis import TechnicalIndicators

from .connection import DEFAULT_DATABASE_PATH, create_database, create_session_factory
from .models import AnalysisSnapshot, Position, Stock
from .repository import StockWatchRepository


@dataclass(frozen=True, slots=True)
class PositionMetrics:
    position: Position
    invested_amount: Decimal
    current_value: Decimal | None
    profit_loss: Decimal | None
    profit_loss_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class DashboardData:
    stock: Stock | None
    snapshot: AnalysisSnapshot | None
    position: PositionMetrics | None
    technical_snapshot: AnalysisSnapshot | None = None


def calculate_position_metrics(
    position: Position, current_price: float | None
) -> PositionMetrics:
    invested = position.purchase_price * position.quantity
    if current_price is None:
        return PositionMetrics(position, invested, None, None, None)
    current_value = Decimal(str(current_price)) * position.quantity
    profit_loss = current_value - invested
    profit_loss_percent = profit_loss / invested * Decimal("100")
    return PositionMetrics(
        position, invested, current_value, profit_loss, profit_loss_percent
    )


class StockWatchService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def from_path(cls, path: str | Path = DEFAULT_DATABASE_PATH) -> "StockWatchService":
        return cls(create_session_factory(create_database(path)))

    def initialize_airbus(
        self,
        purchase_price: Decimal | None = None,
        quantity: Decimal | None = None,
        purchase_date: date | None = None,
    ) -> tuple[Stock, Position | None, bool]:
        if (purchase_price is None) != (quantity is None):
            raise ValueError("Kaufpreis und Stückzahl müssen gemeinsam angegeben werden.")
        if purchase_price is not None and purchase_price <= 0:
            raise ValueError("Der Kaufpreis muss größer als null sein.")
        if quantity is not None and quantity <= 0:
            raise ValueError("Die Stückzahl muss größer als null sein.")

        with self._session_factory.begin() as session:
            repository = StockWatchRepository(session)
            stock = repository.get_or_create_stock("AIR.PAR", "Airbus SE", "EUR", "Paris")
            if purchase_price is None or quantity is None:
                return stock, None, False
            position, created = repository.create_position_if_none(
                stock, purchase_price, quantity, purchase_date or date.today()
            )
            return stock, position, created

    def save_analysis(
        self, symbol: str, indicators: TechnicalIndicators, *,
        price_type: str | None = "DAILY_CLOSE",
        market_timestamp: datetime | None = None,
        fetched_at: datetime | None = None,
        provider: str | None = None,
    ) -> AnalysisSnapshot:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        with self._session_factory.begin() as session:
            repository = StockWatchRepository(session)
            stock = repository.get_stock_by_symbol(symbol)
            if stock is None:
                raise LookupError(f"Aktie {symbol.upper()} ist nicht angelegt.")
            return repository.add_analysis_snapshot(
                stock,
                timestamp=fetched_at,
                price=indicators.current_close,
                change_1d_percent=indicators.previous_day_change_pct,
                performance_5d=indicators.performance_5d_pct,
                performance_20d=indicators.performance_20d_pct,
                performance_60d=indicators.performance_60d_pct,
                sma20=indicators.sma20,
                sma50=indicators.sma50,
                sma200=indicators.sma200,
                rsi14=indicators.rsi14,
                volatility_20d=indicators.volatility_20d_pct,
                distance_sma20_percent=indicators.distance_sma20_pct,
                distance_sma50_percent=indicators.distance_sma50_pct,
                trend=indicators.trend,
                price_type=price_type,
                market_timestamp=market_timestamp,
                fetched_at=fetched_at,
                provider=provider,
            )

    def get_latest_analysis(self, symbol: str) -> AnalysisSnapshot | None:
        with self._session_factory() as session:
            repository = StockWatchRepository(session)
            stock = repository.get_stock_by_symbol(symbol)
            if stock is None:
                return None
            return repository.get_latest_snapshot(stock.id)

    def get_airbus_dashboard(self) -> DashboardData:
        with self._session_factory() as session:
            repository = StockWatchRepository(session)
            stock = repository.get_stock_by_symbol("AIR.PAR")
            if stock is None:
                return DashboardData(None, None, None)
            return self._build_dashboard(repository, stock)

    def list_stock_dashboards(self) -> list[DashboardData]:
        with self._session_factory() as session:
            repository = StockWatchRepository(session)
            return [
                self._build_dashboard(repository, stock)
                for stock in repository.list_stocks()
            ]

    def get_stock_dashboard(self, stock_id: int) -> DashboardData | None:
        with self._session_factory() as session:
            repository = StockWatchRepository(session)
            stock = session.get(Stock, stock_id)
            return self._build_dashboard(repository, stock) if stock is not None else None

    def add_airbus_position(
        self, purchase_price: Decimal, quantity: Decimal, purchase_date: date
    ) -> Position:
        with self._session_factory() as session:
            repository = StockWatchRepository(session)
            stock = repository.get_stock_by_symbol("AIR.PAR")
            if stock is None:
                raise LookupError("AIR.PAR ist nicht in der Datenbank angelegt.")
            stock_id = stock.id
        return self.add_position(stock_id, purchase_price, quantity, purchase_date)

    def add_position(
        self,
        stock_id: int,
        purchase_price: Decimal,
        quantity: Decimal,
        purchase_date: date,
    ) -> Position:
        if purchase_price <= 0:
            raise ValueError("Der Kaufpreis muss größer als null sein.")
        if quantity <= 0:
            raise ValueError("Die Stückzahl muss größer als null sein.")
        with self._session_factory.begin() as session:
            repository = StockWatchRepository(session)
            stock = session.get(Stock, stock_id)
            if stock is None:
                raise LookupError("Die Aktie wurde nicht gefunden.")
            return repository.add_active_position(stock, purchase_price, quantity, purchase_date)

    def save_manual_price(self, stock_id: int, price: Decimal,
                          entered_at: datetime | None = None) -> AnalysisSnapshot:
        if not price.is_finite() or price <= 0:
            raise ValueError("Kurs muss größer als null sein.")
        entered_at=entered_at or datetime.now(timezone.utc)
        with self._session_factory.begin() as session:
            stock=session.get(Stock,stock_id)
            if stock is None:raise LookupError("Die Aktie wurde nicht gefunden.")
            indicators=TechnicalIndicators(float(price),None,None,None,None,None,None,None,
                None,None,None,None,"NEUTRAL")
            repository=StockWatchRepository(session)
            return repository.add_analysis_snapshot(stock,timestamp=entered_at,price=float(price),
                change_1d_percent=None,performance_5d=None,performance_20d=None,performance_60d=None,
                sma20=None,sma50=None,sma200=None,rsi14=None,volatility_20d=None,
                distance_sma20_percent=None,distance_sma50_percent=None,trend=indicators.trend,
                price_type="MANUAL",market_timestamp=entered_at,fetched_at=entered_at,provider="manual",
                manual_source_url=stock.external_quote_url,manual_entered_at=entered_at,
                manual_entered_by="user")

    def set_external_quote_url(self,stock_id:int,value:str|None)->None:
        normalized=(value or "").strip() or None
        if normalized:
            parsed=urlparse(normalized)
            if parsed.scheme not in ("http","https") or not parsed.netloc:
                raise ValueError("Die externe Kurs-URL muss eine gültige HTTP- oder HTTPS-Adresse sein.")
        with self._session_factory.begin() as session:
            stock=session.get(Stock,stock_id)
            if stock is None:raise LookupError("Die Aktie wurde nicht gefunden.")
            stock.external_quote_url=normalized

    def select_catalog_security(self, security_id: int) -> tuple[Stock, bool]:
        """Übernimmt Stammdaten lokal in stocks, ohne Position oder Provideraufruf."""
        with self._session_factory.begin() as session:
            repository = StockWatchRepository(session)
            security = repository.get_catalog_security(security_id)
            if security is None:
                raise LookupError("Der Katalogeintrag wurde nicht gefunden.")
            existing = repository.get_stock_by_symbol(security.symbol)
            if existing is not None:
                return existing, False
            if not security.currency or not security.exchange:
                raise ValueError(
                    "Für die Auswahl müssen Währung und Börse im Katalog vorhanden sein."
                )
            stock = repository.get_or_create_stock(
                security.symbol,
                security.name,
                security.currency,
                security.exchange,
            )
            return stock, True

    @staticmethod
    def _build_dashboard(
        repository: StockWatchRepository, stock: Stock
    ) -> DashboardData:
        snapshot = repository.get_latest_snapshot(stock.id)
        technical_snapshot = repository.get_latest_technical_snapshot(stock.id)
        position = repository.get_active_position(stock.id)
        metrics = (
            calculate_position_metrics(
                position, snapshot.price if snapshot is not None else None
            )
            if position is not None
            else None
        )
        return DashboardData(stock, snapshot, metrics, technical_snapshot)
