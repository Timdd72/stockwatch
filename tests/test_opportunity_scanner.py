from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json, tempfile, unittest
from unittest.mock import Mock
from sqlalchemy import select

from database import (ApiLimits, ApiUsage, ApiUsageService, OpportunityWatchlist, ProviderCache, ScannerRun, ScannerSecurityState,
    SecurityCatalog, create_database, create_session_factory)
from market_data.finnhub import EndpointResult
from market_data.finnhub import FinnhubProvider
from services.opportunity_scanner import (OpportunityScannerService, ScannerAlreadyRunning,
    CandidateWork, ScannerConfig, opportunity_score, quality_score)
from unittest.mock import patch

class FakeFinnhub:
    def __init__(self, payloads=None): self.calls=[]; self.payloads=payloads or {}
    def request(self, area, endpoint, **params):
        self.calls.append((endpoint, params.get("symbol")))
        value=self.payloads.get((endpoint, params.get("symbol")), self.payloads.get(endpoint, {}))
        if isinstance(value, EndpointResult): return value
        return EndpointResult(endpoint, "verfügbar", value)

def metric(cap=50_000, eps=5, missing=False):
    values={"marketCapitalization":cap,"peTTM":22,"epsTTM":eps,"revenueGrowthTTMYoy":12,
        "netIncomeGrowthTTMYoy":8,"roeTTM":24,"totalDebt/totalEquityQuarterly":.4,
        "52WeekHigh":120,"52WeekLow":80}
    if missing:
        for key in ("roeTTM","netIncomeGrowthTTMYoy","52WeekHigh","52WeekLow"): values.pop(key)
    return {"metric":values}

