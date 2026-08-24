"""Persistierter APScheduler für automatische StockWatch-Aktualisierungen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from database.models import SchedulerRun, SchedulerSetting, Stock, UpdateSchedule
from market_data.usage import ApiLimitExceeded

from .market_updates import MarketUpdateError, MarketUpdateService
from .provider_settings import ProviderSettingsService


@dataclass(frozen=True, slots=True)
class ScheduleView:
    schedule: UpdateSchedule
    stock: Stock
    last_run: SchedulerRun | None
    next_run: datetime | None


class SchedulerService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        updates: MarketUpdateService,
        scheduler: BackgroundScheduler | None = None,
    ) -> None:
        self._sessions = session_factory
        self._updates = updates
        self._providers = ProviderSettingsService(session_factory)
        self._scheduler = scheduler or BackgroundScheduler(timezone=timezone.utc)

    def ensure_defaults(self, stock_id: int) -> list[UpdateSchedule]:
        with self._sessions.begin() as session:
            stock = session.get(Stock, stock_id)
            if stock is None:
                raise LookupError("Die Aktie wurde nicht gefunden.")
            existing = {x.data_type: x for x in session.scalars(
                select(UpdateSchedule).where(UpdateSchedule.stock_id == stock_id)
            )}
            for data_type, cron, zone in self._default_values(stock):
                if data_type not in existing:
                    item = UpdateSchedule(stock_id=stock_id, data_type=data_type,
                        enabled=True, schedule_type="cron", cron_expression=cron,
                        timezone=zone, weekdays_only=True)
                    session.add(item); existing[data_type] = item
            session.flush()
            return list(existing.values())

    def ensure_all_defaults(self) -> None:
        with self._sessions() as session:
            ids = list(session.scalars(select(Stock.id)))
        for stock_id in ids:
            self.ensure_defaults(stock_id)
        self.master_enabled()

    def master_enabled(self) -> bool:
        with self._sessions.begin() as session:
            setting = session.scalar(select(SchedulerSetting).limit(1))
            if setting is None:
                setting = SchedulerSetting(scheduler_enabled=False)
                session.add(setting); session.flush()
            return setting.scheduler_enabled

    def set_master_enabled(self, enabled: bool) -> None:
        with self._sessions.begin() as session:
            setting = session.scalar(select(SchedulerSetting).limit(1))
            if setting is None:
                session.add(SchedulerSetting(scheduler_enabled=enabled))
            else:
                setting.scheduler_enabled = enabled
        self.reconstruct_jobs()

    def update_schedule(self, schedule_id: int, *, enabled: bool,
                        cron_expression: str, timezone_name: str,
                        weekdays_only: bool) -> None:
        self._trigger(cron_expression, timezone_name)
        with self._sessions.begin() as session:
            item = session.get(UpdateSchedule, schedule_id)
            if item is None:
                raise LookupError("Zeitplan wurde nicht gefunden.")
            item.enabled = enabled
            item.cron_expression = cron_expression
            item.timezone = timezone_name
            item.weekdays_only = weekdays_only
        self.reconstruct_jobs()

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        self.reconstruct_jobs()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def reconstruct_jobs(self) -> None:
        self._scheduler.remove_all_jobs()
        if not self.master_enabled():
            return
        with self._sessions() as session:
            schedules = list(session.scalars(select(UpdateSchedule).where(UpdateSchedule.enabled.is_(True))))
        for item in schedules:
            self._scheduler.add_job(
                self.run_schedule, self._trigger(item.cron_expression, item.timezone),
                args=[item.id], id=f"stockwatch-{item.id}", replace_existing=True,
                coalesce=True, misfire_grace_time=60, max_instances=1,
            )

    def run_schedule(self, schedule_id: int, planned_at: datetime | None = None) -> str:
        now = datetime.now(timezone.utc)
        with self._sessions() as session:
            item = session.get(UpdateSchedule, schedule_id)
            if item is None:
                return "skipped_disabled"
            stock_id, data_type = item.stock_id, item.data_type
            enabled, weekdays_only, zone = item.enabled, item.weekdays_only, item.timezone
        status, message, provider = "success", None, None
        started = datetime.now(timezone.utc)
        if not enabled or not self.master_enabled():
            status, message = "skipped_disabled", "Automatische Aktualisierung ist deaktiviert."
        elif weekdays_only and (planned_at or started).astimezone(ZoneInfo(zone)).weekday() >= 5:
            status, message = "skipped_market_closed", "Wochenende: Markt geschlossen."
        else:
            try:
                operation = {
                    "quote": self._updates.update_quote,
                    "history": self._updates.update_history,
                    "fundamentals": self._updates.update_fundamentals,
                    "news": self._updates.update_news,
                    "analyst": self._updates.update_analyst,
                    "earnings": self._updates.update_earnings,
                }[data_type]
                operation(stock_id)
                provider = self._providers.get_setting(stock_id, data_type).last_success_provider
                message = "Automatische Aktualisierung erfolgreich."
            except ApiLimitExceeded as exc:
                status, message = "skipped_limit", str(exc)
            except (MarketUpdateError, LookupError, ValueError, KeyError) as exc:
                status, message = "failed", str(exc)
        with self._sessions.begin() as session:
            session.add(SchedulerRun(schedule_id=schedule_id, stock_id=stock_id,
                data_type=data_type, planned_at=planned_at or now, started_at=started,
                finished_at=datetime.now(timezone.utc), status=status,
                provider_used=provider, message=message))
        return status

    def views(self) -> list[ScheduleView]:
        now = datetime.now(timezone.utc)
        with self._sessions() as session:
            schedules = list(session.scalars(select(UpdateSchedule).order_by(UpdateSchedule.stock_id, UpdateSchedule.data_type)))
            result = []
            for item in schedules:
                stock = session.get(Stock, item.stock_id)
                last = session.scalar(select(SchedulerRun).where(
                    SchedulerRun.schedule_id == item.id).order_by(SchedulerRun.started_at.desc()).limit(1))
                next_run = self._trigger(item.cron_expression, item.timezone).get_next_fire_time(None, now) if item.enabled and self.master_enabled() else None
                result.append(ScheduleView(item, stock, last, next_run))
            return result

    def recent_runs(self, limit: int = 20) -> list[SchedulerRun]:
        with self._sessions() as session:
            return list(session.scalars(select(SchedulerRun).order_by(SchedulerRun.started_at.desc()).limit(limit)))

    def next_run(self) -> datetime | None:
        values = [view.next_run for view in self.views() if view.next_run]
        return min(values) if values else None

    @staticmethod
    def _trigger(expression: str, timezone_name: str) -> CronTrigger:
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unbekannte Zeitzone.") from exc
        try:
            return CronTrigger.from_crontab(expression, timezone=zone)
        except ValueError as exc:
            raise ValueError("Ungültiger Cron-Ausdruck.") from exc

    @staticmethod
    def _default_values(stock: Stock) -> list[tuple[str, str, str]]:
        is_us = stock.currency.upper() == "USD" or "NASDAQ" in stock.exchange.upper()
        if not is_us:
            return [
                ("quote", "0 9,13,17 * * 1-5", "Europe/Berlin"),
                ("history", "15 9,13,17 * * 1-5", "Europe/Berlin"),
            ]
        zone = "America/New_York"
        return [
            ("quote", "30 9-15 * * 1-5", zone),
            ("history", "15 16 * * 1-5", zone),
            ("news", "0 8,13,18 * * 1-5", zone),
            ("fundamentals", "0 7 * * 1-5", zone),
            ("analyst", "15 7 * * 1-5", zone),
            ("earnings", "30 7 * * 1-5", zone),
        ]
