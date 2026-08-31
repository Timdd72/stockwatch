"""Tests für persistierte Providerwahl, Capabilities und Fallbacks."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd
from sqlalchemy import select

from database import (
    AnalysisSnapshot,
    ApiUsageService,
    SecurityCatalog,
    Stock,
    StockProviderSymbol,
    create_database,
    create_session_factory,
)
from market_data.finnhub import EndpointResult
from market_data.usage import ApiLimitExceeded
from services import MarketUpdateError, MarketUpdateService, ProviderSettingsService


class FakeFinnhub:
    def __init__(self, result: EndpointResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeAlpha:
    def __init__(self) -> None:
        self.calls = 0
        self.symbols = []

    def get_daily(self, symbol: str) -> pd.DataFrame:
        self.calls += 1
        self.symbols.append(symbol)
        return pd.DataFrame({"Close": [100.0, 101.0]}, index=pd.to_datetime(["2026-08-19","2026-08-20"]))


class ProviderSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_database(Path(self.temp_dir.name) / "providers.db")
        self.sessions = create_session_factory(self.engine)
        with self.sessions.begin() as session:
            eu = Stock(symbol="AIR.PAR", name="Airbus SE", currency="EUR", exchange="Paris")
            us = Stock(symbol="GOOG", name="Alphabet", currency="USD", exchange="NASDAQ")
            session.add_all((eu, us))
            session.flush()
            self.eu_id, self.us_id = eu.id, us.id
            session.add_all(
                (
                    SecurityCatalog(name="Airbus SE", symbol="AIR", exchange="XPAR", currency="EUR", security_type="stock", source="test", provider_symbol_alpha_vantage="AIR.PAR", provider_symbol_finnhub="AIR.PA"),
                    SecurityCatalog(name="Alphabet", symbol="GOOG", exchange="NASDAQ", currency="USD", security_type="stock", source="test", provider_symbol_alpha_vantage="GOOG", provider_symbol_finnhub="GOOG"),
                )
            )
        self.settings = ProviderSettingsService(self.sessions)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_default_configuration_for_us_and_eu(self) -> None:
        us = {item.data_type: item for item in self.settings.ensure_defaults(self.us_id)}
        eu = {item.data_type: item for item in self.settings.ensure_defaults(self.eu_id)}
        self.assertEqual((us["fundamentals"].primary_provider, us["fundamentals"].fallback_provider), ("finnhub", "alpha_vantage"))
        self.assertEqual((eu["history"].primary_provider, eu["history"].fallback_provider), ("finnhub", "alpha_vantage"))
        self.assertIsNone(eu["news"].fallback_provider)

    def test_manual_selection_and_capability_status_are_persisted(self) -> None:
        self.settings.ensure_defaults(self.us_id)
        self.settings.update_settings(self.us_id, {"quote": ("alpha_vantage", "finnhub", True)})
        quote = self.settings.get_setting(self.us_id, "quote")
        self.assertEqual((quote.primary_provider, quote.fallback_provider, quote.auto_select), ("alpha_vantage", "finnhub", True))
        self.settings.save_capability(self.us_id, "finnhub", "quote", "premium_required")
        capability = self.settings.capabilities(self.us_id)[0]
        self.assertEqual(capability.status, "premium_required")

    def _updates(self, finnhub: FakeFinnhub, alpha: FakeAlpha) -> MarketUpdateService:
        return MarketUpdateService(
            self.sessions,
            ApiUsageService(self.sessions),
            alpha_factory=lambda: alpha,
            finnhub_factory=lambda: finnhub,
        )

    def test_finnhub_success_does_not_call_alpha(self) -> None:
        finnhub = FakeFinnhub(EndpointResult("Kurs", "verfügbar", {"c": 123.0,"t":1787236200}))
        alpha = FakeAlpha()
        self._updates(finnhub, alpha).update_quote(self.us_id)
        self.assertEqual((finnhub.calls, alpha.calls), (1, 0))
        with self.sessions() as session:
            row=session.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.stock_id==self.us_id))
            self.assertEqual((row.price_type,row.provider),("REALTIME","finnhub"))
            self.assertNotEqual(row.market_timestamp,row.fetched_at)

    def test_alpha_daily_preserves_market_date_and_is_not_realtime(self) -> None:
        self.settings.ensure_defaults(self.eu_id)
        self.settings.update_settings(self.eu_id,{"quote":("alpha_vantage",None,False)})
        self._updates(FakeFinnhub(EndpointResult("Kurs","leer",{})),FakeAlpha()).update_quote(self.eu_id)
        with self.sessions() as session:
            row=session.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.stock_id==self.eu_id))
            self.assertEqual(row.price_type,"DAILY_CLOSE");self.assertEqual(row.provider,"alpha_vantage")
            self.assertEqual(row.market_timestamp.date().isoformat(),"2026-08-20")
            self.assertGreater(row.fetched_at,row.market_timestamp)

    def test_premium_or_provider_error_uses_fallback(self) -> None:
        for availability in ("Premium-Zugang erforderlich", "API-Fehler"):
            with self.subTest(availability=availability):
                finnhub = FakeFinnhub(EndpointResult("Kurs", availability, message="nicht verfügbar"))
                alpha = FakeAlpha()
                self._updates(finnhub, alpha).update_quote(self.us_id)
                self.assertEqual((finnhub.calls, alpha.calls), (1, 1))

    def test_api_limit_does_not_send_fallback_request(self) -> None:
        finnhub = FakeFinnhub(ApiLimitExceeded("Limit erreicht"))
        alpha = FakeAlpha()
        with self.assertRaises(ApiLimitExceeded):
            self._updates(finnhub, alpha).update_quote(self.us_id)
        self.assertEqual(alpha.calls, 0)

    def test_auto_select_uses_saved_unavailable_status_without_retesting(self) -> None:
        self.settings.ensure_defaults(self.us_id)
        self.settings.update_settings(
            self.us_id, {"quote": ("finnhub", "alpha_vantage", True)}
        )
        self.settings.save_capability(
            self.us_id, "finnhub", "quote", "premium_required"
        )
        finnhub = FakeFinnhub(EndpointResult("Kurs", "verfügbar", {"c": 123.0}))
        alpha = FakeAlpha()
        self._updates(finnhub, alpha).update_quote(self.us_id)
        self.assertEqual((finnhub.calls, alpha.calls), (0, 1))

    def test_airbus_finnhub_premium_quote_is_not_retested(self) -> None:
        self.settings.ensure_defaults(self.eu_id)
        self.settings.update_settings(self.eu_id,{"quote":("finnhub","alpha_vantage",False)})
        self.settings.save_capability(self.eu_id,"finnhub","quote","premium_required")
        finnhub=FakeFinnhub(EndpointResult("Kurs","verfügbar",{"c":200}))
        self._updates(finnhub,FakeAlpha()).update_quote(self.eu_id)
        self.assertEqual(finnhub.calls,0)

    def test_goog_history_uses_saved_premium_fallback_directly(self) -> None:
        self.settings.ensure_defaults(self.us_id)
        self.settings.save_capability(self.us_id, "finnhub", "history", "premium_required")
        finnhub = FakeFinnhub(EndpointResult("Historie", "verfügbar", {"c": [1, 2]}))
        alpha = FakeAlpha()
        self._updates(finnhub, alpha).update_history(self.us_id)
        self.assertEqual((finnhub.calls, alpha.calls), (0, 1))

    def test_quote_and_history_use_persisted_provider_symbol(self) -> None:
        with self.sessions.begin() as session:
            session.add(StockProviderSymbol(stock_id=self.eu_id,provider="alpha_vantage",
                symbol="MBG.DEX",status="available",source="test"))
        self.settings.ensure_defaults(self.eu_id)
        self.settings.update_settings(self.eu_id,{"quote":("alpha_vantage",None,False),"history":("alpha_vantage",None,False)})
        alpha=FakeAlpha();updates=self._updates(FakeFinnhub(EndpointResult("Kurs","leer",{})),alpha)
        updates.update_quote(self.eu_id);updates.update_history(self.eu_id)
        self.assertEqual(alpha.symbols,["MBG.DEX","MBG.DEX"])

    def test_airbus_provider_setting_remains_independent(self) -> None:
        self.settings.ensure_defaults(self.eu_id)
        self.settings.ensure_defaults(self.us_id)
        self.settings.update_settings(self.us_id, {"quote": ("alpha_vantage", None, False)})
        self.assertEqual(self.settings.get_setting(self.eu_id, "quote").primary_provider, "finnhub")


if __name__ == "__main__":
    unittest.main()
