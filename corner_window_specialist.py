"""Especialista de escanteios para as janelas críticas do AURA.

Este módulo é um filtro de contexto, não uma garantia de resultado. Ele opera
somente em shadow/paper trade, exige dados atuais e rejeita previsões quando a
janela, a qualidade ou a continuidade da pressão não podem ser confirmadas.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping


def _num(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sum_pair(row: Mapping[str, Any], stem: str) -> float:
    if stem in row:
        return max(0.0, _num(row.get(stem)))
    return max(0.0, _num(row.get(f"{stem}_casa")) + _num(row.get(f"{stem}_visit")))


def parse_minute(value: Any) -> float | None:
    text = str(value or "").strip().replace("'", "")
    match = re.search(r"(\d+)\s*(?:\+\s*(\d+))?", text)
    if not match:
        return None
    base = float(match.group(1))
    extra = float(match.group(2) or 0.0)
    return base + extra / 100.0


def classify_window(period: Any, minute: Any) -> str:
    p = str(period or "").upper()
    text = str(minute or "").strip().replace("'", "")
    match = re.search(r"(\d+)\s*(?:\+\s*(\d+))?", text)
    if not match:
        return "UNKNOWN"
    base = int(match.group(1))
    first_half = p in {"HT", "1H", "FIRST_HALF", "PRIMEIRO_TEMPO"} or (not p and base <= 45)
    second_half = p in {"ST", "2H", "SECOND_HALF", "SEGUNDO_TEMPO", "FT", "FULL_TIME"} or (not p and base >= 46)
    if first_half and 35 <= base <= 45:
        return "HT_35_TO_INTERVAL"
    if second_half and base >= 85:
        return "FT_85_TO_END"
    return "OUTSIDE_CRITICAL_WINDOW"


def _score_pressure(row: Mapping[str, Any], window: str) -> tuple[float, dict[str, float], list[str]]:
    minute = max(parse_minute(row.get("minute", row.get("tempo"))) or 0.0, 1.0)
    attacks = _sum_pair(row, "dangerous_attacks") or _sum_pair(row, "ataques_perigosos")
    shots = _sum_pair(row, "shots_total") or _sum_pair(row, "chutes_totais")
    corners = _sum_pair(row, "corners") or _sum_pair(row, "escanteios")
    pressure = _sum_pair(row, "pressure") or _sum_pair(row, "pressao1")
    blocked = _sum_pair(row, "blocked_shots") or _sum_pair(row, "chutes_bloqueados")
    crosses_blocked = _num(row.get("crosses_blocked_last_5m", row.get("cruzamentos_bloqueados_ultimos_5m")), 0.0)
    minutes_since_corner = _num(row.get("minutes_since_last_corner", row.get("gap_ultimo_escanteio")), 0.0)
    substitutions = _num(row.get("substitutions_after_75", row.get("substituicoes_apos_75")), 0.0)
    red_cards = _num(row.get("red_cards", row.get("cartoes_vermelhos")), 0.0)
    score_diff = _num(row.get("score_diff", row.get("gol_diff")), 0.0)
    match_type = str(row.get("match_type", row.get("tipo_partida", ""))).lower()
    appm = _num(row.get("appm_total"), attacks / minute if attacks else 0.0)
    appm_10 = _num(row.get("appm_10min", row.get("appm_10")), appm)
    appm_5_present = any(k in row for k in ("appm_5min", "appm_5"))
    appm_5 = _num(row.get("appm_5min", row.get("appm_5")), appm)
    appm_3 = _num(row.get("appm_3min", row.get("appm_3")), appm_5)
    diff_appm_10 = _num(row.get("diff_appm_10"), 0.0)
    diff_appm_5 = _num(row.get("diff_appm_5"), 0.0)
    diff_appm_3 = _num(row.get("diff_appm_3"), 0.0)
    diff_pressure_1 = _num(row.get("diff_pressao1", row.get("diff_pressure1")), 0.0)
    diff_pressure_2 = _num(row.get("diff_pressao2", row.get("diff_pressure2")), 0.0)
    superiority = _num(row.get("superioridade_dominante", row.get("superioridade")), 0.0)
    diff_corners = _num(row.get("diff_esc", row.get("diff_corners")), 0.0)
    odd = _num(row.get("odd_entrada", row.get("entry_odd")), 0.0)
    if not appm:
        appm = attacks / minute
    territorial = _num(row.get("territorial_pressure", row.get("ipt")), 0.0)
    lateral = _num(row.get("lateral_attack_rate", row.get("vfl")), 0.0)
    fouls_5m = _num(row.get("fouls_last_5m", row.get("faltas_ultimos_5m")), 0.0)
    score = 0.0
    reasons: list[str] = []
    score += _clip(appm / 1.0, 0.0, 1.0) * 0.16
    score += _clip(appm_10 / 0.8, 0.0, 1.0) * 0.10
    score += _clip(appm_5 / 0.8, 0.0, 1.0) * 0.16
    score += _clip(max(diff_appm_5, 0.0) / 0.8, 0.0, 1.0) * 0.12
    score += _clip(max(diff_pressure_1, 0.0) / 30.0, 0.0, 1.0) * 0.08
    score += _clip(max(diff_corners, 0.0) / 3.0, 0.0, 1.0) * 0.06
    score += _clip(shots / (7.0 if window.startswith("HT") else 15.0), 0.0, 1.0) * 0.18
    score += _clip(pressure / 70.0, 0.0, 1.0) * 0.16
    score += _clip(blocked / 2.0, 0.0, 1.0) * 0.22
    score += _clip(crosses_blocked / 2.0, 0.0, 1.0) * 0.10
    score += _clip(territorial / 70.0, 0.0, 1.0) * 0.12 if territorial else 0.0
    score += _clip(lateral / 0.55, 0.0, 1.0) * 0.08 if lateral else 0.0
    if fouls_5m > 3:
        score -= 0.35
        reasons.append("high_foul_disruption")
    if minutes_since_corner > 15:
        score -= 0.30
        reasons.append("long_corner_gap")
    if substitutions > 4:
        score -= 0.25
        reasons.append("late_substitution_fragmentation")
    if abs(score_diff) >= 2 and minute >= 75:
        score -= 0.30
        reasons.append("elastic_scoreline_late")
    if red_cards > 0:
        reasons.append("red_card_requires_lateral_conversion_check")
    if any(token in match_type for token in ("friendly", "amistoso", "preseason", "pre_temporada")):
        score -= 0.20
        reasons.append("friendly_match_penalty")
    if territorial and territorial < 60:
        score -= 0.30
        reasons.append("territorial_pressure_below_floor")
    features = {
        "minute": minute, "attacks_per_minute": round(appm, 6), "appm_10min": appm_10, "appm_5min": appm_5, "appm_5_present": 1.0 if appm_5_present else 0.0, "appm_3min": appm_3,
        "diff_appm_10": diff_appm_10, "diff_appm_5": diff_appm_5, "diff_appm_3": diff_appm_3, "diff_pressure_1": diff_pressure_1, "diff_pressure_2": diff_pressure_2,
        "superiority_dominante": superiority, "diff_corners": diff_corners, "odd_entrada": odd, "shots_total": shots,
        "corners_total": corners, "pressure_index": pressure, "blocked_shots": blocked,
        "territorial_pressure": territorial, "lateral_attack_rate": lateral, "fouls_last_5m": fouls_5m,
        "crosses_blocked_last_5m": crosses_blocked, "minutes_since_last_corner": minutes_since_corner,
        "substitutions_after_75": substitutions, "red_cards": red_cards, "score_diff": score_diff,
    }
    if appm >= 1.0: reasons.append("appm_threshold_met")
    if appm_5 >= 0.6: reasons.append("appm_5min_strong")
    if appm_5 > appm_10: reasons.append("momentum_accelerating")
    if diff_appm_5 > 0: reasons.append("diff_appm_5_positive")
    if diff_pressure_1 >= 15: reasons.append("pressure_differential_strong")
    if superiority >= 60: reasons.append("superiority_threshold_met")
    if diff_corners > 0: reasons.append("corner_advantage_positive")
    if 1.70 <= odd <= 2.00: reasons.append("odd_in_reference_band")
    if blocked >= 2: reasons.append("blocked_shots_threshold_met")
    return _clip(score, 0.0, 1.0), features, reasons


class CornerWindowSpecialist:
    MODEL_VERSION = "corner-window-specialist-v2-reviewed-memory"

    def __init__(self, memory_path: str | None = None) -> None:
        default = Path(__file__).with_name("corner_pattern_memory.json")
        self.memory_path = Path(memory_path) if memory_path else default
        self.memory = self._load_memory()

    def _load_memory(self) -> dict[str, Any]:
        try:
            data = json.loads(self.memory_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"patterns": []}
        except (OSError, json.JSONDecodeError):
            return {"patterns": [], "memory_version": "unavailable"}

    def analyse(self, row: Mapping[str, Any]) -> dict[str, Any]:
        window = classify_window(row.get("period"), row.get("minute", row.get("tempo")))
        temporal_role = str(row.get("temporal_role", row.get("alert_role", "ENTRY_OR_DECISION"))).upper()
        league_text = str(row.get("league", row.get("competition", ""))).lower()
        league_prior = self.memory.get("league_priors", {}).get("BRASILEIRAO_2026") if "brasileirao" in league_text or "serie a" in league_text or "serie b" in league_text else None
        score, features, reasons = _score_pressure(row, window)
        if league_prior:
            reasons.append("brasileirao_2026_prior_loaded_context_only")
        memory_matches = []
        for pattern in self.memory.get("patterns", []):
            if pattern.get("window") not in {"BOTH", window}:
                continue
            pattern_id = str(pattern.get("id", ""))
            if pattern_id == "P_RED_LONG_CORNER_GAP" and features.get("minutes_since_last_corner", 0) > 15:
                memory_matches.append(pattern_id)
            elif pattern_id == "P_RED_FRAGMENTATION" and (features.get("fouls_last_5m", 0) > 3 or features.get("substitutions_after_75", 0) > 4):
                memory_matches.append(pattern_id)
            elif pattern_id == "P_RED_STERILE_POSSESSION" and features.get("lateral_attack_rate", 0) == 0 and features.get("blocked_shots", 0) == 0 and features.get("pressure_index", 0) > 40:
                memory_matches.append(pattern_id)
            elif pattern_id == "P_RED_DEEP_BLOCK" and features.get("lateral_attack_rate", 0) == 0 and features.get("crosses_blocked_last_5m", 0) == 0:
                memory_matches.append(pattern_id)
            elif pattern_id == "P_RED_LATE_FRIENDLY" and any(token in str(row.get("match_type", row.get("tipo_partida", ""))).lower() for token in ("friendly", "amistoso", "preseason", "pre_temporada")):
                memory_matches.append(pattern_id)
        if memory_matches:
            reasons.extend([f"reviewed_memory:{x}" for x in memory_matches])
            score = _clip(score - 0.12 * len(memory_matches), 0.0, 1.0)
        hard_ft_momentum_block = window == "FT_85_TO_END" and features.get("appm_5_present", 0.0) == 1.0 and features.get("appm_5min", 0.0) < 0.30
        if hard_ft_momentum_block:
            reasons.append("ft_appm_5_below_0_30_kill")
            score = 0.0
        quality = _clip(_num(row.get("data_quality"), 0.0), 0.0, 1.0)
        missing = [key for key in ("minute",) if row.get(key, row.get("tempo")) in (None, "")]
        if quality == 0.0:
            reasons.append("data_quality_not_declared")
        if missing:
            reasons.append("missing_current_timing")
        if window == "UNKNOWN": reasons.append("unknown_window")
        if window == "OUTSIDE_CRITICAL_WINDOW": reasons.append("outside_configured_window")
        valid = window in {"HT_35_TO_INTERVAL", "FT_85_TO_END"} and quality >= 0.60 and not missing
        confidence = _clip(score * quality, 0.0, 1.0)
        decision = "OBSERVE" if valid and confidence >= 0.62 and not any(x in reasons for x in ("high_foul_disruption", "territorial_pressure_below_floor", "ft_appm_5_below_0_30_kill")) else "NO_BET"
        if temporal_role in {"PRE_ALERT", "PREPARATION", "PRE_ALERTA"} and decision == "OBSERVE":
            decision = "PREPARE"
        reasons.append("pre_alert_is_non_executable" if temporal_role in {"PRE_ALERT", "PREPARATION", "PRE_ALERTA"} else "decision_snapshot")
        return {
            "model_version": self.MODEL_VERSION,
            "window": window,
            "temporal_role": temporal_role,
            "decision": decision,
            "pressure_score": round(score, 6),
            "confidence": round(confidence, 6),
            "features": features,
            "reasons": reasons,
            "memory_version": self.memory.get("memory_version", "unavailable"),
            "memory_matches": memory_matches,
            "league_prior_loaded": bool(league_prior),
            "league_prior_policy": "context_only_not_calibrated_probability" if league_prior else "not_applicable",
            "mode": "SHADOW_PAPER_TRADE",
            "advisory_only": True,
            "execution_allowed": False,
        }

    def evaluate(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return self.analyse(row)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "model_version": self.MODEL_VERSION,
            "memory_version": self.memory.get("memory_version", "unavailable"),
            "specialty": "corners",
            "windows": ["HT_35_TO_INTERVAL", "FT_85_TO_END"],
            "mode": "SHADOW_PAPER_TRADE",
            "advisory_only": True,
            "execution_allowed": False,
        }


__all__ = ["CornerWindowSpecialist", "classify_window", "parse_minute"]
