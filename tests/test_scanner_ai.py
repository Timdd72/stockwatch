from __future__ import annotations
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from pydantic import ValidationError
from sqlalchemy import select

from ai.config import AiConfig
from ai.scanner_schemas import ScannerEntryOutput
from ai.service import AiAnalysisService, AiBudgetExceeded, StructuredAiRun
from database import (AiUsage, ScannerAiAssessment, ScannerCandidate, ScannerCandidateTechnical,
    ScannerRun, ScannerTechnicalRun, SecurityCatalog, create_database, create_session_factory)
from services.scanner_ai import ScannerAiConfig, ScannerAiService

def result(rating="BUY"):
    return ScannerEntryOutput.model_validate({"entry_rating":rating,"entry_confidence":.8,
        "outlook":"POSITIVE","risk":"MEDIUM","summary":"Solide Chance.",
        "entry_reason":"Einstieg ist vertretbar.","reasons":["Qualität"],"risks":["Volatilität"],
        "entry_zone":None,"target_zone":None,"stop_zone":None,"time_horizon":"1-3 months","relevant_news":[]})

class FakeAi:
    def __init__(self):
        self.config=SimpleNamespace(model="test-model"); self.calls=[]
    def request_structured(self, canonical, schema, instructions, **kwargs):
        self.calls.append((canonical,schema,instructions,kwargs))
        return StructuredAiRun(result(),100,20,120,Decimal("0.001"))

class ScannerAiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); path=Path(self.tmp.name)/"db.sqlite"
        self.factory=create_session_factory(create_database(path)); self.ai=FakeAi()
        with self.factory.begin() as s:
            run=ScannerRun(status="COMPLETED"); s.add(run); s.flush()
            tr=ScannerTechnicalRun(scanner_run_id=run.id,status="COMPLETED"); s.add(tr); s.flush()
            self.tr_id=tr.id
            for i,(symbol,score,rsi,verified) in enumerate((("ACAD",86,71,True),("AAPL",78,54,True),("NOPE",99,50,False))):
                cat=SecurityCatalog(name=symbol,symbol=symbol,exchange="NASDAQ",country="US",currency="USD",security_type="stock",source="NASDAQ")
                s.add(cat); s.flush()
                c=ScannerCandidate(scanner_run_id=run.id,catalog_id=cat.id,symbol=symbol,name=symbol,
                    quality_score=90,opportunity_score=score,status="SHORTLISTED",reason="test")
                s.add(c); s.flush()
                if verified:
                    s.add(ScannerCandidateTechnical(technical_run_id=tr.id,scanner_candidate_id=c.id,
                        history_provider="alpha_vantage",trading_days=100,price=100,performance_5d=2,
                        performance_20d=21 if symbol=="ACAD" else -2,performance_60d=10,sma20=99,sma50=95,
                        rsi14=rsi,volatility_20d=30,distance_sma20_percent=1,distance_sma50_percent=5,
                        trend="POSITIV",high_20d=100,low_20d=90,distance_high_20d_percent=0 if symbol=="ACAD" else -7,
                        distance_low_20d_percent=10,momentum_score=70,opportunity_score=score,
                        candidate_type="WATCH" if symbol=="ACAD" else "QUALITY_PULLBACK",reason="technical"))
    def tearDown(self): self.tmp.cleanup()

    def test_only_verified_and_maximum_are_analyzed(self):
        service=ScannerAiService(self.factory,self.ai,ScannerAiConfig(max_candidates=2))
        run=service.run_latest(self.tr_id)
        self.assertEqual({x.symbol for x in run.assessments},{"ACAD","AAPL"})
        self.assertEqual(len(self.ai.calls),2)
        self.assertIn('"price_type":"DAILY_CLOSE"',self.ai.calls[0][0])

    def test_overheated_candidate_is_downgraded_to_watch(self):
        run=ScannerAiService(self.factory,self.ai).run_latest(self.tr_id)
        acad=next(x for x in run.assessments if x.symbol=="ACAD")
        self.assertEqual(acad.entry_rating,"WATCH")

    def test_fingerprint_prevents_second_request(self):
        service=ScannerAiService(self.factory,self.ai)
        service.run_latest(self.tr_id); second=service.run_latest(self.tr_id)
        self.assertEqual(len(self.ai.calls),2); self.assertEqual(second.reused,2)

    def test_schema_rejects_position_rating_and_hold(self):
        payload=result().model_dump(); payload["entry_rating"]="HOLD"
        with self.assertRaises(ValidationError): ScannerEntryOutput.model_validate(payload)
        payload=result().model_dump(); payload["position_rating"]="HOLD"
        with self.assertRaises(ValidationError): ScannerEntryOutput.model_validate(payload)

    def test_existing_stock_analysis_budget_blocks_generic_network_call(self):
        config=AiConfig("test",Decimal("0"),Decimal("1"),Decimal("1"))
        client=Mock(); service=AiAnalysisService(self.factory,config,lambda:client)
        with self.assertRaises(AiBudgetExceeded):
            service.request_structured("{}",ScannerEntryOutput,"test",purpose="scanner_opportunity")
        client.assert_not_called()

    def test_assessments_are_historical_rows(self):
        service=ScannerAiService(self.factory,self.ai); service.run_latest(self.tr_id)
        with self.factory() as s:
            self.assertEqual(len(list(s.scalars(select(ScannerAiAssessment)))),2)
