"""Alpha-Vantage-Implementierung der Marktdaten-Schnittstelle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

import pandas as pd
import requests

from .provider import MarketDataProvider, Quote
from .usage import ApiUsageRecorder


API_URL = "https://www.alphavantage.co/query"


class AlphaVantageError(RuntimeError):
    """Allgemeiner, bereits bereinigter Alpha-Vantage-Fehler."""


class AlphaVantageRateLimitError(AlphaVantageError):
    """Das API-Aufruflimit wurde erreicht."""


@dataclass(frozen=True, slots=True)
class AirbusResult:
    """Ergebnis des kompakten Airbus-Testablaufs."""

    symbol: str
    match: dict[str, str]
    history: pd.DataFrame
    overview: dict[str, Any]
    api_calls: int


def load_api_key(env_path: str | Path = ".env") -> str:
    """Lädt ALPHAVANTAGE_API_KEY, ohne weitere .env-Werte zu verändern."""

    path = Path(env_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AlphaVantageError(f"Die Konfigurationsdatei {path} ist nicht lesbar.") from exc

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == "ALPHAVANTAGE_API_KEY":
            key = value.strip().strip('"').strip("'")
            if key:
                return key
            break
    raise AlphaVantageError("ALPHAVANTAGE_API_KEY fehlt oder ist leer.")


class AlphaVantageProvider(MarketDataProvider):
    """Ruft Marktdaten über die Alpha-Vantage-HTTP-API ab."""

    def __init__(
        self,
        api_key: str,
        timeout: float = 20.0,
        min_request_interval: float = 2.0,
        usage_recorder: ApiUsageRecorder | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key darf nicht leer sein.")
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._min_request_interval = min_request_interval
        self._last_request_at: float | None = None
        self._session = requests.Session()
        self._usage_recorder = usage_recorder
        self.api_calls = 0

    @classmethod
    def from_env(
        cls,
        env_path: str | Path = ".env",
        usage_recorder: ApiUsageRecorder | None = None,
    ) -> "AlphaVantageProvider":
        return cls(load_api_key(env_path), usage_recorder=usage_recorder)

    def _request(self, **params: str) -> dict[str, Any]:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self._min_request_interval:
                time.sleep(self._min_request_interval - elapsed)
        request_params = {**params, "apikey": self._api_key}
        endpoint = params.get("function", "unknown")
        symbol = params.get("symbol")
        if self._usage_recorder is not None:
            self._usage_recorder.before_request("alpha_vantage", endpoint, symbol)
        self.api_calls += 1
        self._last_request_at = time.monotonic()
        http_status: int | None = None
        success = False
        try:
            response = self._session.get(
                API_URL, params=request_params, timeout=self._timeout
            )
            http_status = response.status_code
            if response.status_code != 200:
                raise AlphaVantageError(
                    f"Alpha Vantage antwortete mit HTTP {response.status_code}."
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise AlphaVantageError(
                    "Alpha Vantage lieferte keine gültige JSON-Antwort."
                ) from exc

            if not isinstance(data, dict):
                raise AlphaVantageError("Alpha Vantage lieferte ein ungültiges Datenformat.")
            if "Error Message" in data:
                raise AlphaVantageError(str(data["Error Message"]))
            if "Note" in data:
                raise AlphaVantageRateLimitError(str(data["Note"]))
            if "Information" in data:
                message = str(data["Information"])
                if "rate" in message.lower() or "limit" in message.lower():
                    raise AlphaVantageRateLimitError(message)
                raise AlphaVantageError(message)
            success = True
            return data
        except requests.RequestException as exc:
            # Die Exception kann die vollständige URL samt API-Key enthalten.
            raise AlphaVantageError(
                "Alpha Vantage ist derzeit nicht erreichbar."
            ) from exc
        finally:
            if self._usage_recorder is not None:
                self._usage_recorder.record_request(
                    "alpha_vantage", endpoint, symbol, success, http_status
                )

    def search_symbol(self, keywords: str) -> list[dict[str, str]]:
        if not keywords.strip():
            raise ValueError("keywords darf nicht leer sein.")
        data = self._request(function="SYMBOL_SEARCH", keywords=keywords.strip())
        matches = data.get("bestMatches")
        if not isinstance(matches, list):
            raise AlphaVantageError("Die Symbolsuche lieferte keine Trefferliste.")
        return [{str(k): str(v) for k, v in match.items()} for match in matches]

    def get_daily(self, symbol: str, *, outputsize: str = "compact") -> pd.DataFrame:
        symbol = self._validate_symbol(symbol)
        if outputsize not in ("compact", "full"):
            raise ValueError("outputsize muss 'compact' oder 'full' sein.")
        data = self._request(
            function="TIME_SERIES_DAILY", symbol=symbol, outputsize=outputsize
        )
        series = data.get("Time Series (Daily)")
        if not isinstance(series, dict) or not series:
            raise AlphaVantageError(f"Für {symbol} wurden keine Tageskurse geliefert.")

        history = pd.DataFrame.from_dict(series, orient="index").rename(
            columns={
                "1. open": "Open",
                "2. high": "High",
                "3. low": "Low",
                "4. close": "Close",
                "5. volume": "Volume",
            }
        )
        history = history[["Open", "High", "Low", "Close", "Volume"]]
        history.index = pd.to_datetime(history.index)
        history.index.name = "Date"
        for column in ("Open", "High", "Low", "Close"):
            history[column] = pd.to_numeric(history[column], errors="coerce")
        history["Volume"] = pd.to_numeric(history["Volume"], errors="coerce").astype(
            "Int64"
        )
        return history.sort_index()

    def get_overview(self, symbol: str) -> dict[str, Any]:
        symbol = self._validate_symbol(symbol)
        return self._request(function="OVERVIEW", symbol=symbol)

    def test_airbus(self) -> AirbusResult:
        matches = self.search_symbol("Airbus")
        match = self._choose_airbus_match(matches)
        symbol = match["1. symbol"].upper()
        history = self.get_daily(symbol)
        overview = self.get_overview(symbol)
        return AirbusResult(symbol, match, history, overview, self.api_calls)

    def get_quote(self, symbol: str) -> Quote:
        symbol = self._validate_symbol(symbol)
        history = self.get_daily(symbol)
        latest = history.iloc[-1]
        overview = self.get_overview(symbol)
        return Quote(
            symbol=symbol,
            price=float(latest["Close"]),
            currency=self._value(overview, "Currency"),
            name=self._value(overview, "Name"),
            exchange=self._value(overview, "Exchange"),
        )

    def get_history(self, symbol: str, days: int) -> pd.DataFrame:
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("days muss eine positive ganze Zahl sein.")
        return self.get_daily(symbol).tail(days)

    @staticmethod
    def _choose_airbus_match(matches: list[dict[str, str]]) -> dict[str, str]:
        candidates = [
            match
            for match in matches
            if "airbus" in match.get("2. name", "").lower()
        ]
        if not candidates:
            raise AlphaVantageError("Die Symbolsuche fand kein Airbus-Instrument.")

        # Bevorzugt wird die EUR-Notierung an der Pariser Heimatbörse.
        def rank(match: dict[str, str]) -> tuple[int, int]:
            symbol = match.get("1. symbol", "").upper()
            region = match.get("4. region", "").lower()
            return (int(symbol.endswith(".PAR")), int(region == "france"))

        selected = max(candidates, key=rank)
        if not selected.get("1. symbol"):
            raise AlphaVantageError("Der Airbus-Suchtreffer enthält kein Symbol.")
        return selected

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol darf nicht leer sein.")
        return symbol.strip().upper()

    @staticmethod
    def _value(data: dict[str, Any], key: str) -> str | None:
        value = data.get(key)
        if value in (None, "", "None", "-"):
            return None
        return str(value)
