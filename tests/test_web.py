"""Tests für die lokale FastAPI-Weboberfläche."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import select

from analysis import TechnicalIndicators
from database import (
    MkrAnalysisRecord,
    Position,
    Stock,
    StockAiAnalysis,
    StockWatchService,
    calculate_position_metrics,
)
from database import SecurityCatalog, create_database, create_session_factory
from database.models import MkrAnalysisSource
from web.main import create_app
from services import StockUpdateReport, UpdateItem


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "web_test.db"
        self.service = StockWatchService.from_path(self.database_path)
        self.service.initialize_airbus()
        catalog_session_factory = create_session_factory(create_database(self.database_path))
        with catalog_session_factory.begin() as session:
            session.add(
                SecurityCatalog(
                    name="Alphabet Inc Class A",
                    symbol="GOOGL",
                    isin="US02079K3059",
                    exchange="NASDAQ",
                    country="United States",
                    currency="USD",
                    security_type="Common Stock",
                    source="test",
                )
            )
            session.add(
                Stock(
                    name="Alphabet Inc Class C",
                    symbol="GOOG",
                    exchange="NASDAQ",
                    currency="USD",
                )
            )
        self.service.save_analysis(
            "AIR.PAR",
            TechnicalIndicators(
                current_close=200.0,
                previous_day_change_pct=1.25,
                performance_5d_pct=-2.5,
                performance_20d_pct=3.0,
                performance_60d_pct=10.0,
                sma20=198.0,
                sma50=190.0,
                sma200=None,
                rsi14=55.0,
                volatility_20d_pct=24.0,
                distance_sma20_pct=1.0,
                distance_sma50_pct=5.0,
                trend="POSITIV",
            ),
        )
        self.app = create_app(self.database_path)
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temp_dir.cleanup()

    async def test_dashboard_shows_saved_data_without_market_data_request(self) -> None:
        response = await self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Airbus SE", response.text)
        self.assertIn("AIR.PAR", response.text)
        self.assertIn("200,00 EUR", response.text)
        self.assertIn("Keine aktive Position", response.text)
        self.assertIn("POSITIV", response.text)
        self.assertIn("Schluss", response.text)

    async def test_daily_close_is_never_labeled_current_price(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            air_id=session.scalar(select(Stock.id).where(Stock.symbol=="AIR.PAR"))
        response=await self.client.get(f"/stocks/{air_id}")
        self.assertIn("Letzter verfügbarer Schlusskurs",response.text)
        self.assertNotIn("Aktueller Kurs",response.text)

    async def test_manual_price_accepts_comma_and_dot_and_keeps_page_local(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            air_id=session.scalar(select(Stock.id).where(Stock.symbol=="AIR.PAR"))
        self.app.state.stockwatch_service.set_external_quote_url(air_id,"https://www.finanzen.net/realtimekurs/airbus")
        with patch("requests.sessions.Session.get") as network:
            page=await self.client.get(f"/stocks/{air_id}")
        network.assert_not_called();self.assertIn("Kurs extern prüfen",page.text)
        self.assertIn('target="_blank"',page.text)
        first=await self.client.post(f"/stocks/{air_id}/manual-price",data={"manual_price":"206,10"},follow_redirects=False)
        self.assertEqual(first.status_code,303)
        second=await self.client.post(f"/stocks/{air_id}/manual-price",data={"manual_price":"207.25"},follow_redirects=False)
        self.assertEqual(second.status_code,303)
        detail=await self.client.get(f"/stocks/{air_id}")
        self.assertIn("Manuell eingetragener Kurs",detail.text);self.assertIn("207,25 EUR",detail.text)
        self.assertIn("SMA20",detail.text)

    async def test_invalid_manual_price_is_rejected(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            air_id=session.scalar(select(Stock.id).where(Stock.symbol=="AIR.PAR"))
        for value in ("0","-1","abc"):
            response=await self.client.post(f"/stocks/{air_id}/manual-price",data={"manual_price":value})
            self.assertEqual(response.status_code,422)

    async def test_normal_pages_never_call_external_http(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
        with patch("requests.sessions.Session.get") as network:
            for path in ("/", f"/stocks/{goog_id}", f"/stocks/{goog_id}/providers", "/scheduler", "/opportunities", "/securities/search?q=GOOG"):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200)
        network.assert_not_called()

    async def test_opportunities_page_is_local_and_never_calls_openai(self) -> None:
        scanner = self.app.state.opportunity_scanner
        scanner.run = Mock()
        ai = self.app.state.ai_analysis_service
        ai.analyze_stock = Mock()
        scanner_ai = self.app.state.scanner_ai
        scanner_ai.run_latest = Mock()
        response = await self.client.get("/opportunities")
        self.assertEqual(response.status_code, 200)
        self.assertIn("US Opportunity Scanner", response.text)
        self.assertIn("Noch kein Scanner-Lauf", response.text)
        scanner.run.assert_not_called()
        ai.analyze_stock.assert_not_called()
        scanner_ai.run_latest.assert_not_called()
        self.assertIn("Chancen suchen", response.text)

    async def test_pages_do_not_start_ai_analysis_and_manual_button_uses_service(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
        ai_service = self.app.state.ai_analysis_service
        ai_service.analyze_stock = Mock()

        detail = await self.client.get(f"/stocks/{goog_id}")
        dashboard = await self.client.get("/")
        scheduler = await self.client.get("/scheduler")
        self.assertEqual((detail.status_code, dashboard.status_code, scheduler.status_code), (200, 200, 200))
        self.assertIn("Noch keine KI-Analyse vorhanden.", detail.text)
        self.assertIn("Jetzt analysieren", detail.text)
        self.assertIn("OpenAI:", dashboard.text)
        ai_service.analyze_stock.assert_not_called()

        ai_service.analyze_stock.return_value = SimpleNamespace(reused=False)
        response = await self.client.post(f"/stocks/{goog_id}/ai/analyze", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        ai_service.analyze_stock.assert_called_once_with(goog_id)

    async def test_stock_detail_without_mkr_analysis_is_local(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            airbus_id = session.scalar(select(Stock.id).where(Stock.symbol == "AIR.PAR"))
        mkr = self.app.state.mkr_analysis_service
        mkr.analyze = Mock()

        with patch("requests.sessions.Session.get") as network:
            response = await self.client.get(f"/stocks/{airbus_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("MKR-Analyse", response.text)
        self.assertIn("Für diese Aktie liegt noch keine MKR-Analyse vor.", response.text)
        self.assertIn("MKR-Analyse starten", response.text)
        mkr.analyze.assert_not_called()
        network.assert_not_called()

    async def test_stock_detail_renders_completed_mkr_scorecard_levels_and_sources(self) -> None:
        from tests.test_mkr_service import mkr_output

        factory = create_session_factory(create_database(self.database_path))
        with factory.begin() as session:
            airbus_id = session.scalar(select(Stock.id).where(Stock.symbol == "AIR.PAR"))
            result = mkr_output().model_dump(mode="json")
            origins = ("LOCAL", "WEB", "MIXED", "NOT_AVAILABLE")
            for index, origin in enumerate(origins):
                result["frameworks"][index]["data_origin"] = origin
            result["frameworks"][0]["levels"] = [{
                "value": 199.46, "level_type": "PRICE", "currency": "EUR", "basis": "Kurs",
            }]
            result["frameworks"][1]["levels"] = [{
                "value": 3.01, "level_type": "PERCENTAGE", "currency": None,
                "basis": "Volumenabweichung",
            }]
            result["frameworks"][2]["levels"] = [{
                "value": 37.37, "level_type": "INDICATOR", "currency": None, "basis": "RSI14",
            }]
            result["frameworks"][3]["levels"] = [{
                "value": 1250000, "level_type": "QUANTITY", "currency": None,
                "basis": "Handelsvolumen",
            }]
            parsed = __import__("ai", fromlist=["MkrAnalysis"]).MkrAnalysis.model_validate(result)
            now = datetime.now(timezone.utc)
            record = MkrAnalysisRecord(
                stock_id=airbus_id, generated_at=now, completed_at=now,
                status="COMPLETED", model="test-model", current_price=199.46,
                price_timestamp=now, quote_type="DAILY_CLOSE", prompt_version="1.2",
                structured_result=parsed.model_dump_json(), input_fingerprint="m" * 64,
                confidence=parsed.confidence, data_coverage_full=14,
                data_coverage_limited=0, data_coverage_unavailable=0,
                web_search_used=True, web_search_calls=1,
            )
            session.add(record)
            session.flush()
            session.add(MkrAnalysisSource(
                mkr_analysis_id=record.id, framework_number=10, source_type="WEB",
                title="Airbus Investor Relations", url="https://example.test/airbus-report",
                publisher="Airbus", published_at=now, accessed_at=now,
                usage_note="Aktueller Bericht",
            ))

        response = await self.client.get(f"/stocks/{airbus_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("MKR Scorecard", response.text)
        self.assertIn("Framework 1", response.text)
        self.assertIn("Framework 14", response.text)
        for origin in ("LOCAL", "WEB", "MIXED", "NOT_AVAILABLE"):
            self.assertIn(origin, response.text)
        self.assertIn("199,46 EUR", response.text)
        self.assertIn("3,01 %", response.text)
        self.assertIn("37,37000", response.text)
        self.assertIn("1.250.000,00000", response.text)
        self.assertNotIn("37,37000 EUR", response.text)
        self.assertNotIn("1.250.000,00000 EUR", response.text)
        self.assertIn("Zusammenfassung", response.text)
        self.assertIn("Airbus Investor Relations", response.text)
        self.assertIn('href="https://example.test/airbus-report"', response.text)
        self.assertIn('target="_blank"', response.text)

    async def test_mkr_missing_sources_and_failed_latest_keep_success_visible(self) -> None:
        from tests.test_mkr_service import mkr_output

        factory = create_session_factory(create_database(self.database_path))
        with factory.begin() as session:
            airbus_id = session.scalar(select(Stock.id).where(Stock.symbol == "AIR.PAR"))
            older = datetime(2026, 9, 1, tzinfo=timezone.utc)
            completed = MkrAnalysisRecord(
                stock_id=airbus_id, generated_at=older, completed_at=older,
                status="COMPLETED", model="test-model", current_price=199.46,
                price_timestamp=older, quote_type="DAILY_CLOSE", prompt_version="1.2",
                structured_result=mkr_output().model_dump_json(), input_fingerprint="s" * 64,
                confidence=74, data_coverage_full=14, data_coverage_limited=0,
                data_coverage_unavailable=0, web_search_used=False, web_search_calls=0,
            )
            failed = MkrAnalysisRecord(
                stock_id=airbus_id, generated_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
                completed_at=datetime(2026, 9, 2, tzinfo=timezone.utc), status="FAILED",
                model="test-model", prompt_version="1.2", input_fingerprint="f" * 64,
                web_search_used=False, web_search_calls=0, error_type="api_error",
                error_message="Analyse derzeit nicht verfügbar.",
            )
            session.add_all((completed, failed))

        response = await self.client.get(f"/stocks/{airbus_id}")

        self.assertIn("Fehlgeschlagen", response.text)
        self.assertIn("Framework 14", response.text)
        self.assertIn("Keine externen Quellen gespeichert.", response.text)
        self.assertIn("erfolgreiche Analyse vom", response.text)

    async def test_mkr_post_uses_existing_service_and_handles_errors(self) -> None:
        from ai import MkrAnalysisAlreadyRunning

        with create_session_factory(create_database(self.database_path))() as session:
            airbus_id = session.scalar(select(Stock.id).where(Stock.symbol == "AIR.PAR"))
        service = self.app.state.mkr_analysis_service
        service.analyze = Mock(return_value=SimpleNamespace(reused=False))

        response = await self.client.post(
            f"/stocks/{airbus_id}/mkr-analysis", follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("mkr_message=", response.headers["location"])
        service.analyze.assert_called_once_with(airbus_id)

        service.analyze.reset_mock(side_effect=True)
        service.analyze.side_effect = MkrAnalysisAlreadyRunning(
            "Für diese Aktie läuft bereits eine MKR-14-Analyse."
        )
        failed = await self.client.post(
            f"/stocks/{airbus_id}/mkr-analysis", follow_redirects=True,
        )
        self.assertEqual(failed.status_code, 200)
        self.assertIn("läuft bereits", failed.text)

    async def test_ai_box_shows_separate_position_and_entry_ratings(self) -> None:
        with create_session_factory(create_database(self.database_path)).begin() as session:
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
            now = datetime.now(timezone.utc)
            session.add(StockAiAnalysis(
                stock_id=goog_id, model="test-model", rating="WATCH", confidence=.71,
                position_rating="HOLD", position_confidence=.76,
                entry_rating="WATCH", entry_confidence=.71,
                outlook="NEUTRAL", risk="MEDIUM", summary="Fundamental gut, technisch schwach.",
                position_reason="Gewinne nicht allein wegen kurzfristiger Schwäche aufgeben.",
                entry_reason="Für einen Neukauf auf bessere Technik warten.",
                reasons_json="[]", risks_json="[]", time_horizon="1-3 months",
                data_timestamp=now, input_fingerprint="a" * 64,
            ))
        ai_call = self.app.state.ai_analysis_service.analyze_stock = Mock()
        response = await self.client.get(f"/stocks/{goog_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bestehende Position", response.text)
        self.assertIn("HALTEN", response.text)
        self.assertIn("Neukauf", response.text)
        self.assertIn("BEOBACHTEN", response.text)
        self.assertIn("Horizont: 1–3 Monate", response.text)
        ai_call.assert_not_called()

    async def test_scheduler_page_and_manual_buttons_are_independent(self) -> None:
        page = await self.client.get("/scheduler")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Automatische Aktualisierung AUS", page.text)
        self.assertIn("Europe/Berlin", page.text)
        self.assertIn("America/New_York", page.text)
        dashboard = await self.client.get("/")
        self.assertIn("Aktualisierungsplan und Einstellungen", dashboard.text)
        self.assertIn("Automatik AUS", dashboard.text)

        with create_session_factory(create_database(self.database_path))() as session:
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
        updater = self.app.state.market_update_service
        updater.update_quote = Mock()
        response = await self.client.post(f"/stocks/{goog_id}/update/quote", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        updater.update_quote.assert_called_once_with(goog_id)

    async def test_dashboard_lists_airbus_and_goog_without_goog_snapshot(self) -> None:
        usage_before = self.app.state.api_usage_service.status()
        response = await self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Meine Aktien", response.text)
        self.assertIn("AIR.PAR", response.text)
        self.assertIn("GOOG", response.text)
        self.assertIn("Alphabet Inc Class C", response.text)
        self.assertIn("Noch keine Marktdaten vorhanden", response.text)

        with create_session_factory(create_database(self.database_path))() as session:
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
        self.assertIn(f'href="/stocks/{goog_id}"', response.text)
        detail = await self.client.get(f"/stocks/{goog_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Noch keine Marktdaten vorhanden", detail.text)
        self.assertNotIn("Veränderung Vortag</span>", detail.text)
        usage_after = self.app.state.api_usage_service.status()
        self.assertEqual(usage_before, usage_after)

    async def test_compact_dashboard_shows_price_position_trend_and_status(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            airbus_id = session.scalar(select(Stock.id).where(Stock.symbol == "AIR.PAR"))
        self.service.add_position(airbus_id, Decimal("150"), Decimal("2"), date(2026, 1, 2))
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("200,00 EUR", response.text)
        self.assertIn("100,00 EUR · 33,33 %", response.text)
        self.assertIn("POSITIV", response.text)
        self.assertIn("Performance 20 Tage", response.text)
        self.assertIn("Alpha Vantage:", response.text)
        self.assertNotIn('<section class="api-usage card">', response.text)
        self.assertIn("Automatik AUS", response.text)
        self.assertIn("Systemstatus", response.text)
        self.assertIn('alt="Conny AI – Börsenmaklerin für StockWatch"', response.text)
        self.assertIn('src="data:image/webp;base64,', response.text)
        self.assertIn('class="conny-assistant"', response.text)

    async def test_update_buttons_target_the_requested_stock(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            airbus_id = session.scalar(select(Stock.id).where(Stock.symbol == "AIR.PAR"))
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
        updater = self.app.state.market_update_service
        updater.update_alpha_vantage = Mock()
        updater.update_finnhub_quote = Mock()

        alpha_response = await self.client.post(
            f"/stocks/{airbus_id}/update/alpha", follow_redirects=False
        )
        finnhub_response = await self.client.post(
            f"/stocks/{goog_id}/update/finnhub/quote", follow_redirects=False
        )

        self.assertEqual(alpha_response.status_code, 303)
        self.assertEqual(finnhub_response.status_code, 303)
        updater.update_alpha_vantage.assert_called_once_with(airbus_id)
        updater.update_finnhub_quote.assert_called_once_with(goog_id)

    async def test_api_action_ui_disables_buttons_and_shows_spinner(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            airbus_id=session.scalar(select(Stock.id).where(Stock.symbol=="AIR.PAR"))
        detail=await self.client.get(f"/stocks/{airbus_id}")
        opportunities=await self.client.get("/opportunities")
        script=(Path(__file__).parents[1]/"web/static/action-state.js").read_text()
        self.assertIn("data-api-action",detail.text)
        self.assertIn("data-api-action",opportunities.text)
        self.assertIn("button.disabled = true",script)
        self.assertIn("action-spinner",script)

    async def test_failed_opportunity_run_does_not_publish_provisional_candidates(self) -> None:
        from database.models import OpportunitySearchRun,ScannerCandidate,ScannerRun
        from services.opportunity_candidates import OpportunityCandidateService
        factory=create_session_factory(create_database(self.database_path))
        with factory.begin() as s:
            catalog=s.scalar(select(SecurityCatalog).where(SecurityCatalog.symbol=="GOOGL"))
            good=ScannerRun(status="COMPLETED");s.add(good);s.flush()
            s.add(ScannerCandidate(scanner_run_id=good.id,catalog_id=catalog.id,symbol="OLD",name="Previous result",
                quality_score=80,opportunity_score=70,status="SHORTLISTED",reason="ok"))
            s.add(OpportunitySearchRun(status="COMPLETED",scanner_run_id=good.id,message="ok"))
            failed=ScannerRun(status="COMPLETED");s.add(failed);s.flush()
            s.add(ScannerCandidate(scanner_run_id=failed.id,catalog_id=catalog.id,symbol="AGYS",name="Provisional AGYS",
                quality_score=75,opportunity_score=66,status="SHORTLISTED",reason="provisional"))
            s.add(OpportunitySearchRun(status="FAILED",scanner_run_id=failed.id,message="technical failed"))
        OpportunityCandidateService(factory).record_run(good.id)
        response=await self.client.get("/opportunities")
        self.assertIn("Previous result",response.text);self.assertNotIn("Provisional AGYS",response.text)

    async def test_opportunity_star_adds_and_removes_persistent_favorite_without_api(self) -> None:
        from database.models import OpportunitySearchRun,ScannerCandidate,ScannerRun,OpportunityWatchlist,Stock
        from services.opportunity_candidates import OpportunityCandidateService
        factory=create_session_factory(create_database(self.database_path))
        with factory.begin() as s:
            catalog=s.scalar(select(SecurityCatalog).where(SecurityCatalog.symbol=="GOOGL"))
            run=ScannerRun(status="COMPLETED");s.add(run);s.flush();run_id=run.id
            s.add(ScannerCandidate(scanner_run_id=run.id,catalog_id=catalog.id,symbol="GOOGL",name=catalog.name,
                price=200,quality_score=90,opportunity_score=80,status="SHORTLISTED",reason="Beobachten"))
            s.add(OpportunitySearchRun(status="COMPLETED",scanner_run_id=run.id,message="ok"))
        OpportunityCandidateService(factory).record_run(run_id)
        with patch("requests.sessions.Session.get") as network:
            page=await self.client.get("/opportunities")
            self.assertIn("☆",page.text)
            response=await self.client.post(f"/opportunities/{catalog.id}/favorite",data={"favorite":"1"},follow_redirects=False)
            watched=await self.client.get("/opportunities")
        network.assert_not_called();self.assertEqual(response.status_code,303)
        self.assertIn("MEINE BEOBACHTUNGSLISTE",watched.text);self.assertIn("★",watched.text)
        with factory() as s:
            row=s.scalar(select(OpportunityWatchlist).where(OpportunityWatchlist.catalog_id==catalog.id))
            self.assertTrue(row.favorite);self.assertIsNone(s.scalar(select(Stock).where(Stock.symbol=="GOOGL")))
        await self.client.post(f"/opportunities/{catalog.id}/favorite",data={"favorite":"0"},follow_redirects=False)
        with factory() as s:self.assertFalse(s.scalar(select(OpportunityWatchlist).where(OpportunityWatchlist.catalog_id==catalog.id)).favorite)

    async def test_server_side_double_submit_does_not_call_updater(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            airbus_id=session.scalar(select(Stock.id).where(Stock.symbol=="AIR.PAR"))
        run_id=self.app.state.action_guard.start(f"stock:{airbus_id}:market")
        self.app.state.stock_update_service.update_stock=Mock()
        response=await self.client.post(f"/stocks/{airbus_id}/update",follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertIn("update_error=",response.headers["location"])
        self.app.state.stock_update_service.update_stock.assert_not_called()
        self.app.state.action_guard.finish(run_id,True)

    async def test_provider_selection_is_saved_without_api_call(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
        usage_before = self.app.state.api_usage_service.status()
        response = await self.client.post(
            f"/stocks/{goog_id}/providers",
            data={
                "external_quote_url": "https://example.test/GOOG",
                "primary_quote": "alpha_vantage",
                "fallback_quote": "finnhub",
                "auto_quote": "on",
                "primary_history": "finnhub",
                "fallback_history": "alpha_vantage",
                "primary_fundamentals": "finnhub",
                "fallback_fundamentals": "alpha_vantage",
                "primary_news": "finnhub",
                "fallback_news": "none",
                "primary_analyst": "finnhub",
                "fallback_analyst": "none",
                "primary_earnings": "finnhub",
                "fallback_earnings": "none",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        setting = self.app.state.provider_settings_service.get_setting(goog_id, "quote")
        self.assertEqual(setting.primary_provider, "alpha_vantage")
        self.assertTrue(setting.auto_select)
        dashboard=self.app.state.stockwatch_service.get_stock_dashboard(goog_id)
        self.assertEqual(dashboard.stock.external_quote_url,"https://example.test/GOOG")
        provider_page = await self.client.get(f"/stocks/{goog_id}/providers")
        self.assertEqual(provider_page.status_code, 200)
        self.assertIn("Datenquelle wählen", provider_page.text)
        self.assertIn("Datenquellen prüfen", provider_page.text)
        self.assertEqual(usage_before, self.app.state.api_usage_service.status())

    async def test_provider_page_is_separate_and_update_buttons_remain_last(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
        usage_before = self.app.state.api_usage_service.status()
        detail = await self.client.get(f"/stocks/{goog_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(f'href="/stocks/{goog_id}/providers"', detail.text)
        self.assertNotIn('<section class="card provider-panel">', detail.text)
        self.assertIn("Jetzt aktualisieren", detail.text)
        self.assertNotIn(">Kurs aktualisieren<", detail.text)
        self.assertNotIn(">Historie aktualisieren<", detail.text)
        self.assertEqual(detail.text.count(">Jetzt aktualisieren<"), 1)
        self.assertNotIn('<section class="api-usage card">', detail.text)
        for label in ("Letzter Kurs", "Trend", "RSI14", "Performance 5 Tage", "Performance 20 Tage", "Performance 60 Tage", "SMA20", "SMA50"):
            self.assertIn(label, detail.text)

        providers = await self.client.get(f"/stocks/{goog_id}/providers")
        self.assertEqual(providers.status_code, 200)
        for label in ("Kurs", "Historie", "Fundamentals", "News", "Analysten", "Earnings"):
            self.assertIn(label, providers.text)
        self.assertIn("Zurück zur Aktie", providers.text)
        self.assertEqual(usage_before, self.app.state.api_usage_service.status())

    async def test_central_update_button_displays_compact_report(self) -> None:
        with create_session_factory(create_database(self.database_path))() as session:
            goog_id = session.scalar(select(Stock.id).where(Stock.symbol == "GOOG"))
        completed = datetime(2026, 8, 20, 12, 55, tzinfo=timezone.utc)
        service = self.app.state.stock_update_service
        service.update_stock = Mock(return_value=StockUpdateReport(
            goog_id, completed,
            (UpdateItem("quote", "finnhub", "updated", "aktualisiert"),
             UpdateItem("history", "alpha_vantage", "current", "bereits aktuell")),
            1,
        ))
        response = await self.client.post(f"/stocks/{goog_id}/update", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        service.update_stock.assert_called_once_with(goog_id)
        self.assertIn("Aktualisierung erfolgreich", response.text)
        self.assertIn("Details anzeigen", response.text)
        self.assertIn("Finnhub · aktualisiert", response.text)
        self.assertIn("Alpha Vantage · bereits aktuell", response.text)

    def test_position_value_calculation(self) -> None:
        position = Position(
            stock_id=1,
            purchase_price=Decimal("150.00"),
            quantity=Decimal("2.5"),
            purchase_date=date(2026, 1, 2),
        )
        result = calculate_position_metrics(position, 200.0)

        self.assertEqual(result.invested_amount, Decimal("375.000"))
        self.assertEqual(result.current_value, Decimal("500.00"))
        self.assertEqual(result.profit_loss, Decimal("125.000"))
        self.assertEqual(result.profit_loss_percent, Decimal("33.33333333333333333333333333"))

    async def test_valid_position_form_persists_through_service(self) -> None:
        response = await self.client.post(
            "/positions",
            data={
                "purchase_price": "150,50",
                "quantity": "2.5",
                "purchase_date": "2026-02-03",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Position wurde angelegt", response.text)
        self.assertIn("376,25 EUR", response.text)
        dashboard = self.service.get_airbus_dashboard()
        self.assertIsNotNone(dashboard.position)
        self.assertEqual(
            dashboard.position.position.purchase_price,  # type: ignore[union-attr]
            Decimal("150.5000"),
        )

    async def test_position_form_validation_rejects_invalid_values(self) -> None:
        invalid_forms = [
            ({"purchase_price": "0", "quantity": "2", "purchase_date": "2026-01-01"}, "Kaufpreis muss größer als null sein"),
            ({"purchase_price": "10", "quantity": "-1", "purchase_date": "2026-01-01"}, "Stückzahl muss größer als null sein"),
            ({"purchase_price": "10", "quantity": "2", "purchase_date": "kein-datum"}, "Kaufdatum muss ein gültiges Datum sein"),
        ]
        for form, message in invalid_forms:
            with self.subTest(form=form):
                response = await self.client.post("/positions", data=form)
                self.assertEqual(response.status_code, 422)
                self.assertIn(message, response.text)
                self.assertIsNone(self.service.get_airbus_dashboard().position)

    async def test_local_security_search_and_selection_endpoints(self) -> None:
        search = await self.client.get("/securities/search", params={"q": "Alphabet"})
        self.assertEqual(search.status_code, 200)
        self.assertIn("Alphabet Inc Class A", search.text)
        self.assertIn("US02079K3059", search.text)

        session_factory = create_session_factory(create_database(self.database_path))
        with session_factory() as session:
            security_id = session.scalar(
                select(SecurityCatalog.id).where(SecurityCatalog.symbol == "GOOGL")
            )
        selected = await self.client.post(
            f"/securities/{security_id}/select", follow_redirects=True
        )
        self.assertEqual(selected.status_code, 200)
        self.assertIn("GOOGL wurde zu StockWatch hinzugefügt", selected.text)

    async def test_security_selection_invokes_symbol_resolution_and_provider_page_allows_manual_mapping(self) -> None:
        resolver=self.app.state.provider_symbol_service
        original=resolver.resolve_alpha_vantage
        resolver.resolve_alpha_vantage=Mock()
        factory=create_session_factory(create_database(self.database_path))
        with factory() as session:
            security_id=session.scalar(select(SecurityCatalog.id).where(SecurityCatalog.symbol=="GOOGL"))
        selected=await self.client.post(f"/securities/{security_id}/select",follow_redirects=False)
        self.assertEqual(selected.status_code,303);resolver.resolve_alpha_vantage.assert_called_once()
        resolver.resolve_alpha_vantage=original
        with factory() as session:stock_id=session.scalar(select(Stock.id).where(Stock.symbol=="GOOGL"))
        saved=await self.client.post(f"/stocks/{stock_id}/providers",data={"alpha_vantage_symbol":"GOOGL"},follow_redirects=False)
        self.assertEqual(saved.status_code,303)
        page=await self.client.get(f"/stocks/{stock_id}/providers")
        self.assertIn("Lokales Symbol",page.text);self.assertIn("Alpha-Vantage-Symbol",page.text)
        self.assertIn('value="GOOGL"',page.text);self.assertIn("Status: verfügbar",page.text)


if __name__ == "__main__":
    unittest.main()
