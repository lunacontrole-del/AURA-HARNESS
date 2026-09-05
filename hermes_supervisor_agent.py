# -*- coding: utf-8 -*-
"""
Hermes Supervisor Agent — V26.5
Agente que monitora, diagnostica e tenta corrigir problemas do AURA QUANT-X.

Foco:
  - Captura / feed (NO_FEED, modo simulado aparente)
  - Sync UI ↔ Bridge
  - Serviços offline (Ollama, Voice, Bridge, Engine)
  - Integridade paper-only + Hermes primary

Uso:
  from engine.agents.hermes_supervisor_agent import HermesSupervisor
  h = HermesSupervisor()
  report = h.run_cycle()          # um ciclo diagnóstico + ações seguras
  h.run_loop(interval_sec=30)     # loop contínuo

Ou via CLI:
  python -m engine.agents.hermes_supervisor_agent --once
  python -m engine.agents.hermes_supervisor_agent --loop --interval 30
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from engine.gpu_resource_manager import GPU_GOVERNOR
except Exception:
    GPU_GOVERNOR = None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _safe_print(*args, **kwargs):
    """Print safe on Windows cp1252 consoles."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        msg = " ".join(str(a) for a in args)
        print(msg.encode("ascii", "replace").decode("ascii"), **{k: v for k, v in kwargs.items() if k != "file"})

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FEEDBACK_PATH = ROOT / "engine" / "data" / "system_health_feedback.json"
HERMES_REPORT_PATH = ROOT / "engine" / "data" / "hermes_supervisor_report.json"
HERMES_HISTORY_PATH = ROOT / "engine" / "data" / "hermes_supervisor_history.jsonl"
CAPTURE_HINT_LOG = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "AURA_QUANT_X" / "logs" / "capture_forwarder.log"
)

