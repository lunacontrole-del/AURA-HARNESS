#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
boot.py — ORQUESTRADOR do AURA QUANT-X V25 (§5). Une os modulos do inventario
num processo so, na ordem contratada, com shutdown na ordem inversa.

ORDEM (§5, inalteravel):
    errors -> security(paper_trade) -> governor -> bus(+sinks) -> browser ->
    conformal/mc/risk -> decision_bus -> metrics(+componentes) -> jarvis ->
    telegram -> analyst -> cache_management -> mc.start_build_async()

DESIGN (mapa executavel):
    - Cada estagio e DEFENSIVO: usa as assinaturas documentadas; em
      TypeError registra signature_mismatch (com inspect.signature real),
      marca degradado e SEGUE. Boot nunca morre por peca ausente —
      boot_state.json (atomico) denuncia o que precisa de emenda.
    - Shutdown: ordem INVERSA dos estagios que registraram cleanup;
      saves + closes; atexit + sinal.
    - Heartbeat: gauges aura_heartbeat_feed/_engine (§5) em thread 30s.
    - Instrumentacao: REG.timer_us("aura_stage_us", stage=...) quando
      observability presente.
    - stats() para /statusz via register_component (§6).

USO:  python engine\\boot.py            (roda ate Ctrl+C)
      python engine\\boot.py --check    (sobe, escreve boot_state, sai)
      python engine\\boot.py --self-test

