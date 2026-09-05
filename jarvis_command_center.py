#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jarvis_command_center.py — camada de comando do assistente: ACESSO REAL aos
agentes do AURA para MONITORAR e CONTROLAR, por voz ou HTTP.

FRONTEIRA (§0, aplicada em codigo, nao em promessa):
    - O JARVIS opera o LABORATORIO (servicos, feed, alertas, analytics,
      camera, pessoas, a propria voz). NUNCA a carteira.
    - FERRAMENTA NENHUMA toca aposta/stake/execucao: o registro de tool e
      validado contra DENYLIST (nome+descricao) e rejeita na hora.
    - LEITURA e livre. CONTROLE (mudanca de estado) exige confirmacao por
      voz: "rodar analytics" -> "Confirmar? Diga sim." -> "sim" -> executa.
      Pending com TTL de 45s; qualquer outra fala cancela.

ROTEAMENTO HIBRIDO:
    1. Gramatica deterministica PT (regex, acento-insensivel): zero latencia,
       zero alucinacao — a rota obrigatoria para controle.
    2. Roteador LLM opcional (route_via_llm): para frases fora da gramatica;
       uma chamada, menu compacto, saida JSON, confianca minima. Falhou ->
       chat normal. O fluxo de confirmacao vale igual.

INTEGRACAO (hunks na resposta):
    jarvis_voice_server.py: singleton _COMMANDS no startup com deps reais;
    _talk_events consulta ANTES do LLM; endpoints /api/voice/tools,
    /api/voice/command (texto puro, p/ dashboard/extensao/teste) e
    /api/voice/alerts (fila proativa p/ cliente fazer poll).

    Quando engine/boot.py existir: supervisor_jarvis.alert_callback ->
    push_alert() -> cliente drena /api/voice/alerts e fala (o supervisor
    detecta transicao de estado; a voz anuncia).

DEPENDENCIAS: injetadas via dict `deps` — TODAS opcionais; cada tool degrada
    para "componente indisponivel nesta build" (contabilizado em stats).
    HTTP via urllib puro (bridge :8080/health, engine :8765/api/status,
    ollama :11434/api/tags, metrics_url configuravel).

stdlib only. Python 3.9+. Windows compativel. Console ASCII nos checks.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("aura.command_center")

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# fronteira §0: vocabulario proibido em ferramenta
# ---------------------------------------------------------------------------
_DENYLIST = ("aposta", "apostar", "apostas", "bet", "stake", "banca",
             "executar trade", "executar ordem", "saque", "deposito",
             "dinheiro real", "entrada real", "cashout")

_CONFIRM_TTL = 45.0


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c)).lower()


