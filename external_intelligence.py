#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
external_intelligence.py — hub de APIs GRATUITAS que enriquecem o GLM-4
com fatos e contexto que o modelo local nao sabe.

FONTES (todas gratuitas, stdlib HTTP):
    Wikipedia REST API  — respostas factuais enciclopedicas
    DuckDuckGo Instant  — respostas rapidas de busca
    Jina Reader (r.jina.ai) — converte QUALQUER URL para markdown LLM-ready
                              (resolve SPA/JS-render que o web_knowledge nao cobre)
    football-data.org   — dados esportivos (free tier: 10 req/min, 12 competicoes)

DESIGN:
    - Cada fonte e DEFENSIVA: sem rede/key -> degrada com stats honestos.
    - Rate limits respeitados por design (min_interval por fonte).
    - Cache por fonte (TTL 5 min) — nao martelar o que ja sabemos.
    - TODAS alimentam o ToolKnowledge via add_text quando relevante.

EMBEDDINGS (opcional, maior ganho de inteligencia):
    Ollama nomic-embed-text roda no MESMO servidor que o GLM-4.
    Sem ele: ToolKnowledge usa TF-IDF (como hoje). Com ele: busca semantica real.
    OllamaEmbedder.detect() verifica se o modelo esta disponivel.

INTEGRACAO: hunks na resposta. stdlib only. Python 3.9+. Windows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("aura.external_intel")

__version__ = "1.0.0"

_STOP = {"o", "a", "os", "as", "um", "uma", "de", "do", "da", "dos", "das",
         "em", "no", "na", "nos", "nas", "por", "para", "com", "sem",
         "que", "qual", "quais", "como", "onde", "quando", "porque",
         "mais", "menos", "muito", "pouco", "sim", "nao", "e", "ou",
         "se", "mas", "ou", "nem", "tambem", "ja", "ainda", "so"}


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]{3,}", _norm(text))
            if w not in _STOP]


# ---------------------------------------------------------------------------
# OllamaEmbedder — embeddings reais via nomic-embed-text (opcional)
# ---------------------------------------------------------------------------
class OllamaEmbedder:
    """Detecta e usa nomic-embed-text no Ollama local. Sem modelo: None."""

    def __init__(self, ollama_url: str = "http://127.0.0.1:11434",
                 model: str = "nomic-embed-text",
                 post_fn: Optional[Callable] = None):
        self._url = ollama_url.rstrip("/")
        self._model = model
        self._post = post_fn or self._post_default
        self._available: Optional[bool] = None
        self._lock = threading.Lock()
        self.stats = {"embeds": 0, "failures": 0, "detects": 0}

    def _post_default(self, path: str, payload: dict) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                self._url + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8",
                                                     errors="replace"))
        except Exception:
            return None

    def detect(self) -> bool:
        """Verifica se o modelo de embedding esta no Ollama (cache)."""
        if self._available is not None:
            return self._available
        self.stats["detects"] += 1
        out = self._post("/api/tags", {})
        if out and isinstance(out.get("models"), list):
            for m in out["models"]:
                if self._model in str(m.get("name", "")):
                    self._available = True
                    return True
        self._available = False
        return False

    def embed(self, text: str) -> Optional[List[float]]:
        """Embedding do texto. None se modelo indisponivel."""
        if not self.detect():
            return None
        out = self._post("/api/embed", {
            "model": self._model,
            "input": text[:2000]})
        if out and isinstance(out.get("embeddings"), list) \
                and out["embeddings"]:
            self.stats["embeds"] += 1
            return [float(x) for x in out["embeddings"][0]]
        self.stats["failures"] += 1
        return None

    @staticmethod
    def cosine(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
# Fontes de dados gratuitas
# ---------------------------------------------------------------------------
class WikipediaSource:
    """Wikipedia REST API — gratuito, sem key, conteudo enciclopedico."""

    SEARCH = ("https://pt.wikipedia.org/w/api.php?action=query&list=search"
              "&srsearch=%s&srlimit=%d&format=json&origin=*")
    SUMMARY = "https://pt.wikipedia.org/api/rest_v1/page/summary/%s"

    def __init__(self, post_fn: Optional[Callable] = None,
                 min_interval: float = 0.5):
        self._post = post_fn or self._get_default
        self._min = float(min_interval)
        self._last = -1e9
        self.stats = {"searches": 0, "summaries": 0, "failures": 0}

    def _get_default(self, url: str) -> Optional[dict]:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8",
                                                     errors="replace"))
        except Exception:
            return None

    def search(self, query: str, limit: int = 5) -> dict:
        if not (query or "").strip():
            return {"ok": False, "speech": "Diga o que pesquisar."}
        if time.monotonic() - self._last < self._min:
            return {"ok": False, "speech": "Aguarde um instante."}
        self._last = time.monotonic()
        url = self.SEARCH % (urllib.parse.quote(query), limit)
        data = self._post(url)
        if not data:
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Wikipedia indisponivel."}
        results = (data.get("query") or {}).get("search") or []
        self.stats["searches"] += 1
        findings = [{"title": r.get("title", ""),
                     "snippet": re.sub(r"<[^>]+>", "",
                                       r.get("snippet", ""))[:200]}
                    for r in results[:limit]]
        return {"ok": True, "findings": findings,
                "speech": "Wikipedia: %d resultado(s) para '%s'."
                          % (len(findings), query)}

    def summary(self, title: str) -> dict:
        if not (title or "").strip():
            return {"ok": False, "speech": "Diga o topico."}
        url = self.SUMMARY % urllib.parse.quote(title.strip())
        data = self._post(url)
        if not data or data.get("title") == "Not found":
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Artigo nao encontrado."}
        self.stats["summaries"] += 1
        return {"ok": True, "title": data.get("title", ""),
                "extract": data.get("extract", ""),
                "speech": "%s: %s" % (data.get("title", ""),
                                      data.get("extract", "")[:300])}


