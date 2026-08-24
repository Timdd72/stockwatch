"""Isolierter Vergleichstest von yahoo_fin für Airbus und Microsoft."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import date, timedelta
import io
import sys
from typing import Any
from unittest.mock import patch

import pandas as pd
import requests

# yahoo_fin meldet optionale HTML-Funktionen beim Import auf stdout; für den
# hier verwendeten Chart-Abruf sind sie nicht erforderlich.
with redirect_stdout(io.StringIO()):
    from yahoo_fin import stock_info


SYMBOLS = {"Airbus AIR.PA": "AIR.PA", "Microsoft MSFT": "MSFT"}
YAHOO_CHART_ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class YahooFinHTTPError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Yahoo antwortete mit HTTP {status_code}.")


@dataclass(frozen=True, slots=True)
class TestResult:
    symbol: str
    connection: str
    current_price: float | None
    history: pd.DataFrame | None
    http_429: bool
    error: str | None = None


def fetch_symbol(symbol: str) -> TestResult:
    """Führt genau einen yahoo_fin-Chart-Abruf ohne eigene HTTP-Header aus."""

    original_get = requests.api.get

    def checked_get(*args: Any, **kwargs: Any) -> requests.Response:
        response = original_get(*args, **kwargs)
        if response.status_code == 429:
            raise YahooFinHTTPError(429)
        if not response.ok:
            raise YahooFinHTTPError(response.status_code)
        return response

    try:
        # Ein größerer Kalenderzeitraum stellt trotz Wochenenden fünf Handelstage bereit.
        with patch.object(stock_info.requests, "get", checked_get):
            history = stock_info.get_data(
                symbol,
                start_date=date.today() - timedelta(days=14),
                end_date=date.today() + timedelta(days=1),
                interval="1d",
                headers={},  # Den eingebauten Browser-User-Agent bewusst deaktivieren.
            )
        history = history.dropna(subset=["close"]).tail(5)
        if history.empty:
            return TestResult(
                symbol, "verbunden, aber leer", None, history, False, "Keine Kurse geliefert."
            )
        return TestResult(
            symbol=symbol,
            connection="erfolgreich",
            current_price=float(history["close"].iloc[-1]),
            history=history,
            http_429=False,
        )
    except YahooFinHTTPError as exc:
        return TestResult(
            symbol,
            f"HTTP {exc.status_code}",
            None,
            None,
            exc.status_code == 429,
            str(exc),
        )
    except requests.RequestException:
        return TestResult(
            symbol, "Netzwerkfehler", None, None, False, "Yahoo ist nicht erreichbar."
        )
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        return TestResult(
            symbol,
            "Antwortfehler",
            None,
            None,
            False,
            f"Yahoo-Antwort konnte nicht verarbeitet werden: {type(exc).__name__}.",
        )


def availability(value: object) -> str:
    return "verfügbar" if value is not None else "nicht verfügbar"


def main() -> int:
    results = {name: fetch_symbol(symbol) for name, symbol in SYMBOLS.items()}

    for name, result in results.items():
        print(f"\n{name}")
        print(f"Verbindung:       {result.connection}")
        print(
            "Aktueller Kurs:   "
            + (f"{result.current_price:.2f}" if result.current_price is not None else "nicht verfügbar")
        )
        print(f"HTTP 429:         {'ja' if result.http_429 else 'nein'}")
        if result.error:
            print(f"Fehler:           {result.error}")
        if result.history is not None and not result.history.empty:
            print("Historie der letzten 5 Handelstage:")
            columns = ["open", "high", "low", "close", "volume"]
            print(result.history[columns].to_string(float_format=lambda number: f"{number:.2f}"))

    print("\nVERGLEICH")
    print(f"{'Bereich':22}{'Airbus AIR.PA':20}Microsoft MSFT")
    airbus = results["Airbus AIR.PA"]
    microsoft = results["Microsoft MSFT"]
    print(f"{'Verbindung':22}{airbus.connection:20}{microsoft.connection}")
    print(
        f"{'Aktueller Kurs':22}{availability(airbus.current_price):20}"
        f"{availability(microsoft.current_price)}"
    )
    print(
        f"{'5-Tage-Historie':22}{availability(airbus.history):20}"
        f"{availability(microsoft.history)}"
    )
    print(f"\nVerwendeter Endpunkt: {YAHOO_CHART_ENDPOINT}")
    print("HTTP-Aufrufe: 2 (je Symbol genau einer)")
    print(f"HTTP 429 aufgetreten: {'ja' if any(r.http_429 for r in results.values()) else 'nein'}")
    return 0 if all(result.current_price is not None for result in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
