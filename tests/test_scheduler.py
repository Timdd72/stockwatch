from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from sqlalchemy import select

from database import SchedulerRun, Stock, UpdateSchedule, create_database, create_session_factory
from market_data.usage import ApiLimitExceeded
from services import ProviderSettingsService, SchedulerService


class FakeUpdates:
    def __init__(self, settings=None, provider="finnhub", error=None):
        self.calls = []
        self.settings = settings
        self.provider = provider
        self.error = error

    def _run(self, data_type, stock_id):
        self.calls.append((data_type, stock_id))
        if self.error:
            raise self.error
        if self.settings:
            self.settings.record_success(stock_id, data_type, self.provider)

    def update_quote(self, stock_id): self._run("quote", stock_id)
    def update_history(self, stock_id): self._run("history", stock_id)
    def update_fundamentals(self, stock_id): self._run("fundamentals", stock_id)
    def update_news(self, stock_id): self._run("news", stock_id)
    def update_analyst(self, stock_id): self._run("analyst", stock_id)
    def update_earnings(self, stock_id): self._run("earnings", stock_id)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_database(Path(self.temp.name) / "scheduler.db")
        self.sessions = create_session_factory(self.engine)
        with self.sessions.begin() as session:
            eu = Stock(symbol="AIR.PAR", name="Airbus", currency="EUR", exchange="Paris")
            us = Stock(symbol="GOOG", name="Alphabet", currency="USD", exchange="NASDAQ")
            session.add_all((eu, us)); session.flush()
            self.eu_id, self.us_id = eu.id, us.id
        self.settings = ProviderSettingsService(self.sessions)
        self.settings.ensure_all_stocks()
        self.updates = FakeUpdates(self.settings)
        self.service = SchedulerService(self.sessions, self.updates)
        self.service.ensure_all_defaults()

    def tearDown(self):
        self.service.shutdown(); self.engine.dispose(); self.temp.cleanup()

    def _schedule(self, stock_id, data_type):
        with self.sessions() as session:
            return session.scalar(select(UpdateSchedule).where(
                UpdateSchedule.stock_id == stock_id, UpdateSchedule.data_type == data_type))

    def test_eu_and_us_defaults_use_real_timezones(self):
        eu_quote = self._schedule(self.eu_id, "quote")
        eu_history = self._schedule(self.eu_id, "history")
        us_quote = self._schedule(self.us_id, "quote")
        us_types = {x.data_type for x in self.service.ensure_defaults(self.us_id)}
        self.assertEqual(eu_quote.timezone, "Europe/Berlin")
        self.assertIn("9,13,17", eu_quote.cron_expression)
        self.assertIn("9,13,17", eu_history.cron_expression)
        self.assertEqual(us_quote.timezone, "America/New_York")
        self.assertEqual(us_types, {"quote", "history", "news", "fundamentals", "analyst", "earnings"})
        self.assertNotEqual(ZoneInfo("Europe/Berlin"), ZoneInfo("America/New_York"))

    def test_master_switch_and_weekend_skip(self):
        schedule = self._schedule(self.us_id, "quote")
        self.assertEqual(self.service.run_schedule(schedule.id), "skipped_disabled")
        self.service.set_master_enabled(True)
        saturday = datetime(2026, 8, 22, 12, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(self.service.run_schedule(schedule.id, saturday), "skipped_market_closed")
        self.assertEqual(self.updates.calls, [])

    def test_limit_blocks_update_and_is_recorded(self):
        limited = SchedulerService(self.sessions, FakeUpdates(error=ApiLimitExceeded("Limit")))
        limited.set_master_enabled(True)
        schedule = self._schedule(self.us_id, "quote")
        status = limited.run_schedule(schedule.id, datetime(2026, 8, 21, 12, tzinfo=ZoneInfo("America/New_York")))
        self.assertEqual(status, "skipped_limit")
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(SchedulerRun.status).order_by(SchedulerRun.id.desc())), "skipped_limit")

    def test_provider_is_obtained_from_sqlite_and_restart_reconstructs_jobs(self):
        self.settings.update_settings(self.us_id, {"quote": ("alpha_vantage", None, False)})
        self.updates.provider = "alpha_vantage"
        self.service.set_master_enabled(True)
        schedule = self._schedule(self.us_id, "quote")
        friday = datetime(2026, 8, 21, 12, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(self.service.run_schedule(schedule.id, friday), "success")
        with self.sessions() as session:
            run = session.scalar(select(SchedulerRun).order_by(SchedulerRun.id.desc()))
            self.assertEqual(run.provider_used, "alpha_vantage")
        restarted = SchedulerService(self.sessions, self.updates)
        restarted.reconstruct_jobs()
        self.assertEqual(len(restarted._scheduler.get_jobs()), 8)


if __name__ == "__main__": unittest.main()
