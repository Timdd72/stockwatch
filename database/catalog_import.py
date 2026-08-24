"""CSV-Import für den lokalen Wertpapierkatalog."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import SecurityCatalog


CSV_FIELDS = (
    "name",
    "symbol",
    "isin",
    "exchange",
    "country",
    "currency",
    "security_type",
    "source",
    "provider_symbol_alpha_vantage",
    "provider_symbol_finnhub",
)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    added: int
    updated: int
    errors: tuple[str, ...]


def import_securities_csv(
    csv_path: str | Path, session_factory: sessionmaker[Session]
) -> ImportSummary:
    added = 0
    updated = 0
    errors: list[str] = []
    path = Path(csv_path)

    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        missing_headers = set(CSV_FIELDS) - set(reader.fieldnames or [])
        if missing_headers:
            missing = ", ".join(sorted(missing_headers))
            raise ValueError(f"CSV-Spalten fehlen: {missing}")

        with session_factory.begin() as session:
            for line_number, raw_row in enumerate(reader, start=2):
                try:
                    row = _normalize_row(raw_row)
                except ValueError as exc:
                    errors.append(f"Zeile {line_number}: {exc}")
                    continue

                existing = session.scalar(
                    select(SecurityCatalog).where(
                        SecurityCatalog.source == row["source"],
                        SecurityCatalog.symbol == row["symbol"],
                    )
                )
                if existing is None:
                    session.add(SecurityCatalog(**row))
                    added += 1
                else:
                    for field, value in row.items():
                        setattr(existing, field, value)
                    updated += 1
    return ImportSummary(added, updated, tuple(errors))


def _normalize_row(raw_row: dict[str, str | None]) -> dict[str, str | None]:
    row = {field: (raw_row.get(field) or "").strip() or None for field in CSV_FIELDS}
    for required in ("name", "symbol", "security_type", "source"):
        if row[required] is None:
            raise ValueError(f"Pflichtfeld '{required}' fehlt.")
    row["symbol"] = str(row["symbol"]).upper()
    row["isin"] = str(row["isin"]).upper() if row["isin"] else None
    row["currency"] = str(row["currency"]).upper() if row["currency"] else None
    for provider_field in (
        "provider_symbol_alpha_vantage",
        "provider_symbol_finnhub",
    ):
        if row[provider_field]:
            row[provider_field] = str(row[provider_field]).upper()
    return row
