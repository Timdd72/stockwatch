"""Importiert die offizielle Xetra-Datei in den lokalen Suchkatalog."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sqlalchemy import create_engine

from database import DEFAULT_DATABASE_PATH, create_database, create_session_factory
from database.xetra_import import import_xetra_csv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file")
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
        result = import_xetra_csv(
            args.csv_file, session_factory, dry_run=args.dry_run
        )
    except (OSError, ValueError) as exc:
        print(f"Xetra-Importfehler: {exc}", file=sys.stderr)
        return 1

    print(f"Datum der Xetra-Datei:              {result.file_date}")
    print(f"Datensätze insgesamt:               {result.total}")
    print(f"Aktive Aktien:                      {result.active_shares}")
    print(f"Übersprungene Nicht-Aktien:         {result.skipped_non_shares}")
    print(f"Übersprungene inaktive Instrumente: {result.skipped_inactive}")
    print(f"Fehlerhafte Datensätze:             {result.invalid}")
    print(f"Neue Katalogeinträge:               {result.new}")
    print(f"Bereits vorhandene Einträge:        {result.existing}")
    print(f"Zu aktualisierende Einträge:        {result.to_update}")
    print("\n20 Beispielaktien:")
    print("Name | Mnemonic | ISIN | Währung | MIC")
    for item in result.examples:
        print(
            f"{item.name} | {item.symbol} | {item.isin or '–'} | "
            f"{item.currency} | {item.exchange}"
        )
    if result.errors:
        print("\nFehlerdetails:")
        for error in result.errors:
            print(f"- {error}")
    print("\nModus: " + ("DRY-RUN – SQLite unverändert" if args.dry_run else "IMPORT"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
