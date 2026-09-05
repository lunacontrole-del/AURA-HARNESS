"""Prompt builder determinístico para o pipeline analítico do AURA.

O módulo é deliberadamente puro: lê somente a configuração recebida, aplica
limites seguros e devolve texto. Não acessa rede, não inicia serviços e não
promove configuração automaticamente.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Sequence


_DEFAULTS: dict[str, float | int] = {
    "trend_rising_threshold": 4,
    "excitation_threshold": 0.30,
    "corner_rate_threshold": 2,
    "entra_min_triggers": 2,
    "entra_min_confidence": 0.70,
    "entra_min_score": 70,
}


def _safe_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed >= 0 else default


def _safe_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


@dataclass(frozen=True)
class PromptContext:
    """Valores efetivos usados na renderização do prompt analítico."""

    trend_rising_threshold: int = 4
    excitation_threshold: float = 0.30
    corner_rate_threshold: int = 2
    entra_min_triggers: int = 2
    entra_min_confidence: float = 0.70
    entra_min_score: int = 70
    validated_patterns: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    trigger_reliability: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any] | None,
        *,
        validated_patterns: Sequence[Mapping[str, Any]] | None = None,
        trigger_reliability: Mapping[str, Any] | None = None,
    ) -> "PromptContext":
        values = config if isinstance(config, Mapping) else {}
        patterns = tuple(
            item for item in (validated_patterns or ()) if isinstance(item, Mapping)
        )
        reliability = (
            dict(trigger_reliability)
            if isinstance(trigger_reliability, Mapping)
            else {}
        )
        return cls(
            trend_rising_threshold=_safe_int(
                values.get("trend_rising_threshold"), int(_DEFAULTS["trend_rising_threshold"])
            ),
            excitation_threshold=_safe_float(
                values.get("excitation_threshold"), float(_DEFAULTS["excitation_threshold"])
            ),
            corner_rate_threshold=_safe_int(
                values.get("corner_rate_threshold"), int(_DEFAULTS["corner_rate_threshold"])
            ),
            entra_min_triggers=_safe_int(
                values.get("entra_min_triggers"), int(_DEFAULTS["entra_min_triggers"])
            ),
            entra_min_confidence=_safe_float(
                values.get("entra_min_confidence"), float(_DEFAULTS["entra_min_confidence"])
            ),
            entra_min_score=_safe_int(
                values.get("entra_min_score"), int(_DEFAULTS["entra_min_score"])
            ),
            validated_patterns=patterns,
            trigger_reliability=reliability,
        )


def _render_patterns(patterns: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for pattern in patterns:
        triggers = str(pattern.get("triggers") or "").strip()
        if not triggers:
            continue
        try:
            rate = float(pattern.get("rate", 0.0))
        except (TypeError, ValueError):
            rate = 0.0
        if not math.isfinite(rate):
            rate = 0.0
        try:
            sample = int(pattern.get("n", 0))
        except (TypeError, ValueError):
            sample = 0
        lines.append(f"- {triggers}: {round(rate * 100):d}% (n={sample})")
    return "\n".join(lines) if lines else "- Nenhum padrão validado disponível."


def build_system_prompt(context: PromptContext) -> str:
    """Renderiza um prompt operacional somente para análise e auditoria."""

    patterns = _render_patterns(context.validated_patterns)
    return f"""ANALISTA QUANTITATIVO — AURA QUANT-X

Você é um analista esportivo de escanteios. Produza somente análise auditável,
sem administrar dinheiro, sem apostar e sem executar ordens.

PASSO 1 — VALIDAR A EVIDÊNCIA
- Confirmar dados recentes, amostra e qualidade da fonte.
- Considerar tendência de pressão quando Delta AP 10min >= {context.trend_rising_threshold}.
- Considerar excitação quando o índice for >= {context.excitation_threshold:.2f}.
- Considerar ritmo de escanteios quando houver >= {context.corner_rate_threshold} no recorte definido.
- Em cold start, declarar explicitamente a falta de histórico suficiente.

REGRA DURA DE ENTRA
- Só propor ENTRA quando houver os gatilhos independentes exigidos.
- Pelo menos {context.entra_min_triggers} gatilhos independentes.
- A confiança mínima é >= {context.entra_min_confidence:.2f}.
- O score mínimo é >= {context.entra_min_score}.
- Se a evidência estiver incompleta, divergente ou stale, retornar AGUARDA.

PADRÕES VALIDADOS
{patterns}

PASSO 4 — SAÍDA AUDITÁVEL
- Informar evidências, horizonte, confiança, kills e limitações.
- A decisão é advisory-only e permanece Paper-Only.
- Nunca inventar feed, estatística, evento ou resultado ausente.
"""


__all__ = ["PromptContext", "build_system_prompt"]
