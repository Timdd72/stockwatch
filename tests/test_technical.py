"""Unit-Tests für die lokalen technischen Kennzahlen."""

from __future__ import annotations

import unittest

import pandas as pd

from analysis.technical import TechnicalAnalysisError, calculate_technical_indicators


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


if __name__ == "__main__":
    unittest.main()
