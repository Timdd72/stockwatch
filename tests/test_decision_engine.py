import unittest
from datetime import datetime, timedelta, timezone

from services.decision_engine import DecisionAction, DecisionEngine, DecisionInput, DecisionPerspective, Signal


class DecisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine(now=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc))

    def test_clear_bullish_is_buy(self):
        result = self.engine.evaluate(DecisionInput(current_price=110, trend="POSITIV", performance_20d=8, performance_60d=15, sma20=105, sma50=100, macd_histogram=2))
        self.assertEqual(result.action, DecisionAction.BUY)
        self.assertEqual(result.signal, Signal.STRONG_BULLISH)

    def test_clear_bearish_position_is_sell(self):
        result = self.engine.evaluate(DecisionInput(current_price=80, trend="NEGATIV", performance_20d=-8, performance_60d=-20, sma20=90, sma50=100, macd_histogram=-2, has_position=True))
        self.assertEqual(result.action, DecisionAction.SELL)

    def test_no_position_never_gets_sell(self):
        result = self.engine.evaluate(DecisionInput(current_price=80, trend="NEGATIV", performance_60d=-20))
        self.assertEqual(result.action, DecisionAction.HOLD)

    def test_neutral_is_hold(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, currency="EUR"))
        self.assertEqual(result.action, DecisionAction.HOLD)

    def test_conflicting_signals_reduce_confidence(self):
        aligned = self.engine.evaluate(DecisionInput(current_price=110, trend="POSITIV", performance_20d=8, performance_60d=15, sma20=105, sma50=100, macd_histogram=2))
        conflict = self.engine.evaluate(DecisionInput(current_price=110, trend="POSITIV", performance_20d=-8, performance_60d=-15, sma20=105, sma50=100, macd_histogram=-2))
        self.assertLess(conflict.confidence, aligned.confidence)

    def test_missing_data_reduces_confidence_and_no_fabricated_zones(self):
        result = self.engine.evaluate(DecisionInput(current_price=100))
        self.assertLess(result.confidence, 60)
        self.assertIsNone(result.buy_zone_low)
        self.assertIsNone(result.sell_zone_high)
        self.assertTrue(result.data_gaps)

    def test_stale_data_warns(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, data_timestamp=datetime.now(timezone.utc) - timedelta(days=3)))
        self.assertTrue(any("älter" in warning for warning in result.warnings))

    def test_zones_are_ordered(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, supports=(90, 95), resistances=(110, 120)))
        self.assertLessEqual(result.buy_zone_low, result.buy_zone_high)
        self.assertLessEqual(result.sell_zone_low, result.sell_zone_high)

    def test_position_profit_is_explanatory_only(self):
        result = self.engine.evaluate(DecisionInput(current_price=110, has_position=True, purchase_price=80, profit_loss_percent=37.5))
        self.assertIn("37.50", " ".join(result.reasons))
        self.assertEqual(result.action, DecisionAction.HOLD)

    def test_immediate_actions_are_gated(self):
        result = self.engine.evaluate(DecisionInput(current_price=110, trend="POSITIV", performance_20d=10, performance_60d=20, sma20=100, sma50=90, macd_histogram=2, has_position=True, verified_event=False))
        self.assertNotIn(result.action, (DecisionAction.IMMEDIATE_BUY, DecisionAction.IMMEDIATE_SELL))

    def test_mkr_good_counts_more_than_limited(self):
        good = {"signal": "BULLISH", "confidence": 100, "data_quality": "GOOD"}
        limited = {"signal": "BULLISH", "confidence": 100, "data_quality": "LIMITED"}
        self.assertGreater(self.engine._mkr_score((good,)), self.engine._mkr_score((limited,)))

    def test_mkr_insufficient_does_not_add_signal(self):
        self.assertEqual(self.engine._mkr_score(({"signal": "BULLISH", "confidence": 100, "data_quality": "INSUFFICIENT"},)), 0)

    def test_daily_close_and_manual_are_warned(self):
        daily = self.engine.evaluate(DecisionInput(current_price=100, price_type="DAILY_CLOSE"))
        manual = self.engine.evaluate(DecisionInput(current_price=100, price_type="MANUAL"))
        self.assertTrue(daily.warnings)
        self.assertTrue(manual.warnings)

    def test_manual_quote_uses_older_technical_context(self):
        result = self.engine.evaluate(DecisionInput(
            current_price=95, price_type="MANUAL", data_timestamp=datetime(2026, 9, 9, tzinfo=timezone.utc),
            technical_context_timestamp=datetime(2026, 9, 8, tzinfo=timezone.utc),
            technical_context={"trend": "POSITIV", "performance_20d": 6, "performance_60d": 12, "sma20": 90, "sma50": 85, "rsi14": 52},
        ))
        self.assertIn("Trend ist positiv", result.supporting_signals)
        self.assertIn("Kurs liegt über SMA20", result.supporting_signals)
        self.assertEqual(result.technical_context_timestamp, datetime(2026, 9, 8, tzinfo=timezone.utc))
        self.assertNotEqual(result.data_quality, "INSUFFICIENT")

    def test_current_manual_price_is_used_against_context_levels(self):
        result = self.engine.evaluate(DecisionInput(
            current_price=90, technical_context={"sma20": 100, "sma50": 95, "supports": (85,), "resistances": (110,)},
        ))
        self.assertIn("Kurs liegt unter SMA20", result.opposing_signals)
        self.assertIn("Kurs liegt unter SMA50", result.opposing_signals)
        self.assertEqual(result.buy_zone_low, 85)

    def test_context_rsi_and_macd_are_not_recomputed(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, rsi14=None, macd_histogram=None, technical_context={"rsi14": 37.5, "macd_histogram": -1.2}))
        self.assertIn("MACD-Histogramm ist negativ", result.opposing_signals)
        self.assertEqual(result.data_quality, "LIMITED")

    def test_old_context_reduces_confidence(self):
        fresh = self.engine.evaluate(DecisionInput(current_price=100, technical_context={"trend": "POSITIV", "performance_20d": 4, "performance_60d": 8, "sma20": 95, "sma50": 90, "rsi14": 50}, technical_context_timestamp=datetime(2026, 9, 9, tzinfo=timezone.utc)))
        old = self.engine.evaluate(DecisionInput(current_price=100, technical_context={"trend": "POSITIV", "performance_20d": 4, "performance_60d": 8, "sma20": 95, "sma50": 90, "rsi14": 50}, technical_context_timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc)))
        self.assertLess(old.confidence, fresh.confidence)

    def test_position_and_entry_are_separate_views(self):
        result = self.engine.evaluate(DecisionInput(
            current_price=110, has_position=True, purchase_price=80,
            trend="POSITIV", performance_20d=8, performance_60d=12,
            sma20=105, sma50=100,
        ))
        self.assertIsNotNone(result.position_decision)
        self.assertIsNotNone(result.entry_decision)
        self.assertEqual(result.position_decision.perspective, DecisionPerspective.POSITION)
        self.assertEqual(result.entry_decision.perspective, DecisionPerspective.NEW_ENTRY)
        self.assertEqual(result.position_decision.action, DecisionAction.HOLD)
        self.assertEqual(result.entry_decision.action, DecisionAction.BUY)

    def test_position_and_entry_have_independent_confidence(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, has_position=True, purchase_price=90, trend="POSITIV"))
        self.assertNotEqual(result.position_decision.confidence, result.entry_decision.confidence)

    def test_bearish_position_and_entry_are_both_sell(self):
        result = self.engine.evaluate(DecisionInput(current_price=80, has_position=True, purchase_price=100, trend="NEGATIV", performance_20d=-8, performance_60d=-20, sma20=90, sma50=100, macd_histogram=-2))
        self.assertEqual(result.position_decision.action, DecisionAction.SELL)
        self.assertEqual(result.entry_decision.action, DecisionAction.SELL)

    def test_no_position_has_no_position_view(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, trend="POSITIV"))
        self.assertIsNone(result.position_decision)
        self.assertIsNotNone(result.entry_decision)

    def test_perspectives_have_separate_signal_context(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, has_position=True, trend="NEGATIV", performance_20d=-2))
        self.assertIsNot(result.position_decision.reasons, result.entry_decision.reasons)
        self.assertTrue(result.position_decision.opposing_signals)

    def test_partial_overall_coverage_cannot_reach_100(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, trend="NEGATIV", performance_20d=-5, performance_60d=-8, sma20=110, sma50=115, rsi14=45))
        self.assertLess(result.confidence, 100)
        self.assertNotEqual(result.data_quality, "GOOD")

    def test_fundamental_mkr_signal_offsets_technical_signal(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, trend="NEGATIV", performance_20d=-5, performance_60d=-8, sma20=110, sma50=115, mkr_frameworks=({"number": 10, "signal": "BULLISH", "confidence": 65, "data_quality": "LIMITED"},), mkr_data_coverage_limited=1, mkr_data_coverage_not_available=13))
        self.assertIn("Fundamentals/Katalysatoren sind bullish", result.supporting_signals)
        self.assertTrue(result.data_gaps)

    def test_stockwatch_signals_are_limited_meta_signals(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, trend="NEGATIV", stockwatch_position_action="HOLD", stockwatch_entry_action="WATCH"))
        self.assertNotIn("StockWatch-Einschätzung steht der Richtung entgegen", result.opposing_signals)
        self.assertLess(result.confidence, 100)

    def test_mkr_block_is_clamped_and_framework10_is_separate(self):
        bearish = tuple({"number": i, "signal": "BEARISH", "confidence": 100, "data_quality": "GOOD"} for i in range(1, 10))
        bullish = tuple({"number": i, "signal": "BULLISH", "confidence": 100, "data_quality": "GOOD"} for i in range(1, 10))
        self.assertGreaterEqual(self.engine._mkr_score(bearish), -2)
        self.assertLessEqual(self.engine._mkr_score(bullish), 2)
        self.assertEqual(self.engine._mkr_score(({"number": 10, "signal": "BULLISH", "confidence": 100, "data_quality": "GOOD"},)), 0)

    def test_conflicting_fundamental_and_technical_signals_warn(self):
        result = self.engine.evaluate(DecisionInput(current_price=100, trend="NEGATIV", performance_20d=-4, sma20=110, mkr_frameworks=({"number": 10, "signal": "BULLISH", "confidence": 65, "data_quality": "LIMITED"},), mkr_data_coverage_limited=1, mkr_data_coverage_not_available=13))
        self.assertIn("Signale widersprechen sich.", result.warnings)
        self.assertIn("Fundamentals/Katalysatoren sind bullish", result.supporting_signals)


if __name__ == "__main__":
    unittest.main()
