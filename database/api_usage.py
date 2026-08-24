"""Zentrale lokale API-Nutzungsstatistik und Limitprüfung."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from market_data.usage import ApiLimitExceeded

from .models import ApiUsage


@dataclass(frozen=True, slots=True)
class ApiLimits:
    alpha_vantage_daily: int = 25
    finnhub_per_minute: int = 60

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "ApiLimits":
        values = _read_env_file(env_path)
        return cls(
            alpha_vantage_daily=_positive_int(
                os.getenv("ALPHAVANTAGE_DAILY_LIMIT")
                or values.get("ALPHAVANTAGE_DAILY_LIMIT"),
                25,
            ),
            finnhub_per_minute=_positive_int(
                os.getenv("FINNHUB_REQUESTS_PER_MINUTE")
                or values.get("FINNHUB_REQUESTS_PER_MINUTE"),
                60,
            ),
        )


@dataclass(frozen=True, slots=True)
class ApiUsageEvent:
    provider: str
    endpoint: str
    timestamp: datetime
    error_type: str | None


@dataclass(frozen=True, slots=True)
class ApiUsageStatus:
    alpha_today: int
    alpha_limit: int
    alpha_remaining: int
    finnhub_today: int
    finnhub_last_minute: int
    finnhub_minute_limit: int
    last_success: ApiUsageEvent | None
    last_error: ApiUsageEvent | None


class ApiUsageService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        limits: ApiLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.limits = limits or ApiLimits.from_env()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def before_request(self, provider: str, endpoint: str, symbol: str | None) -> None:
        now = self._clock()
        if provider == "alpha_vantage":
            used = self._count_since(provider, _start_of_day(now))
            if used >= self.limits.alpha_vantage_daily:
                raise ApiLimitExceeded(
                    f"Alpha-Vantage-Tageslimit von {self.limits.alpha_vantage_daily} Aufrufen erreicht."
                )
        elif provider == "finnhub":
            used = self._count_since(provider, now - timedelta(seconds=60))
            if used >= self.limits.finnhub_per_minute:
                raise ApiLimitExceeded(
                    f"Finnhub-Minutenlimit von {self.limits.finnhub_per_minute} Aufrufen erreicht."
                )

    def record_request(
        self,
        provider: str,
        endpoint: str,
        symbol: str | None,
        success: bool,
        http_status: int | None,
    ) -> None:
        data_type = _data_type_for_endpoint(endpoint)
        error_type = None
        if not success:
            error_type = "rate_limit" if http_status == 429 else (
                "http_error" if http_status and http_status >= 400 else "provider_error"
            )
        with self._session_factory.begin() as session:
            session.add(
                ApiUsage(
                    provider=provider,
                    endpoint=endpoint,
                    symbol=symbol,
                    data_type=data_type,
                    timestamp=self._clock(),
                    success=success,
                    http_status=http_status,
                    error_type=error_type,
                )
            )

    def status(self) -> ApiUsageStatus:
        now = self._clock()
        alpha = self._count_since("alpha_vantage", _start_of_day(now))
        finnhub_today = self._count_since("finnhub", _start_of_day(now))
        finnhub_minute = self._count_since("finnhub", now - timedelta(seconds=60))
        with self._session_factory() as session:
            last_success = session.scalar(
                select(ApiUsage).where(ApiUsage.success.is_(True)).order_by(ApiUsage.timestamp.desc()).limit(1)
            )
            last_error = session.scalar(
                select(ApiUsage).where(ApiUsage.success.is_(False)).order_by(ApiUsage.timestamp.desc()).limit(1)
            )
        return ApiUsageStatus(
            alpha_today=alpha,
            alpha_limit=self.limits.alpha_vantage_daily,
            alpha_remaining=max(self.limits.alpha_vantage_daily - alpha, 0),
            finnhub_today=finnhub_today,
            finnhub_last_minute=finnhub_minute,
            finnhub_minute_limit=self.limits.finnhub_per_minute,
            last_success=_event(last_success),
            last_error=_event(last_error),
        )

    def _count_since(self, provider: str, since: datetime) -> int:
        with self._session_factory() as session:
            count = session.scalar(
                select(func.count())
                .select_from(ApiUsage)
                .where(ApiUsage.provider == provider, ApiUsage.timestamp >= since)
            )
            return int(count or 0)


def _start_of_day(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _read_env_file(path: str | Path) -> dict[str, str]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _positive_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _data_type_for_endpoint(endpoint: str) -> str | None:
    return {
        "quote": "quote",
        "TIME_SERIES_DAILY": "history",
        "stock/candle": "history",
        "OVERVIEW": "fundamentals",
        "stock/profile2": "fundamentals",
        "stock/metric": "fundamentals",
        "company-news": "news",
        "stock/recommendation": "analyst",
        "stock/earnings": "earnings",
        "calendar/earnings": "earnings",
    }.get(endpoint)


def _event(item: ApiUsage | None) -> ApiUsageEvent | None:
    return None if item is None else ApiUsageEvent(
        item.provider, item.endpoint, item.timestamp, item.error_type
    )
