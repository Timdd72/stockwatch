from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile,unittest
from unittest.mock import Mock

from sqlalchemy import select

from ai.config import AiConfig
from ai.news_scout_schemas import NewsScoutOutput
from ai.service import AiAnalysisService,AiBudgetExceeded,StructuredAiRun
from database import AiUsage,OpportunityNewsEvent,SecurityCatalog,create_database,create_session_factory
from services.openai_news_scout import OpenAiNewsScoutService

def scout_output():
    base={"event_type":"LARGE_ORDER","headline":"Large order confirmed","summary":"A concrete contract was announced.",
        "published_at":"2026-08-24T10:00:00Z","source_name":"Primary source","expected_direction":"POSITIVE",
        "relevance_score":90,"reason":"Material revenue impact may be possible."}
    return NewsScoutOutput.model_validate({"candidates":[
        {**base,"company_name":"Acme","symbol":"ACME","source_url":"https://example.com/acme"},
        {**base,"company_name":"Unknown","symbol":"NOPE","source_url":"https://example.com/nope"}]})

class FakeAi:
    def __init__(self):self.calls=[]
    def request_structured(self,*args,**kwargs):
        self.calls.append((args,kwargs));return StructuredAiRun(scout_output(),100,30,130,Decimal("0.001"),2)

class OpenAiNewsScoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.factory=create_session_factory(create_database(Path(self.tmp.name)/"s.db"))
        with self.factory.begin() as s:s.add(SecurityCatalog(name="Acme",symbol="ACME",exchange="NASDAQ",country="US",currency="USD",security_type="stock",source="NASDAQ",provider_symbol_finnhub="ACME"))
    def tearDown(self):self.tmp.cleanup()

    def test_web_search_is_limited_sources_saved_and_unknown_symbol_dropped(self):
        ai=FakeAi();result=OpenAiNewsScoutService(self.factory,ai,5).scout(1)
        kwargs=ai.calls[0][1]
        self.assertEqual(kwargs["model"],"gpt-5.6-luna")
        self.assertEqual(kwargs["tools"],[{"type":"web_search"}]);self.assertEqual(kwargs["max_tool_calls"],4)
        self.assertEqual(result.symbols_identified,1);self.assertEqual(result.web_search_calls,2)
        with self.factory() as s:event=s.scalar(select(OpportunityNewsEvent))
        self.assertEqual(event.symbol,"ACME");self.assertEqual(event.url,"https://example.com/acme")
        self.assertEqual(event.provider,"openai_web_search")

    def test_ai_usage_records_web_tool_calls_and_budget_blocks_request(self):
        response=SimpleNamespace(output_parsed=scout_output(),usage=SimpleNamespace(input_tokens=100,output_tokens=20,total_tokens=120),
            output=[SimpleNamespace(type="web_search_call"),SimpleNamespace(type="message")])
        responses=Mock();responses.parse.return_value=response;client=Mock();client.responses=responses
        config=AiConfig("normal",Decimal("8"),Decimal("1"),Decimal("2"),24,Decimal("0.01"))
        ai=AiAnalysisService(self.factory,config,lambda:client)
        OpenAiNewsScoutService(self.factory,ai,5).scout(1)
        with self.factory() as s:usage=s.scalar(select(AiUsage))
        self.assertEqual(usage.purpose,"opportunity_web_scout");self.assertEqual(usage.tool_type,"web_search")
        self.assertEqual(usage.tool_calls,1);self.assertGreater(usage.estimated_cost_eur,Decimal("0.01"))
        blocked=AiAnalysisService(self.factory,AiConfig("normal",Decimal("0"),Decimal("1"),Decimal("2")),lambda:Mock())
        with self.assertRaises(AiBudgetExceeded):OpenAiNewsScoutService(self.factory,blocked,5).scout(2)

    def test_more_than_five_actual_web_calls_is_rejected(self):
        ai=FakeAi()
        ai.request_structured=lambda *args,**kwargs:StructuredAiRun(
            scout_output(),100,30,130,Decimal("0.001"),6)
        from ai.service import AiAnalysisError
        with self.assertRaises(AiAnalysisError):
            OpenAiNewsScoutService(self.factory,ai,5).scout(1)
        with self.factory() as s:self.assertIsNone(s.scalar(select(OpportunityNewsEvent)))

if __name__=="__main__":unittest.main()
