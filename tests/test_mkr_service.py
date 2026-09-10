from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import pandas as pd
from sqlalchemy import func, select

from ai.config import AiConfig
from ai.mkr_input import MkrInputAssembler
from ai.mkr_schemas import MkrAnalysis
from ai.mkr_service import (
    MkrAnalysisAlreadyRunning, MkrAnalysisFailed, MkrAnalysisService, _normalize_levels,
)
from ai.service import AiAnalysisService, AiBudgetExceeded
from analysis import TechnicalIndicators
from database import (
    AiUsage, MkrAnalysisRecord, StockAiAnalysis, StockWatchService,
    create_database, create_session_factory,
)
from database.models import Stock


def mkr_output(*, source_url: str | None = None) -> MkrAnalysis:
    frameworks = [{
        "number": number, "name": f"Framework {number}", "signal": "BULLISH",
        "strength": 4, "confidence": 80, "data_quality": "GOOD",
        "explanation": "Auf Basis der bereitgestellten lokalen Daten.",
        "levels": [{"value": 210.0, "currency": "$", "basis": "Lokales Kursniveau"}],
        "sources": [{"title": "Lokale Daten", "url": source_url,
                     "source": "StockWatch", "source_type": "STOCKWATCH_TECHNICAL"}],
    } for number in range(1, 15)]
    return MkrAnalysis.model_validate({
        "summary": ["Technische Lage ist gemischt.", "Das lokale Niveau ist entscheidend.",
                    "Weitere Bestätigung beobachten."],
        "frameworks": frameworks,
        "price_levels": {"stop_invalidation": None, "support_1": None, "support_2": None,
            "current_price": {"value": 208.05, "currency": "$", "basis": "StockWatch-Kurs"},
            "target_1": None, "target_2": None, "extended_target": None},
        "entry_timing": {"status": "BEOBACHTEN", "trigger_price": None,
                         "stop_invalidation": None, "alerts": []},
        "options": {"available": True, "statement": "Optionsdaten vorhanden.", "sources": []},
        "wheel": {"available": True, "statement": "Wheel möglich.", "sources": []},
        "uni_score": {"score": None, "criteria": [{
            "name": f"Kriterium {index}", "points": None, "explanation": "Nicht verfügbar"
        } for index in range(5)]},
        "confidence": 74, "peg": None, "avoid_if": "Trend bricht.",
        "risks": ["Risiko 1", "Risiko 2", "Risiko 3"],
        "data_coverage": {"full": 14, "limited": 0, "not_available": 0},
        "sources": [], "generated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    })


class FakeResponses:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(output_parsed=self.result,
            usage=SimpleNamespace(input_tokens=1200, output_tokens=800, total_tokens=2000),
            output=[])


class MkrServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_database(Path(self.temp.name) / "mkr-service.db")
        self.sessions = create_session_factory(self.engine)
        stocks = StockWatchService(self.sessions)
        self.stock, _, _ = stocks.initialize_airbus()
        stocks.save_analysis("AIR.PAR", TechnicalIndicators(
            208.05, 1, 2, 3, 4, 205, 200, None, 55, 22, 1.5, 4, "POSITIV"),
            price_type="DAILY_CLOSE", market_timestamp=datetime(2026, 8, 19, tzinfo=timezone.utc),
            provider="alpha_vantage")
        self.config = AiConfig("configured-test-model", Decimal("8"), Decimal("1"), Decimal("2"), 24)

    def tearDown(self) -> None:
        self.engine.dispose(); self.temp.cleanup()

    def service(self, fake: FakeResponses) -> MkrAnalysisService:
        ai = AiAnalysisService(self.sessions, self.config,
            client_factory=lambda: SimpleNamespace(responses=fake))
        return MkrAnalysisService(
            self.sessions, MkrInputAssembler(self.sessions), ai,
            enable_web_search=False,
        )

    def test_success_validates_persists_and_loads_complete_json_and_usage(self) -> None:
        fake = FakeResponses(mkr_output())
        service = self.service(fake)
        run = service.analyze(self.stock.id)
        self.assertFalse(run.reused)
        self.assertEqual(run.record.status, "COMPLETED")
        self.assertEqual(run.record.model, "configured-test-model")
        self.assertEqual(run.record.prompt_version, "1.3")
        self.assertFalse(run.record.web_search_used)
        self.assertEqual(run.record.web_search_calls, 0)
        loaded = service.load_result(run.record)
        self.assertEqual(loaded.model_dump(mode="json"), run.result.model_dump(mode="json"))
        self.assertEqual(service.get_latest_successful(self.stock.id).id, run.record.id)
        self.assertEqual(service.get_latest(self.stock.id).id, run.record.id)
        self.assertEqual(service.get_history(self.stock.id, 1)[0].id, run.record.id)
        with self.sessions() as session:
            usage = session.scalar(select(AiUsage))
            self.assertEqual((usage.purpose, usage.tool_type, usage.total_tokens),
                             ("mkr_analysis", "model", 2000))
            self.assertTrue(usage.success)
        request = fake.calls[0]
        self.assertIs(request["text_format"], MkrAnalysis)
        self.assertNotIn("tools", request)
        self.assertIn("KEINE Web Search", request["input"][0]["content"])

    def test_insufficient_input_is_not_upgraded_and_options_stay_unavailable(self) -> None:
        run = self.service(FakeResponses(mkr_output())).analyze(self.stock.id)
        option = next(item for item in run.result.frameworks if item.number == 9)
        self.assertEqual(option.signal.value, "NOT_AVAILABLE")
        self.assertEqual(option.data_quality.value, "INSUFFICIENT")
        self.assertIsNone(option.strength)
        self.assertFalse(run.result.options.available)
        self.assertFalse(run.result.wheel.available)
        self.assertEqual(run.result.data_coverage.full + run.result.data_coverage.limited +
                         run.result.data_coverage.not_available, 14)

    def test_daily_close_eur_and_manual_price_are_passed_and_currency_is_corrected(self) -> None:
        daily_fake = FakeResponses(mkr_output())
        daily = self.service(daily_fake).analyze(self.stock.id)
        self.assertIn('"price_type":"DAILY_CLOSE"', daily_fake.calls[0]["input"][1]["content"])
        self.assertEqual(daily.result.price_levels.current_price.currency, "EUR")
        StockWatchService(self.sessions).save_manual_price(self.stock.id, Decimal("206.10"))
        manual_fake = FakeResponses(mkr_output())
        self.service(manual_fake).analyze(self.stock.id)
        self.assertIn('"price_type":"MANUAL"', manual_fake.calls[0]["input"][1]["content"])

    def test_usd_stock_keeps_usd(self) -> None:
        with self.sessions.begin() as session:
            stock = Stock(symbol="GOOG", name="Alphabet", currency="USD", exchange="NASDAQ")
            session.add(stock); session.flush(); stock_id = stock.id
        StockWatchService(self.sessions).save_analysis("GOOG", TechnicalIndicators(
            300, 1, 2, 3, 4, 290, 280, None, 50, 20, 3, 7, "POSITIV"),
            price_type="REALTIME", provider="finnhub")
        run = self.service(FakeResponses(mkr_output())).analyze(stock_id)
        self.assertEqual(run.result.price_levels.current_price.currency, "USD")

    def test_fingerprint_reuses_result_and_force_creates_new_analysis(self) -> None:
        fake = FakeResponses(mkr_output()); service = self.service(fake)
        first = service.analyze(self.stock.id)
        second = service.analyze(self.stock.id)
        third = service.analyze(self.stock.id, force=True)
        self.assertFalse(first.reused); self.assertTrue(second.reused); self.assertFalse(third.reused)
        self.assertEqual(len(fake.calls), 2)
        self.assertNotEqual(first.record.id, third.record.id)

    def test_running_row_blocks_parallel_analysis(self) -> None:
        with self.sessions.begin() as session:
            session.add(MkrAnalysisRecord(stock_id=self.stock.id, status="RUNNING",
                model="test", prompt_version="1.0", input_fingerprint="x",
                web_search_used=False, web_search_calls=0))
        fake = FakeResponses(mkr_output())
        with self.assertRaises(MkrAnalysisAlreadyRunning):
            self.service(fake).analyze(self.stock.id, force=True)
        self.assertEqual(fake.calls, [])

    def test_budget_prevents_request_and_marks_run_failed(self) -> None:
        with self.sessions.begin() as session:
            session.add(AiUsage(stock_id=self.stock.id, purpose="existing", model="test",
                input_tokens=0, output_tokens=0, total_tokens=0,
                estimated_cost_eur=Decimal("8"), success=True))
        fake = FakeResponses(mkr_output())
        with self.assertRaises(AiBudgetExceeded):
            self.service(fake).analyze(self.stock.id)
        self.assertEqual(fake.calls, [])
        latest = self.service(fake).get_latest(self.stock.id)
        self.assertEqual((latest.status, latest.error_type), ("FAILED", "budget"))

    def test_openai_and_invalid_output_fail_without_replacing_success(self) -> None:
        successful = self.service(FakeResponses(mkr_output())).analyze(self.stock.id)
        for response in (ConnectionError("secret-value"), {"invalid": True}):
            with self.subTest(response=response):
                with self.assertRaises(MkrAnalysisFailed):
                    self.service(FakeResponses(response)).analyze(self.stock.id, force=True)
                self.assertEqual(self.service(FakeResponses(response)).get_latest_successful(
                    self.stock.id).id, successful.record.id)
                latest = self.service(FakeResponses(response)).get_latest(self.stock.id)
                self.assertEqual(latest.status, "FAILED")
                self.assertIsNone(latest.structured_result)
                self.assertNotIn("secret-value", latest.error_message)

    def test_existing_stock_ai_analysis_table_is_untouched(self) -> None:
        with self.sessions() as session:
            before = session.scalar(select(func.count()).select_from(StockAiAnalysis))
        self.service(FakeResponses(mkr_output())).analyze(self.stock.id)
        with self.sessions() as session:
            after = session.scalar(select(func.count()).select_from(StockAiAnalysis))
        self.assertEqual((before, after), (0, 0))

    def test_deterministic_relationships_quality_caps_and_level_types(self) -> None:
        index = pd.date_range("2025-01-01", periods=260, freq="B")
        close = pd.Series([220 + value * .2 for value in range(260)], index=index)
        history = pd.DataFrame({
            "Open": close - .2, "High": close + 1, "Low": close - 1,
            "Close": close, "Volume": [10_000 + value for value in range(260)],
        })
        assembler = MkrInputAssembler(self.sessions)
        data = assembler.build(self.stock.id, history=history,
                               history_provider="local", history_provider_symbol="AIR.PAR")

        payload = mkr_output().model_dump(mode="json")
        payload["summary"][0] = "RSI und MACD sind negativ."
        payload["frameworks"][12]["explanation"] = (
            "EMA20 liegt unter EMA50. Kurs liegt über EMA20. "
            "EMA50 liegt unter EMA200. SMA50 liegt unter SMA200."
        )
        payload["frameworks"][3]["levels"] = [{
            "value": 37.37, "level_type": "PRICE", "currency": "EUR", "basis": "RSI14"
        }]
        payload["frameworks"][7]["levels"] = [
            {"value": 19.66, "level_type": "PRICE", "currency": "EUR", "basis": "ADX14"},
            {"value": -8.4, "level_type": "PRICE", "currency": "EUR", "basis": "20-Tage-Performance %"},
            {"value": 240.0, "level_type": "PRICE", "currency": "EUR", "basis": "Bollinger-Unterband"},
        ]
        payload["frameworks"][12]["levels"] = [{
            "value": .01709, "level_type": "PRICE", "currency": "EUR", "basis": "TRIX15"
        }]
        payload["frameworks"][5]["levels"] = [{
            "value": 3.01, "level_type": "PRICE", "currency": "EUR",
            "basis": "Volumen 3,01 % über Durchschnitt",
        }]
        model_result = MkrAnalysis.model_validate(payload)
        service = self.service(FakeResponses(model_result))
        corrected = service._enforce_local_data_contract(model_result, data)

        confirmation = corrected.frameworks[12]
        self.assertNotIn("EMA20 liegt unter EMA50", confirmation.explanation)
        self.assertNotIn("Kurs liegt über EMA20", confirmation.explanation)
        self.assertNotIn("EMA50 liegt unter EMA200", confirmation.explanation)
        self.assertNotIn("SMA50 liegt unter SMA200", confirmation.explanation)
        self.assertIn("EMA20", confirmation.explanation)
        self.assertIn("liegt über EMA50", confirmation.explanation)
        self.assertIn("Kurs (208.05) liegt unter EMA20", confirmation.explanation)
        self.assertIn("EMA50", confirmation.explanation)
        self.assertIn("liegt über EMA200", confirmation.explanation)
        self.assertIn("SMA50", confirmation.explanation)
        self.assertIn("liegt über SMA200", confirmation.explanation)

        rsi = corrected.frameworks[3].levels[0]
        adx, performance, bollinger = corrected.frameworks[7].levels
        trix = corrected.frameworks[12].levels[0]
        volume_deviation = corrected.frameworks[5].levels[0]
        self.assertEqual((rsi.level_type.value, rsi.currency), ("INDICATOR", None))
        self.assertEqual((trix.level_type.value, trix.currency), ("INDICATOR", None))
        self.assertEqual((adx.level_type.value, adx.currency), ("INDICATOR", None))
        self.assertEqual((performance.level_type.value, performance.currency), ("PERCENTAGE", None))
        self.assertEqual((bollinger.level_type.value, bollinger.currency), ("PRICE", "EUR"))
        self.assertEqual(
            (volume_deviation.level_type.value, volume_deviation.currency),
            ("PERCENTAGE", None),
        )
        self.assertEqual(corrected.price_levels.current_price.currency, "EUR")
        self.assertIn("RSI ist schwach und MACD ist negativ", corrected.summary[0])
        self.assertNotIn("RSI und MACD sind negativ", corrected.summary[0])
        self.assertEqual(
            service._enforce_local_data_contract(
                MkrAnalysis.model_validate({
                    **model_result.model_dump(mode="json"),
                    "summary": ["Eine negative RSI-Divergenz ist erkennbar.",
                                *model_result.summary[1:]],
                }),
                data,
            ).summary[0],
            "Eine negative RSI-Divergenz ist erkennbar.",
        )

        self.assertEqual(corrected.frameworks[3].data_quality.value, "LIMITED")
        self.assertEqual(corrected.frameworks[5].data_quality.value, "LIMITED")
        for number in (9, 10, 12):
            framework = corrected.frameworks[number - 1]
            self.assertEqual(framework.data_quality.value, "INSUFFICIENT")
            self.assertEqual(framework.signal.value, "NOT_AVAILABLE")

    def test_fibonacci_labels_with_percentages_remain_price_levels(self) -> None:
        levels = [
            {"value": 207.79, "level_type": "PERCENTAGE", "currency": None,
             "basis": "23,6 % Fibonacci-Retracement"},
            {"value": 224.10, "level_type": "PERCENTAGE", "currency": None,
             "basis": "Fibonacci-Extension 1,618"},
        ]
        _normalize_levels(levels, "EUR", 2)
        self.assertEqual(
            [(item["level_type"], item["currency"]) for item in levels],
            [("PRICE", "EUR"), ("PRICE", "EUR")],
        )

    def test_absolute_quantities_are_not_currency_prices(self) -> None:
        levels = [
            {"value": 1_250_000, "level_type": "PRICE", "currency": "EUR",
             "basis": "Absolutes Handelsvolumen"},
            {"value": 718_525, "level_type": "PRICE", "currency": "EUR",
             "basis": "20-Tage-Volumendurchschnitt"},
            {"value": 820, "level_type": "PRICE", "currency": "EUR",
             "basis": "Bestellungen / Orders"},
            {"value": 766, "level_type": "PRICE", "currency": "EUR",
             "basis": "Ausgelieferte Flugzeuge / Deliveries"},
            {"value": 3.01, "level_type": "PRICE", "currency": "EUR",
             "basis": "Volumenabweichung +3,01 % zum Durchschnitt"},
            {"value": 199.46, "level_type": "INDICATOR", "currency": None,
             "basis": "Aktueller Kurs"},
            {"value": 37.37, "level_type": "PRICE", "currency": "EUR",
             "basis": "RSI14"},
        ]
        _normalize_levels(levels, "EUR", 6)
        self.assertEqual(
            [(item["level_type"], item["currency"]) for item in levels],
            [
                ("QUANTITY", None), ("QUANTITY", None),
                ("QUANTITY", None), ("QUANTITY", None),
                ("PERCENTAGE", None), ("PRICE", "EUR"),
                ("INDICATOR", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()
