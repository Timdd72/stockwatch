"""Nicht persistierender Finnhub-Vergleichstest für Microsoft (MSFT)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sys
from typing import Any

from market_data.finnhub import EndpointResult, FinnhubError, FinnhubProvider


SYMBOL = "MSFT"
AIRBUS_RESULTS = {
    "Unternehmensprofil": "Premium erforderlich",
    "Kurs": "Premium erforderlich",
    "Fundamentaldaten": "Premium erforderlich",
    "Analysten": "Premium erforderlich",
    "Kursziele": "Premium erforderlich",
    "Earnings": "Premium erforderlich",
    "News": "Premium erforderlich",
}


def shown(value: Any) -> str:
    return "nicht verfügbar" if value in (None, "") else str(value)


def first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None


def utc_timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return "nicht verfügbar"


def short_status(result: EndpointResult) -> str:
    if result.availability == "Premium-Zugang erforderlich":
        return "Premium erforderlich"
    return result.availability


def combined_status(*results: EndpointResult) -> str:
    statuses = [result.availability for result in results]
    if "verfügbar" in statuses:
        return "verfügbar"
    if "API-Fehler" in statuses:
        return "API-Fehler"
    if "Premium-Zugang erforderlich" in statuses:
        return "Premium erforderlich"
    return "leer"


def report(result: EndpointResult) -> None:
    suffix = f" – {result.message}" if result.message else ""
    print(f"{result.area}: {result.availability}{suffix}")


def latest_reported_revenue(payload: Any) -> tuple[Any, Any, Any]:
    """Liest Umsatz, Einheit und Berichtsende aus dem jüngsten Jahresbericht."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None, None, None
    preferred_concepts = (
        "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax",
        "us-gaap_Revenues",
        "us-gaap_SalesRevenueNet",
    )
    for filing in payload["data"]:
        if not isinstance(filing, dict):
            continue
        report_data = filing.get("report", {})
        income_statement = report_data.get("ic", []) if isinstance(report_data, dict) else []
        if not isinstance(income_statement, list):
            continue
        by_concept = {
            item.get("concept"): item
            for item in income_statement
            if isinstance(item, dict)
        }
        for concept in preferred_concepts:
            item = by_concept.get(concept)
            if item:
                return item.get("value"), item.get("unit"), filing.get("endDate")
    return None, None, None


