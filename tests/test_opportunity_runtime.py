from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch

from database import ApiUsage,create_database,create_session_factory
from services.opportunity_runtime import OpportunityRateGate,OpportunityRuntimeConfig,OpportunityTimeout

class OpportunityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.factory=create_session_factory(create_database(Path(self.tmp.name)/"r.db"))
    def tearDown(self):self.tmp.cleanup()

    def test_defaults_are_central(self):
        with patch.dict("os.environ",{},clear=True):
            config=OpportunityRuntimeConfig.from_env(Path(self.tmp.name)/"missing")
        self.assertEqual((config.max_runtime_minutes,config.finnhub_requests_per_minute,config.max_ai_candidates),(10,45,3))

    def test_rate_gate_waits_for_window_instead_of_total_budget(self):
        state={"now":datetime(2026,8,24,12,tzinfo=timezone.utc)};waits=[]
        with self.factory.begin() as s:
            s.add(ApiUsage(provider="finnhub",endpoint="news",timestamp=state["now"]-timedelta(seconds=59),success=True))
        def sleep(seconds):waits.append(seconds);state["now"]+=timedelta(seconds=seconds+.01)
        gate=OpportunityRateGate(self.factory,1,state["now"]+timedelta(minutes=2),lambda:state["now"],sleep)
        gate.before_request();self.assertEqual(len(waits),1);self.assertGreaterEqual(waits[0],1)

    def test_rate_wait_respects_deadline(self):
        now=datetime(2026,8,24,12,tzinfo=timezone.utc)
        with self.factory.begin() as s:s.add(ApiUsage(provider="finnhub",endpoint="news",timestamp=now,success=True))
        gate=OpportunityRateGate(self.factory,1,now+timedelta(seconds=2),lambda:now,lambda _:None)
        with self.assertRaises(OpportunityTimeout):gate.before_request()

if __name__=="__main__":unittest.main()
