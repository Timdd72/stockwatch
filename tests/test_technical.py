"""Unit-Tests für die lokalen technischen Kennzahlen."""

from __future__ import annotations

import unittest

import pandas as pd

from analysis.technical import (
    TechnicalAnalysisError, aggregate_ohlcv, calculate_mkr_technical_indicators,
    calculate_technical_indicators,
)


class TechnicalIndicatorsTests(unittest.TestCase):
    @staticmethod
    def history(values: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {"Close": values},
            index=pd.date_range("2025-01-01", periods=len(values), freq="B"),
        )

    def test_calculates_performance_averages_and_positive_trend(self) -> None:
        result = calculate_technical_indicators(
            self.history([float(value) for value in range(1, 202)])
        )

        self.assertEqual(result.current_close, 201.0)
        self.assertAlmostEqual(result.previous_day_change_pct or 0, 0.5)
        self.assertAlmostEqual(result.performance_5d_pct or 0, 2.551020408)
        self.assertEqual(result.sma20, 191.5)
        self.assertEqual(result.sma50, 176.5)
        self.assertEqual(result.sma200, 101.5)
        self.assertEqual(result.rsi14, 100.0)
        self.assertEqual(result.trend, "POSITIV")

    def test_short_series_marks_unavailable_values_and_neutral_trend(self) -> None:
        result = calculate_technical_indicators(self.history([100.0, 101.0]))

        self.assertAlmostEqual(result.previous_day_change_pct or 0, 1.0)
        self.assertIsNone(result.performance_5d_pct)
        self.assertIsNone(result.sma20)
        self.assertIsNone(result.rsi14)
        self.assertIsNone(result.volatility_20d_pct)
        self.assertEqual(result.trend, "NEUTRAL")

    def test_detects_negative_trend(self) -> None:
        result = calculate_technical_indicators(
            self.history([float(value) for value in range(100, 40, -1)])
        )
        self.assertEqual(result.trend, "NEGATIV")

    def test_rejects_missing_or_invalid_close_values(self) -> None:
        invalid_histories = [
            pd.DataFrame({"Open": [1.0]}),
            pd.DataFrame({"Close": []}),
            pd.DataFrame({"Close": [0.0]}),
        ]
        for history in invalid_histories:
            with self.subTest(history=history), self.assertRaises(
                TechnicalAnalysisError
            ):
                calculate_technical_indicators(history)

    def test_mkr_indicators_are_deterministic_and_keep_base_analysis(self) -> None:
        index = pd.date_range("2025-01-01", periods=260, freq="B")
        close = pd.Series([100 + value * 0.25 for value in range(260)], index=index)
        history = pd.DataFrame({
            "Open": close - 0.2, "High": close + 1, "Low": close - 1,
            "Close": close, "Volume": [1_000 + value for value in range(260)],
        })
        result = calculate_mkr_technical_indicators(history)

        self.assertEqual(result.base, calculate_technical_indicators(history))
        self.assertEqual(result.trading_days, 260)
        self.assertIsNotNone(result.ema20)
        self.assertIsNotNone(result.ema50)
        self.assertIsNotNone(result.ema200)
        self.assertAlmostEqual(result.macd or 0, 1.75, places=4)
        self.assertAlmostEqual(result.macd_signal or 0, 1.75, places=4)
        self.assertAlmostEqual(result.macd_histogram or 0, 0, places=4)
        self.assertAlmostEqual(result.atr14 or 0, 2.0, places=5)
        self.assertIsNotNone(result.bollinger_upper)
        self.assertIsNotNone(result.volume_vs_average_20d_percent)
        self.assertGreater(result.obv or 0, 0)
        self.assertGreater(result.adx14 or 0, 90)
        self.assertIsNotNone(result.trix15)

    def test_mkr_short_history_returns_none_instead_of_estimating(self) -> None:
        index = pd.date_range("2026-01-01", periods=10, freq="B")
        history = pd.DataFrame({
            "High": range(11, 21), "Low": range(9, 19), "Close": range(10, 20),
        }, index=index)
        result = calculate_mkr_technical_indicators(history)
        self.assertIsNone(result.ema20)
        self.assertIsNone(result.macd)
        self.assertIsNone(result.atr14)
        self.assertIsNone(result.volume_average_20d)
        self.assertIsNone(result.obv)
        self.assertIsNone(result.adx14)
        self.assertIsNone(result.trix15)

    def test_fvg_swings_and_fibonacci_use_only_ohlc_values(self) -> None:
        index = pd.date_range("2026-01-01", periods=15, freq="B")
        closes = [10, 11, 12, 15, 14, 13, 11, 12, 14, 17, 16, 14, 13, 14, 15]
        highs = [value + 0.5 for value in closes]
        lows = [value - 0.5 for value in closes]
        # Am dritten Tag entsteht eine ungefüllte bullische Lücke 11,5 bis 13,0.
        lows[2] = 13.0
        highs[2] = 14.0
        history = pd.DataFrame({"High": highs, "Low": lows, "Close": closes}, index=index)
        result = calculate_mkr_technical_indicators(history)
        self.assertTrue(any(gap.direction == "BULLISH" for gap in result.fair_value_gaps))
        self.assertTrue(any(point.kind == "HIGH" for point in result.swing_points))
        self.assertTrue(any(point.kind == "LOW" for point in result.swing_points))
        self.assertIsNotNone(result.fibonacci)

    def test_mkr_requires_real_ohlc_columns(self) -> None:
        with self.assertRaises(TechnicalAnalysisError):
            calculate_mkr_technical_indicators(self.history([100.0, 101.0]))

    def test_daily_ohlcv_aggregates_to_weekly_and_monthly(self) -> None:
        index = pd.to_datetime(["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-03"])
        daily = pd.DataFrame({
            "Open": [10, 11, 20, 21], "High": [12, 14, 23, 25],
            "Low": [9, 10, 19, 18], "Close": [11, 13, 22, 24],
            "Volume": [100, 200, 300, 400],
        }, index=index)
        weekly = aggregate_ohlcv(daily, "weekly")
        monthly = aggregate_ohlcv(daily, "monthly")
        self.assertEqual(weekly.iloc[0].to_dict(), {
            "Open": 10, "High": 14, "Low": 9, "Close": 13, "Volume": 300})
        self.assertEqual(weekly.iloc[1].to_dict(), {
            "Open": 20, "High": 25, "Low": 18, "Close": 24, "Volume": 700})
        self.assertEqual(monthly.iloc[0].to_dict(), {
            "Open": 10, "High": 14, "Low": 9, "Close": 13, "Volume": 300})
        self.assertEqual(monthly.iloc[1].to_dict(), {
            "Open": 20, "High": 25, "Low": 18, "Close": 24, "Volume": 700})


if __name__ == "__main__":
    unittest.main()
