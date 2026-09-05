"""AURA QUANT-X — orquestrador central do assistente.

Este módulo não executa ordens financeiras. Ele reúne contexto verificável,
roteia a conversa e usa o Ollama local apenas para redigir respostas quando
houver motor disponível. Na ausência do modelo, o chamador pode fornecer um
fallback determinístico.
"""
from __future__ import annotations

# A MENTE DO AURA V23 — comportamento frio/militar (nao assistente amigavel)
AURA_SYSTEM_PROMPT = """
[SYSTEM — AURA V23 HARD GROUNDING]
Voce e o AURA QUANT-X V23, sistema autonomo de analise quantitativa de escanteios em tempo real.
Comportamento: analista de dados frio, militar, objetivo e direto. NAO e assistente virtual amigavel.

REGRAS ABSOLUTAS:
1. NUNCA diga "Ola", "Como posso ajudar?", "Claro", "Desculpe" ou saudacao/despedida.
2. NUNCA diga que e inteligencia artificial ou modelo de linguagem.
3. Respostas ULTRA-CURTAS: no maximo 15 palavras por frase. Fato primeiro.
4. Foque APENAS em: escanteios, pressao, xG, posse, padroes taticos e probabilidade.
5. Fora de futebol/estatistica/sistema: responda so "Foco na operacao. Pergunta irrelevante bloqueada."
6. Dados ruins ou HOLD: "Sem edge detectado. Mantendo posicao."
7. Nunca invente placar, odd, fixture, corner_events, media ou H2H. Sem dado: diga "AUSENTE".
8. paper_trade=true. execution_allowed=false. GLM_ADVISORY_ONLY. Nunca execute ordem real.
9. Use SOMENTE o cartao/snapshot da partida atual. null/N/D permanece N/D. Proibido inferir dados ausentes.
10. Responda em portugues brasileiro.
11. Formato preferencial quando analise completa:
   1. ESTADO
   2. RISCO
   3. ACAO CONSULTIVA
   4. DADO AUSENTE
"""


import json
import os
import urllib.error
import urllib.request

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    from engine.core.semantic_cache import LLM_CACHE
except Exception:
    LLM_CACHE = None

try:
    from glm_inference_gate import GLM_INFERENCE_LOCK, wait_for_glm_resources
except ImportError:
    from engine.glm_inference_gate import GLM_INFERENCE_LOCK, wait_for_glm_resources

from grounding import (
    build_grounded_card,
    card_to_prompt,
    deterministic_voice_summary,
    parse_voice_request,
)


TRADING_KEYWORDS = (
    "trading", "trade", "trader", "jogo", "partida", "escanteio", "corner",
    "placar", "odds", "odd", "edge", "risco", "kelly", "sinal", "entra",
    "aguarda", "mercado", "pressão", "pressao", "gol", "regime", "paper",
    "backtest", "roi", "brier", "drawdown", "stake", "aposta", "probabilidade",
)
SYSTEM_KEYWORDS = (
    "status", "sistema", "serviço", "servico", "engine", "bridge",
    "servidor de voz", "voice offline", "voice online",
    "microfone", "ollama", "whisper", "gpu", "cpu", "memória", "memoria",
    "ram", "diagnóstico", "diagnostico", "latência", "latencia", "erro",
    "monitor", "saúde", "saude", "offline", "online", "health",
)
CURRENT_EXTERNAL_KEYWORDS = (
    "notícia", "noticia", "notícias", "noticias", "agora", "hoje", "último",
    "ultimo", "últimas", "ultimas", "preço atual", "preco atual", "cotação",
    "cotacao", "tempo agora", "ao vivo", "live",
)


def classify_intent(message: str) -> str:
    """Classifica o pedido sem impedir conversa fora do domínio de trading."""
    text = str(message or "").strip().lower()
    if not text:
        return "general"
    if any(k in text for k in SYSTEM_KEYWORDS):
        return "system"
    if any(k in text for k in TRADING_KEYWORDS):
        return "trading"
    if any(k in text for k in CURRENT_EXTERNAL_KEYWORDS):
        return "external_current"
    return "general"


def _json_compact(value: Any, limit: int = 12000) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        raw = str(value)
    return raw[:limit]


