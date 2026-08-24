"""Unit-Tests für den speziellen NASDAQ-Importer."""

from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, select

from database import SecurityCatalog, create_database, create_session_factory
from database.nasdaq_import import EXPECTED_HEADERS, import_nasdaq_file


def stock(**overrides: str) -> dict[str, str]:
    row = {
        "Symbol": "EXAM",
        "Security Name": "Example Corporation - Common Stock",
        "Market Category": "Q",
        "Test Issue": "N",
        "Financial Status": "N",
        "Round Lot Size": "100",
        "ETF": "N",
        "NextShares": "N",
    }
    row.update(overrides)
    return row


class NasdaqImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.input_path = Path(self.temp_dir.name) / "nasdaq.txt"
        self.database_path = Path(self.temp_dir.name) / "test.db"
        self.engine = create_database(self.database_path)
        self.session_factory = create_session_factory(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def write_file(self, rows: list[dict[str, str]]) -> None:
        with self.input_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=EXPECTED_HEADERS, delimiter="|")
            writer.writeheader()
            writer.writerows(rows)
            output.write("File Creation Time: 0819202615:41|||||||\n")

    def dry_run(self, rows: list[dict[str, str]]):
        self.write_file(rows)
        return import_nasdaq_file(
            self.input_path, self.session_factory, dry_run=True
        )

    def test_pipe_header_and_footer_metadata(self) -> None:
        result = self.dry_run([stock()])
        self.assertEqual(result.total, 1)
        self.assertEqual(result.regular_stocks, 1)
        self.assertEqual(result.examples[0].symbol, "EXAM")

    def test_etf_and_test_issue_filters(self) -> None:
        result = self.dry_run(
            [stock(**{"ETF": "Y"}), stock(**{"Symbol": "TEST", "Test Issue": "Y"})]
        )
        self.assertEqual(result.excluded_etfs, 1)
        self.assertEqual(result.excluded_test_issues, 1)

    def test_rights_warrants_units_and_other_non_stocks(self) -> None:
        rows = [
            stock(**{"Symbol": "R", "Security Name": "Example - Rights"}),
            stock(**{"Symbol": "W", "Security Name": "Example - Warrants"}),
            stock(**{"Symbol": "U", "Security Name": "Example - Units"}),
            stock(**{"Symbol": "P", "Security Name": "Example - Preferred Stock"}),
            stock(**{"Symbol": "ADS", "Security Name": "Example - American Depositary Shares"}),
        ]
        result = self.dry_run(rows)
        self.assertEqual(result.excluded_rights_warrants_units, 3)
        self.assertEqual(result.excluded_other_non_stocks, 2)

    def test_normal_class_shares_remain(self) -> None:
        rows = [
            stock(**{"Symbol": "CLA", "Security Name": "Example - Class A Ordinary Shares"}),
            stock(**{"Symbol": "CLB", "Security Name": "Example - Class B Common Stock"}),
            stock(**{"Symbol": "CLC", "Security Name": "Example - Class C Capital Stock"}),
        ]
        result = self.dry_run(rows)
        self.assertEqual(result.regular_stocks, 3)

    def test_only_normal_financial_status_is_accepted(self) -> None:
        rows = [stock(**{"Symbol": code, "Financial Status": code}) for code in "DEHQ"]
        result = self.dry_run(rows)
        self.assertEqual(result.regular_stocks, 0)
        self.assertEqual(result.excluded_financial_status, 4)

    def test_provider_symbols_only_for_simple_tickers(self) -> None:
        result = self.dry_run(
            [stock(**{"Symbol": "MSFT"}), stock(**{"Symbol": "ABC.A"})]
        )
        values = {item.symbol: item.catalog_values() for item in result.examples}
        self.assertEqual(values["MSFT"]["provider_symbol_finnhub"], "MSFT")
        self.assertIsNone(values["ABC.A"]["provider_symbol_alpha_vantage"])

    def test_duplicate_detection_uses_source_and_symbol_only(self) -> None:
        with self.session_factory.begin() as session:
            session.add_all(
                [
                    SecurityCatalog(
                        name="Old NASDAQ Name", symbol="MSFT", exchange="NASDAQ",
                        country="US", currency="USD", security_type="stock", source="NASDAQ",
                    ),
                    SecurityCatalog(
                        name="Protected Xetra", symbol="EXAM", exchange="XETR",
                        country="DE", currency="EUR", security_type="stock", source="XETRA",
                    ),
                ]
            )
        result = self.dry_run(
            [stock(**{"Symbol": "MSFT", "Security Name": "Microsoft Corporation - Common Stock"}), stock()]
        )
        self.assertEqual((result.new, result.existing, result.to_update), (1, 0, 1))
        with self.session_factory() as session:
            xetra = session.scalar(
                select(SecurityCatalog).where(SecurityCatalog.source == "XETRA")
            )
            self.assertEqual(xetra.name, "Protected Xetra")  # type: ignore[union-attr]

    def test_dry_run_does_not_change_database(self) -> None:
        before = self.database_path.read_bytes()
        result = self.dry_run([stock()])
        after = self.database_path.read_bytes()
        self.assertEqual(result.new, 1)
        self.assertEqual(before, after)
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(SecurityCatalog)), 0
            )


if __name__ == "__main__":
    unittest.main()
