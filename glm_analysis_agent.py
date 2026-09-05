#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA QUANT-X V25 — GLM Analysis Agent (advisory only).

Backend: OpenAI-compatible (Ollama / vLLM / LM Studio).
Invariantes: paper_trade=True, execution_allowed=False, GLM advisory only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("aura.glm_agent")

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore


class GLMBackend(Enum):
    OLLAMA = "ollama"
    VLLM = "vllm"
    LM_STUDIO = "lm_studio"
    CUSTOM = "custom"


DEFAULT_SYSTEM_PROMPT = """Você é o AURA QUANT-X, analista quantitativo de escanteios.
Tom frio, militar, objetivo. NUNCA diga Olá/Claro/Desculpe.
Responda APENAS JSON válido:
{
  "decision": "ENTRA|AGUARDA|NAO_ENTRA",
  "confidence": 0.0,
  "score": 0,
  "reasoning": "texto curto",
  "triggers": [],
  "kills": [],
  "timing": "",
  "side": "home|away|both"
}
Regras: ENTRA só se score>=70 e confidence>=0.75 e >=2 triggers.
Liste kills que bloqueiam. Gap >8min = resfriamento. Cluster de ataques perigosos aumenta chance.
Foco só em escanteios/pressão/xG. paper_trade=true — nunca execute ordem."""


@dataclass
class GLMConfig:
    backend: GLMBackend = GLMBackend.OLLAMA
    api_base: str = "http://127.0.0.1:11434/v1"
    model_name: str = "glm-4.7-flash"
    api_key: str = "ollama"
    request_timeout: float = 30.0
    circuit_timeout_sec: float = 15.0
    max_retries: int = 3
    retry_delay: float = 1.0
    max_concurrent_requests: int = 5
    bridge_url: str = "http://127.0.0.1:8080"
    engine_ws_url: str = "ws://127.0.0.1:8765"
    paper_trade: bool = True
    execution_allowed: bool = False
    windows: List[str] = field(default_factory=lambda: ["35_ht", "85_ft"])
    cache_ttl: int = 60
    max_history: int = 1000
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


