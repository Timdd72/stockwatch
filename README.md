# Stockwatch

Dieses Projekt bereitet einen austauschbaren Zugriff auf Marktdaten vor. Eine
Weboberfläche, Datenbank oder KI-Komponente ist derzeit bewusst nicht enthalten.

## MarketDataProvider

`MarketDataProvider` definiert zunächst zwei Operationen:

- `get_quote(symbol)` liefert einen normalisierten `Quote` mit dem zuletzt
  verfügbaren Kurs.
- `get_history(symbol, days)` liefert die Tageshistorie des Providers. Beim
  `YahooProvider` ist das ein `pandas.DataFrame`.

`YahooProvider` implementiert diese Schnittstelle mit `yfinance`. Weitere
Datenquellen werden erst ergänzt, wenn ein passender API-Key vorliegt. Verbraucher
sollten gegen `MarketDataProvider` statt direkt gegen `YahooProvider` entwickelt
werden, damit sich die Quelle später austauschen lässt.

## Bekannte Einschränkung von Yahoo Finance

Yahoo Finance beziehungsweise `yfinance` wird von diesem Server derzeit mit
HTTP 429 (Too Many Requests) blockiert. Der vorhandene eigenständige Test
`yfinance_test.py` bleibt zur Diagnose erhalten, benötigt aber echten
Netzwerkzugriff und kann in dieser Umgebung deshalb fehlschlagen.

Die automatisierten Tests unter `tests/` ersetzen den Yahoo-Zugriff durch Mocks
und benötigen keinen externen Netzwerkzugriff:

```bash
python -m unittest discover -v
```

## Opportunity-Scanner

Der manuell gestartete News-first-Lauf verwendet zentral folgende optionale
`.env`-Werte (die angegebenen Werte sind zugleich die Defaults):

```dotenv
OPPORTUNITY_MAX_RUNTIME_MINUTES=10
OPPORTUNITY_FINNHUB_REQUESTS_PER_MINUTE=45
OPPORTUNITY_MAX_AI_CANDIDATES=3
OPPORTUNITY_MAX_WEB_SEARCH_CALLS=5
```

Der Opportunity-Scanner verwendet kein Alpha Vantage. Fehlende Finnhub-Historie
wird transparent als nicht verfügbar gespeichert; Alpha Vantage bleibt nur für
überwachte Einzelaktien verfügbar.

Der primäre Scout verwendet ausschließlich in seiner ersten Phase
`gpt-5.6-luna` mit dem Responses-API-Tool `web_search`. Die spätere
Scanner-Bewertung erhält keine Tools und arbeitet nur mit den zuvor gespeicherten
Quellen und lokalen Marktdaten. Eine optionale Toolkostenschätzung kann zentral
über `OPENAI_WEB_SEARCH_COST_EUR_PER_CALL` gepflegt werden; maßgeblich bleibt das
externe OpenAI-Billing.
