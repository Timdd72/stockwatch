#!/usr/bin/env python3
"""Manueller, produktiver Konsolenaufruf des bestehenden MKR-14-Service."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from time import monotonic

from sqlalchemy import select
from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai import AiAnalysisService, MkrAnalysisService, MkrInputAssembler
from ai.config import load_openai_api_key
from database import (
    DEFAULT_DATABASE_PATH,
    AiUsage,
    ApiUsage,
    ApiUsageService,
    Stock,
    create_database,
    create_session_factory,
)
from services import MarketUpdateService


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Eine MKR-14-Analyse manuell ausführen.")
    parser.add_argument("--stock-id", type=int, required=True, help="ID aus der stocks-Tabelle")
    parser.add_argument("--force", action="store_true", help="Fingerprint-Cache bewusst umgehen")
    parser.add_argument("--json", action="store_true", help="Zusätzlich das validierte Ergebnis als JSON ausgeben")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH, help=argparse.SUPPRESS)
    return parser.parse_args()


def _value(value: object) -> str:
    return "NICHT VERFÜGBAR" if value is None else str(value)


def _level(level: object) -> str:
    if level is None:
        return "NICHT VERFÜGBAR"
    value = getattr(level, "value", None)
    currency = getattr(level, "currency", "")
    level_type = getattr(getattr(level, "level_type", None), "value", "PRICE")
    basis = getattr(level, "basis", "")
    suffix = f" {currency}" if level_type == "PRICE" and currency else (
        " %" if level_type == "PERCENTAGE" else ""
    )
    amount = "NICHT VERFÜGBAR" if value is None else f"{value:.5g}{suffix}"
    return f"{amount} ({basis})"


def main() -> int:
    args = _arguments()
    sessions = create_session_factory(create_database(args.database))
    usage = ApiUsageService(sessions)
    market_updates = MarketUpdateService(sessions, usage)
    assembler = MkrInputAssembler(
        sessions, history_loader=market_updates.load_configured_history_for_analysis
    )
    # Der kontrollierte Einzellauf darf auch auf Transportebene keinen automatischen
    # zweiten API-Versuch auslösen.
    ai = AiAnalysisService(
        sessions,
        client_factory=lambda: OpenAI(api_key=load_openai_api_key(), max_retries=0),
    )
    service = MkrAnalysisService(sessions, assembler, ai)

    started_at = datetime.now(timezone.utc)
    started = monotonic()
    prepared = assembler.build(args.stock_id)
    technical = prepared.technical_data or {}
    history = technical.get("history_metadata") or {}
    price = prepared.price or {}

    with sessions() as session:
        stock = session.get(Stock, args.stock_id)
        history_requests = list(session.scalars(
            select(ApiUsage).where(
                ApiUsage.timestamp >= started_at,
                ApiUsage.data_type == "history",
            ).order_by(ApiUsage.id)
        ))
    if stock is None:
        raise LookupError("Die Aktie wurde nicht gefunden.")

    print("MKR-14 PRE-FLIGHT")
    print(f"Aktie:             {stock.name}")
    print(f"stock_id:          {stock.id}")
    print(f"Provider-Symbol:   {_value(history.get('provider_symbol'))}")
    print(f"History-Quelle:    {'PROVIDER' if history_requests else 'CACHE'}")
    print(f"History-Provider:  {_value(history.get('provider'))}")
    print(f"Daily-OHLCV:       {_value(history.get('number_of_rows'))} Zeilen")
    print(f"Erstes Datum:      {_value(history.get('first_date'))}")
    print(f"Letztes Datum:     {_value(history.get('last_date'))}")
    print(f"52W High:          {_value(technical.get('week_52_high'))} {stock.currency}")
    print(f"52W Low:           {_value(technical.get('week_52_low'))} {stock.currency}")
    print("Framework-Abdeckung:")
    for item in prepared.framework_availability:
        print(f"  {item.number:>2} {item.name}: {item.data_quality.value} / {item.coverage.value}")
    print("\nOpenAI-Aufruf startet jetzt (Web Search deaktiviert).", flush=True)

    run = service.analyze(args.stock_id, force=args.force)
    duration = monotonic() - started
    result = run.result

    with sessions() as session:
        usage_row = session.scalar(
            select(AiUsage).where(
                AiUsage.stock_id == args.stock_id,
                AiUsage.purpose == "mkr_analysis",
                AiUsage.timestamp >= started_at,
            ).order_by(AiUsage.timestamp.desc(), AiUsage.id.desc()).limit(1)
        )

    print("\nMKR-14 ANALYSE")
    print(f"Aktie:             {stock.name}")
    print(f"Symbol:            {stock.symbol}")
    print(f"Verwendeter Kurs:  {_value(price.get('value'))} {stock.currency}")
    print(f"Kursart:           {_value(price.get('price_type'))}")
    print(f"Kursdatum:         {_value(price.get('market_timestamp'))}")
    print(f"History Provider:  {_value(history.get('provider'))}")
    print(f"History-Zeilen:    {_value(history.get('number_of_rows'))}")
    print(f"Prompt-Version:    {run.record.prompt_version}")
    print(f"OpenAI-Modell:     {run.record.model}")
    print(f"Analyse-ID:        {run.record.id}")
    print(f"Cache verwendet:   {'ja' if run.reused else 'nein'}")
    print(f"Dauer:             {duration:.2f} Sekunden")
    print(f"MKR Confidence:    {result.confidence}/100")
    print(
        "Datenabdeckung:    "
        f"{result.data_coverage.full} GOOD · "
        f"{result.data_coverage.limited} LIMITED · "
        f"{result.data_coverage.not_available} INSUFFICIENT"
    )
    if usage_row is not None:
        print(
            f"Tokenverbrauch:    {usage_row.input_tokens} Input · "
            f"{usage_row.output_tokens} Output · {usage_row.total_tokens} Gesamt"
        )
        print(f"Geschätzte Kosten: {Decimal(usage_row.estimated_cost_eur):.6f} EUR")

    print("\nZUSAMMENFASSUNG")
    for sentence in result.summary:
        print(f"- {sentence}")

    print("\nSCORECARD")
    print("Nr | Framework | Signal | Stärke | Confidence | Datenqualität")
    print("---|-----------|--------|--------|------------|---------------")
    for item in sorted(result.frameworks, key=lambda value: value.number):
        strength = "–" if item.strength is None else str(item.strength)
        print(
            f"{item.number:>2} | {item.name} | {item.signal.value} | {strength} | "
            f"{item.confidence}% | {item.data_quality.value}"
        )

    levels = result.price_levels
    print("\nWICHTIGE PREISLEVELS")
    for label, level in (
        ("Stop / Invalidation", levels.stop_invalidation),
        ("Support 1", levels.support_1), ("Support 2", levels.support_2),
        ("Aktueller Kurs", levels.current_price), ("Ziel 1", levels.target_1),
        ("Ziel 2", levels.target_2), ("Extended Target", levels.extended_target),
    ):
        print(f"{label}: {_level(level)}")

    entry = result.entry_timing
    print("\nEINSTIEGSEINSCHÄTZUNG")
    print(f"Status: {entry.status.value}")
    print(f"Triggerpreis: {_level(entry.trigger_price)}")
    print(f"Stop / Invalidation: {_level(entry.stop_invalidation)}")
    for alert in entry.alerts:
        print(f"- Alarm: {alert}")

    print(f"\nUNI Score: {_value(result.uni_score.score)}/25")
    print(f"PEG: {_value(result.peg)}")
    print("\nTOP-3-RISIKEN")
    for risk in result.risks:
        print(f"- {risk}")

    print("\nDETAILS DER 14 FRAMEWORKS")
    for item in sorted(result.frameworks, key=lambda value: value.number):
        print(f"\n{item.number}. {item.name}")
        print(f"Signal: {item.signal.value} · Stärke: {_value(item.strength)} · Vertrauen: {item.confidence}%")
        print(f"Datenqualität: {item.data_quality.value}")
        print(item.explanation)
        for level in item.levels:
            print(f"- Level: {_level(level)}")
    print(f"\nVERMEIDEN WENN\n{result.avoid_if}")
    print(f"\nOPTIONS / LEAPS\n{result.options.statement}")
    print(f"\nWHEEL / CSP\n{result.wheel.statement}")

    if args.json:
        print("\nVALIDIERTES JSON")
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
