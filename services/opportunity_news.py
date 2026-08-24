"""News-first-Erkennung für den US-Opportunity-Scanner."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Any, Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from database.models import OpportunityNewsEvent, SecurityCatalog
from market_data.finnhub import EndpointResult, FinnhubProvider
from market_data.usage import ApiLimitExceeded


EVENT_RULES: tuple[tuple[str, tuple[str,...], float, str], ...] = (
    ("PROFIT_WARNING",("profit warning","gewinnwarnung","cuts forecast","lowers guidance"),92,"NEGATIVE"),
    ("GUIDANCE_RAISE",("raises guidance","boosts forecast","prognoseanhebung"),86,"POSITIVE"),
    ("LARGE_ORDER",("major order","large order","contract worth","billion contract","großauftrag"),82,"POSITIVE"),
    ("M_AND_A",("acquisition","takeover offer","to acquire","merger agreement"),82,"NEUTRAL"),
    ("REGULATORY",("fda approval","regulatory approval","regulatory investigation","antitrust investigation"),80,"NEUTRAL"),
    ("EARNINGS_SURPRISE",("beats estimates","misses estimates","earnings surprise"),78,"NEUTRAL"),
    ("LEGAL_DECISION",("court ruling","jury verdict","settlement"),72,"NEUTRAL"),
    ("PRODUCT_EVENT",("major product launch","strategic partnership","technology breakthrough"),68,"POSITIVE"),
    ("OPERATIONS",("production halt","supply disruption","recall","lost customer"),78,"NEGATIVE"),
    ("CAPITAL_ACTION",("share offering","capital increase","buyback"),66,"NEUTRAL"),
)
NOISE=("conference participation","fireside chat","investor conference","podcast","interview")

@dataclass(frozen=True,slots=True)
class NewsFirstResult:
    catalog_ids: tuple[int,...]
    events_created: int
    events_seen: int
    requests: int

def classify_event(headline:str,summary:str|None,published_at:datetime,now:datetime)->tuple[str,float,str]|None:
    text=f"{headline} {summary or ''}".lower()
    if any(word in text for word in NOISE):return None
    for event_type,words,base,direction in EVENT_RULES:
        if any(word in text for word in words):
            age=max(0,(now-published_at).total_seconds())
            recency=10 if age<=86400 else 5 if age<=3*86400 else 0
            concrete=5 if re.search(r"\b(?:\$|usd|million|billion|\d{2,})\b",text) else 0
            return event_type,min(100,base+recency+concrete),direction
    return None

class OpportunityNewsService:
    """Nutzt Finnhubs marktweiten ``news?category=general``-Endpunkt."""
    def __init__(self,sessions:sessionmaker[Session],finnhub_factory:Callable[[],FinnhubProvider],
                 clock:Callable[[],datetime]|None=None)->None:
        self._sessions,self._factory=sessions,finnhub_factory
        self._clock=clock or (lambda:datetime.now(timezone.utc))

    def collect(self,search_run_id:int,request_gate:Callable[[],None]|None=None)->NewsFirstResult:
        if request_gate:request_gate()
        result=self._factory().request("general-news","news",category="general")
        if result.availability=="API-Fehler" and result.message and "429" in result.message:
            raise ApiLimitExceeded("Finnhub meldet HTTP 429.")
        if result.availability!="verfügbar" or not isinstance(result.data,list):return NewsFirstResult((),0,0,1)
        created=seen=0; ids:set[int]=set()
        for raw in result.data:
            if not isinstance(raw,dict):continue
            catalog=self._catalog(raw)
            if catalog is None:continue
            published=_published(raw.get("datetime")); classified=classify_event(
                str(raw.get("headline") or ""),raw.get("summary"),published,self._clock())
            if classified is None:continue
            seen+=1;event_type,score,direction=classified
            external=str(raw.get("id")) if raw.get("id") is not None else hashlib.sha256(
                f"{catalog.symbol}|{published.isoformat()}|{raw.get('headline')}".encode()).hexdigest()
            url=str(raw.get("url")) if raw.get("url") else None
            with self._sessions.begin() as s:
                duplicate=s.scalar(select(OpportunityNewsEvent.id).where(
                    OpportunityNewsEvent.provider=="finnhub",or_(
                        OpportunityNewsEvent.external_news_id==external if external else False,
                        OpportunityNewsEvent.url==url if url else False)))
                if duplicate:continue
                row=OpportunityNewsEvent(provider="finnhub",external_news_id=external,url=url,
                    published_at=published,symbol=catalog.symbol,catalog_id=catalog.id,
                    headline=str(raw.get("headline")),summary=raw.get("summary"),source=raw.get("source"),
                    event_type=event_type,event_score=score,direction_hint=direction,
                    processed_at=self._clock(),opportunity_search_run_id=search_run_id)
                s.add(row);created+=1;ids.add(catalog.id)
        with self._sessions() as s:
            ordered=list(s.scalars(select(OpportunityNewsEvent.catalog_id).where(
                OpportunityNewsEvent.opportunity_search_run_id==search_run_id)
                .order_by(OpportunityNewsEvent.event_score.desc())))
        return NewsFirstResult(tuple(dict.fromkeys(ordered)),created,seen,1)

    def _catalog(self,raw:dict[str,Any])->SecurityCatalog|None:
        related=str(raw.get("related") or "")
        symbols={x.strip().upper() for x in related.split(",") if x.strip()}
        if len(symbols)!=1:return None
        symbol=next(iter(symbols))
        with self._sessions() as s:
            rows=list(s.scalars(select(SecurityCatalog).where(
                SecurityCatalog.source=="NASDAQ",SecurityCatalog.security_type=="stock",
                SecurityCatalog.provider_symbol_finnhub==symbol)))
            return rows[0] if len(rows)==1 else None

def _published(value:Any)->datetime:
    try:return datetime.fromtimestamp(int(value),timezone.utc)
    except (TypeError,ValueError,OSError):return datetime.now(timezone.utc)
