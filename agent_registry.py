"""Catalogo operacional seguro dos agentes do AURA QUANT-X.

O modulo separa descoberta estÃ¡tica de execucao. Nenhuma funcao e importada
ou executada apenas por aparecer no filesystem; execucao exige uma entrada
explicitamente allowlisted neste arquivo e permanece em paper trade.
"""
from __future__ import annotations

# V23 BLOCO 5: cache AST por (path, mtime)
import functools
try:
    import ast as _ast_mod
except Exception:
    _ast_mod = None

@functools.lru_cache(maxsize=64)
def get_agent_static_summary(agent_path: str, mtime: float):
    """Parse AST only when file mtime changes."""
    if _ast_mod is None:
        return {"path": agent_path, "mtime": mtime}
    try:
        with open(agent_path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = _ast_mod.parse(src)
        funcs = [n.name for n in tree.body if isinstance(n, _ast_mod.FunctionDef)]
        classes = [n.name for n in tree.body if isinstance(n, _ast_mod.ClassDef)]
        return {"path": agent_path, "mtime": mtime, "functions": funcs[:40], "classes": classes[:20]}
    except Exception as e:
        return {"path": agent_path, "mtime": mtime, "error": str(e)}


import ast
import asyncio
import dataclasses
import importlib
import inspect
import threading
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "agents" / "activation_manifest.json"
for _path in (
    ROOT,
    ROOT / "engine",
    ROOT / "bridge",
    ROOT / "engine" / "reliability",
    ROOT / "engine" / "improvements",
    ROOT / "engine" / "modules",
    ROOT / "bridge" / "cognitive",
    ROOT / "bridge" / "jarvis",
    ROOT / "bridge" / "jarvis" / "modules",
    ROOT / "bridge" / "telegram",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Apenas funcoes puras/diagnosticas ou contratos em paper trade entram aqui.
# O restante continua disponivel no menu para inspecao, mas nao e executado
# automaticamente por seguranca.
RUNNABLE_FUNCTIONS: Dict[str, set[str]] = {
    "engine:corner_intelligence.py": {"analyze_corners", "analyst_brief", "count_since", "p_at_least_one"},
    "engine:hawkes_corners.py": {"p_at_least_one", "classify_event", "build_hawkes_from_payload"},
    "engine:market_edge.py": {"normalize_market_name", "classify_market", "parse_odds_ts", "odds_age_sec", "compute_edge"},
    "engine:voice_planner.py": {"plan_segments", "build_ssml", "narrative_from_card", "apply_pronunciation", "strip_forbidden_fillers"},
    "engine:gpu_resource_manager.py": {"cuda_info", "resolve_cuda_device", "recommended_voice_llm", "status"},
    "engine:data_veracity.py": {"verify_payload"},
    "engine:model_shadow.py": {"poisson_at_least_one", "baseline_from_payload", "build_feature_vector", "shadow_predict", "validate"},
    "engine:backtest_engine.py": {"run_backtest"},
    "engine:digital_twin_monte_carlo.py": {"run_corner_probability"},
    "engine:execution_router.py": {"execute"},
    "engine:health_score.py": {"record", "score"},
    "engine:drift_monitor.py": {"update", "missing_rate", "recent_alerts"},
    "bridge:server.py": {"extract_match_view", "fingerprint", "window_label", "last_corner_gap", "view_to_skill_pack", "validate_skill_pack"},
    "bridge:neural_tts.py": {"sanitize_for_speech", "cache_stats", "available"},
    "bridge:gpu_resource_manager.py": {"device_info", "sample_vram", "vram_snapshot", "cleanup", "profile"},
    "bridge:multi_llm_router.py": {"extract_intent"},
    "bridge:state_vector_daemon.py": {"get_system_state", "get_semantic_graph"},
    "sre:omnipotent_health_profiler": {"run_full_diagnostic", "check_memory_fragmentation", "check_clock_skew", "check_schema_drift", "check_vram_fragmentation", "predict_time_to_failure", "check_phase_space", "analyze_traces"},
    "engine:engine_core.py": {"backend_info", "init_db_wal"},
    "engine:auto_calibrate_risk.py": {"maybe_auto_calibrate"},
    "engine:server_gpu_master.py": {"get_analysis", "get_skill_formatted_context", "health", "status"},
    "engine:server_elite_gpu.py": {"health"},
    "engine:aura_quant_x_master.py": {"health"},
    "bridge:jarvis_voice_server.py": {"health", "voice_diagnostic"},
    "bridge:jarvis_cognitive_server.py": {"api_state", "health"},
    "bridge:emergency_bridge.py": {"now"},
    "engine:server.py": {"api_status", "deep_diagnostic", "performance_diagnostic"},

}


def _load_manifest() -> Dict[str, Any]:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"version": "unknown", "agents": {}, "error": str(exc)}


def _display_name(agent_id: str, path: str) -> str:
    raw = agent_id.split(":", 1)[-1]
    raw = Path(raw).stem.replace("_", " ")
    return " ".join(part.capitalize() for part in raw.split())


def _source_state(path_text: str) -> Dict[str, Any]:
    source = ROOT / path_text
    enabled_copy = ROOT / "agents" / "ENABLED" / (Path(path_text).name + ".enabled")
    if source.is_file():
        return {"exists": True, "kind": "source", "path": str(source.relative_to(ROOT)).replace("\\", "/")}
    if enabled_copy.is_file():
        return {"exists": True, "kind": "enabled_copy", "path": str(enabled_copy.relative_to(ROOT)).replace("\\", "/")}
    return {"exists": False, "kind": "missing", "path": path_text}


def _actions(agent_id: str, layer: str, function_count: int) -> List[str]:
    """Meta-actions always available; function names appended by catalog (F14)."""
    actions = ["status", "inspect", "glm_review"]
    if function_count:
        actions.append("run_function")
    if agent_id == "engine:aura_director_agent.py":
        actions.append("pending")
    if agent_id == "bridge:jarvis_voice_server.py" or layer == "voice":
        actions.append("voice_diagnostic")
    if layer in {"engine", "bridge", "sre"}:
        actions.append("health")
    if agent_id == "engine:execution_router.py":
        actions.append("paper_preview")
    if agent_id == "engine:digital_twin_monte_carlo.py":
        actions.append("simulation_contract")
    return list(dict.fromkeys(actions))


def _signature(node: ast.AST) -> str:
    try:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
            ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
            return f"{prefix}def {node.name}({ast.unparse(node.args)}){ret}"
        return ast.unparse(node)
    except Exception:
        return getattr(node, "name", "callable")


def _static_summary(file_path: Path) -> Dict[str, Any]:
    if not file_path.is_file():
        return {"source_exists": False, "functions": [], "function_details": [], "classes": [], "preview": ""}
    text = file_path.read_text(encoding="utf-8", errors="replace")
    if file_path.suffix.lower() != ".py":
        return {
            "source_exists": True,
            "asset_type": file_path.suffix.lower().lstrip(".") or "text",
            "bytes": len(text.encode("utf-8")),
            "functions": [],
            "function_details": [],
            "classes": [],
            "preview": "\n".join(text.splitlines()[:24]),
        }
    try:
        tree = ast.parse(text, filename=str(file_path))
    except SyntaxError as exc:
        return {
            "source_exists": True,
            "bytes": len(text.encode("utf-8")),
            "functions": [],
            "function_details": [],
            "classes": [],
            "preview": "\n".join(text.splitlines()[:24]),
            "syntax_error": f"{exc.msg} at line {exc.lineno}",
        }
    details: List[Dict[str, Any]] = []
    classes: List[str] = []

    def visit(body: list[ast.stmt], scope: str = "") -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                classes.append(node.name if not scope else f"{scope}.{node.name}")
                visit(node.body, f"{scope}.{node.name}" if scope else node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{scope}.{node.name}" if scope else node.name
                details.append({
                    "name": node.name,
                    "qualified_name": qualified,
                    "scope": scope or "module",
                    "signature": _signature(node),
                    "top_level": not bool(scope),
                    "async": isinstance(node, ast.AsyncFunctionDef),
                })
    visit(tree.body)
    return {
        "source_exists": True,
        "bytes": len(text.encode("utf-8")),
        "functions": [item["name"] for item in details],
        "function_details": details[:160],
        "classes": classes[:80],
        "preview": "\n".join(text.splitlines()[:24]),
    }


_HEALTH_SCORE_INSTANCE = None
_DRIFT_MONITOR_INSTANCE = None


def _payload_args(payload: Dict[str, Any]) -> tuple[list[Any], Dict[str, Any]]:
    args = payload.get("args", [])
    kwargs = payload.get("kwargs")
    if not isinstance(args, list):
        args = []
    if kwargs is None:
        kwargs = {key: value for key, value in payload.items() if key not in {"args", "kwargs", "function", "call"}}
    return args, kwargs if isinstance(kwargs, dict) else {}


def _adapter_monte_carlo(payload: Dict[str, Any]) -> Any:
    import numpy as np
    from engine.digital_twin_monte_carlo import create_engine, run_corner_probability
    args, kwargs = _payload_args(payload)
    state = kwargs.get("current_state") or (args[0] if args else None)
    if state is None:
        raise ValueError("current_state obrigatorio")
    seed = int(kwargs.get("seed", 42))
    engine = create_engine(seed=seed)
    return run_corner_probability(engine, np.asarray(state, dtype=np.float32))


def _adapter_execution(payload: Dict[str, Any]) -> Any:
    from engine.execution_router import ExecutionRouter
    args, kwargs = _payload_args(payload)
    signal = kwargs.get("signal")
    if signal is None and args:
        signal = args[0]
    if signal is None:
        signal = {key: value for key, value in kwargs.items() if key != "paper"}
    return ExecutionRouter(paper=True).execute(signal if isinstance(signal, dict) else {"value": signal})


def _adapter_health_score(function_name: str, payload: Dict[str, Any]) -> Any:
    global _HEALTH_SCORE_INSTANCE
    from engine.reliability.health_score import SystemHealthScore
    if _HEALTH_SCORE_INSTANCE is None:
        _HEALTH_SCORE_INSTANCE = SystemHealthScore()
    args, kwargs = _payload_args(payload)
    if function_name == "record":
        if args and isinstance(args[0], dict):
            kwargs = args[0]
        kwargs = dict(kwargs)
        if "ok" in kwargs and "error" not in kwargs:
            kwargs["error"] = not bool(kwargs.pop("ok"))
        allowed = {"latency_ms", "error", "vram_ratio", "confidence", "cache_hit", "ts"}
        kwargs = {key: value for key, value in kwargs.items() if key in allowed}
        _HEALTH_SCORE_INSTANCE.record(**kwargs)
    return _HEALTH_SCORE_INSTANCE.score()


def _resolve_awaitable(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    holder: Dict[str, Any] = {}
    def runner() -> None:
        try:
            holder["value"] = asyncio.run(value)
        except BaseException as exc:
            holder["error"] = exc
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=30)
    if thread.is_alive():
        raise TimeoutError("handler async excedeu 30s")
    if "error" in holder:
        raise holder["error"]
    return holder.get("value")


def _adapter_drift(function_name: str, payload: Dict[str, Any]) -> Any:
    global _DRIFT_MONITOR_INSTANCE
    from engine.improvements.drift_monitor import FeatureDriftMonitor
    if _DRIFT_MONITOR_INSTANCE is None:
        _DRIFT_MONITOR_INSTANCE = FeatureDriftMonitor()
    args, kwargs = _payload_args(payload)
    if function_name == "update":
        features = kwargs.get("features") or (args[0] if args else {})
        return _DRIFT_MONITOR_INSTANCE.update(features if isinstance(features, dict) else {})
    if function_name == "missing_rate":
        return _DRIFT_MONITOR_INSTANCE.missing_rate(int(kwargs.get("present", 0)), int(kwargs.get("expected", 0)))
    return _DRIFT_MONITOR_INSTANCE.recent_alerts(int(kwargs.get("n", 20)))


def _adapter_fast_parser(payload: Dict[str, Any]) -> Any:
    module = importlib.import_module("bridge.cognitive.multi_llm_router")
    parser = module.FastParser()
    args, kwargs = _payload_args(payload)
    text = kwargs.get("text") or (args[0] if args else "")
    return parser.extract_intent(str(text))


def _adapter_bridge(function_name: str, payload: Dict[str, Any]) -> Any:
    module = importlib.import_module("bridge.server")
    fn = getattr(module, function_name)
    args, kwargs = _payload_args(payload)
    return fn(*args, **kwargs)


def _adapter_sre(function_name: str, payload: Dict[str, Any]) -> Any:
    module = importlib.import_module("engine.sre.omnipotent_health_profiler")
    profiler = module.get_profiler()
    args, kwargs = _payload_args(payload)
    method = getattr(profiler, function_name)
    return _resolve_awaitable(method(*args, **kwargs))


FUNCTION_HANDLERS: Dict[tuple[str, str], Any] = {
    ("engine:digital_twin_monte_carlo.py", "run_corner_probability"): _adapter_monte_carlo,
    ("engine:execution_router.py", "execute"): _adapter_execution,
    ("engine:health_score.py", "record"): lambda payload: _adapter_health_score("record", payload),
    ("engine:health_score.py", "score"): lambda payload: _adapter_health_score("score", payload),
    ("engine:drift_monitor.py", "update"): lambda payload: _adapter_drift("update", payload),
    ("engine:drift_monitor.py", "missing_rate"): lambda payload: _adapter_drift("missing_rate", payload),
    ("engine:drift_monitor.py", "recent_alerts"): lambda payload: _adapter_drift("recent_alerts", payload),
    ("bridge:multi_llm_router.py", "extract_intent"): _adapter_fast_parser,
    ("bridge:server.py", "extract_match_view"): lambda payload: _adapter_bridge("extract_match_view", payload),
    ("bridge:server.py", "fingerprint"): lambda payload: _adapter_bridge("fingerprint", payload),
    ("bridge:server.py", "window_label"): lambda payload: _adapter_bridge("window_label", payload),
    ("bridge:server.py", "last_corner_gap"): lambda payload: _adapter_bridge("last_corner_gap", payload),
    ("bridge:server.py", "view_to_skill_pack"): lambda payload: _adapter_bridge("view_to_skill_pack", payload),
    ("bridge:server.py", "validate_skill_pack"): lambda payload: _adapter_bridge("validate_skill_pack", payload),
    ("sre:omnipotent_health_profiler", "run_full_diagnostic"): lambda payload: _adapter_sre("run_full_diagnostic", payload),
    ("sre:omnipotent_health_profiler", "check_memory_fragmentation"): lambda payload: _adapter_sre("check_memory_fragmentation", payload),
    ("sre:omnipotent_health_profiler", "check_clock_skew"): lambda payload: _adapter_sre("check_clock_skew", payload),
    ("sre:omnipotent_health_profiler", "check_schema_drift"): lambda payload: _adapter_sre("check_schema_drift", payload),
    ("sre:omnipotent_health_profiler", "check_vram_fragmentation"): lambda payload: _adapter_sre("check_vram_fragmentation", payload),
    ("sre:omnipotent_health_profiler", "predict_time_to_failure"): lambda payload: _adapter_sre("predict_time_to_failure", payload),
    ("sre:omnipotent_health_profiler", "check_phase_space"): lambda payload: _adapter_sre("check_phase_space", payload),
    ("sre:omnipotent_health_profiler", "analyze_traces"): lambda payload: _adapter_sre("analyze_traces", payload),
}


# Payloads mínimos e determinísticos para o menu individual. Eles não
# representam dados ao vivo nem autorizam ordens; servem apenas para que cada
# função allowlisted possa ser exercitada sem o usuário ter de descobrir a
# assinatura Python manualmente. O operador pode substituir qualquer campo.
DEFAULT_CALLS: Dict[tuple[str, str], Dict[str, Any]] = {
    ("bridge:gpu_resource_manager.py", "cleanup"): {},
    ("bridge:gpu_resource_manager.py", "device_info"): {},
    ("bridge:gpu_resource_manager.py", "profile"): {},
    ("bridge:gpu_resource_manager.py", "sample_vram"): {"note": "agent_hub_smoke"},
    ("bridge:gpu_resource_manager.py", "vram_snapshot"): {},
    ("bridge:multi_llm_router.py", "extract_intent"): {"text": "analise a partida"},
    ("bridge:neural_tts.py", "available"): {},
    ("bridge:neural_tts.py", "cache_stats"): {},
    ("bridge:neural_tts.py", "sanitize_for_speech"): {"text": "Teste de voz do AURA."},
    ("bridge:server.py", "extract_match_view"): {"payload": {}},
    ("bridge:server.py", "fingerprint"): {"view": {}},
    ("bridge:server.py", "last_corner_gap"): {"view": {}},
    ("bridge:server.py", "validate_skill_pack"): {"pack": {}},
    ("bridge:server.py", "view_to_skill_pack"): {"view": {}, "payload": {}},
    ("bridge:server.py", "window_label"): {"minute": 0, "period": 1},
    ("bridge:state_vector_daemon.py", "get_semantic_graph"): {},
    ("bridge:state_vector_daemon.py", "get_system_state"): {},
    ("engine:backtest_engine.py", "run_backtest"): {"rows": []},
    ("engine:corner_intelligence.py", "analyst_brief"): {"card": {}},
    ("engine:corner_intelligence.py", "analyze_corners"): {"analysis": {}, "payload": {}},
    ("engine:data_veracity.py", "verify_payload"): {"payload": {}},
    ("engine:digital_twin_monte_carlo.py", "run_corner_probability"): {"current_state": [0.0] * 48, "seed": 42},
    ("engine:drift_monitor.py", "missing_rate"): {"present": 0, "expected": 1},
    ("engine:drift_monitor.py", "recent_alerts"): {"n": 20},
    ("engine:drift_monitor.py", "update"): {"features": {}},
    ("engine:execution_router.py", "execute"): {"signal": {"decision": "HOLD", "paper_trade": True}},
    ("engine:gpu_resource_manager.py", "cuda_info"): {},
    ("engine:gpu_resource_manager.py", "recommended_voice_llm"): {"vram_gb": 6.0},
    ("engine:gpu_resource_manager.py", "resolve_cuda_device"): {},
    ("engine:gpu_resource_manager.py", "status"): {},
    ("engine:hawkes_corners.py", "build_hawkes_from_payload"): {"payload": {}},
    ("engine:hawkes_corners.py", "classify_event"): {"ev": {}},
    ("engine:hawkes_corners.py", "p_at_least_one"): {"lam_per_min": 0.1, "horizon_min": 5.0},
    ("engine:health_score.py", "record"): {"latency_ms": 0.0, "error": False, "vram_ratio": 0.0, "confidence": 0.0, "cache_hit": False},
    ("engine:health_score.py", "score"): {},
    ("engine:market_edge.py", "classify_market"): {"name": "corners"},
    ("engine:market_edge.py", "compute_edge"): {"p_model": 0.5, "odds": 2.0},
    ("engine:market_edge.py", "normalize_market_name"): {"name": "corners"},
    ("engine:market_edge.py", "odds_age_sec"): {"odds_ts": None},
    ("engine:market_edge.py", "parse_odds_ts"): {"raw": None},
    ("engine:model_shadow.py", "baseline_from_payload"): {"payload": {}},
    ("engine:model_shadow.py", "build_feature_vector"): {"feats": {}, "p_baseline": 0.0},
    ("engine:model_shadow.py", "poisson_at_least_one"): {"lam": 0.1},
    ("engine:model_shadow.py", "shadow_predict"): {"feats": {}, "p_baseline": 0.0},
    ("engine:voice_planner.py", "apply_pronunciation"): {"text": "Teste de voz"},
    ("engine:voice_planner.py", "build_ssml"): {"text": "Teste de voz"},
    ("engine:voice_planner.py", "narrative_from_card"): {"card": {}},
    ("engine:voice_planner.py", "plan_segments"): {"text": "Teste de voz do AURA."},
    ("engine:voice_planner.py", "strip_forbidden_fillers"): {"text": "Teste de voz"},
    ("sre:omnipotent_health_profiler", "analyze_traces"): {"limit": 20},
    ("sre:omnipotent_health_profiler", "check_clock_skew"): {},
    ("sre:omnipotent_health_profiler", "check_memory_fragmentation"): {},
    ("sre:omnipotent_health_profiler", "check_phase_space"): {},
    ("sre:omnipotent_health_profiler", "check_schema_drift"): {},
    ("sre:omnipotent_health_profiler", "check_vram_fragmentation"): {},
    ("sre:omnipotent_health_profiler", "predict_time_to_failure"): {},
    ("sre:omnipotent_health_profiler", "run_full_diagnostic"): {},
}


def _default_call(item: Dict[str, Any], function_name: str) -> Dict[str, Any]:
    import copy
    return copy.deepcopy(DEFAULT_CALLS.get((item["id"], function_name), {}))


def catalog() -> Dict[str, Any]:
    manifest = _load_manifest()
    items: List[Dict[str, Any]] = []
    for agent_id, spec in (manifest.get("agents") or {}).items():
        path = str(spec.get("path") or "")
        layer = str(spec.get("layer") or agent_id.split(":", 1)[0])
        state = _source_state(path)
        effective = ROOT / state["path"] if state["exists"] else ROOT / path
        inspection = _static_summary(effective)
        runnable = sorted(RUNNABLE_FUNCTIONS.get(agent_id, set()))
        top_level_functions = {detail["name"] for detail in inspection.get("function_details", []) if detail.get("top_level")}
        handler_functions = {name for (registered_agent, name) in FUNCTION_HANDLERS if registered_agent == agent_id}
        # Prefer static/handler match; fallback to full RUNNABLE allowlist (F13).
        matched = [name for name in runnable if name in top_level_functions or name in handler_functions]
        match_mode = "static_or_handler" if matched else ("allowlist_fallback" if runnable else "none")
        runnable_functions = matched if matched else list(runnable)
        # Manifest may also declare actions/runnable_functions
        manifest_fns = []
        for key in ("runnable_functions", "actions"):
            raw = spec.get(key) or []
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str) and item not in {"status", "inspect", "glm_review", "run_function", "health", "pending", "voice_diagnostic", "paper_preview"}:
                        if item not in manifest_fns:
                            manifest_fns.append(item)
        if not runnable_functions and manifest_fns:
            runnable_functions = list(manifest_fns)
            match_mode = "manifest_fallback"
        function_defaults = {name: _default_call({"id": agent_id}, name) for name in runnable_functions}
        # State priority: syntax/asset still block; allowlist can mark runnable even if source via ENABLED.
        if inspection.get("syntax_error"):
            implementation_state = "syntax_error"
        elif inspection.get("asset_type"):
            implementation_state = "asset"
        elif runnable_functions:
            implementation_state = "runnable"
        elif not state["exists"]:
            implementation_state = "source_missing"
        else:
            implementation_state = "inspect_only"
        items.append({
            "id": agent_id,
            "name": _display_name(agent_id, path),
            "layer": layer,
            "file": path,
            "status": str(spec.get("status") or "unknown"),
            "source": state,
            "implementation_state": implementation_state,
            "match_mode": match_mode,
            "syntax_error": inspection.get("syntax_error"),
            "asset_type": inspection.get("asset_type"),
            "functions": inspection.get("functions", []),
            "function_details": inspection.get("function_details", []),
            "runnable_functions": runnable_functions,
            "function_defaults": function_defaults,
            "actions": list(dict.fromkeys(_actions(agent_id, layer, len(runnable_functions)) + runnable_functions)),
            "paper_trade": True,
        })
    items.sort(key=lambda item: (item["layer"], item["name"].lower(), item["id"]))
    runnable_n = sum(1 for item in items if item.get("implementation_state") == "runnable")
    return {
        "ok": True,
        "manifestVersion": manifest.get("version"),
        "declaredCount": manifest.get("agent_count", len(items)),
        "count": len(items),
        "runnable": runnable_n,
        "inspect_only": sum(1 for item in items if item.get("implementation_state") == "inspect_only"),
        "source_missing": sum(1 for item in items if item.get("implementation_state") == "source_missing"),
        "layers": sorted({item["layer"] for item in items}),
        "paper_trade": True,
        "agents": items,
    }


def _find(agent_id: str) -> Dict[str, Any] | None:
    return next((item for item in catalog()["agents"] if item["id"] == agent_id), None)


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "<depth_limit>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value), "preview_base64": value[:96].hex()}
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value), depth + 1)
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in list(value.items())[:200]}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in list(value)[:200]]
    for method in ("model_dump", "to_dict"):
        if hasattr(value, method):
            try:
                return _jsonable(getattr(value, method)(), depth + 1)
            except Exception:
                pass
    if hasattr(value, "tolist"):
        try:
            return _jsonable(value.tolist(), depth + 1)
        except Exception:
            pass
    return str(value)


