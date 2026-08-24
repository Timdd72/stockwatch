"""Tests für lokalen Katalogimport, Suche und Auswahl."""

from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from database import (
    SearchService,
    SecurityCatalog,
    Stock,
    StockWatchService,
    create_database,
    create_session_factory,
    import_securities_csv,
)


class SecurityCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "catalog_test.db"
        self.engine = create_database(self.database_path)
        self.session_factory = create_session_factory(self.engine)
        self.search_service = SearchService(self.session_factory)
        self.stock_service = StockWatchService(self.session_factory)
        with self.session_factory.begin() as session:
            session.add_all(
                [
                    SecurityCatalog(
                        name="Airbus SE", symbol="AIR.PA", isin="NL0000235190",
                        exchange="Euronext Paris", country="Netherlands", currency="EUR",
                        security_type="Common Stock", source="test",
                    ),
                    SecurityCatalog(
                        name="Alphabet Inc Class A", symbol="GOOGL", isin="US02079K3059",
                        exchange="NASDAQ", country="United States", currency="USD",
                        security_type="Common Stock", source="test",
                    ),
                    SecurityCatalog(
                        name="Alphabet Inc Class C", symbol="GOOG", isin="US02079K1079",
                        exchange="NASDAQ", country="United States", currency="USD",
                        security_type="Common Stock", source="test",
                    ),
                ]
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_exact_symbol_is_case_insensitive_and_ranked_first(self) -> None:
        results = self.search_service.search("goog")
        self.assertEqual([result.symbol for result in results], ["GOOG", "GOOGL"])

    def test_exact_isin(self) -> None:
        results = self.search_service.search("nl0000235190")
        self.assertEqual([result.symbol for result in results], ["AIR.PA"])

    def test_name_search_and_partial_match(self) -> None:
        self.assertEqual(
            [result.symbol for result in self.search_service.search("Alphabet")],
            ["GOOGL", "GOOG"],
        )
        self.assertEqual(
            [result.symbol for result in self.search_service.search("Class C")],
            ["GOOG"],
        )

    def test_no_match_and_single_character_fuzzy_search(self) -> None:
        self.assertEqual(self.search_service.search("Unbekannt"), [])
        self.assertEqual(self.search_service.search("A"), [])

    def test_selection_creates_stock_without_position_and_prevents_duplicate(self) -> None:
        security = self.search_service.search("GOOGL")[0]
        stock, created = self.stock_service.select_catalog_security(security.id)
        repeated, created_again = self.stock_service.select_catalog_security(security.id)

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(stock.id, repeated.id)
        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Stock)), 1)
            loaded = session.scalar(select(Stock).where(Stock.symbol == "GOOGL"))
            self.assertEqual(loaded.name, "Alphabet Inc Class A")  # type: ignore[union-attr]
            self.assertEqual(loaded.positions, [])  # type: ignore[union-attr]

    def test_csv_import_adds_reports_errors_and_updates(self) -> None:
        csv_path = Path(self.temp_dir.name) / "import.csv"
        header = (
            "name,symbol,isin,exchange,country,currency,security_type,source,"
            "provider_symbol_alpha_vantage,provider_symbol_finnhub\n"
        )
        csv_path.write_text(
            header
            + "Microsoft Corporation,MSFT,US5949181045,NASDAQ,US,USD,Common Stock,csv,,MSFT\n"
            + ",BROKEN,,,,,,csv,,\n",
            encoding="utf-8",
        )
        first = import_securities_csv(csv_path, self.session_factory)
        self.assertEqual((first.added, first.updated, len(first.errors)), (1, 0, 1))

        csv_path.write_text(
            header
            + "Microsoft Corp,MSFT,US5949181045,NASDAQ,United States,USD,Common Stock,csv,,MSFT\n",
            encoding="utf-8",
        )
        second = import_securities_csv(csv_path, self.session_factory)
        self.assertEqual((second.added, second.updated, second.errors), (0, 1, ()))
        self.assertEqual(self.search_service.search("MSFT")[0].name, "Microsoft Corp")

    def test_search_and_selection_cannot_open_network_socket(self) -> None:
        with patch.object(
            socket.socket, "connect", side_effect=AssertionError("Netzwerkzugriff")
        ):
            result = self.search_service.search("Airbus")[0]
            stock, created = self.stock_service.select_catalog_security(result.id)
        self.assertTrue(created)
        self.assertEqual(stock.symbol, "AIR.PA")


if __name__ == "__main__":
    unittest.main()