class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/"scan.db"
        self.factory=create_session_factory(create_database(self.path))
        with self.factory.begin() as s:
            for symbol,name in (("AAA","Alpha Common Stock"),("BBB","Beta Class A Common Stock"),("CCC","Gamma Ordinary Shares")):
                s.add(SecurityCatalog(name=name,symbol=symbol,exchange="NASDAQ",country="US",currency="USD",
                    security_type="stock",source="NASDAQ",provider_symbol_finnhub=symbol,provider_symbol_alpha_vantage=symbol))
            s.add(SecurityCatalog(name="Fund ETF",symbol="ETF1",exchange="NASDAQ",country="US",currency="USD",
                security_type="etf",source="NASDAQ",provider_symbol_finnhub="ETF1"))
            s.add(SecurityCatalog(name="Bad Warrant",symbol="WAR",exchange="NASDAQ",country="US",currency="USD",
                security_type="warrant",source="NASDAQ",provider_symbol_finnhub="WAR"))
        self.usage=ApiUsageService(self.factory,ApiLimits(25,60))
    def tearDown(self): self.tmp.cleanup()

    def test_local_prefilter_has_no_requests_and_excludes_non_stocks(self):
        fake=FakeFinnhub(); svc=OpportunityScannerService(self.factory,self.usage,finnhub_factory=lambda:fake)
        self.assertEqual(svc.catalog_counts(),(5,3)); self.assertEqual(set(x.symbol for x in svc.rotation()),{"AAA","BBB","CCC"})
        self.assertEqual(fake.calls,[])

    def test_quality_threshold_scores_and_missing_data(self):
        good=quality_score({"market_cap":50e9,"pe_ratio":22,"eps":5,"revenue_growth":12,"profit_growth":8,"roe":24,"debt_to_equity":.4,"week_52_high":120,"week_52_low":80})
        repeated=quality_score({"market_cap":50e9,"pe_ratio":22,"eps":5,"revenue_growth":12,"profit_growth":8,"roe":24,"debt_to_equity":.4,"week_52_high":120,"week_52_low":80})
        sparse=quality_score({"market_cap":50e9,"pe_ratio":None,"eps":5,"revenue_growth":None,"profit_growth":None,"roe":None,"debt_to_equity":None,"week_52_high":None,"week_52_low":None})
        tiny=quality_score({"market_cap":1e9,"eps":2})
        self.assertEqual(good,repeated); self.assertGreater(good.data_quality,sparse.data_quality)
        self.assertIn("2 Milliarden",tiny.excluded_reason)
        self.assertEqual(opportunity_score(80,60,70,50,50),opportunity_score(80,60,70,50,50))
        with self.factory() as s:
            catalog=s.scalar(select(SecurityCatalog).where(SecurityCatalog.symbol=="AAA"))
            kind,_=OpportunityScannerService._classify(CandidateWork(catalog,{},80,80,5,momentum=45,technical_available=False))
        self.assertEqual(kind,"WATCH")

    def test_cache_is_reused_and_budget_is_never_exceeded(self):
        now=datetime.now(timezone.utc)
        with self.factory.begin() as s:
            s.add(ProviderCache(provider="finnhub",symbol="AAA",data_type="scanner_fundamentals",fetched_at=now,
                payload=json.dumps({"availability":"verfügbar","data":metric()})))
        fake=FakeFinnhub({"stock/metric":metric(),"quote":{"c":100},"stock/recommendation":[],"stock/earnings":[]})
        cfg=ScannerConfig(max_requests=2,max_quality_checks=3,max_shortlist=15)
        run=OpportunityScannerService(self.factory,self.usage,cfg,lambda:fake).run()
        self.assertLessEqual(run.finnhub_requests,2); self.assertGreaterEqual(run.cache_hits,1); self.assertEqual(run.status,"PARTIAL_RATE_LIMIT")

    def test_rotation_prefers_never_checked_then_oldest(self):
        with self.factory.begin() as s:
            aaa=s.scalar(select(SecurityCatalog).where(SecurityCatalog.symbol=="AAA")); bbb=s.scalar(select(SecurityCatalog).where(SecurityCatalog.symbol=="BBB"))
            s.add(ScannerSecurityState(catalog_id=aaa.id,last_checked_at=datetime.now(timezone.utc)-timedelta(days=1)))
            s.add(ScannerSecurityState(catalog_id=bbb.id,last_checked_at=datetime.now(timezone.utc)-timedelta(days=2)))
        order=OpportunityScannerService(self.factory,self.usage,finnhub_factory=lambda:FakeFinnhub()).rotation()
        self.assertEqual(order[0].symbol,"CCC")
        self.assertEqual([x.symbol for x in order[1:]],["BBB","AAA"])

    def test_rotation_continues_deterministically_after_checked_batch(self):
        with self.factory.begin() as s:
            for symbol in ("A10","A20","M10","T10","Z10"):
                s.add(SecurityCatalog(name=symbol,symbol=symbol,exchange="NASDAQ",country="US",currency="USD",
                    security_type="stock",source="NASDAQ",provider_symbol_finnhub=symbol))
        service=OpportunityScannerService(self.factory,self.usage,finnhub_factory=lambda:FakeFinnhub())
        first=service.rotation()[:3]
        self.assertEqual([x.symbol for x in first],[x.symbol for x in service.rotation()[:3]])
        with self.factory.begin() as s:
            for item in first:s.add(ScannerSecurityState(catalog_id=item.id,last_checked_at=datetime.now(timezone.utc)))
        second=service.rotation()[:3]
        self.assertTrue(set(x.id for x in first).isdisjoint(x.id for x in second))

    def test_default_request_budget_is_200(self):
        with patch.dict("os.environ",{},clear=True):
            self.assertEqual(ScannerConfig.from_env().max_requests,200)

    def test_active_watchlist_is_checked_before_free_rotation(self):
        with self.factory.begin() as s:
            bbb=s.scalar(select(SecurityCatalog).where(SecurityCatalog.symbol=="BBB"))
            s.add(OpportunityWatchlist(catalog_id=bbb.id,symbol="BBB",reason="weiter beobachten",active=True))
        order=OpportunityScannerService(self.factory,self.usage,finnhub_factory=lambda:FakeFinnhub()).rotation()
        self.assertEqual(order[0].symbol,"BBB")

    def test_running_scan_prevents_parallel_second_scan(self):
        with self.factory.begin() as s: s.add(ScannerRun(status="RUNNING",market="NASDAQ"))
        fake=FakeFinnhub(); svc=OpportunityScannerService(self.factory,self.usage,finnhub_factory=lambda:fake)
        with self.assertRaises(ScannerAlreadyRunning): svc.run()
        self.assertEqual(fake.calls,[])

    def test_premium_capability_is_stored_and_not_retested(self):
        premium=EndpointResult("stock/metric","Premium-Zugang erforderlich")
        fake=FakeFinnhub({"stock/metric":premium}); cfg=ScannerConfig(max_requests=5,max_quality_checks=3)
        first=OpportunityScannerService(self.factory,self.usage,cfg,lambda:fake).run(); self.assertEqual(first.finnhub_requests,3)
        second=OpportunityScannerService(self.factory,self.usage,cfg,lambda:fake).run(); self.assertEqual(second.finnhub_requests,0)
        self.assertEqual(second.premium_skips,3)

    def test_existing_finnhub_minute_limit_prevents_network(self):
        now=datetime.now(timezone.utc)
        with self.factory.begin() as s:
            for _ in range(2):
                s.add(ApiUsage(provider="finnhub",endpoint="quote",timestamp=now,success=True))
        usage=ApiUsageService(self.factory,ApiLimits(25,2),clock=lambda:now)
        provider=FinnhubProvider("not-a-secret",usage_recorder=usage)
        provider._session.get=Mock()
        run=OpportunityScannerService(self.factory,usage,ScannerConfig(max_requests=50,max_quality_checks=1),lambda:provider,lambda:now).run()
        self.assertEqual(run.status,"PARTIAL_RATE_LIMIT"); self.assertEqual(run.finnhub_requests,0)
        provider._session.get.assert_not_called()

if __name__=="__main__": unittest.main()
