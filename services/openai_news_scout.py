"""OpenAI-Web-Search-Scout mit lokaler Symbolvalidierung und Quellenpersistenz."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timezone
import hashlib
from sqlalchemy import or_,select
from sqlalchemy.orm import Session,sessionmaker

from ai.news_scout_schemas import NewsScoutOutput
from ai.service import AiAnalysisError,AiAnalysisService
from database.models import OpportunityNewsEvent,SecurityCatalog

SCOUT_INSTRUCTIONS="""Du bist der News Scout von StockWatch. Suche mit Web Search nach konkreten,
seriös belegten Unternehmensereignissen der letzten 24 bis 48 Stunden für börsennotierte
US-Unternehmen, insbesondere NASDAQ und NYSE. Priorisiere Großaufträge, überraschend gute
Geschäftszahlen, Prognoseanhebungen, wichtige Produkte, FDA-/Behördenzulassungen, bedeutende
Partnerschaften, Übernahmen, Aktienrückkäufe, große neue Kunden, technologische Durchbrüche und
positive regulatorische Entscheidungen. Verwirf allgemeine Marktkommentare, reine Kursmeldungen,
unkonkrete PR, wiederholte Meldungen und Gerüchte ohne seriöse Primär- oder Qualitätsquelle.
Gib höchstens zehn Kandidaten aus. Verwende das tatsächlich an der US-Börse gehandelte Symbol,
eine konkrete Quellen-URL und keine erfundenen Fakten. Dies ist nur ein Nachrichtenscout, keine
Kaufempfehlung. Anlagehorizont der späteren Prüfung: 1-3 months."""

@dataclass(frozen=True,slots=True)
class OpenAiScoutResult:
    catalog_ids:tuple[int,...]
    news_found:int
    symbols_identified:int
    web_search_calls:int

class OpenAiNewsScoutService:
    MODEL="gpt-5.6-luna"
    def __init__(self,sessions:sessionmaker[Session],ai:AiAnalysisService,max_web_search_calls:int=5,
                 clock=None)->None:
        self._sessions,self._ai=sessions,ai;self.max_web_search_calls=max_web_search_calls
        self._clock=clock or (lambda:datetime.now(timezone.utc))

    def scout(self,search_run_id:int)->OpenAiScoutResult:
        # Die API lieferte historisch einmal ein Call-Item mehr als max_tool_calls. Ein Call
        # Sicherheitsreserve plus die harte Nachprüfung halten das StockWatch-Limit ein.
        provider_tool_cap=max(1,self.max_web_search_calls-1)
        call=self._ai.request_structured(
            "Finde aktuelle, konkrete US-Unternehmensnachrichten aus den letzten 24–48 Stunden.",
            NewsScoutOutput,SCOUT_INSTRUCTIONS,purpose="opportunity_web_scout",model=self.MODEL,
            tools=[{"type":"web_search"}],max_tool_calls=provider_tool_cap,
            include=["web_search_call.action.sources"])
        if call.web_search_calls>self.max_web_search_calls:
            raise AiAnalysisError(
                f"OpenAI-Web-Search-Limit überschritten ({call.web_search_calls}/{self.max_web_search_calls}); Scout-Ergebnis wird verworfen."
            )
        output=call.output;assert isinstance(output,NewsScoutOutput)
        ids:list[int]=[]
        for item in output.candidates[:10]:
            catalog=self._catalog(item.symbol)
            if catalog is None:continue
            ids.append(catalog.id);external=hashlib.sha256(item.source_url.encode()).hexdigest()
            with self._sessions.begin() as s:
                exists=s.scalar(select(OpportunityNewsEvent).where(
                    OpportunityNewsEvent.provider=="openai_web_search",or_(
                        OpportunityNewsEvent.external_news_id==external,
                        OpportunityNewsEvent.url==item.source_url)))
                if exists:continue
                s.add(OpportunityNewsEvent(provider="openai_web_search",external_news_id=external,
                    url=item.source_url,published_at=item.published_at,symbol=catalog.symbol,
                    catalog_id=catalog.id,headline=item.headline,summary=item.summary,
                    source=item.source_name,event_type=item.event_type.upper(),
                    event_score=item.relevance_score,direction_hint=item.expected_direction,
                    processed_at=self._clock(),opportunity_search_run_id=search_run_id))
        return OpenAiScoutResult(tuple(dict.fromkeys(ids)),len(output.candidates),len(set(ids)),call.web_search_calls)

    def _catalog(self,symbol:str)->SecurityCatalog|None:
        normalized=symbol.strip().upper()
        with self._sessions() as s:
            rows=list(s.scalars(select(SecurityCatalog).where(
                SecurityCatalog.security_type=="stock",SecurityCatalog.country=="US",
                or_(SecurityCatalog.symbol==normalized,
                    SecurityCatalog.provider_symbol_finnhub==normalized))))
            unique={row.id:row for row in rows}
            return next(iter(unique.values())) if len(unique)==1 else None