def _llm_view(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Recorte mínimo para o LLM — nunca enviar log HTTP/observability."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    analysis = snap.get("analysis") if isinstance(snap.get("analysis"), dict) else {}
    client = snap.get("client") if isinstance(snap.get("client"), dict) else {}
    view = snap.get("view") if isinstance(snap.get("view"), dict) else {}
    payload = snap.get("payload") if isinstance(snap.get("payload"), dict) else {}
    corners = snap.get("corners") if isinstance(snap.get("corners"), dict) else {}
    xg = snap.get("xg") if isinstance(snap.get("xg"), dict) else {}
    dangerous = snap.get("dangerous") if isinstance(snap.get("dangerous"), dict) else {}
    risk_src = analysis.get("risk") if isinstance(analysis.get("risk"), dict) else {}
    if not risk_src and isinstance(snap.get("risk"), dict):
        risk_src = snap.get("risk") or {}
    teams = analysis.get("teams") if isinstance(analysis.get("teams"), dict) else {}
    events = snap.get("events") or view.get("corner_events") or (payload.get("corners") or {}).get("events") or []
    if not isinstance(events, list):
        events = []
    compact_events = []
    for item in events[:12]:
        if not isinstance(item, dict):
            continue
        compact_events.append({
            "m": item.get("m"),
            "side": item.get("side"),
            "team": item.get("team"),
        })
    return {
        "partida": {
            "fixture": snap.get("fixture_id") or snap.get("match_id") or view.get("fixture_id") or analysis.get("fixtureId"),
            "casa": snap.get("home") or view.get("home") or teams.get("home") or client.get("home"),
            "fora": snap.get("away") or view.get("away") or teams.get("away") or client.get("away"),
            "minuto": snap.get("minute") if snap.get("minute") is not None else view.get("minute") if view.get("minute") is not None else analysis.get("minute") or client.get("minute"),
            "placar": snap.get("score") if snap.get("score") is not None else [view.get("score_home"), view.get("score_away")],
            "status": view.get("status") or (payload.get("fixture") or {}).get("status"),
        },
        "escanteios": {
            "casa": corners.get("home") if corners.get("home") is not None else view.get("corners_home"),
            "fora": corners.get("away") if corners.get("away") is not None else view.get("corners_away"),
            "eventos": compact_events,
        },
        "xg": {"casa": xg.get("home") if xg.get("home") is not None else view.get("xg_home"), "fora": xg.get("away") if xg.get("away") is not None else view.get("xg_away")},
        "ataques_perigosos": {
            "casa": dangerous.get("home") if dangerous.get("home") is not None else view.get("dangerous_home"),
            "fora": dangerous.get("away") if dangerous.get("away") is not None else view.get("dangerous_away"),
        },
        "sinal": analysis.get("signal") or analysis.get("decision"),
        "p_canto_5min": analysis.get("corner_prob"),
        "p_gol": analysis.get("goal_prob"),
        "mercado": analysis.get("market") or (payload.get("odds") if isinstance(payload.get("odds"), dict) else None),
        "edge": analysis.get("edge"),
        "motivo": analysis.get("reason") or analysis.get("explanation"),
        "risco": {"estado": risk_src.get("state") or risk_src.get("status"), "aprovado": risk_src.get("approved")},
        "pressao": analysis.get("pressure") or payload.get("pressure"),
        "momentum": analysis.get("momentum"),
        "regime": analysis.get("regime") or analysis.get("skill_regime"),
        "paper_trade": True,
    }


def format_context(snapshot: Dict[str, Any], route: str, fixture_id: Optional[str] = None) -> str:
    """Converte o snapshot para um bloco curto e legível pelo LLM."""
    route_note = {
        "trading": "Priorize análise profissional de trading, qualidade dos dados, edge, risco e decisão; nunca trate sinal como garantia.",
        "system": "Priorize diagnóstico operacional, causa provável, impacto e próximo passo seguro.",
        "external_current": "Não invente fatos atuais. Se não houver fonte online no contexto, declare que não há acesso em tempo real e responda apenas com conhecimento geral.",
        "general": "Converse naturalmente sobre assuntos gerais e externos, mantendo clareza, bom humor e honestidade sobre limites.",
    }.get(route, "Responda com clareza e honestidade.")
    card = build_grounded_card(snapshot, fixture_id, snapshot.get("client") if isinstance(snapshot, dict) else None)
    return (
        f"[ROTA DO ASSISTENTE]\n{route_note}\n"
        f"{card_to_prompt(card)}\n"
        f"[CARTÃO COMPACTO]\n{_json_compact(_llm_view(snapshot), 1800)}"
    )


def _system_prompt(route: str) -> str:
    # V23: alma militar + locks de verdade e paper-trade
    return (
        AURA_SYSTEM_PROMPT.strip()
        + "\n\n"
        + "Fixture lock: um jogo por vez. Diagnostico somente leitura. "
        + "Mudancas sao PROPOSTA advisory; nunca trate 'sim' no chat como execucao. "
        + "Para voz: 1 a 3 frases, sem markdown. "
        + f"Rota atual: {route}."
    )


def _semantic_features(snapshot: Dict[str, Any], route: str, fixture_id: Optional[str], message: str) -> Dict[str, Any]:
    stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), dict) else snapshot
    score = snapshot.get("score") or snapshot.get("placar")
    corners = snapshot.get("corners") or snapshot.get("escanteios")
    corners_total = snapshot.get("corners_total")
    if corners_total is None and isinstance(corners, dict):
        values = [corners.get("home"), corners.get("away")]
        corners_total = sum(float(v or 0) for v in values)
    if corners_total is None:
        corners_total = snapshot.get("corner_total") or snapshot.get("escanteios_total")
    return {
        "fixture_id": fixture_id or snapshot.get("fixture_id") or snapshot.get("fixtureId"),
        "minute": snapshot.get("minute") or snapshot.get("minuto"),
        "score": score,
        "corners_total": corners_total,
        "wom_trend": snapshot.get("wom_trend") or snapshot.get("odds_velocity") or snapshot.get("velocity"),
        "route": route,
        # Evita devolver uma resposta de uma pergunta diferente no mesmo estado.
        "query": " ".join(str(message or "").lower().split())[:240],
        "stats_present": bool(stats),
    }


