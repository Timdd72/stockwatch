from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pandas as pd
from sqlalchemy import func, select

from ai.mkr_input import MkrInputAssembler
from database import ApiUsageService, SecurityCatalog, Stock, create_database, create_session_factory
from database.models import AnalysisSnapshot, ProviderCapability, StockProviderSetting
from market_data.finnhub import EndpointResult
from services import MarketUpdateService, ProviderSettingsService


def history(days: int = 260) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=days, freq="B")
    close = pd.Series([100 + value * .1 for value in range(days)], index=index)
    return pd.DataFrame({"Open": close - .2, "High": close + 1, "Low": close - 1,
                         "Close": close, "Volume": [1000 + value for value in range(days)]})


class FakeAlpha:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_daily(self, symbol: str) -> pd.DataFrame:
        self.calls.append(symbol)
        return history()


class FakeFinnhub:
    def __init__(self, availability: str = "Premium-Zugang erforderlich") -> None:
        self.availability = availability
        self.calls = 0

    def request(self, *args, **kwargs) -> EndpointResult:
        self.calls += 1
        return EndpointResult("Historie", self.availability, message="nicht verfügbar")


class MkrHistoryLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_database(Path(self.temp.name) / "history.db")
        self.sessions = create_session_factory(self.engine)
        with self.sessions.begin() as session:
            stock = Stock(symbol="AIR", name="Airbus", currency="EUR", exchange="XETR")
            session.add(stock); session.flush(); self.stock_id = stock.id
            session.add(SecurityCatalog(name="Airbus", symbol="AIR", exchange="XETR",
                country="DE", currency="EUR", security_type="stock", source="test",
                provider_symbol_alpha_vantage="AIR.DEX", provider_symbol_finnhub="AIR.DE"))
        self.settings = ProviderSettingsService(self.sessions)
        self.settings.ensure_defaults(self.stock_id)

    def tearDown(self) -> None:
        self.engine.dispose(); self.temp.cleanup()

    def service(self, finnhub: FakeFinnhub, alpha: FakeAlpha) -> MarketUpdateService:
        return MarketUpdateService(self.sessions, ApiUsageService(self.sessions),
            alpha_factory=lambda: alpha, finnhub_factory=lambda: finnhub)

    def test_provider_fallback_and_fresh_cache_prevent_duplicate_request(self) -> None:
        finnhub, alpha = FakeFinnhub(), FakeAlpha()
        service = self.service(finnhub, alpha)
        first = service.load_configured_history_for_analysis(self.stock_id)
        second = service.load_configured_history_for_analysis(self.stock_id)
        self.assertEqual((finnhub.calls, alpha.calls), (1, ["AIR.DEX"]))
        self.assertEqual(first.provider, "alpha_vantage")
        self.assertEqual(first.provider_symbol, "AIR.DEX")
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.history.index[0].date(), first.history.index[0].date())

    def test_saved_premium_capability_skips_primary_provider(self) -> None:
        self.settings.save_capability(self.stock_id, "finnhub", "history", "premium_required")
        finnhub, alpha = FakeFinnhub(), FakeAlpha()
        result = self.service(finnhub, alpha).load_configured_history_for_analysis(self.stock_id)
        self.assertEqual(finnhub.calls, 0)
        self.assertEqual(alpha.calls, ["AIR.DEX"])
        self.assertEqual(result.provider, "alpha_vantage")

    def test_assembler_uses_market_service_without_changing_domain_data(self) -> None:
        self.settings.save_capability(self.stock_id, "finnhub", "history", "premium_required")
        finnhub, alpha = FakeFinnhub(), FakeAlpha()
        market = self.service(finnhub, alpha)
        before = self.settings.get_setting(self.stock_id, "history")
        assembled = MkrInputAssembler(
            self.sessions, market.load_configured_history_for_analysis,
        ).build(self.stock_id)
        after = self.settings.get_setting(self.stock_id, "history")
        with self.sessions() as session:
            snapshots = session.scalar(select(func.count()).select_from(AnalysisSnapshot))
            capabilities = list(session.scalars(select(ProviderCapability)))
            settings = list(session.scalars(select(StockProviderSetting)))
        self.assertEqual(alpha.calls, ["AIR.DEX"])
        self.assertEqual(assembled.technical_data["history_metadata"]["provider_symbol"], "AIR.DEX")
        self.assertEqual(snapshots, 0)
        self.assertEqual((before.primary_provider, before.fallback_provider),
                         (after.primary_provider, after.fallback_provider))
        self.assertEqual(len(capabilities), 1)
        self.assertEqual(len(settings), 6)


if __name__ == "__main__":
    unittest.main()