class GLMClient:
    def __init__(self, config: GLMConfig):
        self.config = config
        self.session: Any = None
        self._request_count = 0
        self._error_count = 0
        self._last_success: Optional[datetime] = None
        self._fail_count = 0
        self._circuit_open_until = 0.0
        self._max_fails = 3
        self._circuit_cooldown_sec = 60.0
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)

    def is_circuit_open(self) -> bool:
        if time.time() >= self._circuit_open_until:
            if self._circuit_open_until:
                self._circuit_open_until = 0.0
                self._fail_count = 0
            return False
        return True

    def record_failure(self) -> None:
        self._fail_count += 1
        if self._fail_count >= self._max_fails:
            self._circuit_open_until = time.time() + self._circuit_cooldown_sec
            logger.critical("CIRCUIT BREAKER ABERTO: Ollama falhou 3x; fallback heurístico por 60s.")

    def record_success(self) -> None:
        self._fail_count = 0
        self._circuit_open_until = 0.0

    async def start(self) -> None:
        if aiohttp is None:
            raise RuntimeError("aiohttp nao instalado (pip install aiohttp)")
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=min(self.config.request_timeout, self.config.circuit_timeout_sec))
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.config.api_key}",
                },
            )
            logger.info("GLM Client: %s model=%s", self.config.api_base, self.config.model_name)

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def health_check(self) -> Dict[str, Any]:
        try:
            await self.start()
            url = f"{self.config.api_base.rstrip('/')}/models"
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return {"healthy": False, "error": f"HTTP {resp.status}"}
                data = await resp.json()
                models = [m.get("id", "") for m in data.get("data", [])]
                return {
                    "healthy": True,
                    "backend": self.config.backend.value,
                    "models_available": models,
                    "target_model_present": self.config.model_name in models
                    or any(self.config.model_name in str(m) for m in models),
                    "requests_made": self._request_count,
                    "errors": self._error_count,
                    "last_success": self._last_success.isoformat() if self._last_success else None,
                    "paper_trade": True,
                }
        except Exception as e:
            return {"healthy": False, "error": str(e), "paper_trade": True}

    def build_payload(self, messages: List[Dict[str, str]],
                      temperature: float = 0.1,
                      max_tokens: int = 1500) -> Dict[str, Any]:
        """Constrói payload limitado para proteger contexto/VRAM.

        O limite é aplicado localmente; nenhum serviço é iniciado por este método.
        """
        history = list(messages or [])[-4:]
        return {
            "model": self.config.model_name,
            "messages": history,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
            "keep_alive": "0m",
            "options": {"num_ctx": 2048, "temperature": 0.3, "top_p": 0.9},
            "paper_trade": True,
            "execution_allowed": False,
        }

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> Optional[str]:
        if self.is_circuit_open():
            logger.warning("Ollama em cooldown; usando fallback heurístico.")
            return None
        await self.start()
        payload = self.build_payload(messages, temperature=temperature,
                                     max_tokens=max_tokens)
        for attempt in range(self.config.max_retries):
            try:
                async with self._semaphore:
                    async with self.session.post(
                        f"{self.config.api_base.rstrip('/')}/chat/completions",
                        json=payload,
                    ) as resp:
                        self._request_count += 1
                        if resp.status == 200:
                            data = await resp.json()
                            content = data["choices"][0]["message"]["content"]
                            self._last_success = datetime.now(timezone.utc)
                            self.record_success()
                            return content
                        err = await resp.text()
                        self.record_failure()
                        logger.warning("GLM HTTP %s try %s: %s", resp.status, attempt + 1, err[:200])
            except asyncio.TimeoutError:
                self._error_count += 1
                self.record_failure()
                logger.warning("GLM timeout try %s", attempt + 1)
            except Exception as e:
                self._error_count += 1
                self.record_failure()
                logger.error("GLM error try %s: %s", attempt + 1, e)
            await asyncio.sleep(self.config.retry_delay * (2 ** attempt))
        return None


