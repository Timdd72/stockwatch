"""Importiert eine CSV-Datei ohne Netzwerkzugriff in security_catalog."""

from __future__ import annotations

import argparse
import sys

from database import (
    DEFAULT_DATABASE_PATH,
    create_database,
    create_session_factory,
    import_securities_csv,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file")
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    args = parser.parse_args()
    try:
        session_factory = create_session_factory(create_database(args.database))
        result = import_securities_csv(args.csv_file, session_factory)
    except (OSError, ValueError) as exc:
        print(f"Importfehler: {exc}", file=sys.stderr)
        return 1

    print(f"Neu hinzugefügt: {result.added}")
    print(f"Aktualisiert:    {result.updated}")
    if result.errors:
        print("Fehlerhafte Zeilen:")
        for error in result.errors:
            print(f"- {error}")
    else:
        print("Fehlerhafte Zeilen: keine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