def main() -> int:
    provider: FinnhubProvider | None = None
    try:
        provider = FinnhubProvider.from_env()
        today = date.today()
        results = {
            "profile": provider.request("Unternehmensprofil", "stock/profile2", symbol=SYMBOL),
            "quote": provider.request("Kursdaten", "quote", symbol=SYMBOL),
            "metrics": provider.request(
                "Fundamentaldaten", "stock/metric", symbol=SYMBOL, metric="all"
            ),
            "financials": provider.request(
                "Veröffentlichte Finanzdaten",
                "stock/financials-reported",
                symbol=SYMBOL,
                freq="annual",
            ),
            "recommendations": provider.request(
                "Analystenempfehlungen", "stock/recommendation", symbol=SYMBOL
            ),
            "targets": provider.request(
                "Analysten-Kursziele", "stock/price-target", symbol=SYMBOL
            ),
            "calendar": provider.request(
                "Nächster Earnings-Termin",
                "calendar/earnings",
                symbol=SYMBOL,
                **{"from": today.isoformat(), "to": (today + timedelta(days=180)).isoformat()},
            ),
            "earnings": provider.request(
                "Letzte Earnings-Ergebnisse", "stock/earnings", symbol=SYMBOL, limit=4
            ),
            "news": provider.request(
                "Unternehmensnachrichten",
                "company-news",
                symbol=SYMBOL,
                **{"from": (today - timedelta(days=30)).isoformat(), "to": today.isoformat()},
            ),
        }

        profile = results["profile"].data if isinstance(results["profile"].data, dict) else {}
        quote = results["quote"].data if isinstance(results["quote"].data, dict) else {}
        metric_data = results["metrics"].data if isinstance(results["metrics"].data, dict) else {}
        metrics = metric_data.get("metric", {})
        if not isinstance(metrics, dict):
            metrics = {}
        revenue, revenue_unit, revenue_date = latest_reported_revenue(
            results["financials"].data
        )

        print("MICROSOFT / FINNHUB")
        print("\nVerfügbarkeit:")
        for result in results.values():
            report(result)

        print("\nUnternehmensprofil:")
        profile_fields = {
            "Name": profile.get("name"),
            "Symbol": profile.get("ticker") or SYMBOL,
            "Börse": profile.get("exchange"),
            "Währung": profile.get("currency"),
            "Land": profile.get("country"),
            "Branche": profile.get("finnhubIndustry"),
            "Marktkapitalisierung": profile.get("marketCapitalization"),
        }
        for label, value in profile_fields.items():
            print(f"{label + ':':25}{shown(value)}")

        print("\nKursdaten:")
        print(f"{'Aktueller Kurs:':25}{shown(quote.get('c'))}")
        print(f"{'Vorheriger Schlusskurs:':25}{shown(quote.get('pc'))}")
        print(f"{'Tageshoch:':25}{shown(quote.get('h'))}")
        print(f"{'Tagestief:':25}{shown(quote.get('l'))}")
        print(f"{'Zeitpunkt (UTC):':25}{utc_timestamp(quote.get('t'))}")

        print("\nFundamentaldaten:")
        print(
            f"{'Umsatz (letzter Bericht):':25}{shown(revenue)} {shown(revenue_unit)} "
            f"(Berichtsende: {shown(revenue_date)})"
        )
        fields = {
            "KGV": ("peBasicExclExtraTTM", "peTTM"),
            "EPS": ("epsBasicExclExtraItemsTTM", "epsTTM"),
            "Umsatz je Aktie TTM": ("revenuePerShareTTM",),
            "Umsatzwachstum": ("revenueGrowthTTMYoy", "revenueGrowth3Y"),
            "Gewinnwachstum": ("netIncomeGrowthTTMYoy", "epsGrowthTTMYoy"),
            "ROE": ("roeTTM", "roeRfy"),
            "Gesamtverschuldung/EK": (
                "totalDebt/totalEquityAnnual",
                "totalDebt/totalEquityQuarterly",
            ),
            "52-Wochen-Hoch": ("52WeekHigh",),
            "52-Wochen-Tief": ("52WeekLow",),
        }
        for label, keys in fields.items():
            print(f"{label + ':':25}{shown(first_value(metrics, *keys))}")

        recommendations = results["recommendations"].data
        if isinstance(recommendations, list) and recommendations:
            latest = recommendations[0]
            print("\nNeueste Analystenempfehlung:")
            for key in ("period", "strongBuy", "buy", "hold", "sell", "strongSell"):
                print(f"{key + ':':12}{shown(latest.get(key))}")

        targets = results["targets"].data
        if isinstance(targets, dict) and targets:
            print("\nKursziele:")
            for key in ("lastUpdated", "targetHigh", "targetLow", "targetMean", "targetMedian"):
                print(f"{key + ':':15}{shown(targets.get(key))}")

        calendar = results["calendar"].data
        if isinstance(calendar, dict) and calendar.get("earningsCalendar"):
            print(f"\nNächster Earnings-Termin: {calendar['earningsCalendar'][0]}")
        earnings = results["earnings"].data
        if isinstance(earnings, list) and earnings:
            print(f"Letztes Earnings-Ergebnis: {earnings[0]}")

        news = results["news"].data
        if isinstance(news, list) and news:
            print("\nAktuelle Microsoft-Nachrichten (maximal 10):")
            for item in news[:10]:
                print(
                    f"- {utc_timestamp(item.get('datetime'))} | {item.get('headline')} | "
                    f"{item.get('source')} | {item.get('url')}"
                )

        microsoft = {
            "Unternehmensprofil": short_status(results["profile"]),
            "Kurs": short_status(results["quote"]),
            "Fundamentaldaten": combined_status(results["metrics"], results["financials"]),
            "Analysten": short_status(results["recommendations"]),
            "Kursziele": short_status(results["targets"]),
            "Earnings": combined_status(results["calendar"], results["earnings"]),
            "News": short_status(results["news"]),
        }
        print("\nDIREKTER VERGLEICH")
        print(f"{'Bereich':24}{'Airbus AIR.PA':25}Microsoft MSFT")
        for area, airbus_status in AIRBUS_RESULTS.items():
            print(f"{area:24}{airbus_status:25}{microsoft[area]}")
        print(f"\nFinnhub-API-Aufrufe dieses MSFT-Tests: {provider.api_calls}")
        return 0
    except FinnhubError as exc:
        calls = provider.api_calls if provider is not None else 0
        print(f"Finnhub-Fehler: {exc}", file=sys.stderr)
        print(f"Finnhub-API-Aufrufe bis zum Fehler: {calls}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
