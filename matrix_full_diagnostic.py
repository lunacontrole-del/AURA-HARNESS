#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostico completo multi-camada + narrativa Hermes/Ollama para a Matriz."""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(os.environ.get("AURA_ROOT", Path(__file__).resolve().parents[1])).resolve()
LOGDIR = ROOT / "logs_supervisor"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("CORNERAI_CHAT_MODEL") or os.environ.get("AURA_HERMES_MODEL") or "llama3.2:3b"


def _http(url: str, timeout: float = 3.5, method: str = "GET", data: Optional[bytes] = None, headers: Optional[dict] = None):
    h = {"User-Agent": "AURA-MatrixDiag/1.0", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = Request(url, data=data, headers=h, method=method)
    with urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", errors="replace")
        code = getattr(r, "status", 200)
        try:
            return code, json.loads(body)
        except Exception:
            return code, {"raw": body[:800]}


def _port(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _layer(name: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    fails = sum(1 for i in items if i.get("status") == "FAIL")
    warns = sum(1 for i in items if i.get("status") == "WARN")
    oks = sum(1 for i in items if i.get("status") == "OK")
    status = "FAIL" if fails else ("WARN" if warns else "OK")
    return {"layer": name, "status": status, "ok": oks, "warn": warns, "fail": fails, "items": items}


def collect_layers(*, engine_self: bool = True) -> Dict[str, Any]:
    layers: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    # L0 files
    files = [
        "engine/server.py", "bridge/server.py", "desktop/ui/matriz_v22/index.html",
        "desktop/publish/Aura.QuantX.Desktop.exe", "desktop/capture/aura-capture.js",
        "engine/agents/hermes_supervisor_agent.py", "engine/venv/Scripts/python.exe",
        "desktop/config/desktop.json", "engine/data/llm_preference.json",
    ]
    items = []
    for rel in files:
        p = ROOT / rel
        items.append({
            "name": rel,
            "status": "OK" if p.exists() else "FAIL",
            "detail": f"size={p.stat().st_size}" if p.exists() else "ausente",
            "fix": "" if p.exists() else f"Restaure {rel} do pacote ZIP",
        })
    layers.append(_layer("L0_arquivos", items))

    # L1 ports
    items = []
    for name, port in [("bridge", 8080), ("engine", 8765), ("voice", 8099), ("ollama", 11434)]:
        ok = True if (name == "engine" and engine_self) else _port(port)
        if name == "engine" and engine_self:
            ok = True
        items.append({
            "name": f"{name}:{port}",
            "status": "OK" if ok else "FAIL",
            "detail": "LISTEN" if ok else "CLOSED",
            "fix": "" if ok else f"Suba o servico {name} na porta {port}",
        })
    layers.append(_layer("L1_portas", items))

    # L2 HTTP
    probes = [
        ("bridge_health", "http://127.0.0.1:8080/health", False),
        ("engine_health", "http://127.0.0.1:8765/api/health", True),
        ("voice_health", "http://127.0.0.1:8099/api/voice/health", False),
        ("ollama_tags", "http://127.0.0.1:11434/api/tags", False),
        ("ui_state", "http://127.0.0.1:8765/api/ui/state", True),
        ("agents", "http://127.0.0.1:8765/api/agents", True),
        ("bridge_latest", "http://127.0.0.1:8080/api/cornerai/latest", False),
        ("glm_status", "http://127.0.0.1:8765/api/agents/glm/status", True),
    ]
    items = []
    for name, url, is_engine in probes:
        if is_engine and engine_self and name == "engine_health":
            items.append({"name": name, "status": "OK", "detail": "self_liveness", "fix": ""})
            continue
        try:
            code, data = _http(url)
            if name == "bridge_latest":
                if code == 200:
                    items.append({"name": name, "status": "OK", "detail": "feed presente", "fix": ""})
                elif code == 404:
                    items.append({
                        "name": name, "status": "WARN",
                        "detail": "HTTP 404 sem captura SokkerPRO",
                        "fix": "Desktop F2 → SokkerPRO → partida AO VIVO (nao use Chrome externo)",
                    })
                elif code == 401:
                    items.append({
                        "name": name, "status": "FAIL",
                        "detail": "HTTP 401 token Bridge",
                        "fix": "CORNERAI_BRIDGE_REQUIRE_TOKEN=0 ou header X-CornerAI-Token",
                    })
                else:
                    items.append({"name": name, "status": "WARN", "detail": f"HTTP {code}", "fix": ""})
            elif name == "glm_status":
                enabled = bool((data or {}).get("glm_enabled"))
                items.append({
                    "name": name,
                    "status": "OK" if not enabled else "WARN",
                    "detail": f"glm_enabled={enabled}",
                    "fix": "" if not enabled else "Desligue GLM; use Hermes/Ollama",
                })
            else:
                st = "OK" if code == 200 else "WARN"
                items.append({"name": name, "status": st, "detail": f"HTTP {code}", "fix": ""})
        except HTTPError as e:
            if name == "bridge_latest" and e.code == 404:
                items.append({
                    "name": name, "status": "WARN", "detail": "HTTP 404 sem payload",
                    "fix": "Abra SokkerPRO no WebView do AURA (F2)",
                })
            elif name == "bridge_latest" and e.code == 401:
                items.append({
                    "name": name, "status": "FAIL", "detail": "HTTP 401",
                    "fix": "Token Bridge: X-CornerAI-Token",
                })
            else:
                items.append({
                    "name": name, "status": "FAIL", "detail": f"HTTP {e.code}",
                    "fix": f"Falha em {url}",
                })
        except Exception as e:
            items.append({
                "name": name, "status": "FAIL", "detail": str(e)[:140],
                "fix": f"Sem comunicacao com {url}",
            })
    layers.append(_layer("L2_http", items))

    # L3 feed disk
    items = []
    latest = ROOT / "bridge" / "live_latest.json"
    feed = ROOT / "bridge" / "live_feed.jsonl"
    if latest.exists() and latest.stat().st_size > 2:
        age = time.time() - latest.stat().st_mtime
        items.append({
            "name": "live_latest.json",
            "status": "OK" if age < 180 else "WARN",
            "detail": f"size={latest.stat().st_size} ageSec={int(age)}",
            "fix": "" if age < 180 else "Feed antigo — reabra jogo ao vivo",
        })
    else:
        items.append({
            "name": "live_latest.json", "status": "FAIL", "detail": "vazio/ausente",
            "fix": "Captura inativa: F2 SokkerPRO no Desktop",
        })
    items.append({
        "name": "live_feed.jsonl",
        "status": "OK" if feed.exists() else "WARN",
        "detail": f"size={feed.stat().st_size}" if feed.exists() else "ausente",
        "fix": "",
    })
    layers.append(_layer("L3_captura", items))

    # L4 desktop
    items = []
    for label, rel in [
        ("ui_matriz", "desktop/ui/matriz_v22/index.html"),
        ("ui_publish", "desktop/publish/ui/matriz_v22/index.html"),
        ("exe", "desktop/publish/Aura.QuantX.Desktop.exe"),
        ("mainform", "desktop/MainForm.cs"),
    ]:
        p = ROOT / rel
        items.append({
            "name": label,
            "status": "OK" if p.exists() else "FAIL",
            "detail": str(rel),
            "fix": "" if p.exists() else f"Falta {rel}",
        })
    layers.append(_layer("L4_desktop", items))

    # L5 LLM
    items = []
    try:
        code, data = _http(f"{OLLAMA}/api/tags")
        models = [m.get("name") for m in (data.get("models") or [])]
        has = any("llama3.2" in (n or "") for n in models)
        items.append({
            "name": "ollama_models",
            "status": "OK" if has else "WARN",
            "detail": ", ".join(models[:8]) if models else "nenhum",
            "fix": "" if has else "AURA_INSTALL_HERMES_OLLAMA.bat",
        })
    except Exception as e:
        items.append({
            "name": "ollama", "status": "FAIL", "detail": str(e)[:120],
            "fix": "ollama serve",
        })
    pref = ROOT / "engine" / "data" / "llm_preference.json"
    if pref.exists():
        try:
            pj = json.loads(pref.read_text(encoding="utf-8"))
            items.append({
                "name": "llm_preference",
                "status": "OK" if not pj.get("glm_enabled") else "WARN",
                "detail": json.dumps(pj, ensure_ascii=False)[:200],
                "fix": "",
            })
        except Exception:
            items.append({"name": "llm_preference", "status": "WARN", "detail": "json invalido", "fix": ""})
    layers.append(_layer("L5_llm_hermes", items))

    # L6 safety
    pt = os.environ.get("PAPER_TRADE", "true").lower() in ("1", "true", "yes", "on")
    ex = os.environ.get("EXECUTION_ALLOWED", "false").lower() in ("1", "true", "yes", "on")
    items = [
        {"name": "paper_trade", "status": "OK" if pt else "FAIL", "detail": str(pt), "fix": "PAPER_TRADE=true"},
        {"name": "execution_allowed", "status": "OK" if not ex else "FAIL", "detail": str(ex), "fix": "EXECUTION_ALLOWED=false"},
        {"name": "llm_backend", "status": "OK", "detail": os.environ.get("AURA_LLM_BACKEND", "hermes"), "fix": ""},
        {"name": "chat_model", "status": "OK", "detail": MODEL, "fix": ""},
    ]
    layers.append(_layer("L6_seguranca", items))

    # totals
    ok = warn = fail = 0
    for ly in layers:
        ok += ly["ok"]
        warn += ly["warn"]
        fail += ly["fail"]
    global_status = "FAIL" if fail else ("DEGRADED" if warn else "HEALTHY")

    # communication map
    comms = {
        "matriz_ui": "https://aura.local/index.html → Desktop WebView",
        "engine": "http://127.0.0.1:8765",
        "bridge": "http://127.0.0.1:8080",
        "voice": "http://127.0.0.1:8099",
        "ollama_hermes": f"{OLLAMA} model={MODEL}",
        "capture_path": "SokkerPRO WebView (F2) → aura-capture.js → POST Bridge /api/cornerai/feed → Engine ui/state",
        "breaks_if": [
            "SokkerPRO so no Chrome externo (sem injecao)",
            "Bridge token 401 no /api/cornerai/latest",
            "live_latest.json vazio",
            "Ollama offline → chat/diagnostico LLM falha",
        ],
    }

    return {
        "ok": global_status != "FAIL",
        "status": global_status,
        "ts": now,
        "root": str(ROOT),
        "counts": {"ok": ok, "warn": warn, "fail": fail},
        "layers": layers,
        "communication": comms,
        "matrix": {
            "service_badges": {
                "engine": "ON" if any(i["name"].startswith("engine") and i["status"] == "OK" for ly in layers if ly["layer"] == "L1_portas" for i in ly["items"]) else "OFF",
                "bridge": "ON" if _port(8080) else "OFF",
                "voice": "ON" if _port(8099) else "OFF",
                "ollama": "ON" if _port(11434) else "OFF",
                "glm": "OFF",
            },
            "capture": "LIVE" if (latest.exists() and latest.stat().st_size > 2 and (time.time() - latest.stat().st_mtime) < 180) else "NO_FEED",
        },
    }


def llm_narrative(snapshot: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
    """Gera diagnostico em portugues via Ollama (Hermes backend)."""
    compact = {
        "status": snapshot.get("status"),
        "counts": snapshot.get("counts"),
        "matrix": snapshot.get("matrix"),
        "communication": snapshot.get("communication"),
        "layers": [
            {
                "layer": ly["layer"],
                "status": ly["status"],
                "problems": [
                    {"name": i["name"], "status": i["status"], "detail": i["detail"], "fix": i.get("fix")}
                    for i in ly["items"] if i["status"] in ("WARN", "FAIL")
                ],
            }
            for ly in snapshot.get("layers", [])
        ],
    }
    prompt = (
        "Voce e o Hermes Supervisor do AURA Operator OS (paper-trade only).\n"
        "Com base no JSON de diagnostico, escreva em portugues:\n"
        "1) Status global em uma linha\n"
        "2) O que esta OK\n"
        "3) Onde a comunicacao quebra (caminho exato)\n"
        "4) Lista priorizada do que falta fazer (comandos curtos)\n"
        "5) Se captura NO_FEED: instrua F2 SokkerPRO no Desktop, nao Chrome\n"
        "Nao invente fixtures. Nao sugira apostas reais. GLM esta desligado; LLM e Ollama llama3.2.\n"
        f"JSON:\n{json.dumps(compact, ensure_ascii=False)[:6000]}"
    )
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 500},
    }).encode("utf-8")
    try:
        code, data = _http(
            f"{OLLAMA}/api/generate",
            timeout=timeout,
            method="POST",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        text = (data or {}).get("response") or (data or {}).get("raw") or ""
        return {"ok": True, "model": MODEL, "narrative": text.strip(), "http": code}
    except Exception as e:
        return {
            "ok": False,
            "model": MODEL,
            "narrative": (
                f"LLM indisponivel ({e}). Status bruto: {snapshot.get('status')} "
                f"OK={snapshot.get('counts',{}).get('ok')} WARN={snapshot.get('counts',{}).get('warn')} "
                f"FAIL={snapshot.get('counts',{}).get('fail')}. "
                f"Captura={snapshot.get('matrix',{}).get('capture')}."
            ),
            "error": str(e)[:200],
        }


def run_full(*, with_llm: bool = True, engine_self: bool = True) -> Dict[str, Any]:
    snap = collect_layers(engine_self=engine_self)
    llm = llm_narrative(snap) if with_llm else {"ok": False, "narrative": "", "skipped": True}
    out = {
        **snap,
        "llm": llm,
        "report_text": _to_text(snap, llm),
    }
    try:
        LOGDIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (LOGDIR / f"MATRIX_DIAG_{ts}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        (LOGDIR / "MATRIX_DIAG_LATEST.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        (LOGDIR / "MATRIX_DIAG_LATEST.txt").write_text(out["report_text"], encoding="utf-8")
    except Exception:
        pass
    return out


def _to_text(snap: Dict[str, Any], llm: Dict[str, Any]) -> str:
    lines = [
        "================================================================",
        " AURA DIAGNOSTICO MATRIZ + HERMES/OLLAMA",
        f" status={snap.get('status')}  counts={snap.get('counts')}",
        f" captura={snap.get('matrix',{}).get('capture')}  badges={snap.get('matrix',{}).get('service_badges')}",
        "================================================================",
    ]
    for ly in snap.get("layers", []):
        lines.append(f"--- {ly['layer']} [{ly['status']}] ok={ly['ok']} warn={ly['warn']} fail={ly['fail']}")
        for i in ly["items"]:
            lines.append(f"  [{i['status']}] {i['name']}: {i['detail']}")
            if i.get("fix") and i["status"] != "OK":
                lines.append(f"       FIX → {i['fix']}")
    lines.append("--- COMUNICACAO")
    for k, v in (snap.get("communication") or {}).items():
        if isinstance(v, list):
            lines.append(f"  {k}:")
            for x in v:
                lines.append(f"    - {x}")
        else:
            lines.append(f"  {k}: {v}")
    lines.append("--- HERMES / LLM")
    lines.append(llm.get("narrative") or "(sem narrativa)")
    lines.append("================================================================")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import pprint
    r = run_full(with_llm=True, engine_self=False)
    print(r["report_text"])
