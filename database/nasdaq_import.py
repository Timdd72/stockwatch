"""Parser und sicherer Import der offiziellen NASDAQ-Instrumentendatei."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import SecurityCatalog


EXPECTED_HEADERS = (
    "Symbol",
    "Security Name",
    "Market Category",
    "Test Issue",
    "Financial Status",
    "Round Lot Size",
    "ETF",
    "NextShares",
)
ACCEPTED_FINANCIAL_STATUS = {"N": "Normal"}
EXCLUDED_FINANCIAL_STATUS = {
    "D": "Deficient",
    "E": "Delinquent",
    "Q": "Bankrupt",
    "G": "Deficient and Bankrupt",
    "H": "Deficient and Delinquent",
    "J": "Delinquent and Bankrupt",
    "K": "Deficient, Delinquent and Bankrupt",
}

RIGHTS_WARRANTS_UNITS = re.compile(
    r"(?:\bright(?:s)?\b|\bwarrant(?:s)?\b|\bunit(?:s)?\b)", re.IGNORECASE
)
OTHER_NON_STOCK = re.compile(
    r"(?:\bETF\b|\bETN\b|\bnotes?\b|\bdebentures?\b|"
    r"preferred (?:stock|shares?|securities)|\bdepositary shares?\b|"
    r"\bdepository shares?\b|\bADSs?\b|\bADRs?\b|closed end fund)",
    re.IGNORECASE,
)
DIRECT_PROVIDER_SYMBOL = re.compile(r"^[A-Z][A-Z0-9]{0,5}$")


@dataclass(frozen=True, slots=True)
class NasdaqSecurity:
    name: str
    symbol: str
    market_category: str
    financial_status: str

    def catalog_values(self) -> dict[str, str | None]:
        provider_symbol = self.symbol if DIRECT_PROVIDER_SYMBOL.fullmatch(self.symbol) else None
        return {
            "name": self.name,
            "symbol": self.symbol,
            "isin": None,
            "exchange": "NASDAQ",
            "country": "US",
            "currency": "USD",
            "security_type": "stock",
            "source": "NASDAQ",
            "provider_symbol_alpha_vantage": provider_symbol,
            "provider_symbol_finnhub": provider_symbol,
        }


@dataclass(frozen=True, slots=True)
class NasdaqImportResult:
    total: int
    regular_stocks: int
    excluded_etfs: int
    excluded_test_issues: int
    excluded_rights_warrants_units: int
    excluded_other_non_stocks: int
    excluded_financial_status: int
    invalid: int
    new: int
    existing: int
    to_update: int
    errors: tuple[str, ...]
    examples: tuple[NasdaqSecurity, ...]
    requested_examples: tuple[NasdaqSecurity, ...]


def import_nasdaq_file(
    input_path: str | Path,
    session_factory: sessionmaker[Session] | None,
    *,
    dry_run: bool,
) -> NasdaqImportResult:
    records: list[NasdaqSecurity] = []
    errors: list[str] = []
    total = regular = etfs = tests = rwu = other = financial = invalid = 0

    with Path(input_path).open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file, delimiter="|")
        if tuple(reader.fieldnames or ()) != EXPECTED_HEADERS:
            raise ValueError("Die NASDAQ-Kopfzeile entspricht nicht dem erwarteten Format.")
        for line_number, row in enumerate(reader, start=2):
            symbol = (row.get("Symbol") or "").strip().upper()
            if symbol.startswith("FILE CREATION TIME:"):
                continue
            total += 1
            name = (row.get("Security Name") or "").strip()
            if not symbol or not name:
                invalid += 1
                errors.append(f"Zeile {line_number}: Symbol oder Security Name fehlt.")
                continue
            if (row.get("Test Issue") or "").strip().upper() != "N":
                tests += 1
                continue
            if (row.get("ETF") or "").strip().upper() != "N":
                etfs += 1
                continue
            status = (row.get("Financial Status") or "").strip().upper()
            if status not in ACCEPTED_FINANCIAL_STATUS:
                financial += 1
                continue
            if RIGHTS_WARRANTS_UNITS.search(name):
                rwu += 1
                continue
            if OTHER_NON_STOCK.search(name):
                other += 1
                continue

            regular += 1
            records.append(
                NasdaqSecurity(
                    name=name,
                    symbol=symbol,
                    market_category=(row.get("Market Category") or "").strip(),
                    financial_status=status,
                )
            )

    existing_entries = _load_existing_nasdaq(session_factory)
    existing_by_symbol = {entry.symbol.upper(): entry for entry in existing_entries}
    planned_symbols: set[str] = set()
    new_records: list[NasdaqSecurity] = []
    updates: list[tuple[SecurityCatalog, NasdaqSecurity]] = []
    existing_count = 0
    for record in records:
        entry = existing_by_symbol.get(record.symbol)
        if entry is not None:
            if _needs_update(entry, record):
                updates.append((entry, record))
            else:
                existing_count += 1
        elif record.symbol in planned_symbols:
            existing_count += 1
        else:
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

    requested = tuple(record for record in records if record.symbol in {"MSFT", "GOOGL", "GOOG"})
    return NasdaqImportResult(
        total=total,
        regular_stocks=regular,
        excluded_etfs=etfs,
        excluded_test_issues=tests,
        excluded_rights_warrants_units=rwu,
        excluded_other_non_stocks=other,
        excluded_financial_status=financial,
        invalid=invalid,
        new=len(new_records),
        existing=existing_count,
        to_update=len(updates),
        errors=tuple(errors),
        examples=tuple(records[:20]),
        requested_examples=requested,
    )


def _load_existing_nasdaq(
    session_factory: sessionmaker[Session] | None,
) -> list[SecurityCatalog]:
    if session_factory is None:
        return []
    with session_factory() as session:
        return list(
            session.scalars(
                select(SecurityCatalog).where(SecurityCatalog.source == "NASDAQ")
            )
        )


def _needs_update(entry: SecurityCatalog, record: NasdaqSecurity) -> bool:
    values = record.catalog_values()
    return any(getattr(entry, field) != value for field, value in values.items())
