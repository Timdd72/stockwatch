"""Parser und sicherer Import der offiziellen Xetra-Instrumentendatei."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import SecurityCatalog


REQUIRED_HEADERS = {
    "Product Status",
    "Instrument Status",
    "Instrument",
    "ISIN",
    "Mnemonic",
    "MIC Code",
    "Instrument Type",
    "Unit of Quotation",
    "Settlement Currency",
    "Currency",
}


@dataclass(frozen=True, slots=True)
class XetraSecurity:
    name: str
    symbol: str
    isin: str | None
    exchange: str
    country: str | None
    currency: str
    wkn: str | None
    primary_market_mic: str | None

    def catalog_values(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "symbol": self.symbol,
            "isin": self.isin,
            "exchange": self.exchange,
            "country": self.country,
            "currency": self.currency,
            "security_type": "stock",
            "source": "XETRA",
            "provider_symbol_alpha_vantage": None,
            "provider_symbol_finnhub": None,
        }


@dataclass(frozen=True, slots=True)
class XetraImportResult:
    file_date: str
    total: int
    active_shares: int
    skipped_non_shares: int
    skipped_inactive: int
    invalid: int
    new: int
    existing: int
    to_update: int
    errors: tuple[str, ...]
    examples: tuple[XetraSecurity, ...]


def import_xetra_csv(
    csv_path: str | Path,
    session_factory: sessionmaker[Session] | None,
    *,
    dry_run: bool,
) -> XetraImportResult:
    path = Path(csv_path)
    records: list[XetraSecurity] = []
    errors: list[str] = []
    total = active_shares = skipped_non_shares = skipped_inactive = invalid = 0

    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        raw_reader = csv.reader(csv_file, delimiter=";")
        market_row = next(raw_reader, None)
        date_row = next(raw_reader, None)
        header = next(raw_reader, None)
        if not market_row or market_row[0].strip() != "Market:":
            raise ValueError("Die Xetra-Metadatenzeile 'Market:' fehlt.")
        if not date_row or date_row[0].strip() != "Date Last Update:":
            raise ValueError("Die Xetra-Metadatenzeile 'Date Last Update:' fehlt.")
        if not header:
            raise ValueError("Die Kopfzeile in Zeile 3 fehlt.")
        missing = REQUIRED_HEADERS - set(header)
        if missing:
            raise ValueError(f"Xetra-Spalten fehlen: {', '.join(sorted(missing))}")

        file_date = date_row[1].strip() if len(date_row) > 1 else ""
        reader = csv.DictReader(csv_file, fieldnames=header, delimiter=";")
        for line_number, row in enumerate(reader, start=4):
            total += 1
            if (row.get("Product Status") or "").strip() != "Active" or (
                row.get("Instrument Status") or ""
            ).strip() != "Active":
                skipped_inactive += 1
                continue
            if not _is_share(row):
                skipped_non_shares += 1
                continue
            try:
                record = _parse_security(row)
            except ValueError as exc:
                invalid += 1
                errors.append(f"Zeile {line_number}: {exc}")
                continue
            active_shares += 1
            records.append(record)

    existing_entries = _load_existing(session_factory)
    existing_by_identity = {
        _catalog_identity(entry.isin, entry.symbol, entry.exchange): entry
        for entry in existing_entries
    }
    xetra_by_symbol = {
        entry.symbol: entry for entry in existing_entries if entry.source == "XETRA"
    }
    planned_identities: set[tuple[str, str, str]] = set()
    planned_symbols: set[str] = set()
    new_records: list[XetraSecurity] = []
    updates: list[tuple[SecurityCatalog, XetraSecurity]] = []
    existing_count = 0

    for record in records:
        identity = _catalog_identity(record.isin, record.symbol, record.exchange)
        entry = existing_by_identity.get(identity) or xetra_by_symbol.get(record.symbol)
        if entry is not None:
            if entry.source != "XETRA":
                existing_count += 1
            elif _needs_update(entry, record):
                updates.append((entry, record))
            else:
                existing_count += 1
            continue
        if identity in planned_identities or record.symbol in planned_symbols:
            existing_count += 1
            continue
        planned_identities.add(identity)
        planned_symbols.add(record.symbol)
        new_records.append(record)

    if not dry_run:
        if session_factory is None:
            raise ValueError("Für einen echten Import wird eine Datenbankverbindung benötigt.")
        with session_factory.begin() as session:
            for record in new_records:
                session.add(SecurityCatalog(**record.catalog_values()))
            for old_entry, record in updates:
                current = session.get(SecurityCatalog, old_entry.id)
                if current is not None:
                    for field, value in record.catalog_values().items():
                        setattr(current, field, value)

    return XetraImportResult(
        file_date=file_date,
        total=total,
        active_shares=active_shares,
        skipped_non_shares=skipped_non_shares,
        skipped_inactive=skipped_inactive,
        invalid=invalid,
        new=len(new_records),
        existing=existing_count,
        to_update=len(updates),
        errors=tuple(errors),
        examples=tuple(records[:20]),
    )


def _parse_security(row: dict[str, str | None]) -> XetraSecurity:
    name = (row.get("Instrument") or "").strip()
    symbol = (row.get("Mnemonic") or "").strip().upper()
    exchange = (row.get("MIC Code") or "").strip().upper()
    currency = (row.get("Currency") or "").strip().upper() or (
        row.get("Settlement Currency") or ""
    ).strip().upper()
    missing = [
        label
        for label, value in (
            ("Instrument", name),
            ("Mnemonic", symbol),
            ("MIC Code", exchange),
            ("Currency/Settlement Currency", currency),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Pflichtwerte fehlen: {', '.join(missing)}")
    return XetraSecurity(
        name=name,
        symbol=symbol,
        isin=(row.get("ISIN") or "").strip().upper() or None,
        exchange=exchange,
        country=(row.get("Country Of Issue") or "").strip() or None,
        currency=currency,
        wkn=(row.get("WKN") or "").strip().upper() or None,
        primary_market_mic=(row.get("Primary Market MIC Code") or "").strip().upper()
        or None,
    )


def _is_share(row: dict[str, str | None]) -> bool:
    instrument_type = (row.get("Instrument Type") or "").strip()
    unit = (row.get("Unit of Quotation") or "").strip()
    return instrument_type == "Shares" or (instrument_type == "CS" and unit == "Shares")


def _load_existing(
    session_factory: sessionmaker[Session] | None,
) -> list[SecurityCatalog]:
    if session_factory is None:
        return []
    with session_factory() as session:
        return list(session.scalars(select(SecurityCatalog)))


def _catalog_identity(
    isin: str | None, symbol: str, exchange: str | None
) -> tuple[str, str, str]:
    mic = (exchange or "").upper()
    if isin:
        return ("isin", isin.upper(), mic)
    return ("symbol", symbol.upper(), mic)


def _needs_update(entry: SecurityCatalog, record: XetraSecurity) -> bool:
    values = record.catalog_values()
    return any(getattr(entry, field) != value for field, value in values.items())
