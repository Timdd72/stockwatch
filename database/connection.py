"""Erzeugung von SQLite-Verbindungen und Sessions."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


DEFAULT_DATABASE_PATH = Path("data/stockwatch.db")


def create_database(path: str | Path = DEFAULT_DATABASE_PATH) -> Engine:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations(engine)
    return engine


def _apply_lightweight_migrations(engine: Engine) -> None:
    """Ergänzt nullable Spalten, ohne bestehende SQLite-Daten neu aufzubauen."""
    columns = {item["name"] for item in inspect(engine).get_columns("api_usage")}
    statements = []
    if "data_type" not in columns:
        statements.append("ALTER TABLE api_usage ADD COLUMN data_type VARCHAR(32)")
    if "error_type" not in columns:
        statements.append("ALTER TABLE api_usage ADD COLUMN error_type VARCHAR(64)")
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)
    _migrate_stock_ai_analysis(engine)
    _migrate_analysis_snapshots(engine)
    _migrate_manual_quotes(engine)
    _migrate_opportunity_search(engine)
    _migrate_ai_usage(engine)
    _migrate_scanner_candidate_technicals(engine)
    _migrate_opportunity_watchlist(engine)
    _backfill_opportunity_candidate_summaries(engine)
    _migrate_opportunity_status_semantics(engine)
    _backfill_stock_provider_symbols(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_action_running ON manual_action_runs(action_key) WHERE status='RUNNING'")
        connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_search_running ON opportunity_search_runs(status) WHERE status='RUNNING'")


def _migrate_stock_ai_analysis(engine: Engine) -> None:
    """Erweitert historische KI-Analysen ohne vorhandene Zeilen zu verlieren."""
    inspector = inspect(engine)
    if "stock_ai_analysis" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("stock_ai_analysis")}
    additions = {
        "legacy_rating": "VARCHAR(24)",
        "position_rating": "VARCHAR(24)",
        "position_confidence": "FLOAT",
        "entry_rating": "VARCHAR(24)",
        "entry_confidence": "FLOAT",
        "position_reason": "TEXT",
        "entry_reason": "TEXT",
        "protect_profit_low": "FLOAT",
        "protect_profit_high": "FLOAT",
    }
    with engine.begin() as connection:
        for name, sql_type in additions.items():
            if name not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE stock_ai_analysis ADD COLUMN {name} {sql_type}"
                )
        connection.exec_driver_sql(
            "UPDATE stock_ai_analysis SET legacy_rating = rating "
            "WHERE legacy_rating IS NULL AND position_rating IS NULL"
        )


def _migrate_analysis_snapshots(engine: Engine) -> None:
    """Ergänzt Kurssemantik; unsichere historische Marktzeitpunkte bleiben NULL."""
    inspector = inspect(engine)
    if "analysis_snapshots" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("analysis_snapshots")}
    additions = {"price_type":"VARCHAR(16)","market_timestamp":"DATETIME",
                 "fetched_at":"DATETIME","provider":"VARCHAR(32)"}
    with engine.begin() as connection:
        for name,sql_type in additions.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE analysis_snapshots ADD COLUMN {name} {sql_type}")
        connection.exec_driver_sql("UPDATE analysis_snapshots SET fetched_at=timestamp WHERE fetched_at IS NULL")
        # Nur zeitlich eindeutig einem protokollierten Request zuordenbare Altzeilen klassifizieren.
        connection.exec_driver_sql("""
            UPDATE analysis_snapshots SET price_type='DAILY_CLOSE', provider='alpha_vantage'
            WHERE price_type IS NULL AND EXISTS (
              SELECT 1 FROM stocks s JOIN api_usage u ON u.symbol=s.symbol
              WHERE s.id=analysis_snapshots.stock_id AND u.provider='alpha_vantage'
                AND u.endpoint='TIME_SERIES_DAILY' AND u.success=1
                AND abs((julianday(u.timestamp)-julianday(analysis_snapshots.timestamp))*86400)<10
            )
        """)
        connection.exec_driver_sql("""
            UPDATE analysis_snapshots SET price_type='REALTIME', provider='finnhub'
            WHERE price_type IS NULL AND EXISTS (
              SELECT 1 FROM stocks s JOIN api_usage u ON u.symbol=s.symbol
              WHERE s.id=analysis_snapshots.stock_id AND u.provider='finnhub'
                AND u.endpoint='quote' AND u.success=1
                AND abs((julianday(u.timestamp)-julianday(analysis_snapshots.timestamp))*86400)<10
            )
        """)


def _migrate_manual_quotes(engine: Engine) -> None:
    inspector=inspect(engine)
    tables=set(inspector.get_table_names())
    with engine.begin() as connection:
        if "stocks" in tables:
            columns={x["name"] for x in inspector.get_columns("stocks")}
            if "external_quote_url" not in columns:
                connection.exec_driver_sql("ALTER TABLE stocks ADD COLUMN external_quote_url TEXT")
        if "analysis_snapshots" in tables:
            columns={x["name"] for x in inspector.get_columns("analysis_snapshots")}
            for name,kind in {"manual_source_url":"TEXT","manual_entered_at":"DATETIME",
                              "manual_entered_by":"VARCHAR(32)"}.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE analysis_snapshots ADD COLUMN {name} {kind}")


def _migrate_opportunity_search(engine: Engine) -> None:
    inspector=inspect(engine)
    if "opportunity_search_runs" not in inspector.get_table_names():return
    columns={x["name"] for x in inspector.get_columns("opportunity_search_runs")}
    additions={"phase":"VARCHAR(32)","deadline_at":"DATETIME","checked_total":"INTEGER NOT NULL DEFAULT 0",
        "news_candidates":"INTEGER NOT NULL DEFAULT 0","watchlist_candidates":"INTEGER NOT NULL DEFAULT 0",
        "rotation_candidates":"INTEGER NOT NULL DEFAULT 0","finalists":"INTEGER NOT NULL DEFAULT 0",
        "current_symbol":"VARCHAR(32)","web_search_calls":"INTEGER NOT NULL DEFAULT 0",
        "news_found":"INTEGER NOT NULL DEFAULT 0"}
    with engine.begin() as connection:
        for name,kind in additions.items():
            if name not in columns:connection.exec_driver_sql(f"ALTER TABLE opportunity_search_runs ADD COLUMN {name} {kind}")


def _migrate_ai_usage(engine:Engine)->None:
    inspector=inspect(engine)
    if "ai_usage" not in inspector.get_table_names():return
    columns={x["name"] for x in inspector.get_columns("ai_usage")}
    with engine.begin() as connection:
        if "tool_type" not in columns:connection.exec_driver_sql("ALTER TABLE ai_usage ADD COLUMN tool_type VARCHAR(32)")
        if "tool_calls" not in columns:connection.exec_driver_sql("ALTER TABLE ai_usage ADD COLUMN tool_calls INTEGER NOT NULL DEFAULT 0")


def _migrate_scanner_candidate_technicals(engine:Engine)->None:
    """Macht ausschließlich bei fehlender Historie legitime Technikfelder nullable."""
    inspector=inspect(engine)
    table="scanner_candidate_technicals"
    if table not in inspector.get_table_names():return
    columns={x["name"]:x for x in inspector.get_columns(table)}
    nullable_fields=("price","trend","momentum_score","opportunity_score")
    if all(columns[name]["nullable"] for name in nullable_fields):return
    names=[x["name"] for x in inspector.get_columns(table)]
    quoted=", ".join(names)
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE scanner_candidate_technicals__new (
              id INTEGER NOT NULL PRIMARY KEY,
              technical_run_id INTEGER NOT NULL,
              scanner_candidate_id INTEGER NOT NULL,
              history_provider VARCHAR(32) NOT NULL,
              trading_days INTEGER NOT NULL,
              price FLOAT,
              performance_5d FLOAT,
              performance_20d FLOAT,
              performance_60d FLOAT,
              sma20 FLOAT,
              sma50 FLOAT,
              sma200 FLOAT,
              rsi14 FLOAT,
              volatility_20d FLOAT,
              distance_sma20_percent FLOAT,
              distance_sma50_percent FLOAT,
              trend VARCHAR(16),
              high_20d FLOAT,
              low_20d FLOAT,
              distance_high_20d_percent FLOAT,
              distance_low_20d_percent FLOAT,
              high_60d FLOAT,
              low_60d FLOAT,
              momentum_score FLOAT,
              opportunity_score FLOAT,
              candidate_type VARCHAR(32) NOT NULL,
              reason TEXT NOT NULL,
              created_at DATETIME NOT NULL,
              CONSTRAINT uq_technical_run_candidate UNIQUE (technical_run_id, scanner_candidate_id),
              FOREIGN KEY(technical_run_id) REFERENCES scanner_technical_runs (id),
              FOREIGN KEY(scanner_candidate_id) REFERENCES scanner_candidates (id)
            )
        """)
        connection.exec_driver_sql(f"INSERT INTO scanner_candidate_technicals__new ({quoted}) SELECT {quoted} FROM scanner_candidate_technicals")
        connection.exec_driver_sql("DROP TABLE scanner_candidate_technicals")
        connection.exec_driver_sql("ALTER TABLE scanner_candidate_technicals__new RENAME TO scanner_candidate_technicals")
        connection.exec_driver_sql("CREATE INDEX ix_scanner_candidate_technicals_technical_run_id ON scanner_candidate_technicals (technical_run_id)")


