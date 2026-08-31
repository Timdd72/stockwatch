from pathlib import Path
import tempfile
import unittest

import pandas as pd
from sqlalchemy import select

from database import ProviderCapability,SecurityCatalog,Stock,StockProviderSymbol,create_database,create_session_factory
from market_data.alpha_vantage import AlphaVantageError
from services import ProviderSymbolService
from services import ProviderSettingsService


class FakeAlpha:
    def __init__(self,direct=True,matches=None):
        self.direct=direct;self.matches=matches or [];self.daily_calls=[];self.search_calls=[]
    def get_daily(self,symbol):
        self.daily_calls.append(symbol)
        if not self.direct:raise AlphaVantageError("unknown symbol")
        return pd.DataFrame({"Close":[100.0]})
    def search_symbol(self,keywords):
        self.search_calls.append(keywords);return self.matches


def match(symbol="MBG.DEX",name="Mercedes-Benz Group AG",currency="EUR",region="Frankfurt"):
    return {"1. symbol":symbol,"2. name":name,"4. region":region,"8. currency":currency,"9. matchScore":"0.95"}


class ProviderSymbolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.factory=create_session_factory(create_database(Path(self.tmp.name)/"symbols.db"))
        with self.factory.begin() as s:
            cat=SecurityCatalog(name="Mercedes-Benz Group AG",symbol="MBG",isin="DE0007100000",
                exchange="XETR",country="DE",currency="EUR",security_type="stock",source="XETRA")
            stock=Stock(name=cat.name,symbol="MBG",exchange="XETR",currency="EUR")
            s.add_all((cat,stock));s.flush();self.stock_id=stock.id
    def tearDown(self):self.tmp.cleanup()

    def test_xetra_direct_candidate_is_verified_without_symbol_search(self):
        alpha=FakeAlpha();result=ProviderSymbolService(self.factory,lambda:alpha).resolve_alpha_vantage(self.stock_id)
        self.assertEqual((result.symbol,result.status,result.api_calls),("MBG.DEX","available",1))
        self.assertEqual(alpha.daily_calls,["MBG.DEX"]);self.assertEqual(alpha.search_calls,[])

    def test_fallback_symbol_search_accepts_one_plausible_match(self):
        alpha=FakeAlpha(False,[match()]);result=ProviderSymbolService(self.factory,lambda:alpha).resolve_alpha_vantage(self.stock_id)
        self.assertEqual((result.symbol,result.source,result.api_calls),("MBG.DEX","search",2))
        self.assertEqual(alpha.search_calls,["MBG"])

    def test_uncertain_or_missing_match_is_not_saved_as_symbol(self):
        alpha=FakeAlpha(False,[match("WRONG","Other Company","USD","United States")])
        result=ProviderSymbolService(self.factory,lambda:alpha).resolve_alpha_vantage(self.stock_id)
        self.assertEqual((result.symbol,result.status),(None,"unavailable"))
        with self.factory() as s:self.assertIsNone(s.scalar(select(StockProviderSymbol.symbol)))

    def test_existing_mapping_is_reused_without_provider_creation(self):
        with self.factory.begin() as s:s.add(StockProviderSymbol(stock_id=self.stock_id,provider="alpha_vantage",
            symbol="MBG.DEX",status="available",source="manual"))
        calls=[];result=ProviderSymbolService(self.factory,lambda:calls.append(1)).resolve_alpha_vantage(self.stock_id)
        self.assertEqual((result.symbol,result.api_calls),("MBG.DEX",0));self.assertEqual(calls,[])

    def test_manual_mapping_does_not_change_local_symbol(self):
        service=ProviderSymbolService(self.factory,lambda:FakeAlpha());service.set_manual(self.stock_id,"mbg.dex")
        with self.factory() as s:
            stock=s.get(Stock,self.stock_id);mapping=s.scalar(select(StockProviderSymbol))
        self.assertEqual((stock.symbol,stock.exchange),("MBG","XETR"));self.assertEqual(mapping.symbol,"MBG.DEX")

    def test_changed_symbol_invalidates_only_matching_stock_and_provider(self):
        with self.factory.begin() as s:
            other=Stock(name="Other",symbol="OTH",exchange="XETR",currency="EUR");s.add(other);s.flush();other_id=other.id
            s.add(StockProviderSymbol(stock_id=self.stock_id,provider="alpha_vantage",symbol="MBG",status="available",source="test"))
        settings=ProviderSettingsService(self.factory)
        settings.save_capability(self.stock_id,"alpha_vantage","history","unavailable")
        settings.save_capability(self.stock_id,"finnhub","history","available")
        settings.save_capability(other_id,"alpha_vantage","history","unavailable")
        ProviderSymbolService(self.factory,lambda:FakeAlpha()).set_manual(self.stock_id,"MBG.DEX")
        with self.factory() as s:
            mapping=s.scalar(select(StockProviderSymbol).where(StockProviderSymbol.stock_id==self.stock_id))
            own_alpha=list(s.scalars(select(ProviderCapability).where(ProviderCapability.stock_id==self.stock_id,ProviderCapability.provider=="alpha_vantage")))
            own_finnhub=list(s.scalars(select(ProviderCapability).where(ProviderCapability.stock_id==self.stock_id,ProviderCapability.provider=="finnhub")))
            other_alpha=list(s.scalars(select(ProviderCapability).where(ProviderCapability.stock_id==other_id,ProviderCapability.provider=="alpha_vantage")))
        self.assertEqual(mapping.symbol,"MBG.DEX");self.assertEqual(own_alpha,[])
        self.assertEqual(len(own_finnhub),1);self.assertEqual(len(other_alpha),1)

    def test_same_symbol_keeps_capabilities(self):
        service=ProviderSymbolService(self.factory,lambda:FakeAlpha());service.set_manual(self.stock_id,"MBG.DEX")
        ProviderSettingsService(self.factory).save_capability(self.stock_id,"alpha_vantage","history","available")
        service.set_manual(self.stock_id,"MBG.DEX")
        with self.factory() as s:
            rows=list(s.scalars(select(ProviderCapability).where(ProviderCapability.stock_id==self.stock_id)))
        self.assertEqual(len(rows),1)

    def test_same_symbol_becoming_available_invalidates_stale_capability(self):
        with self.factory.begin() as s:s.add(StockProviderSymbol(stock_id=self.stock_id,provider="alpha_vantage",
            symbol="MBG.DEX",status="unavailable",source="automatic"))
        ProviderSettingsService(self.factory).save_capability(self.stock_id,"alpha_vantage","quote","unavailable")
        ProviderSymbolService(self.factory,lambda:FakeAlpha()).set_manual(self.stock_id,"MBG.DEX")
        with self.factory() as s:
            self.assertEqual(list(s.scalars(select(ProviderCapability))),[])


if __name__=="__main__":unittest.main()
