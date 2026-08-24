"""Tests der SQLite-Modelle, Repository- und Service-Schicht."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import inspect

from analysis import TechnicalIndicators
from database import (
    AnalysisSnapshot,
    StockWatchRepository,
    StockWatchService,
    create_database,
    create_session_factory,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test_stockwatch.db"
        self.engine = create_database(self.database_path)
        self.session_factory = create_session_factory(self.engine)
        self.service = StockWatchService(self.session_factory)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_schema_contains_expected_tables_and_nullable_indicators(self) -> None:
        inspector = inspect(self.engine)
        self.assertEqual(
            set(inspector.get_table_names()),
            {
                "stocks",
                "positions",
                "analysis_snapshots",
                "security_catalog",
                "api_usage",
                "provider_cache",
                "stock_provider_settings",
                "provider_capabilities",
                "company_data",
                "news_items",
                "analyst_recommendations",
                "earnings_results",
                "scheduler_settings",
                "update_schedules",
                "scheduler_runs",
                "ai_usage",
                "stock_ai_analysis",
                "ai_news_assessments",
                "scanner_runs",
                "scanner_candidates",
                "scanner_security_state",
                "scanner_provider_capabilities",
                "scanner_technical_runs",
                "scanner_candidate_technicals",
            "scanner_ai_assessments", "scanner_ai_news_assessments", "opportunity_news_events",
            "opportunity_watchlist", "opportunity_search_runs",
            "opportunity_candidate_summaries",
            "opportunity_watch_observations",
            "manual_action_runs",
            },
        )
        columns = {
            column["name"]: column for column in inspector.get_columns("analysis_snapshots")
        }
        nullable = {
            "change_1d_percent",
            "performance_5d",
            "performance_20d",
            "performance_60d",
            "sma20",
            "sma50",
            "sma200",
            "rsi14",
            "volatility_20d",
            "distance_sma20_percent",
            "distance_sma50_percent",
        }
        self.assertTrue(all(columns[name]["nullable"] for name in nullable))
        technical={column["name"]:column for column in inspector.get_columns("scanner_candidate_technicals")}
        self.assertTrue(all(technical[name]["nullable"] for name in (
            "price","trend","momentum_score","opportunity_score","sma20","rsi14")))

    def test_airbus_is_created_idempotently(self) -> None:
        first, _, _ = self.service.initialize_airbus()
        second, _, _ = self.service.initialize_airbus()

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.symbol, "AIR.PAR")
        self.assertEqual(first.name, "Airbus SE")
        with self.session_factory() as session:
            repository = StockWatchRepository(session)
            loaded = repository.get_stock_by_symbol("air.par")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.currency, "EUR")  # type: ignore[union-attr]
            self.assertEqual(loaded.exchange, "Paris")  # type: ignore[union-attr]

    def test_position_is_only_created_once_with_supplied_values(self) -> None:
        self.service.initialize_airbus()
        _, position, created = self.service.initialize_airbus(
            Decimal("123.45"), Decimal("2.5"), date(2026, 1, 2)
        )
        _, repeated, created_again = self.service.initialize_airbus(
            Decimal("999.99"), Decimal("8"), date(2026, 2, 3)
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertIsNotNone(position)
        self.assertEqual(position.id, repeated.id)  # type: ignore[union-attr]
        self.assertEqual(position.purchase_price, Decimal("123.4500"))  # type: ignore[union-attr]
        self.assertEqual(position.quantity, Decimal("2.500000"))  # type: ignore[union-attr]

    def test_snapshot_with_null_values_is_saved_and_read(self) -> None:
        stock, _, _ = self.service.initialize_airbus()
        with self.session_factory.begin() as session:
            repository = StockWatchRepository(session)
            snapshot = repository.add_analysis_snapshot(
                stock,
                timestamp=datetime(2026, 8, 19, tzinfo=timezone.utc),
                price=209.8,
                change_1d_percent=-2.08,
                performance_5d=None,
                performance_20d=None,
                performance_60d=None,
                sma20=None,
                sma50=None,
                sma200=None,
                rsi14=None,
                volatility_20d=None,
                distance_sma20_percent=None,
                distance_sma50_percent=None,
                trend="NEUTRAL",
            )
            snapshot_id = snapshot.id

        loaded = self.service.get_latest_analysis("AIR.PAR")
        self.assertIsInstance(loaded, AnalysisSnapshot)
        self.assertEqual(loaded.id, snapshot_id)  # type: ignore[union-attr]
        self.assertIsNone(loaded.sma200)  # type: ignore[union-attr]

    def test_service_maps_all_calculated_values_to_snapshot(self) -> None:
        self.service.initialize_airbus()
        indicators = TechnicalIndicators(
            current_close=209.8,
            previous_day_change_pct=-2.08,
            performance_5d_pct=-1.27,
            performance_20d_pct=7.81,
            performance_60d_pct=21.78,
            sma20=210.61,
            sma50=200.11,
            sma200=None,
            rsi14=53.53,
            volatility_20d_pct=34.61,
            distance_sma20_pct=-0.38,
            distance_sma50_pct=4.84,
            trend="NEUTRAL",
        )
        saved = self.service.save_analysis("AIR.PAR", indicators)
        loaded = self.service.get_latest_analysis("AIR.PAR")

        self.assertEqual(loaded.id, saved.id)  # type: ignore[union-attr]
        self.assertEqual(loaded.price, 209.8)  # type: ignore[union-attr]
        self.assertEqual(loaded.performance_60d, 21.78)  # type: ignore[union-attr]
        self.assertIsNone(loaded.sma200)  # type: ignore[union-attr]
        self.assertEqual(loaded.trend, "NEUTRAL")  # type: ignore[union-attr]
        self.assertEqual(loaded.price_type, "DAILY_CLOSE")  # type: ignore[union-attr]
        self.assertEqual(loaded.sma20, 210.61)  # type: ignore[union-attr]
        self.assertIsNotNone(loaded.fetched_at)  # type: ignore[union-attr]

    def test_manual_price_updates_position_without_replacing_technical_analysis(self) -> None:
        stock,_,_=self.service.initialize_airbus(Decimal("100"),Decimal("2"),date(2026,8,1))
        technical=self.service.save_analysis("AIR.PAR",TechnicalIndicators(200,1,2,3,4,190,180,None,50,20,5,10,"POSITIV"))
        manual=self.service.save_manual_price(stock.id,Decimal("206.10"),datetime(2030,8,20,15,5,tzinfo=timezone.utc))
        dashboard=self.service.get_stock_dashboard(stock.id)
        self.assertEqual((manual.price_type,manual.provider,manual.manual_entered_by),("MANUAL","manual","user"))
        self.assertEqual(dashboard.snapshot.id,manual.id);self.assertEqual(dashboard.technical_snapshot.id,technical.id)
        self.assertEqual(dashboard.position.current_value,Decimal("412.20"))
        self.assertEqual(dashboard.position.profit_loss,Decimal("212.20"))


if __name__ == "__main__":
    unittest.main()
