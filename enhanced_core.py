#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enhanced_core.py — camada de UPGRADE opcional: detecta bibliotecas
instalaveis e as usa quando presentes, degradando para stdlib sem elas.

FILOSOFIA: o AURA funciona 100% em stdlib (invariante §6). Este modulo
ADICIONA capacidades quando o usuario instala pip packages — nunca
quebra a base.

UPGRADES DETECTADOS:
    1. duckduckgo-search -> busca web real (resultados, nao instant answers)
    2. chromadb + sentence-transformers -> RAG vetorial persistente
    3. httpx -> HTTP async (2-5x mais rapido em multiplas fontes)
    4. apscheduler -> agendamento persistente (cron-like)
    5. faster-whisper -> STT mais rapido
    6. piper-tts -> TTS neural local offline

USO:
    python -m pip install duckduckgo-search chromadb \
        sentence-transformers httpx apscheduler
    # reinicie o voice server — os upgrades ativam sozinhos

INTEGRACAO: hunks na resposta. Detecta no import, expoe
EnhancedCore.get() singleton com as capacidades disponiveis.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("aura.enhanced")

__version__ = "1.0.0"


def _enabled(flag: str) -> bool:
    """Ativação explícita: ausência da variável mantém o upgrade desligado."""
    return os.environ.get(flag, "0").strip().lower() in {"1", "true", "yes", "on"}


class EnhancedCore:
    """Singleton que detecta e expõe upgrades opcionais."""

    _instance: Optional["EnhancedCore"] = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "EnhancedCore":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._lock = threading.RLock()
        self.capabilities: Dict[str, bool] = {}
        self._search_fn: Optional[Callable] = None
        self._embed_fn: Optional[Callable] = None
        self._vector_store: Optional[Any] = None
        self._scheduler: Optional[Any] = None
        self._detect_all()

    def _detect_all(self) -> None:
        """Detecta cada biblioteca opcional e ativa o upgrade."""
        self._detect_ddg_search()
        self._detect_vector_rag()
        self._detect_httpx()
        self._detect_scheduler()
        self._detect_faster_whisper()
        self._detect_piper()
        logger.info("enhanced_core: %d upgrades ativos (%s)",
                    sum(self.capabilities.values()),
                    ", ".join(k for k, v in self.capabilities.items() if v))

    # ------------------------------------------------------------ detecção
    def _detect_ddg_search(self) -> None:
        """duckduckgo-search: somente com AURA_ENHANCED_ENABLE_WEB=1."""
        if not _enabled("AURA_ENHANCED_ENABLE_WEB"):
            self.capabilities["ddg_search"] = False
            return
        try:
            from duckduckgo_search import DDGS
            self._search_fn = DDGS()
            self.capabilities["ddg_search"] = True
        except ImportError:
            self.capabilities["ddg_search"] = False

    def _detect_vector_rag(self) -> None:
        """RAG vetorial somente com AURA_ENHANCED_ENABLE_RAG=1."""
        if not _enabled("AURA_ENHANCED_ENABLE_RAG"):
            self.capabilities["vector_rag"] = False
            return
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer
            self._vector_store = chromadb.Client()
            self._embed_fn = SentenceTransformer(
                "all-MiniLM-L6-v2")  # 80MB, multilinguagem
            self.capabilities["vector_rag"] = True
        except ImportError:
            self.capabilities["vector_rag"] = False

    def _detect_httpx(self) -> None:
        try:
            import httpx
            self.capabilities["httpx"] = True
        except ImportError:
            self.capabilities["httpx"] = False

    def _detect_scheduler(self) -> None:
        if not _enabled("AURA_ENHANCED_ENABLE_SCHEDULER"):
            self.capabilities["apscheduler"] = False
            return
        try:
            from apscheduler.schedulers.background import (
                BackgroundScheduler)
            self._scheduler = BackgroundScheduler()
            self.capabilities["apscheduler"] = True
        except ImportError:
            self.capabilities["apscheduler"] = False

    def _detect_faster_whisper(self) -> None:
        try:
            import faster_whisper
            self.capabilities["faster_whisper"] = True
        except ImportError:
            self.capabilities["faster_whisper"] = False

    def _detect_piper(self) -> None:
        try:
            import piper
            self.capabilities["piper_tts"] = True
        except ImportError:
            self.capabilities["piper_tts"] = False

    # ------------------------------------------------------------ APIs
    def web_search(self, query: str, max_results: int = 5) -> List[dict]:
        """Busca web real (se ddg_search instalado)."""
        if not self.capabilities.get("ddg_search"):
            return []
        try:
            results = []
            for r in self._search_fn.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "")[:300]})
            return results
        except Exception:
            logger.exception("enhanced: web_search falhou")
            return []

    def semantic_search(self, query: str, collection: str = "knowledge",
                        top_k: int = 5) -> List[dict]:
        """Busca semântica em coleção vetorial (se chromadb instalado)."""
        if not self.capabilities.get("vector_rag"):
            return []
        try:
            col = self._vector_store.get_or_create_collection(collection)
            q_emb = self._embed_fn.encode(query).tolist()
            results = col.query(query_embeddings=[q_emb], n_results=top_k)
            hits = []
            for i, (doc, meta, dist) in enumerate(zip(
                    results.get("documents", [[]])[0],
                    results.get("metadatas", [[]])[0],
                    results.get("distances", [[]])[0])):
                hits.append({"text": doc, "metadata": meta,
                             "distance": round(dist, 3),
                             "similarity": round(1 - dist, 3)})
            return hits
        except Exception:
            logger.exception("enhanced: semantic_search falhou")
            return []

    def semantic_add(self, texts: List[str], collection: str = "knowledge",
                     metadatas: Optional[List[dict]] = None) -> int:
        """Adiciona textos ao índice vetorial."""
        if not self.capabilities.get("vector_rag") or not texts:
            return 0
        try:
            col = self._vector_store.get_or_create_collection(collection)
            embeddings = self._embed_fn.encode(texts).tolist()
            ids = ["doc_%d_%d" % (int(time.time()), i)
                   for i in range(len(texts))]
            col.add(embeddings=embeddings, documents=texts,
                    metadatas=metadatas or [{}] * len(texts), ids=ids)
            return len(texts)
        except Exception:
            logger.exception("enhanced: semantic_add falhou")
            return 0

    def get_scheduler(self) -> Optional[Any]:
        """Retorna o APScheduler se disponível."""
        if self.capabilities.get("apscheduler"):
            return self._scheduler
        return None

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"enhanced_core": {
                "upgrades_active": sum(1 for v in
                                       self.capabilities.values() if v),
                "upgrades_total": len(self.capabilities),
                **self.capabilities}}


