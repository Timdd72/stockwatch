"""Gemeinsamer Vertrag für Marktdatenanbieter."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Quote:
    """Ein normalisiertes aktuelles Kursbild eines Symbols."""

    symbol: str
    price: float
    currency: str | None = None
    name: str | None = None
    exchange: str | None = None


class MarketDataProvider(ABC):
    """Abstrakte Schnittstelle für austauschbare Marktdatenquellen."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Liefert den zuletzt verfügbaren Kurs für ``symbol``."""

    @abstractmethod
    def get_history(self, symbol: str, days: int) -> Any:
        """Liefert die Tageshistorie der letzten ``days`` Kalendertage.

        Das konkrete Tabellenformat wird vom Provider bestimmt. Provider, die
        pandas einsetzen, geben beispielsweise einen ``pandas.DataFrame`` zurück.
        """
