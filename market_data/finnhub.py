"""Isolierter Finnhub-Testprovider ohne StockWatch-Integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import requests

from .usage import ApiUsageRecorder


FINNHUB_API_URL = "https://finnhub.io/api/v1"
Availability = Literal[
    "verfügbar", "nicht verfügbar", "leer", "Premium-Zugang erforderlich", "API-Fehler"
]


class FinnhubError(RuntimeError):
    """Bereinigter Finnhub-Fehler ohne geheime Request-Daten."""


@dataclass(frozen=True, slots=True)
class EndpointResult:
    area: str
    availability: Availability
    data: Any = None
    message: str | None = None


def load_finnhub_api_key(env_path: str | Path = ".env") -> str:
    path = Path(env_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FinnhubError(f"Die Konfigurationsdatei {path} ist nicht lesbar.") from exc

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == "FINNHUB_API_KEY":
            api_key = value.strip().strip('"').strip("'")
            if api_key:
                return api_key
            break
    raise FinnhubError("FINNHUB_API_KEY fehlt oder ist leer.")


class FinnhubProvider:
    """Kleiner HTTP-Client zum Prüfen verfügbarer Finnhub-Datenbereiche."""

    def __init__(
        self,
        api_key: str,
        timeout: float = 20.0,
        usage_recorder: ApiUsageRecorder | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key darf nicht leer sein.")
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._session = requests.Session()
        self._usage_recorder = usage_recorder
        self.api_calls = 0

    @classmethod
    def from_env(
        cls,
        env_path: str | Path = ".env",
        usage_recorder: ApiUsageRecorder | None = None,
    ) -> "FinnhubProvider":
        return cls(load_finnhub_api_key(env_path), usage_recorder=usage_recorder)

    def request(self, area: str, endpoint: str, **params: str | int) -> EndpointResult:
        symbol_value = params.get("symbol")
        symbol = str(symbol_value) if symbol_value is not None else None
        if self._usage_recorder is not None:
            self._usage_recorder.before_request("finnhub", endpoint, symbol)
        self.api_calls += 1
        http_status: int | None = None
        try:
            response = self._session.get(
                f"{FINNHUB_API_URL}/{endpoint}",
                params=params,
                headers={"X-Finnhub-Token": self._api_key},
                timeout=self._timeout,
            )
            http_status = response.status_code
        except requests.RequestException:
            # Keine Originalexception ausgeben: Sie könnte Requestdetails enthalten.
            result = EndpointResult(area, "API-Fehler", message="Finnhub ist nicht erreichbar.")
            self._record_usage(endpoint, symbol, result, http_status)
            return result

        try:
            payload = response.json()
        except ValueError:
            payload = None

        error_message = self._error_message(payload)
        if response.status_code in (401, 403) or self._requires_premium(error_message):
            result = EndpointResult(
                area,
                "Premium-Zugang erforderlich",
                message="Dieser Endpunkt ist mit dem vorhandenen Account nicht freigeschaltet.",
            )
            self._record_usage(endpoint, symbol, result, http_status)
            return result
        if response.status_code != 200:
            message = (
                "Finnhub-Limit erreicht (HTTP 429). Bitte später erneut versuchen."
                if response.status_code == 429
                else f"Finnhub antwortete mit HTTP {response.status_code}."
            )
            result = EndpointResult(
                area, "API-Fehler", message=message
            )
            self._record_usage(endpoint, symbol, result, http_status)
            return result
        if error_message:
            result = EndpointResult(area, "API-Fehler", message=error_message)
            self._record_usage(endpoint, symbol, result, http_status)
            return result
        if payload in (None, {}, []):
            result = EndpointResult(area, "leer", data=payload)
            self._record_usage(endpoint, symbol, result, http_status)
            return result
        result = EndpointResult(area, "verfügbar", data=payload)
        self._record_usage(endpoint, symbol, result, http_status)
        return result

    def _record_usage(
        self,
        endpoint: str,
        symbol: str | None,
        result: EndpointResult,
        http_status: int | None,
    ) -> None:
        if self._usage_recorder is not None:
            self._usage_recorder.record_request(
                "finnhub",
                endpoint,
                symbol,
                result.availability in ("verfügbar", "leer"),
                http_status,
            )

    def resolve_airbus_symbol(self) -> tuple[str, EndpointResult]:
        result = self.request("Symbolsuche", "search", q="NL0000235190")
        if result.availability != "verfügbar" or not isinstance(result.data, dict):
            raise FinnhubError(
                f"Airbus-Symbol konnte nicht ermittelt werden: {result.availability}."
            )
        matches = result.data.get("result", [])
        if not isinstance(matches, list) or not matches:
            raise FinnhubError("Die Finnhub-Suche fand keinen Treffer für die Airbus-ISIN.")

        candidates = [
            match
            for match in matches
            if isinstance(match, dict)
            and (
                "airbus" in str(match.get("description", "")).lower()
                or "NL0000235190" in str(match.get("description", "")).upper()
            )
        ]
        if not candidates:
            candidates = [match for match in matches if isinstance(match, dict)]
        if not candidates or not candidates[0].get("symbol"):
            raise FinnhubError("Der Airbus-Suchtreffer enthält kein Finnhub-Symbol.")
        return str(candidates[0]["symbol"]).upper(), result

    @staticmethod
    def _error_message(payload: Any) -> str | None:
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
        return None

    @staticmethod
    def _requires_premium(message: str | None) -> bool:
        if not message:
            return False
        normalized = message.lower()
        phrases = ("premium", "upgrade", "don't have access", "not have access")
        return any(phrase in normalized for phrase in phrases)
