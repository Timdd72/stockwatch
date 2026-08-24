"""Realer, nicht persistierender Finnhub-Verfügbarkeitstest für Airbus SE."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sys
from typing import Any

from market_data.finnhub import EndpointResult, FinnhubError, FinnhubProvider


def value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        candidate = data.get(key)
        if candidate not in (None, "", 0):
            return candidate
    return None


def shown(item: Any, suffix: str = "") -> str:
    return "nicht verfügbar" if item is None else f"{item}{suffix}"


def timestamp(unix_time: Any) -> str:
    if not unix_time:
        return "nicht verfügbar"
    try:
        return datetime.fromtimestamp(int(unix_time), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return "nicht verfügbar"


def report_status(result: EndpointResult) -> None:
    suffix = f" – {result.message}" if result.message else ""
    print(f"{result.area}: {result.availability}{suffix}")


def main() -> int:
    provider: FinnhubProvider | None = None
    try:
        provider = FinnhubProvider.from_env()
        symbol, search = provider.resolve_airbus_symbol()
        today = date.today()

        results = {
            "profile": provider.request("Unternehmensdaten", "stock/profile2", symbol=symbol),
            "quote": provider.request("Kursdaten", "quote", symbol=symbol),
            "metrics": provider.request(
                "Fundamentaldaten", "stock/metric", symbol=symbol, metric="all"
            ),
            "financials": provider.request(
                "Veröffentlichte Finanzdaten",
                "stock/financials-reported",
                symbol=symbol,
                freq="annual",
            ),
            "recommendations": provider.request(
                "Analystenempfehlungen", "stock/recommendation", symbol=symbol
            ),
            "targets": provider.request(
                "Analysten-Kursziele", "stock/price-target", symbol=symbol
            ),
            "calendar": provider.request(
                "Nächster Earnings-Termin",
                "calendar/earnings",
                symbol=symbol,
                **{"from": today.isoformat(), "to": (today + timedelta(days=180)).isoformat()},
            ),
            "earnings": provider.request(
                "Letzte Earnings-Ergebnisse", "stock/earnings", symbol=symbol, limit=4
            ),
            "news": provider.request(
                "Unternehmensnachrichten",
                "company-news",
                symbol=symbol,
                **{"from": (today - timedelta(days=30)).isoformat(), "to": today.isoformat()},
            ),
        }

        profile = results["profile"].data if isinstance(results["profile"].data, dict) else {}
        quote = results["quote"].data if isinstance(results["quote"].data, dict) else {}
        metric_wrapper = (
            results["metrics"].data if isinstance(results["metrics"].data, dict) else {}
        )
        metrics = metric_wrapper.get("metric", {})
        if not isinstance(metrics, dict):
            metrics = {}

        print("AIRBUS / FINNHUB")
        print(f"Symbol:                 {symbol}")
        print(f"Handelsplatz:           {shown(profile.get('exchange'))}")
        print(f"ISIN:                   {shown(profile.get('isin'))}")
        print(f"Name:                   {shown(profile.get('name'))}")
        print(f"Währung:                {shown(profile.get('currency'))}")
        print(f"Land:                   {shown(profile.get('country'))}")
        print(f"Branche:                {shown(profile.get('finnhubIndustry'))}")
        print(f"Marktkapitalisierung:   {shown(profile.get('marketCapitalization'))}")
        print("\nVerfügbarkeit:")
        report_status(search)
        for result in results.values():
            report_status(result)

        print("\nKurs:")
        print(f"Aktueller Kurs:         {shown(quote.get('c'))}")
        print(f"Vorheriger Schlusskurs: {shown(quote.get('pc'))}")
        print(f"Tageshoch:              {shown(quote.get('h'))}")
        print(f"Tagestief:              {shown(quote.get('l'))}")
        print(f"Kurszeitpunkt (UTC):    {timestamp(quote.get('t'))}")

        print("\nFundamentaldaten:")
        fundamental_fields = {
            "KGV": ("peBasicExclExtraTTM", "peTTM"),
            "EPS": ("epsBasicExclExtraItemsTTM", "epsTTM"),
            "Umsatz je Aktie TTM": ("revenuePerShareTTM",),
            "Umsatzwachstum": ("revenueGrowthTTMYoy", "revenueGrowth3Y"),
            "Gewinnwachstum": ("netIncomeGrowthTTMYoy", "epsGrowthTTMYoy"),
            "Eigenkapitalrendite": ("roeTTM", "roeRfy"),
            "Gesamtverschuldung/Eigenkapital": (
                "totalDebt/totalEquityAnnual",
                "totalDebt/totalEquityQuarterly",
            ),
            "52-Wochen-Hoch": ("52WeekHigh",),
            "52-Wochen-Tief": ("52WeekLow",),
        }
        for label, keys in fundamental_fields.items():
            print(f"{label + ':':31}{shown(value(metrics, *keys))}")

        recommendations = results["recommendations"].data
        if isinstance(recommendations, list) and recommendations:
            latest = recommendations[0]
            print("\nNeueste Analystenempfehlung:")
            for key in ("period", "strongBuy", "buy", "hold", "sell", "strongSell"):
                print(f"{key}: {latest.get(key)}")

        targets = results["targets"].data
        if isinstance(targets, dict) and targets:
            print("\nAnalysten-Kursziele:")
            for key in ("lastUpdated", "targetHigh", "targetLow", "targetMean", "targetMedian"):
                print(f"{key}: {shown(targets.get(key))}")

        calendar = results["calendar"].data
        if isinstance(calendar, dict):
            events = calendar.get("earningsCalendar", [])
            if events:
                print(f"\nNächster Earnings-Termin: {events[0]}")
        earnings = results["earnings"].data
        if isinstance(earnings, list) and earnings:
            print(f"Letztes Earnings-Ergebnis: {earnings[0]}")

        news = results["news"].data
        if isinstance(news, list) and news:
            print("\nAktuelle Unternehmensnachrichten (maximal 10):")
            for item in news[:10]:
                print(
                    f"- {timestamp(item.get('datetime'))} | {item.get('headline')} | "
                    f"{item.get('source')} | {item.get('url')}"
                )

        print(f"\nFinnhub-API-Aufrufe: {provider.api_calls}")
        return 0
    except FinnhubError as exc:
        calls = provider.api_calls if provider is not None else 0
        print(f"Finnhub-Fehler: {exc}", file=sys.stderr)
        print(f"Finnhub-API-Aufrufe bis zum Fehler: {calls}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
