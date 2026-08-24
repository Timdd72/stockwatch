from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile,unittest

from sqlalchemy import func,select

from database import OpportunityNewsEvent,SecurityCatalog,create_database,create_session_factory
from market_data.finnhub import EndpointResult
from services.opportunity_news import OpportunityNewsService,classify_event
from market_data.usage import ApiLimitExceeded

class FakeFinnhub:
    def __init__(self,rows,availability="verfügbar",message=None):self.rows=rows;self.calls=[];self.availability=availability;self.message=message
    def request(self,area,endpoint,**params):
        self.calls.append((endpoint,params));return EndpointResult(area,self.availability,self.rows,self.message)

class OpportunityNewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.factory=create_session_factory(create_database(Path(self.tmp.name)/"n.db"))
        with self.factory.begin() as s:
            s.add(SecurityCatalog(name="Acme",symbol="ACME",exchange="NASDAQ",country="US",currency="USD",
                security_type="stock",source="NASDAQ",provider_symbol_finnhub="ACME"))
        self.now=datetime(2026,8,24,12,tzinfo=timezone.utc)
    def tearDown(self):self.tmp.cleanup()

    def test_general_news_unique_symbol_and_high_score_are_persisted_once(self):
        rows=[{"id":7,"datetime":int(self.now.timestamp()),"related":"ACME","headline":"Acme raises guidance after major order worth $2 billion","summary":"Concrete contract","url":"https://n/7","source":"wire"}]
        fake=FakeFinnhub(rows);service=OpportunityNewsService(self.factory,lambda:fake,lambda:self.now)
        first=service.collect(1);second=service.collect(2)
        self.assertEqual(first.catalog_ids,tuple([1]));self.assertEqual(first.events_created,1)
        self.assertEqual(second.events_created,0)
        with self.factory() as s:
            event=s.scalar(select(OpportunityNewsEvent));count=s.scalar(select(func.count()).select_from(OpportunityNewsEvent))
        self.assertEqual(count,1);self.assertGreaterEqual(event.event_score,85)
        self.assertEqual(fake.calls[0][0],"news");self.assertEqual(fake.calls[0][1]["category"],"general")

    def test_ambiguous_or_missing_symbol_is_discarded(self):
        rows=[{"id":8,"datetime":int(self.now.timestamp()),"related":"ACME,OTHER","headline":"Major order worth $2 billion"},
              {"id":9,"datetime":int(self.now.timestamp()),"related":"UNKNOWN","headline":"Major order worth $2 billion"}]
        result=OpportunityNewsService(self.factory,lambda:FakeFinnhub(rows),lambda:self.now).collect(1)
        self.assertEqual(result.catalog_ids,());self.assertEqual(result.events_created,0)

    def test_noise_is_not_an_event(self):
        self.assertIsNone(classify_event("Acme conference participation",None,self.now,self.now))

    def test_http_429_is_not_retried(self):
        fake=FakeFinnhub([],"API-Fehler","Finnhub-Limit erreicht (HTTP 429).")
        with self.assertRaises(ApiLimitExceeded):
            OpportunityNewsService(self.factory,lambda:fake,lambda:self.now).collect(1)
        self.assertEqual(len(fake.calls),1)

if __name__=="__main__":unittest.main()
