from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile, unittest
import pandas as pd
from sqlalchemy import func, select

from analysis import TechnicalIndicators
from database import (ApiLimits, ApiUsageService, ScannerCandidate, ScannerCandidateTechnical,
    ScannerRun, ScannerTechnicalRun, SecurityCatalog, Stock, create_database, create_session_factory)
from market_data.usage import ApiLimitExceeded
from services.market_updates import MarketUpdateError
from services.scanner_technical import (ScannerTechnicalService, TechnicalVerificationConfig,
    classify_technical, technical_momentum)

class FakeMarket:
    def __init__(self, history, finnhub_error=False, limit=False):
        self.history=history; self.finnhub_error=finnhub_error; self.limit=limit; self.calls=[]
    def load_history(self,provider,symbol):
        self.calls.append((provider,symbol))
        if self.limit: raise ApiLimitExceeded("Limit")
        if provider=="finnhub" and self.finnhub_error: raise MarketUpdateError("nicht freigeschaltet")
        return self.history.copy()

class ScannerTechnicalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/"technical.db"
        self.factory=create_session_factory(create_database(self.path)); self.run_id=0
        with self.factory.begin() as s:
            run=ScannerRun(status="COMPLETED",market="NASDAQ",candidates_shortlisted=6); s.add(run); s.flush(); self.run_id=run.id
            for index,symbol in enumerate(("AAA","BBB","CCC","DDD","EEE","FFF")):
                cat=SecurityCatalog(name=f"{symbol} Inc",symbol=symbol,exchange="NASDAQ",country="US",currency="USD",security_type="stock",source="NASDAQ",provider_symbol_finnhub=symbol,provider_symbol_alpha_vantage=symbol)
                s.add(cat); s.flush()
                s.add(ScannerCandidate(scanner_run_id=run.id,catalog_id=cat.id,symbol=symbol,name=cat.name,
                    quality_score=95-index,opportunity_score=60+index,fundamental_score=80,analyst_score=70,
                    momentum_score=45,risk_score=50,status="SHORTLISTED",candidate_type="WATCH",reason="test"))
        self.history=pd.DataFrame({"Close":[100+i*.2 for i in range(250)],"High":[101+i*.2 for i in range(250)],"Low":[99+i*.2 for i in range(250)]})
    def tearDown(self):self.tmp.cleanup()

    def test_only_top_five_and_no_watchlist_stocks(self):
        market=FakeMarket(self.history); service=ScannerTechnicalService(self.factory,market)
        run=service.run(self.run_id)
        self.assertEqual(run.candidates_checked,5); self.assertEqual(len(market.calls),5)
        with self.factory() as s:
            symbols=set(s.scalars(select(ScannerCandidateTechnical.history_provider)))
            stock_count=s.scalar(select(func.count()).select_from(Stock))
        self.assertEqual(symbols,{"finnhub"}); self.assertEqual(stock_count,0)

    def test_finnhub_premium_uses_existing_alpha_fallback(self):
        market=FakeMarket(self.history,finnhub_error=True); service=ScannerTechnicalService(self.factory,market)
        run=service.run(self.run_id); self.assertEqual(run.candidates_checked,5)
        self.assertEqual(market.calls[:2],[("finnhub","AAA"),("alpha_vantage","AAA")])
        with self.factory() as s:
            providers=set(s.scalars(select(ScannerCandidateTechnical.history_provider)))
        self.assertEqual(providers,{"alpha_vantage"})

    def test_central_search_mode_never_calls_alpha_vantage(self):
        market=FakeMarket(self.history,finnhub_error=True)
        run=ScannerTechnicalService(self.factory,market).run(self.run_id,allow_alpha_vantage=False)
        self.assertEqual(run.status,"PARTIAL_DATA")
        self.assertTrue(market.calls)
        self.assertTrue(all(provider=="finnhub" for provider,_ in market.calls))

    def test_fresh_history_cache_is_reused(self):
        market=FakeMarket(self.history); first=ScannerTechnicalService(self.factory,market).run(self.run_id)
        calls=len(market.calls); second=ScannerTechnicalService(self.factory,market).run(self.run_id)
        self.assertEqual(len(market.calls),calls); self.assertEqual(second.cache_hits,5)

    def test_technical_values_and_opportunity_are_recalculated(self):
        market=FakeMarket(self.history); run=ScannerTechnicalService(self.factory,market).run(self.run_id)
        with self.factory() as s:
            row=s.scalar(select(ScannerCandidateTechnical).where(ScannerCandidateTechnical.technical_run_id==run.id))
            original=s.get(ScannerCandidate,row.scanner_candidate_id)
        self.assertAlmostEqual(row.price,149.8); self.assertIsNotNone(row.sma20); self.assertIsNotNone(row.sma200)
        self.assertEqual(row.high_20d,149.8); self.assertEqual(row.low_20d,146.0)
        self.assertNotEqual(row.opportunity_score,original.opportunity_score)

    def test_pullback_is_explainable_and_negative_momentum_stays_watch(self):
        pullback=TechnicalIndicators(98,-1,-2,-4,8,100,97,90,48,25,-2,1,"NEUTRAL")
        kind,_=classify_technical(85,pullback,-8); self.assertEqual(kind,"QUALITY_PULLBACK")
        negative=TechnicalIndicators(70,-3,-8,-15,-25,80,90,100,28,60,-12,-22,"NEGATIV")
        kind,_=classify_technical(90,negative,-30); self.assertEqual(kind,"WATCH")
        self.assertLess(technical_momentum(negative),50)

    def test_api_limit_stops_without_followup(self):
        market=FakeMarket(self.history,limit=True); run=ScannerTechnicalService(self.factory,market).run(self.run_id)
        self.assertEqual(run.status,"PARTIAL_RATE_LIMIT"); self.assertEqual(run.candidates_checked,0); self.assertEqual(len(market.calls),1)

    def test_missing_finnhub_history_is_nullable_and_remaining_candidates_continue(self):
        class OneMissing(FakeMarket):
            def load_history(inner,provider,symbol):
                inner.calls.append((provider,symbol))
                if symbol=="AAA":raise MarketUpdateError("Premium: nicht freigeschaltet")
                return inner.history.copy()
        market=OneMissing(self.history)
        run=ScannerTechnicalService(self.factory,market).run(
            self.run_id,allow_alpha_vantage=False,preserve_unavailable=True)
        self.assertEqual(run.status,"COMPLETED");self.assertEqual(run.candidates_checked,5)
        self.assertIn("tarifbedingt",run.message)
        with self.factory() as s:
            rows=list(s.scalars(select(ScannerCandidateTechnical).where(
                ScannerCandidateTechnical.technical_run_id==run.id)))
        self.assertEqual(len(rows),5)
        missing=next(row for row in rows if row.history_provider=="unavailable")
        self.assertEqual(missing.trading_days,0);self.assertEqual(missing.candidate_type,"WATCH")
        for field in ("trend","performance_5d","performance_20d","performance_60d","sma20",
                      "sma50","sma200","rsi14","volatility_20d","momentum_score","opportunity_score"):
            self.assertIsNone(getattr(missing,field),field)

    def test_stale_running_technical_lock_is_cleaned(self):
        with self.factory.begin() as s:
            stale=ScannerTechnicalRun(scanner_run_id=self.run_id,status="RUNNING",
                started_at=datetime.now(timezone.utc)-timedelta(minutes=20))
            s.add(stale);s.flush();stale_id=stale.id
        run=ScannerTechnicalService(self.factory,FakeMarket(self.history)).run(self.run_id)
        self.assertEqual(run.status,"COMPLETED")
        with self.factory() as s:old=s.get(ScannerTechnicalRun,stale_id)
        self.assertEqual(old.status,"PARTIAL_DATA");self.assertIsNotNone(old.finished_at)

    def test_unexpected_technical_error_never_leaves_running_lock(self):
        broken=pd.DataFrame({"Wrong":[1,2,3]})
        with self.assertRaises(Exception):
            ScannerTechnicalService(self.factory,FakeMarket(broken)).run(self.run_id)
        with self.factory() as s:
            run=s.scalar(select(ScannerTechnicalRun).order_by(ScannerTechnicalRun.id.desc()))
        self.assertEqual(run.status,"FAILED");self.assertIsNotNone(run.finished_at)

if __name__=="__main__":unittest.main()
