"""Importiert die offizielle NASDAQ-Datei in den lokalen Suchkatalog."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sqlalchemy import create_engine

from database import DEFAULT_DATABASE_PATH, create_database, create_session_factory
from database.nasdaq_import import (
    ACCEPTED_FINANCIAL_STATUS,
    EXCLUDED_FINANCIAL_STATUS,
    import_nasdaq_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        database_path = Path(args.database)
        if args.dry_run:
            session_factory = (
                create_session_factory(create_engine(f"sqlite:///{database_path}"))
                if database_path.exists()
                else None
            )
        else:
            session_factory = create_session_factory(create_database(database_path))
        result = import_nasdaq_file(
            args.input_file, session_factory, dry_run=args.dry_run
        )
    except (OSError, ValueError) as exc:
        print(f"NASDAQ-Importfehler: {exc}", file=sys.stderr)
        return 1

    print(f"Datensätze insgesamt:                         {result.total}")
    print(f"Reguläre Aktien:                              {result.regular_stocks}")
    print(f"ETFs ausgeschlossen:                          {result.excluded_etfs}")
    print(f"Test Issues ausgeschlossen:                   {result.excluded_test_issues}")
    print(
        "Rights/Warrants/Units ausgeschlossen:        "
        f"{result.excluded_rights_warrants_units}"
    )
    print(f"Sonstige Nicht-Aktien ausgeschlossen:         {result.excluded_other_non_stocks}")
    print(f"Problematische Financial-Status ausgeschlossen: {result.excluded_financial_status}")
    print(f"Fehlerhafte Datensätze:                       {result.invalid}")
    print(f"Neue Katalogeinträge:                         {result.new}")
    print(f"Bereits vorhandene Einträge:                  {result.existing}")
    print(f"Zu aktualisierende Einträge:                  {result.to_update}")

    print("\nAkzeptierter Financial Status:")
    for code, meaning in ACCEPTED_FINANCIAL_STATUS.items():
        print(f"- {code}: {meaning}")
    print("Ausgeschlossene Financial Status:")
    for code, meaning in EXCLUDED_FINANCIAL_STATUS.items():
        print(f"- {code}: {meaning}")

    print("\n20 Beispielaktien:")
    print("Name | Symbol | Market Category | Financial Status")
    for item in result.examples:
        print(
            f"{item.name} | {item.symbol} | {item.market_category} | "
            f"{item.financial_status}"
        )
    print("\nGezielt geprüfte Aktien:")
    for item in result.requested_examples:
        print(
            f"{item.name} | {item.symbol} | {item.market_category} | "
            f"{item.financial_status}"
        )
    if result.errors:
        print("\nFehlerdetails:")
        for error in result.errors:
            print(f"- {error}")
    print("\nModus: " + ("DRY-RUN – SQLite unverändert" if args.dry_run else "IMPORT"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