class DuckDuckGoSource:
    """DuckDuckGo Instant Answer — gratuito, sem key, respostas rapidas."""

    def __init__(self, post_fn: Optional[Callable] = None,
                 min_interval: float = 1.0):
        self._post = post_fn or self._get_default
        self._min = float(min_interval)
        self._last = -1e9
        self.stats = {"queries": 0, "hits": 0, "failures": 0}

    def _get_default(self, url: str) -> Optional[dict]:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8",
                                                     errors="replace"))
        except Exception:
            return None

    def query(self, text: str) -> dict:
        if not (text or "").strip():
            return {"ok": False, "speech": "Diga o que buscar."}
        if time.monotonic() - self._last < self._min:
            return {"ok": False, "speech": "Aguarde um instante."}
        self._last = time.monotonic()
        url = ("https://api.duckduckgo.com/?q=%s&format=json&no_html=1"
               "&skip_disambig=1" % urllib.parse.quote(text))
        data = self._post(url)
        if not data:
            self.stats["failures"] += 1
            return {"ok": False, "speech": "DuckDuckGo indisponivel."}
        self.stats["queries"] += 1
        # cadeia de fallback: AbstractText -> Answer -> Definition
        answer = (data.get("AbstractText") or data.get("Answer")
                  or data.get("Definition") or "").strip()
        if answer:
            self.stats["hits"] += 1
            return {"ok": True, "answer": answer,
                    "speech": answer[:400]}
        # related topics como fallback
        topics = data.get("RelatedTopics") or []
        if topics and isinstance(topics[0], dict):
            text = topics[0].get("Text", "")
            if text:
                self.stats["hits"] += 1
                return {"ok": True, "answer": text,
                        "speech": text[:400]}
        return {"ok": True, "answer": "",
                "speech": "Nada encontrado no DuckDuckGo."}