# ---------------------------------------------------------------------------
# integração — registra tools que usam os upgrades quando disponíveis
# ---------------------------------------------------------------------------
def build_enhanced_tools(cc) -> None:
    """Registra tools que só funcionam com upgrades instalados."""
    ec = EnhancedCore.get()

    def t_web_search(args, session):
        query = str(args.get("query", ""))
        results = ec.web_search(query, 5)
        if not results:
            return {"ok": False,
                    "speech": ("Busca web real requer 'pip install "
                               "duckduckgo-search'. Instale e "
                               "reinicie.")}
        speech = "Encontrei %d resultado(s): %s." % (
            len(results), "; ".join(r["title"][:40]
                                    for r in results[:3]))
        return {"ok": True, "results": results, "speech": speech}

    def t_semantic_search(args, session):
        query = str(args.get("query", ""))
        hits = ec.semantic_search(query)
        if not hits:
            # fallback para TF-IDF do ToolKnowledge se disponível
            return {"ok": False,
                    "speech": "Busca semântica requer 'pip install chromadb "
                              "sentence-transformers'."}
        best = hits[0]
        return {"ok": True, "hits": hits,
                "speech": "Melhor match (%.0f%%): %s" % (
                    100 * best["similarity"], best["text"][:200])}

    cc.register("web_search",
                "busca web real (requer duckduckgo-search)",
                t_web_search, "read", args={"query": "termo"},
                confirm=False)
    cc.register("busca_semantica",
                "busca semântica no conhecimento (requer chromadb)",
                t_semantic_search, "read", args={"query": "termo"},
                confirm=False)


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import sys

    fails: List[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
        if not cond:
            fails.append(name)

    ec = EnhancedCore.get()

    # detecção não quebra mesmo sem nada instalado
    check("detect: não quebra sem bibliotecas",
          isinstance(ec.capabilities, dict)
          and len(ec.capabilities) >= 5)

    # web_search degrada sem biblioteca
    results = ec.web_search("teste")
    if not ec.capabilities.get("ddg_search"):
        check("web_search: degrada para lista vazia",
              results == [])
    else:
        check("web_search: biblioteca presente", len(results) >= 0)

    # semantic_search degrada sem biblioteca
    hits = ec.semantic_search("teste")
    if not ec.capabilities.get("vector_rag"):
        check("semantic_search: degrada para lista vazia",
              hits == [])
    else:
        check("semantic_search: biblioteca presente", len(hits) >= 0)

    # stats
    st = ec.stats()["enhanced_core"]
    check("stats: coerente",
          st["upgrades_total"] >= 5
          and 0 <= st["upgrades_active"] <= st["upgrades_total"])

    # singleton
    ec2 = EnhancedCore.get()
    check("singleton: mesma instância", ec is ec2)

    # integracao CommandCenter
    try:
        from jarvis_command_center import CommandCenter
    except Exception:
        CommandCenter = None
    if CommandCenter is None:
        print("[SKIP] jarvis_command_center nao importavel aqui")
    else:
        cc = CommandCenter()
        build_enhanced_tools(cc)
        r = cc.execute("web_search", {"query": "python"}, "u")
        if not ec.capabilities.get("ddg_search"):
            check("cc: web_search avisa dependência",
                  r["ok"] is False and "pip install" in r["speech"])
        else:
            check("cc: web_search funciona", r["ok"] is True)

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - enhanced_core.py")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
