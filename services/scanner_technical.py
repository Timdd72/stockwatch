"""Technische Verifikation der Top-Kandidaten eines bestehenden Scanner-Laufs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Callable
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from analysis import TechnicalIndicators, calculate_technical_indicators
from database.models import (ApiUsage, ProviderCache, ScannerCandidate, ScannerCandidateTechnical,
    ScannerProviderCapability, ScannerRun, ScannerTechnicalRun, SecurityCatalog)
from market_data.usage import ApiLimitExceeded
from market_data.alpha_vantage import AlphaVantageError
from market_data.finnhub import FinnhubError
from .market_updates import MarketUpdateError, MarketUpdateService
from .opportunity_scanner import opportunity_score

class TechnicalVerificationRunning(RuntimeError): pass
class HistoryUnavailable(RuntimeError): pass

@dataclass(frozen=True, slots=True)
class TechnicalVerificationConfig:
    max_candidates: int = 5
    history_max_age: timedelta = timedelta(hours=24)
    pullback_min_quality: float = 70
    pullback_min_rsi: float = 35
    pullback_max_rsi: float = 60
    pullback_min_distance_sma20: float = -5
    pullback_min_distance_sma50: float = -3
    pullback_min_distance_high20: float = -20
    pullback_max_distance_high20: float = -3
    max_volatility: float = 55

class ScannerTechnicalService:
    def __init__(self, sessions: sessionmaker[Session], market_updates: MarketUpdateService,
                 config: TechnicalVerificationConfig | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._sessions, self._market = sessions, market_updates
        self.config = config or TechnicalVerificationConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def latest_run(self, scanner_run_id: int) -> ScannerTechnicalRun | None:
        with self._sessions() as s:
            return s.scalar(select(ScannerTechnicalRun).where(ScannerTechnicalRun.scanner_run_id == scanner_run_id).order_by(ScannerTechnicalRun.id.desc()).limit(1))

    def results(self, technical_run_id: int) -> dict[int, ScannerCandidateTechnical]:
        with self._sessions() as s:
            rows=s.scalars(select(ScannerCandidateTechnical).where(ScannerCandidateTechnical.technical_run_id==technical_run_id))
            return {row.scanner_candidate_id:row for row in rows}

    def top_candidates(self, scanner_run_id: int) -> list[ScannerCandidate]:
        with self._sessions() as s:
            return list(s.scalars(select(ScannerCandidate).where(ScannerCandidate.scanner_run_id==scanner_run_id,
                ScannerCandidate.status=="SHORTLISTED").order_by(ScannerCandidate.quality_score.desc(),
                ScannerCandidate.opportunity_score.desc(),ScannerCandidate.symbol).limit(self.config.max_candidates)))

    def run(self, scanner_run_id: int, *, allow_alpha_vantage: bool = True,
            deadline: datetime | None = None, request_gate=None, progress=None,
            preserve_unavailable:bool=False) -> ScannerTechnicalRun:
        with self._sessions.begin() as s:
            active=s.scalar(select(ScannerTechnicalRun).where(ScannerTechnicalRun.status=="RUNNING").limit(1))
            if active:
                started=active.started_at if active.started_at.tzinfo else active.started_at.replace(tzinfo=timezone.utc)
                if started>=self._clock()-timedelta(minutes=15):
                    raise TechnicalVerificationRunning("Es läuft bereits eine technische Prüfung.")
                active.status="PARTIAL_DATA";active.finished_at=self._clock()
                active.message="Verwaister technischer Lauf nach einem Prozess- oder Programmabbruch bereinigt."
            if s.get(ScannerRun,scanner_run_id) is None: raise LookupError("Scanner-Lauf nicht gefunden.")
            run=ScannerTechnicalRun(scanner_run_id=scanner_run_id,status="RUNNING"); s.add(run); s.flush(); run_id=run.id
        return self._execute(run_id,scanner_run_id,allow_alpha_vantage,deadline,request_gate,progress,preserve_unavailable)

    def resume(self, technical_run_id: int) -> ScannerTechnicalRun:
        """Setzt ausschließlich einen nach Prozessabbruch verbliebenen RUNNING-Lauf fort."""
        with self._sessions() as s:
            run=s.get(ScannerTechnicalRun,technical_run_id)
            if run is None or run.status!="RUNNING": raise LookupError("Kein fortsetzbarer technischer Lauf gefunden.")
            scanner_run_id=run.scanner_run_id
        return self._execute(technical_run_id,scanner_run_id,True)

    def _execute(self,run_id:int,scanner_run_id:int,allow_alpha_vantage:bool,
                 deadline:datetime|None=None,request_gate=None,progress=None,preserve_unavailable:bool=False)->ScannerTechnicalRun:
        cache_hits=0; checked=0; status="COMPLETED"; message="Technische Prüfung abgeschlossen."
        with self._sessions() as s:
            existing=set(s.scalars(select(ScannerCandidateTechnical.scanner_candidate_id).where(ScannerCandidateTechnical.technical_run_id==run_id)))
        try:
            for candidate in self.top_candidates(scanner_run_id):
                if deadline is not None and self._clock()>=deadline:
                    status,message="PARTIAL_RUNTIME_LIMIT","Technische Prüfung wegen des Laufzeitlimits beendet.";break
                if candidate.id in existing: checked+=1; continue
                if progress:progress("WATCHLIST",checked,candidate.symbol)
                try:
                    history,provider,cached=self._history(candidate,allow_alpha_vantage,request_gate)
                    cache_hits += int(cached)
                    result=self._calculate(candidate,history,provider,run_id)
                    with self._sessions.begin() as s: s.add(result)
                    checked += 1
                except ApiLimitExceeded:
                    status,message="PARTIAL_RATE_LIMIT","Technische Prüfung wegen eines API-Limits beendet."; break
                except HistoryUnavailable as exc:
                    if preserve_unavailable:
                        with self._sessions.begin() as s:
                            s.add(ScannerCandidateTechnical(technical_run_id=run_id,scanner_candidate_id=candidate.id,
                                history_provider="unavailable",trading_days=0,price=candidate.price,
                                momentum_score=None,opportunity_score=None,trend=None,
                                candidate_type="WATCH",reason=str(exc)))
                        checked += 1
                        message="Technische Prüfung abgeschlossen; tarifbedingt fehlende Historien wurden gekennzeichnet."
                        continue
                    status,message="PARTIAL_DATA",str(exc);continue
                except (MarketUpdateError, AlphaVantageError, FinnhubError) as exc:
                    status="PARTIAL_DATA";message="Mindestens eine vorgesehene Historie konnte wegen eines Providerproblems nicht verarbeitet werden."
                    if preserve_unavailable:
                        with self._sessions.begin() as s:
                            s.add(ScannerCandidateTechnical(technical_run_id=run_id,scanner_candidate_id=candidate.id,
                                history_provider="unavailable",trading_days=0,price=candidate.price,
                                momentum_score=None,opportunity_score=None,trend=None,candidate_type="WATCH",
                                reason="Technische Historie wegen eines unerwarteten Providerproblems nicht verfügbar."))
                        checked += 1
                    continue
        except Exception:
            self._finish_run(run_id,"FAILED","Technische Prüfung wegen eines internen Fehlers abgebrochen.",checked,cache_hits)
            raise
        return self._finish_run(run_id,status,message,checked,cache_hits)

    def _finish_run(self,run_id:int,status:str,message:str,checked:int,cache_hits:int)->ScannerTechnicalRun:
        with self._sessions.begin() as s:
            run=s.get(ScannerTechnicalRun,run_id); assert run
            run.finished_at=self._clock(); run.status=status; run.candidates_checked=checked
            run.finnhub_requests=self._usage_since("finnhub",run.started_at)
            run.alpha_vantage_requests=self._usage_since("alpha_vantage",run.started_at)
            run.cache_hits=cache_hits; run.message=message
            return run

    def _history(self,candidate:ScannerCandidate,allow_alpha_vantage:bool=True,request_gate=None)->tuple[pd.DataFrame,str,bool]:
        with self._sessions() as s: catalog=s.get(SecurityCatalog,candidate.catalog_id)
        if catalog is None: raise MarketUpdateError("Katalogeintrag fehlt.")
        cached=self._cached_history(catalog.symbol,allow_alpha_vantage)
        if cached: return cached[0],cached[1],True
        finnhub=catalog.provider_symbol_finnhub; alpha=catalog.provider_symbol_alpha_vantage
        capability=self._capability(catalog.id,"stock/candle")
        if finnhub and capability not in ("premium_required","unavailable"):
            try:
                if request_gate:request_gate()
                history=self._market.load_history("finnhub",finnhub); self._save_history(catalog.symbol,"finnhub",history)
                return history,"finnhub",False
            except MarketUpdateError as exc:
                text=str(exc).lower(); status="premium_required" if "premium" in text or "freigeschaltet" in text else "unavailable"
                self._save_capability(catalog.id,"stock/candle",status)
        if allow_alpha_vantage and alpha:
            if request_gate:request_gate()
            history=self._market.load_history("alpha_vantage",alpha); self._save_history(catalog.symbol,"alpha_vantage",history)
            return history,"alpha_vantage",False
        if finnhub and (capability in ("premium_required","unavailable") or not allow_alpha_vantage):
            raise HistoryUnavailable("Technische Historie im Finnhub-Tarif nicht verfügbar.")
        raise MarketUpdateError("Kein verfügbarer History-Provider.")

    def _calculate(self,c:ScannerCandidate,h:pd.DataFrame,provider:str,run_id:int)->ScannerCandidateTechnical:
        indicators=calculate_technical_indicators(h); close=pd.to_numeric(h["Close"],errors="coerce").dropna()
        high20=float(close.tail(20).max()) if len(close)>=1 else None; low20=float(close.tail(20).min()) if len(close)>=1 else None
        high60=float(close.tail(60).max()) if len(close)>=1 else None; low60=float(close.tail(60).min()) if len(close)>=1 else None
        price=indicators.current_close
        dh=(price/high20-1)*100 if high20 else None; dl=(price/low20-1)*100 if low20 else None
        momentum=technical_momentum(indicators); kind,reason=classify_technical(c.quality_score,indicators,dh,self.config)
        score=opportunity_score(c.quality_score,momentum,c.fundamental_score,c.analyst_score,50)
        if kind=="WATCH" and indicators.trend=="NEGATIV": score=min(score,55)
        return ScannerCandidateTechnical(technical_run_id=run_id,scanner_candidate_id=c.id,history_provider=provider,
            trading_days=len(close),price=price,performance_5d=indicators.performance_5d_pct,
            performance_20d=indicators.performance_20d_pct,performance_60d=indicators.performance_60d_pct,
            sma20=indicators.sma20,sma50=indicators.sma50,sma200=indicators.sma200,rsi14=indicators.rsi14,
            volatility_20d=indicators.volatility_20d_pct,distance_sma20_percent=indicators.distance_sma20_pct,
            distance_sma50_percent=indicators.distance_sma50_pct,trend=indicators.trend,high_20d=high20,low_20d=low20,
            distance_high_20d_percent=dh,distance_low_20d_percent=dl,high_60d=high60,low_60d=low60,
            momentum_score=momentum,opportunity_score=round(score,2),candidate_type=kind,reason=reason)

    def _cached_history(self,symbol:str,allow_alpha_vantage:bool=True)->tuple[pd.DataFrame,str]|None:
        cutoff=self._clock()-self.config.history_max_age
        with self._sessions() as s:
            providers=("finnhub","alpha_vantage") if allow_alpha_vantage else ("finnhub",)
            row=s.scalar(select(ProviderCache).where(ProviderCache.provider.in_(providers),
                ProviderCache.symbol==symbol,ProviderCache.data_type=="scanner_history",ProviderCache.fetched_at>=cutoff).order_by(ProviderCache.fetched_at.desc()).limit(1))
            if not row:return None
            payload=json.loads(row.payload); frame=pd.DataFrame(payload["records"])
            return frame,row.provider
    def _save_history(self,symbol:str,provider:str,history:pd.DataFrame)->None:
        records=history.reset_index(drop=True).where(pd.notna(history.reset_index(drop=True)),None).to_dict("records")
        with self._sessions.begin() as s:
            row=s.scalar(select(ProviderCache).where(ProviderCache.provider==provider,ProviderCache.symbol==symbol,ProviderCache.data_type=="scanner_history"))
            payload=json.dumps({"records":records},default=str)
            if row:row.fetched_at,row.payload=self._clock(),payload
            else:s.add(ProviderCache(provider=provider,symbol=symbol,data_type="scanner_history",fetched_at=self._clock(),payload=payload))
    def _capability(self,catalog_id:int,endpoint:str)->str|None:
        with self._sessions() as s:return s.scalar(select(ScannerProviderCapability.status).where(ScannerProviderCapability.catalog_id==catalog_id,ScannerProviderCapability.provider=="finnhub",ScannerProviderCapability.endpoint==endpoint))
    def _save_capability(self,catalog_id:int,endpoint:str,status:str)->None:
        with self._sessions.begin() as s:
            row=s.scalar(select(ScannerProviderCapability).where(ScannerProviderCapability.catalog_id==catalog_id,ScannerProviderCapability.provider=="finnhub",ScannerProviderCapability.endpoint==endpoint))
            if row:row.status,row.last_checked_at=status,self._clock()
            else:s.add(ScannerProviderCapability(catalog_id=catalog_id,provider="finnhub",endpoint=endpoint,status=status,last_checked_at=self._clock()))
    def _usage_count(self,provider:str)->int:
        with self._sessions() as s:return int(s.scalar(select(func.count()).select_from(ApiUsage).where(ApiUsage.provider==provider)) or 0)
    def _usage_since(self,provider:str,since:datetime)->int:
        with self._sessions() as s:return int(s.scalar(select(func.count()).select_from(ApiUsage).where(ApiUsage.provider==provider,ApiUsage.timestamp>=since)) or 0)

def technical_momentum(i:TechnicalIndicators)->float:
    score=50.0
    score += 15 if i.trend=="POSITIV" else -25 if i.trend=="NEGATIV" else 0
    if i.performance_20d_pct is not None:score+=max(-20,min(15,i.performance_20d_pct))
    if i.performance_60d_pct is not None:score+=max(-15,min(12,i.performance_60d_pct/2))
    if i.rsi14 is not None:score += -18 if i.rsi14>75 else 8 if 40<=i.rsi14<=60 else -8 if i.rsi14<30 else 0
    if i.volatility_20d_pct is not None and i.volatility_20d_pct>55:score-=15
    return round(max(0,min(100,score)),2)

def classify_technical(quality:float,i:TechnicalIndicators,distance_high20:float|None,c:TechnicalVerificationConfig|None=None)->tuple[str,str]:
    c=c or TechnicalVerificationConfig(); rsi=i.rsi14
    falling=(i.trend=="NEGATIV" and (i.performance_60d_pct or 0)<-15) or (i.distance_sma50_pct is not None and i.distance_sma50_pct<-8)
    excessive=(i.volatility_20d_pct or 0)>c.max_volatility or (rsi is not None and rsi>75)
    pullback=(quality>=c.pullback_min_quality and not falling and not excessive and i.trend!="NEGATIV" and
        i.distance_sma20_pct is not None and c.pullback_min_distance_sma20<=i.distance_sma20_pct<=2 and
        i.distance_sma50_pct is not None and i.distance_sma50_pct>=c.pullback_min_distance_sma50 and
        rsi is not None and c.pullback_min_rsi<=rsi<=c.pullback_max_rsi and distance_high20 is not None and
        c.pullback_min_distance_high20<=distance_high20<=c.pullback_max_distance_high20)
    if pullback:return "QUALITY_PULLBACK","Hohe Qualität, moderater Rücksetzer und intaktes SMA50-/RSI-Bild."
    momentum=(quality>=70 and i.trend=="POSITIV" and not excessive and (i.performance_20d_pct or 0)>0 and (i.performance_20d_pct or 0)<18)
    if momentum:return "MOMENTUM","Hohe Qualität mit positivem, nicht extrem überkauftem Trend."
    if falling:return "WATCH","Gute Qualität, aber negatives SMA-/60-Tage-Bild; kein positives Opportunity-Signal."
    return "WATCH","Qualität vorhanden, das technische Signal für 1–3 Monate ist noch nicht überzeugend."
