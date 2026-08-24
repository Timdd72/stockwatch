"""Zentrale Laufzeit- und Finnhub-Taktung der Chancensuche."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import os,time
from pathlib import Path
from typing import Callable
from sqlalchemy import select
from sqlalchemy.orm import Session,sessionmaker
from database.models import ApiUsage

class OpportunityTimeout(RuntimeError):pass

@dataclass(frozen=True,slots=True)
class OpportunityRuntimeConfig:
    max_runtime_minutes:int=10
    finnhub_requests_per_minute:int=45
    max_ai_candidates:int=3
    max_web_search_calls:int=5
    @classmethod
    def from_env(cls,path:str|Path=".env")->"OpportunityRuntimeConfig":
        values=_env(path)
        return cls(_positive(os.getenv("OPPORTUNITY_MAX_RUNTIME_MINUTES") or values.get("OPPORTUNITY_MAX_RUNTIME_MINUTES"),10),
            _positive(os.getenv("OPPORTUNITY_FINNHUB_REQUESTS_PER_MINUTE") or values.get("OPPORTUNITY_FINNHUB_REQUESTS_PER_MINUTE"),45),
            _positive(os.getenv("OPPORTUNITY_MAX_AI_CANDIDATES") or values.get("OPPORTUNITY_MAX_AI_CANDIDATES"),3),
            _positive(os.getenv("OPPORTUNITY_MAX_WEB_SEARCH_CALLS") or values.get("OPPORTUNITY_MAX_WEB_SEARCH_CALLS"),5))

class OpportunityRateGate:
    def __init__(self,sessions:sessionmaker[Session],limit:int,deadline:datetime,
                 clock:Callable[[],datetime]|None=None,sleeper:Callable[[float],None]|None=None)->None:
        self._sessions,self.limit,self.deadline=sessions,limit,deadline
        self._clock=clock or (lambda:datetime.now(timezone.utc));self._sleep=sleeper or time.sleep
    def before_request(self)->None:
        while True:
            now=self._clock()
            if now>=self.deadline:raise OpportunityTimeout("Maximale Laufzeit erreicht.")
            cutoff=now-timedelta(seconds=60)
            with self._sessions() as s:
                rows=list(s.scalars(select(ApiUsage.timestamp).where(ApiUsage.provider=="finnhub",
                    ApiUsage.timestamp>=cutoff).order_by(ApiUsage.timestamp.asc())))
            if len(rows)<self.limit:return
            oldest=rows[0] if rows[0].tzinfo else rows[0].replace(tzinfo=timezone.utc)
            wait=max(.05,(oldest+timedelta(seconds=60)-now).total_seconds())
            if now+timedelta(seconds=wait)>=self.deadline:raise OpportunityTimeout("Maximale Laufzeit während Rate-Limit-Wartezeit erreicht.")
            self._sleep(wait)

def _env(path:str|Path)->dict[str,str]:
    try:lines=Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:return {}
    return {k.strip():v.strip().strip('"').strip("'") for line in lines if line.strip() and not line.lstrip().startswith("#") and "=" in line for k,v in [line.split("=",1)]}
def _positive(value:str|None,default:int)->int:
    try:n=int(value) if value else default
    except ValueError:return default
    return n if n>0 else default
