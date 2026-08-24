"""Ausschließlich lokale Suche im Wertpapierkatalog."""

from __future__ import annotations

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .models import SecurityCatalog


class SearchService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def search(self, query: str, limit: int = 20) -> list[SecurityCatalog]:
        term = query.strip()
        if not term:
            return []
        normalized = term.upper()
        exact_conditions = [func.upper(SecurityCatalog.symbol) == normalized]
        if len(normalized) == 12:
            exact_conditions.append(func.upper(SecurityCatalog.isin) == normalized)

        conditions = list(exact_conditions)
        if len(term) >= 2:
            pattern = f"%{term.lower()}%"
            conditions.extend(
                [
                    func.lower(SecurityCatalog.name).like(pattern),
                    func.lower(SecurityCatalog.symbol).like(pattern),
                    func.lower(func.coalesce(SecurityCatalog.isin, "")).like(pattern),
                ]
            )

        ranking = case(
            (func.upper(SecurityCatalog.symbol) == normalized, 0),
            (func.upper(SecurityCatalog.isin) == normalized, 0),
            (func.lower(SecurityCatalog.name) == term.lower(), 1),
            (func.lower(SecurityCatalog.name).like(f"{term.lower()}%"), 2),
            (func.lower(SecurityCatalog.symbol).like(f"{term.lower()}%"), 3),
            else_=4,
        )
        statement = (
            select(SecurityCatalog)
            .where(or_(*conditions))
            .order_by(ranking, func.lower(SecurityCatalog.name), SecurityCatalog.symbol)
            .limit(min(max(limit, 1), 20))
        )
        with self._session_factory() as session:
            return list(session.scalars(statement))