class GLMAnalysisAgent:
    DECISIONS_FILE = Path(__file__).resolve().parents[1] / "data" / "glm_decisions.jsonl"

    """Agente advisory — nunca executa ordem."""

    def __init__(self, config: Optional[GLMConfig] = None):
        self.config = config or GLMConfig()
        # forca invariantes
        self.config.paper_trade = True
        self.config.execution_allowed = False
        self.glm_client = GLMClient(self.config)
        self._running = False
        self._active_matches: Dict[str, Dict] = {}
        self._analysis_cache: Dict[str, Tuple[float, Dict]] = {}
        self._history: List[Dict] = []
        self.stats = {
            "matches_analyzed": 0,
            "decisions_made": 0,
            "enters_recommended": 0,
            "waits_recommended": 0,
            "no_bets_recommended": 0,
            "glm_calls": 0,
            "glm_errors": 0,
        }
        # --- V2: features + memoria + reasoning multi-passo ---
        try:
            from agents.feature_engine import FeatureEngine, MatchTimeline
            from agents.memory_store import MemoryStore
            from agents.reasoning_engine import ReasoningEngine
        except ImportError:
            from engine.agents.feature_engine import FeatureEngine, MatchTimeline
            from engine.agents.memory_store import MemoryStore
            from engine.agents.reasoning_engine import ReasoningEngine
        mem_path = Path(__file__).resolve().parents[1] / "data" / "glm_memory.json"
        self.memory = MemoryStore(mem_path)
        self.timelines: Dict[str, MatchTimeline] = {}
        self.feature_engine = FeatureEngine(self.timelines)
        self.reasoning = ReasoningEngine(self.glm_client, self.memory, self.feature_engine)
        self._v2 = True

    async def glm_analyze(
        self, match_data: Dict[str, Any], force_refresh: bool = False
    ) -> Dict[str, Any]:
        fixture_id = str(
            match_data.get("fixture_id")
            or match_data.get("fixtureId")
            or match_data.get("match_id")
            or "unknown"
        )
        minute = match_data.get("minute", 0)
        cache_key = f"{fixture_id}:{minute}"

        if not force_refresh and cache_key in self._analysis_cache:
            ts, cached = self._analysis_cache[cache_key]
            if time.time() - ts < self.config.cache_ttl:
                return cached

        t0 = time.time()
        try:
            # V2 multi-passo (features → analise → critica → consolidacao)
            analysis = await self.reasoning.analyze(match_data)
            self.stats["glm_calls"] += 2  # primary + critique (aprox)
        except Exception as e:
            self.stats["glm_errors"] += 1
            logger.exception("GLM V2 pipeline error")
            analysis = {
                "error": str(e),
                "decision": "AGUARDA",
                "reasoning": "pipeline V2 falhou",
                "paper_trade": True,
                "execution_allowed": False,
            }
        elapsed = time.time() - t0
        analysis["response_time_sec"] = round(elapsed, 2)
        analysis["timestamp"] = datetime.now(timezone.utc).isoformat()
        analysis["fixture_id"] = fixture_id
        analysis["paper_trade"] = True
        analysis["execution_allowed"] = False
        analysis["agent_version"] = "glm_v2"

        # continuidade de raciocinio entre minutos
        tl = self.timelines.get(fixture_id)
        if tl is not None:
            tl.last_conclusion = (
                f"{analysis.get('decision')} @ {minute}' — "
                f"{', '.join((analysis.get('triggers') or [])[:3]) or 'sem gatilhos'}"
            )

        self._analysis_cache[cache_key] = (time.time(), analysis)
        if len(self._analysis_cache) > 100:
            oldest = min(self._analysis_cache, key=lambda k: self._analysis_cache[k][0])
            del self._analysis_cache[oldest]

        decision = analysis.get("decision", "AGUARDA")
        if decision == "ENTRA":
            self.stats["enters_recommended"] += 1
        elif decision == "NAO_ENTRA":
            self.stats["no_bets_recommended"] += 1
        else:
            self.stats["waits_recommended"] += 1
        self.stats["decisions_made"] += 1
        self._history.append(
            {
                "fixture_id": fixture_id,
                "minute": minute,
                "decision": decision,
                "score": analysis.get("score"),
                "confidence": analysis.get("confidence"),
                "triggers": analysis.get("triggers") or [],
                "timestamp": analysis.get("timestamp"),
            }
        )
        if len(self._history) > self.config.max_history:
            self._history = self._history[-self.config.max_history :]
        self._journal_decision(analysis, match_data)
        return analysis

    
    def _journal_decision(self, analysis: Dict, view: Dict) -> None:
        """Persiste decisao para FeedbackConnector resolver pos-jogo."""
        try:
            self.DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "fixture_id": view.get("fixture_id") or view.get("fixtureId") or view.get("match_id"),
                "home": view.get("home"),
                "away": view.get("away"),
                "league": view.get("league"),
                "minute": view.get("minute"),
                "window": self._get_window(int(view.get("minute") or 0)),
                "decision": analysis.get("decision", "AGUARDA"),
                "confidence": analysis.get("confidence", 0.0),
                "score": analysis.get("score"),
                "triggers": analysis.get("triggers", []),
                "kills": analysis.get("kills", []),
                "critique": (analysis.get("_meta") or {}).get("critique_verdict"),
            }
            with self.DECISIONS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("journal_decision falhou (nao critico): %s", e)

    async def resolve_match(self, fixture_id: str, outcome: Dict[str, Any]) -> None:
        """Feedback pos-jogo: alimenta MemoryStore (calibração)."""
        for h in [x for x in self._history if x.get("fixture_id") == fixture_id]:
            self.memory.record_outcome(
                fixture_id=fixture_id,
                triggers=list(h.get("triggers") or []),
                confidence=float(h.get("confidence") or 0.5),
                was_correct=bool(outcome.get("correct", False)),
            )

    def _parse_response(self, response: str, fixture_id: str, elapsed: float) -> Dict[str, Any]:
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.strip("`")
            if clean.startswith("json"):
                clean = clean[4:].strip()
        try:
            analysis = json.loads(clean)
            if not isinstance(analysis, dict):
                raise ValueError("not dict")
        except Exception:
            logger.warning("GLM JSON invalido para %s", fixture_id)
            return {
                "error": "invalid_json",
                "raw_response": response[:500],
                "decision": "AGUARDA",
                "fixture_id": fixture_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        analysis["response_time_sec"] = round(elapsed, 2)
        analysis["timestamp"] = datetime.now(timezone.utc).isoformat()
        analysis["fixture_id"] = fixture_id
        return analysis

    async def evaluate(self, window: str, match_context: Dict) -> Dict[str, Any]:
        if window not in self.config.windows:
            return {
                "action": "no_bet",
                "reason": f"Janela {window} nao suportada",
                "paper_trade": True,
            }
        match_data = match_context.get("match", match_context)
        analysis = await self.glm_analyze(match_data)
        decision_map = {
            "ENTRA": "prepare",
            "AGUARDA": "observe",
            "NAO_ENTRA": "no_bet",
        }
        action = decision_map.get(analysis.get("decision", "AGUARDA"), "observe")
        return {
            "action": action,
            "window": window,
            "glm_analysis": analysis,
            "paper_trade": True,
            "execution_allowed": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def observe(self, match_id: str) -> Dict[str, Any]:
        return {"action": "observe", "match_id": match_id, "paper_trade": True}

    async def prepare(self, match_id: str, signal: Dict) -> Dict[str, Any]:
        return {
            "action": "prepare",
            "match_id": match_id,
            "signal": signal,
            "execution": "BLOCKED",
            "reason": "paper_trade=true, execution_allowed=false",
            "paper_trade": True,
            "execution_allowed": False,
        }

    async def no_bet(self, match_id: str, reason: str = "") -> Dict[str, Any]:
        return {
            "action": "no_bet",
            "match_id": match_id,
            "reason": reason or "Condicoes insuficientes",
            "paper_trade": True,
        }

    async def glm_health_check(self) -> Dict[str, Any]:
        return {
            "agent": "glm_analysis_v1",
            "glm_backend": await self.glm_client.health_check(),
            "agent_stats": self.stats,
            "active_matches": len(self._active_matches),
            "cache_size": len(self._analysis_cache),
            "history_size": len(self._history),
            "paper_trade": True,
            "execution_allowed": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def glm_batch_process(self, matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        tasks = [asyncio.create_task(self.glm_analyze(m)) for m in matches]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: List[Dict[str, Any]] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                out.append(
                    {
                        "fixture_id": matches[i].get("fixture_id", "unknown"),
                        "error": str(r),
                        "decision": "AGUARDA",
                        "paper_trade": True,
                    }
                )
            else:
                out.append(r)  # type: ignore
        self.stats["matches_analyzed"] += len(matches)
        return out

    def _build_analysis_prompt(self, match_data: Dict) -> str:
        home = match_data.get("home", "?")
        away = match_data.get("away", "?")
        minute = match_data.get("minute", 0)
        score = f"{match_data.get('score_home', 0)}x{match_data.get('score_away', 0)}"
        corners_h = match_data.get("corners_home", match_data.get("corners", 0))
        corners_a = match_data.get("corners_away", 0)
        dang_h = match_data.get("dangerous_home", match_data.get("dangerous_attacks", 0))
        dang_a = match_data.get("dangerous_away", 0)
        xg_h = match_data.get("xg_home", match_data.get("xG", match_data.get("xg", 0)))
        xg_a = match_data.get("xg_away", 0)
        pressure = match_data.get("pressure", match_data.get("pressure_ma", "N/D"))
        pf = match_data.get("pressure_features") or {}
        corner_events = match_data.get("corner_events") or []
        last_corners = [
            f"{e.get('minute', '?')}' ({e.get('team', '?')})" for e in corner_events[-5:]
        ]
        gap = "N/A"
        try:
            if corner_events and minute is not None:
                gap = f"{int(minute) - int(corner_events[-1].get('minute', 0))} min"
        except Exception:
            pass
        window = (
            "35-45 HT"
            if 30 <= int(minute or 0) <= 48
            else "80-90 FT"
            if int(minute or 0) >= 80
            else "fora"
        )
        return f"""ANALISE ESCANTEIOS (paper trade):
Partida: {home} x {away} | Minuto: {minute}' | Placar: {score}
Corners: {corners_h}-{corners_a} | Perigosos: {dang_h}-{dang_a}
xG: {xg_h}-{xg_a} | Pressao: {pressure}
pressure_ma={pf.get('pressure_ma')} delta={pf.get('pressure_delta')} dang_rate_10m={pf.get('dang_rate_10m')}
Ultimos cantos: {last_corners or ['nenhum']} | Gap: {gap} | Janela: {window}
Responda SO JSON no formato do system prompt."""

    def _get_window(self, minute: int) -> str:
        if 30 <= minute <= 48:
            return "35_ht"
        if minute >= 80:
            return "85_ft"
        return "out"

    async def start(self) -> None:
        self._running = True
        await self.glm_client.start()
        logger.info(
            "GLM Agent start backend=%s model=%s paper_trade=True",
            self.config.backend.value,
            self.config.model_name,
        )

    async def stop(self) -> None:
        self._running = False
        await self.glm_client.close()

    def get_stats(self) -> Dict[str, Any]:
        return {
            **self.stats,
            "active_matches": len(self._active_matches),
            "cache_size": len(self._analysis_cache),
            "history_size": len(self._history),
            "paper_trade": True,
            "execution_allowed": False,
        }


def load_config(config_path: Optional[str] = None) -> GLMConfig:
    cfg = GLMConfig()
    if config_path and Path(config_path).exists():
        try:
            import yaml  # optional

            data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
            for key, value in data.items():
                if not hasattr(cfg, key) or key == "system_prompt" and not value:
                    continue
                if key == "backend":
                    value = GLMBackend(str(value))
                setattr(cfg, key, value)
        except Exception as e:
            logger.warning("glm_config load failed: %s — using defaults", e)
    cfg.paper_trade = True
    cfg.execution_allowed = False
    return cfg


# Instancia global para import no engine
glm_agent: Optional[GLMAnalysisAgent] = None


def get_glm_agent(config_path: Optional[str] = None) -> GLMAnalysisAgent:
    global glm_agent
    if glm_agent is None:
        glm_agent = GLMAnalysisAgent(load_config(config_path))
    return glm_agent


async def main() -> None:
    parser = argparse.ArgumentParser(description="GLM Analysis Agent AURA V25")
    parser.add_argument("--config", "-c", default=None)
    parser.add_argument("--backend", choices=["ollama", "vllm", "lm_studio"])
    parser.add_argument("--model", "-m")
    parser.add_argument("--health-check", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.backend:
        cfg.backend = GLMBackend(args.backend)
        ports = {"ollama": "11434", "vllm": "8000", "lm_studio": "1234"}
        cfg.api_base = f"http://127.0.0.1:{ports[args.backend]}/v1"
    if args.model:
        cfg.model_name = args.model
    agent = GLMAnalysisAgent(cfg)
    if args.health_check:
        print(json.dumps(await agent.glm_health_check(), indent=2, ensure_ascii=False))
        await agent.stop()
        return
    await agent.start()
    print(json.dumps(await agent.glm_health_check(), indent=2, ensure_ascii=False))
    await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
