"""Manueller, budgetierter US-Opportunity-Scanner ohne OpenAI-Nutzung."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json, os
from pathlib import Path
from typing import Any, Callable
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from database.api_usage import ApiUsageService
from database.models import (AnalysisSnapshot, CompanyData, OpportunityWatchlist, ProviderCache, ScannerCandidate,
    ScannerProviderCapability, ScannerRun, ScannerSecurityState, SecurityCatalog, Stock)
from market_data.finnhub import EndpointResult, FinnhubProvider
from market_data.usage import ApiLimitExceeded

class ScannerAlreadyRunning(RuntimeError): pass
class ScannerBudgetReached(RuntimeError): pass

@dataclass(frozen=True, slots=True)
class ScannerConfig:
    max_requests: int = 200
    min_market_cap_usd: float = 2_000_000_000
    quality_threshold: float = 55
    max_quality_checks: int = 35
    max_shortlist: int = 15
    fundamentals_max_age: timedelta = timedelta(hours=24)
    quote_max_age: timedelta = timedelta(minutes=60)
    analyst_max_age: timedelta = timedelta(hours=24)
    earnings_max_age: timedelta = timedelta(hours=24)
    news_max_age: timedelta = timedelta(hours=4)
    @classmethod
    def from_env(cls, path: str | Path = ".env") -> "ScannerConfig":
        values = _env_values(path)
        return cls(max_requests=_positive_int(os.getenv("SCANNER_FINNHUB_MAX_REQUESTS") or values.get("SCANNER_FINNHUB_MAX_REQUESTS"), 200),
            min_market_cap_usd=_positive_float(os.getenv("SCANNER_MIN_MARKET_CAP_USD") or values.get("SCANNER_MIN_MARKET_CAP_USD"), 2e9),
            quality_threshold=_positive_float(os.getenv("SCANNER_QUALITY_THRESHOLD") or values.get("SCANNER_QUALITY_THRESHOLD"), 55))

@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    quality: float; fundamental: float; data_quality: float; excluded_reason: str | None

@dataclass(slots=True)
class CandidateWork:
    catalog: SecurityCatalog; metrics: dict[str, float | None]; quality: float; fundamental: float; data_quality: float
    price: float | None = None; momentum: float = 45; analyst: float = 50; earnings: float = 50; risk: float = 50
    opportunity: float = 0; candidate_type: str = "WATCH"; status: str = "CHECKED"; reason: str = ""
    technical_available: bool = False

class OpportunityScannerService:
    def __init__(self, session_factory: sessionmaker[Session], usage: ApiUsageService,
                 config: ScannerConfig | None = None, finnhub_factory: Callable[[], FinnhubProvider] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._sessions, self._usage = session_factory, usage
        self.config = config or ScannerConfig.from_env()
        self._finnhub_factory = finnhub_factory or (lambda: FinnhubProvider.from_env(usage_recorder=usage))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._requests = self._cache_hits = self._premium_skips = 0

    def catalog_counts(self) -> tuple[int, int]:
        with self._sessions() as s:
            total = s.scalar(select(func.count()).select_from(SecurityCatalog).where(SecurityCatalog.source == "NASDAQ")) or 0
            filtered = s.scalar(select(func.count()).select_from(SecurityCatalog).where(*_local_filter())) or 0
        return int(total), int(filtered)

    def rotation(self, limit: int | None = None, prioritized_catalog_ids: list[int] | None = None) -> list[SecurityCatalog]:
        priorities={catalog_id:index for index,catalog_id in enumerate(prioritized_catalog_ids or [])}
        with self._sessions() as s:
            modulus=int(s.scalar(select(func.max(SecurityCatalog.id))) or 1)+1
            spread_order=(SecurityCatalog.id*7919) % modulus
            rows=list(s.scalars(select(SecurityCatalog).outerjoin(ScannerSecurityState,
                ScannerSecurityState.catalog_id == SecurityCatalog.id).outerjoin(OpportunityWatchlist,
                OpportunityWatchlist.catalog_id == SecurityCatalog.id).where(*_local_filter()).order_by(
                case((OpportunityWatchlist.active.is_(True), 0), else_=1),
                OpportunityWatchlist.last_checked_at.asc(),
                case((ScannerSecurityState.last_checked_at.is_(None), 0), else_=1),
                case((ScannerSecurityState.last_checked_at.is_(None), spread_order), else_=0),
                ScannerSecurityState.last_checked_at.asc(), SecurityCatalog.id.asc())))
            rows.sort(key=lambda row:(0,priorities[row.id]) if row.id in priorities else (1,0))
            return rows[:limit or self.config.max_quality_checks]

    def latest_run(self) -> ScannerRun | None:
        with self._sessions() as s:
            return s.scalar(select(ScannerRun).order_by(ScannerRun.started_at.desc(), ScannerRun.id.desc()).limit(1))

    def candidates(self, run_id: int) -> list[ScannerCandidate]:
        with self._sessions() as s:
            return list(s.scalars(select(ScannerCandidate).where(ScannerCandidate.scanner_run_id == run_id,
                ScannerCandidate.status == "SHORTLISTED").order_by(ScannerCandidate.opportunity_score.desc())))

    def run(self, prioritized_catalog_ids: list[int] | None = None,
            deadline: datetime | None = None, request_gate: Callable[[],None] | None = None,
            progress: Callable[[str,int,str|None],None] | None = None,
            enforce_request_budget: bool = True,strong_event_catalog_ids:set[int]|None=None,
            max_candidates:int|None=None) -> ScannerRun:
        total, filtered = self.catalog_counts(); run = self._start_run(total, filtered)
        self._requests = self._cache_hits = self._premium_skips = 0
        works: list[CandidateWork] = []; excluded = 0; status = "COMPLETED"
        message = "Scan abgeschlossen. Die Ergebnisse sind keine Kaufempfehlung."
        provider = self._finnhub_factory()
        try:
            for catalog in self.rotation(limit=max_candidates,prioritized_catalog_ids=prioritized_catalog_ids):
                if deadline is not None and self._clock()>=deadline:
                    raise ScannerBudgetReached("Maximale Laufzeit erreicht.")
                if progress:progress("ROTATION",len(works),catalog.symbol)
                self._request_gate=request_gate;self._enforce_request_budget=enforce_request_budget
                metrics = self._fundamentals(provider, catalog); score = quality_score(metrics, self.config)
                work = CandidateWork(catalog, metrics, score.quality, score.fundamental, score.data_quality)
                strong_event=catalog.id in (strong_event_catalog_ids or set())
                if score.excluded_reason or (work.quality < self.config.quality_threshold and not strong_event):
                    work.status, excluded = "EXCLUDED", excluded + 1
                    work.reason = score.excluded_reason or f"Quality Score {work.quality:.0f} unter Mindestwert {self.config.quality_threshold:.0f}."
                works.append(work); self._mark_checked(catalog.id)
            qualified = sorted((w for w in works if w.status == "CHECKED"), key=lambda w: (-w.quality, w.catalog.symbol))[:self.config.max_shortlist]
            for work in qualified:
                self._enrich(provider, work)
                work.opportunity = opportunity_score(work.quality, work.momentum, work.fundamental, work.analyst, work.earnings)
                work.candidate_type, work.reason = self._classify(work); work.status = "SHORTLISTED"
                if work.catalog.id in (strong_event_catalog_ids or set()):
                    work.candidate_type="NEWS_CHANCE";work.reason="Starkes, eindeutig zugeordnetes News-Ereignis mit ausreichender Fundamentaldatenbasis."
        except ScannerBudgetReached as exc:
            status = "PARTIAL_RUNTIME_LIMIT" if "Laufzeit" in str(exc) else "PARTIAL_RATE_LIMIT"
            message = str(exc)
        except ApiLimitExceeded:
            status,message="PARTIAL_RATE_LIMIT","Finnhub-Limit erreicht."
        except Exception:
            status, message = "FAILED", "Der Scanner wurde wegen eines bereinigten Providerfehlers beendet."
        return self._finish(run.id, works, status, message, excluded)

    def load_final_news(self, candidates: list[ScannerCandidate],request_gate=None,deadline:datetime|None=None) -> tuple[int, int]:
        """Lädt News spät im Trichter über denselben budgetierten Finnhub-/Cache-Pfad."""
        before_requests, before_cache = self._requests, self._cache_hits
        provider = self._finnhub_factory()
        for candidate in candidates:
            if deadline is not None and self._clock()>=deadline:break
            with self._sessions() as s: catalog = s.get(SecurityCatalog, candidate.catalog_id)
            if catalog is None: continue
            self._request_gate=request_gate;self._enforce_request_budget=False
            self._cached_request(provider, catalog, "scanner_news", self.config.news_max_age,
                                 "company-news", **{"from": (self._clock()-timedelta(days=7)).date().isoformat(),
                                                    "to": self._clock().date().isoformat()})
        return self._requests-before_requests, self._cache_hits-before_cache

    def _start_run(self, total: int, filtered: int) -> ScannerRun:
        with self._sessions.begin() as s:
            if s.scalar(select(ScannerRun).where(ScannerRun.status == "RUNNING").limit(1)):
                raise ScannerAlreadyRunning("Es läuft bereits ein Opportunity-Scan.")
            run = ScannerRun(status="RUNNING", market="NASDAQ", catalog_total=total, candidates_total=filtered)
            s.add(run); s.flush(); return run

    def _finish(self, run_id: int, works: list[CandidateWork], status: str, message: str, excluded: int) -> ScannerRun:
        with self._sessions.begin() as s:
            run = s.get(ScannerRun, run_id); assert run
            for w in works:
                v = w.metrics
                s.add(ScannerCandidate(scanner_run_id=run.id, catalog_id=w.catalog.id, symbol=w.catalog.symbol, name=w.catalog.name,
                    market_cap=v.get("market_cap"), price=w.price, pe_ratio=v.get("pe_ratio"), eps=v.get("eps"),
                    revenue_growth=v.get("revenue_growth"), profit_growth=v.get("profit_growth"), roe=v.get("roe"),
                    debt_to_equity=v.get("debt_to_equity"), week_52_high=v.get("week_52_high"), week_52_low=v.get("week_52_low"),
                    quality_score=w.quality, opportunity_score=w.opportunity, momentum_score=w.momentum,
                    fundamental_score=w.fundamental, analyst_score=w.analyst, risk_score=w.risk,
                    candidate_type=w.candidate_type, status=w.status, reason=w.reason))
            run.finished_at, run.status, run.message = self._clock(), status, message
            run.candidates_checked, run.candidates_shortlisted = len(works), sum(w.status == "SHORTLISTED" for w in works)
            run.finnhub_requests, run.cache_hits, run.premium_skips, run.quality_excluded = self._requests, self._cache_hits, self._premium_skips, excluded
            return run

    def _fundamentals(self, provider: FinnhubProvider, catalog: SecurityCatalog) -> dict[str, float | None]:
        existing = self._existing_company(catalog.symbol)
        if existing is not None: self._cache_hits += 1; return existing
        result = self._cached_request(provider, catalog, "scanner_fundamentals", self.config.fundamentals_max_age, "stock/metric", metric="all")
        data = result.data if isinstance(result.data, dict) else {}; metric = data.get("metric", data)
        return _extract_metrics(metric if isinstance(metric, dict) else {})

    def _enrich(self, provider: FinnhubProvider, work: CandidateWork) -> None:
        quote = self._cached_request(provider, work.catalog, "scanner_quote", self.config.quote_max_age, "quote")
        if isinstance(quote.data, dict): work.price = _number(quote.data.get("c"))
        technical = self._technical(work.catalog.symbol)
        work.technical_available = technical is not None and technical.sma20 is not None
        work.momentum, work.risk = momentum_score(technical)
        if not getattr(self,"_enforce_request_budget",True) or self._requests < self.config.max_requests:
            work.analyst = analyst_score(self._cached_request(provider, work.catalog, "scanner_analyst", self.config.analyst_max_age, "stock/recommendation").data)
        if not getattr(self,"_enforce_request_budget",True) or self._requests < self.config.max_requests:
            work.earnings = earnings_score(self._cached_request(provider, work.catalog, "scanner_earnings", self.config.earnings_max_age, "stock/earnings", limit=4).data)

    def _cached_request(self, provider: FinnhubProvider, catalog: SecurityCatalog, cache_type: str,
                        max_age: timedelta, endpoint: str, **params: str | int) -> EndpointResult:
        if self._capability(catalog.id, endpoint) == "premium_required":
            self._premium_skips += 1; return EndpointResult(endpoint, "Premium-Zugang erforderlich")
        symbol = catalog.provider_symbol_finnhub or catalog.symbol
        cached = self._cache(symbol, cache_type, max_age)
        if cached: self._cache_hits += 1; return cached
        if getattr(self,"_enforce_request_budget",True) and self._requests >= self.config.max_requests: raise ScannerBudgetReached(f"Scanner-Request-Budget von {self.config.max_requests} erreicht; Teilergebnisse wurden gespeichert.")
        gate=getattr(self,"_request_gate",None)
        if gate:gate()
        result = provider.request(endpoint, endpoint, symbol=symbol, **params); self._requests += 1
        if result.availability == "Premium-Zugang erforderlich": self._save_capability(catalog.id, endpoint, "premium_required"); self._premium_skips += 1
        if result.availability == "API-Fehler" and result.message and "429" in result.message: raise ApiLimitExceeded("Finnhub-Limit erreicht; Teilergebnisse wurden gespeichert.")
        self._save_cache(symbol, cache_type, result); return result

    def _existing_company(self, symbol: str) -> dict[str, float | None] | None:
        cutoff = self._clock() - self.config.fundamentals_max_age
        with self._sessions() as s:
            r = s.scalar(select(CompanyData).join(Stock, Stock.id == CompanyData.stock_id).where(Stock.symbol == symbol,
                CompanyData.provider == "finnhub", CompanyData.updated_at >= cutoff).limit(1))
            if not r: return None
            return {"market_cap": _market_cap_usd(r.market_cap), "pe_ratio": r.pe_ratio, "eps": r.eps,
                "revenue_growth": r.revenue_growth, "profit_growth": None, "roe": r.roe,
                "debt_to_equity": r.debt_to_equity, "week_52_high": None, "week_52_low": None}

    def _technical(self, symbol: str) -> AnalysisSnapshot | None:
        with self._sessions() as s:
            return s.scalar(select(AnalysisSnapshot).join(Stock, Stock.id == AnalysisSnapshot.stock_id).where(Stock.symbol == symbol).order_by(AnalysisSnapshot.timestamp.desc()).limit(1))

    def _cache(self, symbol: str, data_type: str, age: timedelta) -> EndpointResult | None:
        with self._sessions() as s:
            r = s.scalar(select(ProviderCache).where(ProviderCache.provider == "finnhub", ProviderCache.symbol == symbol,
                ProviderCache.data_type == data_type, ProviderCache.fetched_at >= self._clock() - age))
            if not r: return None
            p = json.loads(r.payload); return EndpointResult(data_type, p.get("availability", "leer"), p.get("data"))

    def _save_cache(self, symbol: str, data_type: str, result: EndpointResult) -> None:
        with self._sessions.begin() as s:
            r = s.scalar(select(ProviderCache).where(ProviderCache.provider == "finnhub", ProviderCache.symbol == symbol, ProviderCache.data_type == data_type))
            payload = json.dumps({"availability": result.availability, "data": result.data}, default=str)
            if r: r.fetched_at, r.payload = self._clock(), payload
            else: s.add(ProviderCache(provider="finnhub", symbol=symbol, data_type=data_type, fetched_at=self._clock(), payload=payload))

    def _capability(self, catalog_id: int, endpoint: str) -> str | None:
        with self._sessions() as s: return s.scalar(select(ScannerProviderCapability.status).where(ScannerProviderCapability.catalog_id == catalog_id, ScannerProviderCapability.provider == "finnhub", ScannerProviderCapability.endpoint == endpoint))
    def _save_capability(self, catalog_id: int, endpoint: str, status: str) -> None:
        with self._sessions.begin() as s:
            r = s.scalar(select(ScannerProviderCapability).where(ScannerProviderCapability.catalog_id == catalog_id, ScannerProviderCapability.provider == "finnhub", ScannerProviderCapability.endpoint == endpoint))
            if r: r.status, r.last_checked_at = status, self._clock()
            else: s.add(ScannerProviderCapability(catalog_id=catalog_id, provider="finnhub", endpoint=endpoint, status=status, last_checked_at=self._clock()))
    def _mark_checked(self, catalog_id: int) -> None:
        with self._sessions.begin() as s:
            r = s.scalar(select(ScannerSecurityState).where(ScannerSecurityState.catalog_id == catalog_id))
            if r: r.last_checked_at = self._clock()
            else: s.add(ScannerSecurityState(catalog_id=catalog_id, last_checked_at=self._clock()))
    @staticmethod
    def _classify(w: CandidateWork) -> tuple[str, str]:
        if w.technical_available and w.quality >= 70 and w.momentum <= 50: return "QUALITY_PULLBACK", "Hohe Unternehmensqualität bei einem lokal bestätigten kurzfristigen Rücksetzer."
        if w.momentum >= 65: return "MOMENTUM", "Solide Qualität mit positivem, nicht extremem Momentum."
        if w.analyst >= 70: return "ANALYST_SUPPORT", "Solide Qualität mit breiter Analystenunterstützung."
        if w.earnings >= 70: return "EARNINGS_STRENGTH", "Solide Qualität und zuletzt überwiegend positive Earnings-Überraschungen."
        return "WATCH", "Solides Unternehmen; für den 1–3-Monats-Horizont fehlen noch lokal bestätigte technische Signale."

def quality_score(v: dict[str, float | None], config: ScannerConfig | None = None) -> ScoreBreakdown:
    c = config or ScannerConfig(); cap, eps, debt = v.get("market_cap"), v.get("eps"), v.get("debt_to_equity")
    available = sum(v.get(k) is not None for k in ("market_cap","pe_ratio","eps","revenue_growth","profit_growth","roe","debt_to_equity","week_52_high","week_52_low")); dq = min(10., available / 9 * 10)
    if cap is None: return ScoreBreakdown(dq, 0, dq, "Marktkapitalisierung fehlt; Datenqualität unzureichend.")
    if cap < c.min_market_cap_usd: return ScoreBreakdown(dq, 0, dq, "Marktkapitalisierung unter 2 Milliarden USD.")
    if eps is not None and eps < 0: return ScoreBreakdown(dq, 0, dq, "Negatives EPS.")
    if debt is not None and debt > 3: return ScoreBreakdown(dq, 0, dq, "Sehr hohe Verschuldung.")
    size = 20 if cap >= 50e9 else 16 if cap >= 10e9 else 12; roe = v.get("roe")
    profit = 0 if roe is None else 20 if roe >= 20 else 15 if roe >= 10 else 8 if roe >= 0 else 0
    growth = _growth(v.get("revenue_growth")) + _growth(v.get("profit_growth")); leverage = 5 if debt is None else 15 if debt <= .5 else 10 if debt <= 1 else 5 if debt <= 2 else 0
    pe = v.get("pe_ratio"); valuation = 5 if pe is None else 15 if 10 <= pe <= 30 else 10 if 0 < pe <= 45 else 3
    fundamental = min(100., (size + profit + growth + leverage + valuation) / 90 * 100)
    return ScoreBreakdown(round(size + profit + growth + leverage + valuation + dq, 2), round(fundamental, 2), round(dq, 2), None)

def opportunity_score(q: float, m: float, f: float, a: float, e: float) -> float: return round(max(0., min(100., q*.35+m*.30+f*.15+a*.10+e*.10)), 2)
def momentum_score(s: AnalysisSnapshot | None) -> tuple[float,float]:
    if s is None or s.sma20 is None: return 45,50
    score=50.+(15 if s.trend=="POSITIV" else -15 if s.trend=="NEGATIV" else 0)
    if s.performance_20d is not None: score += max(-15,min(15,s.performance_20d))
    if s.rsi14 is not None: score += -15 if s.rsi14>75 else 8 if 40<=s.rsi14<=65 else 0
    return round(max(0,min(100,score)),2), 70 if s.volatility_20d and s.volatility_20d>50 else 40
def analyst_score(data: Any) -> float:
    rows=data if isinstance(data,list) else []
    if not rows or not isinstance(rows[0],dict): return 50
    r=rows[0]; pos=(_number(r.get("strongBuy")) or 0)+(_number(r.get("buy")) or 0); total=pos+sum((_number(r.get(k)) or 0) for k in ("hold","sell","strongSell"))
    return round(pos/total*100,2) if total else 50
def earnings_score(data: Any) -> float:
    rows=data if isinstance(data,list) else []; vals=[x for x in (_number(r.get("surprisePercent")) for r in rows if isinstance(r,dict)) if x is not None]
    return 50 if not vals else round(max(0,min(100,50+sum(vals)/len(vals)*2)),2)
def _extract_metrics(m: dict[str,Any]) -> dict[str,float|None]:
    return {"market_cap":_market_cap_usd(_first(m,"marketCapitalization","marketCap")),"pe_ratio":_number(_first(m,"peTTM","peNormalizedAnnual")),"eps":_number(_first(m,"epsTTM","epsAnnual")),"revenue_growth":_number(_first(m,"revenueGrowthTTMYoy","revenueGrowth5Y")),"profit_growth":_number(_first(m,"netIncomeGrowthTTMYoy","epsGrowthTTMYoy")),"roe":_number(_first(m,"roeTTM","roeRfy")),"debt_to_equity":_number(_first(m,"totalDebt/totalEquityQuarterly","totalDebt/totalEquityAnnual")),"week_52_high":_number(_first(m,"52WeekHigh")),"week_52_low":_number(_first(m,"52WeekLow"))}
def _local_filter() -> tuple[Any,...]: return (SecurityCatalog.source=="NASDAQ",SecurityCatalog.security_type=="stock",SecurityCatalog.provider_symbol_finnhub.is_not(None),SecurityCatalog.exchange=="NASDAQ",SecurityCatalog.country=="US",SecurityCatalog.currency=="USD")
def _growth(v: float|None)->float: return 0 if v is None else 10 if v>=10 else 7 if v>=3 else 4 if v>=0 else 0
def _first(v:dict[str,Any],*keys:str)->Any: return next((v[k] for k in keys if v.get(k) not in (None,"")),None)
def _number(v:Any)->float|None:
    try:return None if v in (None,"","-") else float(v)
    except (TypeError,ValueError):return None
def _market_cap_usd(v:Any)->float|None:
    n=_number(v); return None if n is None else n*1_000_000 if n<100_000_000 else n
def _env_values(path:str|Path)->dict[str,str]:
    try:lines=Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:return {}
    return {k.strip():v.strip().strip('"').strip("'") for line in lines if line.strip() and not line.lstrip().startswith("#") and "=" in line for k,v in [line.split("=",1)]}
def _positive_int(v:str|None,d:int)->int:
    try:n=int(v) if v else d
    except ValueError:return d
    return n if n>0 else d
def _positive_float(v:str|None,d:float)->float:
    try:n=float(v) if v else d
    except ValueError:return d
    return n if n>0 else d
