from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, select

from database import NewsItem, Stock, create_database, create_session_factory
from database.market_data_store import MarketDataStore


class MarketDataStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_database(Path(self.temp.name) / "store.db")
        self.sessions = create_session_factory(self.engine)
        with self.sessions.begin() as session:
            stock = Stock(symbol="GOOG", name="Alphabet", currency="USD", exchange="NASDAQ")
            session.add(stock); session.flush(); self.stock_id = stock.id

    def tearDown(self) -> None:
        self.engine.dispose(); self.temp.cleanup()

    def test_news_duplicates_are_prevented(self) -> None:
        store = MarketDataStore(self.sessions)
        rows = [{"datetime": 1787000000, "headline": "Alphabet News", "source": "Test", "url": "https://example.test/1", "summary": "Kurz"}]
        self.assertEqual(store.save_news(self.stock_id, "finnhub", rows), 1)
        self.assertEqual(store.save_news(self.stock_id, "finnhub", rows), 0)
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(NewsItem)), 1)


if __name__ == "__main__":
    unittest.main()
