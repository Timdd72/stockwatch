from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from analysis import TechnicalIndicators
from database import CompanyData, SecurityCatalog, Stock, StockWatchService, create_database, create_session_factory
from database.repository import StockWatchRepository
from market_data.usage import ApiLimitExceeded
from services import ProviderSettingsService, StockUpdateService


class FakeUpdates:
    def __init__(self, settings, errors=None):
        self.settings = settings
        self.errors = errors or {}
        self.calls = []

    def _call(self, data_type, stock_id):
        self.calls.append(data_type)
        if data_type in self.errors:
            raise self.errors[data_type]
        self.settings.record_success(stock_id, data_type, "finnhub")

    def update_quote(self, stock_id): self._call("quote", stock_id)
    def update_history(self, stock_id): self._call("history", stock_id)
    def update_fundamentals(self, stock_id): self._call("fundamentals", stock_id)
    def update_news(self, stock_id): self._call("news", stock_id)
    def update_analyst(self, stock_id): self._call("analyst", stock_id)
    def update_earnings(self, stock_id): self._call("earnings", stock_id)


class StockUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_database(Path(self.temp.name) / "update.db")
        self.sessions = create_session_factory(self.engine)
        with self.sessions.begin() as session:
            stock = Stock(symbol="GOOG", name="Alphabet", currency="USD", exchange="NASDAQ")
            session.add(stock); session.flush(); self.stock_id = stock.id
            session.add(SecurityCatalog(name="Alphabet", symbol="GOOG", exchange="NASDAQ", currency="USD", security_type="stock", source="test", provider_symbol_alpha_vantage="GOOG", provider_symbol_finnhub="GOOG"))
        self.settings = ProviderSettingsService(self.sessions)
        self.settings.ensure_defaults(self.stock_id)
        self.stockwatch = StockWatchService(self.sessions)

    def tearDown(self):
        self.engine.dispose(); self.temp.cleanup()

    def _technical(self, timestamp=None):
        values = TechnicalIndicators(100, 1, 2, 3, 4, 95, 90, None, 55, 20, 5, 10, "POSITIV")
        if timestamp is None:
            return self.stockwatch.save_analysis("GOOG", values)
        with self.sessions.begin() as session:
            stock = session.get(Stock, self.stock_id)
            return StockWatchRepository(session).add_analysis_snapshot(
                stock, timestamp=timestamp, price=100, change_1d_percent=1,
                performance_5d=2, performance_20d=3, performance_60d=4,
                sma20=95, sma50=90, sma200=None, rsi14=55,
                volatility_20d=20, distance_sma20_percent=5,
                distance_sma50_percent=10, trend="POSITIV")

    def test_quote_snapshot_does_not_hide_previous_technical_values(self):
        technical = self._technical()
        self.stockwatch.save_analysis("GOOG", TechnicalIndicators(111, None, None, None, None, None, None, None, None, None, None, None, "NEUTRAL"))
        dashboard = self.stockwatch.get_stock_dashboard(self.stock_id)
        self.assertEqual(dashboard.snapshot.price, 111)
        self.assertEqual(dashboard.technical_snapshot.id, technical.id)
        self.assertEqual(dashboard.technical_snapshot.sma20, 95)

    def test_current_data_is_not_reloaded_but_quote_always_is(self):
        self._technical()
        for data_type in ("history", "fundamentals", "news", "analyst", "earnings"):
            self.settings.record_success(self.stock_id, data_type, "finnhub")
        with self.sessions.begin() as session:
            session.add(CompanyData(stock_id=self.stock_id, provider="finnhub", updated_at=datetime.now(timezone.utc)))
        fake = FakeUpdates(self.settings)
        report = StockUpdateService(self.sessions, fake).update_stock(self.stock_id)
        self.assertEqual(fake.calls, ["quote"])
        self.assertEqual({x.data_type: x.status for x in report.items}["history"], "current")

    def test_missing_technical_data_triggers_history_and_stale_areas(self):
        fake = FakeUpdates(self.settings)
        report = StockUpdateService(self.sessions, fake).update_stock(self.stock_id)
        self.assertEqual(fake.calls, ["quote", "history", "fundamentals", "news", "analyst", "earnings"])
        self.assertTrue(all(x.status == "updated" for x in report.items))

    def test_limit_in_one_area_does_not_stop_other_updates(self):
        fake = FakeUpdates(self.settings, {"quote": ApiLimitExceeded("Limit")})
        report = StockUpdateService(self.sessions, fake).update_stock(self.stock_id)
        statuses = {x.data_type: x.status for x in report.items}
        self.assertEqual(statuses["quote"], "limit")
        self.assertIn("history", fake.calls)
        self.assertEqual(statuses["history"], "updated")


if __name__ == "__main__": unittest.main()
