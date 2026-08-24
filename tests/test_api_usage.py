"""Tests für zentrale API-Protokollierung und lokale Limits."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from sqlalchemy import select

from database import ApiLimits, ApiUsage, ApiUsageService, create_database, create_session_factory
from market_data.alpha_vantage import AlphaVantageError, AlphaVantageProvider
from market_data.finnhub import FinnhubProvider
from market_data.usage import ApiLimitExceeded


class ApiUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_database(Path(self.temp_dir.name) / "usage.db")
        self.session_factory = create_session_factory(self.engine)
        self.now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        self.usage = ApiUsageService(
            self.session_factory,
            ApiLimits(alpha_vantage_daily=25, finnhub_per_minute=60),
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_successful_and_failed_alpha_calls_are_counted(self) -> None:
        provider = AlphaVantageProvider(
            "secret", min_request_interval=0, usage_recorder=self.usage
        )
        success = Mock(status_code=200)
        success.json.return_value = {"value": 1}
        failure = Mock(status_code=500)
        provider._session.get = Mock(side_effect=[success, failure])

        self.assertEqual(provider._request(function="TEST", symbol="MSFT"), {"value": 1})
        with self.assertRaises(AlphaVantageError):
            provider._request(function="TEST", symbol="MSFT")

        with self.session_factory() as session:
            entries = list(session.scalars(select(ApiUsage).order_by(ApiUsage.id)))
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0].success)
        self.assertFalse(entries[1].success)
        self.assertEqual(entries[1].http_status, 500)

    def test_alpha_daily_limit_blocks_request(self) -> None:
        limited_usage = ApiUsageService(
            self.session_factory,
            ApiLimits(alpha_vantage_daily=1, finnhub_per_minute=60),
            clock=lambda: self.now,
        )
        limited_usage.record_request("alpha_vantage", "TEST", "MSFT", True, 200)
        provider = AlphaVantageProvider(
            "secret", min_request_interval=0, usage_recorder=limited_usage
        )
        provider._session.get = Mock()

        with self.assertRaises(ApiLimitExceeded):
            provider._request(function="TEST", symbol="MSFT")
        provider._session.get.assert_not_called()
        self.assertEqual(limited_usage.status().alpha_today, 1)

    def test_finnhub_http_429_is_logged_without_retry(self) -> None:
        provider = FinnhubProvider("secret", usage_recorder=self.usage)
        response = Mock(status_code=429)
        response.json.return_value = {"error": "API limit reached"}
        provider._session.get = Mock(return_value=response)

        result = provider.request("Kurs", "quote", symbol="MSFT")

        self.assertEqual(result.availability, "API-Fehler")
        self.assertIn("HTTP 429", result.message or "")
        provider._session.get.assert_called_once()
        with self.session_factory() as session:
            entry = session.scalar(select(ApiUsage))
        self.assertFalse(entry.success)  # type: ignore[union-attr]
        self.assertEqual(entry.http_status, 429)  # type: ignore[union-attr]
        self.assertEqual(entry.data_type, "quote")  # type: ignore[union-attr]
        self.assertEqual(entry.error_type, "rate_limit")  # type: ignore[union-attr]

    def test_finnhub_minute_limit_blocks_request(self) -> None:
        limited = ApiUsageService(
            self.session_factory,
            ApiLimits(alpha_vantage_daily=25, finnhub_per_minute=1),
            clock=lambda: self.now,
        )
        limited.record_request("finnhub", "quote", "GOOG", True, 200)
        provider = FinnhubProvider("secret", usage_recorder=limited)
        provider._session.get = Mock()
        with self.assertRaises(ApiLimitExceeded):
            provider.request("Kurs", "quote", symbol="GOOG")
        provider._session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
