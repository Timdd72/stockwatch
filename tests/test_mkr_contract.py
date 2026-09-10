from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

import pandas as pd
from pydantic import ValidationError

from ai.mkr_input import MkrInputAssembler
from ai.mkr_prompt import MKR_MASTER_PROMPT, MKR_PROMPT_VERSION, MKR_SYSTEM_INSTRUCTIONS
from ai.mkr_schemas import MkrAnalysis, MkrLevel
from analysis import TechnicalIndicators
from database import StockWatchService, create_database, create_session_factory
from database.models import CompanyData, Position, ProviderCache


def valid_output() -> dict:
    frameworks = [{
        "number": number, "name": f"Framework {number}", "signal": "NOT_AVAILABLE",
        "strength": None, "confidence": 0, "data_quality": "INSUFFICIENT",
        "explanation": "Nicht verfügbar.", "levels": [], "sources": [],
    } for number in range(1, 15)]
    return {
        "summary": ["Satz eins.", "Satz zwei.", "Satz drei."],
        "frameworks": frameworks,
        "price_levels": {"stop_invalidation": None, "support_1": None, "support_2": None,
                         "current_price": None, "target_1": None, "target_2": None,
                         "extended_target": None},
        "entry_timing": {"status": "BEOBACHTEN", "trigger_price": None,
                         "stop_invalidation": None, "alerts": []},
        "options": {"available": False, "statement": "Nicht verfügbar.", "sources": []},
        "wheel": {"available": False, "statement": "Nicht verfügbar.", "sources": []},
        "uni_score": {"score": None, "criteria": [
            {"name": f"Kriterium {value}", "points": None, "explanation": "N/V"}
            for value in range(5)]},
        "confidence": 0, "peg": None, "avoid_if": "Keine belastbaren Daten.",
        "risks": ["Risiko 1", "Risiko 2", "Risiko 3"],
        "data_coverage": {"full": 0, "limited": 0, "not_available": 14},
        "sources": [], "generated_at": datetime.now(timezone.utc),
    }


class MkrContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.factory = create_session_factory(create_database(Path(self.temp.name) / "mkr.db"))
        service = StockWatchService(self.factory)
        self.stock, _, _ = service.initialize_airbus()
        service.save_analysis("AIR.PAR", TechnicalIndicators(
            208.05, 1, 2, 3, 4, 205, 200, None, 55, 22, 1.5, 4, "POSITIV"),
            price_type="DAILY_CLOSE", market_timestamp=datetime(2026, 8, 19, tzinfo=timezone.utc),
            provider="alpha_vantage",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def history(self, days: int = 260) -> pd.DataFrame:
        index = pd.date_range("2025-01-01", periods=days, freq="B")
        close = pd.Series([100 + value * .1 + (value % 9) * .2 for value in range(days)], index=index)
        return pd.DataFrame({"Open": close - .2, "High": close + 1, "Low": close - 1,
                             "Close": close, "Volume": [10_000 + value for value in range(days)]})

    def test_schema_has_14_unique_frameworks_and_framework_sources(self) -> None:
        result = MkrAnalysis.model_validate(valid_output())
        self.assertEqual(len(result.frameworks), 14)
        self.assertEqual(result.frameworks[0].sources, [])
        invalid = valid_output(); invalid["frameworks"][-1]["number"] = 13
        with self.assertRaises(ValidationError):
            MkrAnalysis.model_validate(invalid)

    def test_prompt_is_central_versioned_and_forbids_invented_values(self) -> None:
        self.assertEqual(MKR_PROMPT_VERSION, "1.3")
        self.assertIn("14 MKR-Frameworks", MKR_MASTER_PROMPT)
        self.assertIn("Wachstumsraten in Prozent sind PERCENTAGE", MKR_MASTER_PROMPT)
        self.assertIn("KGV/P-E, PEG, P/S, P/B", MKR_MASTER_PROMPT)
        self.assertIn("Zählwerte sind QUANTITY mit currency null", MKR_MASTER_PROMPT)
        self.assertIn("PRICE -> currency erforderlich", MKR_MASTER_PROMPT)
        self.assertIn("Schätze oder ergänze niemals", MKR_SYSTEM_INSTRUCTIONS)
        self.assertIn("DAILY_CLOSE", MKR_SYSTEM_INSTRUCTIONS)
        self.assertIn("MANUAL", MKR_SYSTEM_INSTRUCTIONS)

    def test_level_types_keep_currency_exclusively_for_prices(self) -> None:
        price = MkrLevel(value=194.74, level_type="PRICE", currency="EUR", basis="Kurs")
        indicator = MkrLevel(value=37.37, level_type="INDICATOR", basis="RSI14")
        percentage = MkrLevel(value=-8.4, level_type="PERCENTAGE", basis="Performance")
        ratio = MkrLevel(value=.85, level_type="RATIO", basis="Put/Call")
        self.assertEqual(price.currency, "EUR")
        self.assertIsNone(indicator.currency)
        self.assertIsNone(percentage.currency)
        self.assertIsNone(ratio.currency)
        with self.assertRaises(ValidationError):
            MkrLevel(value=37.37, level_type="INDICATOR", currency="EUR", basis="RSI14")
        with self.assertRaises(ValidationError):
            MkrLevel(value=194.74, level_type="PRICE", basis="Kurs")

    def test_assembler_preserves_daily_close_and_uses_injected_existing_loader(self) -> None:
        calls: list[int] = []
        assembler = MkrInputAssembler(
            self.factory, lambda stock_id: (calls.append(stock_id) or self.history(), "alpha_vantage"))
        result = assembler.build(self.stock.id)
        self.assertEqual(calls, [self.stock.id])
        self.assertEqual(result.price["price_type"], "DAILY_CLOSE")
        self.assertEqual(result.price["market_timestamp"], "2026-08-19T00:00:00+00:00")
        self.assertEqual(result.technical_data["history_provider"], "alpha_vantage")
        self.assertIsNotNone(result.technical_data["ema200"])
        self.assertEqual(len(result.framework_availability), 14)
        self.assertEqual(result.framework_availability[8].coverage.value, "NOT_AVAILABLE")

    def test_manual_price_is_preserved_and_does_not_erase_older_technical_data(self) -> None:
        StockWatchService(self.factory).save_manual_price(self.stock.id, Decimal("206.10"))
        result = MkrInputAssembler(self.factory).build(self.stock.id)
        self.assertEqual(result.price["price_type"], "MANUAL")
        self.assertEqual(result.price["provider"], "manual")
        self.assertEqual(result.technical_data["sma20"], 205)
        self.assertIsNone(result.technical_data.get("ema20"))

    def test_missing_history_and_fundamentals_remain_unavailable(self) -> None:
        result = MkrInputAssembler(self.factory).build(self.stock.id)
        self.assertIsNone(result.fundamental_data)
        self.assertEqual(result.framework_availability[0].data_quality.value, "INSUFFICIENT")
        self.assertEqual(result.framework_availability[8].coverage.value, "NOT_AVAILABLE")

    def test_fundamentals_and_position_are_read_without_writes(self) -> None:
        with self.factory.begin() as session:
            session.add(Position(stock_id=self.stock.id, purchase_price=Decimal("180"),
                                 quantity=Decimal("2"), purchase_date=date(2026, 1, 1), active=True))
            session.add(CompanyData(stock_id=self.stock.id, provider="finnhub", name="Airbus SE",
                                    currency="EUR", exchange="Paris", market_cap=100.0,
                                    pe_ratio=20.0, eps=5.0, revenue=50.0, revenue_growth=3.0,
                                    roe=12.0, debt_to_equity=0.5))
        result = MkrInputAssembler(self.factory).build(self.stock.id, history=self.history(60),
                                                       history_provider="existing-service")
        self.assertEqual(result.stock["currency"], "EUR")
        self.assertAlmostEqual(result.position["profit_loss"], 56.10)
        self.assertIsNone(result.fundamental_data["peg"])
        self.assertIsNone(result.fundamental_data["week_52_high"])

    def test_fresh_local_history_is_preferred_and_exposes_metadata(self) -> None:
        records = []
        for index, row in self.history(260).iterrows():
            records.append({"Date": index.isoformat(), **{key: float(value) for key, value in row.items()}})
        import json
        with self.factory.begin() as session:
            session.add(ProviderCache(provider="alpha_vantage", symbol="AIR.PAR",
                data_type="mkr_history", fetched_at=datetime.now(timezone.utc),
                payload=json.dumps({"records": records})))
        calls = []
        result = MkrInputAssembler(self.factory, lambda _stock_id: calls.append(True)).build(self.stock.id)
        metadata = result.technical_data["history_metadata"]
        self.assertEqual(calls, [])
        self.assertEqual(metadata["provider"], "alpha_vantage")
        self.assertEqual(metadata["provider_symbol"], "AIR.PAR")
        self.assertEqual(metadata["number_of_rows"], 260)
        self.assertTrue(metadata["has_ohlc"])
        self.assertTrue(metadata["has_volume"])
        self.assertTrue(metadata["sufficient_for_ema200"])
        self.assertEqual(metadata["freshness"], "FRESH")
        expected = self.history(260).iloc[-252:]
        self.assertEqual(result.fundamental_data["week_52_high"], float(expected["High"].max()))
        self.assertEqual(result.fundamental_data["week_52_low"], float(expected["Low"].min()))

    def test_long_history_improves_framework_coverage(self) -> None:
        result = MkrInputAssembler(self.factory).build(
            self.stock.id, history=self.history(520), history_provider="fake",
            history_provider_symbol="AIR.PAR")
        by_number = {item.number: item for item in result.framework_availability}
        for number in (1, 2, 3, 7, 8, 11, 13):
            self.assertEqual(by_number[number].coverage.value, "FULL", number)
        for number in (4, 5, 6):
            self.assertEqual(by_number[number].coverage.value, "LIMITED", number)
        self.assertEqual(by_number[9].coverage.value, "NOT_AVAILABLE")
        metadata = result.technical_data["history_metadata"]
        self.assertTrue(metadata["sufficient_for_monthly"])
        self.assertGreater(len(result.technical_data["timeframes"]["weekly"]["bars"]), 0)
        self.assertGreater(len(result.technical_data["timeframes"]["monthly"]["bars"]), 0)


if __name__ == "__main__":
    unittest.main()
