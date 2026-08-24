"""Initialisiert die StockWatch-Datenbank und legt Airbus an."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
import sys

from database import DEFAULT_DATABASE_PATH, StockWatchService


def positive_decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Muss eine gültige Dezimalzahl sein.") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("Muss größer als null sein.")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--purchase-price", type=positive_decimal)
    parser.add_argument("--quantity", type=positive_decimal)
    parser.add_argument("--purchase-date", type=date.fromisoformat)
    args = parser.parse_args()
    if (args.purchase_price is None) != (args.quantity is None):
        parser.error("--purchase-price und --quantity müssen gemeinsam angegeben werden.")
    if args.purchase_date is not None and args.purchase_price is None:
        parser.error("--purchase-date erfordert --purchase-price und --quantity.")
    return args


def main() -> int:
    args = parse_args()
    try:
        service = StockWatchService.from_path(args.database)
        stock, position, created = service.initialize_airbus(
            args.purchase_price, args.quantity, args.purchase_date
        )
        print(f"Datenbank initialisiert: {args.database}")
        print(f"Aktie: {stock.symbol} – {stock.name}")
        if position is None:
            print("Keine Position angelegt (Kaufpreis und Stückzahl nicht angegeben).")
        elif created:
            print(f"Position angelegt (ID {position.id}).")
        else:
            print(f"Bereits vorhandene Position beibehalten (ID {position.id}).")
        return 0
    except (LookupError, ValueError) as exc:
        print(f"Datenbankfehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
