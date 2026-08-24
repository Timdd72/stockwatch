"""Ein manueller, API-schonender Orchestrator der bestehenden Scanner-Stufen."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
from typing import Callable
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import IntegrityError

from ai.service import AiAnalysisError, AiBudgetExceeded
from database.models import (AiUsage, ApiUsage, OpportunitySearchRun, OpportunityWatchlist,
    ScannerAiAssessment, ScannerCandidate, ScannerCandidateTechnical, ScannerSecurityState)
from market_data.usage import ApiLimitExceeded
from .opportunity_scanner import OpportunityScannerService
from .scanner_ai import ScannerAiService
from .scanner_technical import ScannerTechnicalService
from .opportunity_news import NewsFirstResult, OpportunityNewsService
from .openai_news_scout import OpenAiNewsScoutService,OpenAiScoutResult
from .opportunity_runtime import OpportunityRateGate, OpportunityRuntimeConfig, OpportunityTimeout
from .opportunity_candidates import OpportunityCandidateService

@dataclass(frozen=True, slots=True)
class OpportunitySearchResult:
    run: OpportunitySearchRun
    assessments: tuple[ScannerAiAssessment,...]

@dataclass(frozen=True, slots=True)
class OpportunitySearchProgress:
    run: OpportunitySearchRun
    candidates_checked: int
    finnhub_requests: int
    elapsed_seconds: int

class OpportunitySearchService:
    def __init__(self,sessions:sessionmaker[Session],scanner:OpportunityScannerService,
                 technical:ScannerTechnicalService,scanner_ai:ScannerAiService,
                 news:OpportunityNewsService|None=None,config:OpportunityRuntimeConfig|None=None,
                 scout:OpenAiNewsScoutService|None=None,
                 clock:Callable[[],datetime]|None=None,sleeper=None)->None:
        self._sessions,self._scanner,self._technical,self._ai=sessions,scanner,technical,scanner_ai
        self._clock=clock or (lambda:datetime.now(timezone.utc))
        self.config=config or OpportunityRuntimeConfig.from_env();self._news=news;self._scout=scout
        self._sleeper=sleeper
        self._candidate_store=OpportunityCandidateService(sessions,self._clock)

    def latest_run(self)->OpportunitySearchRun|None:
        with self._sessions() as s:return s.scalar(select(OpportunitySearchRun).order_by(OpportunitySearchRun.id.desc()).limit(1))

    def progress(self)->OpportunitySearchProgress|None:
        """Liest den laufenden Fortschritt ausschließlich aus SQLite."""
        with self._sessions() as s:
            run=s.scalar(select(OpportunitySearchRun).where(
                OpportunitySearchRun.status=="RUNNING").order_by(OpportunitySearchRun.id.desc()).limit(1))
            if run is None:return None
            checked=int(s.scalar(select(func.count()).select_from(ScannerSecurityState).where(
                ScannerSecurityState.last_checked_at>=run.started_at)) or 0)
            requests=int(s.scalar(select(func.count()).select_from(ApiUsage).where(
                ApiUsage.provider=="finnhub",ApiUsage.timestamp>=run.started_at)) or 0)
            started=run.started_at
            if started.tzinfo is None:started=started.replace(tzinfo=timezone.utc)
            elapsed=max(0,int((self._clock()-started).total_seconds()))
            s.expunge(run)
            return OpportunitySearchProgress(run,checked,requests,elapsed)

    def run_search(self,force:bool=False)->OpportunitySearchResult:
        del force  # Caches/Fingerprints der bestehenden Stufen entscheiden über Aktualität.
        now=self._clock();deadline=now+timedelta(minutes=self.config.max_runtime_minutes)
        try:
            with self._sessions.begin() as s:
                active=s.scalar(select(OpportunitySearchRun).where(OpportunitySearchRun.status=="RUNNING").limit(1))
                if active:
                    stamp=active.deadline_at or (active.started_at+timedelta(minutes=self.config.max_runtime_minutes+2))
                    if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
                    if stamp>=now:raise RuntimeError("Chancensuche läuft bereits.")
                    active.status="PARTIAL_RUNTIME_LIMIT";active.finished_at=now;active.message="Veralteter RUNNING-Lock wurde bereinigt."
                run=OpportunitySearchRun(status="RUNNING",phase="NEWS_SCOUT",deadline_at=deadline);s.add(run);s.flush();run_id=run.id
        except IntegrityError:
            raise RuntimeError("Chancensuche läuft bereits.") from None
        started=self._usage(); assessments=(); status="COMPLETED"; message="Chancensuche abgeschlossen."
        scanner_run=technical_run=None
        gate=OpportunityRateGate(self._sessions,self.config.finnhub_requests_per_minute,deadline,self._clock,self._sleeper)
        try:
            if self._scout:
                news_result=self._scout.scout(run_id)
                news_ids=news_result.catalog_ids;news_found=news_result.news_found;web_calls=news_result.web_search_calls
            elif self._news:
                legacy=self._news.collect(run_id,gate.before_request)
                news_ids=legacy.catalog_ids;news_found=legacy.events_seen;web_calls=0
            else:news_ids=();news_found=web_calls=0
            strong_news_ids=self._strong_news_ids(run_id)
            self._phase(run_id,"CANDIDATE_CHECK",news_candidates=len(news_ids),news_found=news_found,web_search_calls=web_calls)
            watch_ids=self._watchlist_ids()
            self._phase(run_id,"CANDIDATE_CHECK",watchlist_candidates=len(watch_ids))
            prioritized=list(dict.fromkeys((*news_ids,*watch_ids)))
            # Freie Rotation ist nur der begrenzte Fallback, wenn der Scout keine Aktie liefert.
            rotation_limit=getattr(getattr(self._scanner,"config",None),"max_quality_checks",35)
            if not isinstance(rotation_limit,int):rotation_limit=35
            max_candidates=len(prioritized) if news_ids else len(prioritized)+rotation_limit
            scanner_run=self._scanner.run(prioritized_catalog_ids=prioritized,deadline=deadline,
                request_gate=gate.before_request,progress=lambda phase,count,symbol:self._progress(run_id,"CANDIDATE_CHECK",count,symbol),
                enforce_request_budget=False,strong_event_catalog_ids=strong_news_ids,max_candidates=max_candidates)
            self._phase(run_id,"TECHNICAL",rotation_candidates=max(0,getattr(scanner_run,"candidates_checked",0)-len(prioritized)))
            technical_run=self._technical.run(scanner_run.id,allow_alpha_vantage=False,deadline=deadline,
                request_gate=gate.before_request,progress=lambda phase,count,symbol:self._progress(run_id,"TECHNICAL",count,symbol),
                preserve_unavailable=True)
            final=self._final_candidates(technical_run.id)
            self._phase(run_id,"FINAL_AI",finalists=len(final))
            self._scanner.load_final_news([c for c,_ in final],request_gate=gate.before_request,deadline=deadline)
            if self._clock()>=deadline:raise OpportunityTimeout("Maximale Laufzeit erreicht.")
            ai_result=self._ai.run_latest(technical_run.id,{c.id for c,_ in final})
            assessments=ai_result.assessments
            self._update_watchlist(final,assessments)
            if self._clock()>=deadline:
                status="PARTIAL_RUNTIME_LIMIT";message=f"Chancensuche nach {self.config.max_runtime_minutes} Minuten beendet · Ergebnisse gespeichert."
            elif scanner_run.status=="PARTIAL_RUNTIME_LIMIT" or technical_run.status=="PARTIAL_RUNTIME_LIMIT":
                status="PARTIAL_RUNTIME_LIMIT";message=f"Chancensuche nach {self.config.max_runtime_minutes} Minuten beendet · Ergebnisse gespeichert."
            elif scanner_run.status=="PARTIAL_RATE_LIMIT" or technical_run.status=="PARTIAL_RATE_LIMIT":
                status="PARTIAL_RATE_LIMIT";message="Chancensuche teilweise abgeschlossen · Finnhub-Limit erreicht."
            elif "PARTIAL_DATA" in (scanner_run.status,technical_run.status):
                status="PARTIAL_DATA";message="Chancensuche teilweise abgeschlossen · ein vorgesehener Datenabruf konnte nicht verarbeitet werden."
            elif assessments:message=f"Chancensuche abgeschlossen · {len(assessments)} interessante Kandidaten gefunden."
            else:message="Chancensuche abgeschlossen · derzeit keine ausreichend bestätigte Kaufchance."
        except OpportunityTimeout:
            status="PARTIAL_RUNTIME_LIMIT";message=f"Chancensuche nach {self.config.max_runtime_minutes} Minuten beendet · Ergebnisse gespeichert."
        except ApiLimitExceeded as exc:
            status="PARTIAL_RATE_LIMIT";message="Chancensuche teilweise abgeschlossen · Finnhub-Limit erreicht."
        except AiBudgetExceeded as exc:
            status="PARTIAL_RATE_LIMIT";message=str(exc)
        except AiAnalysisError as exc:
            status="PARTIAL_DATA";message=f"Marktdaten wurden verarbeitet; KI-Bewertung konnte nicht abgeschlossen werden: {exc}"
        except Exception as exc:
            status="FAILED";message=str(exc)
        if status!="FAILED" and scanner_run is not None:
            self._candidate_store.record_run(scanner_run.id,getattr(technical_run,"id",None))
        finished=self._usage();
        with self._sessions.begin() as s:
            row=s.get(OpportunitySearchRun,run_id);assert row
            row.finished_at=self._clock();row.status=status;row.message=message
            if status=="COMPLETED":row.phase="COMPLETED"
            row.scanner_run_id=getattr(scanner_run,"id",None);row.technical_run_id=getattr(technical_run,"id",None)
            row.candidates_found=len(assessments);row.finalists=len(assessments);row.finnhub_requests=finished[0]-started[0]
            row.web_search_calls=self._web_calls_since(row.started_at)
            row.alpha_vantage_requests=finished[1]-started[1];row.openai_requests=finished[2]-started[2]
            row.estimated_ai_cost_eur=finished[3]-started[3]
            row.cache_hits=(getattr(scanner_run,"cache_hits",0) or 0)+(getattr(technical_run,"cache_hits",0) or 0)
            s.flush();s.expunge(row);return OpportunitySearchResult(row,tuple(assessments))

    def _watchlist_ids(self)->list[int]:
        with self._sessions() as s:
            return list(s.scalars(select(OpportunityWatchlist.catalog_id).where(OpportunityWatchlist.active.is_(True))
                .order_by(case((OpportunityWatchlist.favorite.is_(True),0),else_=1),
                          case((OpportunityWatchlist.reason.like("NEUE CHANCE%"),0),else_=1),
                          OpportunityWatchlist.last_checked_at.asc(),OpportunityWatchlist.id.asc())))

    def _phase(self,run_id:int,phase:str,**values)->None:
        with self._sessions.begin() as s:
            row=s.get(OpportunitySearchRun,run_id);assert row;row.phase=phase
            for key,value in values.items():setattr(row,key,value)

    def _progress(self,run_id:int,phase:str,count:int,symbol:str|None)->None:
        with self._sessions.begin() as s:
            row=s.get(OpportunitySearchRun,run_id);assert row
            row.phase=phase;row.checked_total=count;row.current_symbol=symbol

    def _final_candidates(self,technical_run_id:int)->list[tuple[ScannerCandidate,ScannerCandidateTechnical]]:
        with self._sessions() as s:
            rows=list(s.execute(select(ScannerCandidate,ScannerCandidateTechnical)
                .join(ScannerCandidateTechnical,ScannerCandidateTechnical.scanner_candidate_id==ScannerCandidate.id)
                .where(ScannerCandidateTechnical.technical_run_id==technical_run_id,
                       or_(ScannerCandidate.quality_score>=75,ScannerCandidate.candidate_type=="NEWS_CHANCE"),
                       ScannerCandidateTechnical.opportunity_score>=60)
                .order_by(ScannerCandidateTechnical.opportunity_score.desc())))
        def eligible(pair):
            _,t=pair
            falling=t.trend=="NEGATIV" and (t.performance_60d or 0)<-15
            extreme=(t.rsi14 or 0)>78 or (t.volatility_20d or 0)>70
            # Hochwertige überhitzte WATCH-Werte bleiben als spätere Einstiegschance zulässig.
            return not falling and (not extreme or t.candidate_type=="WATCH")
        return [x for x in rows if eligible(x)][:min(self.config.max_ai_candidates,self._ai.config.max_candidates)]

    def _strong_news_ids(self,run_id:int)->set[int]:
        from database.models import OpportunityNewsEvent
        with self._sessions() as s:
            return set(s.scalars(select(OpportunityNewsEvent.catalog_id).where(
                OpportunityNewsEvent.opportunity_search_run_id==run_id,OpportunityNewsEvent.event_score>=85)))

    def _update_watchlist(self,final,assessments)->None:
        by_id={a.scanner_candidate_id:a for a in assessments}
        with self._sessions.begin() as s:
            for c,t in final:
                a=by_id.get(c.id)
                if not a:continue
                row=s.scalar(select(OpportunityWatchlist).where(OpportunityWatchlist.catalog_id==c.catalog_id))
                improved=bool(row and row.last_entry_rating=="WATCH" and a.entry_rating in ("BUY","STRONG_BUY"))
                improved=improved or bool(row and (row.last_rsi14 or 0)>=70 and 35<=(t.rsi14 or 0)<=60 and t.trend!="NEGATIV")
                reason=("NEUE CHANCE · " if improved else "")+a.entry_reason
                if row is None:
                    if c.quality_score>=75 and a.entry_rating in ("WATCH","BUY","STRONG_BUY"):
                        row=OpportunityWatchlist(catalog_id=c.catalog_id,symbol=c.symbol,reason=reason);s.add(row)
                    else:continue
                row.last_checked_at=self._clock();row.last_quality_score=c.quality_score
                row.last_opportunity_score=t.opportunity_score;row.last_classification=t.candidate_type
                row.last_entry_rating=a.entry_rating;row.last_rsi14=t.rsi14;row.reason=reason
                weak=a.entry_rating=="AVOID" or c.quality_score<60 or (t.trend=="NEGATIV" and (t.performance_60d or 0)<-15)
                row.weak_checks=row.weak_checks+1 if weak else 0
                if row.weak_checks>=2 and not row.favorite:row.active=False

    def _usage(self)->tuple[int,int,int,Decimal]:
        with self._sessions() as s:
            f=int(s.scalar(select(func.count()).select_from(ApiUsage).where(ApiUsage.provider=="finnhub")) or 0)
            a=int(s.scalar(select(func.count()).select_from(ApiUsage).where(ApiUsage.provider=="alpha_vantage")) or 0)
            o=int(s.scalar(select(func.count()).select_from(AiUsage)) or 0)
            cost=Decimal(str(s.scalar(select(func.coalesce(func.sum(AiUsage.estimated_cost_eur),0))) or 0))
            return f,a,o,cost

    def _web_calls_since(self,started:datetime)->int:
        with self._sessions() as s:
            return int(s.scalar(select(func.coalesce(func.sum(AiUsage.tool_calls),0)).where(
                AiUsage.timestamp>=started,AiUsage.tool_type=="web_search")) or 0)
