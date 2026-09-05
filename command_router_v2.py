#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
command_router_v2.py — roteador inteligente com CACHE, CONTEXTO de sessao
e PREFETCH. Substitui a chamada direta handle_utterance/route_via_llm do
telegram_employee e do voice server.

MELHORIAS AGILIDADE:
    1. CACHE de roteamento: hash(normalizado(frase)) -> (tool, args) com
       TTL 10 min. Frase repetida = rota instantanea, zero LLM.
    2. CONTEXTO: janela das ultimas 3 interacoes resolve elipses ("e
       agora?", "retoma", "de novo") sem LLM quando o alvo e obvio.
    3. PREFETCH de briefing: padroes de cumprimento disparam status_geral
       em paralelo — quando o usuario terminar de falar, o dado ja chegou.

    Cache e contexto NUNCA autorizam sozinhos: o resultado de cache segue
    pelo MESMO fluxo de confirmacao do CommandCenter (plano + sim). O cache
    lembra a ROTA, nao a permissao.

INTEGRACAO: hunks na resposta. Fallback transparente: sem LLM fn, o router
v2 ainda faz parser deterministico + cache + contexto.

stdlib only. Python 3.9+. Console ASCII.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import unicodedata
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("aura.router_v2")

__version__ = "1.0.0"
CACHE_TTL = 600.0
MAX_CTX = 3

GREETING_RE = None  # compilado abaixo (precisa de re)
import re  # noqa: E402
GREETING_RE = re.compile(
    r"^(bom dia|boa tarde|boa noite|oi|ola|e ai|acorda|bora|tudo bom)\b")


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    # Pontuação não deve criar chaves de cache nem quebrar elipses.
    t = re.sub(r"[^\w\s]+", " ", t, flags=re.UNICODE)
    return " ".join(t.split())


class CommandRouterV2:
    """Parser deterministico + cache + contexto + LLM fallback (opcional)."""

    def __init__(self, cc: Any,
                 ask_fn: Optional[Callable[[str, str], str]] = None,
                 cache_ttl: float = CACHE_TTL,
                 prefetch_fn: Optional[Callable[[], None]] = None):
        self._cc = cc
        self._ask = ask_fn
        self._ttl = float(cache_ttl)
        self._prefetch = prefetch_fn
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, str, Dict[str, Any]]] = {}
        self._ctx: Dict[str, deque] = {}
        self.stats = {"handled": 0, "cache_hits": 0, "ctx_resolved": 0,
                      "llm_routes": 0, "prefetches": 0, "unhandled": 0,
                      "errors": 0}

    # ------------------------------------------------------------ cache
    def _cache_key(self, text: str, session: str) -> str:
        return hashlib.sha256(("%s|%s" % (session, _norm(text)))
                              .encode("utf-8")).hexdigest()[:32]

    def _cache_get(self, key: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        with self._lock:
            hit = self._cache.get(key)
            if not hit:
                return None
            ts, tool, args = hit
            if time.time() - ts > self._ttl:
                self._cache.pop(key, None)
                return None
            return tool, args

    def _cache_put(self, key: str, tool: str, args: Dict[str, Any]) -> None:
        with self._lock:
            if len(self._cache) > 500:
                oldest = sorted(self._cache.items(),
                                key=lambda kv: kv[1][0])[:100]
                for k, _v in oldest:
                    self._cache.pop(k, None)
            self._cache[key] = (time.time(), tool, args)

    # ------------------------------------------------------------ contexto
    def _ctx_push(self, session: str, tool: str) -> None:
        q = self._ctx.setdefault(session, deque(maxlen=MAX_CTX))
        q.append((tool, time.time()))

    def _ctx_last_tool(self, session: str) -> Optional[str]:
        q = self._ctx.get(session)
        if not q:
            return None
        tool, ts = q[-1]
        if time.time() - ts > 1800:  # contexto expira em 30 min
            return None
        return tool

    def _resolve_ellipsis(self, text: str, session: str
                          ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Elipses obvias sobre a ultima tool."""
        t = _norm(text)
        last = self._ctx_last_tool(session)
        if not last:
            return None
        if t in ("e agora", "status", "como esta", "atualiza"):
            if last in ("pausar_aura", "retomar_aura"):
                return ("status_geral", {})
        if t in ("desfaz", "volta", "cancela isso"):
            if last == "pausar_aura":
                return ("retomar_aura", {})
            if last == "retomar_aura":
                return ("pausar_aura", {})
        if t == "de novo" or t == "repete":
            return (last, {})  # roteia de novo; confirmacao segue valendo
        return None

    # ------------------------------------------------------------ rota
    def route(self, text: str, session: str = "default"
              ) -> Optional[Dict[str, Any]]:
        """Devolve o resultado do CommandCenter ou None (nao era comando).
        Ordem: greeting+prefetch -> cache -> parser det. -> ellipsis ->
        LLM fallback. Confirmacoes ('sim') SEMPRE direto ao CommandCenter
        (pending fica la)."""
        self.stats["handled"] += 1
        t = _norm(text)
        if not t:
            return None

        # pending do CommandCenter tem prioridade absoluta (confirmacoes)
        if self._cc is not None:
            try:
                if t in ("sim", "confirmo", "pode", "manda", "afirmativo",
                         "ok", "isso", "nao", "negativo", "cancela",
                         "cancelar", "esquece", "deixa"):
                    return self._cc.handle_utterance(text, session)
            except Exception:
                self.stats["errors"] += 1
                logger.exception("router v2: pending check falhou")

        # greeting + prefetch (agilidade)
        if GREETING_RE.match(t) and self._prefetch is not None:
            try:
                self._prefetch()
                self.stats["prefetches"] += 1
            except Exception:
                logger.exception("router v2: prefetch falhou")

        # cache
        key = self._cache_key(text, session)
        cached = self._cache_get(key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            tool, args = cached
            self._ctx_push(session, tool)
            return self._cc.execute(tool, args, session)

        # parser deterministico do CommandCenter
        if self._cc is not None:
            try:
                det = self._cc.parse(text)
            except Exception:
                det = None
            if det is not None:
                tool, args = det
                self._cache_put(key, tool, dict(args))
                self._ctx_push(session, tool)
                return self._cc.execute(tool, args, session)

        # elipse sobre o contexto
        ell = self._resolve_ellipsis(text, session)
        if ell is not None:
            self.stats["ctx_resolved"] += 1
            tool, args = ell
            return self._cc.execute(tool, args, session)

        # fallback LLM (uma chamada)
        if self._ask is not None and self._cc is not None:
            try:
                res = self._cc.route_via_llm(text, self._ask, session)
            except Exception:
                res = None
            if res is not None and res.get("tool"):
                self._cache_put(key, res["tool"], {})
                self._ctx_push(session, res["tool"])
                self.stats["llm_routes"] += 1
                return res
        self.stats["unhandled"] += 1
        return None

    def stats_dict(self) -> dict:
        with self._lock:
            return {"command_router_v2": {
                **self.stats, "cache_size": len(self._cache)}}


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test() -> int:
    fails: List[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
        if not cond:
            fails.append(name)

    class FakeCC:
        def __init__(self):
            self.executed: List[Tuple[str, dict, str]] = []
            self.pending_session = None
            self.pending_tool = None

        def parse(self, text):
            t = _norm(text)
            if "status geral" in t:
                return ("status_geral", {})
            if "pausa o aura" in t:
                return ("pausar_aura", {})
            if "retoma" in t:
                return ("retomar_aura", {})
            return None

        def execute(self, tool, args, session):
            self.executed.append((tool, dict(args), session))
            return {"ok": True, "tool": tool, "speech": "ran %s" % tool}

        def handle_utterance(self, text, session):
            if _norm(text) in ("sim",) and self.pending_tool:
                self.executed.append((self.pending_tool, {},
                                      session + ":confirm"))
                out = {"ok": True, "tool": self.pending_tool,
                       "speech": "confirmed"}
                self.pending_tool = None
                return out
            return None

        def route_via_llm(self, text, ask, session):
            return None  # desligado no teste deterministico

    cc = FakeCC()
    prefetched: List[str] = []

    r = CommandRouterV2(cc, ask_fn=None,
                        prefetch_fn=lambda: prefetched.append("briefing"))

    # deterministico
    out = r.route("status geral", "u1")
    check("det: status geral roteia", out is not None
          and out.get("tool") == "status_geral")
    # cache: segunda vez nao re-parseia (conta hit)
    out = r.route("status geral!", "u1")
    check("cache: hit na frase quase igual (normalizada)",
          out is not None and r.stats["cache_hits"] == 1)

    # confirmacao vai direto ao pending do CC
    cc.pending_tool = "rodar_analytics"
    out = r.route("sim", "u1")
    check("confirm: 'sim' chega ao pending do CC", out is not None
          and out.get("speech") == "confirmed")

    # elipse: pausa -> "e agora?" -> status
    r.route("pausa o aura", "u2")
    out = r.route("e agora?", "u2")
    check("ctx: elipse pos-pausa vira status",
          out is not None and out.get("tool") == "status_geral")
    out = r.route("desfaz", "u2")
    check("ctx: desfaz pausa -> retoma",
          out is not None and out.get("tool") == "retomar_aura")

    # greeting dispara prefetch
    r.route("bom dia", "u3")
    check("prefetch: cumprimento disparou briefing",
          prefetched == ["briefing"])

    # LLM fallback com ask_fn fake
    class FakeCC2(FakeCC):
        def parse(self, text):
            return None  # força LLM

        def route_via_llm(self, text, ask, session):
            return {"ok": True, "tool": "briefing", "speech": "via llm"}

    cc2 = FakeCC2()
    r2 = CommandRouterV2(cc2, ask_fn=lambda s, p: "x")
    out = r2.route("me atualiza do sistema", "u9")
    check("llm: fallback roteia quando parser falha",
          out is not None and out.get("tool") == "briefing")
    out = r2.route("me atualiza do sistema", "u9")
    check("llm: resultado entra no cache",
          r2.stats["cache_hits"] == 1)

    # contexto expira
    r3 = CommandRouterV2(FakeCC())
    r3._ctx["u4"] = deque([("pausar_aura", time.time() - 4000)])
    check("ctx: expira apos 30 min",
          r3._resolve_ellipsis("desfaz", "u4") is None)

    st = r.stats_dict()["command_router_v2"]
    check("stats: coerente", st["handled"] >= 6 and st["cache_hits"] >= 1)

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - command_router_v2.py")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
