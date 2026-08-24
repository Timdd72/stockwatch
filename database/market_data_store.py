"""Strukturierte Persistenz manuell geladener Unternehmensdaten."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import AnalystRecommendation, CompanyData, EarningsResult, NewsItem


class MarketDataStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def save_company(self, stock_id: int, provider: str, profile: dict[str, Any], metrics: dict[str, Any]) -> None:
        metric = metrics.get("metric", metrics) if isinstance(metrics, dict) else {}
        values = {
            "name": profile.get("name") or metrics.get("Name"),
            "country": profile.get("country") or metrics.get("Country"),
            "currency": profile.get("currency") or metrics.get("Currency"),
            "exchange": profile.get("exchange") or metrics.get("Exchange"),
            "industry": profile.get("finnhubIndustry") or metrics.get("Industry"),
            "market_cap": _number(profile.get("marketCapitalization") or metrics.get("MarketCapitalization")),
            "pe_ratio": _number(metric.get("peTTM") or metrics.get("PERatio")),
            "eps": _number(metric.get("epsTTM") or metrics.get("EPS")),
            "revenue": _number(metric.get("revenueTTM") or metrics.get("RevenueTTM")),
            "revenue_growth": _number(metric.get("revenueGrowthTTMYoy") or metrics.get("QuarterlyRevenueGrowthYOY")),
            "roe": _number(metric.get("roeTTM") or metrics.get("ReturnOnEquityTTM")),
            "debt_to_equity": _number(metric.get("totalDebt/totalEquityQuarterly")),
            "updated_at": datetime.now(timezone.utc),
        }
        with self._sessions.begin() as session:
            item = session.scalar(select(CompanyData).where(CompanyData.stock_id == stock_id, CompanyData.provider == provider))
            if item is None:
                item = CompanyData(stock_id=stock_id, provider=provider)
                session.add(item)
            for key, value in values.items():
                setattr(item, key, value)

    def save_news(self, stock_id: int, provider: str, rows: list[dict[str, Any]]) -> int:
        added = 0
        with self._sessions.begin() as session:
            existing = set(session.scalars(select(NewsItem.url).where(NewsItem.stock_id == stock_id)))
            for row in rows:
                url = str(row.get("url") or "").strip()
                headline = str(row.get("headline") or "").strip()
                if not url or not headline or url in existing:
                    continue
                raw_time = row.get("datetime")
                when = datetime.fromtimestamp(float(raw_time), timezone.utc) if raw_time else datetime.now(timezone.utc)
                session.add(NewsItem(stock_id=stock_id, datetime=when, headline=headline,
                                     source=str(row.get("source") or "Unbekannt"), url=url,
                                     summary=str(row.get("summary")) if row.get("summary") else None,
                                     provider=provider))
                existing.add(url)
                added += 1
        return added

    def save_analyst(self, stock_id: int, provider: str, rows: list[dict[str, Any]]) -> int:
        return self._upsert_dated(stock_id, provider, rows, AnalystRecommendation, "period", {
            "strong_buy": "strongBuy", "buy": "buy", "hold": "hold", "sell": "sell", "strong_sell": "strongSell"
        })

    def save_earnings(self, stock_id: int, provider: str, rows: list[dict[str, Any]]) -> int:
        return self._upsert_dated(stock_id, provider, rows, EarningsResult, "event_date", {
            "estimated_eps": "estimate", "actual_eps": "actual", "surprise_percent": "surprisePercent"
        }, source_date="period")

    def _upsert_dated(self, stock_id, provider, rows, model, date_field, mapping, source_date=None) -> int:
        saved = 0
        with self._sessions.begin() as session:
            for row in rows:
                try:
                    day = date.fromisoformat(str(row.get(source_date or date_field, ""))[:10])
                except ValueError:
                    continue
                item = session.scalar(select(model).where(model.stock_id == stock_id,
                    getattr(model, date_field) == day, model.provider == provider))
                if item is None:
                    item = model(stock_id=stock_id, provider=provider, **{date_field: day})
                    session.add(item)
                for target, source in mapping.items():
                    value = row.get(source)
                    setattr(item, target, int(value) if model is AnalystRecommendation and value is not None else _number(value))
                saved += 1
        return saved


def _number(value: object) -> float | None:
    if value in (None, "", "None", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
