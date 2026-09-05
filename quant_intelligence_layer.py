"""Filtro quantitativo leve e determinístico do AURA Quant-X.

O módulo reduz ruído e comprime telemetria normalizada antes de qualquer
possível revisão GLM. Ele mantém somente memória RAM bounded por fixture,
não abre SQLite, não chama Ollama/GLM e nunca transforma heurística em
ordem, autorização ou probabilidade garantida.
"""
from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from threading import RLock
from typing import Any, Callable, Mapping

try:
    from imc_learning_module import IMCPostMatchLearner
except ImportError:
    from engine.imc_learning_module import IMCPostMatchLearner


Number = int | float


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _sum_value(value: Any) -> float:
    if isinstance(value, Mapping):
        return sum(_sum_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_sum_value(item) for item in value)
    return _finite(value, 0.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class QuantIntelligenceLayer:
    """Aplica dead zone e engenharia de atributos sem efeitos externos."""

    def __init__(
        self,
        *,
        dead_zone_xg: float = 0.05,
        dead_zone_pressure: float = 10.0,
        max_fixtures: int = 128,
        max_patterns: int = 512,
        pattern_lookup: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.dead_zone_xg = max(0.0, _finite(dead_zone_xg, 0.05))
        self.dead_zone_pressure = max(0.0, _finite(dead_zone_pressure, 10.0))
        self.max_fixtures = max(1, int(max_fixtures))
        self.max_patterns = max(1, int(max_patterns))
        self._states: OrderedDict[str, dict[str, float]] = OrderedDict()
        self._patterns: OrderedDict[str, dict[str, int]] = OrderedDict()
        self._pattern_lookup = pattern_lookup
        self._lock = RLock()
        self.learner = IMCPostMatchLearner()

    @staticmethod
    def _normalise(payload: Mapping[str, Any]) -> dict[str, Any]:
        minute = _finite(payload.get("minute"), 0.0)
        corners = max(0.0, _sum_value(payload.get("corners", 0)))
        xg = max(0.0, _sum_value(payload.get("xg", payload.get("xG", 0.0))))
        pressure = _clamp(_finite(payload.get("pressure", payload.get("pressure_percent", 0.0))), 0.0, 100.0)
        danger = max(0.0, _sum_value(payload.get("dangerous_attacks", payload.get("danger", 0))))
        fixture_id = str(payload.get("fixture_id") or payload.get("fixtureId") or "__default__").strip()[:160] or "__default__"
        return {
            "fixture_id": fixture_id,
            "minute": minute,
            "corners": corners,
            "xg": xg,
            "pressure": pressure,
            "dangerous_attacks": danger,
        }

    def _bounded_put(self, mapping: OrderedDict[str, Any], key: str, value: Any, limit: int) -> None:
        mapping[key] = value
        mapping.move_to_end(key)
        while len(mapping) > limit:
            mapping.popitem(last=False)

    def _feature_vector(self, state: Mapping[str, Any]) -> dict[str, float | str]:
        minute = max(_finite(state.get("minute"), 0.0), 1.0)
        corner_rate = _finite(state.get("corners"), 0.0) / minute
        danger_rate = _finite(state.get("dangerous_attacks"), 0.0) / minute
        pressure_norm = _clamp(_finite(state.get("pressure"), 0.0) / 100.0, 0.0, 1.0)
        weights = self.learner.get_current_weights()
        momentum_index = round((danger_rate * 10.0) * (_finite(state.get("xg"), 0.0) * weights["xg_multiplier"]) * (pressure_norm * weights["pressure_multiplier"]), 2)
        return {
            "momentum_index": momentum_index,
            "corner_rate_per_min": round(corner_rate, 4),
            "danger_rate_per_min": round(danger_rate, 4),
            "minute_bucket": int(max(_finite(state.get("minute"), 0.0), 0.0) // 15),
            "corner_band": "HIGH" if corner_rate > 0.15 else "LOW",
        }

    @staticmethod
    def _pattern_id(features: Mapping[str, Any]) -> str:
        material = "|".join(
            (
                f"MI{_finite(features.get('momentum_index')):.1f}",
                str(features.get("corner_band", "LOW")),
                f"MIN{int(_finite(features.get('minute_bucket'), 0))}",
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def _find_pattern(self, pattern_id: str) -> Mapping[str, Any] | None:
        local = self._patterns.get(pattern_id)
        if local:
            occurrences = int(local.get("occurrences", 0))
            happened = int(local.get("positive", 0))
            return {
                "occurrences": occurrences,
                "historical_rate": round((happened / occurrences) * 100.0, 1) if occurrences else 0.0,
                "source": "ram_observations",
            }
        if self._pattern_lookup is not None:
            try:
                candidate = self._pattern_lookup(pattern_id)
                return dict(candidate) if isinstance(candidate, Mapping) else None
            except Exception:
                return None
        return None

    def process_telemetry(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """Retorna IGNORE_SILENT, PATTERN_MATCH ou NEEDS_AI advisory-only."""
        try:
            source = payload if isinstance(payload, Mapping) else {}
            state = self._normalise(source)
            fixture_id = str(state["fixture_id"])
            features = self._feature_vector(state)
            with self._lock:
                previous = self._states.get(fixture_id)
                if previous is not None:
                    delta_xg = abs(float(state["xg"]) - float(previous["xg"]))
                    delta_pressure = abs(float(state["pressure"]) - float(previous["pressure"]))
                    if delta_xg < self.dead_zone_xg and delta_pressure < self.dead_zone_pressure and state["corners"] == previous["corners"]:
                        return {
                            "action": "IGNORE_SILENT",
                            "reason": "dead_zone_noise",
                            "fixture_id": fixture_id,
                            "features": features,
                            "advisory_only": True,
                            "execution_allowed": False,
                        }
                self._bounded_put(self._states, fixture_id, {key: float(value) for key, value in state.items() if isinstance(value, (int, float))}, self.max_fixtures)
                pattern_id = self._pattern_id(features)
                pattern = self._find_pattern(pattern_id)
            if pattern is not None:
                return {
                    "action": "PATTERN_MATCH",
                    "reason": "ram_pattern_observation",
                    "fixture_id": fixture_id,
                    "pattern_id": pattern_id,
                    "historical": dict(pattern),
                    "features": features,
                    "advisory_only": True,
                    "execution_allowed": False,
                }
            compressed = {
                "fixture_id": fixture_id,
                "minute": round(float(state["minute"]), 2),
                "corners": round(float(state["corners"]), 2),
                "xg": round(float(state["xg"]), 4),
                "pressure": round(float(state["pressure"]), 2),
                "dangerous_attacks": round(float(state["dangerous_attacks"]), 2),
                **features,
            }
            return {
                "action": "NEEDS_AI",
                "reason": "relevant_normalised_event",
                "fixture_id": fixture_id,
                "pattern_id": self._pattern_id(features),
                "features": features,
                "compressed_context": compressed,
                "advisory_only": True,
                "execution_allowed": False,
            }
        except Exception as exc:
            return {
                "action": "NEEDS_AI",
                "reason": "quant_filter_degraded",
                "error_type": type(exc).__name__,
                "advisory_only": True,
                "execution_allowed": False,
            }

    def record_outcome(self, fixture_id: str, payload: Mapping[str, Any], positive: bool) -> dict[str, Any]:
        """Regista uma observação RAM-only; não persiste nem autoriza sinais."""
        state = self._normalise({**dict(payload), "fixture_id": fixture_id})
        features = self._feature_vector(state)
        pattern_id = self._pattern_id(features)
        with self._lock:
            current = self._patterns.get(pattern_id, {"occurrences": 0, "positive": 0})
            current["occurrences"] += 1
            current["positive"] += 1 if positive else 0
            self._bounded_put(self._patterns, pattern_id, current, self.max_patterns)
        return {"ok": True, "pattern_id": pattern_id, "ram_only": True, "execution_allowed": False}

    def record_post_match(self, match_data: Mapping[str, Any]) -> dict[str, Any]:
        """Atualiza o shadow IMC somente com outcome final explícito."""
        result = self.learner.process_end_of_match(match_data, finalized=bool(match_data.get("finalized")))
        result["learning_mode"] = "POST_MATCH_ONLY"
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "implementation": "DETERMINISTIC_RAM_BOUNDED",
                "tracked_fixtures": len(self._states),
                "pattern_count": len(self._patterns),
                "max_fixtures": self.max_fixtures,
                "max_patterns": self.max_patterns,
                "dead_zone_xg": self.dead_zone_xg,
                "dead_zone_pressure": self.dead_zone_pressure,
                "glm_calls": 0,
                "advisory_only": True,
                "execution_allowed": False,
                "imc_learning": self.learner.status(),
            }


__all__ = ["QuantIntelligenceLayer"]

# --- V23 BLOCO 7: persistencia de padroes ---
def flush_patterns_to_db(db_conn, patterns: dict | None = None) -> int:
    """Salva padroes aprendidos na RAM para SQLite (INSERT OR REPLACE)."""
    data = patterns if patterns is not None else globals().get("_patterns") or {}
    if not isinstance(data, dict) or not data:
        return 0
    n = 0
    try:
        db_conn.execute(
            """CREATE TABLE IF NOT EXISTS pattern_stats (
                pattern_id TEXT PRIMARY KEY,
                occurrences INTEGER,
                positive_rate REAL
            )"""
        )
        for pattern_id, pdata in data.items():
            if not isinstance(pdata, dict):
                continue
            db_conn.execute(
                """INSERT OR REPLACE INTO pattern_stats (pattern_id, occurrences, positive_rate)
                   VALUES (?, ?, ?)""",
                (
                    str(pattern_id),
                    int(pdata.get("count") or pdata.get("occurrences") or 0),
                    float(pdata.get("positive") or pdata.get("positive_rate") or 0.0),
                ),
            )
            n += 1
        db_conn.commit()
    except Exception as e:
        try:
            from core.error_handler import safe_catch
            safe_catch(e, "flush_patterns_to_db")
        except Exception:
            pass
    return n