def _backfill_opportunity_candidate_summaries(engine:Engine)->None:
    """Stellt historische Shortlists für sieben Tage wieder bereit, ohne sie zu verändern."""
    inspector=inspect(engine)
    required={"opportunity_candidate_summaries","scanner_candidates"}
    if not required.issubset(inspector.get_table_names()):return
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            INSERT OR IGNORE INTO opportunity_candidate_summaries
              (catalog_id,symbol,name,first_seen,last_seen,latest_scanner_run_id,
               latest_candidate_id,quality_score,opportunity_score,classification)
            SELECT first.catalog_id,last.symbol,last.name,first.first_seen,last.created_at,
                   last.scanner_run_id,last.id,last.quality_score,last.opportunity_score,last.candidate_type
            FROM (SELECT catalog_id,MIN(created_at) AS first_seen
                  FROM scanner_candidates WHERE status='SHORTLISTED' GROUP BY catalog_id) first
            JOIN scanner_candidates last ON last.id=(
                SELECT c.id FROM scanner_candidates c
                WHERE c.catalog_id=first.catalog_id AND c.status='SHORTLISTED'
                ORDER BY c.created_at DESC,c.id DESC LIMIT 1)
        """)


def _migrate_opportunity_watchlist(engine:Engine)->None:
    inspector=inspect(engine)
    if "opportunity_watchlist" not in inspector.get_table_names():return
    columns={x["name"] for x in inspector.get_columns("opportunity_watchlist")}
    additions={"favorite":"BOOLEAN NOT NULL DEFAULT 0","favorited_at":"DATETIME",
        "initial_price":"FLOAT","initial_quality_score":"FLOAT","initial_opportunity_score":"FLOAT",
        "initial_entry_rating":"VARCHAR(24)","initial_reason":"TEXT"}
    with engine.begin() as connection:
        for name,kind in additions.items():
            if name not in columns:connection.exec_driver_sql(f"ALTER TABLE opportunity_watchlist ADD COLUMN {name} {kind}")


def _migrate_opportunity_status_semantics(engine:Engine)->None:
    """Korrigiert ausschließlich den früher pauschal als PARTIAL markierten Capability-Fall."""
    tables=set(inspect(engine).get_table_names())
    needed={"scanner_technical_runs","scanner_candidate_technicals","opportunity_search_runs","scanner_runs"}
    if not needed.issubset(tables):return
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            UPDATE scanner_technical_runs SET status='COMPLETED',
              message='Technische Prüfung abgeschlossen; tarifbedingt fehlende Historien wurden gekennzeichnet.'
            WHERE status='PARTIAL'
              AND message='Mindestens eine Historie war nicht verfügbar; übrige Ergebnisse wurden gespeichert.'
              AND NOT EXISTS (SELECT 1 FROM scanner_candidate_technicals t
                              WHERE t.technical_run_id=scanner_technical_runs.id
                                AND t.history_provider!='unavailable')
        """)