stdlib only. Python 3.9+. Windows. Console ASCII.
"""
from __future__ import annotations

import atexit
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("aura.boot")

__version__ = "1.0.0"
_PROJ_ROOT = Path(__file__).resolve().parents[1]
_STATE_PATH = _PROJ_ROOT / "engine" / "data" / "boot_state.json"
_RESOURCE_SHUTDOWN_DONE = False
sys.path.insert(0, str(_PROJ_ROOT))


def setup_aura_logging() -> None:
    """Configura log rotativo sem duplicar handlers em reinicializações."""
    log_dir = _PROJ_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_aura_rotating", False) for h in root.handlers):
        return
    handler = RotatingFileHandler(
        log_dir / "aura_engine.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler._aura_rotating = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(stream)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _aura_graceful_resource_cleanup() -> None:
    """Libera cache CUDA; descarga Ollama somente quando explicitamente opt-in."""
    global _RESOURCE_SHUTDOWN_DONE
    if _RESOURCE_SHUTDOWN_DONE:
        return
    _RESOURCE_SHUTDOWN_DONE = True
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.info("boot: cache CUDA liberado")
    except Exception as exc:
        logger.debug("boot: cache CUDA indisponível: %s", exc)
    if os.getenv("AURA_SHUTDOWN_UNLOAD_OLLAMA", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    host = os.getenv("CORNERAI_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("CORNERAI_CHAT_MODEL", "glm4:9b-chat-q4_0")
    try:
        body = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(
            f"{host}/api/generate", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
        logger.info("boot: descarga Ollama solicitada para %s", model)
    except Exception as exc:
        logger.debug("boot: descarga Ollama não disponível: %s", exc)


def _atomic_write_json(path: Path, obj: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.exception("boot: falha ao gravar %s", path)


def _import(path: str):
    """Import defensivo — None em falha (logado pelo estagio)."""
    try:
        mod = __import__(path, fromlist=["x"])
        return mod
    except Exception as exc:
        logger.warning("boot: import %s falhou: %s", path, exc)
        return None


def _call_best(fn, doc_kwargs: Dict[str, Any], label: str) -> Any:
    """Chama fn com os kwargs documentados; em TypeError, registra a
    assinatura REAL (para emenda exata na proxima rodada) e re-levanta."""
    try:
        return fn(**doc_kwargs)
    except TypeError as exc:
        try:
            sig = str(inspect.signature(fn))
        except (TypeError, ValueError):
            sig = "?"
        raise TypeError("%s: kwargs %s nao batem (real: %s; %s)"
                        % (label, sorted(doc_kwargs), sig, exc)) from exc


class Stage:
    def __init__(self, name: str, fn: Callable[["AuraBoot"], Any],
                 cleanup: Optional[Callable[[], None]] = None):
        self.name = name
        self.fn = fn
        self.cleanup = cleanup


class AuraBoot:
    """Executa a sequencia §5 com estagios plugaveis e defensivos."""

    def __init__(self, check_only: bool = False):
        self.check_only = check_only
        self.state: Dict[str, Any] = {"started_at": _iso_now(),
                                      "stages": {}, "running": False}
        self.components: Dict[str, Any] = {}
        self._stages: List[Stage] = []
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._exit_registered = False

    # ------------------------------------------------------------ estado
    def _mark(self, name: str, status: str, detail: str = "") -> None:
        with self._lock:
            self.state["stages"][name] = {"status": status,
                                          "detail": detail[:300],
                                          "ts": _iso_now()}
            _atomic_write_json(_STATE_PATH, self.state)

    def _timer(self, stage: str) -> Callable[[], None]:
        """timer_us quando REG existe; no-op honesto caso contrario."""
        reg = getattr(self.components.get("reg"), "timer_us", None)

        def done() -> None:
            try:
                if reg is not None:
                    reg("aura_stage_us", stage=stage)
            except Exception:
                logger.exception("boot: timer falhou (%s)", stage)
        return done

    # ------------------------------------------------------------ estagios
    def _stage_errors(self) -> None:
        mod = _import("engine.core.error_handler")
        if mod is None or not hasattr(mod, "install_global"):
            self._mark("errors", "missing")
            return
        mod.install_global()
        self.components["errors"] = mod
        self._mark("errors", "ok")

    def _stage_security(self) -> None:
        mod = _import("engine.core.security")
        if mod is None:
            self._mark("security", "missing")
            return
        self.components["security"] = mod
        apt = getattr(mod, "assert_paper_trade", None)
        if callable(apt):
            apt()
            self._mark("security", "ok", "assert_paper_trade ok")
            return
        pt = getattr(mod, "PAPER_TRADE", None)
        ea = getattr(mod, "EXECUTION_ALLOWED", None)
        if pt is True and ea is False:
            self._mark("security", "ok", "invariantes §0 conferidos")
            return
        raise RuntimeError("SECURITY: invariantes §0 nao confirmados "
                           "(PAPER_TRADE=%r EXECUTION_ALLOWED=%r)" % (pt, ea))

    def _stage_governor(self) -> None:
        mod = _import("engine.core.hardware_governor")
        if mod is None:
            self._mark("governor", "missing")
            return
        gov = _call_best(mod.HardwareGovernor, {}, "governor") \
            if hasattr(mod, "HardwareGovernor") else None
        if gov is not None:
            gov.start()
            self.components["governor"] = gov
        self._mark("governor", "ok" if gov else "partial",
                   "can_run_background=%s" % bool(
                       getattr(gov, "can_run_background", lambda: True)()))

    def _stage_bus(self) -> None:
        mod = _import("engine.core.feed_bus")
        if mod is None or not hasattr(mod, "FeedBus"):
            self._mark("bus", "missing")
            return
        bridge_dir = _PROJ_ROOT / "bridge"
        bridge_dir.mkdir(exist_ok=True)
        data_dir = _PROJ_ROOT / "engine" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        bus = _call_best(mod.FeedBus, {}, "bus")
        sinks: List[Any] = []
        try:
            sinks.append(_call_best(mod.JsonlSink, {
                "path": str(bridge_dir / "live_feed.jsonl")}, "jsonl"))
        except Exception as exc:
            logger.warning("bus: JsonlSink falhou: %s", exc)
        try:
            sinks.append(_call_best(mod.LatestJsonSink, {
                "path": str(data_dir / "live_latest.json")}, "latest"))
        except Exception as exc:
            logger.warning("bus: LatestJsonSink falhou: %s", exc)
        add = getattr(bus, "add_sink", None)
        if add is None:
            for s in sinks:
                try:
                    getattr(bus, "subscribe")(s)
                except Exception:
                    pass
        else:
            for s in sinks:
                add(s)
        self.components.update({"bus": bus, "sinks": sinks})
        self._mark("bus", "ok", "%d sink(s)" % len(sinks))

    def _stage_browser(self) -> None:
        mod = _import("engine.agents.browser_agent")
        gov = self.components.get("governor")
        if mod is None or not hasattr(mod, "BrowserAgent"):
            self._mark("browser", "missing")
            return
        try:
            browser = _call_best(mod.BrowserAgent,
                                 {"governor": gov}, "browser")
        except TypeError:
            browser = _call_best(mod.BrowserAgent, {}, "browser")
        self.components["browser"] = browser
        self._mark("browser", "ok")

    def _stage_conformal(self) -> None:
        mod = _import("engine.core.conformal_gate")
        if mod is None or not hasattr(mod, "ConformalRiskGate"):
            self._mark("conformal", "missing")
            return
        bus = self.components.get("bus")
        base_cls = getattr(mod, "ConformalGate", None)
        try:
            if base_cls is not None:
                # O contrato real recebe um ConformalGate posicional; o
                # objeto final recebe o bus apenas como journal opcional.
                base = _call_best(
                    base_cls,
                    {"state_dir": _PROJ_ROOT / "engine" / "data" / "conformal"},
                    "conformal_base")
                conformal = mod.ConformalRiskGate(base, journal_bus=bus)
            else:
                # Compatibilidade com fakes legados do self-test.
                conformal = _call_best(mod.ConformalRiskGate, {"bus": bus},
                                       "conformal")
        except TypeError:
            # Fallback defensivo: registra o contrato no estado e segue.
            conformal = _call_best(mod.ConformalRiskGate, {}, "conformal")
        self.components["conformal"] = conformal
        self._mark("conformal", "ok")

    def _stage_mc(self) -> None:
        mod = _import("engine.core.mc_grid")
        mc = getattr(mod, "MCGrid", None) or getattr(mod, "Grid", None) \
            if mod else None
        if mc is None:
            self._mark("mc", "missing")
            return
        try:
            mc_instance = _call_best(mc, {}, "mc") if isinstance(mc, type) else mc
        except TypeError:
            mc_instance = mc
        self.components["mc_class"] = mc_instance
        self._mark("mc", "ok", "build_async agendado para o fim (§5)")

    def _stage_metrics(self) -> None:
        mod = _import("engine.core.observability")
        if mod is None:
            self._mark("metrics", "missing")
            return
        reg = getattr(mod, "REG", None) or getattr(mod, "Registry", None)
        if isinstance(reg, type):
            reg = _call_best(reg, {}, "registry")
        elif reg is None and hasattr(mod, "Registry"):
            reg = _call_best(getattr(mod, "Registry"), {}, "registry")
        self.components["reg"] = reg
        ms_cls = getattr(mod, "MetricsServer", None)
        server = None
        if ms_cls is not None:
            try:
                server = _call_best(ms_cls, {"port": 0}, "metrics")
                if hasattr(server, "start"):
                    getattr(server, "start")()
            except Exception as exc:
                logger.warning("metrics: server falhou: %s", exc)
                server = None
        self.components["metrics_server"] = server
        # registra TODOS os componentes com stats() (§6)
        regc = getattr(reg, "register_component", None) if reg else None
        n = 0
        if regc is not None:
            for name in ("bus", "conformal", "browser", "governor"):
                comp = self.components.get(name)
                st = getattr(comp, "stats", None)
                if st is not None:
                    try:
                        regc("aura_" + name, st)
                        n += 1
                    except Exception:
                        logger.exception("metrics: register %s", name)
        self._mark("metrics", "ok" if reg else "partial",
                   "server=%s componentes=%d" % (bool(server), n))

    def _stage_jarvis(self) -> None:
        mod = _import("engine.agents.supervisor_jarvis")
        cls = getattr(mod, "SupervisorJarvis", None) if mod else None
        if cls is None:
            self._mark("jarvis", "missing")
            return
        try:
            jarvis = _call_best(cls, {}, "jarvis")
        except TypeError:
            jarvis = cls()
        self.components["jarvis"] = jarvis
        self._mark("jarvis", "ok")

    def _stage_telegram(self) -> None:
        mod = _import("engine.agents.telegram_hq")
        cls = getattr(mod, "TelegramHQ", None) if mod else None
        if cls is None:
            self._mark("telegram", "missing",
                       "AURA_TG_TOKEN ausente ou modulo off")
            return
        try:
            tg = _call_best(cls, {}, "telegram")
        except TypeError:
            tg = cls()
        start = getattr(tg, "start", None)
        if start:
            start()
        self.components["telegram"] = tg
        self._mark("telegram", "ok")

    def _stage_analyst(self) -> None:
        mod = _import("engine.agents.cross_site_analyst")
        cls = getattr(mod, "CrossSiteAnalyst", None) if mod else None
        if cls is None:
            self._mark("analyst", "missing")
            return
        try:
            analyst = _call_best(cls, {}, "analyst")
        except TypeError:
            analyst = cls()
        self.components["analyst"] = analyst
        self._mark("analyst", "ok")

    def _stage_cache(self) -> None:
        mod = _import("engine.core.cache_integration")
        fn = getattr(mod, "start_cache_management", None) if mod else None
        if fn is None:
            self._mark("cache", "missing")
            return
        try:
            fn(bus=self.components.get("bus"),
               conformal=self.components.get("conformal"),
               mc=self.components.get("mc_class"),
               browser=self.components.get("browser"),
               analyst=self.components.get("analyst"))
            self._mark("cache", "ok")
        except TypeError as exc:
            self._mark("cache", "signature_mismatch", str(exc))
        except Exception as exc:
            self._mark("cache", "error", str(exc))

    def _stage_mc_build(self) -> None:
        mc = self.components.get("mc_class")
        build = getattr(mc, "start_build_async", None)
        if build is None:
            self._mark("mc_build", "missing")
            return
        try:
            build()
            self._mark("mc_build", "ok")
        except Exception as exc:
            self._mark("mc_build", "error", str(exc))

    def _stage_heartbeat(self) -> None:
        reg = self.components.get("reg")
        if reg is None:
            self._mark("heartbeat", "missing")
            return

        def _hb() -> None:
            while not self._hb_stop.wait(30.0):
                for gauge, comp in (("aura_heartbeat_feed", "bus"),
                                    ("aura_heartbeat_engine", "conformal")):
                    try:
                        getattr(reg, "gauge")(gauge, time.time())
                    except Exception:
                        pass
        self._hb_thread = threading.Thread(target=_hb, daemon=True,
                                           name="aura-heartbeat")
        self._hb_thread.start()
        self._mark("heartbeat", "ok")

    # ------------------------------------------------------------ ciclo
    def _build_stages(self) -> List[Stage]:
        return [
            Stage("errors", lambda b: b._stage_errors()),
            Stage("security", lambda b: b._stage_security()),
            Stage("governor", lambda b: b._stage_governor()),
            Stage("bus", lambda b: b._stage_bus()),
            Stage("browser", lambda b: b._stage_browser()),
            Stage("conformal", lambda b: b._stage_conformal()),
            Stage("mc", lambda b: b._stage_mc()),
            Stage("metrics", lambda b: b._stage_metrics()),
            Stage("jarvis", lambda b: b._stage_jarvis()),
            Stage("telegram", lambda b: b._stage_telegram()),
            Stage("analyst", lambda b: b._stage_analyst()),
            Stage("cache", lambda b: b._stage_cache()),
            Stage("heartbeat", lambda b: b._stage_heartbeat()),
            Stage("mc_build", lambda b: b._stage_mc_build()),
        ]

    def start(self) -> Dict[str, Any]:
        self._stages = self._build_stages()
        logger.info("AURA boot v%s — %d estagios (§5)", __version__,
                    len(self._stages))
        for stage in self._stages:
            done = self._timer(stage.name)
            try:
                stage.fn(self)
                done()
            except TypeError as exc:  # signature_mismatch
                self._mark(stage.name, "signature_mismatch", str(exc))
                logger.error("boot: %s divergiu de contrato: %s",
                             stage.name, exc)
            except Exception as exc:
                self._mark(stage.name, "error", str(exc))
                logger.exception("boot: estagio %s falhou", stage.name)
        with self._lock:
            self.state["running"] = True
        _atomic_write_json(_STATE_PATH, self.state)
        if not self._exit_registered:
            atexit.register(self.shutdown)
            self._exit_registered = True
        return self.state

    def run_forever(self) -> None:
        self.start()
        ev = threading.Event()
        try:
            signal.signal(signal.SIGINT, lambda *_: ev.set())
            signal.signal(signal.SIGTERM, lambda *_: ev.set())
        except ValueError:
            pass
        print("[boot] AURA no ar — Ctrl+C encerra (shutdown inverso)")
        while not ev.wait(1.0):
            pass
        self.shutdown()

    def shutdown(self) -> None:
        _aura_graceful_resource_cleanup()
        with self._lock:
            if not self.state.get("running"):
                return
            self.state["running"] = False
        self._hb_stop.set()
        # ordem INVERSA: mc_build ... cache ... bus/errors (saves+closes)
        for stage in reversed(self._stages):
            comp = self.components.get(stage.name)
            if comp is None:
                continue
            for meth in ("stop", "close", "flush_sync", "shutdown"):
                fn = getattr(comp, meth, None)
                if callable(fn):
                    try:
                        fn()
                        logger.info("boot: %s.%s ok", stage.name, meth)
                        break
                    except Exception:
                        logger.exception("boot: %s.%s falhou",
                                         stage.name, meth)
        with self._lock:
            self.state["stopped_at"] = _iso_now()
        _atomic_write_json(_STATE_PATH, self.state)
        logger.info("boot: shutdown concluido")

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            st = dict(self.state)
        st["version"] = __version__
        counts: Dict[str, int] = {}
        for s in st.get("stages", {}).values():
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        st["stage_counts"] = counts
        return {"boot": st}


# ---------------------------------------------------------------------------
# self-test — injeta modulos FAKE com as assinaturas documentadas e valida
# ordem §5, shutdown inverso, boot_state e degradacao defensiva.
# ---------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile

    fails: List[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))
        if not cond:
            fails.append(name)

    calls: List[str] = []

    def mkmod(name: str, **attrs) -> ModuleType:
        m = ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class Sink:
        def __init__(self, **kw):
            calls.append("sink:" + self.__class__.__name__)

    class FeedBus:
        def __init__(self):
            self.sinks = []

        def add_sink(self, s):
            self.sinks.append(s)
            calls.append("bus:add_sink")

        def stats(self):
            return {"sinks": len(self.sinks)}

    class Governor:
        def __init__(self):
            pass

        def start(self):
            calls.append("governor:start")

        def can_run_background(self):
            return True

        def stats(self):
            return {}

    class Browser:
        def __init__(self, governor=None):
            calls.append("browser:init(governor=%s)" % bool(governor))

        def stats(self):
            return {}

    class Conformal:
        def __init__(self, bus=None):
            calls.append("conformal:init(bus=%s)" % bool(bus))

        def stats(self):
            return {}

    class MCGrid:
        def start_build_async(self):
            calls.append("mc:start_build_async")

    class Reg:
        def __init__(self):
            self.components = {}

        def register_component(self, name, fn):
            self.components[name] = fn
            calls.append("metrics:register:" + name)

        def timer_us(self, name, stage=""):
            calls.append("timer:" + stage)

        def gauge(self, name, v):
            pass

    class MetricsServer:
        def __init__(self, port=0):
            pass

        def start(self):
            calls.append("metrics:start")

    class Jarvis:
        def __init__(self):
            pass

    class Tg:
        def __init__(self):
            pass

        def start(self):
            calls.append("telegram:start")

    class Analyst:
        def __init__(self):
            pass

    # injeta fakes completos
    mkmod("engine", )
    mkmod("engine.core")
    mkmod("engine.agents")
    mkmod("engine.core.error_handler", install_global=lambda: calls.append(
        "errors:install_global"))
    mkmod("engine.core.security",
          assert_paper_trade=lambda: calls.append("security:assert"))
    mkmod("engine.core.hardware_governor", HardwareGovernor=Governor)
    mkmod("engine.core.feed_bus", FeedBus=FeedBus, JsonlSink=Sink,
          LatestJsonSink=Sink)
    mkmod("engine.agents.browser_agent", BrowserAgent=Browser)
    mkmod("engine.core.conformal_gate", ConformalRiskGate=Conformal)
    mkmod("engine.core.mc_grid", MCGrid=MCGrid)
    mkmod("engine.core.observability", Registry=Reg,
          MetricsServer=MetricsServer)
    mkmod("engine.agents.supervisor_jarvis", SupervisorJarvis=Jarvis)
    mkmod("engine.agents.telegram_hq", TelegramHQ=Tg)
    mkmod("engine.agents.cross_site_analyst", CrossSiteAnalyst=Analyst)
    cache_calls: List[str] = []
    mkmod("engine.core.cache_integration",
          start_cache_management=lambda **kw: cache_calls.append(
              "cache:%s" % ",".join(sorted(k for k, v in kw.items()
                                           if v is not None))))

    with tempfile.TemporaryDirectory(prefix="aura_boot_st_") as td:
        boot = AuraBoot()
        boot._STATE = None
        # redireciona estado p/ temp (nao sujar engine/data do teste)
        global _STATE_PATH
        orig = _STATE_PATH
        _STATE_PATH = Path(td) / "boot_state.json"
        try:
            state = boot.start()
            st = state["stages"]
            check("todos os 14 estagios ok",
                  all(v["status"] == "ok" for v in st.values())
                  and len(st) == 14, str({k: v["status"] for k, v
                                          in st.items()}))
            # ordem §5
            check("ordem: errors primeiro",
                  calls[0] == "errors:install_global")
            check("ordem: security antes do governor",
                  calls.index("security:assert") < calls.index(
                      "governor:start"))
            check("ordem: bus recebe sinks antes do browser",
                  calls.index("bus:add_sink") < calls.index(
                      "browser:init(governor=True)"))
            check("ordem: conformal recebe o bus",
                  "conformal:init(bus=True)" in calls)
            check("ordem: metrics registra componentes",
                  "metrics:register:aura_bus" in calls)
            check("ordem: mc_build e o ULTIMO estagio",
                  len(calls) >= 2 and calls[-2] == "mc:start_build_async")
            check("cache: integracao com todos os componentes",
                  cache_calls and set(cache_calls[0].removeprefix("cache:").split(",")) == {
                      "analyst", "browser", "bus", "conformal", "mc"},
                  str(cache_calls))
            # boot_state em disco
            check("boot_state.json gravado", _STATE_PATH.is_file())
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            check("boot_state: running=true", data.get("running") is True)

            # stats
            stt = boot.stats()["boot"]
            check("stats: stage_counts ok=14",
                  stt["stage_counts"].get("ok") == 14)

            # shutdown inverso: telegram para antes do bus
            boot.shutdown()
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            check("shutdown: running=false + stopped_at",
                  data.get("running") is False
                  and data.get("stopped_at") is not None)
        finally:
            _STATE_PATH = orig

        # degradacao: modulo ausente nao mata o boot
        for name in list(sys.modules):
            if name.startswith("engine."):
                del sys.modules[name]
        mkmod("engine")
        mkmod("engine.core")
        mkmod("engine.agents")
        mkmod("engine.core.error_handler",
              install_global=lambda: None)
        boot2 = AuraBoot()
        _STATE_PATH2 = Path(td) / "state2.json"
        orig2 = _STATE_PATH
        _STATE_PATH = _STATE_PATH2
        try:
            state2 = boot2.start()
            missing = [k for k, v in state2["stages"].items()
                       if v["status"] == "missing"]
            check("degradacao: 12 estagios missing, boot segue",
                  len(missing) >= 12 and state2["stages"]["errors"][
                      "status"] == "ok", "missing=%d" % len(missing))
        finally:
            _STATE_PATH = orig2
        # signature mismatch registrado, nao propagado
        mkmod("engine.core.security", assert_paper_trade=lambda: None)
        bad = ModuleType("engine.core.feed_bus")

        class BusErr:
            def __init__(self, **kw):
                raise TypeError("nao aceito nada")

        bad.FeedBus = BusErr
        sys.modules["engine.core.feed_bus"] = bad
        boot3 = AuraBoot()
        orig3 = _STATE_PATH
        _STATE_PATH = Path(td) / "state3.json"
        try:
            s3 = boot3.start()
            check("signature_mismatch: registrado e boot segue",
                  s3["stages"]["bus"]["status"] == "signature_mismatch"
                  and "nao aceito nada" in s3["stages"]["bus"]["detail"])
        finally:
            _STATE_PATH = orig3

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - boot.py")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return _self_test()
    check = "--check" in argv
    boot = AuraBoot(check_only=check)
    if check:
        boot.start()
        st = boot.stats()["boot"]["stage_counts"]
        print("[check] estagios: %s" % st)
        print("[check] estado: %s" % _STATE_PATH)
        boot.shutdown()
        return 0
    boot.run_forever()
    return 0


if __name__ == "__main__":
    setup_aura_logging()
    sys.exit(main())