def _http_get_json(url: str, timeout: float = 2.5) -> Optional[Dict[str, Any]]:
    """GET JSON defensivo. None = inalcançavel (nunca levanta)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# centro de comando
# ---------------------------------------------------------------------------
class CommandCenter:
    def __init__(self, deps: Optional[Dict[str, Any]] = None) -> None:
        self._lock = threading.RLock()
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._pending: Dict[str, Dict[str, Any]] = {}  # session -> {tool,args,ts}
        self._deps = dict(deps or {})
        self._alerts: List[Dict[str, Any]] = []
        self._calls = 0
        self._executed = 0
        self._confirmations_asked = 0
        self._confirmations_ok = 0
        self._confirmations_expired = 0
        self._denylist_rejected = 0
        self._llm_routes = 0
        self._last_tool: Optional[str] = None

    # ------------------------------------------------------------ registro
    def register(self, name: str, desc: str, handler: Callable[..., Any],
                 risk: str = "read", args: Optional[Dict[str, str]] = None,
                 confirm: bool = False,
                 confirm_speech_fn: Optional[Callable[[Dict[str, Any]], str]] = None) -> bool:
        """Registra tool. risk: 'read'|'control'. confirm=True exige 'sim'.
        Aplica denylist §0: nome+descricao com vocabulario de aposta ->
        registro REJEITADO (retorna False, contabilizado)."""
        blob = _strip_accents(name + " " + desc)
        if any(tok in blob for tok in _DENYLIST):
            self._denylist_rejected += 1
            logger.error("command_center: tool rejeitada pelo denylist §0: %s", name)
            return False
        if risk not in ("read", "control"):
            risk = "control"
        with self._lock:
            self._tools[name] = {"name": name, "desc": desc, "handler": handler,
                                 "risk": risk, "args": args or {},
                                 "confirm": confirm or risk == "control",
                                 "confirm_speech_fn": confirm_speech_fn}
        return True

    def list_tools(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{"name": t["name"], "desc": t["desc"], "risk": t["risk"],
                     "args": t["args"], "confirm": t["confirm"]}
                    for t in sorted(self._tools.values(), key=lambda x: x["name"])]

    # ------------------------------------------------------------ parser
    _RE_CONFIRM = re.compile(r"^(sim|confirmo|pode|manda|afirmativo|ok|isso)\b")
    _RE_CANCEL = re.compile(r"^(nao|negativo|cancela|cancelar|esquece|deixa)\b")

    def parse(self, utterance: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Gramatica deterministica PT. Devolve (tool, args) ou None."""
        t = _strip_accents(utterance)
        if not t:
            return None
        if re.search(r"\b(ajuda|comandos|o que (voce|vc) (pode|faz|controla))\b", t):
            return ("ajuda", {})
        if re.search(r"\b(briefing|resumo do dia|me atualiza|como esta o sistema)\b", t):
            return ("briefing", {})
        if re.search(r"status.*\b(geral|geral do sistema|tudo|completo)\b|^status$", t):
            return ("status_geral", {})
        m = re.search(r"status (?:do |da )?(bridge|engine|voz|ollama|metrics)", t)
        if m:
            return ("status_servico", {"servico": m.group(1)})
        if re.search(r"\b(feed|linhas de feed|captura)\b.*(status|como|quantas)?|^status do feed$", t):
            return ("feed_health", {})
        if re.search(r"\b(alertas?|alertando|algum problema)\b", t) and not re.search(r"\bcamera\b", t):
            return ("alertas", {})
        if re.search(r"quem (?:esta|ta|eh que esta) (?:aqui|presente|comigo)", t):
            return ("quem_esta_aqui", {})
        if re.search(r"\b(pessoas|gente) (?:registradas|cadastradas|que (voce|vc) conhece)\b", t):
            return ("pessoas_registradas", {})
        if re.search(r"\b(scorecard|decisoes|quantas decisoes)\b", t):
            return ("scorecard", {})
        if re.search(r"\b(green ?light|verde o green|meta ?label\w*)\b", t):
            return ("green_light", {})
        if re.search(r"\b(diagnostico|saude|status)\b.*\b(voz|fala|t t s)\b", t):
            return ("voz_diagnostico", {})
        m = re.search(r"esquecer? (?:a |o )?pessoa ([a-z]+(?: [a-z]+)?)", t)
        if m:
            return ("esquecer_pessoa", {"nome": m.group(1).strip()})
        if re.search(r"\b(rodar|executar|gerar|roda)\b.*\b(analytics|relatorio|analise semanal)\b", t):
            return ("rodar_analytics", {})
        if re.search(r"\b(camera|webcam)\b.*\bstatus\b|status.*\bcamera\b", t):
            return ("camera_status", {})
        if re.search(r"\b(liga|ligar|ativa|ativar|abre|abrir)\b.*\bcamera\b|\bcamera\b.*\b(liga|ligar)\b", t):
            return ("camera_ligar", {})
        if re.search(r"\b(desliga|desligar|pausa|pausar|para|parar|fecha|fechar)\b.*\bcamera\b|\bcamera\b.*\b(desliga|desligar|pausa|pausar)\b", t):
            return ("camera_pausar", {})
        if re.search(r"\b(recarrega|recarregar|reinicia|reiniciar)\b.*\b(voz|fala|voz do jarvis)\b", t):
            return ("recarregar_voz", {})
        return None

    # ------------------------------------------------------------ execucao
    def execute(self, name: str, args: Optional[Dict[str, Any]] = None,
                session: str = "default", confirmed: bool = False
                ) -> Dict[str, Any]:
        """Executa tool com o fluxo de confirmacao. Nunca levanta."""
        args = args or {}
        with self._lock:
            self._calls += 1
            spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "speech": "Ferramenta %s nao existe." % name,
                    "tool": name}
        if spec["confirm"] and not confirmed:
            with self._lock:
                self._pending[session] = {"tool": name, "args": args,
                                          "ts": time.monotonic()}
                self._confirmations_asked += 1
            try:
                prompt = (spec["confirm_speech_fn"](args)
                          if callable(spec.get("confirm_speech_fn"))
                          else "Confirmar: %s? Diga sim para executar." % spec["desc"])
            except Exception:
                prompt = "Confirmar: %s? Diga sim para executar." % spec["desc"]
            return {"ok": True, "tool": name, "awaiting_confirmation": True,
                    "speech": prompt}

        try:
            out = spec["handler"](args, session)
        except Exception as exc:
            logger.exception("command_center: tool %s falhou", name)
            return {"ok": False, "tool": name,
                    "speech": "Falha ao executar %s: %s" % (name, exc)}
        with self._lock:
            self._executed += 1
            self._last_tool = name
        if isinstance(out, dict):
            speech = str(out.get("speech") or out.get("ok") or "Feito.")
            response = {"ok": bool(out.get("ok", True)), "tool": name,
                        "speech": speech, "detail": out}
            # Preserva campos estruturados do handler no nível da resposta
            # para consumidores como Vision (`answer`) e pesquisa (`results`).
            response.update({k: v for k, v in out.items()
                             if k not in ("ok", "tool", "speech")})
            return response
        return {"ok": True, "tool": name, "speech": str(out)}

    def handle_utterance(self, utterance: str, session: str = "default"
                         ) -> Optional[Dict[str, Any]]:
        """Ponto de entrada por voz: pending-words, gramatica, execucao.
        None = nao era comando (caller segue fluxo normal de chat)."""
        text = (utterance or "").strip()
        if not text:
            return None
        with self._lock:
            pend = self._pending.get(session)
        if pend is not None:
            low = _strip_accents(text)
            if time.monotonic() - pend["ts"] > _CONFIRM_TTL:
                with self._lock:
                    self._pending.pop(session, None)
                    self._confirmations_expired += 1
                pend = None
            elif self._RE_CONFIRM.match(low):
                with self._lock:
                    self._pending.pop(session, None)
                    self._confirmations_ok += 1
                return self.execute(pend["tool"], pend["args"],
                                    session, confirmed=True)
            elif self._RE_CANCEL.match(low):
                with self._lock:
                    self._pending.pop(session, None)
                return {"ok": True, "cancelled": True,
                        "speech": "Cancelado."}
            else:
                with self._lock:
                    self._pending.pop(session, None)  # outra fala = desiste
        det = self.parse(text)
        if det is None:
            return None
        name, args = det
        return self.execute(name, args, session)

    # ------------------------------------------------------------ roteador LLM
    def route_via_llm(self, utterance: str,
                      ask_fn: Callable[[str, str], str],
                      session: str = "default",
                      min_confidence: float = 0.6
                      ) -> Optional[Dict[str, Any]]:
        """Fallback estatistico: LLM escolhe tool de um menu compacto.
        Uma chamada; JSON {tool,args,confidence}; abaixo do threshold ou
        ilegivel -> None (chat normal). Confirmacao continua valendo."""
        tools = self.list_tools()
        if not tools:
            return None
        menu = "\n".join("- %s(%s): %s%s" % (
            t["name"], ", ".join(t["args"]), t["desc"],
            " [requer confirmacao]" if t["confirm"] else "") for t in tools)
        prompt = ("Escolha UMA ferramenta para o comando do usuario, ou null. "
                  "Responda APENAS JSON: {\"tool\": nome, \"args\": {}, "
                  "\"confidence\": 0-1}\nFerramentas:\n%s\nComando: %s"
                  % (menu, utterance[:300]))
        try:
            raw = ask_fn(session, prompt)
        except Exception:
            return None
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        conf = data.get("confidence")
        name = data.get("tool")
        if not isinstance(name, str) or not isinstance(conf, (int, float)):
            return None
        if float(conf) < min_confidence:
            return None
        with self._lock:
            if name not in self._tools:
                return None
            self._llm_routes += 1
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        return self.execute(name, args, session)

    # ------------------------------------------------------------ alertas
    def push_alert(self, text: str, severity: str = "warn") -> int:
        """Fonte: supervisor_jarvis.alert_callback (quando boot.py existir),
        ou qualquer componente. Cliente drena via /api/voice/alerts."""
        with self._lock:
            self._alerts.append({"ts": time.time(), "severity": severity,
                                 "text": str(text)[:400]})
            self._alerts = self._alerts[-100:]
            return len(self._alerts)

    def take_alerts(self, max_items: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            out = self._alerts[:max_items]
            self._alerts = self._alerts[max_items:]
            return out

    # ------------------------------------------------------------ estado
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"command_center": {
                "tools": len(self._tools),
                "calls": self._calls, "executed": self._executed,
                "confirmations_asked": self._confirmations_asked,
                "confirmations_ok": self._confirmations_ok,
                "confirmations_expired": self._confirmations_expired,
                "denylist_rejected": self._denylist_rejected,
                "llm_routes": self._llm_routes,
                "pending_sessions": len(self._pending),
                "alerts_queued": len(self._alerts),
                "last_tool": self._last_tool,
            }}


