#!/usr/bin/env python3
"""
mora_resource_orchestrator.py — Orquestrador de Recursos do Agente MORA.

Responsabilidades:
1. Pausar agentes das camadas `bridge` e `agents` preservando invariantes.
2. Alocar até 85% de VRAM/CPU para a GLM via gpu_resource_manager.
3. Garantir retomada segura do sistema ao vivo (mesmo em exceção).

Invariantes protegidos programaticamente:
- paper_trade = True
- execution_allowed = False
- advisory_only = True
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Layers que DEVEM ser pausadas durante o ciclo MORA
PAUSABLE_LAYERS: frozenset = frozenset({"bridge", "agents"})

# Layers que PERMANECEM ativas (necessárias para a auditoria)
CRITICAL_LAYERS: frozenset = frozenset({"engine", "sre"})

# Limite de GPU (85% conforme protocolo MORA)
GPU_LIMIT_PERCENT: float = 0.85

# VRAM mínimo de reserva para evitar OOM no TTS/engine (MB)
VRAM_RESERVE_MB: int = 512


@dataclass
class AgentState:
    """Snapshot imutável do estado de um agente antes da pausa."""
    name: str
    layer: str
    original_status: str
    paper_trade: bool
    execution_allowed: bool
    advisory_only: bool
    path: str
    paused: bool = False


@dataclass
class ResourceLease:
    """Contrato de recursos alocados pelo MORA durante o ciclo."""
    started_at: datetime
    paused_agents: List[AgentState] = field(default_factory=list)
    vram_allocated_mb: int = 0
    cpu_threads_allocated: int = 0
    gpu_device: Optional[str] = None
    invariant_violations: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True se nenhum invariante foi violado durante a pausa."""
        return len(self.invariant_violations) == 0


