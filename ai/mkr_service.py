"""Persistenter MKR-14-Service auf Basis der vorhandenen Responses-Infrastruktur."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from database.models import AiUsage, MkrAnalysisRecord, MkrAnalysisSource, Stock
from .mkr_input import MkrInputAssembler
from .mkr_prompt import (
    MKR_LOCAL_ONLY_INSTRUCTIONS, MKR_PROMPT_VERSION,
    MKR_WEB_AUGMENTED_INSTRUCTIONS, build_mkr_prompt,
)
from .mkr_schemas import (
    MkrAnalysis, MkrDataQuality, MkrInputData, MkrSignal, MkrWebFinding,
)
from .service import AiAnalysisError, AiAnalysisService, AiBudgetExceeded
from .mkr_web import MkrWebContext, MkrWebResearchService


class MkrAnalysisAlreadyRunning(RuntimeError):
    pass


class MkrAnalysisFailed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MkrAnalysisRun:
    record: MkrAnalysisRecord
    result: MkrAnalysis
    reused: bool


class MkrAnalysisService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        assembler: MkrInputAssembler,
        ai_service: AiAnalysisService,
        web_research: MkrWebResearchService | None = None,
        enable_web_search: bool = True,
        clock=None,
    ) -> None:
        self._sessions = session_factory
        self._assembler = assembler
        self._ai = ai_service
        self._web = web_research or MkrWebResearchService(session_factory, ai_service)
        self._enable_web_search = enable_web_search
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def analyze(self, stock_id: int, force: bool = False) -> MkrAnalysisRun:
        if self._running(stock_id):
            raise MkrAnalysisAlreadyRunning(
                "Für diese Aktie läuft bereits eine MKR-14-Analyse."
            )
        input_data = self._assembler.build(stock_id)
        web = self._web.collect(stock_id, input_data) if self._enable_web_search else MkrWebContext(
            (), 0, False, 0
        )
        canonical = self._canonical_input(stock_id, input_data, web.findings)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        if not force:
            existing = self._successful_fingerprint(stock_id, fingerprint)
            if existing is not None:
                return MkrAnalysisRun(existing, self._result(existing), True)

        running = self._create_running(stock_id, input_data, fingerprint, web)
        try:
            call = self._ai.request_structured(
                build_mkr_prompt(canonical), MkrAnalysis,
                MKR_WEB_AUGMENTED_INSTRUCTIONS if web.findings else MKR_LOCAL_ONLY_INSTRUCTIONS,
                purpose="mkr_analysis", stock_id=stock_id, tool_type="model",
            )
            if not isinstance(call.output, MkrAnalysis):
                raise ValueError("OpenAI lieferte kein gültiges MKR-Ergebnis.")
            corrected = self._enforce_local_data_contract(
                call.output, input_data, web.findings
            )
            completed = self._complete(running.id, corrected)
            return MkrAnalysisRun(completed, corrected, False)
        except AiBudgetExceeded as exc:
            self._fail(running.id, "budget", str(exc))
            raise
        except AiAnalysisError as exc:
            error_type = self._latest_usage_error(stock_id, running.generated_at) or "api_error"
            self._fail(running.id, error_type, str(exc))
            raise MkrAnalysisFailed(str(exc)) from None
        except Exception:
            message = "OpenAI lieferte keine gültige strukturierte MKR-Analyse."
            self._fail(running.id, "invalid_output", message)
            raise MkrAnalysisFailed(message) from None

    def get_latest_successful(self, stock_id: int) -> MkrAnalysisRecord | None:
        with self._sessions() as session:
            return session.scalar(select(MkrAnalysisRecord).where(
                MkrAnalysisRecord.stock_id == stock_id,
                MkrAnalysisRecord.status == "COMPLETED",
            ).order_by(MkrAnalysisRecord.completed_at.desc(), MkrAnalysisRecord.id.desc()).limit(1))

    def get_latest(self, stock_id: int) -> MkrAnalysisRecord | None:
        with self._sessions() as session:
            return session.scalar(select(MkrAnalysisRecord).where(
                MkrAnalysisRecord.stock_id == stock_id,
            ).order_by(MkrAnalysisRecord.generated_at.desc(), MkrAnalysisRecord.id.desc()).limit(1))

    def get_history(self, stock_id: int, limit: int = 20) -> list[MkrAnalysisRecord]:
        if limit < 1 or limit > 200:
            raise ValueError("limit muss zwischen 1 und 200 liegen.")
        with self._sessions() as session:
            return list(session.scalars(select(MkrAnalysisRecord).where(
                MkrAnalysisRecord.stock_id == stock_id,
            ).order_by(MkrAnalysisRecord.generated_at.desc(), MkrAnalysisRecord.id.desc()).limit(limit)))

    def get_sources(self, analysis_id: int) -> list[MkrAnalysisSource]:
        with self._sessions() as session:
            return list(session.scalars(select(MkrAnalysisSource).where(
                MkrAnalysisSource.mkr_analysis_id == analysis_id,
            ).order_by(MkrAnalysisSource.framework_number, MkrAnalysisSource.id)))

    @staticmethod
    def load_result(record: MkrAnalysisRecord) -> MkrAnalysis | None:
        return MkrAnalysis.model_validate_json(record.structured_result) \
            if record.structured_result else None

    def _create_running(
        self, stock_id: int, data: MkrInputData, fingerprint: str, web: MkrWebContext,
    ) -> MkrAnalysisRecord:
        price = data.price or {}
        timestamp = _parse_datetime(price.get("market_timestamp"))
        record = MkrAnalysisRecord(
            stock_id=stock_id, generated_at=self._clock(), status="RUNNING",
            model=self._ai.config.model, current_price=price.get("value"),
            price_timestamp=timestamp, quote_type=price.get("price_type"),
            prompt_version=MKR_PROMPT_VERSION, input_fingerprint=fingerprint,
            web_search_used=web.used_web_search, web_search_calls=web.web_search_calls,
        )
        try:
            with self._sessions.begin() as session:
                if session.get(Stock, stock_id) is None:
                    raise LookupError("Die Aktie wurde nicht gefunden.")
                session.add(record)
                session.flush()
            return record
        except IntegrityError:
            raise MkrAnalysisAlreadyRunning(
                "Für diese Aktie läuft bereits eine MKR-14-Analyse."
            ) from None

    def _complete(self, record_id: int, result: MkrAnalysis) -> MkrAnalysisRecord:
        with self._sessions.begin() as session:
            record = session.get(MkrAnalysisRecord, record_id)
            if record is None or record.status != "RUNNING":
                raise RuntimeError("Der MKR-Lauf ist nicht mehr aktiv.")
            record.status = "COMPLETED"
            record.completed_at = self._clock()
            record.structured_result = result.model_dump_json()
            record.confidence = result.confidence
            record.data_coverage_full = result.data_coverage.full
            record.data_coverage_limited = result.data_coverage.limited
            record.data_coverage_unavailable = result.data_coverage.not_available
            self._save_sources(session, record.id, result)
            return record

    def _fail(self, record_id: int, error_type: str, message: str) -> None:
        with self._sessions.begin() as session:
            record = session.get(MkrAnalysisRecord, record_id)
            if record is None:
                return
            record.status = "FAILED"
            record.completed_at = self._clock()
            record.error_type = error_type
            record.error_message = _safe_error(message)

    def _successful_fingerprint(
        self, stock_id: int, fingerprint: str,
    ) -> MkrAnalysisRecord | None:
        with self._sessions() as session:
            return session.scalar(select(MkrAnalysisRecord).where(
                MkrAnalysisRecord.stock_id == stock_id,
                MkrAnalysisRecord.input_fingerprint == fingerprint,
                MkrAnalysisRecord.status == "COMPLETED",
            ).order_by(MkrAnalysisRecord.completed_at.desc(), MkrAnalysisRecord.id.desc()).limit(1))

    def _running(self, stock_id: int) -> bool:
        with self._sessions() as session:
            return session.scalar(select(MkrAnalysisRecord.id).where(
                MkrAnalysisRecord.stock_id == stock_id,
                MkrAnalysisRecord.status == "RUNNING",
            ).limit(1)) is not None

    @staticmethod
    def _canonical_input(
        stock_id: int, data: MkrInputData,
        web_findings: tuple[MkrWebFinding, ...] = (),
    ) -> str:
        payload = {
            "stock_id": stock_id,
            "mkr_input": data.model_dump(mode="json"),
            "verified_web_findings": [item.model_dump(mode="json") for item in web_findings],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _enforce_local_data_contract(
        self, result: MkrAnalysis, data: MkrInputData,
        web_findings: tuple[MkrWebFinding, ...] = (),
    ) -> MkrAnalysis:
        payload = result.model_dump(mode="json")
        input_quality = {item.number: item.data_quality for item in data.framework_availability}
        allowed_urls = {item.get("url") for item in data.news_data if item.get("url")}
        web_by_framework = _web_sources_by_framework(web_findings)
        currency = str(data.stock.get("currency") or "").upper()

        for framework in payload["frameworks"]:
            quality = input_quality[framework["number"]]
            verified_web = web_by_framework.get(framework["number"], [])
            web_can_augment = framework["number"] in (10, 12, 14) and bool(verified_web)
            if quality == MkrDataQuality.INSUFFICIENT and not web_can_augment:
                framework.update(signal=MkrSignal.NOT_AVAILABLE.value, strength=None,
                                 confidence=min(framework["confidence"], 20),
                                 data_quality=MkrDataQuality.INSUFFICIENT.value)
                framework["levels"] = []
                framework["explanation"] = _insufficient_explanation(framework["explanation"])
            elif quality == MkrDataQuality.INSUFFICIENT and web_can_augment:
                if framework["data_quality"] == "GOOD":
                    framework["data_quality"] = "LIMITED"
            elif _quality_rank(framework["data_quality"]) > _quality_rank(quality.value):
                framework["data_quality"] = quality.value
            framework["explanation"] = _correct_deterministic_relationships(
                framework["explanation"], data, force_facts=framework["number"] in (7, 13)
            )
            framework["sources"] = _sanitize_sources(
                framework.get("sources", []), allowed_urls, framework["number"], verified_web)
            framework["sources"] = _merge_web_sources(
                framework["sources"], verified_web, limit=12
            )
            has_local = quality != MkrDataQuality.INSUFFICIENT
            framework["data_origin"] = (
                "MIXED" if has_local and verified_web else "WEB" if verified_web else
                "LOCAL" if has_local else "NOT_AVAILABLE"
            )
            _normalize_levels(framework.get("levels", []), currency, framework["number"])

        framework_nine = next(item for item in payload["frameworks"] if item["number"] == 9)
        if framework_nine["data_quality"] == "INSUFFICIENT":
            payload["options"] = {
                "available": False,
                "statement": "Für diese Aktie stehen derzeit keine ausreichenden Optionsdaten zur Verfügung.",
                "sources": [],
            }
            payload["wheel"] = {
                "available": False,
                "statement": "Ohne verlässliche Optionsdaten ist keine Wheel-/CSP-Auswertung möglich.",
                "sources": [],
            }

        for key, level in payload["price_levels"].items():
            if level is not None:
                level["level_type"] = "PRICE"
                level["currency"] = currency
        for key in ("trigger_price", "stop_invalidation"):
            if payload["entry_timing"][key] is not None:
                payload["entry_timing"][key]["level_type"] = "PRICE"
                payload["entry_timing"][key]["currency"] = currency
        all_web_sources = [source for sources in web_by_framework.values() for source in sources]
        payload["sources"] = _sanitize_sources(
            payload.get("sources", []), allowed_urls, None, all_web_sources)
        payload["sources"] = _merge_web_sources(payload["sources"], all_web_sources)
        payload["summary"] = [
            _correct_deterministic_relationships(value, data) for value in payload["summary"]
        ]
        payload["risks"] = [
            _correct_deterministic_relationships(value, data) for value in payload["risks"]
        ]
        payload["avoid_if"] = _correct_deterministic_relationships(
            payload["avoid_if"], data, max_length=1000
        )
        payload["entry_timing"]["alerts"] = [
            _correct_deterministic_relationships(value, data)
            for value in payload["entry_timing"]["alerts"]
        ]
        payload["generated_at"] = self._clock().isoformat()
        qualities = [item["data_quality"] for item in payload["frameworks"]]
        payload["data_coverage"] = {
            "full": qualities.count("GOOD"), "limited": qualities.count("LIMITED"),
            "not_available": qualities.count("INSUFFICIENT"),
        }
        return MkrAnalysis.model_validate(payload)

    @staticmethod
    def _save_sources(session: Session, analysis_id: int, result: MkrAnalysis) -> None:
        unique: set[tuple[int | None, str]] = set()
        sources = [source for item in result.frameworks for source in item.sources]
        sources.extend(result.sources)
        for source in sources:
            if source.source_type.value != "WEB" or not source.url:
                continue
            key = (source.framework_number, source.url)
            if key in unique:
                continue
            unique.add(key)
            session.add(MkrAnalysisSource(
                mkr_analysis_id=analysis_id,
                framework_number=source.framework_number,
                source_type=source.source_type.value,
                title=source.title,
                url=source.url,
                publisher=source.publisher or source.source,
                published_at=source.published_at,
                accessed_at=source.accessed_at or datetime.now(timezone.utc),
                usage_note=source.usage_note,
            ))

    def _latest_usage_error(self, stock_id: int, since: datetime) -> str | None:
        with self._sessions() as session:
            return session.scalar(select(AiUsage.error_type).where(
                AiUsage.stock_id == stock_id, AiUsage.purpose == "mkr_analysis",
                AiUsage.success.is_(False), AiUsage.timestamp >= since,
            ).order_by(AiUsage.timestamp.desc(), AiUsage.id.desc()).limit(1))

    @staticmethod
    def _result(record: MkrAnalysisRecord) -> MkrAnalysis:
        result = MkrAnalysisService.load_result(record)
        if result is None:
            raise RuntimeError("Gespeicherte MKR-Analyse enthält kein Ergebnis.")
        return result


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _safe_error(value: str) -> str:
    # Öffentliche Servicefehler enthalten bereits keine Rohantwort oder Secrets.
    return " ".join(value.split())[:1000]


def _insufficient_explanation(value: str) -> str:
    suffix = " Lokale StockWatch-Daten sind für dieses Framework nicht ausreichend."
    return value if suffix.strip() in value else (value.rstrip() + suffix)[:1500]


def _source_type(number: int | None) -> str:
    if number in (1, 2, 3, 4, 5, 6, 7, 8, 11, 13):
        return "STOCKWATCH_TECHNICAL"
    if number in (10, 12):
        return "STOCKWATCH_FUNDAMENTALS"
    if number == 14:
        return "STOCKWATCH_POSITION"
    return "STOCKWATCH_TECHNICAL"


def _sanitize_sources(
    sources: list[dict], allowed_urls: set[str], number: int | None,
    allowed_web: list[dict] | None = None,
) -> list[dict]:
    cleaned: list[dict] = []
    web_by_url = {item.get("url"): item for item in (allowed_web or []) if item.get("url")}
    for source in sources:
        url = source.get("url")
        if url in web_by_url:
            cleaned.append(dict(web_by_url[url]))
            continue
        is_news = bool(url and url in allowed_urls)
        cleaned.append({
            "title": str(source.get("title") or "Lokale StockWatch-Daten")[:500],
            "url": url if is_news else None,
            "source": str(source.get("source") or "StockWatch")[:255],
            "source_type": "STOCKWATCH_NEWS" if is_news else _source_type(number),
            "publisher": source.get("publisher") if is_news else None,
            "published_at": source.get("published_at") if is_news else None,
            "accessed_at": None,
            "framework_number": number,
            "usage_note": source.get("usage_note"),
        })
    return cleaned


def _web_sources_by_framework(
    findings: tuple[MkrWebFinding, ...],
) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for finding in findings:
        for source in finding.sources:
            payload = source.model_dump(mode="json")
            payload["framework_number"] = finding.framework_number
            payload["usage_note"] = source.usage_note or finding.fact[:500]
            grouped.setdefault(finding.framework_number, []).append(payload)
    return grouped


def _merge_web_sources(
    existing: list[dict], web_sources: list[dict], *, limit: int = 30,
) -> list[dict]:
    merged = list(existing)
    urls = {item.get("url") for item in merged if item.get("url")}
    for source in web_sources:
        if source.get("url") not in urls:
            merged.append(dict(source))
            urls.add(source.get("url"))
    return merged[:limit]


def _quality_rank(value: str) -> int:
    return {"INSUFFICIENT": 0, "LIMITED": 1, "GOOD": 2}[value]


def _normalize_levels(levels: list[dict], currency: str, framework_number: int) -> None:
    for level in levels:
        basis = str(level.get("basis") or "").lower()
        if re.search(r"fibonacci|retracement|extension|golden\s+pocket", basis):
            # Prozentangaben bezeichnen hier die Fibonacci-Stufe; der Wert ist ein Kurs.
            level_type = "PRICE"
        elif re.search(r"put\s*/?\s*call|verhältnis|ratio", basis):
            level_type = "RATIO"
        elif (re.search(r"%|prozent|performance|volatil", basis) or
              re.search(r"volumen.*(?:über|unter|abweichung|differenz).*durchschnitt", basis)):
            level_type = "PERCENTAGE"
        elif re.search(r"\brsi\d*\b|\btrix\d*\b|\badx\d*\b|\bmacd\b|\bobv\b", basis):
            level_type = "INDICATOR"
        elif framework_number == 4:
            level_type = "INDICATOR"
        elif _is_quantity_basis(basis):
            level_type = "QUANTITY"
        else:
            level_type = "PRICE"
        level["level_type"] = level_type
        level["currency"] = currency if level_type == "PRICE" else None


def _is_quantity_basis(basis: str) -> bool:
    """Erkennt eindeutig absolute Mengen, nicht monetäre Volumina oder Quoten."""

    if re.search(r"\b(?:eur|usd|gbp|chf|jpy)\b|[€$£¥]|\b(?:umsatz|erlös|revenue)\b", basis):
        return False
    return re.search(
        r"\b(?:handelsvolumen|daily[-\s]?volumen|volumendurchschnitt|"
        r"durchschnittliches?\s+(?:handels)?volumen|volumen|"
        r"orders?|bestellungen?|auftragszahl|auftragseingang|auftragsbestand|"
        r"deliveries|auslieferungen?|stückzahl|stücke?|anzahl|shares?|aktien|flugzeuge?)\b",
        basis,
    ) is not None


def _correct_deterministic_relationships(
    explanation: str, data: MkrInputData, *, force_facts: bool = False,
    max_length: int = 1500,
) -> str:
    explanation = _correct_rsi_wording(explanation)
    technical = data.technical_data or {}
    price = (data.price or {}).get("value")
    comparisons = (
        ("EMA20", technical.get("ema20"), "EMA50", technical.get("ema50")),
        ("Kurs", price, "EMA20", technical.get("ema20")),
        ("Kurs", price, "EMA50", technical.get("ema50")),
        ("EMA50", technical.get("ema50"), "EMA200", technical.get("ema200")),
        ("SMA50", technical.get("sma50"), "SMA200", technical.get("sma200")),
    )
    known = [item for item in comparisons if item[1] is not None and item[3] is not None]
    if not known:
        return explanation

    mentions_relationship = any(
        left.lower() in explanation.lower() and right.lower() in explanation.lower()
        for left, _, right, _ in known
    )
    if not force_facts and not mentions_relationship:
        return explanation

    sentences = re.split(r"(?<=[.!?])\s+", explanation.strip())
    retained = [sentence for sentence in sentences if not any(
        _contradicts(sentence, left, float(a), right, float(b))
        for left, a, right, b in known
    )]
    facts = "; ".join(
        f"{left} ({float(a):.5g}) liegt {_relation(float(a), float(b))} "
        f"{right} ({float(b):.5g})"
        for left, a, right, b in known
    )
    canonical = f"Deterministische StockWatch-Beziehungen: {facts}."
    return f"{' '.join(retained).strip()} {canonical}".strip()[:max_length]


def _correct_rsi_wording(value: str) -> str:
    """Trennt einen schwachen RSI sprachlich von einem negativ gerichteten MACD."""

    value = re.sub(
        r"\b(RSI(?:14)?)\s+(?:und|sowie)\s+(?:der\s+)?(MACD(?:-Histogramm)?)\s+"
        r"(?:sind|ist)\s+negativ\b",
        lambda match: f"{match.group(1)} ist schwach und {match.group(2)} ist negativ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(RSI(?:14)?)\s+ist\s+negativ\b",
        lambda match: f"{match.group(1)} ist schwach",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\bnegativer\s+(RSI(?:14)?)\b(?![-\s]*divergenz)",
        lambda match: f"schwacher {match.group(1)}",
        value,
        flags=re.IGNORECASE,
    )


def _relation(left: float, right: float) -> str:
    return "über" if left > right else "unter" if left < right else "gleichauf mit"


def _contradicts(sentence: str, left: str, left_value: float,
                 right: str, right_value: float) -> bool:
    if left_value > right_value:
        wrong = r"(?:unter|kleiner(?:\s+als)?|<)"
    elif left_value < right_value:
        wrong = r"(?:über|größer(?:\s+als)?|>)"
    else:
        wrong = r"(?:unter|über|kleiner(?:\s+als)?|größer(?:\s+als)?|<|>)"
    pattern = (
        rf"{re.escape(left)}\s*(?:\([^)]*\))?\s*(?:liegt|ist)?\s*{wrong}\s*"
        rf"(?:dem\s+)?{re.escape(right)}"
    )
    return re.search(pattern, sentence, flags=re.IGNORECASE) is not None
