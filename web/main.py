"""FastAPI-Anwendung für die lokale StockWatch-Übersicht."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
from urllib.parse import quote
from zoneinfo import ZoneInfo
from sqlalchemy import select

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from ai import AiAnalysisError, AiAnalysisService, AiBudgetExceeded

from database import (
    DEFAULT_DATABASE_PATH,
    ApiUsageService,
    SearchService,
    StockWatchService,
    create_database,
    create_session_factory,
)
from market_data.usage import ApiLimitExceeded
from market_data.alpha_vantage import AlphaVantageProvider
from market_data.finnhub import FinnhubProvider
from services import (
    DATA_TYPES,
    PROVIDERS,
    MarketUpdateError,
    MarketUpdateService,
    ProviderCapabilityService,
    ProviderSettingsService,
    OpportunityScannerService,
    ScannerAlreadyRunning,
    ScannerTechnicalService,
    ScannerAiService,
    OpportunitySearchService,
    OpenAiNewsScoutService,
    OpportunityRuntimeConfig,
    ActionAlreadyRunning,
    ActionGuard,
    ProviderSymbolService,
    TechnicalVerificationRunning,
    SchedulerService,
    StockUpdateService,
)


WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")
AI_RATING_LABELS = {
    "HOLD": "HALTEN", "ADD": "AUFSTOCKEN PRÜFEN", "REDUCE": "REDUZIEREN",
    "SELL": "VERKAUF PRÜFEN", "STRONG_SELL": "STARKES VERKAUFSSIGNAL",
    "STRONG_BUY": "STARKES KAUFSIGNAL", "BUY": "KAUF INTERESSANT",
    "WATCH": "BEOBACHTEN", "AVOID": "DERZEIT MEIDEN",
}


def number(value: object, decimals: int = 2) -> str:
    if value is None:
        return "nicht verfügbar"
    return f"{float(value):,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def value_class(value: object) -> str:
    if value is None:
        return "neutral"
    numeric = float(value)
    return "positive" if numeric > 0 else "negative" if numeric < 0 else "neutral"


def berlin_time(value: object) -> str:
    if not isinstance(value, datetime):
        return "–"
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y, %H:%M")


templates.env.filters["number"] = number
templates.env.filters["value_class"] = value_class
templates.env.filters["berlin_time"] = berlin_time


def create_app(database_path: str | Path = DEFAULT_DATABASE_PATH) -> FastAPI:
    application = FastAPI(title="StockWatch", docs_url=None, redoc_url=None)
    session_factory = create_session_factory(create_database(database_path))
    application.state.stockwatch_service = StockWatchService(session_factory)
    application.state.search_service = SearchService(session_factory)
    application.state.api_usage_service = ApiUsageService(session_factory)
    application.state.ai_analysis_service = AiAnalysisService(session_factory)
    application.state.market_update_service = MarketUpdateService(
        session_factory, application.state.api_usage_service
    )
    application.state.stock_update_service = StockUpdateService(
        session_factory, application.state.market_update_service
    )
    application.state.opportunity_scanner = OpportunityScannerService(
        session_factory, application.state.api_usage_service
    )
    application.state.scanner_technical = ScannerTechnicalService(
        session_factory, application.state.market_update_service
    )
    application.state.scanner_ai = ScannerAiService(
        session_factory, application.state.ai_analysis_service
    )
    opportunity_config=OpportunityRuntimeConfig.from_env()
    application.state.opportunity_news = OpenAiNewsScoutService(
        session_factory,application.state.ai_analysis_service,
        opportunity_config.max_web_search_calls
    )
    application.state.opportunity_search = OpportunitySearchService(
        session_factory, application.state.opportunity_scanner,
        application.state.scanner_technical, application.state.scanner_ai,
        config=opportunity_config,scout=application.state.opportunity_news
    )
    application.state.action_guard = ActionGuard(session_factory)
    application.state.provider_settings_service = ProviderSettingsService(session_factory)
    application.state.provider_symbol_service = ProviderSymbolService(
        session_factory,lambda:AlphaVantageProvider.from_env(
            usage_recorder=application.state.api_usage_service))
    application.state.provider_settings_service.ensure_all_stocks()
    application.state.provider_capability_service = ProviderCapabilityService(
        application.state.provider_settings_service,
        application.state.market_update_service,
        lambda: AlphaVantageProvider.from_env(
            usage_recorder=application.state.api_usage_service
        ),
        lambda: FinnhubProvider.from_env(
            usage_recorder=application.state.api_usage_service
        ),
    )
    application.state.scheduler_service = SchedulerService(
        session_factory, application.state.market_update_service
    )
    application.state.scheduler_service.ensure_all_defaults()
    application.router.add_event_handler("startup", application.state.scheduler_service.start)
    application.router.add_event_handler("shutdown", application.state.scheduler_service.shutdown)
    application.mount(
        "/static", StaticFiles(directory=WEB_DIR / "static"), name="static"
    )

    @application.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        stocks = request.app.state.stockwatch_service.list_stock_dashboards()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "stocks": stocks,
                "usage": request.app.state.api_usage_service.status(),
                "support": request.app.state.market_update_service.support_map(
                    [item.stock.id for item in stocks if item.stock is not None]
                ),
                "scheduler_enabled": request.app.state.scheduler_service.master_enabled(),
                "scheduler_next_run": request.app.state.scheduler_service.next_run(),
                "ai_status": request.app.state.ai_analysis_service.status(),
            },
        )

    @application.get("/opportunities", response_class=HTMLResponse)
    async def opportunities(request: Request) -> HTMLResponse:
        scanner = request.app.state.opportunity_scanner
        latest_search=request.app.state.opportunity_search.latest_run()
        from services.opportunity_candidates import OpportunityCandidateService
        from database.models import (OpportunityNewsEvent,OpportunitySearchRun,OpportunityWatchlist,
            ScannerAiNewsAssessment,ScannerRun)
        with session_factory() as session:
            search_run=session.scalar(select(OpportunitySearchRun).where(
                OpportunitySearchRun.status!="FAILED",OpportunitySearchRun.scanner_run_id.is_not(None))
                .order_by(OpportunitySearchRun.id.desc()).limit(1))
            latest=session.get(ScannerRun,search_run.scanner_run_id) if search_run else None
        if latest is None and latest_search and latest_search.status!="FAILED":latest=scanner.latest_run()
        technical_run = request.app.state.scanner_technical.latest_run(latest.id) if latest else None
        candidate_service=OpportunityCandidateService(session_factory)
        display_items=candidate_service.visible();favorites=candidate_service.favorites()
        favorite_ids={x.watch.catalog_id for x in favorites}
        ai_assessments={x.candidate.id:x.assessment for x in display_items if x.assessment}
        with session_factory() as session:
            watch_rows=list(session.scalars(select(OpportunityWatchlist).where(OpportunityWatchlist.active.is_(True))))
            assessment_ids=[row.id for row in ai_assessments.values()]
            news_rows=list(session.scalars(select(ScannerAiNewsAssessment).where(
                ScannerAiNewsAssessment.assessment_id.in_(assessment_ids)))) if assessment_ids else []
            event_rows=[x.news for x in display_items if x.news]
        ai_news: dict[int,list] = {}
        for row in news_rows: ai_news.setdefault(row.assessment_id,[]).append(row)
        events_by_catalog={row.catalog_id:row for row in sorted(event_rows,key=lambda x:x.event_score)}
        candidates=[x.candidate for x in display_items]
        current_run_id=search_run.scanner_run_id if search_run else None
        groups={"AKTUELLE CHANCEN":[],"WEITER BEOBACHTEN":[]}
        for display in display_items:
            if display.catalog.id in favorite_ids:continue
            label="AKTUELLE CHANCEN" if display.summary.latest_scanner_run_id==current_run_id else "WEITER BEOBACHTEN"
            groups[label].append(display.candidate)
        technicals={x.candidate.id:x.technical for x in display_items if x.technical}
        summaries={x.candidate.id:x.summary for x in display_items}
        external_links={x.candidate.id:x.external_url for x in display_items}
        search_progress=request.app.state.opportunity_search.progress()
        return templates.TemplateResponse(
            request=request, name="opportunities.html",
            context={
                "run": latest,
                "candidates": candidates,
                "candidate_groups": groups,
                "news_events": events_by_catalog,
                "request_limit": scanner.config.max_requests,
                "max_runtime_minutes": request.app.state.opportunity_search.config.max_runtime_minutes,
                "technical_run": technical_run,
                "technicals": technicals,
                "ai_assessments": ai_assessments,
                "ai_rating_labels": AI_RATING_LABELS,
                "watchlist": {row.catalog_id: row for row in watch_rows},
                "ai_news": ai_news,
                "search_run": search_run,
                "search_progress": search_progress,
                "candidate_summaries":summaries,
                "external_links":external_links,
                "favorites":favorites,
                "favorite_ids":favorite_ids,
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
        )

    @application.post("/opportunities/{catalog_id}/favorite")
    async def toggle_opportunity_favorite(catalog_id:int, favorite:str=Form(...)) -> RedirectResponse:
        from services.opportunity_candidates import OpportunityCandidateService
        try:
            enabled=favorite=="1"
            OpportunityCandidateService(session_factory).set_favorite(catalog_id,enabled)
            message="Zur Beobachtungsliste hinzugefügt." if enabled else "Dauerhafte Beobachtung beendet."
            return RedirectResponse(f"/opportunities?message={quote(message)}",status_code=303)
        except LookupError as exc:
            return RedirectResponse(f"/opportunities?error={quote(str(exc))}",status_code=303)

    @application.post("/opportunities/scan")
    async def scan_opportunities(request: Request) -> RedirectResponse:
        action_id=None;success=False
        try:
            action_id=request.app.state.action_guard.start("opportunity",timedelta(minutes=request.app.state.opportunity_search.config.max_runtime_minutes+2))
            run = request.app.state.opportunity_scanner.run()
            url = f"/opportunities?message={quote(f'Scan {run.status}: {run.candidates_shortlisted} Kandidaten.')}"
            success=True
        except (ScannerAlreadyRunning,ActionAlreadyRunning) as exc:
            url = f"/opportunities?error={quote(str(exc))}"
        finally:
            if action_id is not None:request.app.state.action_guard.finish(action_id,success)
        return RedirectResponse(url, status_code=303)

    @application.post("/opportunities/search")
    async def search_opportunities(request: Request) -> RedirectResponse:
        action_id=None;success=False
        try:
            action_id=request.app.state.action_guard.start("opportunity",timedelta(minutes=request.app.state.opportunity_search.config.max_runtime_minutes+2))
            result=request.app.state.opportunity_search.run_search()
            key="message" if result.run.status=="COMPLETED" or result.run.status.startswith("PARTIAL_") else "error"
            url=f"/opportunities?{key}={quote(result.run.message or result.run.status)}"
            success=result.run.status=="COMPLETED" or result.run.status.startswith("PARTIAL_")
        except (RuntimeError,ActionAlreadyRunning) as exc:
            url=f"/opportunities?error={quote(str(exc))}"
        finally:
            if action_id is not None:request.app.state.action_guard.finish(action_id,success)
        return RedirectResponse(url,status_code=303)

    @application.post("/opportunities/ai")
    async def assess_opportunities(request: Request) -> RedirectResponse:
        latest = request.app.state.opportunity_scanner.latest_run()
        technical = request.app.state.scanner_technical.latest_run(latest.id) if latest else None
        if technical is None:
            return RedirectResponse("/opportunities?error=Keine+technische+Verifikation+vorhanden.", status_code=303)
        action_id=None;success=False
        try:
            action_id=request.app.state.action_guard.start("opportunity",timedelta(minutes=request.app.state.opportunity_search.config.max_runtime_minutes+2))
            result = request.app.state.scanner_ai.run_latest(technical.id)
            text = f"KI-geprüft: {len(result.assessments)} · wiederverwendet: {result.reused}"
            url = f"/opportunities?message={quote(text)}"
            success=True
        except (AiBudgetExceeded, AiAnalysisError, LookupError,ActionAlreadyRunning) as exc:
            url = f"/opportunities?error={quote(str(exc))}"
        finally:
            if action_id is not None:request.app.state.action_guard.finish(action_id,success)
        return RedirectResponse(url, status_code=303)

    @application.post("/opportunities/technical")
    async def verify_opportunities(request: Request) -> RedirectResponse:
        latest = request.app.state.opportunity_scanner.latest_run()
        if latest is None:
            return RedirectResponse("/opportunities?error=Kein+Scanner-Lauf+vorhanden.", status_code=303)
        action_id=None;success=False
        try:
            action_id=request.app.state.action_guard.start("opportunity",timedelta(minutes=request.app.state.opportunity_search.config.max_runtime_minutes+2))
            run = request.app.state.scanner_technical.run(latest.id)
            text = f"Technisch geprüft: {run.candidates_checked} · Finnhub: {run.finnhub_requests} · Alpha Vantage: {run.alpha_vantage_requests}"
            url = f"/opportunities?message={quote(text)}"
            success=True
        except (TechnicalVerificationRunning,ActionAlreadyRunning) as exc:
            url = f"/opportunities?error={quote(str(exc))}"
        finally:
            if action_id is not None:request.app.state.action_guard.finish(action_id,success)
        return RedirectResponse(url, status_code=303)

    def render_stock_detail(
        request: Request,
        stock_id: int,
        *,
        error: str | None = None,
        form: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        data = request.app.state.stockwatch_service.get_stock_dashboard(stock_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Aktie nicht gefunden")
        settings_service = request.app.state.provider_settings_service
        return templates.TemplateResponse(
            request=request,
            name="stock_detail.html",
            context={
                "dashboard": data,
                "error": error,
                "form": form or {},
                "created": request.query_params.get("position") == "created",
                "updated": request.query_params.get("updated"),
                "update_error": request.query_params.get("update_error"),
                "usage": request.app.state.api_usage_service.status(),
                "support": request.app.state.market_update_service.support_for_stock(
                    stock_id
                ),
                "provider_settings": settings_service.get_settings(stock_id),
                "update_report": _decode_update_report(
                    request.query_params.get("update_report")
                ),
                "ai_analysis": request.app.state.ai_analysis_service.latest(stock_id),
                "ai_message": request.query_params.get("ai_message"),
                "ai_error": request.query_params.get("ai_error"),
                "ai_rating_labels": AI_RATING_LABELS,
                "manual_quote_enabled": data.stock.currency.upper() != "USD" and "NASDAQ" not in data.stock.exchange.upper(),
            },
            status_code=status_code,
        )

    @application.get("/stocks/{stock_id}", response_class=HTMLResponse)
    async def stock_detail(request: Request, stock_id: int) -> HTMLResponse:
        return render_stock_detail(request, stock_id)

    @application.get("/stocks/{stock_id}/providers", response_class=HTMLResponse)
    async def provider_settings(request: Request, stock_id: int) -> HTMLResponse:
        dashboard = request.app.state.stockwatch_service.get_stock_dashboard(stock_id)
        if dashboard is None or dashboard.stock is None:
            raise HTTPException(status_code=404, detail="Aktie nicht gefunden")
        settings_service = request.app.state.provider_settings_service
        capabilities = {
            (item.provider, item.data_type): item
            for item in settings_service.capabilities(stock_id)
        }
        alpha_symbol=request.app.state.provider_symbol_service.get(stock_id,"alpha_vantage")
        return templates.TemplateResponse(
            request=request,
            name="providers.html",
            context={
                "stock": dashboard.stock,
                "provider_settings": settings_service.get_settings(stock_id),
                "capabilities": capabilities,
                "provider_options": PROVIDERS,
                "capability_estimate": request.app.state.provider_capability_service.estimate_requests(stock_id),
                "alpha_symbol":alpha_symbol,
                "saved": request.query_params.get("saved") == "1",
                "checked": request.query_params.get("checked"),
                "error": request.query_params.get("error"),
            },
        )

    def save_position(
        request: Request,
        stock_id: int,
        purchase_price: str,
        quantity: str,
        purchase_date: str,
    ) -> HTMLResponse:
        form = {
            "purchase_price": purchase_price,
            "quantity": quantity,
            "purchase_date": purchase_date,
        }
        try:
            price = _positive_decimal(purchase_price, "Kaufpreis")
            amount = _positive_decimal(quantity, "Stückzahl")
            bought_on = date.fromisoformat(purchase_date)
            request.app.state.stockwatch_service.add_position(
                stock_id, price, amount, bought_on
            )
        except (InvalidOperation, ValueError, LookupError) as exc:
            message = (
                "Das Kaufdatum muss ein gültiges Datum sein."
                if isinstance(exc, ValueError) and "Invalid isoformat" in str(exc)
                else str(exc)
            )
            return render_stock_detail(
                request, stock_id, error=message, form=form, status_code=422
            )
        return RedirectResponse(url=f"/stocks/{stock_id}?position=created", status_code=303)

    @application.post("/stocks/{stock_id}/positions", response_class=HTMLResponse)
    async def create_stock_position(
        request: Request,
        stock_id: int,
        purchase_price: str = Form(""),
        quantity: str = Form(""),
        purchase_date: str = Form(""),
    ) -> HTMLResponse:
        return save_position(
            request, stock_id, purchase_price, quantity, purchase_date
        )

    @application.post("/stocks/{stock_id}/manual-price", response_class=HTMLResponse)
    async def save_manual_price(request:Request,stock_id:int,
                                manual_price:str=Form(""))->HTMLResponse:
        try:
            value=_positive_decimal(manual_price,"Kurs")
            request.app.state.stockwatch_service.save_manual_price(stock_id,value)
        except (InvalidOperation,ValueError,LookupError) as exc:
            return render_stock_detail(request,stock_id,error=str(exc),
                form={"manual_price":manual_price},status_code=422)
        return RedirectResponse(f"/stocks/{stock_id}?updated={quote('Manueller Kurs wurde gespeichert.')}",status_code=303)

    @application.post("/positions", response_class=HTMLResponse)
    async def create_legacy_airbus_position(
        request: Request,
        purchase_price: str = Form(""),
        quantity: str = Form(""),
        purchase_date: str = Form(""),
    ) -> HTMLResponse:
        airbus = request.app.state.stockwatch_service.get_airbus_dashboard()
        if airbus.stock is None:
            raise HTTPException(status_code=404, detail="AIR.PAR nicht gefunden")
        return save_position(
            request, airbus.stock.id, purchase_price, quantity, purchase_date
        )

    def run_update(request: Request, stock_id: int, kind: str) -> RedirectResponse:
        action_id=None;success=False
        try:
            action_id=request.app.state.action_guard.start(f"stock:{stock_id}:market")
            updater = request.app.state.market_update_service
            if kind == "alpha":
                updater.update_alpha_vantage(stock_id)
                message = "Alpha Vantage wurde aktualisiert."
            elif kind == "finnhub-quote":
                updater.update_finnhub_quote(stock_id)
                message = "Finnhub-Kurs wurde aktualisiert."
            elif kind == "finnhub-company":
                fetched, cached = updater.update_finnhub_company_data(stock_id)
                message = f"Finnhub-Unternehmensdaten: {fetched} geladen, {cached} aus Cache."
            elif kind == "quote":
                updater.update_quote(stock_id)
                provider = request.app.state.provider_settings_service.get_setting(stock_id, "quote").last_success_provider
                message = f"Kurs über {_provider_name(provider)} erfolgreich aktualisiert."
            elif kind == "history":
                setting = request.app.state.provider_settings_service.get_setting(stock_id, "history")
                updater.update_history(stock_id)
                provider = request.app.state.provider_settings_service.get_setting(stock_id, "history").last_success_provider
                prefix = f"{_provider_name(setting.primary_provider)} für Historie nicht verfügbar. " if provider != setting.primary_provider else ""
                message = f"{prefix}{_provider_name(provider)} wurde als Fallback verwendet; technische Analyse aktualisiert." if prefix else f"Historie über {_provider_name(provider)} erfolgreich aktualisiert."
            elif kind == "company":
                updater.update_fundamentals(stock_id)
                provider = request.app.state.provider_settings_service.get_setting(stock_id, "fundamentals").last_success_provider
                message = f"Unternehmensdaten über {_provider_name(provider)} aktualisiert."
            elif kind in ("news", "analyst", "earnings"):
                getattr(updater, f"update_{kind}")(stock_id)
                provider = request.app.state.provider_settings_service.get_setting(stock_id, kind).last_success_provider
                labels = {"news": "News", "analyst": "Analystendaten", "earnings": "Earnings"}
                message = f"{labels[kind]} über {_provider_name(provider)} aktualisiert."
            else:
                raise ValueError("Unbekannte Aktualisierungsart.")
            url = f"/stocks/{stock_id}?updated={quote(message)}"
            success=True
        except (ApiLimitExceeded, MarketUpdateError, LookupError, ValueError,ActionAlreadyRunning) as exc:
            url = f"/stocks/{stock_id}?update_error={quote(str(exc))}"
        finally:
            if action_id is not None:request.app.state.action_guard.finish(action_id,success)
        return RedirectResponse(url=url, status_code=303)

    @application.post("/stocks/{stock_id}/update/alpha")
    async def update_alpha(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "alpha")

    @application.post("/stocks/{stock_id}/update/finnhub/quote")
    async def update_finnhub_quote(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "finnhub-quote")

    @application.post("/stocks/{stock_id}/update/finnhub/company")
    async def update_finnhub_company(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "finnhub-company")

    @application.post("/stocks/{stock_id}/update/quote")
    async def update_quote(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "quote")

    @application.post("/stocks/{stock_id}/update/history")
    async def update_history(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "history")

    @application.post("/stocks/{stock_id}/update/company")
    async def update_company(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "company")

    @application.post("/stocks/{stock_id}/update/news")
    async def update_news(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "news")

    @application.post("/stocks/{stock_id}/update/analyst")
    async def update_analyst(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "analyst")

    @application.post("/stocks/{stock_id}/update/earnings")
    async def update_earnings(request: Request, stock_id: int) -> RedirectResponse:
        return run_update(request, stock_id, "earnings")

    @application.post("/stocks/{stock_id}/update")
    async def update_stock(request: Request, stock_id: int) -> RedirectResponse:
        action_id=None;success=False
        try:
            action_id=request.app.state.action_guard.start(f"stock:{stock_id}:market")
            report = request.app.state.stock_update_service.update_stock(stock_id)
            payload = {
                "completed_at": report.completed_at.isoformat(),
                "api_calls": report.api_calls,
                "items": [
                    {
                        "data_type": item.data_type,
                        "provider": item.provider,
                        "status": item.status,
                        "message": item.message,
                    }
                    for item in report.items
                ],
            }
            url = f"/stocks/{stock_id}?update_report={quote(json.dumps(payload, separators=(',', ':')))}"
            success=True
        except (LookupError,ActionAlreadyRunning) as exc:
            url = f"/stocks/{stock_id}?update_error={quote(str(exc))}"
        finally:
            if action_id is not None:request.app.state.action_guard.finish(action_id,success)
        return RedirectResponse(url, status_code=303)

    @application.post("/stocks/{stock_id}/ai/analyze")
    async def analyze_stock(request: Request, stock_id: int) -> RedirectResponse:
        action_id=None;success=False
        try:
            action_id=request.app.state.action_guard.start(f"stock:{stock_id}:ai")
            run = request.app.state.ai_analysis_service.analyze_stock(stock_id)
            message = (
                "Vorhandene aktuelle KI-Analyse wird weiterverwendet."
                if run.reused else "KI-Analyse erfolgreich erstellt."
            )
            url = f"/stocks/{stock_id}?ai_message={quote(message)}"
            success=True
        except (AiBudgetExceeded, AiAnalysisError, LookupError,ActionAlreadyRunning) as exc:
            url = f"/stocks/{stock_id}?ai_error={quote(str(exc))}"
        finally:
            if action_id is not None:request.app.state.action_guard.finish(action_id,success)
        return RedirectResponse(url, status_code=303)

    @application.post("/stocks/{stock_id}/providers")
    async def save_providers(request: Request, stock_id: int) -> RedirectResponse:
        try:
            form = await request.form()
            values = {}
            for data_type in DATA_TYPES:
                values[data_type] = (
                    str(form.get(f"primary_{data_type}", "none")),
                    str(form.get(f"fallback_{data_type}", "none")),
                    f"auto_{data_type}" in form,
                )
            request.app.state.provider_settings_service.update_settings(stock_id, values)
            request.app.state.stockwatch_service.set_external_quote_url(
                stock_id,str(form.get("external_quote_url", ""))
            )
            if "alpha_vantage_symbol" in form:
                request.app.state.provider_symbol_service.set_manual(
                    stock_id,str(form.get("alpha_vantage_symbol", "")))
            url = f"/stocks/{stock_id}/providers?saved=1"
        except (LookupError, ValueError) as exc:
            url = f"/stocks/{stock_id}/providers?error={quote(str(exc))}"
        return RedirectResponse(url=url, status_code=303)

    @application.post("/stocks/{stock_id}/providers/check")
    async def check_providers(request: Request, stock_id: int) -> RedirectResponse:
        action_id=None;success=False
        try:
            action_id=request.app.state.action_guard.start(f"stock:{stock_id}:capability")
            calls = request.app.state.provider_capability_service.check_stock(stock_id)
            url = f"/stocks/{stock_id}/providers?checked={quote(f'Datenquellen geprüft: {calls} API-Aufrufe.')}"
            success=True
        except (ApiLimitExceeded, LookupError, ValueError,ActionAlreadyRunning) as exc:
            url = f"/stocks/{stock_id}/providers?error={quote(str(exc))}"
        finally:
            if action_id is not None:request.app.state.action_guard.finish(action_id,success)
        return RedirectResponse(url=url, status_code=303)

    @application.get("/securities/search", response_class=HTMLResponse)
    async def search_securities(request: Request, q: str = "") -> HTMLResponse:
        results = request.app.state.search_service.search(q) if q.strip() else []
        return templates.TemplateResponse(
            request=request,
            name="search.html",
            context={
                "query": q,
                "results": results,
                "searched": bool(q.strip()),
                "selection": request.query_params.get("selection"),
                "symbol": request.query_params.get("symbol", ""),
                "error": request.query_params.get("error"),
            },
        )

    @application.post("/securities/{security_id}/select")
    async def select_security(request: Request, security_id: int) -> RedirectResponse:
        try:
            stock, created = request.app.state.stockwatch_service.select_catalog_security(
                security_id
            )
            request.app.state.provider_settings_service.ensure_defaults(stock.id)
            request.app.state.scheduler_service.ensure_defaults(stock.id)
            request.app.state.provider_symbol_service.resolve_alpha_vantage(stock.id)
            selection = "created" if created else "existing"
            url = (
                f"/securities/search?selection={selection}&symbol="
                f"{quote(stock.symbol)}"
            )
        except (LookupError, ValueError) as exc:
            url = f"/securities/search?error={quote(str(exc))}"
        return RedirectResponse(url=url, status_code=303)

    @application.get("/scheduler", response_class=HTMLResponse)
    async def scheduler_page(request: Request) -> HTMLResponse:
        service = request.app.state.scheduler_service
        return templates.TemplateResponse(
            request=request,
            name="scheduler.html",
            context={
                "enabled": service.master_enabled(),
                "schedules": service.views(),
                "runs": service.recent_runs(),
                "saved": request.query_params.get("saved"),
                "error": request.query_params.get("error"),
            },
        )

    @application.post("/scheduler/toggle")
    async def scheduler_toggle(request: Request) -> RedirectResponse:
        form = await request.form()
        request.app.state.scheduler_service.set_master_enabled("enabled" in form)
        return RedirectResponse("/scheduler?saved=Master-Schalter gespeichert.", status_code=303)

    @application.post("/scheduler/schedules/{schedule_id}")
    async def scheduler_save(request: Request, schedule_id: int) -> RedirectResponse:
        form = await request.form()
        try:
            request.app.state.scheduler_service.update_schedule(
                schedule_id,
                enabled="enabled" in form,
                cron_expression=str(form.get("cron_expression", "")),
                timezone_name=str(form.get("timezone", "")),
                weekdays_only="weekdays_only" in form,
            )
            url = "/scheduler?saved=Zeitplan gespeichert."
        except (LookupError, ValueError) as exc:
            url = f"/scheduler?error={quote(str(exc))}"
        return RedirectResponse(url, status_code=303)

    return application


def _positive_decimal(raw_value: str, label: str) -> Decimal:
    try:
        value = Decimal(raw_value.strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"{label} muss eine gültige Zahl sein.") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{label} muss größer als null sein.")
    return value


def _provider_name(provider: str | None) -> str:
    return {"finnhub": "Finnhub", "alpha_vantage": "Alpha Vantage", "yahoo": "Yahoo"}.get(provider, "dem konfigurierten Provider")


def _decode_update_report(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


app = create_app()
