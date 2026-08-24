"""Gemeinsamer Vertrag für API-Limits und Nutzungsprotokollierung."""

from __future__ import annotations

from typing import Protocol


class ApiLimitExceeded(RuntimeError):
    """Ein lokal konfiguriertes Providerlimit wurde erreicht."""


class ApiUsageRecorder(Protocol):
    def before_request(self, provider: str, endpoint: str, symbol: str | None) -> None: ...

    def record_request(
        self,
        provider: str,
        endpoint: str,
        symbol: str | None,
        success: bool,
        http_status: int | None,
    ) -> None: ...