def _ask_ollama(
    message: str,
    snapshot: Dict[str, Any],
    route: str,
    history: Optional[Iterable[Dict[str, Any]]] = None,
    fixture_id: Optional[str] = None,
) -> str:
    """Consulta o Ollama local para texto geral; falha silenciosamente para fallback."""
    cache_features = _semantic_features(snapshot, route, fixture_id, message)
    if LLM_CACHE is not None:
        cached = LLM_CACHE.get_features(cache_features)
        if cached is not None:
            return cached
    host = os.getenv("CORNERAI_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("CORNERAI_CHAT_MODEL", "glm4:9b-chat-q4_0")
    messages: List[Dict[str, str]] = [{"role": "system", "content": _system_prompt(route)}]
    for item in list(history or [])[-8:]:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant") and item.get("content"):
            messages.append({"role": item["role"], "content": str(item["content"])[:3000]})
    messages.append({"role": "user", "content": f"{format_context(snapshot, route, fixture_id)}\n\n[MENSAGEM]\n{message}"})
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": os.getenv("AURA_OLLAMA_KEEP_ALIVE", "0m"),
        "options": {"temperature": 0.28, "num_predict": 96, "num_ctx": 3072, "num_batch": 128, "num_gpu": 99, "top_k": 30, "top_p": 0.85},
    }
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        timeout_s = max(2.0, float(os.getenv("CORNERAI_CHAT_TIMEOUT", "20")))
        try:
            gate_wait_s = max(0.0, min(float(os.getenv("AURA_CHAT_GLM_GATE_WAIT", "1.5")), timeout_s))
        except (TypeError, ValueError):
            gate_wait_s = 1.5
        try:
            resource_wait_s = max(0.0, min(float(os.getenv("AURA_CHAT_GLM_RESOURCE_WAIT", "0.5")), timeout_s))
        except (TypeError, ValueError):
            resource_wait_s = 0.5
        resource_decision = wait_for_glm_resources(resource_wait_s)
        if not resource_decision.get("ready"):
            return ""
        if not GLM_INFERENCE_LOCK.acquire(timeout=gate_wait_s):
            return ""
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            reply = str((data.get("message") or {}).get("content") or data.get("response") or "").strip()
            if reply and LLM_CACHE is not None:
                LLM_CACHE.set_features(cache_features, reply)
            return reply
        finally:
            GLM_INFERENCE_LOCK.release()
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return ""
    except Exception:
        return ""



