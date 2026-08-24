"""Einmaliger, isolierter HTML-Verfügbarkeitstest für finanzen.net.

Keine Integration, keine Retries, keine Browserautomation und keine internen Endpunktaufrufe.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests
from bs4 import BeautifulSoup

URL = "https://www.finanzen.net/realtimekurs/airbus"
VENUES = ("Xetra", "Tradegate", "Stuttgart", "Paris")


@dataclass(frozen=True, slots=True)
class VenueQuote:
    venue: str
    price: str | None = None
    currency: str | None = None
    timestamp: str | None = None
    bid: str | None = None
    ask: str | None = None
    freshness: str | None = None
    source: str = "HTML"


def _clean(value: str) -> str:
    return " ".join(value.split())


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.I)
    return _clean(match.group(1)) if match else None


def _json_ld(soup: BeautifulSoup) -> list[Any]:
    values: list[Any] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            values.append(json.loads(node.get_text(strip=True)))
        except (json.JSONDecodeError, TypeError):
            continue
    return values


def _name(soup: BeautifulSoup, structured: list[Any]) -> str | None:
    for value in structured:
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("name"), str) and "Airbus" in row["name"]:
                return _clean(row["name"])
    heading = soup.find(["h1", "title"])
    return _clean(heading.get_text(" ", strip=True)) if heading else None


def _venue_quotes(soup: BeautifulSoup) -> list[VenueQuote]:
    quotes: list[VenueQuote] = []
    seen: set[str] = set()
    for venue in VENUES:
        candidates = soup.find_all(string=re.compile(rf"\b{re.escape(venue)}\b", re.I))
        for candidate in candidates:
            container = candidate.find_parent(["tr", "article", "section", "li", "div"])
            if container is None:
                continue
            text = _clean(container.get_text(" ", strip=True))
            if not any(label in text.lower() for label in ("kurs", "bid", "ask", "geld", "brief")):
                continue
            price = _first(r"(?:Kurs|Letzter|Last)\s*:?[\s€]*([0-9]+(?:[.,][0-9]+)?)", text)
            if price is None:
                price = _first(r"\b([0-9]{2,4}[.,][0-9]{2,4})\b", text)
            bid = _first(r"(?:Bid|Geld)\s*:?[\s€]*([0-9]+(?:[.,][0-9]+)?)", text)
            ask = _first(r"(?:Ask|Brief)\s*:?[\s€]*([0-9]+(?:[.,][0-9]+)?)", text)
            timestamp = _first(r"((?:[0-3]?\d\.[01]?\d\.(?:20)?\d{2}[, ]+)?[0-2]?\d:[0-5]\d(?::[0-5]\d)?)", text)
            freshness = "Realtime" if re.search(r"\brealtime\b", text, re.I) else (
                "Delayed" if re.search(r"\b(?:delayed|verzögert)\b", text, re.I) else None)
            key = f"{venue}|{price}|{timestamp}|{bid}|{ask}"
            if key not in seen:
                seen.add(key)
                quotes.append(VenueQuote(venue, price, "EUR" if "EUR" in text or "€" in text else None,
                                         timestamp, bid, ask, freshness))
            break
    return quotes


def main() -> int:
    requests_count = 1
    try:
        response = requests.get(URL, timeout=20)  # bewusst kein Retry und kein eigener User-Agent
    except requests.RequestException as exc:
        print("FINANZEN.NET / AIRBUS")
        print("HTTP-Status: nicht verfügbar")
        print(f"Requests: {requests_count}")
        print(f"Netzwerkfehler: {exc.__class__.__name__}")
        return 1

    print("FINANZEN.NET / AIRBUS")
    print(f"HTTP-Status: {response.status_code}")
    print(f"Requests: {requests_count}")
    print(f"Content-Type: {response.headers.get('content-type', 'nicht verfügbar')}")
    if response.status_code != 200:
        print("HTML-Auswertung: nicht möglich (Zugriff abgewiesen oder Seite nicht verfügbar)")
        return 0

    soup = BeautifulSoup(response.text, "html.parser")
    text = _clean(soup.get_text(" ", strip=True))
    structured = _json_ld(soup)
    isin = _first(r"\bISIN\s*:?[\s]*([A-Z]{2}[A-Z0-9]{10})\b", text)
    wkn = _first(r"\bWKN\s*:?[\s]*([A-Z0-9]{6})\b", text)
    quotes = _venue_quotes(soup)
    print(f"Name: {_name(soup, structured) or 'nicht verfügbar'}")
    print(f"ISIN: {isin or 'nicht verfügbar'}")
    print(f"WKN: {wkn or 'nicht verfügbar'}")
    print(f"ISIN-Zuordnung möglich: {'ja' if isin else 'nein'}")
    print(f"JSON-LD-Blöcke: {len(structured)}")
    print(f"JavaScript-Dateien: {len(soup.select('script[src]'))}")
    endpoint_hints = sorted(set(re.findall(r'''["']([^"']*(?:/ajax/|/api/|\.json(?:\?|$))[^"']*)["']''', response.text, re.I)))
    print("Öffentliche JSON-/Datenendpunkte im HTML: " + (", ".join(endpoint_hints[:5]) if endpoint_hints else "keine eindeutig dokumentierten"))
    print("Gefundene Handelsplätze: " + (", ".join(q.venue for q in quotes) if quotes else "keine zuverlässig extrahierbaren"))
    for quote in quotes:
        print(f"- {quote.venue} | Kurs {quote.price or 'nicht verfügbar'} | {quote.currency or 'Währung nicht verfügbar'} | "
              f"Zeit {quote.timestamp or 'nicht verfügbar'} | Bid {quote.bid or 'nicht verfügbar'} | "
              f"Ask {quote.ask or 'nicht verfügbar'} | {quote.freshness or 'Aktualität nicht eindeutig'} | Quelle {quote.source}")
    print("HTML-Tabellen: " + str(len(soup.find_all("table"))))
    print("Daten direkt im HTML: " + ("teilweise/ja" if quotes or isin or wkn else "nicht eindeutig"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