# Portas oficiais
PORTS = {
    "bridge": 8080,
    "engine": 8765,
    "voice": 8099,
    "ollama": 11434,
    "dashboard": 3000,
    "ui_matriz": 8766,
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    code: str
    severity: str  # CRITICAL | HIGH | MEDIUM | LOW | INFO
    message: str
    action: str = ""
    auto_fixable: bool = False
    fixed: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SupervisorReport:
    timestamp: str
    status: str  # HEALTHY | DEGRADED | CRITICAL | UNKNOWN
    findings: List[Finding] = field(default_factory=list)
    actions_taken: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    services: Dict[str, Any] = field(default_factory=dict)
    capture: Dict[str, Any] = field(default_factory=dict)
    sync: Dict[str, Any] = field(default_factory=dict)
    policy: Dict[str, Any] = field(default_factory=dict)
    message_for_operator: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Helpers de rede / processo
# ---------------------------------------------------------------------------
def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_get_json(url: str, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except Exception:
        return None


def _http_get_text(url: str, timeout: float = 4.0) -> Optional[str]:
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _run_bat_async(bat_name: str) -> bool:
    """Dispara BAT de forma segura (não bloqueia). Retorna True se o start foi ok."""
    bat = ROOT / bat_name
    if not bat.exists():
        # tenta bat_extra
        bat = ROOT / "bat_extra" / bat_name
    if not bat.exists():
        return False
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/c", "start", "/MIN", str(bat)],
                cwd=str(ROOT),
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            subprocess.Popen(["bash", str(bat)], cwd=str(ROOT))
        return True
    except Exception:
        return False


def _run_python_module(module_or_script: str, args: Optional[List[str]] = None) -> bool:
    venv_py = ROOT / "engine" / "venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    cmd = [py, "-u"]
    script = ROOT / module_or_script
    if script.exists():
        cmd.append(str(script))
    else:
        cmd.extend(["-m", module_or_script])
    if args:
        cmd.extend(args)
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            subprocess.Popen(cmd, cwd=str(ROOT))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Hermes Supervisor
# ---------------------------------------------------------------------------
class HermesSupervisor:
    """Agente supervisor: diagnostica + aplica correções seguras (restart serviços, orientações)."""

    def __init__(self, auto_fix: bool = True, root: Optional[Path] = None):
        self.auto_fix = auto_fix
        self.root = root or ROOT
        self._last_report: Optional[SupervisorReport] = None

    # ---- coleta ----------------------------------------------------------
    def probe_services(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, port in PORTS.items():
            listening = _port_open(port)
            entry: Dict[str, Any] = {"port": port, "listening": listening}
            if name == "bridge" and listening:
                h = _http_get_json(f"http://127.0.0.1:{port}/health")
                entry["health"] = h
            elif name == "engine" and listening:
                h = _http_get_json(f"http://127.0.0.1:{port}/health") or _http_get_json(
                    f"http://127.0.0.1:{port}/api/health"
                )
                entry["health"] = h
                ui = _http_get_json(f"http://127.0.0.1:{port}/api/ui/state")
                entry["ui_state"] = ui
            elif name == "ollama" and listening:
                tags = _http_get_json(f"http://127.0.0.1:{port}/api/tags")
                entry["tags"] = tags
            elif name == "voice" and listening:
                d = _http_get_json(f"http://127.0.0.1:{port}/api/voice/diagnostic")
                entry["diagnostic"] = d
            out[name] = entry
        return out

    def probe_capture_and_sync(self, services: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        capture: Dict[str, Any] = {
            "has_live_fixture": False,
            "source": None,
            "home": None,
            "away": None,
            "minute": None,
            "freshness_hint": None,
            "simulated_suspected": False,
            "no_feed": False,
        }
        sync: Dict[str, Any] = {"ui_bridge_aligned": None, "detail": None}

        eng = services.get("engine") or {}
        ui = eng.get("ui_state") or {}
        if isinstance(ui, dict) and ui:
            home = ui.get("home") or ui.get("home_team") or (ui.get("fixture") or {}).get("home")
            away = ui.get("away") or ui.get("away_team") or (ui.get("fixture") or {}).get("away")
            minute = ui.get("minute") or ui.get("min") or (ui.get("fixture") or {}).get("minute")
            source = ui.get("source") or ui.get("src")
            capture["home"] = home
            capture["away"] = away
            capture["minute"] = minute
            capture["source"] = source
            capture["has_live_fixture"] = bool(home and away)
            # "modo simulado" aparente: sem times ou source vazio / waiting
            if not home or not away:
                capture["no_feed"] = True
                capture["simulated_suspected"] = True
            if source and str(source).lower() in ("simulated", "sim", "demo", "aguardando", "waiting"):
                capture["simulated_suspected"] = True

        # feedback E2E se existir
        if FEEDBACK_PATH.exists():
            try:
                fb = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
                capture["feedback_status"] = fb.get("status")
                capture["feedback_errors"] = fb.get("errors") or []
                # sync metric se presente
                for k in ("SyncUIBridge", "sync_ui_bridge", "sync_pct"):
                    if k in fb:
                        sync["from_feedback"] = fb.get(k)
            except Exception:
                pass

        # log de captura Windows
        if CAPTURE_HINT_LOG.exists():
            try:
                tail = CAPTURE_HINT_LOG.read_text(encoding="utf-8", errors="replace")[-2000:]
                capture["capture_log_tail"] = tail[-500:]
                if "TOKEN" in tail.upper() and ("vazio" in tail.lower() or "empty" in tail.lower()):
                    capture["token_empty"] = True
                if "NO_FEED" in tail.upper() or "skip" in tail.lower():
                    capture["log_no_feed"] = True
            except Exception:
                pass

        return capture, sync

    def probe_policy(self) -> Dict[str, Any]:
        try:
            from engine.core.policy_runtime import get_system_policy

            return dict(get_system_policy())
        except Exception as e:
            return {"error": str(e), "paper_trade": True, "hermes_primary": True, "mode": "paper"}

    def probe_hermes_pipeline(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"import_ok": False, "primary": None}
        try:
            from engine.agents.aura_hermes_router import is_primary_pipeline, route_corner_analysis

            out["import_ok"] = True
            out["primary"] = is_primary_pipeline()
            # Smoke com 1 snapshot minimo (lista vazia nao e erro de Hermes, e ausencia de captura)
            try:
                _now = datetime.now(timezone.utc).isoformat()
                sample = [
                    {
                        "fixture_id": "hermes-supervisor-smoke",
                        "minute": 10,
                        "corners_home": 2,
                        "corners_away": 1,
                        "attacks_home": 8,
                        "attacks_away": 5,
                        "dangerous_home": 3,
                        "dangerous_away": 2,
                        "timestamp": _now,
                        "source": "hermes-smoke",
                    }
                ]
                env = route_corner_analysis(sample, fixture_id="hermes-supervisor-smoke")
                out["smoke_decision"] = getattr(env, "final_decision", None)
                out["smoke_ok"] = True
            except Exception as e:
                out["smoke_ok"] = False
                out["smoke_error"] = str(e)
        except Exception as e:
            out["import_error"] = str(e)
        return out

    def probe_gpu_ollama(self) -> Dict[str, Any]:
        """Diagnostico GPU/VRAM + modelos Ollama (Hermes AURA nao usa GPU; Ollama sim)."""
        info: Dict[str, Any] = {
            "note": "Hermes Supervisor AURA roda em CPU. VRAM e usada pelo Ollama ao carregar modelos.",
            "ollama_models": [],
            "gpu_hint": None,
        }
        tags = _http_get_json("http://127.0.0.1:11434/api/tags", timeout=3.0)
        if tags and isinstance(tags.get("models"), list):
            info["ollama_models"] = [
                m.get("name") or m.get("model") for m in tags["models"] if isinstance(m, dict)
            ]
        # nvidia-smi se existir
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                info["gpu_hint"] = r.stdout.strip().splitlines()[0]
        except Exception:
            info["gpu_hint"] = "nvidia-smi indisponivel (CPU only ou driver ausente)"
        if GPU_GOVERNOR is not None:
            try:
                info["governor"] = GPU_GOVERNOR.health(required_gb=1.5)
            except Exception as exc:
                info["governor"] = {"status": "ERROR", "message": str(exc)}
        return info

    def check_gpu_health(self) -> Dict[str, Any]:
        """Retorna saúde de VRAM sem iniciar, parar ou matar processos."""
        if GPU_GOVERNOR is None:
            return {"status": "UNAVAILABLE", "message": "GPU governor indisponível."}
        try:
            return GPU_GOVERNOR.health(required_gb=1.5)
        except Exception as exc:
            return {"status": "ERROR", "message": f"Falha ao checar GPU: {exc}"}

    # ---- diagnóstico -----------------------------------------------------
    def diagnose(
        self,
        services: Dict[str, Any],
        capture: Dict[str, Any],
        sync: Dict[str, Any],
        policy: Dict[str, Any],
        hermes: Dict[str, Any],
    ) -> List[Finding]:
        findings: List[Finding] = []

        # Serviços críticos
        if not (services.get("bridge") or {}).get("listening"):
            findings.append(
                Finding(
                    code="BRIDGE_DOWN",
                    severity="CRITICAL",
                    message="Bridge (8080) offline — sem feed e sem API de captura.",
                    action="Reiniciar Bridge: AURA_SUBIR_BRIDGE_VISIVEL.bat ou AURA_TUDO_EM_UM.bat",
                    auto_fixable=True,
                )
            )
        if not (services.get("engine") or {}).get("listening"):
            findings.append(
                Finding(
                    code="ENGINE_DOWN",
                    severity="CRITICAL",
                    message="Engine (8765) offline — Hermes e Operator não respondem.",
                    action="Reiniciar Engine: AURA_SUBIR_ENGINE_VISIVEL.bat ou AURA_TUDO_EM_UM.bat",
                    auto_fixable=True,
                )
            )
        if not (services.get("ollama") or {}).get("listening"):
            findings.append(
                Finding(
                    code="OLLAMA_DOWN",
                    severity="HIGH",
                    message="Ollama (11434) offline — chat IA e modelos locais indisponíveis.",
                    action="Inicie o app Ollama ou rode: ollama serve  |  depois: ollama pull llama3.2:3b",
                    auto_fixable=False,
                )
            )
        else:
            models = (hermes.get("gpu") or {}).get("ollama_models") or []
            if not models:
                findings.append(
                    Finding(
                        code="OLLAMA_NO_MODELS",
                        severity="HIGH",
                        message="Ollama online mas sem modelos — nada carregado em VRAM/RAM.",
                        action="ollama pull llama3.2:3b   (depois: ollama ps para ver se esta na GPU)",
                        auto_fixable=False,
                    )
                )
            gpu_hint = (hermes.get("gpu") or {}).get("gpu_hint")
            if gpu_hint:
                findings.append(
                    Finding(
                        code="GPU_INFO",
                        severity="INFO",
                        message=f"GPU: {gpu_hint}",
                        action="Modelos Ollama usam VRAM ao fazer ollama run / chat. Hermes AURA e CPU.",
                        auto_fixable=False,
                    )
                )
        if not (services.get("voice") or {}).get("listening"):
            findings.append(
                Finding(
                    code="VOICE_DOWN",
                    severity="MEDIUM",
                    message="Voice (8099) offline (não bloqueia paper).",
                    action="AURA_RUN_VOICE_SEGURO.bat",
                    auto_fixable=True,
                )
            )
        if not (services.get("dashboard") or {}).get("listening"):
            findings.append(
                Finding(
                    code="DASHBOARD_DOWN",
                    severity="LOW",
                    message="Dashboard (3000) offline.",
                    action="cd interface\\aura-quant-x-dashboard && ABRIR_INTERFACE.bat",
                    auto_fixable=False,
                )
            )

        # Captura / modo simulado
        if capture.get("no_feed") or capture.get("simulated_suspected"):
            findings.append(
                Finding(
                    code="CAPTURE_NO_FEED_OR_SIM",
                    severity="HIGH",
                    message=(
                        "Captura sem fixture ao vivo ou aparência de modo simulado. "
                        "Operator costuma mostrar FONTE AGUARDANDO FEED / NO_FEED."
                    ),
                    action=(
                        "1) Abra SokkerPRO na PANE DIREITA do Desktop (não no Chrome). "
                        "2) Login + partida AO VIVO. "
                        "3) Confirme rodapé: fila N - ok M - drop 0. "
                        "4) Se TOKEN vazio: AURA_INICIAR_SERVICOS_EMERGENCIA.bat e reabra o EXE."
                    ),
                    auto_fixable=False,
                    detail={
                        "home": capture.get("home"),
                        "away": capture.get("away"),
                        "source": capture.get("source"),
                        "token_empty": capture.get("token_empty"),
                    },
                )
            )
        if capture.get("token_empty"):
            findings.append(
                Finding(
                    code="CAPTURE_TOKEN_EMPTY",
                    severity="HIGH",
                    message="capture_forwarder.log indica TOKEN vazio — captura não encaminha.",
                    action="Rode AURA_INICIAR_SERVICOS_EMERGENCIA.bat e reabra Aura.QuantX.Desktop.exe",
                    auto_fixable=True,
                )
            )

        # Hermes pipeline
        if not hermes.get("import_ok"):
            findings.append(
                Finding(
                    code="HERMES_IMPORT_FAIL",
                    severity="CRITICAL",
                    message=f"Falha ao importar Hermes: {hermes.get('import_error')}",
                    action="Verifique engine/agents/aura_hermes_router.py e PYTHONPATH=raiz do AURA",
                    auto_fixable=False,
                )
            )
        elif hermes.get("smoke_ok") is False:
            findings.append(
                Finding(
                    code="HERMES_SMOKE_FAIL",
                    severity="HIGH",
                    message=f"Smoke Hermes falhou: {hermes.get('smoke_error')}",
                    action="Revise adapter/contracts e policy_runtime",
                    auto_fixable=False,
                )
            )
        elif hermes.get("primary") is False:
            findings.append(
                Finding(
                    code="HERMES_NOT_PRIMARY",
                    severity="MEDIUM",
                    message="hermes_primary=False na política — pipeline pode não usar Hermes como autoridade.",
                    action="Garanta hermes_primary=True em policy (padrão V26).",
                    auto_fixable=False,
                )
            )

        # Policy
        if policy.get("mode") == "paper":
            findings.append(
                Finding(
                    code="PAPER_ONLY_OK",
                    severity="INFO",
                    message="Sistema em paper-only (seguro). LIVE só com unlock triplo consciente.",
                    action="",
                    auto_fixable=False,
                )
            )

        if not findings:
            findings.append(
                Finding(
                    code="ALL_CLEAR",
                    severity="INFO",
                    message="Nenhum problema crítico detectado neste ciclo.",
                    action="",
                    auto_fixable=False,
                )
            )
        return findings

    # ---- ações seguras ---------------------------------------------------
    def apply_fixes(self, findings: List[Finding]) -> List[str]:
        """Auto-reparos seguros: só reinicia processos locais paper-trade.
        Nunca liga execução real, Telegram, nem apaga venv/banco.
        Cada ação é registada em actions_taken + hermes_repair_log.jsonl.
        """
        actions: List[str] = []
        if not self.auto_fix:
            return actions

        codes = {f.code for f in findings}
        repair_log = ROOT / "engine" / "data" / "hermes_repair_log.jsonl"
        repair_log.parent.mkdir(parents=True, exist_ok=True)

        def _log_repair(kind: str, detail: str, ok: bool) -> None:
            try:
                entry = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": kind,
                    "detail": detail,
                    "ok": ok,
                    "paper_trade": True,
                    "execution_allowed": False,
                }
                with repair_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

        # Bridge caído -> sobe só o Bridge
        if "BRIDGE_DOWN" in codes:
            ok = _run_python_module("bridge/server.py", ["--host", "127.0.0.1", "--port", "8080"])
            if not ok:
                ok = _run_bat_async("AURA_SUBIR_BRIDGE_VISIVEL.bat") or _run_bat_async("AURA_TUDO_EM_UM.bat")
            msg = "RESTART_BRIDGE: bridge/server.py :8080" if ok else "RESTART_BRIDGE_FAIL"
            actions.append(msg)
            _log_repair("RESTART_BRIDGE", msg, ok)
            if ok:
                for f in findings:
                    if f.code == "BRIDGE_DOWN":
                        f.fixed = True

        # Engine caído -> sobe só o Engine
        if "ENGINE_DOWN" in codes:
            ok = _run_python_module("engine/server.py", ["--host", "127.0.0.1", "--port", "8765"])
            if not ok:
                ok = _run_bat_async("AURA_SUBIR_ENGINE_VISIVEL.bat") or _run_bat_async("AURA_TUDO_EM_UM.bat")
            msg = "RESTART_ENGINE: engine/server.py :8765" if ok else "RESTART_ENGINE_FAIL"
            actions.append(msg)
            _log_repair("RESTART_ENGINE", msg, ok)
            if ok:
                for f in findings:
                    if f.code == "ENGINE_DOWN":
                        f.fixed = True

        # Voice caído -> sobe Voice (não crítico)
        if "VOICE_DOWN" in codes:
            ok = False
            for cand in ("bridge/jarvis_voice_server.py", "bridge/voice_server.py"):
                if (ROOT / cand).exists():
                    ok = _run_python_module(cand, ["--host", "127.0.0.1", "--port", "8099"])
                    if ok:
                        break
            if not ok:
                ok = _run_bat_async("AURA_RUN_VOICE_SEGURO.bat")
            msg = "RESTART_VOICE: :8099" if ok else "RESTART_VOICE_FAIL"
            actions.append(msg)
            _log_repair("RESTART_VOICE", msg, ok)
            if ok:
                for f in findings:
                    if f.code == "VOICE_DOWN":
                        f.fixed = True

        # Token de captura vazio -> só orienta + tenta BAT de emergencia se existir
        if "CAPTURE_TOKEN_EMPTY" in codes:
            if _run_bat_async("AURA_INICIAR_SERVICOS_EMERGENCIA.bat"):
                actions.append("REPAIR_CAPTURE_TOKEN: AURA_INICIAR_SERVICOS_EMERGENCIA.bat")
                _log_repair("REPAIR_CAPTURE_TOKEN", "emergency bat", True)
                for f in findings:
                    if f.code == "CAPTURE_TOKEN_EMPTY":
                        f.fixed = True
            else:
                actions.append("REPAIR_CAPTURE_TOKEN: BAT ausente — reabra Desktop para provisionar token")
                _log_repair("REPAIR_CAPTURE_TOKEN", "bat missing", False)

        return actions

    def build_recommendations(self, findings: List[Finding], capture: Dict[str, Any]) -> List[str]:
        recs: List[str] = []
        codes = {f.code for f in findings}

        if "OLLAMA_DOWN" in codes:
            recs.append("Instale/inicie Ollama (https://ollama.com) e rode: ollama pull llama3.2:3b")
            recs.append("Teste: AURA_OLLAMA_REST_TEST.bat")

        if "CAPTURE_NO_FEED_OR_SIM" in codes or capture.get("simulated_suspected"):
            recs.append(
                "Modo simulado / NO_FEED: SokkerPRO DEVE ficar na pane DIREITA do Desktop "
                "(não abra só no Chrome — a extensão/captura nativa não injeta lá)."
            )
            recs.append("Abra partida AO VIVO na pane direita e aguarde ok>0 no rodapé.")
            recs.append("Se persistir: limpe cache da toolbar do Desktop e reabra o fixture.")

        if "DASHBOARD_DOWN" in codes:
            recs.append("Dashboard opcional: interface\\aura-quant-x-dashboard\\ABRIR_INTERFACE.bat (Node 22+)")

        if "BRIDGE_DOWN" in codes or "ENGINE_DOWN" in codes:
            recs.append("Se BATs falharem, use AURA_SUBIR_BRIDGE_VISIVEL.bat e AURA_SUBIR_ENGINE_VISIVEL.bat e leia o log.")

        recs.append("Após correções: RODAR_TESTE_AUTOMATICO.bat")
        recs.append("Monitor contínuo: RODAR_MONITOR_CONTINUO_IA.bat")
        return recs

    def operator_message(self, report: SupervisorReport) -> str:
        crit = [f for f in report.findings if f.severity == "CRITICAL"]
        high = [f for f in report.findings if f.severity == "HIGH"]
        if crit:
            return (
                "HERMES SUPERVISOR - CRITICAL — "
                + "; ".join(f.message for f in crit[:3])
                + " | Ações: "
                + ("; ".join(report.actions_taken) or "intervenha manualmente")
            )
        if high:
            return (
                "HERMES SUPERVISOR - DEGRADED — "
                + "; ".join(f.message for f in high[:3])
                + " | Veja recomendações no relatório."
            )
        return "HERMES SUPERVISOR - HEALTHY — core e pipeline Hermes OK neste ciclo."

    # ---- ciclo principal -------------------------------------------------
    def run_cycle(self) -> SupervisorReport:
        services = self.probe_services()
        capture, sync = self.probe_capture_and_sync(services)
        policy = self.probe_policy()
        hermes = self.probe_hermes_pipeline()
        gpu = self.probe_gpu_ollama()
        gpu["governor_health"] = self.check_gpu_health()
        hermes["gpu"] = gpu
        findings = self.diagnose(services, capture, sync, policy, hermes)
        actions = self.apply_fixes(findings)
        recs = self.build_recommendations(findings, capture)

        severities = {f.severity for f in findings}
        if "CRITICAL" in severities:
            status = "CRITICAL"
        elif "HIGH" in severities:
            status = "DEGRADED"
        elif any(f.code != "ALL_CLEAR" and f.severity not in ("INFO", "LOW") for f in findings):
            status = "DEGRADED"
        else:
            status = "HEALTHY"

        report = SupervisorReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            status=status,
            findings=findings,
            actions_taken=actions,
            recommendations=recs,
            services={k: {"listening": v.get("listening"), "port": v.get("port")} for k, v in services.items()},
            capture={
                k: capture.get(k)
                for k in (
                    "has_live_fixture",
                    "home",
                    "away",
                    "minute",
                    "source",
                    "simulated_suspected",
                    "no_feed",
                    "token_empty",
                )
            },
            sync=sync,
            policy={k: policy.get(k) for k in ("mode", "paper_trade", "hermes_primary", "execution_allowed")},
        )
        report.message_for_operator = self.operator_message(report)
        self._persist(report)
        self._last_report = report
        return report

    def _persist(self, report: SupervisorReport) -> None:
        HERMES_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            HERMES_REPORT_PATH.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        try:
            with HERMES_HISTORY_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(report.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            pass
        # também alimenta system_health_feedback para a IA ler
        try:
            fb = {
                "status": report.status,
                "timestamp": report.timestamp,
                "source": "hermes_supervisor_agent",
                "errors": [f.message for f in report.findings if f.severity in ("CRITICAL", "HIGH")],
                "ok": [f.message for f in report.findings if f.severity == "INFO"],
                "message_for_ia": report.message_for_operator,
                "recommendations": report.recommendations,
                "actions_taken": report.actions_taken,
                "services": report.services,
                "capture": report.capture,
            }
            FEEDBACK_PATH.write_text(json.dumps(fb, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def run_loop(self, interval_sec: float = 30.0, max_cycles: Optional[int] = None) -> None:
        n = 0
        while True:
            report = self.run_cycle()
            _safe_print(
                f"[{report.timestamp}] HermesSupervisor status={report.status} "
                f"findings={len(report.findings)} actions={len(report.actions_taken)}"
            )
            _safe_print(f"  -> {report.message_for_operator}")
            for a in report.actions_taken:
                _safe_print(f"  [FIX] {a}")
            n += 1
            if max_cycles is not None and n >= max_cycles:
                break
            time.sleep(interval_sec)

    def format_for_chat(self, report: Optional[SupervisorReport] = None) -> str:
        r = report or self._last_report or self.run_cycle()
        lines = [
            f"**Hermes Supervisor** - {r.status}",
            f"UTC: {r.timestamp}",
            "",
            r.message_for_operator,
            "",
            "### Achados",
        ]
        for f in r.findings:
            mark = "[OK]" if f.fixed else "-"
            lines.append(f"{mark} [{f.severity}] {f.code}: {f.message}")
            if f.action:
                lines.append(f"    -> {f.action}")
        if r.actions_taken:
            lines.append("")
            lines.append("### Ações automáticas")
            for a in r.actions_taken:
                lines.append(f"- {a}")
        if r.recommendations:
            lines.append("")
            lines.append("### Recomendações")
            for rec in r.recommendations:
                lines.append(f"- {rec}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Integração com system_health_reader / chat
# ---------------------------------------------------------------------------
def get_hermes_supervisor_status() -> Dict[str, Any]:
    if HERMES_REPORT_PATH.exists():
        try:
            return json.loads(HERMES_REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return HermesSupervisor(auto_fix=False).run_cycle().to_dict()


def format_hermes_for_chat() -> str:
    return HermesSupervisor(auto_fix=False).format_for_chat()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Hermes Supervisor Agent — monitora e corrige AURA")
    p.add_argument("--once", action="store_true", help="Um ciclo e sai")
    p.add_argument("--loop", action="store_true", help="Loop contínuo")
    p.add_argument("--interval", type=float, default=30.0, help="Intervalo do loop (s)")
    p.add_argument("--no-fix", action="store_true", help="Só diagnostica, não aplica fix")
    p.add_argument("--max-cycles", type=int, default=None)
    args = p.parse_args(argv)

    agent = HermesSupervisor(auto_fix=not args.no_fix)
    if args.loop:
        agent.run_loop(interval_sec=args.interval, max_cycles=args.max_cycles)
        return 0

    report = agent.run_cycle()
    _safe_print(agent.format_for_chat(report))
    _safe_print(f"\nRelatorio: {HERMES_REPORT_PATH}")
    return 0 if report.status in ("HEALTHY", "DEGRADED") else 1




# --- PASCHE hermes_melhorias_v25q ---
# ============================================================
# MELHORIAS V25Q — Injetar no topo de engine/agents/hermes_supervisor_agent.py
# ============================================================
import os

def detect_gpu_profile() -> dict:
    """Detecta GPU NVIDIA e retorna perfil recomendado."""
    try:
        import subprocess
        result = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            gpus = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            mem = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            vrams = [int(line.strip()) for line in mem.stdout.strip().split("\n") if line.strip().isdigit()] if mem.returncode == 0 else []
            total_vram = sum(vrams) if vrams else 0
            return {
                "detected": True,
                "gpu_count": len(gpus),
                "total_vram_mb": total_vram,
                "profile": "full" if total_vram >= 8192 else "limited" if total_vram >= 4096 else "cpu",
                "num_gpu": 99 if total_vram >= 8192 else 50 if total_vram >= 4096 else 0,
            }
    except Exception:
        pass
    return {"detected": False, "profile": "cpu", "num_gpu": 0, "reason": "nvidia-smi indisponivel"}


def health_check_with_retry(url: str, retries: int = 3, timeout: int = 5) -> dict:
    """Health check com retry exponencial."""
    import time
    for attempt in range(retries):
        try:
            import requests
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return {"healthy": True, "attempt": attempt + 1, "latency_ms": int(r.elapsed.total_seconds() * 1000)}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"healthy": False, "attempt": attempt + 1, "error": str(e)}
    return {"healthy": False, "error": "max_retries_exceeded"}


def structured_log(level: str, component: str, message: str, extra: dict = None):
    import json
    from datetime import datetime
    entry = {
        "timestamp": datetime.now().isoformat(),
        "level": level,
        "component": component,
        "message": message,
        "paper_trade": os.environ.get("PAPER_TRADE", "true"),
        "execution_allowed": os.environ.get("EXECUTION_ALLOWED", "false"),
    }
    if extra:
        entry.update(extra)
    print(json.dumps(entry, ensure_ascii=False))

# ============================================================
# USO: Cole estas funcoes no topo do arquivo hermes_supervisor_agent.py
# antes do bloco 'if __name__ == "__main__":'
# ============================================================


if __name__ == "__main__":
    raise SystemExit(main())