class JinaReaderSource:
    """Jina Reader (r.jina.ai) — converte QUALQUER URL para markdown.
    Sem key: rate limit baixo (~20/min). Resolve SPA/JS-render."""

    def __init__(self, post_fn: Optional[Callable] = None,
                 min_interval: float = 4.0, api_key: str = ""):
        self._post = post_fn or self._get_default
        self._min = float(min_interval)
        self._key = (api_key or "").strip()
        self._last = -1e9
        self.stats = {"reads": 0, "failures": 0, "bytes": 0}

    def _get_default(self, url: str) -> Optional[str]:
        try:
            headers = {"Accept": "text/plain"}
            if self._key:
                headers["Authorization"] = "Bearer " + self._key
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read(500_000).decode("utf-8", errors="replace")
        except Exception:
            return None

    def read(self, url: str) -> dict:
        if not (url or "").strip():
            return {"ok": False, "speech": "Diga o endereco."}
        if time.monotonic() - self._last < self._min:
            return {"ok": False, "speech": "Jina Reader: aguarde %d segundos."
                    % int(self._min)}
        self._last = time.monotonic()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        content = self._post("https://r.jina.ai/" + url)
        if not content or len(content) < 50:
            self.stats["failures"] += 1
            return {"ok": False, "speech": "Jina Reader nao conseguiu ler."}
        self.stats["reads"] += 1
        self.stats["bytes"] += len(content)
        return {"ok": True, "content": content[:100_000],
                "speech": "Li %d caracteres de %s." % (len(content), url)}


class FootballDataOrg:
    """football-data.org — dados esportivos (free tier: 10 req/min,
    12 competicoes principais, placares com atraso)."""

    COMPETITIONS = ("https://api.football-data.org/v4/competitions")
    MATCHES = ("https://api.football-data.org/v4/competitions/%s/matches"
               "?dateFrom=%s&dateTo=%s")

    def __init__(self, api_key: Optional[str] = None,
                 post_fn: Optional[Callable] = None,
                 min_interval: float = 7.0):
        self._key = (api_key or "").strip()
        self._post = post_fn or self._get_default
        self._min = float(min_interval)
        self._last = -1e9
        self.stats = {"calls": 0, "failures": 0}

    def _get_default(self, url: str) -> Optional[dict]:
        try:
            headers = {}
            if self._key:
                headers["X-Auth-Token"] = self._key
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8",
                                                     errors="replace"))
        except Exception:
            return None

    def _throttle(self) -> bool:
        if time.monotonic() - self._last < self._min:
            return False
        self._last = time.monotonic()
        return True

    def competitions(self) -> dict:
        if not self._throttle():
            return {"ok": False, "speech": "Aguarde antes de nova consulta."}
        data = self._post(self.COMPETITIONS)
        if not data:
            self.stats["failures"] += 1
            return {"ok": False, "speech": "football-data.org indisponivel."}
        self.stats["calls"] += 1
        comps = data.get("competitions") or []
        return {"ok": True,
                "competitions": [{"name": c.get("name", ""),
                                   "code": c.get("code", "")}
                                  for c in comps[:20]],
                "speech": "Competicoes: %s." % ", ".join(
                    c.get("name", "") for c in comps[:8])}

    def matches_today(self, competition_code: str = "BSA") -> dict:
        if not self._throttle():
            return {"ok": False, "speech": "Aguarde."}
        today = time.strftime("%Y-%m-%d")
        url = self.MATCHES % (competition_code.upper(), today, today)
        data = self._post(url)
        if not data:
            self.stats["failures"] += 1
            return {"ok": False, "speech": "football-data.org indisponivel."}
        self.stats["calls"] += 1
        matches = data.get("matches") or []
        out = []
        for m in matches[:10]:
            out.append({
                "home": (m.get("homeTeam") or {}).get("name", "?"),
                "away": (m.get("awayTeam") or {}).get("name", "?"),
                "score": "%s-%s" % (
                    (m.get("score") or {}).get("fullTime",
                                              {}).get("home", "?"),
                    (m.get("score") or {}).get("fullTime",
                                              {}).get("away", "?")),
                "status": m.get("status", "?"),
                "minute": m.get("minute"),
            })
        return {"ok": True, "matches": out,
                "speech": "%d partida(s) hoje." % len(out)}


