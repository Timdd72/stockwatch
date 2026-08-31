"""Sparsame, persistente Auflösung provider-spezifischer Aktiensymbole."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime,timezone
import re
from typing import Callable

from sqlalchemy import func,select
from sqlalchemy.orm import Session,sessionmaker

from database.models import SecurityCatalog,Stock,StockProviderSymbol
from market_data.alpha_vantage import AlphaVantageProvider,AlphaVantageRateLimitError
from market_data.usage import ApiLimitExceeded
from .provider_settings import ProviderSettingsService


@dataclass(frozen=True,slots=True)
class SymbolResolution:
    symbol:str|None
    status:str
    source:str
    api_calls:int


class ProviderSymbolService:
    def __init__(self,sessions:sessionmaker[Session],alpha_factory:Callable[[],AlphaVantageProvider]):
        self._sessions=sessions;self._alpha_factory=alpha_factory
        self._settings=ProviderSettingsService(sessions)

    def get(self,stock_id:int,provider:str)->StockProviderSymbol|None:
        with self._sessions() as s:
            return s.scalar(select(StockProviderSymbol).where(
                StockProviderSymbol.stock_id==stock_id,StockProviderSymbol.provider==provider))

    def resolve_alpha_vantage(self,stock_id:int)->SymbolResolution:
        with self._sessions() as s:
            stock=s.get(Stock,stock_id)
            if stock is None:raise LookupError("Die Aktie wurde nicht gefunden.")
            existing=s.scalar(select(StockProviderSymbol).where(
                StockProviderSymbol.stock_id==stock_id,StockProviderSymbol.provider=="alpha_vantage"))
            if existing:return SymbolResolution(existing.symbol,existing.status,existing.source,0)
            catalog=s.scalar(select(SecurityCatalog).where(
                func.upper(SecurityCatalog.symbol)==stock.symbol.upper(),
                func.upper(func.coalesce(SecurityCatalog.exchange,""))==stock.exchange.upper()).limit(1))
            if catalog is None:
                catalog=s.scalar(select(SecurityCatalog).where(
                    func.upper(SecurityCatalog.symbol)==stock.symbol.upper()).limit(1))
            if catalog and catalog.provider_symbol_alpha_vantage:
                return self._save(stock_id,catalog.provider_symbol_alpha_vantage,"available","catalog",0,False)
            # Reguläre NASDAQ-Symbole stammen aus dem offiziellen Katalog und sind direkt nutzbar.
            if catalog and stock.exchange.upper()=="NASDAQ" and re.fullmatch(r"[A-Z][A-Z0-9]{0,5}",stock.symbol.upper()):
                return self._save(stock_id,stock.symbol.upper(),"available","catalog",0,False)
            local_symbol,name,currency,exchange=stock.symbol,stock.name,stock.currency,stock.exchange
        calls=0
        try:provider=self._alpha_factory()
        except Exception:return self._save(stock_id,None,"error","automatic",0,False)
        if exchange.upper() in ("XETR","XETRA"):
            direct=f"{local_symbol.upper()}.DEX"
            calls+=1
            try:
                history=provider.get_daily(direct)
                if history is not None and not history.empty:
                    return self._save(stock_id,direct,"available","verified",calls,True)
            except (AlphaVantageRateLimitError,ApiLimitExceeded):
                return self._save(stock_id,None,"error","automatic",calls,False)
            except Exception:
                pass
        try:
            calls+=1;matches=provider.search_symbol(local_symbol)
        except Exception:
            return self._save(stock_id,None,"unavailable","automatic",calls,False)
        plausible=[m for m in matches if self._plausible(m,name,currency,exchange)]
        symbols={m.get("1. symbol","").strip().upper() for m in plausible if m.get("1. symbol")}
        if len(symbols)==1:
            return self._save(stock_id,next(iter(symbols)),"available","search",calls,True)
        return self._save(stock_id,None,"unavailable","automatic",calls,False)

    def set_manual(self,stock_id:int,symbol:str|None)->SymbolResolution:
        value=(symbol or "").strip().upper() or None
        if value and not re.fullmatch(r"[A-Z0-9.^_-]{1,32}",value):
            raise ValueError("Ungültiges Alpha-Vantage-Symbol.")
        return self._save(stock_id,value,"available" if value else "unavailable","manual",0,False)

    def _save(self,stock_id:int,symbol:str|None,status:str,source:str,calls:int,verified:bool)->SymbolResolution:
        now=datetime.now(timezone.utc)
        invalidate=False
        with self._sessions.begin() as s:
            row=s.scalar(select(StockProviderSymbol).where(
                StockProviderSymbol.stock_id==stock_id,StockProviderSymbol.provider=="alpha_vantage"))
            if row is None:
                invalidate=symbol is not None
                row=StockProviderSymbol(stock_id=stock_id,provider="alpha_vantage",symbol=symbol,
                    status=status,source=source,verified_at=now if verified else None);s.add(row)
            else:
                invalidate=row.symbol!=symbol or row.status!=status
                row.symbol=symbol;row.status=status;row.source=source
                row.verified_at=now if verified else row.verified_at;row.updated_at=now
        if invalidate:self._settings.invalidate_capabilities(stock_id,"alpha_vantage")
        return SymbolResolution(symbol,status,source,calls)

    @staticmethod
    def _plausible(match:dict[str,str],name:str,currency:str,exchange:str)->bool:
        if match.get("8. currency","").upper()!=currency.upper():return False
        region=match.get("4. region","").lower()
        if exchange.upper() in ("XETR","XETRA") and not any(x in region for x in ("germany","frankfurt","xetra")):
            return False
        score=float(match.get("9. matchScore","0") or 0)
        wanted={x for x in re.findall(r"[a-z0-9]+",name.lower()) if x not in {"ag","se","inc","group","corporation"}}
        found=set(re.findall(r"[a-z0-9]+",match.get("2. name","").lower()))
        return score>=0.75 and bool(wanted) and len(wanted&found)/len(wanted)>=0.5
