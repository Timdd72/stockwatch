"""Minimaler Abruf von Airbus-Kursdaten mit yfinance."""

from __future__ import annotations

import logging
import sys

import pandas as pd
import yfinance as yf


SYMBOL = "AIR.PA"


def main() -> int:
    try:
        # yfinance protokolliert Netzwerkfehler sonst zusätzlich ungefiltert auf stderr.
        yf.utils.get_yf_logger().setLevel(logging.CRITICAL)
        aktie = yf.Ticker(SYMBOL)
        kurse = aktie.history(period="5d", interval="1d", auto_adjust=False)

        if kurse.empty:
            raise RuntimeError(f"Für {SYMBOL} wurden keine Kursdaten geliefert.")

        # Nicht für die kompakte Terminalausgabe benötigte Spalten ausblenden.
        spalten = ["Open", "High", "Low", "Close", "Volume"]
        ausgabe = kurse.loc[:, spalten].copy()
        ausgabe.index = ausgabe.index.strftime("%Y-%m-%d")
        ausgabe.index.name = "Datum"
        ausgabe.columns = ["Eröffnung", "Hoch", "Tief", "Schluss", "Volumen"]

        info = aktie.info
        name = info.get("longName") or info.get("shortName") or "Unbekannt"
        symbol = info.get("symbol") or SYMBOL
        waehrung = info.get("currency") or "Unbekannt"
        boerse = (
            info.get("fullExchangeName")
            or info.get("exchangeName")
            or info.get("exchange")
            or "Unbekannt"
        )
        letzter_kurs = float(kurse["Close"].dropna().iloc[-1])

        print("Airbus SE – grundlegende Informationen")
        print(f"Name:                    {name}")
        print(f"Symbol:                  {symbol}")
        print(f"Währung:                 {waehrung}")
        print(f"Börse:                   {boerse}")
        print(f"Letzter verfügbarer Kurs: {letzter_kurs:.2f} {waehrung}")
        print("\nKursdaten der letzten 5 Handelstage:")
        with pd.option_context(
            "display.max_columns", None,
            "display.width", 120,
            "display.float_format", "{:.2f}".format,
        ):
            print(ausgabe.to_string())

        return 0
    except Exception as exc:
        print(
            f"Fehler beim Abruf der Yahoo-Finance-Daten für {SYMBOL}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
