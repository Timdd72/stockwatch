from __future__ import annotations
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock
from sqlalchemy import select

from database import (OpportunitySearchRun, OpportunityWatchlist, ScannerCandidate, ScannerCandidateTechnical,
    ScannerRun, ScannerTechnicalRun, SecurityCatalog, create_database, create_session_factory)
from services.opportunity_search import OpportunitySearchService
from services.opportunity_news import NewsFirstResult
from services.opportunity_runtime import OpportunityRuntimeConfig,OpportunityTimeout
from services.openai_news_scout import OpenAiScoutResult
from services.scanner_technical import TechnicalVerificationRunning

class OpportunitySearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.factory=create_session_factory(create_database(Path(self.tmp.name)/"x.db"))
        with self.factory.begin() as s:
            sr=ScannerRun(status="COMPLETED",cache_hits=4);s.add(sr);s.flush();self.sr=sr.id
            tr=ScannerTechnicalRun(scanner_run_id=sr.id,status="COMPLETED",cache_hits=2);s.add(tr);s.flush();self.tr=tr.id
            self.ids=[]
            specs=(("ONE",90,80,"POSITIV",50),("TWO",85,75,"POSITIV",72),("THREE",80,70,"NEUTRAL",55),("FALL",90,90,"NEGATIV",30))
            for symbol,q,o,trend,rsi in specs:
                cat=SecurityCatalog(name=symbol,symbol=symbol,exchange="NASDAQ",country="US",currency="USD",security_type="stock",source="NASDAQ",provider_symbol_finnhub=symbol)
                s.add(cat);s.flush();c=ScannerCandidate(scanner_run_id=sr.id,catalog_id=cat.id,symbol=symbol,name=symbol,quality_score=q,opportunity_score=o,status="SHORTLISTED",reason="x");s.add(c);s.flush();self.ids.append(c.id)
                s.add(ScannerCandidateTechnical(technical_run_id=tr.id,scanner_candidate_id=c.id,history_provider="cache",trading_days=100,price=100,performance_20d=2,performance_60d=-20 if symbol=="FALL" else 5,sma20=99,sma50=95,rsi14=rsi,volatility_20d=30,distance_sma20_percent=1,distance_sma50_percent=5,trend=trend,high_20d=105,low_20d=90,distance_high_20d_percent=-5,distance_low_20d_percent=10,momentum_score=60,opportunity_score=o,candidate_type="WATCH",reason="x"))
        self.scanner=Mock();self.scanner.run.return_value=SimpleNamespace(id=self.sr,status="COMPLETED",cache_hits=4)
        self.tech=Mock();self.tech.run.return_value=SimpleNamespace(id=self.tr,status="COMPLETED",cache_hits=2)
        self.ai=Mock();self.ai.config=SimpleNamespace(max_candidates=3)
        assessments=tuple(SimpleNamespace(scanner_candidate_id=i,entry_rating="WATCH",entry_reason="Auf Rücksetzer warten.") for i in self.ids[:3])
        self.ai.run_latest.return_value=SimpleNamespace(assessments=assessments,reused=0)
        self.service=OpportunitySearchService(self.factory,self.scanner,self.tech,self.ai)
    def tearDown(self):self.tmp.cleanup()

    def test_one_call_orchestrates_existing_services_and_only_final_news(self):
        result=self.service.run_search()
        self.scanner.run.assert_called_once()
        self.assertEqual(self.scanner.run.call_args.kwargs["prioritized_catalog_ids"],[])
        self.assertFalse(self.scanner.run.call_args.kwargs["enforce_request_budget"])
        self.tech.run.assert_called_once()
        self.assertFalse(self.tech.run.call_args.kwargs["allow_alpha_vantage"])
        news=self.scanner.load_final_news.call_args.args[0]
        self.assertEqual({x.symbol for x in news},{"ONE","TWO","THREE"})
        self.assertEqual(len(self.ai.run_latest.call_args.args[1]),3)
        self.assertEqual(result.run.status,"COMPLETED")

    def test_falling_knife_is_not_sent_to_openai(self):
        self.service.run_search();ids=self.ai.run_latest.call_args.args[1]
        self.assertNotIn(self.ids[3],ids)

    def test_interesting_watch_is_remembered_without_stock(self):
        self.service.run_search()
        with self.factory() as s:
            rows=list(s.scalars(select(OpportunityWatchlist)))
            self.assertEqual(len(rows),3);self.assertTrue(all(x.active for x in rows))

    def test_manual_favorite_is_prioritized_before_automatic_watch(self):
        with self.factory.begin() as s:
            candidates=list(s.scalars(select(ScannerCandidate).order_by(ScannerCandidate.id)))
            s.add(OpportunityWatchlist(catalog_id=candidates[0].catalog_id,symbol=candidates[0].symbol,
                reason="automatisch",active=True,favorite=False))
            s.add(OpportunityWatchlist(catalog_id=candidates[1].catalog_id,symbol=candidates[1].symbol,
                reason="manuell",active=True,favorite=True,favorited_at=datetime.now(timezone.utc)))
        ids=self.service._watchlist_ids()
        with self.factory() as s:favored=s.get(ScannerCandidate,self.ids[1]).catalog_id
        self.assertEqual(ids[0],favored)

    def test_improvement_marks_new_chance_and_repeated_avoid_deactivates(self):
        c,t=self.service._final_candidates(self.tr)[0]
        with self.factory.begin() as s:s.add(OpportunityWatchlist(catalog_id=c.catalog_id,symbol=c.symbol,
            reason="watch",last_entry_rating="WATCH",last_rsi14=71,weak_checks=0))
        t.rsi14=55
        buy=SimpleNamespace(scanner_candidate_id=c.id,entry_rating="BUY",entry_reason="Jetzt interessant.")
        self.service._update_watchlist([(c,t)],(buy,))
        with self.factory() as s:
            row=s.scalar(select(OpportunityWatchlist).where(OpportunityWatchlist.catalog_id==c.catalog_id));self.assertTrue(row.reason.startswith("NEUE CHANCE"))
        avoid=SimpleNamespace(scanner_candidate_id=c.id,entry_rating="AVOID",entry_reason="Meiden.")
        self.service._update_watchlist([(c,t)],(avoid,));self.service._update_watchlist([(c,t)],(avoid,))
        with self.factory() as s:self.assertFalse(s.scalar(select(OpportunityWatchlist).where(OpportunityWatchlist.catalog_id==c.catalog_id)).active)

    def test_openai_budget_failure_keeps_partial_run(self):
        from ai.service import AiBudgetExceeded
        self.ai.run_latest.side_effect=AiBudgetExceeded("Budget erreicht")
        result=self.service.run_search();self.assertEqual(result.run.status,"PARTIAL_RATE_LIMIT")

    def test_parallel_second_search_is_blocked_before_any_service(self):
        with self.factory.begin() as s:s.add(OpportunitySearchRun(status="RUNNING"))
        with self.assertRaisesRegex(RuntimeError,"bereits"):
            self.service.run_search()
        self.scanner.run.assert_not_called();self.tech.run.assert_not_called();self.ai.run_latest.assert_not_called()

    def test_news_first_precedes_watchlist_and_rotation(self):
        trace=[];news=Mock()
        news.collect.side_effect=lambda *args:(trace.append("NEWS") or NewsFirstResult((3,),1,1,1))
        self.scanner.run.side_effect=lambda **kwargs:(trace.append("ROTATION") or SimpleNamespace(id=self.sr,status="COMPLETED",cache_hits=0,candidates_checked=1))
        self.tech.run.side_effect=lambda *args,**kwargs:(trace.append("TECHNICAL") or SimpleNamespace(id=self.tr,status="COMPLETED",cache_hits=0))
        service=OpportunitySearchService(self.factory,self.scanner,self.tech,self.ai,news=news)
        service.run_search()
        self.assertEqual(trace,["NEWS","ROTATION","TECHNICAL"])
        self.assertEqual(self.scanner.run.call_args.kwargs["prioritized_catalog_ids"],[3])

    def test_timeout_and_stale_lock_are_cleaned(self):
        now=datetime(2026,8,24,12,tzinfo=timezone.utc)
        with self.factory.begin() as s:
            s.add(OpportunitySearchRun(status="RUNNING",started_at=now-timedelta(minutes=20),
                deadline_at=now-timedelta(minutes=10)))
        news=Mock();news.collect.side_effect=OpportunityTimeout("timeout")
        service=OpportunitySearchService(self.factory,self.scanner,self.tech,self.ai,news=news,
            config=OpportunityRuntimeConfig(10,45,3),clock=lambda:now)
        result=service.run_search()
        self.assertEqual(result.run.status,"PARTIAL_RUNTIME_LIMIT")
        with self.factory() as s:
            old=s.scalar(select(OpportunitySearchRun).where(OpportunitySearchRun.id!=result.run.id))
        self.assertEqual(old.status,"PARTIAL_RUNTIME_LIMIT")

    def test_expected_missing_history_can_complete_with_zero_finalists(self):
        self.tech.run.return_value=SimpleNamespace(id=self.tr,status="COMPLETED",cache_hits=0)
        self.ai.run_latest.return_value=SimpleNamespace(assessments=(),reused=0)
        with self.factory.begin() as s:
            rows=list(s.scalars(select(ScannerCandidateTechnical).where(
                ScannerCandidateTechnical.technical_run_id==self.tr)))
            for row in rows:row.opportunity_score=None;row.trend=None;row.history_provider="unavailable"
        result=self.service.run_search()
        self.assertEqual(result.run.status,"COMPLETED")
        self.assertIn("keine ausreichend bestätigte Kaufchance",result.run.message)

    def test_openai_scout_candidates_replace_free_rotation(self):
        scout=Mock();scout.scout.return_value=OpenAiScoutResult((self.ids[0],self.ids[1]),2,2,1)
        service=OpportunitySearchService(self.factory,self.scanner,self.tech,self.ai,scout=scout)
        service.run_search()
        kwargs=self.scanner.run.call_args.kwargs
        self.assertEqual(kwargs["prioritized_catalog_ids"],[self.ids[0],self.ids[1]])
        self.assertEqual(kwargs["max_candidates"],2)
        self.assertFalse(kwargs["enforce_request_budget"])
        self.assertFalse(self.tech.run.call_args.kwargs["allow_alpha_vantage"])

    def test_empty_scout_uses_only_limited_rotation_fallback(self):
        scout=Mock();scout.scout.return_value=OpenAiScoutResult((),0,0,1)
        service=OpportunitySearchService(self.factory,self.scanner,self.tech,self.ai,scout=scout)
        service.run_search()
        self.assertEqual(self.scanner.run.call_args.kwargs["max_candidates"],35)

    def test_failure_keeps_the_actual_phase_instead_of_claiming_completed(self):
        self.tech.run.side_effect=TechnicalVerificationRunning("Es läuft bereits eine technische Prüfung.")
        result=self.service.run_search()
        self.assertEqual(result.run.status,"FAILED")
        self.assertEqual(result.run.phase,"TECHNICAL")
