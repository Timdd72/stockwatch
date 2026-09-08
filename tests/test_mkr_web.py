from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from sqlalchemy import delete, func, select

from ai.config import AiConfig
from ai.mkr_input import MkrInputAssembler
from ai.mkr_schemas import MkrAnalysis, MkrWebResearch
from ai.mkr_service import MkrAnalysisService
from ai.mkr_web import MkrWebResearchService
from ai.service import AiAnalysisService, StructuredAiRun
from analysis import TechnicalIndicators
from database import (
    AiUsage, MkrAnalysisSource, ProviderCache, StockWatchService,
    create_database, create_session_factory,
)
from tests.test_mkr_service import mkr_output


SOURCE_URL = "https://investors.example.com/report.pdf"


def web_output(*, source_url: str = SOURCE_URL, framework: int = 10,
               topic: str = "fundamentals") -> MkrWebResearch:
    return MkrWebResearch.model_validate({"findings": [{
        "topic": topic,
        "framework_number": framework,
        "fact": "Der aktuelle Unternehmensbericht bestätigt die Guidance.",
        "as_of": "2026-09-01T10:00:00Z",
        "sources": [{
            "title": "Investor report", "url": source_url,
            "source": "Example IR", "source_type": "WEB",
            "publisher": "Example IR", "published_at": "2026-09-01T10:00:00Z",
            "framework_number": framework, "usage_note": "Aktuelle Guidance",
        }],
    }]})


