"""Netzwerkfreie Tests für die Marktdaten-Provider."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import pandas as pd

from market_data import MarketDataProvider, Quote, YahooProvider


class MarketDataProviderTests(unittest.TestCase):
    def test_base_class_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            MarketDataProvider()


class YahooProviderTests(unittest.TestCase):
    @patch("market_data.yahoo.yf.Ticker")
    def test_get_quote_normalizes_yahoo_data(self, ticker_factory: Mock) -> None:
        ticker = ticker_factory.return_value
        ticker.history.return_value = pd.DataFrame({"Close": [123.45]})
        ticker.info = {
            "symbol": "AIR.PA",
            "longName": "Airbus SE",
            "currency": "EUR",
            "fullExchangeName": "Euronext Paris",
        }

        quote = YahooProvider().get_quote(" air.pa ")

        self.assertEqual(
            quote,
            Quote("AIR.PA", 123.45, "EUR", "Airbus SE", "Euronext Paris"),
        )
        ticker_factory.assert_called_once_with("AIR.PA")
        ticker.history.assert_called_once_with(
            period="1d", interval="1d", auto_adjust=False
        )

    @patch("market_data.yahoo.yf.Ticker")
    def test_get_history_uses_requested_period(self, ticker_factory: Mock) -> None:
        expected = pd.DataFrame({"Close": [100.0, 101.0]})
        ticker_factory.return_value.history.return_value = expected

        result = YahooProvider().get_history("air.pa", 5)

        self.assertIs(result, expected)
        ticker_factory.return_value.history.assert_called_once_with(
            period="5d", interval="1d", auto_adjust=False
        )

    def test_rejects_invalid_arguments_without_network_access(self) -> None:
        provider = YahooProvider()
        for symbol in ("", "   ", None):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                provider.get_history(symbol, 5)  # type: ignore[arg-type]
        for days in (0, -1, 1.5, True):
            with self.subTest(days=days), self.assertRaises(ValueError):
                provider.get_history("AIR.PA", days)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