# ---------------------------------------------------------------------------
# ferramentas padrao — TODAS defensivas: sem dep, falam "indisponivel"
# ---------------------------------------------------------------------------
def build_default_tools(cc: CommandCenter,
                        deps: Optional[Dict[str, Any]] = None) -> CommandCenter:
    """Registra o catalogo padrao. deps (todas opcionais):
      people, persona, camera, voice_state_fn, voice_reload_fn,
      analytics_fn (scorecard/green), analytics_run_fn (job),
      urls: {"bridge":..., "engine":..., "ollama":..., "metrics":...}
    """
    d = dict(deps or {})
    urls = d.get("urls") or {}
    bridge_url = urls.get("bridge", "http://127.0.0.1:8080/health")
    engine_url = urls.get("engine", "http://127.0.0.1:8765/api/status")
    ollama_url = urls.get("ollama", "http://127.0.0.1:11434/api/tags")
    metrics_url = urls.get("metrics")  # None = nao monitorado (honesto)

    def _ok(extra: str) -> str:
        return extra

    def _unavailable(what: str) -> str:
        return "%s indisponivel nesta build." % what

    # ---------------- leitura ----------------
    def t_ajuda(args, session):
        names = [t["name"] for t in cc.list_tools()]
        ctrl = [t["name"] for t in cc.list_tools() if t["confirm"]]
        return ("Posso: %s. Comandos de controle pedem confirmacao: %s. "
                "E nada de aposta — eu opero o laboratorio."
                % (", ".join(names[:10]), ", ".join(ctrl)))

    def _svc_line(label: str, data: Optional[Dict[str, Any]],
                  ok_if: Callable[[Dict[str, Any]], bool],
                  detail: Callable[[Dict[str, Any]], str]) -> str:
        if data is None:
            return "%s fora do ar" % label
        return "%s %s" % (label, "ok" if ok_if(data) else "degradado"), \
            detail(data) if False else "%s %s (%s)" % (
                label, "ok" if ok_if(data) else "degradado", detail(data))

    def t_status_geral(args, session):
        parts: List[str] = []
        vs = d.get("voice_state_fn")
        parts.append(("voz %s" % vs()) if callable(vs) else "voz local")
        b = _http_get_json(bridge_url)
        parts.append("bridge %s" % ("ok, %s linhas de feed"
                                    % b.get("feedLines", "?")
                                    if isinstance(b, dict) and b.get("status") in ("ok", None)
                                    and b else "fora do ar"))
        e = _http_get_json(engine_url)
        parts.append("engine %s" % (str(e.get("status", "up"))
                                    if isinstance(e, dict) else "fora do ar"))
        o = _http_get_json(ollama_url)
        parts.append("olama %s" % ("ok" if isinstance(o, dict)
                                   and "models" in o else "fora"))
        pe = d.get("people")
        if pe is not None:
            try:
                n = len(pe.get_present())
                parts.append("%d pessoa%s presente%s" % (n, "" if n == 1 else "s",
                                                         "" if n == 1 else "s"))
            except Exception:
                pass
        cam = d.get("camera")
        if cam is not None:
            try:
                parts.append("camera %s" % ("ligada" if cam.stats()[
                    "camera_watcher"]["running"] else "pausada"))
            except Exception:
                pass
        return "Status: " + "; ".join(parts) + "."

    def t_status_servico(args, session):
        svc = str(args.get("servico", "")).lower()
        if svc == "bridge":
            b = _http_get_json(bridge_url)
            return ("Bridge ok, %s linhas de feed." % b.get("feedLines")
                    if isinstance(b, dict) and b else "Bridge fora do ar.")
        if svc == "engine":
            e = _http_get_json(engine_url)
            return ("Engine %s." % e.get("status", "up")
                    if isinstance(e, dict) else "Engine fora do ar.")
        if svc == "ollama":
            o = _http_get_json(ollama_url)
            if not isinstance(o, dict):
                return "Ollama fora do ar."
            models = [m.get("name") for m in (o.get("models") or [])][:5]
            return "Ollama ok. Modelos: %s." % (", ".join(filter(None, models)) or "nenhum")
        if svc == "metrics":
            if not metrics_url:
                return _unavailable("Servidor de metricas (url nao configurada)")
            m = _http_get_json(metrics_url)
            return ("Metricas ok." if isinstance(m, dict)
                    else "Servidor de metricas fora do ar.")
        if svc == "voz":
            vs = d.get("voice_state_fn")
            return ("Voz: %s." % vs()) if callable(vs) else "Voz: estado local."
        return "Servico desconhecido: %s." % svc

    def t_feed(args, session):
        b = _http_get_json(bridge_url)
        if not isinstance(b, dict):
            return "Bridge fora do ar; sem dado de feed."
        fl = b.get("feedLines", b.get("feed_lines"))
        return ("Feed com %s linhas." % fl if fl is not None
                else "Bridge ok; contador de linhas ausente.")

    def t_alertas(args, session):
        if metrics_url:
            a = _http_get_json(metrics_url.rstrip("/") + "/alerts")
            if isinstance(a, dict):
                firing = a.get("alerts") or a.get("firing") or []
                if not firing:
                    return "Nenhum alerta disparado."
                return ("%d alerta(s): %s." % (
                    len(firing), "; ".join(str(x)[:80] for x in firing[:3])))
        q = cc.take_alerts(5)
        if not q:
            return "Nenhum alerta na fila."
        return "%d alerta(s): %s." % (len(q), "; ".join(x["text"][:80]
                                                        for x in q))

    def t_briefing(args, session):
        parts: List[str] = []
        b = _http_get_json(bridge_url)
        if isinstance(b, dict):
            parts.append("feed com %s linhas" % b.get("feedLines", "?"))
        an = d.get("analytics_fn")
        if callable(an):
            try:
                sc = an().get("scorecard") or {}
                total = sc.get("total")
                if total is not None:
                    parts.append("%s decisoes registradas" % total)
            except Exception:
                pass
        pe = d.get("people")
        if pe is not None:
            try:
                pr = pe.get_present()
                if pr:
                    parts.append("%s presente" % ", ".join(
                        p["name"] for p in pr[:3]))
            except Exception:
                pass
        if not parts:
            return "Poucos dados agora: " + _unavailable("fontes") 
        return "Briefing: " + "; ".join(parts) + ". Bom trabalho por ai."

    def t_quem(args, session):
        pe = d.get("people")
        if pe is None:
            return _unavailable("Memoria de pessoas")
        pr = pe.get_present()
        if not pr:
            return "Ninguem reconhecido por aqui agora."
        return "Presentes: %s." % ", ".join(
            "%s (conf %.2f)" % (p["name"], p["score"]) for p in pr)

    def t_pessoas(args, session):
        pe = d.get("people")
        if pe is None:
            return _unavailable("Memoria de pessoas")
        lst = pe.list_people()
        if not lst:
            return "Nenhuma pessoa registrada ainda."
        return "Registradas: %s." % ", ".join(
            "%s (%d encontros)" % (p["name"], p["times_seen"]) for p in lst)

    def t_scorecard(args, session):
        an = d.get("analytics_fn")
        if not callable(an):
            return _unavailable("Analytics")
        try:
            data = an()
        except Exception:
            logger.exception("scorecard via analytics falhou")
            raise
        sc = data.get("scorecard") or {}
        if not sc:
            return "Sem decisoes registradas ainda."
        total = sc.get("total", 0)
        res = sc.get("resolved", 0)
        top = sc.get("por_decisao") or {}
        maior = max(top.items(), key=lambda kv: kv[1])[0] if top else "?"
        return ("Scorecard: %s decisoes, %s resolvidas. %s lidera o placar — "
                "saudavel, se e HOLD." % (total, res, maior))

    def t_green(args, session):
        an = d.get("analytics_fn")
        if not callable(an):
            return _unavailable("Analytics")
        try:
            data = an()
        except Exception:
            return "Analytics falhou ao consultar."
        g = data.get("green") or {}
        if not g:
            return "Sem dado de green-light ainda."
        feitos = [k for k, v in g.items() if v]
        return ("Green light do meta labeling: %d de %d criterios (%s). "
                "Ativacao segue sendo decisao humana."
                % (len(feitos), len(g), ", ".join(feitos) or "nenhum"))

    def t_voz_diag(args, session):
        vs = d.get("voice_state_fn")
        if not callable(vs):
            return _unavailable("Diagnostico de voz")
        return "Diagnostico: %s." % vs()

    def t_cam_status(args, session):
        cam = d.get("camera")
        if cam is None:
            return _unavailable("Camera")
        st = cam.stats().get("camera_watcher", {})
        return ("Camera %s, %d frames amostrados."
                % ("ligada" if st.get("running") else "pausada",
                   st.get("frames", 0)))

    # ---------------- controle (confirmacao conforme regra) ----------------
    def t_cam_on(args, session):
        cam = d.get("camera")
        if cam is None:
            return _unavailable("Camera")
        return "Camera ligada." if cam.start() else "Nao consegui abrir a camera."

    def t_cam_off(args, session):
        cam = d.get("camera")
        if cam is None:
            return _unavailable("Camera")
        cam.stop()
        return "Camera pausada."

    def t_reload_voz(args, session):
        fn = d.get("voice_reload_fn")
        if not callable(fn):
            return _unavailable("Recarga de voz")
        fn()
        return "Recarregando motores de voz."

    def t_esquecer(args, session):
        pe = d.get("people")
        if pe is None:
            return _unavailable("Memoria de pessoas")
        nome = str(args.get("nome", "")).strip()
        if not nome:
            return "Diga o nome da pessoa para esquecer."
        ok = pe.forget(nome.title())
        return ("Esqueci %s — arquivos apagados." % nome.title() if ok
                else "Nao encontrei %s." % nome.title())

    def t_rodar_analytics(args, session):
        fn = d.get("analytics_run_fn")
        if not callable(fn):
            return _unavailable("Job de analytics")
        try:
            out = fn()
        except Exception:
            logger.exception("job de analytics falhou")
            return "O job de analytics falhou; veja o log."
        return ("Analytics atualizado: %s frames, %s decisoes, %s outcomes. "
                "Relatorio no disco." % (out.get("frames", "?"),
                                         out.get("decisions", "?"),
                                         out.get("outcomes", "?")))

    cc.register("ajuda", "listar o que eu posso fazer", t_ajuda, "read")
    cc.register("briefing", "resumo do estado do sistema", t_briefing, "read")
    cc.register("status_geral", "status de todos os servicos", t_status_geral, "read")
    cc.register("status_servico", "status de um servico", t_status_servico, "read",
                args={"servico": "bridge|engine|voz|ollama|metrics"})
    cc.register("feed_health", "linhas e saude do feed", t_feed, "read")
    cc.register("alertas", "alertas disparados ou na fila", t_alertas, "read")
    cc.register("quem_esta_aqui", "pessoas reconhecidas agora", t_quem, "read")
    cc.register("pessoas_registradas", "pessoas na memoria", t_pessoas, "read")
    cc.register("scorecard", "placar de decisoes do sistema", t_scorecard, "read")
    cc.register("green_light", "progresso dos criterios do meta labeling",
                t_green, "read")
    cc.register("voz_diagnostico", "saude da propria voz", t_voz_diag, "read")
    cc.register("camera_status", "estado da camera", t_cam_status, "read")
    # controle: camera e reload sao reversiveis e locais -> sem confirm extra
    cc.register("camera_ligar", "ligar a camera de reconhecimento",
                t_cam_on, "control", confirm=False)
    cc.register("camera_pausar", "pausar a camera",
                t_cam_off, "control", confirm=False)
    cc.register("recarregar_voz", "recarregar motores de voz",
                t_reload_voz, "control", confirm=False)
    # destrutivo/pesado -> confirmacao obrigatoria
    cc.register("esquecer_pessoa", "apagar pessoa da memoria (biometria)",
                t_esquecer, "control", args={"nome": "nome da pessoa"})
    cc.register("rodar_analytics", "executar o job semanal de analytics agora",
                t_rodar_analytics, "control")
    return cc


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
        if not cond:
            fails.append(name)

    cc = CommandCenter()
    build_default_tools(cc, deps={})  # sem deps nenhum: tudo degrada

    # parser deterministico
    cases = [
        ("status geral", "status_geral"),
        ("qual o status do feed?", "feed_health"),
        ("me dá um briefing", "briefing"),
        ("quem está aqui comigo?", "quem_esta_aqui"),
        ("como está o scorecard de decisões", "scorecard"),
        ("já deu verde o green light?", "green_light"),
        ("quero o diagnóstico da voz", "voz_diagnostico"),
        ("pode rodar o analytics agora?", "rodar_analytics"),
        ("liga a câmera pra mim", "camera_ligar"),
        ("esquece a pessoa João", "esquecer_pessoa"),
        ("ajuda, o que você控制a".replace("控制", "controla"), "ajuda"),
    ]
    for phrase, expected in cases:
        got = cc.parse(phrase)
        check("parser: '%s' -> %s" % (phrase, expected),
              got is not None and got[0] == expected,
              "got=%s" % (got[0] if got else None))
    check("parser: conversa comum nao e comando",
          cc.parse("e o jogo do benfica, ta pressionando?") is None)

    # acento-insensivel
    check("parser: acentos normalizados",
          cc.parse("quem ta aqui")[0] == "quem_esta_aqui")

    # degrade sem deps
    r = cc.execute("quem_esta_aqui", {}, "s1")
    check("degrade: pessoas indisponivel falado",
          r["ok"] is True and "indisponivel" in r["speech"])
    r = cc.execute("feed_health", {}, "s1")
    check("degrade: bridge fora falado", "fora do ar" in r["speech"])

    # confirmacao: rodar_analytics
    r1 = cc.execute("rodar_analytics", {}, "s2")
    check("confirmacao pedida", r1.get("awaiting_confirmation") is True
          and "Confirmar" in r1["speech"])
    r2 = cc.handle_utterance("sim", "s2")
    check("'sim' executa o pendente",
          r2 is not None and r2.get("tool") == "rodar_analytics"
          and "indisponivel" in r2["speech"])  # sem deps -> falado, mas executou
    r3 = cc.execute("rodar_analytics", {}, "s3")
    r4 = cc.handle_utterance("não, cancela", "s3")
    check("'nao' cancela", r4 is not None and r4.get("cancelled") is True)
    r5 = cc.execute("rodar_analytics", {}, "s4")
    r6 = cc.handle_utterance("outra coisa qualquer", "s4")
    check("fala diferente dissolve o pendente", r6 is None
          or r6.get("awaiting_confirmation") is None)

    # TTL
    r7 = cc.execute("rodar_analytics", {}, "s5")
    cc._pending["s5"]["ts"] -= 100.0
    r8 = cc.handle_utterance("sim", "s5")
    check("TTL expira: 'sim' nao executa pendente velho",
          r8 is None or r8.get("tool") != "rodar_analytics")

    # denylist §0
    ok = cc.register("fazer_aposta", "envia aposta real na bet365",
                     lambda a, s: "x", "control")
    check("denylist rejeita tool de aposta", ok is False)
    st = cc.stats()["command_center"]
    check("denylist contabilizada", st["denylist_rejected"] == 1)

    # deps reais: analytics fake + people fake
    class FakePeople:
        def get_present(self):
            return [{"name": "Hálem", "score": 0.91, "via": "face",
                     "relation": "dono", "times_seen": 7,
                     "minutes_ago": 0.1, "first_time_today": False}]
        def list_people(self):
            return [{"name": "Hálem", "times_seen": 7}]

    def fake_analytics():
        return {"scorecard": {"total": 214, "resolved": 190,
                              "por_decisao": {"HOLD": 160, "ENTER": 21}},
                "green": {"resolutions": True, "coverage": False,
                          "drops": False, "p99": False}}

    cc2 = CommandCenter()
    build_default_tools(cc2, deps={"people": FakePeople(),
                                   "analytics_fn": fake_analytics})
    r = cc2.execute("quem_esta_aqui", {}, "s")
    check("people real: nome e confianca na fala",
          "Hálem" in r["speech"] and "0.91" in r["speech"])
    r = cc2.execute("scorecard", {}, "s")
    check("scorecard real: numeros e HOLD dominante",
          "214" in r["speech"] and "HOLD" in r["speech"])
    r = cc2.execute("green_light", {}, "s")
    check("green real: 1 de 4 e decisao humana",
          "1 de 4" in r["speech"] and "humana" in r["speech"])

    # roteador LLM com fake ask_fn
    def fake_ask(session, prompt):
        return '{"tool": "scorecard", "args": {}, "confidence": 0.9}'
    r = cc2.route_via_llm("me mostra como o sistema ta decidindo", fake_ask)
    check("roteador LLM escolhe tool", r is not None
          and r.get("tool") == "scorecard")
    def bad_ask(session, prompt):
        return "acho que não sei, vamos conversar"
    check("roteador LLM: lixo -> None",
          cc2.route_via_llm("xyz", bad_ask) is None)
    def low_ask(session, prompt):
        return '{"tool": "scorecard", "args": {}, "confidence": 0.3}'
    check("roteador LLM: confianca baixa -> None",
          cc2.route_via_llm("xyz", low_ask) is None)

    # alertas
    cc2.push_alert("feed silencioso por 60s", "warn")
    got = cc2.take_alerts()
    check("fila de alertas drena", len(got) == 1
          and "feed silencioso" in got[0]["text"])
    check("fila vazia apos drain", cc2.take_alerts() == [])

    st2 = cc2.stats()["command_center"]
    check("stats coerente", st2["tools"] >= 17 and st2["executed"] >= 3)

    # handlers nunca levantam
    cc3 = CommandCenter()
    build_default_tools(cc3, deps={"analytics_fn": lambda: (_ for _ in ()).throw(
        RuntimeError("boom"))})
    r = cc3.execute("scorecard", {}, "s")
    check("handler que explode vira fala de falha",
          r["ok"] is False and "Falha" in r["speech"])

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - jarvis_command_center.py")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
