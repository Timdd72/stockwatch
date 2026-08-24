"""Gebündelte Datenbankzugriffe für StockWatch."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import AnalysisSnapshot, Position, PRICE_TYPES, SecurityCatalog, Stock


class StockWatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_stock_by_symbol(self, symbol: str) -> Stock | None:
        return self.session.scalar(select(Stock).where(Stock.symbol == symbol.upper()))

    def list_stocks(self) -> list[Stock]:
        return list(self.session.scalars(select(Stock).order_by(Stock.name, Stock.symbol)))

    def get_catalog_security(self, security_id: int) -> SecurityCatalog | None:
        return self.session.get(SecurityCatalog, security_id)

    def get_or_create_stock(
        self, symbol: str, name: str, currency: str, exchange: str
    ) -> Stock:
        normalized_symbol = symbol.strip().upper()
        stock = self.get_stock_by_symbol(normalized_symbol)
        if stock is None:
            stock = Stock(
                symbol=normalized_symbol,
                name=name,
                currency=currency,
                exchange=exchange,
            )
            self.session.add(stock)
            self.session.flush()
        return stock

    def create_position_if_none(
        self,
        stock: Stock,
        purchase_price: Decimal,
        quantity: Decimal,
        purchase_date: date,
    ) -> tuple[Position, bool]:
        existing = self.session.scalar(
            select(Position).where(Position.stock_id == stock.id).limit(1)
        )
        if existing is not None:
            return existing, False
        position = Position(
            stock_id=stock.id,
            purchase_price=purchase_price,
            quantity=quantity,
            purchase_date=purchase_date,
        )
        self.session.add(position)
        self.session.flush()
        return position, True

    def get_active_position(self, stock_id: int) -> Position | None:
        return self.session.scalar(
            select(Position)
            .where(Position.stock_id == stock_id, Position.active.is_(True))
            .order_by(Position.created_at.desc(), Position.id.desc())
            .limit(1)
        )

    def add_active_position(
        self,
        stock: Stock,
        purchase_price: Decimal,
        quantity: Decimal,
        purchase_date: date,
    ) -> Position:
        if self.get_active_position(stock.id) is not None:
            raise ValueError(f"Für {stock.symbol} existiert bereits eine aktive Position.")
        position = Position(
            stock_id=stock.id,
            purchase_price=purchase_price,
            quantity=quantity,
            purchase_date=purchase_date,
            active=True,
        )
        self.session.add(position)
        self.session.flush()
        return position

    def add_analysis_snapshot(
        self,
        stock: Stock,
        *,
        timestamp: datetime,
        price: float,
        change_1d_percent: float | None,
        performance_5d: float | None,
        performance_20d: float | None,
        performance_60d: float | None,
        sma20: float | None,
        sma50: float | None,
        sma200: float | None,
        rsi14: float | None,
        volatility_20d: float | None,
        distance_sma20_percent: float | None,
        distance_sma50_percent: float | None,
        trend: str,
        price_type: str | None = None,
        market_timestamp: datetime | None = None,
        fetched_at: datetime | None = None,
        provider: str | None = None,
        manual_source_url: str | None = None,
        manual_entered_at: datetime | None = None,
        manual_entered_by: str | None = None,
    ) -> AnalysisSnapshot:
        if price_type is not None and price_type not in PRICE_TYPES:
            raise ValueError("Ungültiger Kursdatentyp.")
        snapshot = AnalysisSnapshot(
            stock_id=stock.id,
            timestamp=timestamp,
            price=price,
            price_type=price_type,
            market_timestamp=market_timestamp,
            fetched_at=fetched_at or timestamp,
            provider=provider,
            manual_source_url=manual_source_url,
            manual_entered_at=manual_entered_at,
            manual_entered_by=manual_entered_by,
            change_1d_percent=change_1d_percent,
            performance_5d=performance_5d,
            performance_20d=performance_20d,
            performance_60d=performance_60d,
            sma20=sma20,
            sma50=sma50,
            sma200=sma200,
            rsi14=rsi14,
            volatility_20d=volatility_20d,
            distance_sma20_percent=distance_sma20_percent,
            distance_sma50_percent=distance_sma50_percent,
            trend=trend,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def get_latest_snapshot(self, stock_id: int) -> AnalysisSnapshot | None:
        return self.session.scalar(
            select(AnalysisSnapshot)
            .where(AnalysisSnapshot.stock_id == stock_id)
            .order_by(AnalysisSnapshot.timestamp.desc(), AnalysisSnapshot.id.desc())
            .limit(1)
        )

    def get_latest_technical_snapshot(self, stock_id: int) -> AnalysisSnapshot | None:
        """Liefert die jüngste Analyse mit mindestens einer berechneten Kennzahl."""
        return self.session.scalar(
            select(AnalysisSnapshot)
            .where(
                AnalysisSnapshot.stock_id == stock_id,
                or_(
                    AnalysisSnapshot.change_1d_percent.is_not(None),
                    AnalysisSnapshot.performance_5d.is_not(None),
                    AnalysisSnapshot.performance_20d.is_not(None),
                    AnalysisSnapshot.performance_60d.is_not(None),
                    AnalysisSnapshot.sma20.is_not(None),
                    AnalysisSnapshot.sma50.is_not(None),
                    AnalysisSnapshot.sma200.is_not(None),
                    AnalysisSnapshot.rsi14.is_not(None),
                    AnalysisSnapshot.volatility_20d.is_not(None),
                ),
            )
            .order_by(AnalysisSnapshot.timestamp.desc(), AnalysisSnapshot.id.desc())
            .limit(1)
        )
