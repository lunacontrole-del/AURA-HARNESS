"""Checagem máxima de veracidade dos dados captados (SokkerPro / fontes).

Objetivo: impedir que dados errados, stale, inconsistentes ou fisicamente
impossíveis alimentem análise, edge ou decisão.

Camadas:
1. Identidade (fixture, times)
2. Frescor temporal (timestamp / idade)
3. Limites físicos (ranges impossíveis)
4. Consistência cruzada (stats vs eventos, placar vs gols)
5. Monotonicidade por fixture (minuto/escanteios não regridem sem reset)
6. Concordância multi-fonte (quando houver)

Status:
- BLOCK  → não operar / não confiar na análise
- WARN   → analisar com confiança reduzida
- OK     → dados coerentes no que foi possível verificar
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# Memória de sessão por fixture (processo local do Engine)
_FIXTURE_MEMORY: Dict[str, Dict[str, Any]] = {}
_MEMORY_MAX = 64


def _num(value: Any) -> Optional[float]:
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pair(stats: Any, key: str) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(stats, dict):
        return None, None
    block = stats.get(key)
    if isinstance(block, dict):
        return _num(block.get("home")), _num(block.get("away"))
    if isinstance(block, (list, tuple)) and len(block) >= 2:
        return _num(block[0]), _num(block[1])
    return None, None


def _stamp_ms(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    stamp = (
        payload.get("capturedAt")
        or payload.get("captured_at")
        or payload.get("timestamp")
        or payload.get("ts")
        or payload.get("lastUpdate")
    )
    if stamp is None:
        return None, "capture_timestamp_missing"
    if isinstance(stamp, (int, float)):
        ms = float(stamp * 1000 if stamp < 10_000_000_000 else stamp)
        return ms, None
    if isinstance(stamp, str):
        try:
            text = stamp.replace("Z", "+00:00")
            from datetime import datetime, timezone

            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp() * 1000.0, None
        except Exception:
            return None, "capture_timestamp_invalid"
    return None, "capture_timestamp_invalid"


def _count_events(events: Any, keywords: Tuple[str, ...]) -> int:
    if not isinstance(events, list):
        return 0
    n = 0
    for event in events:
        if isinstance(event, dict):
            text = " ".join(
                str(event.get(k) or "") for k in ("type", "event", "name", "kind", "label")
            ).lower()
        else:
            text = str(event).lower()
        if any(k in text for k in keywords):
            n += 1
    return n


def _remember(fid: str, minute: Optional[float], corners_total: Optional[float], score_sum: Optional[float]) -> List[str]:
    """Detecta regressões impossíveis no mesmo fixture (salvo reset explícito)."""
    issues: List[str] = []
    if not fid:
        return issues
    prev = _FIXTURE_MEMORY.get(fid)
    now = {
        "minute": minute,
        "corners_total": corners_total,
        "score_sum": score_sum,
        "ts": time.time(),
    }
    if prev:
        # Minuto não deve regredir mais que 1' (exceto HT/novo tempo tratado como warning se cair muito)
        if (
            minute is not None
            and prev.get("minute") is not None
            and minute + 1.0 < float(prev["minute"])
            and not (float(prev["minute"]) >= 45 and minute <= 1.5)  # início 2º tempo
        ):
            # Queda forte = possível troca de jogo com mesmo id ou dado podre
            if float(prev["minute"]) - minute > 5:
                issues.append("minute_regressed")
        if (
            corners_total is not None
            and prev.get("corners_total") is not None
            and corners_total + 0.01 < float(prev["corners_total"])
        ):
            issues.append("corners_regressed")
        if (
            score_sum is not None
            and prev.get("score_sum") is not None
            and score_sum + 0.01 < float(prev["score_sum"])
        ):
            issues.append("score_regressed")
    _FIXTURE_MEMORY[fid] = now
    if len(_FIXTURE_MEMORY) > _MEMORY_MAX:
        # remove mais antigos
        ordered = sorted(_FIXTURE_MEMORY.items(), key=lambda kv: kv[1].get("ts", 0))
        for k, _ in ordered[: max(0, len(ordered) - _MEMORY_MAX)]:
            _FIXTURE_MEMORY.pop(k, None)
    return issues


def verify_payload(payload: Dict[str, Any], *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Executa checagem máxima de veracidade. Não inventa dados."""
    issues: List[str] = []
    warnings: List[str] = []
    checks: Dict[str, str] = {}
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    fid = str(payload.get("fixtureId") or payload.get("fixture_id") or payload.get("fixture") or "").strip()
    home = str(payload.get("home") or payload.get("mandante") or "").strip()
    away = str(payload.get("away") or payload.get("visitante") or "").strip()

    # --- 1) Identidade ---
    if not fid:
        issues.append("fixture_id_missing")
        checks["identity"] = "BLOCK"
    else:
        checks["identity"] = "OK"
    if not home or not away or home.lower() == away.lower():
        issues.append("teams_invalid")
        checks["teams"] = "BLOCK"
    else:
        checks["teams"] = "OK"

    # --- 2) Frescor ---
    captured_ms, stamp_err = _stamp_ms(payload)
    age_ms = None
    if stamp_err:
        issues.append(stamp_err)
        checks["freshness"] = "BLOCK"
    else:
        age_ms = max(0, now_ms - int(captured_ms or 0))
        if age_ms > 45_000:
            issues.append("capture_stale_over_45s")
            checks["freshness"] = "BLOCK"
        elif age_ms > 15_000:
            warnings.append("capture_older_than_15s")
            checks["freshness"] = "WARN"
        else:
            checks["freshness"] = "OK"

    # --- 3) Limites físicos ---
    minute = _num(payload.get("minute"))
    if minute is not None and (minute < 0 or minute > 130):
        issues.append("minute_out_of_range")
        checks["minute_range"] = "BLOCK"
    else:
        checks["minute_range"] = "OK" if minute is not None else "MISSING"

    stats = payload.get("stats") or payload.get("estatisticas") or {}
    if not isinstance(stats, dict):
        stats = {}

    # valores negativos
    for name, block in stats.items():
        if not isinstance(block, dict):
            continue
        for side in ("home", "away", "total", "value"):
            if side not in block:
                continue
            n = _num(block.get(side))
            if n is not None and n < 0:
                issues.append(f"negative_{name}_{side}")

    # posse 0–100
    poss_h, poss_a = _pair(stats, "possession")
    if poss_h is None and poss_a is None:
        poss_h, poss_a = _pair(stats, "posse")
    if poss_h is not None and not (0 <= poss_h <= 100):
        issues.append("possession_home_out_of_range")
    if poss_a is not None and not (0 <= poss_a <= 100):
        issues.append("possession_away_out_of_range")
    if poss_h is not None and poss_a is not None:
        s = poss_h + poss_a
        if s < 90 or s > 110:
            warnings.append("possession_sum_abnormal")

    # chutes no gol ≤ chutes
    shots_h, shots_a = _pair(stats, "shots")
    son_h, son_a = _pair(stats, "shotsOn")
    if son_h is None:
        son_h, son_a = _pair(stats, "shots_on_target")
    if shots_h is not None and son_h is not None and son_h > shots_h + 0.01:
        issues.append("shots_on_gt_shots_home")
    if shots_a is not None and son_a is not None and son_a > shots_a + 0.01:
        issues.append("shots_on_gt_shots_away")

    # xG não absurdo
    xg_h, xg_a = _pair(stats, "xg")
    if xg_h is not None and (xg_h < 0 or xg_h > 15):
        issues.append("xg_home_out_of_range")
    if xg_a is not None and (xg_a < 0 or xg_a > 15):
        issues.append("xg_away_out_of_range")

    checks["physical_limits"] = "BLOCK" if any(
        x.startswith("negative_") or x.endswith("_out_of_range") or x.startswith("shots_on_")
        for x in issues
    ) else "OK"

    # --- 4) Consistência cruzada stats vs eventos ---
    events = payload.get("events") or payload.get("eventos") or payload.get("matchEvents") or []
    corners_h, corners_a = _pair(stats, "corners")
    corner_total = None
    if corners_h is not None or corners_a is not None:
        corner_total = (corners_h or 0) + (corners_a or 0)

    corner_events = _count_events(events, ("corner", "escante", "canto"))
    # P0 fix: when events_complete=True, empty list is authoritative total=0
    if corner_total is not None and isinstance(events, list):
        if events or payload.get("events_complete") is True:
            gap = abs(int(round(corner_total)) - int(corner_events))
            if gap > 0:
                warnings.append("corners_stats_events_mismatch")
            if payload.get("events_complete") is True and gap > 0:
                # any mismatch against complete empty/full list is authoritative conflict
                issues.append("corners_authoritative_sources_conflict")

    # placar vs eventos de gol
    score = payload.get("score") or {}
    sh = _num(score.get("home") if isinstance(score, dict) else None)
    sa = _num(score.get("away") if isinstance(score, dict) else None)
    if sh is None:
        sh = _num(payload.get("homeScore") or payload.get("goalsHome"))
    if sa is None:
        sa = _num(payload.get("awayScore") or payload.get("goalsAway"))
    score_sum = None
    if sh is not None and sa is not None:
        if sh < 0 or sa < 0 or sh > 30 or sa > 30:
            issues.append("score_out_of_range")
        score_sum = sh + sa
        goal_events = _count_events(events, ("goal", "gol", "golo"))
        # P0: empty complete events list means 0 goals
        if isinstance(events, list) and payload.get("events_complete") is True:
            if abs(int(round(score_sum)) - int(goal_events)) > 1:
                issues.append("score_events_conflict")

    checks["cross_source"] = "BLOCK" if any(
        x in issues
        for x in (
            "corners_authoritative_sources_conflict",
            "score_events_conflict",
            "score_out_of_range",
        )
    ) else ("WARN" if "corners_stats_events_mismatch" in warnings else "OK")

    # --- 5) Conflitos declarados pela extensão ---
    source_conflicts = payload.get("sourceConflicts") or payload.get("source_conflicts") or []
    if source_conflicts:
        if isinstance(source_conflicts, list):
            for x in source_conflicts[:6]:
                issues.append("source_conflict:" + str(x))
        else:
            issues.append("source_conflict")
        checks["source_conflicts"] = "BLOCK"
    else:
        checks["source_conflicts"] = "OK"

    # --- 6) Monotonicidade por fixture ---
    mono_issues = _remember(fid, minute, corner_total, score_sum)
    issues.extend(mono_issues)
    checks["monotonicity"] = "BLOCK" if mono_issues else "OK"

    # --- Score de veracidade 0–100 (só sobre o que foi observado) ---
    hard = len(issues)
    soft = len(warnings)
    score_v = max(0, 100 - hard * 18 - soft * 6)
    if checks.get("freshness") == "OK" and hard == 0:
        score_v = min(100, score_v + 5)

    status = "BLOCK" if issues else ("WARN" if warnings else "OK")
    return {
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "checks": checks,
        "veracity_score": score_v,
        "age_ms": age_ms,
        "checked_at": now_ms,
        "fixture_id": fid or None,
        "policy": "max_veracity_v1",
        "safe_to_analyze": status != "BLOCK",
        "safe_to_trade": status == "OK" and score_v >= 70,
    }