class MoraResourceOrchestrator:
    """
    Orquestra alocação de recursos para o ciclo diário do MORA.

    Uso típico:
        orchestrator = MoraResourceOrchestrator(registry, gpu_manager)
        async with orchestrator.acquire_resources() as lease:
            # GLM tem 85% da GPU disponível
            # agentes de linha estão pausados
            await run_pipeline(lease)
        # ao sair do bloco: agentes são retomados automaticamente
    """

    def __init__(
        self,
        agent_registry: Any,
        gpu_manager: Any,
        manifest_path: Optional[Path] = None,
    ):
        self.registry = agent_registry
        self.gpu_manager = gpu_manager
        self.manifest_path = manifest_path or Path("agents/activation_manifest.json")
        self._lock = asyncio.Lock()
        self._active_lease: Optional[ResourceLease] = None

    @asynccontextmanager
    async def acquire_resources(self):
        """
        Context manager assíncrono que adquire recursos, executa bloco,
        e libera — mesmo em caso de exceção.

        Raises:
            RuntimeError: Se invariantes forem violados durante a pausa.
        """
        async with self._lock:
            lease = await self._acquire()
            if not lease.is_valid:
                # Se invariantes violados, libera imediatamente e aborta
                await self._release(lease)
                violation_str = "; ".join(lease.invariant_violations)
                raise RuntimeError(f"Invariantes violados: {violation_str}")
            self._active_lease = lease
            logger.info(
                "MORA recursos adquiridos: %d agentes pausados, %dMB VRAM",
                len(lease.paused_agents),
                lease.vram_allocated_mb,
            )

        try:
            yield lease
        finally:
            async with self._lock:
                await self._release(lease)
                self._active_lease = None

    async def _acquire(self) -> ResourceLease:
        """Executa sequência de aquisição de recursos."""
        lease = ResourceLease(started_at=datetime.now(timezone.utc))

        # Passo 1: Carregar manifest e identificar agentes pausáveis
        manifest = self._load_manifest()
        agents = manifest.get("agents", {})

        # Passo 2: Pausar agentes das camadas bridge e agents
        for name, spec in agents.items():
            layer = spec.get("layer", "")
            status = spec.get("status", "unknown")
            if layer in PAUSABLE_LAYERS and status == "enabled":
                state = AgentState(
                    name=name,
                    layer=layer,
                    original_status=status,
                    paper_trade=spec.get("paper_trade", True),
                    execution_allowed=spec.get("execution_allowed", False),
                    advisory_only=spec.get("advisory_only", True),
                    path=spec.get("path", ""),
                )
                await self._pause_agent(name, state)
                lease.paused_agents.append(state)

        # Passo 3: Verificar invariantes preservados
        lease.invariant_violations = self._verify_invariants(lease.paused_agents)

        # Passo 4: Alocar GPU e CPU
        gpu_info = await self._query_gpu()
        if gpu_info:
            lease.gpu_device = gpu_info.get("device")
            total_vram = gpu_info.get("total_vram_mb", 0)
            # 85% do total, menos reserva de segurança
            allocated = int(total_vram * GPU_LIMIT_PERCENT) - VRAM_RESERVE_MB
            lease.vram_allocated_mb = max(0, allocated)

            # CPU: 85% dos cores
            cpu_count = os.cpu_count() or 4
            lease.cpu_threads_allocated = max(1, int(cpu_count * GPU_LIMIT_PERCENT))

            # Configurar GLM via variáveis de ambiente
            os.environ["GLM_VRAM_LIMIT_MB"] = str(lease.vram_allocated_mb)
            os.environ["GLM_CPU_THREADS"] = str(lease.cpu_threads_allocated)
            logger.info(
                "GPU alocada: %s — %dMB VRAM, %d threads CPU",
                lease.gpu_device,
                lease.vram_allocated_mb,
                lease.cpu_threads_allocated,
            )

        return lease

    async def _release(self, lease: ResourceLease) -> None:
        """Libera recursos e retoma agentes pausados."""
        # Passo 1: Limpar variáveis de ambiente de alocação
        for key in ("GLM_VRAM_LIMIT_MB", "GLM_CPU_THREADS"):
            os.environ.pop(key, None)

        # Passo 2: Retomar agentes pausados (ordem inversa para consistência)
        for state in reversed(lease.paused_agents):
            await self._resume_agent(state.name, state)

        # Passo 3: Verificar invariantes pós-retomada
        violations = self._verify_invariants(lease.paused_agents)
        if violations:
            logger.error("Invariantes violados após retomada: %s", violations)
        else:
            logger.info(
                "MORA recursos liberados: %d agentes retomados com sucesso",
                len(lease.paused_agents),
            )

    def _load_manifest(self) -> Dict[str, Any]:
        """Carrega activation_manifest.json de forma defensiva."""
        if not self.manifest_path.exists():
            logger.warning("Manifest não encontrado: %s", self.manifest_path)
            return {"agents": {}}
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error("Manifest JSON inválido: %s", e)
            return {"agents": {}}

    async def _pause_agent(self, name: str, state: AgentState) -> None:
        """Pausa um agente via registry. Defensive — não quebra se falhar."""
        try:
            if self.registry and hasattr(self.registry, "set_status"):
                result = self.registry.set_status(name, "paused")
                if asyncio.iscoroutine(result):
                    await result
            state.paused = True
            logger.info("Agente pausado: %s", name)
        except Exception as e:
            logger.warning("Falha ao pausar %s: %s", name, e)
            state.paused = False

    async def _resume_agent(self, name: str, state: AgentState) -> None:
        """Retoma um agente via registry."""
        try:
            if self.registry and hasattr(self.registry, "set_status"):
                result = self.registry.set_status(name, state.original_status)
                if asyncio.iscoroutine(result):
                    await result
            state.paused = False
            logger.info("Agente retomado: %s", name)
        except Exception as e:
            logger.error("Falha CRÍTICA ao retomar %s: %s", name, e)
            state.paused = True  # marca como ainda pausado para alerta

    def _verify_invariants(self, agents: List[AgentState]) -> List[str]:
        """
        Verifica que invariantes críticos permanecem intactos.

        Retorna lista de violações (vazia se tudo OK).
        """
        violations: List[str] = []
        for a in agents:
            if not a.paper_trade:
                violations.append(
                    f"{a.name}: paper_trade=False (deve ser True)"
                )
            if a.execution_allowed:
                violations.append(
                    f"{a.name}: execution_allowed is True (deve ser False)"
                )
            if not a.advisory_only and a.layer == "agents":
                violations.append(
                    f"{a.name}: advisory_only=False (deve ser True)"
                )
        return violations

    async def _query_gpu(self) -> Optional[Dict[str, Any]]:
        """Consulta GPU via gpu_resource_manager de forma defensiva."""
        if not self.gpu_manager:
            return None
        try:
            info = self.gpu_manager.cuda_info()
            if asyncio.iscoroutine(info):
                info = await info
            if not isinstance(info, dict):
                return None
            return {
                "device": info.get("device_name") or info.get("device", "unknown"),
                "total_vram_mb": info.get("total_vram_mb", 0),
                "free_vram_mb": info.get("free_vram_mb", 0),
            }
        except Exception as e:
            logger.warning("GPU query falhou: %s", e)
            return None

    async def get_status(self) -> Dict[str, Any]:
        """Retorna status atual do orquestrador (para health check)."""
        active = self._active_lease is not None
        return {
            "active": active,
            "paused_count": len(self._active_lease.paused_agents) if active else 0,
            "vram_mb": self._active_lease.vram_allocated_mb if active else 0,
            "gpu_device": self._active_lease.gpu_device if active else None,
        }