class FakeWebAi:
    def __init__(self, output: MkrWebResearch, *, calls: int = 1,
                 sources: tuple[dict, ...] | None = None) -> None:
        self.output = output
        self.web_calls = calls
        self.sources = sources if sources is not None else ({
            "url": SOURCE_URL, "title": "Investor report", "publisher": "Example IR",
            "published_at": "2026-09-01T10:00:00Z",
        },)
        self.calls: list[tuple[tuple, dict]] = []

    def request_structured(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return StructuredAiRun(
            self.output, 100, 20, 120, Decimal("0.001"), self.web_calls, self.sources
        )


class SequencedResponses:
    def __init__(self, *, web_result=None, web_error: Exception | None = None) -> None:
        self.web_result = web_result or web_output()
        self.web_error = web_error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tools"):
            if self.web_error:
                raise self.web_error
            return SimpleNamespace(
                output_parsed=self.web_result,
                usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
                output=[SimpleNamespace(
                    type="web_search_call",
                    action=SimpleNamespace(sources=[SimpleNamespace(
                        url=SOURCE_URL, title="Investor report", publisher="Example IR",
                        published_at="2026-09-01T10:00:00Z",
                    )]),
                )],
            )
        return SimpleNamespace(
            output_parsed=mkr_output(),
            usage=SimpleNamespace(input_tokens=1200, output_tokens=800, total_tokens=2000),
            output=[],
        )


class MkrWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_database(Path(self.temp.name) / "mkr-web.db")
        self.sessions = create_session_factory(self.engine)
        stocks = StockWatchService(self.sessions)
        self.stock, _, _ = stocks.initialize_airbus()
        stocks.save_analysis("AIR.PAR", TechnicalIndicators(
            208.05, 1, 2, 3, 4, 205, 200, None, 55, 22, 1.5, 4, "POSITIV"),
            price_type="DAILY_CLOSE", provider="alpha_vantage")
        self.input = MkrInputAssembler(self.sessions).build(self.stock.id)
        self.config = AiConfig(
            "configured-test-model", Decimal("8"), Decimal("1"), Decimal("2"), 24,
            Decimal("0.01"),
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def test_no_web_search_when_external_frameworks_are_locally_good(self) -> None:
        payload = self.input.model_dump(mode="json")
        for item in payload["framework_availability"]:
            if item["number"] in (10, 12):
                item.update(data_quality="GOOD", coverage="FULL")
        complete = type(self.input).model_validate(payload)
        fake = FakeWebAi(web_output())
        result = MkrWebResearchService(self.sessions, fake).collect(self.stock.id, complete)
        self.assertEqual(fake.calls, [])
        self.assertFalse(result.used_web_search)

    def test_only_external_topics_are_bundled_and_five_call_cap_is_enforced(self) -> None:
        fake = FakeWebAi(web_output())
        result = MkrWebResearchService(self.sessions, fake, max_web_search_calls=5).collect(
            self.stock.id, self.input)
        kwargs = fake.calls[0][1]
        self.assertEqual(kwargs["tools"], [{"type": "web_search"}])
        self.assertEqual(kwargs["max_tool_calls"], 4)
        self.assertNotIn("technical_data", fake.calls[0][0][0])
        self.assertEqual(result.web_search_calls, 1)

        excessive = FakeWebAi(web_output(), calls=6)
        limited = MkrWebResearchService(
            self.sessions, excessive, max_web_search_calls=5,
            clock=lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
        ).collect(self.stock.id, self.input)
        self.assertTrue(limited.failed)
        self.assertEqual(limited.findings, ())
        # Der Modellwert ist nur noch ein ignoriertes Kompatibilitätsfeld.
        ignored = web_output().model_dump(mode="json")
        ignored["findings"][0]["framework_number"] = 13
        self.assertEqual(MkrWebResearch.model_validate(
            ignored).findings[0].framework_number, 13)

    def test_unverified_url_is_dropped_and_cache_prevents_second_search(self) -> None:
        missing = FakeWebAi(web_output(), sources=())
        result = MkrWebResearchService(self.sessions, missing).collect(self.stock.id, self.input)
        self.assertEqual(result.findings, ())
        with self.sessions.begin() as session:
            session.execute(delete(ProviderCache).where(
                ProviderCache.provider == "openai_web_search"))

        fake = FakeWebAi(web_output())
        service = MkrWebResearchService(self.sessions, fake)
        first = service.collect(self.stock.id, self.input)
        second = service.collect(self.stock.id, self.input)
        self.assertEqual(len(first.findings), 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(second.cache_hits, 3)
        with self.sessions() as session:
            self.assertIsNotNone(session.scalar(select(ProviderCache).where(
                ProviderCache.data_type == "mkr_web_fundamentals")))

    def test_exact_response_url_is_accepted(self) -> None:
        service = MkrWebResearchService(
            self.sessions, FakeWebAi(web_output())
        )
        result = service.collect(self.stock.id, self.input)
        self.assertEqual(result.findings[0].sources[0].url, SOURCE_URL)
        self.assertEqual(result.diagnostics[0].match_type, "EXACT")
        self.assertTrue(result.diagnostics[0].accepted)

    def test_conservatively_equivalent_url_uses_authoritative_response_url(self) -> None:
        response_url = "https://example.com/reports/~airbus?q=annual&year=2026"
        model_url = (
            "http://www.EXAMPLE.com/reports/%7Eairbus/?year=2026&q=annual"
            "&utm_source=assistant#result"
        )
        fake = FakeWebAi(
            web_output(source_url=model_url),
            sources=({"url": response_url, "title": "Airbus report",
                      "publisher": "Example IR"},),
        )
        result = MkrWebResearchService(self.sessions, fake).collect(
            self.stock.id, self.input)
        self.assertEqual(result.findings[0].sources[0].url, response_url)
        self.assertEqual(result.diagnostics[0].match_type, "NORMALIZED")

    def test_different_article_on_same_domain_is_not_accepted(self) -> None:
        fake = FakeWebAi(
            web_output(source_url="https://example.com/reports/claimed"),
            sources=({"url": "https://example.com/reports/actual",
                      "title": "Actual", "publisher": "Example"},),
        )
        result = MkrWebResearchService(self.sessions, fake).collect(
            self.stock.id, self.input)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.diagnostics[0].reason, "URL_NOT_IN_RESPONSE")

    def test_topic_assigns_framework_and_ignores_model_framework(self) -> None:
        cases = (("catalysts", 10), ("fundamentals", 10), ("uni_structure", 12))
        for topic, expected in cases:
            with self.subTest(topic=topic):
                output = web_output(topic=topic, framework=14)
                service = MkrWebResearchService(self.sessions, FakeWebAi(output))
                verified = service._verified_findings(
                    output.findings,
                    ({"url": SOURCE_URL, "title": "Report", "publisher": "IR"},),
                    [topic],
                )
                self.assertEqual(verified[0].framework_number, expected)
                self.assertEqual(verified[0].sources[0].framework_number, expected)
                self.assertEqual(service.last_diagnostics[0].model_framework_number, 14)
                self.assertEqual(service.last_diagnostics[0].assigned_framework_number, expected)

    def test_cached_legacy_framework_is_reassigned_from_topic(self) -> None:
        with self.sessions.begin() as session:
            session.add(ProviderCache(
                provider="openai_web_search", symbol="AIR.PAR",
                data_type="mkr_web_catalysts", fetched_at=datetime.now(timezone.utc),
                payload=f"[{web_output(framework=14).findings[0].model_dump_json()}]",
            ))
        cached = MkrWebResearchService(
            self.sessions, FakeWebAi(web_output())
        )._cached("AIR.PAR", "catalysts")
        self.assertEqual(cached[0].framework_number, 10)
        self.assertEqual(cached[0].sources[0].framework_number, 10)

    def test_empty_findings_create_no_sources(self) -> None:
        empty = MkrWebResearch(findings=[])
        result = MkrWebResearchService(
            self.sessions, FakeWebAi(empty)
        ).collect(self.stock.id, self.input)
        self.assertEqual(result.findings, ())

    def test_sources_usage_quality_and_origin_are_persisted(self) -> None:
        responses = SequencedResponses()
        client = SimpleNamespace(responses=responses)
        ai = AiAnalysisService(self.sessions, self.config, lambda: client)
        service = MkrAnalysisService(
            self.sessions, MkrInputAssembler(self.sessions), ai,
            MkrWebResearchService(self.sessions, ai),
        )
        run = service.analyze(self.stock.id)
        framework = run.result.frameworks[9]
        self.assertEqual(framework.data_quality.value, "LIMITED")
        self.assertEqual(framework.data_origin.value, "WEB")
        web_source = next(item for item in framework.sources if item.source_type.value == "WEB")
        self.assertEqual(web_source.url, SOURCE_URL)
        self.assertEqual(web_source.publisher, "Example IR")
        self.assertTrue(run.record.web_search_used)
        self.assertEqual(run.record.web_search_calls, 1)
        self.assertEqual(run.result.frameworks[8].data_quality.value, "INSUFFICIENT")
        with self.sessions() as session:
            source = session.scalar(select(MkrAnalysisSource))
            self.assertEqual(source.url, SOURCE_URL)
            self.assertEqual(source.framework_number, 10)
            usage = list(session.scalars(select(AiUsage).order_by(AiUsage.id)))
        self.assertEqual([(item.purpose, item.tool_type) for item in usage], [
            ("mkr_analysis", "web_search"), ("mkr_analysis", "model")])
        self.assertEqual(service.get_sources(run.record.id)[0].url, SOURCE_URL)

    def test_web_failure_continues_with_local_analysis(self) -> None:
        responses = SequencedResponses(web_error=ConnectionError("offline"))
        ai = AiAnalysisService(
            self.sessions, self.config, lambda: SimpleNamespace(responses=responses)
        )
        run = MkrAnalysisService(
            self.sessions, MkrInputAssembler(self.sessions), ai,
            MkrWebResearchService(self.sessions, ai),
        ).analyze(self.stock.id)
        self.assertEqual(run.record.status, "COMPLETED")
        self.assertFalse(run.record.web_search_used)
        self.assertEqual(run.result.frameworks[9].data_quality.value, "INSUFFICIENT")
        self.assertEqual(len(responses.calls), 2)
        with self.sessions() as session:
            failed = session.scalar(select(func.count()).select_from(AiUsage).where(
                AiUsage.tool_type == "web_search", AiUsage.success.is_(False)))
        self.assertEqual(failed, 1)

    def test_existing_analysis_without_sources_remains_readable(self) -> None:
        result = mkr_output()
        self.assertEqual(result.sources, [])
        self.assertIsInstance(MkrAnalysis.model_validate_json(result.model_dump_json()), MkrAnalysis)


if __name__ == "__main__":
    unittest.main()