def _module_name(item: Dict[str, Any]) -> str:
    path = item["file"].replace("\\", "/")
    if path.endswith(".py"):
        path = path[:-3]
    return path.replace("/", ".")


def _call_allowlisted(item: Dict[str, Any], function_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = set(item.get("runnable_functions") or [])
    if function_name not in allowed:
        return {
            "ok": False,
            "error": "function_not_allowlisted",
            "function": function_name,
            "allowed_functions": sorted(allowed),
            "message": "A funcao esta disponivel para inspecao, mas nao possui handler seguro neste release.",
        }
    if not payload:
        payload = _default_call(item, function_name)
    if len(json.dumps(payload, ensure_ascii=False, default=str)) > 32768:
        return {"ok": False, "error": "payload_too_large", "limit_bytes": 32768}
    try:
        module = importlib.import_module(_module_name(item))
        adapter = FUNCTION_HANDLERS.get((item["id"], function_name))
        if adapter is not None:
            result = adapter(payload)
            return {"ok": True, "agent_id": item["id"], "function": function_name, "result": _jsonable(result), "execution": "paper_trade_only"}
        fn = getattr(module, function_name)
        if not callable(fn):
            return {"ok": False, "error": "attribute_not_callable", "function": function_name}
        args = payload.get("args", [])
        kwargs = payload.get("kwargs")
        if not isinstance(args, list) or (kwargs is not None and not isinstance(kwargs, dict)):
            return {"ok": False, "error": "invalid_call_shape", "expected": {"args": [], "kwargs": {}}}
        if kwargs is None:
            kwargs = {key: value for key, value in payload.items() if key not in {"args", "kwargs"}}
        if len(args) > 8 or len(kwargs) > 32:
            return {"ok": False, "error": "too_many_arguments"}
        # Nunca libera uma ordem real pelo menu.
        if item["id"] == "engine:execution_router.py":
            kwargs["paper"] = True
        result = fn(*args, **kwargs)
        return {"ok": True, "agent_id": item["id"], "function": function_name, "result": _jsonable(result), "execution": "paper_trade_only"}
    except Exception as exc:
        return {"ok": False, "agent_id": item["id"], "function": function_name, "error": f"{type(exc).__name__}: {exc}", "execution": "blocked_on_error"}


def status(agent_id: str) -> Dict[str, Any]:
    item = _find(agent_id)
    if item is None:
        return {"ok": False, "error": "agent_not_found", "agent_id": agent_id}
    return {
        "ok": True,
        "agent": item,
        "ready": bool(item["source"]["exists"] and item["status"] == "enabled"),
        "execution": "paper_trade_only",
    }


def action(agent_id: str, action_name: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = payload or {}
    item = _find(agent_id)
    if item is None:
        return {"ok": False, "error": "agent_not_found", "agent_id": agent_id}
    allowed_actions = list(item.get("actions") or [])
    runnable = list(item.get("runnable_functions") or [])
    # F14: function name is a first-class action when allowlisted.
    if action_name == "run_function":
        function_name = str(payload.get("function") or "")
        call_payload = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        return _call_allowlisted(item, function_name, call_payload)
    if action_name in runnable:
        call_payload = payload.get("call") if isinstance(payload.get("call"), dict) else payload
        return _call_allowlisted(item, action_name, call_payload)
    if action_name not in allowed_actions:
        return {
            "ok": False,
            "error": "action_not_allowlisted",
            "agent_id": agent_id,
            "action": action_name,
            "allowed": list(dict.fromkeys(allowed_actions + runnable)),
            "paper_trade": True,
        }
    if action_name == "status":
        return status(agent_id)
    if action_name == "glm_review":
        return {"ok": True, "agent": item, "execution": "advisory_only", "execution_allowed": False, "message": "Envie o motivo ao endpoint /api/agents/{agent_id}/glm-review para enfileirar uma revisão GLM supervisionada."}
    if action_name == "inspect":
        source_path = item["source"].get("path") or item["file"]
        source = ROOT / source_path
        return {"ok": True, "agent": item, "inspection": _static_summary(source), "execution": "static_only"}
    if action_name == "health":
        return {"ok": True, "agent": item, "health": "registered_and_source_present" if item["source"]["exists"] else "source_missing", "execution": "no_side_effect"}
    if action_name == "pending":
        pending_path = ROOT / "director_pending_actions.json"
        if not pending_path.exists():
            return {"ok": True, "agent": item, "pending": False, "message": "Nenhuma acao do Diretor pendente."}
        try:
            return {"ok": True, "agent": item, "pending": True, "action": json.loads(pending_path.read_text(encoding="utf-8"))}
        except Exception as exc:
            return {"ok": False, "agent": item, "error": f"pending_read_failed: {exc}"}
    if action_name == "paper_preview":
        return {"ok": True, "agent": item, "paper_trade": True, "execution_allowed": False, "message": "ExecutionRouter exposto somente como pre-visualizacao; nenhuma ordem real e permitida.", "received": {key: payload.get(key) for key in ("market", "side", "price", "stake") if key in payload}}
    if action_name == "simulation_contract":
        return {"ok": True, "agent": item, "contract": {"required": ["current_state"], "optional": ["seed"], "output": {"field": "Corner_Ocorre_em_20s", "horizon_seconds": 20, "n_trajectories": 10000}}, "message": "Contrato exposto; envie current_state para uma simulacao controlada em paper trade.", "execution_allowed": False}
    if action_name == "voice_diagnostic":
        url = "http://127.0.0.1:8099/api/voice/diagnostic"
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                data = json.loads(response.read().decode("utf-8"))
            return {"ok": True, "agent": item, "voice": data}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return {"ok": False, "agent": item, "error": f"voice_unavailable: {exc}"}
    return {"ok": False, "error": "unhandled_action", "agent_id": agent_id, "action": action_name}
