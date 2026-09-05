# -*- coding: utf-8 -*-
"""
PILAR 2 - Roteador Determinístico (Intenções)
AURA QUANT-X v12.7.0-RECONSOLIDADO

Classificação local, previsível e sem GPU. `route()` retorna o contrato tipado
dos anexos; `classify()` mantém o dicionário usado pelo servidor do Engine.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Pattern, Tuple

logger = logging.getLogger("aura.pilar2.router")
CACHE_TTL_MS = 350
CACHE_CAPACITY = 1000


class IntentRoute(Enum):
    TRADING = "trading"
    SYSTEM = "system"
    GENERAL = "general"
    EXTERNAL_CURRENT = "external_current"
    UNKNOWN = "unknown"


Intent = IntentRoute


@dataclass(frozen=True)
class RouteResult:
    route: IntentRoute
    confidence: float
    response_override: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_cached: bool = False
    text: str = ""
    latency_us: int = 0
    gpu_bypassed: bool = True

    @property
    def cache_hit(self) -> bool:
        return self.is_cached

    @property
    def intent(self) -> IntentRoute:
        return self.route

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.route.value,
            "route": self.route.value,
            "text": self.text,
            "confidence": self.confidence,
            "response_override": self.response_override,
            "metadata": dict(self.metadata),
            "is_cached": self.is_cached,
            "cache_hit": self.is_cached,
            "latency_us": self.latency_us,
            "gpu_bypassed": self.gpu_bypassed,
        }


class LRUCacheTTL:
    """Cache LRU com TTL ultra-curto e contadores thread-safe."""

    def __init__(self, capacity: int = CACHE_CAPACITY, ttl_ms: int = CACHE_TTL_MS):
        self.capacity = max(1, int(capacity))
        self.ttl_ms = max(0, int(ttl_ms))
        self._data: OrderedDict[str, Tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        now = time.time() * 1000.0
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return None
            value, ts = self._data[key]
            if (now - ts) > self.ttl_ms:
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: Any) -> None:
        now = time.time() * 1000.0
        with self._lock:
            if key in self._data:
                del self._data[key]
            elif len(self._data) >= self.capacity:
                self._data.popitem(last=False)
            self._data[key] = (value, now)

    def set(self, key: str, value: Any) -> None:
        self.put(key, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "size": len(self._data)}

    def get_stats(self) -> Dict[str, int]:
        return self.stats()


class LRUCache(LRUCacheTTL):
    """Nome de compatibilidade do contrato original do Pilar 2."""


class DeterministicRouter:
    """Roteador determinístico com prioridade SYSTEM > EXTERNAL > TRADING."""

    def __init__(self, cache_capacity: int = CACHE_CAPACITY, cache_ttl_ms: int = CACHE_TTL_MS):
        self._cache = LRUCache(cache_capacity, cache_ttl_ms)
        self._patterns: List[Tuple[Pattern[str], IntentRoute]] = self._compile_patterns()
        self._route_counts: Dict[str, int] = {route.value: 0 for route in IntentRoute}
        self._total_routed = 0
        self._stats_lock = threading.Lock()
        logger.info("Roteador determinístico ativo | patterns=%d | cache_ttl=%dms", len(self._patterns), cache_ttl_ms)

    def _compile_patterns(self) -> List[Tuple[Pattern[str], IntentRoute]]:
        raw = [
            (r"\b(status|diagn[oó]stico|porta|portas?|mem[oó]ria|log|logs|reiniciar|restart|sa[uú]de|health|engine|bridge|voz|voice|ollama|servi[cç]o|supervisor|gpu|cpu|ram|ajuda|help)\b", IntentRoute.SYSTEM),
            (r"\b(not[ií]cia|not[ií]cias|news|resultado ao vivo|live score|placar atual|tempo real|jogador|contusão|contus[aã]o|escalação|escala[cç][aã]o|transferência|transferencia|clima|chuva|tempo agora)\b", IntentRoute.EXTERNAL_CURRENT),
            (r"\b(entrada|aposta|sinal|kelly|edge|corner|escanteio|asiático|asiatica|asian|hdp|over|under|buy|sell|stake|analis(ar|e)|análise|analise|probabilidade|poisson|risco|risk|velocity|weight of money|wom|xg|expected goals|dangerous|attacks|mercado|partida|jogo|resumo|pressão|pressao|regime)\b", IntentRoute.TRADING),
        ]
        return [(re.compile(pattern, re.IGNORECASE | re.UNICODE), route) for pattern, route in raw]

    def _static_response(self, route: IntentRoute, text: str) -> Optional[str]:
        low = text.lower()
        if route is IntentRoute.SYSTEM:
            if "engine" in low:
                return "O Engine está operacional no caminho local; consulte os gates e o status detalhado."
            if "bridge" in low:
                return "A Bridge é o canal local de captura e encaminhamento; consulte a saúde da porta 8080."
            if "voz" in low or "voice" in low:
                return "O serviço de voz é local e depende dos motores STT, Ollama e TTS configurados."
            if "ajuda" in low or "help" in low:
                return "O sistema está operacional. Posso analisar a partida, mostrar risco, dados, mercado ou diagnosticar serviços."
            if low.strip() == "status":
                return "O sistema está operacional em modo paper trade; consulte o status detalhado para portas e componentes."
            return "Posso consultar o diagnóstico local sem inventar estado de serviços."
        if route is IntentRoute.EXTERNAL_CURRENT:
            return "Não há fonte online validada nesta rota; não vou inventar notícia ou informação atual."
        if route is IntentRoute.GENERAL:
            return "Estou pronta para conversar e coordenar o AURA QUANT-X."
        if route is IntentRoute.UNKNOWN:
            return "Não reconheci essa intenção. Posso analisar trading, diagnosticar o sistema ou conversar sobre um assunto geral."
        return None

    def _classify_uncached(self, text: str) -> Tuple[IntentRoute, Optional[str], Dict[str, Any], float]:
        route = IntentRoute.UNKNOWN if text.strip() else IntentRoute.GENERAL
        metadata: Dict[str, Any] = {}
        for pattern, candidate in self._patterns:
            if pattern.search(text):
                route = candidate
                break
        greeting = re.sub(r"[!?.]+$", "", text.strip().lower())
        if route is IntentRoute.UNKNOWN and greeting in {"oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "obrigado", "tchau"}:
            route = IntentRoute.GENERAL
        if route is IntentRoute.TRADING and re.search(r"weight\s+of\s+money|\bwom\b", text, re.IGNORECASE):
            metadata["response_key"] = "wom"
        static = self._static_response(route, text)
        confidence = 1.0 if text.strip().lower() in {"status", "oi", "olá", "ola"} else (0.92 if route is not IntentRoute.UNKNOWN else 0.25)
        return route, static, metadata, confidence

    def route(self, text: str) -> RouteResult:
        raw = str(text or "")
        key = raw.strip().lower()[:256]
        if key:
            cached = self._cache.get(key)
            if cached is not None:
                result = RouteResult(**{**cached, "is_cached": True, "latency_us": 0})
                self._record(result.route)
                return result
        t0 = time.perf_counter_ns()
        route, static, metadata, confidence = self._classify_uncached(raw)
        result = RouteResult(
            route=route,
            confidence=confidence,
            response_override=static,
            metadata=metadata,
            is_cached=False,
            text=raw[:200],
            latency_us=(time.perf_counter_ns() - t0) // 1000,
        )
        if key:
            self._cache.put(key, result.__dict__)
        self._record(route)
        return result

    def classify(self, text: str) -> Dict[str, Any]:
        return self.route(text).to_dict()

    def _record(self, route: IntentRoute) -> None:
        with self._stats_lock:
            self._total_routed += 1
            self._route_counts[route.value] = self._route_counts.get(route.value, 0) + 1

    def clear_cache(self) -> None:
        self._cache.clear()

    def stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            result = {
                "total_routed": self._total_routed,
                "route_distribution": dict(self._route_counts),
            }
        result.update(self._cache.stats())
        return result

    def get_stats(self) -> Dict[str, Any]:
        return self.stats()


_router: Optional[DeterministicRouter] = None
_router_lock = threading.Lock()


def get_router() -> DeterministicRouter:
    global _router
    with _router_lock:
        if _router is None:
            _router = DeterministicRouter()
        return _router


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = get_router()
    for text in ("status", "analisar entrada de corner", "notícias sobre o jogo", "olá"):
        print(r.route(text).to_dict())
