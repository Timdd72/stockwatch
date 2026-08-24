"""Unit-Tests für den speziellen Xetra-Importer."""

from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, select

from database import SecurityCatalog, create_database, create_session_factory
from database.xetra_import import import_xetra_csv


HEADERS = [
    "Product Status",
    "Instrument Status",
    "Instrument",
    "ISIN",
    "WKN",
    "Mnemonic",
    "MIC Code",
    "Instrument Type",
    "Unit of Quotation",
    "Primary Market MIC Code",
    "Settlement Currency",
    "Country Of Issue",
    "Currency",
]


def share(**overrides: str) -> dict[str, str]:
    row = {
        "Product Status": "Active",
        "Instrument Status": "Active",
        "Instrument": "Example AG",
        "ISIN": "DE0000000001",
        "WKN": "ABC123",
        "Mnemonic": "EXA",
        "MIC Code": "XETR",
        "Instrument Type": "Shares",
        "Unit of Quotation": "Shares",
        "Primary Market MIC Code": "XETR",
        "Settlement Currency": "EUR",
        "Country Of Issue": "DE",
        "Currency": "EUR",
    }
    row.update(overrides)
    return row


class XetraImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.temp_dir.name) / "xetra.csv"
        self.database_path = Path(self.temp_dir.name) / "test.db"
        self.engine = create_database(self.database_path)
        self.session_factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def write_csv(self, rows: list[dict[str, str]]) -> None:
        with self.csv_path.open("w", encoding="utf-8", newline="") as output:
            output.write("Market:;XETR\n")
            output.write("Date Last Update:;19.08.2026\n")
            writer = csv.DictWriter(output, fieldnames=HEADERS, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)

    def dry_run(self, rows: list[dict[str, str]]):
        self.write_csv(rows)
        return import_xetra_csv(self.csv_path, self.session_factory, dry_run=True)

    def test_reads_metadata_and_header_from_third_line_with_semicolon(self) -> None:
        result = self.dry_run([share()])
        self.assertEqual(result.file_date, "19.08.2026")
        self.assertEqual(result.total, 1)
        self.assertEqual(result.examples[0].symbol, "EXA")

    def test_shares_filter(self) -> None:
        result = self.dry_run([share(), share(**{"Instrument Type": "ETF", "Mnemonic": "ETF1"})])
        self.assertEqual(result.active_shares, 1)
        self.assertEqual(result.skipped_non_shares, 1)

    def test_official_common_stock_code_with_shares_unit(self) -> None:
        result = self.dry_run([share(**{"Instrument Type": "CS"})])
        self.assertEqual(result.active_shares, 1)
        self.assertEqual(result.skipped_non_shares, 0)

    def test_active_filters(self) -> None:
        result = self.dry_run(
            [
                share(**{"Product Status": "Inactive"}),
                share(**{"Instrument Status": "Inactive", "Mnemonic": "EXB"}),
            ]
        )
        self.assertEqual(result.active_shares, 0)
        self.assertEqual(result.skipped_inactive, 2)

    def test_currency_uses_currency_then_settlement_currency(self) -> None:
        result = self.dry_run(
            [
                share(**{"Currency": "USD"}),
                share(**{"Currency": "", "Mnemonic": "EXB", "ISIN": "DE0000000002"}),
            ]
        )
        self.assertEqual([item.currency for item in result.examples], ["USD", "EUR"])

    def test_duplicate_detection_prefers_isin_and_mic_and_protects_other_source(self) -> None:
        with self.session_factory.begin() as session:
            session.add(
                SecurityCatalog(
                    name="Manual Name",
                    symbol="MANUAL",
                    isin="DE0000000001",
                    exchange="XETR",
                    country="DE",
                    currency="EUR",
                    security_type="stock",
                    source="manual",
                )
            )
        result = self.dry_run([share()])
        self.assertEqual((result.new, result.existing, result.to_update), (0, 1, 0))
        with self.session_factory() as session:
            item = session.scalar(select(SecurityCatalog))
            self.assertEqual(item.name, "Manual Name")  # type: ignore[union-attr]

    def test_xetra_duplicate_is_reported_as_update(self) -> None:
        with self.session_factory.begin() as session:
            session.add(
                SecurityCatalog(
                    name="Old Name",
                    symbol="EXA",
                    isin="DE0000000001",
                    exchange="XETR",
                    country="DE",
                    currency="EUR",
                    security_type="stock",
                    source="XETRA",
                )
            )
        result = self.dry_run([share()])
        self.assertEqual((result.new, result.existing, result.to_update), (0, 0, 1))

    def test_dry_run_does_not_change_database(self) -> None:
        before = self.database_path.read_bytes()
        result = self.dry_run([share()])
        after = self.database_path.read_bytes()

        self.assertEqual(result.new, 1)
        self.assertEqual(before, after)
        with self.session_factory() as session:
            count = session.scalar(select(func.count()).select_from(SecurityCatalog))
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
