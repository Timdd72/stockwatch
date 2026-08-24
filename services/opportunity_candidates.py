"""Persistente, deduplizierte Anzeige der zuletzt interessanten Chancen."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from sqlalchemy import or_,select
from sqlalchemy.orm import Session, sessionmaker

from database.models import (OpportunityCandidateSummary, OpportunityNewsEvent,OpportunityWatchlist,
    OpportunityWatchObservation,ScannerAiAssessment, ScannerCandidate, ScannerCandidateTechnical, SecurityCatalog)


RETENTION_DAYS = 7


def _utc(value:datetime)->datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class OpportunityDisplayItem:
    summary: OpportunityCandidateSummary
    candidate: ScannerCandidate
    catalog: SecurityCatalog
    technical: ScannerCandidateTechnical | None
    assessment: ScannerAiAssessment | None
    news: OpportunityNewsEvent | None
    external_url: str


@dataclass(frozen=True, slots=True)
class FavoriteDisplayItem:
    display: OpportunityDisplayItem
    watch: OpportunityWatchlist
    previous_rating: str | None
    performance_percent: float | None
    duration_days: int
    change: str | None


class OpportunityCandidateService:
    def __init__(self, sessions: sessionmaker[Session], clock=None) -> None:
        self._sessions=sessions
        self._clock=clock or (lambda:datetime.now(timezone.utc))

    def record_run(self, scanner_run_id:int, technical_run_id:int|None=None) -> None:
        """Upsert statt Duplikat; vorhandene Detailreferenzen werden nicht mit NULL überschrieben."""
        with self._sessions.begin() as s:
            candidates=list(s.scalars(select(ScannerCandidate).where(
                ScannerCandidate.scanner_run_id==scanner_run_id,
                ScannerCandidate.status=="SHORTLISTED")))
            for candidate in candidates:
                technical=s.scalar(select(ScannerCandidateTechnical).where(
                    ScannerCandidateTechnical.scanner_candidate_id==candidate.id,
                    *([ScannerCandidateTechnical.technical_run_id==technical_run_id] if technical_run_id else []))
                    .order_by(ScannerCandidateTechnical.id.desc()).limit(1))
                assessment=s.scalar(select(ScannerAiAssessment).where(
                    ScannerAiAssessment.scanner_candidate_id==candidate.id)
                    .order_by(ScannerAiAssessment.id.desc()).limit(1))
                news=s.scalar(select(OpportunityNewsEvent).where(
                    OpportunityNewsEvent.catalog_id==candidate.catalog_id)
                    .order_by(OpportunityNewsEvent.published_at.desc(),OpportunityNewsEvent.id.desc()).limit(1))
                row=s.scalar(select(OpportunityCandidateSummary).where(
                    OpportunityCandidateSummary.catalog_id==candidate.catalog_id))
                if row is None:
                    row=OpportunityCandidateSummary(catalog_id=candidate.catalog_id,symbol=candidate.symbol,
                        name=candidate.name,first_seen=candidate.created_at,last_seen=candidate.created_at,
                        latest_scanner_run_id=scanner_run_id,latest_candidate_id=candidate.id,
                        quality_score=candidate.quality_score,opportunity_score=(technical.opportunity_score
                            if technical and technical.opportunity_score is not None else candidate.opportunity_score),
                        classification=(technical.candidate_type if technical else candidate.candidate_type))
                    s.add(row)
                else:
                    row.last_seen=max(_utc(row.last_seen),_utc(candidate.created_at))
                    row.symbol=candidate.symbol;row.name=candidate.name
                    row.latest_scanner_run_id=scanner_run_id;row.latest_candidate_id=candidate.id
                    row.quality_score=candidate.quality_score
                    row.opportunity_score=(technical.opportunity_score if technical and technical.opportunity_score is not None else candidate.opportunity_score)
                    row.classification=technical.candidate_type if technical else candidate.candidate_type
                if technical:row.latest_technical_id=technical.id
                if assessment:row.latest_ai_assessment_id=assessment.id
                if news:row.latest_news_event_id=news.id
                watch=s.scalar(select(OpportunityWatchlist).where(
                    OpportunityWatchlist.catalog_id==candidate.catalog_id,
                    OpportunityWatchlist.favorite.is_(True)))
                if watch:self._observe(s,watch,candidate,technical,assessment)

    def visible(self) -> list[OpportunityDisplayItem]:
        cutoff=self._clock()-timedelta(days=RETENTION_DAYS)
        with self._sessions() as s:
            rows=list(s.scalars(select(OpportunityCandidateSummary).outerjoin(OpportunityWatchlist,
                OpportunityWatchlist.catalog_id==OpportunityCandidateSummary.catalog_id).where(or_(
                OpportunityCandidateSummary.last_seen>=cutoff,
                OpportunityWatchlist.favorite.is_(True)))
                .order_by(OpportunityCandidateSummary.last_seen.desc(),OpportunityCandidateSummary.opportunity_score.desc())))
            result=[]
            for row in rows:
                candidate=s.get(ScannerCandidate,row.latest_candidate_id);catalog=s.get(SecurityCatalog,row.catalog_id)
                if candidate is None or catalog is None:continue
                technical=s.get(ScannerCandidateTechnical,row.latest_technical_id) if row.latest_technical_id else None
                assessment=s.get(ScannerAiAssessment,row.latest_ai_assessment_id) if row.latest_ai_assessment_id else None
                news=s.get(OpportunityNewsEvent,row.latest_news_event_id) if row.latest_news_event_id else None
                key=catalog.isin or catalog.symbol
                external=f"https://www.finanzen.net/suchergebnis.asp?_search={quote_plus(key)}"
                for value in (row,candidate,catalog,technical,assessment,news):
                    if value is not None:s.expunge(value)
                result.append(OpportunityDisplayItem(row,candidate,catalog,technical,assessment,news,external))
            return result

    def set_favorite(self,catalog_id:int,enabled:bool)->None:
        with self._sessions.begin() as s:
            summary=s.scalar(select(OpportunityCandidateSummary).where(
                OpportunityCandidateSummary.catalog_id==catalog_id))
            if summary is None:raise LookupError("Opportunity-Kandidat nicht gefunden.")
            candidate=s.get(ScannerCandidate,summary.latest_candidate_id);assert candidate
            technical=s.get(ScannerCandidateTechnical,summary.latest_technical_id) if summary.latest_technical_id else None
            assessment=s.get(ScannerAiAssessment,summary.latest_ai_assessment_id) if summary.latest_ai_assessment_id else None
            row=s.scalar(select(OpportunityWatchlist).where(OpportunityWatchlist.catalog_id==catalog_id))
            if row is None:
                row=OpportunityWatchlist(catalog_id=catalog_id,symbol=candidate.symbol,
                    reason=candidate.reason,active=True);s.add(row);s.flush()
            if enabled:
                if not row.favorite:
                    now=self._clock();price=technical.price if technical and technical.price is not None else candidate.price
                    row.favorite=True;row.active=True;row.favorited_at=now;row.initial_price=price
                    row.initial_quality_score=summary.quality_score;row.initial_opportunity_score=summary.opportunity_score
                    row.initial_entry_rating=assessment.entry_rating if assessment else None
                    row.initial_reason=candidate.reason
                self._observe(s,row,candidate,technical,assessment)
            else:
                row.favorite=False

    def favorites(self)->list[FavoriteDisplayItem]:
        displays={x.catalog.id:x for x in self.visible()}
        now=self._clock();result=[]
        with self._sessions() as s:
            watches=list(s.scalars(select(OpportunityWatchlist).where(OpportunityWatchlist.favorite.is_(True))
                .order_by(OpportunityWatchlist.favorited_at.asc())))
            for watch in watches:
                display=displays.get(watch.catalog_id)
                if not display:continue
                observations=list(s.scalars(select(OpportunityWatchObservation).where(
                    OpportunityWatchObservation.watchlist_id==watch.id)
                    .order_by(OpportunityWatchObservation.recorded_at.desc(),OpportunityWatchObservation.id.desc()).limit(2)))
                current=observations[0] if observations else None;previous=observations[1] if len(observations)>1 else None
                price=(display.technical.price if display.technical and display.technical.price is not None else display.candidate.price)
                perf=((price/watch.initial_price-1)*100 if price is not None and watch.initial_price else None)
                current_rating=current.entry_rating if current else watch.last_entry_rating
                previous_rating=previous.entry_rating if previous and previous.entry_rating!=current_rating else None
                if previous_rating and current_rating:change="Entry Rating geändert"
                elif previous and current and previous.trend!=current.trend:change=f"Trend {previous.trend or '—'} → {current.trend or '—'}"
                elif previous and current and previous.opportunity_score is not None and current.opportunity_score is not None and abs(current.opportunity_score-previous.opportunity_score)>=5:
                    change=f"Opportunity {previous.opportunity_score:.0f} → {current.opportunity_score:.0f}"
                else:change=None
                favored=_utc(watch.favorited_at or watch.added_at)
                s.expunge(watch)
                result.append(FavoriteDisplayItem(display,watch,previous_rating,perf,max(0,(now-favored).days),change))
        return result

    @staticmethod
    def _observe(s:Session,watch:OpportunityWatchlist,candidate:ScannerCandidate,
                 technical:ScannerCandidateTechnical|None,assessment:ScannerAiAssessment|None)->None:
        exists=s.scalar(select(OpportunityWatchObservation.id).where(
            OpportunityWatchObservation.watchlist_id==watch.id,
            OpportunityWatchObservation.scanner_candidate_id==candidate.id))
        if exists:return
        s.add(OpportunityWatchObservation(watchlist_id=watch.id,scanner_candidate_id=candidate.id,
            recorded_at=candidate.created_at,price=(technical.price if technical and technical.price is not None else candidate.price),
            quality_score=candidate.quality_score,
            opportunity_score=(technical.opportunity_score if technical and technical.opportunity_score is not None else candidate.opportunity_score),
            entry_rating=assessment.entry_rating if assessment else None,
            trend=technical.trend if technical else None,performance_5d=technical.performance_5d if technical else None,
            performance_20d=technical.performance_20d if technical else None,
            performance_60d=technical.performance_60d if technical else None,sma20=technical.sma20 if technical else None,
            sma50=technical.sma50 if technical else None,rsi14=technical.rsi14 if technical else None,
            volatility_20d=technical.volatility_20d if technical else None,reason=candidate.reason))