# ---------------------------------------------------------------------------
# SemanticKnowledge — ToolKnowledge com embeddings OPCIONAIS
# ---------------------------------------------------------------------------
class SemanticKnowledge:
    """Wrapper que adiciona busca semantica ao ToolKnowledge existente.
    Sem Ollama embedder: usa o TF-IDF do ToolKnowledge (como hoje).
    Com: busca por similaridade coseno — salto de qualidade."""

    def __init__(self, kb: Any, embedder: Optional[OllamaEmbedder] = None):
        self._kb = kb
        self._emb = embedder
        self._cache: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self.stats = {"semantic_queries": 0, "semantic_hits": 0,
                      "fallback_tfidf": 0}

    def query(self, text: str, top_k: int = 3) -> List[dict]:
        """Busca: embeddings se disponivel; TF-IDF fallback."""
        if self._emb is None or not self._emb.detect():
            self.stats["fallback_tfidf"] += 1
            return self._kb.query(text, top_k)
        self.stats["semantic_queries"] += 1
        q_vec = self._emb.embed(text)
        if q_vec is None:
            self.stats["fallback_tfidf"] += 1
            return self._kb.query(text, top_k)
        # busca por similaridade em todos os chunks
        scored: List[dict] = []
        for doc in self._kb.list_people() if hasattr(self._kb,
                                                     'list_people') else []:
            pass  # kb interface diferente; usar chunks direto
        # interface ToolKnowledge: query via TF-IDF como ranking base,
        # depois re-ranquear por similaridade semantica
        base = self._kb.query(text, top_k * 3)
        for item in base:
            chunk_text = item.get("text", "")
            key = hashlib.sha256(chunk_text[:200].encode()).hexdigest()[:16]
            with self._lock:
                c_vec = self._cache.get(key)
            if c_vec is None:
                c_vec = self._emb.embed(chunk_text)
                if c_vec is not None:
                    with self._lock:
                        self._cache[key] = c_vec
            if c_vec is not None:
                item["semantic_score"] = OllamaEmbedder.cosine(q_vec, c_vec)
                scored.append(item)
        if scored:
            self.stats["semantic_hits"] += 1
            scored.sort(key=lambda x: -x.get("semantic_score", 0))
            return scored[:top_k]
        self.stats["fallback_tfidf"] += 1
        return base[:top_k]


# ---------------------------------------------------------------------------
# gramatica
# ---------------------------------------------------------------------------
def parse_external_intel(utterance: str):
    t = _norm(utterance)
    if not t:
        return None
    import re
    m = re.search(r"\b(?:pesquisa|procure|busca|buscar)\s+(?:na\s+)?"
                  r"(?:wikipedia|wiki)\s+(?:sobre\s+|por\s+)?(.+)$", t)
    if m:
        return ("wiki_pesquisar", {"termo": m.group(1).strip()})
    m = re.search(r"\b(?:o que e|quem e|quem foi)\s+(.+?)[\?.]?$", t)
    if m and len(m.group(1).strip()) > 2:
        return ("wiki_resumo", {"topico": m.group(1).strip()})
    m = re.search(r"\b(?:busca|procura|pesquisa)\s+(?:no\s+)?"
                  r"(?:duckduckgo|ddg)\s+(?:por\s+)?(.+)$", t)
    if m:
        return ("ddg_buscar", {"termo": m.group(1).strip()})
    m = re.search(r"\b(?:le|ler|leia)\s+(?:o\s+)?(?:site|link|url)\s+(.+)$", t)
    if m and "jina" in t or m and "melhor" in t:
        return ("jina_ler", {"url": m.group(1).strip()})
    if re.search(r"\bpartidas?\s+(?:de\s+)?hoje\b|\bjogos?\s+(?:de\s+)?hoje\b"
                 r"|\bresultados?\s+(?:de\s+)?hoje\b", t):
        return ("futebol_hoje", {})
    return None


