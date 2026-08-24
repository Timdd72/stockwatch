"""Zentrale OpenAI-Konfiguration; Secrets werden nur im Arbeitsspeicher gehalten."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path


def _env_values(path: str | Path = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting(name: str, default: str, values: dict[str, str]) -> str:
    return os.environ.get(name) or values.get(name) or default


@dataclass(frozen=True, slots=True)
class AiConfig:
    model: str
    monthly_budget_eur: Decimal
    input_price_eur_per_million: Decimal
    output_price_eur_per_million: Decimal
    analysis_fresh_hours: int = 24
    web_search_cost_eur_per_call: Decimal = Decimal("0")

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "AiConfig":
        values = _env_values(env_path)
        try:
            return cls(
                model=_setting("OPENAI_MODEL", "gpt-5.6-luna", values),
                monthly_budget_eur=Decimal(
                    _setting("OPENAI_MONTHLY_BUDGET_EUR", "8.00", values)
                ),
                # Zentral änderbare EUR-Kalkulationswerte. Das externe Billing bleibt maßgeblich.
                input_price_eur_per_million=Decimal(
                    _setting("OPENAI_INPUT_PRICE_EUR_PER_MILLION", "0.18", values)
                ),
                output_price_eur_per_million=Decimal(
                    _setting("OPENAI_OUTPUT_PRICE_EUR_PER_MILLION", "1.08", values)
                ),
                analysis_fresh_hours=int(
                    _setting("OPENAI_ANALYSIS_FRESH_HOURS", "24", values)
                ),
                web_search_cost_eur_per_call=Decimal(
                    _setting("OPENAI_WEB_SEARCH_COST_EUR_PER_CALL", "0", values)
                ),
            )
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Die OpenAI-Konfiguration enthält ungültige Zahlenwerte.") from exc


def load_openai_api_key(env_path: str | Path = ".env") -> str:
    values = _env_values(env_path)
    key = os.environ.get("OPENAI_API_KEY") or values.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY fehlt oder ist leer.")
    return key
