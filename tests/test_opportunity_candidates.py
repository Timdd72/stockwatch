from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func,select

from database import (OpportunityCandidateSummary,OpportunityWatchlist,OpportunityWatchObservation,ScannerCandidate,ScannerRun,Stock,
    SecurityCatalog,create_database,create_session_factory)
from services.opportunity_candidates import OpportunityCandidateService


class OpportunityCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/"x.db"
        self.factory=create_session_factory(create_database(self.path))
        self.now=datetime(2026,8,24,18,tzinfo=timezone.utc)
        with self.factory.begin() as s:
            cat=SecurityCatalog(name="ePlus inc.",symbol="PLUS",isin=None,exchange="NASDAQ",
                country="US",currency="USD",security_type="stock",source="NASDAQ",provider_symbol_finnhub="PLUS")
            s.add(cat);s.flush();self.catalog_id=cat.id
    def tearDown(self):self.tmp.cleanup()

    def _candidate(self,seen,score=73.8):
        with self.factory.begin() as s:
            run=ScannerRun(status="COMPLETED");s.add(run);s.flush()
            c=ScannerCandidate(scanner_run_id=run.id,catalog_id=self.catalog_id,symbol="PLUS",name="ePlus inc.",
                quality_score=84,opportunity_score=score,candidate_type="ANALYST_SUPPORT",
                status="SHORTLISTED",reason="interessant",created_at=seen)
            s.add(c);s.flush();return run.id,c.id

    def test_candidate_is_kept_seven_days_and_deduplicated(self):
        run1,_=self._candidate(self.now-timedelta(days=6),70)
        service=OpportunityCandidateService(self.factory,clock=lambda:self.now)
        service.record_run(run1);run2,latest_id=self._candidate(self.now,79);service.record_run(run2)
        visible=service.visible();self.assertEqual([x.candidate.id for x in visible],[latest_id])
        with self.factory() as s:
            row=s.scalar(select(OpportunityCandidateSummary));count=s.scalar(select(func.count()).select_from(OpportunityCandidateSummary))
        self.assertEqual(count,1);self.assertEqual(row.first_seen.date(),(self.now-timedelta(days=6)).date())
        self.assertEqual(row.opportunity_score,79)

    def test_candidate_older_than_seven_days_is_hidden_not_deleted(self):
        run,_=self._candidate(self.now-timedelta(days=8));service=OpportunityCandidateService(self.factory,clock=lambda:self.now)
        service.record_run(run);self.assertEqual(service.visible(),[])
        with self.factory() as s:self.assertEqual(s.scalar(select(func.count()).select_from(ScannerCandidate)),1)

    def test_finanzen_link_uses_safe_search_when_isin_is_missing(self):
        run,_=self._candidate(self.now);service=OpportunityCandidateService(self.factory,clock=lambda:self.now)
        service.record_run(run);url=service.visible()[0].external_url
        self.assertEqual(url,"https://www.finanzen.net/suchergebnis.asp?_search=PLUS")

    def test_failed_or_empty_run_does_not_remove_existing_candidate(self):
        run,_=self._candidate(self.now);service=OpportunityCandidateService(self.factory,clock=lambda:self.now)
        service.record_run(run)
        with self.factory.begin() as s:s.add(ScannerRun(status="FAILED"))
        self.assertEqual(len(service.visible()),1)

    def test_historical_plus_is_backfilled_without_network(self):
        self._candidate(self.now-timedelta(hours=1))
        # create_database führt die verlustfreie Migration erneut aus.
        create_database(self.path)
        service=OpportunityCandidateService(self.factory,clock=lambda:self.now)
        self.assertEqual(service.visible()[0].candidate.symbol,"PLUS")

    def test_favorite_survives_retention_and_never_creates_stock(self):
        run,_=self._candidate(self.now-timedelta(days=30));service=OpportunityCandidateService(self.factory,clock=lambda:self.now)
        service.record_run(run);service.set_favorite(self.catalog_id,True)
        favorites=service.favorites();self.assertEqual(len(favorites),1)
        self.assertEqual(favorites[0].watch.initial_opportunity_score,73.8)
        with self.factory() as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(Stock)),0)
            self.assertEqual(s.scalar(select(func.count()).select_from(OpportunityWatchObservation)),1)

    def test_unfavorite_keeps_observation_history(self):
        run,_=self._candidate(self.now);service=OpportunityCandidateService(self.factory,clock=lambda:self.now)
        service.record_run(run);service.set_favorite(self.catalog_id,True);service.set_favorite(self.catalog_id,False)
        self.assertEqual(service.favorites(),[])
        with self.factory() as s:
            watch=s.scalar(select(OpportunityWatchlist));count=s.scalar(select(func.count()).select_from(OpportunityWatchObservation))
        self.assertFalse(watch.favorite);self.assertEqual(count,1)

    def test_repeated_discovery_updates_without_overwriting_observations(self):
        service=OpportunityCandidateService(self.factory,clock=lambda:self.now)
        run1,_=self._candidate(self.now-timedelta(days=1),70);service.record_run(run1);service.set_favorite(self.catalog_id,True)
        run2,_=self._candidate(self.now,82);service.record_run(run2)
        with self.factory() as s:
            count=s.scalar(select(func.count()).select_from(OpportunityWatchObservation))
            rows=list(s.scalars(select(OpportunityWatchObservation).order_by(OpportunityWatchObservation.recorded_at)))
        self.assertEqual(count,2);self.assertEqual([x.opportunity_score for x in rows],[70,82])


if __name__=="__main__":unittest.main()