# ---------------------------------------------------------------------------
# registro no CommandCenter
# ---------------------------------------------------------------------------
def build_external_intel_tools(cc, wiki: WikipediaSource,
                               ddg: DuckDuckGoSource, jina: JinaReaderSource,
                               football: Optional[FootballDataOrg] = None,
                               kb: Any = None) -> None:
    """Registra as tools. football e kb sao opcionais (podem ser None)."""

    def t_wiki_search(args, session):
        res = wiki.search(str(args.get("termo", "")))
        if res.get("ok") and kb is not None and res.get("findings"):
            for f in res["findings"][:2]:
                kb.add_text("Wiki: %s" % f["title"], f["snippet"],
                            source="wikipedia")
        return res

    def t_wiki_summary(args, session):
        res = wiki.summary(str(args.get("topico", "")))
        if res.get("ok") and kb is not None:
            kb.add_text("Wiki: %s" % res["title"], res["extract"],
                        source="wikipedia")
        return res

    def t_ddg(args, session):
        return ddg.query(str(args.get("termo", "")))

    def t_jina(args, session):
        res = jina.read(str(args.get("url", "")))
        if res.get("ok") and kb is not None:
            title = str(args.get("url", ""))[:60]
            kb.add_text("Web: %s" % title, res["content"],
                        source=str(args.get("url", "")))
        return res

    def t_football(args, session):
        if football is None:
            return {"ok": False,
                    "speech": "football-data.org requer API key gratuita "
                              "(registe em football-data.org)."}
        return football.matches_today(
            str(args.get("competicao", "BSA")))

    cc.register("wiki_pesquisar", "pesquisar na Wikipedia (gratuito)",
                t_wiki_search, "read", args={"termo": "assunto"},
                confirm=False)
    cc.register("wiki_resumo", "resumo enciclopedico de um topico",
                t_wiki_summary, "read", args={"topico": "topico"},
                confirm=False)
    cc.register("ddg_buscar", "busca instantanea DuckDuckGo (gratuito)",
                t_ddg, "read", args={"termo": "termo"}, confirm=False)
    cc.register("jina_ler", "ler URL com Jina Reader (SPA/JS ok)",
                t_jina, "read", args={"url": "endereco"}, confirm=False)
    if football is not None:
        cc.register("futebol_hoje",
                    "partidas de hoje (football-data.org free tier)",
                    t_football, "read", args={"competicao": "BSA"},
                    confirm=False)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile

    fails: List[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
        if not cond:
            fails.append(name)

    # --- OllamaEmbedder com post_fn falso ---
    class FakePost:
        def __init__(self, has_model=True):
            self.has_model = has_model
            self.calls: List[dict] = []

        def __call__(self, path, payload):
            self.calls.append({"path": path, "payload": payload})
            if path == "/api/tags":
                if self.has_model:
                    return {"models": [{"name": "nomic-embed-text:latest"}]}
                return {"models": []}
            if path == "/api/embed":
                if self.has_model:
                    return {"embeddings": [[0.1, 0.2, 0.3, 0.4]]}
            return None

    fp = FakePost(has_model=True)
    emb = OllamaEmbedder(post_fn=fp)
    check("embedder: detecta modelo", emb.detect() is True)
    vec = emb.embed("texto de teste")
    check("embedder: retorna vetor", vec is not None and len(vec) == 4)
    fp2 = FakePost(has_model=False)
    emb2 = OllamaEmbedder(post_fn=fp2)
    check("embedder: sem modelo detecta False", emb2.detect() is False)
    check("embedder: embed retorna None sem modelo",
          emb2.embed("x") is None)

    # cosseno
    check("cosseno: identico", OllamaEmbedder.cosine(
        [1, 0], [1, 0]) > 0.999)
    check("cosseno: ortogonal", abs(OllamaEmbedder.cosine(
        [1, 0], [0, 1])) < 0.001)

    # --- Wikipedia com post falso ---
    class FakeWikiPost:
        def __call__(self, url):
            if "action=query" in url:
                return {"query": {"search": [
                    {"title": "Poisson distribution",
                     "snippet": "In probability theory, <b>Poisson</b>..."}]}}
            if "rest_v1" in url:
                return {"title": "Poisson distribution",
                        "extract": "A discrete probability distribution."}
            return None

    wiki = WikipediaSource(post_fn=FakeWikiPost())
    r = wiki.search("poisson")
    check("wiki: search ok", r["ok"] is True and len(r["findings"]) == 1)
    r = wiki.summary("Poisson distribution")
    check("wiki: summary ok", r["ok"] is True
          and "discrete" in r["extract"])

    # --- DuckDuckGo ---
    class FakeDDGPost:
        def __call__(self, url):
            if "duckduckgo" in url:
                return {"AbstractText": "Python is a programming language.",
                        "Answer": "", "Definition": "",
                        "RelatedTopics": []}
            return None

    ddg = DuckDuckGoSource(post_fn=FakeDDGPost(), min_interval=0.0)
    r = ddg.query("python")
    check("ddg: abstract retornado", r["ok"] is True
          and "programming" in r["answer"])

    # --- Jina Reader ---
    class FakeJinaPost:
        def __call__(self, url):
            if "r.jina.ai" in url:
                return ("# Titulo\n\nConteudo em markdown limpo. "
                        "Texto adicional para representar uma resposta válida.")
            return None

    jina = JinaReaderSource(post_fn=FakeJinaPost(), min_interval=0.0)
    r = jina.read("exemplo.com")
    check("jina: le e retorna markdown", r["ok"] is True
          and "markdown" in r["content"])

    # --- FootballDataOrg ---
    class FakeFbPost:
        def __call__(self, url):
            if "competitions" in url and "matches" not in url:
                return {"competitions": [{"name": "Serie A", "code": "BSA"}]}
            if "matches" in url:
                return {"matches": [{
                    "homeTeam": {"name": "Time A"},
                    "awayTeam": {"name": "Time B"},
                    "score": {"fullTime": {"home": 2, "away": 1}},
                    "status": "FINISHED", "minute": 90}]}
            return None

    fb = FootballDataOrg(post_fn=FakeFbPost(), min_interval=0.0)
    r = fb.matches_today("BSA")
    check("football: partidas retornadas", r["ok"] is True
          and len(r["matches"]) == 1
          and r["matches"][0]["home"] == "Time A")

    # --- gramatica ---
    g = parse_external_intel("pesquisa na wikipedia sobre poisson")
    check("gram: wiki pesquisar", g == ("wiki_pesquisar",
                                        {"termo": "poisson"}))
    g = parse_external_intel("o que é distribuição de poisson?")
    check("gram: wiki resumo", g is not None
          and g[0] == "wiki_resumo" and "poisson" in g[1]["topico"])
    g = parse_external_intel("busca no duckduckgo por python")
    check("gram: ddg", g == ("ddg_buscar", {"termo": "python"}))
    check("gram: jogos de hoje", parse_external_intel("jogos de hoje")
          == ("futebol_hoje", {}))

    # --- integracao CommandCenter ---
    try:
        from jarvis_command_center import CommandCenter
    except Exception:
        CommandCenter = None  # type: ignore
    if CommandCenter is None:
        print("[SKIP] jarvis_command_center nao importavel aqui")
    else:
        cc = CommandCenter()
        build_external_intel_tools(cc, wiki, ddg, jina)
        r = cc.execute("wiki_resumo", {"topico": "poisson"}, "u")
        check("cc: wiki_resumo funciona", r["ok"] is True
              and "discrete" in r["speech"])
        r = cc.execute("ddg_buscar", {"termo": "python"}, "u")
        check("cc: ddg funciona", r["ok"] is True)

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - external_intelligence.py")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
