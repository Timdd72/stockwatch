"""Echter Alpha-Vantage-Test für Airbus."""

from __future__ import annotations

import argparse
import sys
from typing import Any

import pandas as pd

from analysis import calculate_technical_indicators
from database import DEFAULT_DATABASE_PATH, StockWatchService
from market_data import AlphaVantageError, AlphaVantageProvider


def display(value: Any, suffix: str = "") -> str:
    if value in (None, "", "None", "-"):
        return "nicht verfügbar"
    return f"{value}{suffix}"


def metric(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "nicht verfügbar"
    return f"{value:.2f}{suffix}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--save", action="store_true", help="Analyse-Snapshot in SQLite speichern"
    )
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provider: AlphaVantageProvider | None = None
    try:
        provider = AlphaVantageProvider.from_env()
        result = provider.test_airbus()
        latest = result.history.iloc[-1]
        overview = result.overview
        company_name = overview.get("Name") or result.match.get("2. name")
        currency_value = overview.get("Currency") or result.match.get("8. currency")
        exchange = overview.get("Exchange") or result.match.get("4. region")
        currency = display(currency_value)

        print("Alpha Vantage – Airbus SE")
        print(f"Funktionierendes Symbol: {result.symbol}")
        print(f"Unternehmensname:        {display(company_name)}")
        print(f"Währung:                 {currency}")
        print(f"Börse/Handelsplatz:      {display(exchange)}")
        print(f"Letzter Kurs:            {latest['Close']:.2f} {currency}")
        print(f"Tageshoch:               {latest['High']:.2f} {currency}")
        print(f"Tagestief:               {latest['Low']:.2f} {currency}")
        print(f"Volumen:                 {int(latest['Volume']):,}")
        print(f"Marktkapitalisierung:    {display(overview.get('MarketCapitalization'))}")
        print(f"KGV:                     {display(overview.get('PERatio'))}")
        print(f"EPS:                     {display(overview.get('EPS'))}")
        print(f"52-Wochen-Hoch:          {display(overview.get('52WeekHigh'))}")
        print(f"52-Wochen-Tief:          {display(overview.get('52WeekLow'))}")

        recent = result.history.tail(5).copy()
        recent.index = recent.index.strftime("%Y-%m-%d")
        recent.index.name = "Datum"
        recent.columns = ["Eröffnung", "Hoch", "Tief", "Schluss", "Volumen"]
        print("\nHistorische Tageskurse (letzte 5 Handelstage):")
        with pd.option_context(
            "display.max_columns", None,
            "display.width", 120,
            "display.float_format", "{:.2f}".format,
        ):
            print(recent.to_string())

        technical = calculate_technical_indicators(result.history)
        print("\nLokale technische Kennzahlen:")
        print(f"Aktueller Schlusskurs:       {technical.current_close:.2f} {currency}")
        print(f"Veränderung zum Vortag:     {metric(technical.previous_day_change_pct, ' %')}")
        print(f"Performance 5 Handelstage:  {metric(technical.performance_5d_pct, ' %')}")
        print(f"Performance 20 Handelstage: {metric(technical.performance_20d_pct, ' %')}")
        print(f"Performance 60 Handelstage: {metric(technical.performance_60d_pct, ' %')}")
        print(f"SMA20:                       {metric(technical.sma20, ' ' + currency)}")
        print(f"SMA50:                       {metric(technical.sma50, ' ' + currency)}")
        print(f"SMA200:                      {metric(technical.sma200, ' ' + currency)}")
        print(f"RSI14:                       {metric(technical.rsi14)}")
        print(f"20-Tage-Volatilität:         {metric(technical.volatility_20d_pct, ' % p.a.')}")
        print(f"Abstand zum SMA20:           {metric(technical.distance_sma20_pct, ' %')}")
        print(f"Abstand zum SMA50:           {metric(technical.distance_sma50_pct, ' %')}")
        print(f"Trendbewertung:              {technical.trend}")
        print("Hinweis: Die Trendbewertung ist keine Kauf- oder Verkaufsempfehlung.")

        if args.save:
            service = StockWatchService.from_path(args.database)
            service.initialize_airbus()
            saved = service.save_analysis(result.symbol, technical)
            loaded = service.get_latest_analysis(result.symbol)
            if loaded is None or loaded.id != saved.id:
                raise RuntimeError("Der gespeicherte Snapshot konnte nicht gelesen werden.")
            print(f"\nAnalyse-Snapshot gespeichert und gelesen (ID {saved.id}).")
        print(f"\nAPI-Aufrufe: {result.api_calls}")
        return 0
    except (AlphaVantageError, LookupError, RuntimeError, ValueError) as exc:
        calls = provider.api_calls if provider is not None else 0
        print(f"Alpha-Vantage-Fehler: {exc}", file=sys.stderr)
        print(f"API-Aufrufe bis zum Fehler: {calls}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