def _backfill_stock_provider_symbols(engine:Engine)->None:
    """Übernimmt sichere Katalogmappings und das explizit bekannte MBG-Xetra-Mapping."""
    tables=set(inspect(engine).get_table_names())
    if not {"stocks","security_catalog","stock_provider_symbols"}.issubset(tables):return
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            INSERT OR IGNORE INTO stock_provider_symbols
              (stock_id,provider,symbol,status,source,verified_at,updated_at)
            SELECT s.id,'alpha_vantage',c.provider_symbol_alpha_vantage,'available','catalog',NULL,CURRENT_TIMESTAMP
            FROM stocks s JOIN security_catalog c ON upper(c.symbol)=upper(s.symbol)
            WHERE c.provider_symbol_alpha_vantage IS NOT NULL
        """)
        connection.exec_driver_sql("""
            INSERT OR IGNORE INTO stock_provider_symbols
              (stock_id,provider,symbol,status,source,verified_at,updated_at)
            SELECT id,'alpha_vantage','MBG.DEX','available','migration',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
            FROM stocks WHERE upper(symbol)='MBG' AND upper(exchange) IN ('XETR','XETRA')
        """)
        connection.exec_driver_sql("""
            UPDATE opportunity_search_runs SET status='COMPLETED',phase='COMPLETED',
              message=CASE WHEN candidates_found>0
                THEN 'Chancensuche abgeschlossen · ' || candidates_found || ' interessante Kandidaten gefunden.'
                ELSE 'Chancensuche abgeschlossen · derzeit keine ausreichend bestätigte Kaufchance.' END
            WHERE status='PARTIAL'
              AND message='Chancensuche teilweise abgeschlossen · API-Budget erreicht oder Daten nicht verfügbar.'
              AND EXISTS (SELECT 1 FROM scanner_runs s WHERE s.id=opportunity_search_runs.scanner_run_id AND s.status='COMPLETED')
              AND EXISTS (SELECT 1 FROM scanner_technical_runs t WHERE t.id=opportunity_search_runs.technical_run_id AND t.status='COMPLETED')
        """)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
