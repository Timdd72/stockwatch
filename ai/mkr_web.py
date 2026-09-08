"""Gezielte, gecachte Web-Ergänzungen für MKR – niemals technische Indikatoren."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from database.models import ProviderCache
from .mkr_schemas import MkrInputData, MkrSource, MkrWebFinding, MkrWebResearch
from .service import AiAnalysisError, AiAnalysisService, AiBudgetExceeded


TOPIC_TTLS = {
    "catalysts": timedelta(hours=12),
    "fundamentals": timedelta(days=3),
    "uni_structure": timedelta(days=7),
}
TOPIC_FRAMEWORK = {"catalysts": 10, "fundamentals": 10, "uni_structure": 12}

MKR_WEB_INSTRUCTIONS = """Du ergänzt eine MKR-14-Analyse ausschließlich um belegte externe Fakten.
Verwende als topic ausschließlich eines der ausdrücklich angeforderten Research-Themen. Die
MKR-Framework-Zuordnung erfolgt ausschließlich lokal durch StockWatch; entscheide oder interpretiere
sie nicht selbst. Das kompatibilitätsbedingt vorhandene Feld framework_number wird von StockWatch
ignoriert. Lokale technische Werte sind
autoritativ und dürfen weder gesucht noch ersetzt werden. Suche keine EMA, SMA, RSI, MACD, ATR,
Bollinger, ADX, TRIX, FVG oder Fibonacci-Werte. Options Flow, Dark Pool und Block Trades bleiben ohne
belastbare spezialisierte Daten NOT_AVAILABLE. Jede Aussage benötigt mindestens eine vom Web-Tool
tatsächlich gelieferte Quelle. Bevorzuge Investor Relations, Regulierungs-/Börsenmitteilungen und
seriöse Finanznachrichten. Katalysatoren bevorzugt aus den letzten 90 Tagen; Earnings/Guidance aus
dem aktuellsten Bericht; Analystenziele und Insidertransaktionen nur mit Datum. Erfinde oder
kombiniere keine Zahlen. Gib bei fehlender belastbarer Quelle einfach kein Finding aus."""

_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_TRACKING_QUERY_KEYS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
})


@dataclass(frozen=True, slots=True)
class MkrWebContext:
    findings: tuple[MkrWebFinding, ...]
    web_search_calls: int
    used_web_search: bool
    cache_hits: int
    failed: bool = False
    diagnostics: tuple["MkrWebFindingDiagnostic", ...] = ()


@dataclass(frozen=True, slots=True)
class MkrWebFindingDiagnostic:
    finding_index: int
    topic: str
    model_framework_number: int
    assigned_framework_number: int | None
    model_url: str | None
    authoritative_url: str | None
    match_type: str | None
    accepted: bool
    reason: str


class MkrWebResearchService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        ai: AiAnalysisService,
        *,
        max_web_search_calls: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._ai = ai
        self.max_web_search_calls = max_web_search_calls or _positive_env_int(
            "MKR_MAX_WEB_SEARCH_CALLS", 5
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_diagnostics: tuple[MkrWebFindingDiagnostic, ...] = ()

    def collect(self, stock_id: int, data: MkrInputData) -> MkrWebContext:
        self.last_diagnostics = ()
        topics = self._needed_topics(data)
        if not topics:
            return MkrWebContext((), 0, False, 0)

        symbol = str(data.stock.get("symbol") or stock_id)
        findings: list[MkrWebFinding] = []
        missing: list[str] = []
        for topic in topics:
            cached = self._cached(symbol, topic)
            if cached is None:
                missing.append(topic)
            else:
                findings.extend(cached)
        if not missing:
            return MkrWebContext(tuple(findings), 0, False, len(topics))

        request = json.dumps({
            "company": {
                "name": data.stock.get("name"), "symbol": data.stock.get("symbol"),
                "exchange": data.stock.get("exchange"), "currency": data.stock.get("currency"),
            },
            "requested_topics": missing,
            "available_local_external_data": {
                "fundamentals": data.fundamental_data,
                "analyst": data.analyst_data,
                "earnings": data.earnings_data,
                "news": data.news_data,
            },
        }, ensure_ascii=False, sort_keys=True, default=str)
        try:
            call = self._ai.request_structured(
                request, MkrWebResearch, MKR_WEB_INSTRUCTIONS,
                purpose="mkr_analysis", stock_id=stock_id,
                tools=[{"type": "web_search"}],
                max_tool_calls=max(1, self.max_web_search_calls - 1),
                include=["web_search_call.action.sources"],
                tool_type="web_search",
            )
        except (AiAnalysisError, AiBudgetExceeded):
            return MkrWebContext(tuple(findings), 0, False, len(topics) - len(missing), True)
        if call.web_search_calls > self.max_web_search_calls:
            return MkrWebContext(tuple(findings), call.web_search_calls, True,
                                 len(topics) - len(missing), True)

        output = call.output
        assert isinstance(output, MkrWebResearch)
        verified = self._verified_findings(output.findings, call.web_sources, missing)
        for topic in missing:
            topic_findings = [item for item in verified if item.topic == topic]
            # Auch ein belastbar leeres Suchergebnis wird bis zum jeweiligen TTL gecacht.
            self._store(symbol, topic, topic_findings)
        findings.extend(verified)
        return MkrWebContext(tuple(findings), call.web_search_calls, True,
                             len(topics) - len(missing), diagnostics=self.last_diagnostics)

    @staticmethod
    def _needed_topics(data: MkrInputData) -> tuple[str, ...]:
        quality = {item.number: item.data_quality.value for item in data.framework_availability}
        topics: list[str] = []
        if quality.get(10) != "GOOD":
            topics.extend(("catalysts", "fundamentals"))
        if quality.get(12) != "GOOD":
            topics.append("uni_structure")
        return tuple(topics)

    def _verified_findings(
        self, findings: list[MkrWebFinding], response_sources: tuple[dict, ...],
        requested_topics: list[str],
    ) -> list[MkrWebFinding]:
        exact = {item.get("url"): item for item in response_sources if item.get("url")}
        normalized: dict[str, list[dict]] = {}
        for evidence in exact.values():
            canonical = _normalize_source_url(str(evidence["url"]))
            if canonical:
                normalized.setdefault(canonical, []).append(evidence)
        verified: list[MkrWebFinding] = []
        now = self._clock()
        diagnostics: list[MkrWebFindingDiagnostic] = []
        for finding_index, finding in enumerate(findings):
            topic = finding.topic.value
            expected_framework = TOPIC_FRAMEWORK.get(topic)
            if topic not in requested_topics:
                diagnostics.append(MkrWebFindingDiagnostic(
                    finding_index, topic, finding.framework_number, expected_framework,
                    None, None, None, False, "TOPIC_NOT_REQUESTED",
                ))
                continue
            if expected_framework is None:
                diagnostics.append(MkrWebFindingDiagnostic(
                    finding_index, topic, finding.framework_number, None,
                    None, None, None, False, "TOPIC_NOT_ALLOWED",
                ))
                continue
            sources: list[MkrSource] = []
            for source in finding.sources:
                evidence = exact.get(source.url)
                match_type = "EXACT" if evidence is not None else None
                rejection = "URL_NOT_IN_RESPONSE"
                if evidence is None:
                    canonical = _normalize_source_url(source.url)
                    matches = normalized.get(canonical, []) if canonical else []
                    # Eine normalisierte Zuordnung ist nur bei genau einem echten
                    # Response-Beleg zulässig; Domain-Gleichheit genügt nie.
                    evidence = matches[0] if len(matches) == 1 else None
                    if evidence is not None:
                        match_type = "NORMALIZED"
                    elif len(matches) > 1:
                        rejection = "AMBIGUOUS_NORMALIZED_URL"
                if evidence is None:
                    diagnostics.append(MkrWebFindingDiagnostic(
                        finding_index, topic, finding.framework_number, expected_framework,
                        source.url, None, None, False, rejection,
                    ))
                    continue
                authoritative_url = str(evidence["url"])
                publisher = str(
                    evidence.get("publisher") or urlsplit(authoritative_url).netloc
                )
                sources.append(MkrSource(
                    title=str(evidence.get("title") or source.title),
                    url=authoritative_url,
                    source=publisher,
                    source_type="WEB",
                    publisher=publisher,
                    published_at=_datetime(evidence.get("published_at")),
                    accessed_at=now,
                    framework_number=expected_framework,
                    usage_note=source.usage_note or f"Beleg für {topic}",
                ))
                diagnostics.append(MkrWebFindingDiagnostic(
                    finding_index, topic, finding.framework_number, expected_framework,
                    source.url, authoritative_url, match_type, True, "ACCEPTED",
                ))
            if sources:
                verified.append(finding.model_copy(update={
                    "framework_number": expected_framework, "sources": sources,
                }))
        self.last_diagnostics = tuple(diagnostics)
        return verified

    def _cached(self, symbol: str, topic: str) -> list[MkrWebFinding] | None:
        cutoff = self._clock() - TOPIC_TTLS[topic]
        with self._sessions() as session:
            row = session.scalar(select(ProviderCache).where(
                ProviderCache.provider == "openai_web_search",
                ProviderCache.symbol == symbol,
                ProviderCache.data_type == f"mkr_web_{topic}",
                ProviderCache.fetched_at >= cutoff,
            ))
        if row is None:
            return None
        try:
            findings = [MkrWebFinding.model_validate(item) for item in json.loads(row.payload)]
            normalized: list[MkrWebFinding] = []
            for finding in findings:
                assigned = TOPIC_FRAMEWORK.get(finding.topic.value)
                if assigned is None:
                    continue
                sources = [source.model_copy(update={"framework_number": assigned})
                           for source in finding.sources]
                normalized.append(finding.model_copy(update={
                    "framework_number": assigned, "sources": sources,
                }))
            return normalized
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _store(self, symbol: str, topic: str, findings: list[MkrWebFinding]) -> None:
        payload = json.dumps([item.model_dump(mode="json") for item in findings],
                             ensure_ascii=False, sort_keys=True)
        with self._sessions.begin() as session:
            row = session.scalar(select(ProviderCache).where(
                ProviderCache.provider == "openai_web_search",
                ProviderCache.symbol == symbol,
                ProviderCache.data_type == f"mkr_web_{topic}",
            ))
            if row is None:
                session.add(ProviderCache(provider="openai_web_search", symbol=symbol,
                    data_type=f"mkr_web_{topic}", fetched_at=self._clock(), payload=payload))
            else:
                row.fetched_at = self._clock()
                row.payload = payload


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


def _datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_source_url(value: str) -> str | None:
    """Normalisiert ausschließlich artikelerhaltende URL-Varianten.

    Pfad und fachliche Query-Parameter bleiben vollständig erhalten. Entfernt
    werden nur Fragment, bekannte Tracking-Parameter und rein syntaktische
    Unterschiede. Dadurch ist dies ausdrücklich keine Domain-Zuordnung.
    """

    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname.lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        port = parsed.port
        if port and not ((parsed.scheme.lower() == "http" and port == 80) or
                         (parsed.scheme.lower() == "https" and port == 443)):
            hostname = f"{hostname}:{port}"
        path = _decode_unreserved(parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        query = [
            (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
        ]
        query.sort()
        return urlunsplit(("https", hostname, path, urlencode(query, doseq=True), ""))
    except (TypeError, ValueError):
        return None


def _decode_unreserved(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        return character if character in _UNRESERVED else f"%{match.group(1).upper()}"

    return re.sub(r"%([0-9A-Fa-f]{2})", replace, value)