# --- V23 OTIMIZAÇÃO 3: Cliente HTTP assíncrono para Ollama (anti-congelamento) ---
async def ask_ollama_async(
    prompt: str,
    model: str = "glm4:9b-chat-q4_0",
    timeout: float = 125.0,
    *,
    messages: list | None = None,
) -> str:
    """Chamada assíncrona ao Ollama. Não bloqueia o event loop do FastAPI."""
    if httpx is None:
        return "[Erro: httpx não instalado — rode: pip install httpx]"
    host = __import__("os").getenv("CORNERAI_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    url = f"{host}/api/generate"
    # Injeta personalidade AURA (system). Fallback: prefixa no prompt se /api/generate.
    payload = {
        "model": model,
        "prompt": prompt,
        "system": AURA_SYSTEM_PROMPT,
        "stream": False,
        "keep_alive": os.getenv("AURA_OLLAMA_KEEP_ALIVE", "0m"),
    }
    if messages:
        url = f"{host}/api/chat"
        # garante system no inicio das messages
        msgs = list(messages)
        if not msgs or msgs[0].get("role") != "system":
            msgs = [{"role": "system", "content": AURA_SYSTEM_PROMPT}] + msgs
        payload = {"model": model, "messages": msgs, "stream": False, "keep_alive": os.getenv("AURA_OLLAMA_KEEP_ALIVE", "0m")}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if isinstance(data.get("message"), dict):
                return str(data["message"].get("content") or "")
            return str(data.get("response") or "")
    except Exception as e:
        try:
            import logging
            logging.getLogger("aura.orchestrator").error("[ERRO OLLAMA] %s", e)
        except Exception:
            pass
        return f"[Erro: Ollama indisponível ou timeout: {type(e).__name__}]"



def _read_system_health_for_chat() -> str:
    """V26.3-FIX: conecta system_health_reader ao pipeline de chat (antes era codigo morto)."""
    try:
        try:
            from engine.agents.system_health_reader import format_health_for_chat, get_system_health
        except Exception:
            from agents.system_health_reader import format_health_for_chat, get_system_health
        h = get_system_health()
        return format_health_for_chat(h)
    except Exception as exc:
        return f"[SYSTEM HEALTH] indisponivel: {type(exc).__name__}: {exc}"


def _fallback_general(route: str) -> str:
    if route == "external_current":
        return "Posso conversar sobre esse assunto, mas não vou inventar informação em tempo real. Se você me disser a fonte ou habilitar uma fonte online, eu analiso os dados; caso contrário, posso explicar o contexto geral."
    if route == "system":
        health = _read_system_health_for_chat()
        return (
            "Diagnóstico local com dados reais do monitor:\n"
            + health
            + "\n\nPosso detalhar Engine, Bridge, Desktop, voz, Ollama, GPU, captura ou risco. Qual componente?"
        )
    if route == "trading":
        return "Estou sem o modelo local neste momento. Ainda posso mostrar o estado observado da partida, a qualidade dos dados e os bloqueios de risco, mas não vou fabricar uma decisão."
    return "Estou pronta para conversar sobre assuntos gerais e também coordenar o AURA QUANT-X. O que você quer explorar?"


def orchestrate_chat(
    message: str,
    snapshot: Dict[str, Any],
    route: str,
    fixture_id: Optional[str],
    fallback: Optional[Callable[[], Dict[str, Any]]] = None,
    history: Optional[Iterable[Dict[str, Any]]] = None,
    route_locked: bool = False,
) -> Dict[str, Any]:
    base = dict(fallback() if fallback else {})
    compact = str(message or "").strip()
    speak, cleaned = parse_voice_request(compact)
    work = cleaned or ""
    if work and not route_locked:
        route = classify_intent(work)
    card = build_grounded_card(
        snapshot,
        fixture_id,
        snapshot.get("client") if isinstance(snapshot, dict) else None,
    )
    low = (work or compact).lower()
    skip_llm = (not work) or (len(work) <= 2 and work in {"!", "?", ".", "ok", "oi"}) or low in {"teste", "test", "ping"}
    if speak and not work:
        reply = deterministic_voice_summary(card)
    else:
        # V26.3-FIX: rota system injeta health real no contexto do LLM e no fallback
        if route == "system" and work and not skip_llm:
            health_ctx = _read_system_health_for_chat()
            enriched = (
                work
                + "\n\n[CONTEXTO SYSTEM_HEALTH]\n"
                + health_ctx
                + "\n[FIM SYSTEM_HEALTH]\nResponda com base nesses dados reais."
            )
            reply = _ask_ollama(enriched, snapshot, route, history, fixture_id=fixture_id)
        else:
            reply = "" if skip_llm else _ask_ollama(work or compact, snapshot, route, history, fixture_id=fixture_id)
        if not reply:
            reply = base.get("reply") or _fallback_general(route)
    base.update({
        "ok": True,
        "reply": reply,
        "speak": speak,
        "voice": {
            "requested": speak,
            "router": ["kanteiro_neural", "voice_8099", "edge_antonio"],
            "default_voice": "pt-BR-AntonioNeural",
        },
        "grounding": {
            "fixture_lock": card.get("fixture_lock"),
            "missing": card.get("missing"),
            "policy": card.get("policy"),
        },
        "model": "AURA Orchestrator + Ollama local" if reply else "AURA Orchestrator",
        "route": route,
        "orchestrator": {
            "active": True,
            "fixture_id": fixture_id,
            "tools": ["system_snapshot", "live_analysis", "risk_status", "paper_summary", "backtest", "voice_health"],
            "data_policy": "observed_context_only",
        },
        # V23: never ship the full raw snapshot in the response path — compact only
        "system_snapshot_compact": _llm_view(snapshot) if isinstance(snapshot, dict) else {},
    })
    return base
