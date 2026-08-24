"""SQLite-basierter Doppelklick-/Parallel-Schutz für manuelle Aktionen."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from database.models import ManualActionRun

class ActionAlreadyRunning(RuntimeError): pass

class ActionGuard:
    def __init__(self,sessions:sessionmaker[Session],stale_after:timedelta=timedelta(hours=1))->None:
        self._sessions=sessions;self._stale_after=stale_after
    def start(self,key:str,stale_after:timedelta|None=None)->int:
        now=datetime.now(timezone.utc);cutoff=now-(stale_after or self._stale_after)
        try:
            with self._sessions.begin() as s:
                active=s.scalar(select(ManualActionRun).where(ManualActionRun.action_key==key,
                    ManualActionRun.status=="RUNNING").order_by(ManualActionRun.id.desc()).limit(1))
                if active:
                    stamp=active.started_at if active.started_at.tzinfo else active.started_at.replace(tzinfo=timezone.utc)
                    if stamp>=cutoff:raise ActionAlreadyRunning("Diese Aktion läuft bereits. Bitte warten.")
                    active.status="FAILED";active.finished_at=now
                row=ManualActionRun(action_key=key,started_at=now,status="RUNNING");s.add(row);s.flush();return row.id
        except IntegrityError:
            raise ActionAlreadyRunning("Diese Aktion läuft bereits. Bitte warten.") from None
    def finish(self,run_id:int,success:bool=True)->None:
        with self._sessions.begin() as s:
            row=s.get(ManualActionRun,run_id)
            if row:row.status="SUCCESS" if success else "FAILED";row.finished_at=datetime.now(timezone.utc)
