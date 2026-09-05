"""Grounding rígido do CornerAI / AURA.

Princípio: capturado → validado → normalizado → entregue à IA.
null permanece null. Ausência nunca vira zero, média ou chute.
Contexto de outra fixture nunca entra no prompt.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple


VOICE_REQUEST_RE = re.compile(
    r"(?:responda|responde|responder|fale|falar|leia|ler|diga|dizer)\s+"
    r"(?:isso\s+)?(?:com|por|em)\s+(?:voz|audio|áudio)"
    r"|(?:sempre\s+)?(?:com|por|em)\s+voz(?:\s+alta)?"
    r"|\bouvir\s+(?:a\s+)?resposta"
    r"|^/(?:voz|speak|tts)\b",
    re.IGNORECASE,
)
SPEAK_ALWAYS_RE = re.compile(r"sempre\s+(responda|responde|fale|falar).{0,20}voz|voz\s+sempre|always\s+speak", re.I)


def parse_voice_request(message: str) -> Tuple[bool, str]:
    """Detecta pedido de TTS e devolve (speak, mensagem_limpa)."""
    text = str(message or "").strip()
    if not text:
        return False, ""
    speak = bool(VOICE_REQUEST_RE.search(text))
    cleaned = VOICE_REQUEST_RE.sub(" ", text) if speak else text
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t,.;:-")
    return speak, cleaned


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return n


def _pair(value: Any) -> Dict[str, Optional[float]]:
    if value is None or value == "":
        return {"home": None, "away": None}
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return {"home": _num(value[0]), "away": _num(value[1])}
    if isinstance(value, dict):
        return {
            "home": _num(value.get("home", value.get("h"))),
            "away": _num(value.get("away", value.get("a"))),
        }
    return {"home": None, "away": None}


def _pair_filled(*values: Any) -> Dict[str, Optional[float]]:
    for value in values:
        pair = _pair(value)
        if pair.get("home") is not None or pair.get("away") is not None:
            return pair
    return {"home": None, "away": None}


def _pick(src: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in src and src.get(key) not in ("",):
            return src.get(key)
    return None


def _same_fixture(candidate: Any, fixture_id: Optional[str]) -> bool:
    if not fixture_id:
        return False
    if not isinstance(candidate, dict):
        return False
    cid = candidate.get("fixtureId") or candidate.get("fixture_id") or candidate.get("id")
    return str(cid) == str(fixture_id) if cid not in (None, "") else False


def fmt_nd(value: Any) -> str:
    if value is None or value == "":
        return "N/D"
    return str(value)


def fmt_pair(pair: Dict[str, Optional[float]]) -> str:
    return f"{fmt_nd(pair.get('home'))} / {fmt_nd(pair.get('away'))}"


def build_grounded_card(
    snapshot: Dict[str, Any],
    fixture_id: Optional[str],
    client_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Recorte permitido. Qualquer dado de outra fixture é descartado."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    client = client_context if isinstance(client_context, dict) else {}
    if isinstance(snap.get("client"), dict):
        # prefer explicit client_context, then snapshot.client
        merged_client = dict(snap.get("client") or {})
        merged_client.update(client)
        client = merged_client

    analysis = snap.get("analysis") if isinstance(snap.get("analysis"), dict) else None
    if analysis and fixture_id and not _same_fixture(analysis, fixture_id):
        analysis = None

    live = snap.get("live_metrics") if isinstance(snap.get("live_metrics"), dict) else {}
    live_fid = live.get("fixtureId") or live.get("fixture_id")
    if live and fixture_id and (not live_fid or str(live_fid) == str(fixture_id)):
        merged_live = dict(live)
        merged_live.update(client)
        client = merged_live

    # client context só entra se for da fixture atual
    allowed_client = client
    client_fid = client.get("fixtureId") or client.get("fixture_id") or (
        (client.get("activeFixture") or {}).get("fixtureId") if isinstance(client.get("activeFixture"), dict) else None
    )
    if fixture_id and client_fid and str(client_fid) != str(fixture_id):
        allowed_client = {}
    if not fixture_id:
        allowed_client = {}

    teams = (analysis or {}).get("teams") if isinstance((analysis or {}).get("teams"), dict) else {}
    view = snap.get("view") if isinstance(snap.get("view"), dict) else {}
    payload = snap.get("payload") if isinstance(snap.get("payload"), dict) else {}
    stats = {}
    if isinstance(snap.get("stats"), dict):
        stats = dict(snap.get("stats") or {})
    if isinstance((analysis or {}).get("stats"), dict):
        for k, v in (analysis.get("stats") or {}).items():
            stats.setdefault(k, v)
    if isinstance(allowed_client.get("stats"), dict):
        for k, v in allowed_client["stats"].items():
            stats.setdefault(k, v)

    score = _pair_filled(
        _pick(snap, "score"),
        [view.get("score_home"), view.get("score_away")],
        _pick(allowed_client, "score"),
        _pick(analysis or {}, "score"),
        (payload.get("fixture") or {}).get("score") if isinstance(payload.get("fixture"), dict) else None,
    )
    corners = _pair_filled(
        _pick(snap, "corners"),
        [view.get("corners_home"), view.get("corners_away")],
        (payload.get("corners") or {}).get("total") if isinstance(payload.get("corners"), dict) else None,
        _pick(stats, "corners"),
        _pick(allowed_client, "corners"),
    )
    xg = _pair_filled(
        _pick(snap, "xg"),
        [view.get("xg_home"), view.get("xg_away")],
        (payload.get("pressure") or {}).get("xg") if isinstance(payload.get("pressure"), dict) else None,
        _pick(stats, "xg"),
        _pick(allowed_client, "xg"),
    )
    appm = _pair_filled(_pick(stats, "appm"), _pick(allowed_client, "appm"))
    possession = _pair_filled(
        [view.get("possession_home"), view.get("possession_away")],
        _pick(stats, "possession"),
        _pick(allowed_client, "possession"),
    )
    extra_stats = {}
    for key in ("attacks", "dangerous", "shots", "shotsOn", "shotsOff", "fouls", "offsides", "yellow", "red", "subs", "crosses", "saves", "passes"):
        extra_stats[key] = _pair_filled(
            _pick(snap, key),
            _pick(stats, key),
            (allowed_client.get("stats") or {}).get(key) if isinstance(allowed_client.get("stats"), dict) else None,
            [view.get(f"{key}_home"), view.get(f"{key}_away")] if key in ("attacks",) else None,
            [view.get("dangerous_home"), view.get("dangerous_away")] if key == "dangerous" else None,
            (payload.get("pressure") or {}).get(key) if isinstance(payload.get("pressure"), dict) else None,
        )

    h2h = None
    for src in (analysis or {}, allowed_client, snap.get("intelligence") if isinstance(snap.get("intelligence"), dict) else {}):
        cand = src.get("h2h") if isinstance(src, dict) else None
        if not isinstance(cand, dict):
            continue
        cid = cand.get("fixtureId") or cand.get("fixture_id")
        if cid and fixture_id and str(cid) != str(fixture_id):
            continue
        if not fixture_id:
            continue
        compact = {
            "fixtureId": cid or fixture_id,
            "games": cand.get("games") or (cand.get("summary") or {}).get("total") or (cand.get("parameters") or {}).get("matches"),
            "summary": cand.get("summary"),
            "parameters": cand.get("parameters"),
            "averages": cand.get("averages") if isinstance(cand.get("averages"), dict) else None,
        }
        h2h = compact
        break

    hist = None
    for src in (analysis or {}, allowed_client):
        cand = src.get("history") or src.get("historico") or src.get("averages")
        if cand is None:
            continue
        if isinstance(cand, dict) and cand.get("fixtureId") and str(cand.get("fixtureId")) != str(fixture_id):
            continue
        hist = cand
        break

    home = snap.get("home") or view.get("home") or teams.get("home") or allowed_client.get("home")
    away = snap.get("away") or view.get("away") or teams.get("away") or allowed_client.get("away")
    minute = snap.get("minute")
    if minute is None:
        minute = view.get("minute")
    if minute is None:
        minute = (analysis or {}).get("minute")
    if minute is None:
        minute = allowed_client.get("minute")

    events = snap.get("events") or view.get("corner_events") or (payload.get("corners") or {}).get("events") or allowed_client.get("events") or []
    if not isinstance(events, list):
        events = []

    card = {
        "fixture_lock": str(fixture_id or snap.get("fixture_id") or snap.get("match_id") or view.get("fixture_id") or "") or None,
        "home": home if home not in ("", None) else None,
        "away": away if away not in ("", None) else None,
        "minute": _num(minute),
        "extra_minute": _num(view.get("extra") if view.get("extra") is not None else allowed_client.get("extraMinute") if allowed_client.get("extraMinute") is not None else (analysis or {}).get("extraMinute")),
        "live_status": view.get("status") or allowed_client.get("liveStatus") or (analysis or {}).get("liveStatus"),
        "score": score,
        "corners": corners,
        "xg": xg,
        "appm": appm,
        "possession": possession,
        "stats_extra": extra_stats,
        "events": events[:16],
        "odds": allowed_client.get("odds"),
        "quality": allowed_client.get("quality"),
        "charts": allowed_client.get("charts"),
        "intelligence": allowed_client.get("intelligence"),
        "client_analysis": allowed_client.get("analysis"),
        "signal": (analysis or {}).get("signal") or (analysis or {}).get("decision") or (allowed_client.get("analysis") or {}).get("decision"),
        "corner_prob": _num((analysis or {}).get("corner_prob")),
        "goal_prob": _num((analysis or {}).get("goal_prob")),
        "market": (analysis or {}).get("market"),
        "edge": _num((analysis or {}).get("edge")),
        "reason": (analysis or {}).get("reason") or (analysis or {}).get("explanation"),
        "h2h": h2h,
        "history": hist,
        "missing": [],
        "policy": {
            "null_stays_null": True,
            "never_invent_average": True,
            "never_use_other_fixture": True,
            "absence_is_not_zero": True,
        },
    }
    for key in ("home", "away", "minute", "corners", "xg"):
        val = card.get(key)
        if val is None or (isinstance(val, dict) and val.get("home") is None and val.get("away") is None):
            card["missing"].append(key)
    if not fixture_id:
        card["missing"].append("fixture_id")
    return card


def card_to_prompt(card: Dict[str, Any]) -> str:
    fid = card.get("fixture_lock") or "NENHUMA"
    lines = [
        "[FIXTURE LOCK]",
        f"id: {fid}",
        f"contexto_permitido: somente fixture {fid}",
        "qualquer dado com fixture != deste id FOI DESCARTADO",
        f"casa: {fmt_nd(card.get('home'))}",
        f"visitante: {fmt_nd(card.get('away'))}",
        f"minuto: {fmt_nd(card.get('minute'))}",
        f"acréscimo: {fmt_nd(card.get('extra_minute'))}",
        f"placar: {fmt_pair(card.get('score') or {})}",
        f"status: {fmt_nd(card.get('live_status'))}",
        "",
        "[STATS OBSERVADAS — N/D significa AUSENTE, não use zero]",
        f"escanteios: {fmt_pair(card.get('corners') or {})}",
        "corner_events: "
        + (
            "; ".join(
                f"{(e or {}).get('m', '?')}′ {(e or {}).get('side', '?')} {(e or {}).get('team') or ''}".strip()
                for e in (card.get("events") or [])[:12]
                if isinstance(e, dict)
            )
            or "N/D"
        ),
        f"xG: {fmt_pair(card.get('xg') or {})}",
        f"AP/min: {fmt_pair(card.get('appm') or {})}",
        f"posse: {fmt_pair(card.get('possession') or {})}",
        f"ataques: {fmt_pair((card.get('stats_extra') or {}).get('attacks') or {})}",
        f"ataques perigosos: {fmt_pair((card.get('stats_extra') or {}).get('dangerous') or {})}",
        f"finalizações: {fmt_pair((card.get('stats_extra') or {}).get('shots') or {})}",
        f"no gol: {fmt_pair((card.get('stats_extra') or {}).get('shotsOn') or {})}",
        f"cartões: A {fmt_pair((card.get('stats_extra') or {}).get('yellow') or {})} · V {fmt_pair((card.get('stats_extra') or {}).get('red') or {})}",
        f"finalizações fora: {fmt_pair((card.get('stats_extra') or {}).get('shotsOff') or {})}",
        f"faltas: {fmt_pair((card.get('stats_extra') or {}).get('fouls') or {})}",
        f"impedimentos: {fmt_pair((card.get('stats_extra') or {}).get('offsides') or {})}",
        f"cruzamentos: {fmt_pair((card.get('stats_extra') or {}).get('crosses') or {})}",
        f"defesas: {fmt_pair((card.get('stats_extra') or {}).get('saves') or {})}",
        f"passes: {fmt_pair((card.get('stats_extra') or {}).get('passes') or {})}",
        f"eventos recentes: {json.dumps(card.get('events') or [], ensure_ascii=False)}",
        f"odds: {json.dumps(card.get('odds'), ensure_ascii=False) if card.get('odds') else 'N/D'}",
        f"qualidade captura: {fmt_nd(card.get('quality'))}",
        f"motor: {json.dumps(card.get('client_analysis'), ensure_ascii=False) if card.get('client_analysis') else 'N/D'}",
        f"sinal: {fmt_nd(card.get('signal'))}",
        f"P(canto): {fmt_nd(card.get('corner_prob'))}",
        f"P(gol): {fmt_nd(card.get('goal_prob'))}",
        f"mercado: {fmt_nd(card.get('market'))}",
        f"edge: {fmt_nd(card.get('edge'))}",
        "",
        "[HISTÓRICO / H2H]",
        f"h2h: {json.dumps(card.get('h2h'), ensure_ascii=False) if card.get('h2h') else 'N/D — não inventar'}",
        f"médias/histórico: {json.dumps(card.get('history'), ensure_ascii=False) if card.get('history') else 'N/D — não inventar'}",
        "",
        "[REGRAS ABSOLUTAS]",
        "1. null / N/D permanece N/D.",
        "2. Nunca converter ausência em zero.",
        "3. Nunca inventar média de escanteios ou qualquer estatística.",
        "4. Nunca usar números de outra partida.",
        "5. Sempre identificar fixtureId e equipes quando existirem.",
        "6. Diferencie live atual, H2H, histórico, mercado e gráfico.",
        "7. Se não houver dado, diga exatamente: não há dado.",
        "8. Você não controla o áudio. Nunca diga que não pode falar.",
        f"campos_ausentes: {', '.join(card.get('missing') or []) or 'nenhum'}",
    ]
    return "\n".join(lines)


def deterministic_voice_summary(card: Dict[str, Any]) -> str:
    """Resposta falável sem LLM quando o pedido é só 'responda com voz'."""
    if not card.get("fixture_lock"):
        return (
            "Não há partida capturada neste momento. "
            "Quando o snapshot chegar, eu falo somente os dados observados."
        )
    home = fmt_nd(card.get("home"))
    away = fmt_nd(card.get("away"))
    minute = fmt_nd(card.get("minute"))
    score = card.get("score") or {}
    corners = card.get("corners") or {}
    parts = [
        f"Hálem, Partida {home} contra {away}, fixture {card.get('fixture_lock')}.",
        f"Minuto {minute}, placar {fmt_nd(score.get('home'))} a {fmt_nd(score.get('away'))}.",
        f"Escanteios observados: {fmt_pair(corners)}.",
    ]
    if card.get("signal"):
        parts.append(f"Sinal atual: {card.get('signal')}.")
    if card.get("corner_prob") is None:
        parts.append("Não há probabilidade de canto calculada neste snapshot.")
    else:
        parts.append(f"Probabilidade de canto no motor: {card.get('corner_prob')}.")
    if "xg" in (card.get("missing") or []):
        parts.append("xG não disponível.")
    if not card.get("h2h"):
        parts.append("Não há H2H neste contexto.")
    if not card.get("history"):
        parts.append("Não há médias históricas neste contexto. Não vou inventar número.")
    return " ".join(parts)
