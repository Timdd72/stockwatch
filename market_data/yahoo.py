"""Yahoo-Finance-Implementierung der Marktdaten-Schnittstelle."""

from __future__ import annotations

import yfinance as yf

from .provider import MarketDataProvider, Quote


class YahooProvider(MarketDataProvider):
    """Ruft Marktdaten über yfinance ab."""

    def get_quote(self, symbol: str) -> Quote:
        symbol = self._validate_symbol(symbol)
        ticker = yf.Ticker(symbol)
        history = ticker.history(period="1d", interval="1d", auto_adjust=False)
        if history.empty or history["Close"].dropna().empty:
            raise RuntimeError(f"Für {symbol} wurde kein Kurs geliefert.")

        info = ticker.info
        return Quote(
            symbol=info.get("symbol") or symbol,
            price=float(history["Close"].dropna().iloc[-1]),
            currency=info.get("currency"),
            name=info.get("longName") or info.get("shortName"),
            exchange=(
                info.get("fullExchangeName")
                or info.get("exchangeName")
                or info.get("exchange")
            ),
        )

    def get_history(self, symbol: str, days: int):
        symbol = self._validate_symbol(symbol)
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("days muss eine positive ganze Zahl sein.")

        return yf.Ticker(symbol).history(
            period=f"{days}d",
            interval="1d",
            auto_adjust=False,
        )

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol darf nicht leer sein.")
        return symbol.strip().upper()
