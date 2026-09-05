#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA QUANT-X V25 — Browser Agent v4: dual-mode + agentes + GLM.

v4 adiciona:
  - mode="lite": bloqueia imagens/fontes/CSS/ads/analytics → 5x menos RAM
  - mode="full": renderização completa (login, debug, visual)
  - BROWSER (lite, headless) para agentes e captura
  - BROWSER_FULL (full, windowed) para login e debug
  - Agent API: extract_view(), get_active_fixture(), ask_glm()
  - GLM integrado no _publish + ask_glm() para agentes
  - Lock de concorrência mantido
  - WsFrameDecoder + WebSocket tempo real

Drivers: Playwright (lite usa page.route) > Selenium (lite usa prefs) > Mock.
"""
from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("aura.browser")

__version__ = "4.0.0"
__all__ = ["BrowserAgent", "BROWSER", "BROWSER_FULL",
           "HeuristicWalker", "FixtureState", "GLMBridge", "WsFrameDecoder"]

# ===========================================================================
# Config
# ===========================================================================
SOKKERPRO_DOMAINS = ("sokkerpro.com",)
PATH_ALLOWLIST = re.compile(
    r"(history|histor|fixture|match|stats|preodds|odds|x7|timeline|events|incidents)",
    re.I)

STAT_ALIASES: Dict[str, str] = {
    "corners": "corners", "attacks": "attacks", "dangerous": "dangerous",
    "shots": "shots", "shotson": "shotsOn", "shotsoff": "shotsOff",
    "shotsontarget": "shotsOn", "shotsofftarget": "shotsOff",
    "possession": "possession", "xg": "xg", "fouls": "fouls",
    "offsides": "offsides", "yellow": "yellow", "yellowcards": "yellow",
    "red": "red", "redcards": "red", "subs": "subs", "substitutions": "subs",
    "crosses": "crosses", "saves": "saves", "passes": "passes",
    "passesfailed": "passesFailed",
    "escanteios": "corners", "ataques": "attacks",
    "ataquesperigosos": "dangerous", "perigosos": "dangerous",
    "finalizacoes": "shots", "chutes": "shots",
    "finalizacoesnoalvo": "shotsOn", "chutesnoalvo": "shotsOn",
    "finalizacoesfora": "shotsOff", "chutesfora": "shotsOff",
    "posse": "possession", "possedebola": "possession",
    "faltas": "fouls", "impedimentos": "offsides",
    "amarelos": "yellow", "cartoesamarelos": "yellow",
    "vermelhos": "red", "cartoesvermelhos": "red",
    "substituicoes": "subs", "cruzamentos": "crosses",
    "defesas": "saves", "passeserrados": "passesFailed",
}

FIXTURE_KEYS = {
    "home": "home", "casa": "home",
    "away": "away", "fora": "away", "visitante": "away",
    "minute": "minute", "minuto": "minute",
    "league": "league", "liga": "league", "campeonato": "league",
    "period": "period", "periodo": "period", "status": "status",
}
SCORE_KEYS = {"score", "placar", "resultado"}
EVENT_MIN_KEYS = {"minute", "minuto", "m", "time"}
EVENT_TEAM_KEYS = {"team", "side", "equipe", "lado", "club", "clube"}
EVENT_TYPE_KEYS = {"type", "tipo", "event", "evento"}
SKIP_KEYS = {
    "timestamp", "ts", "id", "url", "source", "created", "updated", "version",
    "hash", "signature", "debug", "log", "error", "message", "description",
    "help", "link", "href", "icon", "image", "logo", "color", "font", "css",
    "class", "style", "width", "height",
}

BOOTSTRAP_PATHS = [
    "/fixture/{fid}", "/fixture/{fid}/stats", "/fixture/{fid}/events",
    "/fixture/{fid}/timeline", "/fixture/{fid}/incidents",
    "/fixture/{fid}/preodds", "/fixture/{fid}/x7",
    "/fixture/{fid}/history", "/fixture/{fid}/projecao", "/fixture/{fid}/grafic",
]
ALL_BOOTSTRAP = BOOTSTRAP_PATHS + [
    p.replace("/fixture/", "/api/fixture/") for p in BOOTSTRAP_PATHS
]
BOOTSTRAP_ORIGINS = ("https://m2.sokkerpro.com", "https://m4.sokkerpro.com")
_LITE_BLOCK_TYPES = {"image", "font", "media", "stylesheet"}


def _to_num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    except (TypeError, ValueError):
        return None


def _is_sokkerpro_url(url: str) -> bool:
    return any(d in str(url).lower() for d in SOKKERPRO_DOMAINS)


def _is_interesting_path(url: str) -> bool:
    return bool(PATH_ALLOWLIST.search(str(url)))


def _extract_fixture_id(url: str) -> Optional[str]:
    for pat in (r"/fixture/(\d+)", r"/match/(\d+)", r"/ws/fixture/(\d+)"):
        m = re.search(pat, str(url))
        if m:
            return m.group(1)
    return None


def _extract_pair(v: Any) -> Optional[List[Optional[float]]]:
    if isinstance(v, (list, tuple)):
        if len(v) >= 2:
            return [_to_num(v[0]), _to_num(v[1])]
        return None
    if isinstance(v, dict):
        # Nao usar `or` — 0 e valor valido de placar/stat
        h = v.get("home")
        if h is None:
            h = v.get("h")
        if h is None:
            h = v.get("casa")
        a = v.get("away")
        if a is None:
            a = v.get("a")
        if a is None:
            a = v.get("fora")
        if a is None:
            a = v.get("visitante")
        if h is not None or a is not None:
            return [_to_num(h), _to_num(a)]
    return None


def _looks_like_stat(key: str, value: Any) -> bool:
    kl = str(key).lower().replace("_", "").replace(" ", "")
    if kl in SKIP_KEYS:
        return False
    p = _extract_pair(value)
    if p is None:
        return False
    h, a = p
    if h is None and a is None:
        return False
    if h is not None and abs(h) > 9999:
        return False
    if a is not None and abs(a) > 9999:
        return False
    return True


def _iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ===========================================================================
# WsFrameDecoder
# ===========================================================================
class WsFrameDecoder:
    """Deserializa frames WS texto/binario do SokkerPRO para JSON."""

    @staticmethod
    def decode(raw: Any) -> Optional[Any]:
        if raw is None:
            return None
        if isinstance(raw, (dict, list)):
            return raw
        if isinstance(raw, str):
            return WsFrameDecoder._from_text(raw)
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return WsFrameDecoder._from_bytes(bytes(raw))
        return None

    @staticmethod
    def _from_text(text: str) -> Optional[Any]:
        t = text.strip()
        if not t:
            return None
        if (t[0] == "{" and t[-1] == "}") or (t[0] == "[" and t[-1] == "]"):
            try:
                return json.loads(t)
            except Exception:
                pass
        island = WsFrameDecoder._json_island(t)
        if island:
            try:
                return json.loads(island)
            except Exception:
                return None
        return None

    @staticmethod
    def _from_bytes(u8: bytes) -> Optional[Any]:
        if not u8:
            return None
        try:
            txt = u8.decode("utf-8", errors="strict")
            data = WsFrameDecoder._from_text(txt)
            if data is not None:
                return data
        except Exception:
            pass
        for endian in (">", "<"):
            for size, fmt in ((4, "I"), (2, "H")):
                if len(u8) < size + 2:
                    continue
                try:
                    (n,) = struct.unpack(endian + fmt, u8[:size])
                except Exception:
                    continue
                if n <= 0 or size + n > len(u8) or n > 2_000_000:
                    continue
                try:
                    txt = u8[size:size + n].decode("utf-8", errors="strict")
                except Exception:
                    continue
                data = WsFrameDecoder._from_text(txt)
                if data is not None:
                    return data
        try:
            txt = u8.decode("utf-8", errors="replace")
            island = WsFrameDecoder._json_island(txt)
            if island:
                return json.loads(island)
        except Exception:
            pass
        return None

    @staticmethod
    def _json_island(text: str) -> Optional[str]:
        if not text:
            return None
        t = text
        start_obj = t.find("{")
        start_arr = t.find("[")
        if start_obj < 0 and start_arr < 0:
            return None
        if start_obj >= 0 and (start_arr < 0 or start_obj <= start_arr):
            start, open_c, close_c = start_obj, "{", "}"
        else:
            start, open_c, close_c = start_arr, "[", "]"
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(t)):
            c = t[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
                continue
            if c == open_c:
                depth += 1
            elif c == close_c:
                depth -= 1
                if depth == 0:
                    return t[start:i + 1]
        return None


class _WSDecoder:
    """Decoder com buffer de chunks incompletos (Selenium/legacy)."""

    def __init__(self):
        self._b: Dict[str, str] = {}

    def decode(self, url: str, payload: Any) -> Optional[Any]:
        if isinstance(payload, (dict, list)):
            return payload
        data = WsFrameDecoder.decode(payload)
        if data is not None:
            self._b.pop(url, None)
            return data
        if not isinstance(payload, str):
            if isinstance(payload, (bytes, bytearray)):
                try:
                    payload = bytes(payload).decode("utf-8", errors="replace")
                except Exception:
                    return None
            else:
                return None
        buf = self._b.get(url, "") + payload
        try:
            d = json.loads(buf)
            self._b.pop(url, None)
            return d
        except Exception:
            self._b[url] = buf
            if len(buf) > 50000:
                self._b.pop(url, None)
            return None

    def reset(self, url: str = "") -> None:
        if url:
            self._b.pop(url, None)
        else:
            self._b.clear()


# ===========================================================================
# HeuristicWalker
# ===========================================================================
class HeuristicWalker:
    def __init__(self):
        self.stats: Dict[str, List] = {}
        self.events: List[dict] = []
        self.fixture: Dict[str, Any] = {}
        self.odds: List[dict] = []
        self.advanced: Dict[str, Any] = {}

    def walk(self, obj: Any, *, depth: int = 0, max_depth: int = 12) -> None:
        if depth > max_depth:
            return
        if isinstance(obj, dict):
            self._wd(obj, depth, max_depth)
        elif isinstance(obj, list):
            self._wl(obj, depth, max_depth)

    def _wd(self, d: dict, depth: int, max_depth: int) -> None:
        for k, v in d.items():
            kl = str(k).lower().replace("_", "").replace(" ", "").replace("-", "")
            if kl in STAT_ALIASES:
                p = _extract_pair(v)
                if p:
                    self.stats[STAT_ALIASES[kl]] = p
            elif kl in FIXTURE_KEYS:
                c = FIXTURE_KEYS[kl]
                if c in ("minute", "period"):
                    n = _to_num(v)
                    if n is not None:
                        self.fixture[c] = n
                elif v and isinstance(v, str):
                    self.fixture[c] = v
            elif kl in SCORE_KEYS:
                p = _extract_pair(v)
                if p:
                    self.fixture["score_home"], self.fixture["score_away"] = p
            elif kl in (
                "cpiv2", "cpi", "appm", "temporal", "offensive_acceleration",
                "correlation_signals", "clusters", "match_context",
                "score_pressao_canto",
            ):
                self.advanced[k] = v
            elif kl in ("odds", "odd", "preodds", "cotacoes", "apostas", "1x2",
                        "result", "resultado"):
                self._eo({k: v} if kl in ("1x2", "result", "resultado") else v)
            elif _looks_like_stat(k, v):
                p = _extract_pair(v)
                if p and p[0] is not None and p[1] is not None:
                    self.stats[kl] = p
            else:
                self.walk(v, depth=depth + 1, max_depth=max_depth)

    def _wl(self, lst: list, depth: int, max_depth: int) -> None:
        for item in lst:
            if isinstance(item, dict):
                ev = self._ee(item)
                if ev:
                    self.events.append(ev)
            self.walk(item, depth=depth + 1, max_depth=max_depth)

    @staticmethod
    def _ee(d: dict) -> Optional[dict]:
        ev: Dict[str, Any] = {}
        for k, v in d.items():
            kl = str(k).lower().replace("_", "")
            if kl in EVENT_MIN_KEYS:
                n = _to_num(v)
                if n is not None:
                    ev["minute"] = int(n)
            elif kl in EVENT_TEAM_KEYS:
                ev["team"] = str(v)
            elif kl in EVENT_TYPE_KEYS:
                ev["type"] = str(v)
        return ev if "minute" in ev else None

    def _eo(self, obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    o: Dict[str, Any] = {}
                    for k, v in item.items():
                        kl = str(k).lower()
                        if kl in ("decimalodds", "odds", "odd", "price",
                                  "quota", "cotacao"):
                            n = _to_num(v)
                            if n and 1.01 <= n <= 1000:
                                o["price"] = n
                        elif kl in ("selection", "outcome", "label"):
                            o["selection"] = str(v)
                        elif kl in ("market", "markettype"):
                            o["market"] = str(v).lower()
                        elif kl in ("line", "total", "handicap"):
                            o["line"] = _to_num(v)
                    if "price" in o:
                        self.odds.append(o)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in ("1x2", "result", "resultado"):
                    if isinstance(v, dict):
                        for s in ("home", "draw", "away"):
                            n = _to_num(v.get(s) if s != "draw" else
                                        (v.get("draw") or v.get("x")))
                            if n:
                                self.odds.append(
                                    {"market": "1x2", "selection": s, "price": n})
                else:
                    self._eo(v if isinstance(v, list) else [v])

    def result(self) -> dict:
        return {
            "stats": dict(self.stats),
            "events": list(self.events),
            "fixture": dict(self.fixture),
            "odds": list(self.odds),
            "advanced": dict(self.advanced),
        }


# ===========================================================================
# FixtureState
# ===========================================================================
class FixtureState:
    def __init__(self, fid: str):
        self.fixture_id = str(fid)
        self.stats: Dict[str, List] = {}
        self.events: List[dict] = []
        self.fixture: Dict[str, Any] = {}
        self.odds: List[dict] = []
        self.advanced: Dict[str, Any] = {}
        self.last_update = 0.0
        self.responses = 0

    def merge(self, wr: dict) -> None:
        self.stats.update(wr.get("stats", {}))
        ex = {(e.get("minute"), e.get("team"), e.get("type")) for e in self.events}
        for ev in wr.get("events", []):
            k = (ev.get("minute"), ev.get("team"), ev.get("type"))
            if k not in ex:
                self.events.append(ev)
                ex.add(k)
        self.fixture.update(wr.get("fixture", {}))
        self.odds.extend(wr.get("odds", []))
        self.advanced.update(wr.get("advanced", {}))
        self.last_update = time.time()
        self.responses += 1

    def build_view(self) -> Optional[dict]:
        f = self.fixture
        if not f.get("home") or not f.get("away"):
            return None
        s = self.stats
        events = sorted(self.events, key=lambda e: e.get("minute", 0))
        ce = [
            {"minute": e["minute"], "team": e.get("team")}
            for e in events
            if "corner" in str(e.get("type", "")).lower()
            or "canto" in str(e.get("type", "")).lower()
        ]
        if not ce:
            ce = [{"minute": e["minute"], "team": e.get("team")} for e in events]
        cpi: List[Optional[float]] = [None, None]
        adv = self.advanced
        cd = adv.get("CPI_v2") or adv.get("cpi") or {}
        if isinstance(cd, dict):
            for i, sd in enumerate(("home", "away")):
                d = cd.get(sd) or cd.get(sd[0]) or {}
                if isinstance(d, dict):
                    cpi[i] = _to_num(d.get("cpi"))
                elif isinstance(d, (int, float)):
                    cpi[i] = float(d)
        t = adv.get("temporal") or {}
        pred = t.get("prediction_corner_2m") if isinstance(t, dict) else None
        if pred is None:
            pred = adv.get("prediction_corner_2m")
        return {
            "schema": "cornerai-analyst-1",
            "source": "aura-browser-v4",
            "exportedAt": _iso(),
            "fixture": {
                "id": self.fixture_id,
                "league": f.get("league"),
                "home": f.get("home"),
                "away": f.get("away"),
                "minute": f.get("minute"),
                "period": f.get("period"),
                "status": f.get("status") or ("live" if f.get("minute") else "unknown"),
                "score": {
                    "home": f.get("score_home", 0),
                    "away": f.get("score_away", 0),
                },
            },
            "pressure": {
                "attacks": s.get("attacks", [None, None]),
                "dangerous": s.get("dangerous", [None, None]),
                "shotsOn": s.get("shotsOn", [None, None]),
                "possession": s.get("possession", [None, None]),
                "xg": s.get("xg", [None, None]),
            },
            "corners": {"total": s.get("corners", [None, None]), "events": ce},
            "stats": {k: v for k, v in s.items()},
            "corner_events": ce,
            "match_events": events,
            "advanced_metrics": dict(adv) if adv else None,
            "cpi_home": cpi[0],
            "cpi_away": cpi[1],
            "pred": pred,
            "odds": self.odds if self.odds else None,
            "quality": {"score": 0.85 if f.get("home") else 0.2},
        }


# ===========================================================================
# GLM Bridge
# ===========================================================================
class GLMBridge:
    def __init__(self, *, api_url: str = "", api_key: str = "", model: str = "glm-4",
                 timeout: float = 30.0, max_tokens: int = 500):
        self.api_url = api_url or os.getenv(
            "GLM_API_URL", "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.api_key = api_key or os.getenv("GLM_API_KEY", "")
        self.model = model
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self._calls = 0
        self._errors = 0
        self._last: Optional[str] = None

    def format_prompt(self, view: dict) -> str:
        f = view.get("fixture", {})
        s = view.get("stats", {})
        sc = f.get("score", {})
        ev = view.get("corner_events", [])
        last = ", ".join(
            f"{e.get('minute', 0)}'({e.get('team', '?')})" for e in ev[-5:]
        ) or "nenhum"

        def pair(key, default=None):
            v = s.get(key, default if default is not None else [0, 0])
            if not isinstance(v, (list, tuple)) or len(v) < 2:
                return 0, 0
            return v[0] if v[0] is not None else 0, v[1] if v[1] is not None else 0

        c = pair("corners")
        a = pair("attacks")
        d = pair("dangerous")
        so = pair("shotsOn")
        xg = pair("xg")
        pos = pair("possession")
        fou = pair("fouls")
        yel = pair("yellow")
        sub = pair("subs")
        cr = pair("crosses")
        pa = pair("passes")
        lines = [
            f"Analise {f.get('home', '?')} x {f.get('away', '?')} ({f.get('league', '?')}):",
            f"Minuto: {f.get('minute', '?')}' Placar: {sc.get('home', 0)} x {sc.get('away', 0)}",
            "",
            "Stats (Casa|Visitante):",
            f"- Escanteios: {c[0]}|{c[1]}",
            f"- Ataques: {a[0]}|{a[1]}",
            f"- Perigosos: {d[0]}|{d[1]}",
            f"- Chutes alvo: {so[0]}|{so[1]}",
            f"- xG: {xg[0]}|{xg[1]}",
            f"- Posse: {pos[0]}%|{pos[1]}%",
            f"- Faltas: {fou[0]}|{fou[1]}",
            f"- Amarelos: {yel[0]}|{yel[1]}",
            f"- Subs: {sub[0]}|{sub[1]}",
            f"- Cruzamentos: {cr[0]}|{cr[1]}",
            f"- Passes: {pa[0]}|{pa[1]}",
            "",
            f"Ultimos cantos: {last}",
            f"CPI: {view.get('cpi_home', '?')}|{view.get('cpi_away', '?')}",
            "",
            "Responda: DECISAO | LADO | PROB | JUSTIFICATIVA",
            "DECISAO=ENTRA/AGUARDA/NAO ENTRA para escanteio em 10 min.",
        ]
        return "\n".join(lines)

    def call(self, prompt: str) -> Optional[str]:
        if not self.api_key:
            return None
        try:
            data = json.dumps({
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.max_tokens,
            }).encode("utf-8")
            req = urllib.request.Request(
                self.api_url, data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                })
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                resp = json.loads(r.read())
                self._calls += 1
                t = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                self._last = t
                return t
        except Exception as e:
            log.error("[glm] %s", e)
            self._errors += 1
            return None

    def get_advisory(self, view: dict) -> Optional[str]:
        return self.call(self.format_prompt(view))

    def parse_hold(self, adv: Optional[str]) -> bool:
        return bool(adv and "AGUARDA" in adv.upper())

    def stats(self) -> dict:
        return {
            "calls": self._calls,
            "errors": self._errors,
            "active": bool(self.api_key),
            "last_advisory": self._last[:200] if self._last else None,
        }


_SELENIUM_INTERCEPTOR_JS = r"""
(function(){'use strict';window.__aura_captured=[];window.__aura_ws_buf='';
const ALLOW=/(history|histor|fixture|match|stats|preodds|odds|x7|timeline|events|incidents)/i;
const DOM=/sokkerpro\.com/i;function cap(u,d){if(!d)return;
try{window.__aura_captured.push({url:u,data:d,ts:Date.now()});
if(window.__aura_captured.length>500)window.__aura_captured=window.__aura_captured.slice(-500);}catch(e){}}
const of=window.fetch;window.fetch=function(u,o){return of.call(this,u,o).then(r=>{
const s=String(u);if(DOM.test(s)&&ALLOW.test(s))r.clone().json().then(d=>cap(s,d)).catch(function(){});return r;});};
const oo=XMLHttpRequest.prototype.open,os=XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open=function(m,u){this.__url=String(u);return oo.apply(this,arguments);};
XMLHttpRequest.prototype.send=function(b){this.addEventListener('load',function(){
if(DOM.test(this.__url)&&ALLOW.test(this.__url)){try{cap(this.__url,JSON.parse(this.responseText));}catch(e){}}});
return os.apply(this,arguments);};const OW=window.WebSocket;
function WS(u,p){const w=p===undefined?new OW(u):new OW(u,p);if(DOM.test(u))w.addEventListener('message',function(e){
var d=e.data;if(typeof d==='string'){try{d=JSON.parse(d);}catch(e2){
window.__aura_ws_buf+=e.data;try{d=JSON.parse(window.__aura_ws_buf);window.__aura_ws_buf='';}catch(e3){return;}}}
cap('ws:'+u,d);});return w;}WS.prototype=OW.prototype;
WS.CONNECTING=OW.CONNECTING;WS.OPEN=OW.OPEN;WS.CLOSING=OW.CLOSING;WS.CLOSED=OW.CLOSED;
window.WebSocket=WS;console.info('[aura-interceptor] ativo');})();
"""


# ===========================================================================
# Drivers
# ===========================================================================
class _DriverBase:
    name = "base"

    def launch(self, *, headless, session_path, mode="full"):
        raise NotImplementedError

    def navigate(self, url, *, timeout=15.0):
        raise NotImplementedError

    def evaluate(self, js):
        raise NotImplementedError

    def wait_for(self, s, *, timeout=10.0):
        raise NotImplementedError

    def click(self, s):
        raise NotImplementedError

    def type_text(self, s, t):
        raise NotImplementedError

    def screenshot(self, p):
        raise NotImplementedError

    def save_session(self, p):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def is_alive(self):
        raise NotImplementedError

    def set_response_handler(self, h):
        pass

    def set_ws_handler(self, h):
        pass


class _MockDriver(_DriverBase):
    name = "mock"

    def __init__(self):
        self._alive = False
        self._url = ""
        self._rh = None
        self._wh = None
        self._mode = "full"

    def set_response_handler(self, h):
        self._rh = h

    def set_ws_handler(self, h):
        self._wh = h

    def launch(self, *, headless, session_path, mode="full"):
        self._alive = True
        self._mode = mode
        return True

    def navigate(self, url, *, timeout=15.0):
        if not self._alive:
            return False
        self._url = url
        threading.Thread(target=self._sim, daemon=True).start()
        return True

    def _sim(self):
        time.sleep(0.3)
        fid = _extract_fixture_id(self._url) or "123"
        canned = [
            (f"https://m2.sokkerpro.com/fixture/{fid}", {
                "home": "Home FC", "away": "Away United", "league": "Test",
                "minute": 82, "score": {"home": 1, "away": 0}, "status": "live"}),
            (f"https://m2.sokkerpro.com/fixture/{fid}/stats", {
                "corners": [5, 4], "attacks": [30, 22], "dangerous": [12, 8],
                "shotsOn": [3, 2], "shots": [10, 8], "shotsOff": [7, 6],
                "possession": [55, 45], "xg": [1.2, 0.8],
                "fouls": [10, 12], "offsides": [2, 1], "yellow": [1, 2], "red": [0, 0],
                "subs": [3, 2], "crosses": [15, 10], "saves": [2, 3],
                "passes": [400, 350], "passesFailed": [50, 40]}),
            (f"https://m4.sokkerpro.com/fixture/{fid}/events", [
                {"minute": 15, "team": "home", "type": "corner"},
                {"minute": 33, "team": "away", "type": "corner"},
                {"minute": 55, "team": "home", "type": "goal"}]),
            (f"https://m2.sokkerpro.com/fixture/{fid}/projecao", {
                "CPI_v2": {"home": {"cpi": 0.72}, "away": {"cpi": 0.45}},
                "temporal": {"prediction_corner_2m": 0.65}}),
        ]
        for url, body in canned:
            if self._rh:
                try:
                    self._rh(url, body)
                except Exception:
                    pass
            time.sleep(0.05)
        if self._wh:
            ws = f"wss://m2.sokkerpro.com/ws/fixture/{fid}"
            for frame in (
                {"minute": 83, "corners": [5, 5]},
                {"minute": 84, "events": [{"minute": 84, "team": "away", "type": "corner"}]},
                {"minute": 85, "score": {"home": 1, "away": 1}, "status": "live"},
            ):
                try:
                    self._wh(ws, frame)
                except Exception:
                    pass
                time.sleep(0.05)

    def evaluate(self, js):
        return None

    def wait_for(self, s, *, timeout=10.0):
        return True

    def click(self, s):
        return True

    def type_text(self, s, t):
        return True

    def screenshot(self, p):
        try:
            from pathlib import Path
            Path(p).write_bytes(b"\x89PNG")
            return True
        except Exception:
            return False

    def save_session(self, p):
        return True

    def close(self):
        self._alive = False

    def is_alive(self):
        return self._alive


class _PlaywrightDriver(_DriverBase):
    name = "playwright"

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._rh = None
        self._wh = None
        self._wsdec = _WSDecoder()
        self._mode = "full"

    def set_response_handler(self, h):
        self._rh = h

    def set_ws_handler(self, h):
        self._wh = h

    def launch(self, *, headless, session_path, mode="full"):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False
        try:
            self._mode = mode
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=headless,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
            from pathlib import Path as P
            if session_path and P(session_path).exists():
                self._context = self._browser.new_context(storage_state=session_path)
            else:
                self._context = self._browser.new_context()
            self._page = self._context.new_page()
            self._page.on("response", self._on_resp)
            self._page.on("websocket", self._on_ws)
            if mode == "lite":
                self._page.route("**/*", self._lite_route)
                log.info("[browser:pw] LITE mode — bloqueando imagens/fontes/CSS/ads")
            return True
        except Exception:
            log.exception("[browser:pw] falha")
            self.close()
            return False

    def _lite_route(self, route):
        req = route.request
        if req.resource_type in _LITE_BLOCK_TYPES:
            route.abort()
            return
        url = str(req.url).lower()
        if "sokkerpro.com" not in url and "127.0.0.1" not in url and "localhost" not in url:
            route.abort()
            return
        route.continue_()

    def _on_resp(self, response):
        if not self._rh:
            return
        try:
            url = response.url
            if not _is_sokkerpro_url(url) or not _is_interesting_path(url):
                return
            if not response.ok:
                return
            try:
                body = response.json()
            except Exception:
                try:
                    body = json.loads(response.text())
                except Exception:
                    return
            if body:
                self._rh(url, body)
        except Exception:
            pass

    def _on_ws(self, ws):
        url = getattr(ws, "url", "") or ""
        if not _is_sokkerpro_url(url):
            return
        log.info("[browser:pw] WebSocket: %s", url[:160])

        def _frame(*args):
            if not self._wh:
                return
            p = args[-1] if args else None
            if hasattr(p, "payload"):
                p = p.payload
            d = self._wsdec.decode(url, p)
            if d is None:
                d = WsFrameDecoder.decode(p)
            if d:
                self._wh(url, d)

        ws.on("framereceived", _frame)

    def navigate(self, url, *, timeout=15.0):
        if not self._page:
            return False
        try:
            self._page.goto(url, timeout=int(timeout * 1000))
            return True
        except Exception as e:
            log.error("[browser:pw] nav: %s", e)
            return False

    def evaluate(self, js):
        if not self._page:
            return None
        try:
            return self._page.evaluate(js)
        except Exception as e:
            log.error("[browser:pw] eval: %s", e)
            return None

    def wait_for(self, s, *, timeout=10.0):
        if not self._page:
            return False
        try:
            self._page.wait_for_selector(s, timeout=int(timeout * 1000))
            return True
        except Exception:
            return False

    def click(self, s):
        if not self._page:
            return False
        try:
            self._page.click(s, timeout=5000)
            return True
        except Exception:
            return False

    def type_text(self, s, t):
        if not self._page:
            return False
        try:
            self._page.fill(s, t, timeout=5000)
            return True
        except Exception:
            return False

    def screenshot(self, p):
        if not self._page:
            return False
        try:
            self._page.screenshot(path=p)
            return True
        except Exception:
            return False

    def save_session(self, p):
        if not self._context:
            return False
        try:
            self._context.storage_state(path=p)
            return True
        except Exception:
            return False

    def close(self):
        for a in ("_page", "_context", "_browser", "_pw"):
            o = getattr(self, a, None)
            if o is not None:
                try:
                    if a == "_pw":
                        o.stop()
                    else:
                        o.close()
                except Exception:
                    pass
                setattr(self, a, None)

    def is_alive(self):
        return self._page is not None and self._browser is not None


class _SeleniumDriver(_DriverBase):
    name = "selenium"

    def __init__(self):
        self._driver = None
        self._injected = False
        self._mode = "full"

    def launch(self, *, headless, session_path, mode="full"):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError:
            return False
        try:
            self._mode = mode
            opts = Options()
            if headless:
                opts.add_argument("--headless=new")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--no-sandbox")
            if mode == "lite":
                opts.add_experimental_option("prefs", {
                    "profile.managed_default_content_settings.images": 2,
                    "profile.managed_default_content_settings.fonts": 2,
                    "profile.managed_default_content_settings.stylesheets": 2,
                    "profile.managed_default_content_settings.media_stream": 2,
                })
                log.info("[browser:selenium] LITE mode — prefs de bloqueio")
            self._driver = webdriver.Chrome(options=opts)
            return True
        except Exception:
            log.exception("[browser:selenium] falha")
            self.close()
            return False

    def navigate(self, url, *, timeout=15.0):
        if not self._driver:
            return False
        try:
            self._driver.set_page_load_timeout(int(timeout))
            self._driver.get(url)
            self._inject()
            return True
        except Exception:
            return False

    def _inject(self):
        if self._injected:
            return
        try:
            self._driver.execute_script(_SELENIUM_INTERCEPTOR_JS)
            self._injected = True
        except Exception:
            pass

    def evaluate(self, js):
        if not self._driver:
            return None
        try:
            return self._driver.execute_script("return (" + js + ")()")
        except Exception:
            return None

    def wait_for(self, s, *, timeout=10.0):
        if not self._driver:
            return False
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            WebDriverWait(self._driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, s)))
            return True
        except Exception:
            return False

    def click(self, s):
        if not self._driver:
            return False
        try:
            from selenium.webdriver.common.by import By
            self._driver.find_element(By.CSS_SELECTOR, s).click()
            return True
        except Exception:
            return False

    def type_text(self, s, t):
        if not self._driver:
            return False
        try:
            from selenium.webdriver.common.by import By
            el = self._driver.find_element(By.CSS_SELECTOR, s)
            el.clear()
            el.send_keys(t)
            return True
        except Exception:
            return False

    def screenshot(self, p):
        if not self._driver:
            return False
        try:
            self._driver.save_screenshot(p)
            return True
        except Exception:
            return False

    def save_session(self, p):
        if not self._driver:
            return False
        try:
            from pathlib import Path as P
            P(p).write_text(json.dumps(self._driver.get_cookies()), encoding="utf-8")
            return True
        except Exception:
            return False

    def close(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def is_alive(self):
        return self._driver is not None


def _select_driver() -> _DriverBase:
    try:
        import playwright  # noqa: F401
        return _PlaywrightDriver()
    except ImportError:
        pass
    try:
        import selenium  # noqa: F401
        return _SeleniumDriver()
    except ImportError:
        pass
    log.warning("[browser] sem Playwright/Selenium — Mock")
    return _MockDriver()


# ===========================================================================
# Browser Agent v4
# ===========================================================================
class BrowserAgent:
    """v4: dual-mode (full/lite) + agent API + GLM + WS realtime."""

    def __init__(self, *, feed_bus=None, bridge_url="", governor=None,
                 headless=True, mode="lite", session_path=None,
                 capture_interval=1.0, min_publish_interval=0.5,
                 poll_interval=0.5, glm=None):
        self.feed_bus = feed_bus
        self.bridge_url = bridge_url or "http://127.0.0.1:8080/api/cornerai/feed"
        self.governor = governor
        self.headless = headless
        self.mode = mode
        self.session_path = session_path
        self.capture_interval = float(capture_interval)
        self.min_publish_interval = float(min_publish_interval)
        self.poll_interval = float(poll_interval)
        self.glm = glm

        self._driver: Optional[_DriverBase] = None
        self._states: Dict[str, FixtureState] = {}
        self._capturing = False
        self._poll_thread: Optional[threading.Thread] = None
        self._nav_lock = threading.Lock()
        self._last_publish: Dict[str, float] = {}
        self._last_fp: Dict[str, str] = {}

        self._captures = 0
        self._published = 0
        self._errors = 0
        self._nav_count = 0
        self._launched_at = 0.0
        self._ws_frames = 0
        self._ws_last_ts = 0.0

    @property
    def driver_name(self) -> str:
        return self._driver.name if self._driver else "none"

    def is_alive(self) -> bool:
        return self._driver is not None and self._driver.is_alive()

    def launch(self) -> bool:
        if self._driver is not None and self._driver.is_alive():
            return True
        if self.governor is not None:
            try:
                if not self.governor.can_run_background():
                    log.warning("[browser] governor bloqueou")
                    return False
            except Exception:
                pass
        with self._nav_lock:
            self._driver = _select_driver()
            self._driver.set_response_handler(self._on_response)
            self._driver.set_ws_handler(self._on_ws_frame)
            ok = self._driver.launch(
                headless=self.headless,
                session_path=self.session_path,
                mode=self.mode)
            if ok:
                self._launched_at = time.time()
                log.info("[browser] %s lancado (mode=%s)", self._driver.name, self.mode)
            return ok

    def close(self) -> None:
        self.stop_capture()
        with self._nav_lock:
            if self._driver:
                try:
                    self._driver.close()
                except Exception:
                    pass
                self._driver = None

    def navigate(self, url: str, *, timeout: float = 15.0) -> bool:
        if not self.is_alive():
            return False
        with self._nav_lock:
            ok = self._driver.navigate(url, timeout=timeout)
            if ok:
                self._nav_count += 1
            return ok

    def evaluate(self, js: str) -> Any:
        if not self.is_alive():
            return None
        with self._nav_lock:
            return self._driver.evaluate(js)

    def click(self, s: str) -> bool:
        if not self.is_alive():
            return False
        with self._nav_lock:
            return self._driver.click(s)

    def type_text(self, s: str, t: str) -> bool:
        if not self.is_alive():
            return False
        with self._nav_lock:
            return self._driver.type_text(s, t)

    def screenshot(self, p: str) -> bool:
        if not self.is_alive():
            return False
        with self._nav_lock:
            return self._driver.screenshot(p)

    def save_session(self, path: Optional[str] = None) -> bool:
        path = path or self.session_path
        if not path or not self.is_alive():
            return False
        with self._nav_lock:
            return self._driver.save_session(path)

    def login(self, url: str, username: str, password: str, *,
              user_sel: str = 'input[type="text"]',
              pass_sel: str = 'input[type="password"]',
              submit_sel: str = 'button[type="submit"]') -> bool:
        if not self.is_alive():
            return False
        with self._nav_lock:
            try:
                if not self._driver.navigate(url):
                    return False
                if not self._driver.wait_for(user_sel, timeout=10.0):
                    return False
                if not self._driver.type_text(user_sel, username):
                    return False
                if not self._driver.type_text(pass_sel, password):
                    return False
                if not self._driver.click(submit_sel):
                    return False
                time.sleep(2.0)
                if self.session_path:
                    self._driver.save_session(self.session_path)
                log.info("[browser] login executado")
                return True
            except Exception as e:
                log.error("[browser] login: %s", e)
                return False

    def login_manual(self, url: str) -> bool:
        self.headless = False
        if not self.launch():
            return False
        if not self.navigate(url):
            return False
        log.info("[browser] login manual — faca login e chame save_session()")
        return True

    # --- Agent API ---
    def extract_view(self) -> Optional[dict]:
        with self._nav_lock:
            if not self._states:
                return None
            latest = max(self._states.values(), key=lambda s: s.last_update)
            return latest.build_view()

    def get_active_fixture(self) -> Optional[str]:
        with self._nav_lock:
            if not self._states:
                return None
            return max(self._states.values(), key=lambda s: s.last_update).fixture_id

    def ask_glm(self, question: str = "") -> Optional[str]:
        if not self.glm:
            return None
        view = self.extract_view()
        if not view:
            return None
        prompt = self.glm.format_prompt(view)
        if question:
            prompt += f"\n\nPergunta extra: {question}"
        return self.glm.call(prompt)

    def start_capture(self, url: Optional[str] = None) -> bool:
        if not self.is_alive():
            if not self.launch():
                return False
        if url:
            if not self.navigate(url):
                return False
        fid = _extract_fixture_id(url or "")
        if fid:
            self._bootstrap(fid)
        if self.driver_name == "selenium":
            self._start_poll()
        if self._capturing:
            return True
        self._capturing = True
        log.info("[browser] captura iniciada (mode=%s,driver=%s)",
                 self.mode, self.driver_name)
        return True

    def stop_capture(self) -> None:
        self._capturing = False
        if self._poll_thread:
            self._poll_thread.join(timeout=3.0)
            self._poll_thread = None
        log.info("[browser] captura parada")

    def _bootstrap(self, fid: str) -> None:
        paths = [p.replace("{fid}", str(fid)) for p in ALL_BOOTSTRAP]
        js = (
            "()=>{const p=%s,o=%s;"
            "for(const x of o)for(const y of p){"
            "fetch(x+y,{credentials:'include'})"
            ".then(r=>r.json().catch(()=>null))"
            ".then(d=>{if(d)console.log('ok',y);}).catch(()=>{});}}"
        ) % (json.dumps(paths), json.dumps(list(BOOTSTRAP_ORIGINS)))
        try:
            with self._nav_lock:
                self._driver.evaluate(js)
            log.info("[browser] bootstrap: %d paths fixture %s", len(paths), fid)
        except Exception:
            pass

    def _start_poll(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._capturing = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="browser-poll", daemon=True)
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        while self._capturing and self.is_alive():
            try:
                with self._nav_lock:
                    cap = self._driver.evaluate(
                        "()=>{const a=window.__aura_captured||[];"
                        "window.__aura_captured=[];return a;}")
                if isinstance(cap, list):
                    for item in cap:
                        u = item.get("url", "") if isinstance(item, dict) else ""
                        d = item.get("data") if isinstance(item, dict) else item
                        if d:
                            self._process(u, d)
            except Exception:
                pass
            time.sleep(self.poll_interval)

    def _on_response(self, url: str, body: Any) -> None:
        try:
            self._process(url, body)
        except Exception as e:
            log.error("[browser] _on_resp: %s", e)
            self._errors += 1

    def _on_ws_frame(self, url: str, payload: Any) -> None:
        try:
            data = WsFrameDecoder.decode(payload)
            if data is None:
                data = payload
            self._ws_frames += 1
            self._ws_last_ts = time.time()
            self._process(url, data)
        except Exception as e:
            log.error("[browser] _on_ws: %s", e)
            self._errors += 1

    def _process(self, url: str, body: Any) -> None:
        fid = _extract_fixture_id(url) or "unknown"
        if isinstance(body, dict):
            for k in ("fixtureId", "fixture_id", "matchId", "match_id", "id"):
                v = body.get(k)
                if v is not None and str(v).isdigit() and len(str(v)) >= 5:
                    fid = str(v)
                    break
        with self._nav_lock:
            if fid not in self._states:
                self._states[fid] = FixtureState(fid)
            state = self._states[fid]
        walker = HeuristicWalker()
        walker.walk(body)
        state.merge(walker.result())
        self._captures += 1
        view = state.build_view()
        if view:
            self._publish(fid, view)

    def _publish(self, fid: str, view: dict) -> None:
        fp = "|".join(str(x) for x in (
            view.get("fixture", {}).get("minute"),
            view.get("fixture", {}).get("score", {}).get("home"),
            view.get("fixture", {}).get("score", {}).get("away"),
            view.get("corners", {}).get("total"),
            len(view.get("corner_events", [])),
        ))
        now = time.time()
        last = self._last_publish.get(fid, 0)
        if fp == self._last_fp.get(fid, "") and now - last < 5.0:
            return
        if now - last < self.min_publish_interval:
            return
        self._last_publish[fid] = now
        self._last_fp[fid] = fp

        if self.glm:
            try:
                adv = self.glm.get_advisory(view)
                if adv:
                    view["glm_advisory"] = adv
                    if self.glm.parse_hold(adv):
                        view["glm_hold"] = True
            except Exception:
                pass

        if self.feed_bus:
            try:
                ok = self.feed_bus.publish(
                    {"received_at": _iso(), "view": view,
                     "payload": view, "fingerprint": fp},
                    key=fid)
                if ok:
                    self._published += 1
                return
            except Exception as e:
                log.error("[browser] bus: %s", e)
                self._errors += 1
        try:
            data = json.dumps(view, ensure_ascii=False, default=str).encode("utf-8")
            req = urllib.request.Request(
                self.bridge_url, data=data,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3.0) as r:
                if r.status == 200:
                    self._published += 1
        except Exception as e:
            log.error("[browser] http: %s", e)
            self._errors += 1

    def get_state(self, fid: str) -> Optional[FixtureState]:
        with self._nav_lock:
            return self._states.get(str(fid))

    def stats(self) -> dict:
        with self._nav_lock:
            return {
                "driver": self.driver_name,
                "alive": self.is_alive(),
                "capturing": self._capturing,
                "mode": self.mode,
                "captures": self._captures,
                "published": self._published,
                "errors": self._errors,
                "navigations": self._nav_count,
                "fixtures_tracked": len(self._states),
                "ws_frames": self._ws_frames,
                "ws_last_ts": self._ws_last_ts,
                "uptime_sec": round(time.time() - self._launched_at, 1)
                              if self._launched_at else 0,
                "glm": self.glm.stats() if self.glm else None,
            }


BROWSER = BrowserAgent(mode="lite", headless=True)
BROWSER_FULL = BrowserAgent(mode="full", headless=False)


# ===========================================================================
# Self-test
# ===========================================================================
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    errs: List[str] = []

    def check(n, c, x=""):
        s = "PASS" if c else "FAIL"
        print(f"[{s}] {n}" + (f" — {x}" if x else ""))
        if not c:
            errs.append(n)

    w = HeuristicWalker()
    w.walk({
        "corners": [5, 4], "escanteios": [3, 2], "home": "A", "away": "B",
        "minute": 82, "score": {"home": 1, "away": 0},
        "fouls": [10, 12], "throw_ins": [20, 15],
    })
    r = w.result()
    check("walker: corners PT override", r["stats"].get("corners") == [3, 2])
    check("walker: fouls", r["stats"].get("fouls") == [10, 12])
    check("walker: unknown throw_ins", r["stats"].get("throwins") == [20, 15])
    check("walker: fixture", r["fixture"].get("home") == "A")
    check("walker: score 1-0",
          r["fixture"].get("score_home") == 1 and r["fixture"].get("score_away") == 0)

    # score 0-0
    w0 = HeuristicWalker()
    w0.walk({"score": {"home": 0, "away": 0}, "home": "X", "away": "Y"})
    check("walker: score 0-0",
          w0.result()["fixture"].get("score_home") == 0
          and w0.result()["fixture"].get("score_away") == 0)

    wa = HeuristicWalker()
    wa.walk({
        "corners": [5, 4], "attacks": [30, 22], "dangerous": [12, 8],
        "shots": [10, 8], "shotsOn": [3, 2], "shotsOff": [7, 6],
        "possession": [55, 45], "xg": [1.2, 0.8], "fouls": [10, 12],
        "offsides": [2, 1], "yellow": [1, 2], "red": [0, 0],
        "subs": [3, 2], "crosses": [15, 10], "saves": [2, 3],
        "passes": [400, 350], "passesFailed": [50, 40],
    })
    exp = {
        "corners", "attacks", "dangerous", "shots", "shotsOn", "shotsOff",
        "possession", "xg", "fouls", "offsides", "yellow", "red", "subs",
        "crosses", "saves", "passes", "passesFailed",
    }
    check("walker: 16 stats", not (exp - set(wa.result()["stats"].keys())))

    dec = _WSDecoder()
    check("ws: direto", dec.decode("ws://x", '{"a":1}') == {"a": 1})
    dec.reset()
    check("ws: chunk", dec.decode("ws://x", '{"a":') is None)
    check("ws: chunk completo", dec.decode("ws://x", "1}") == {"a": 1})
    check("ws: length-prefix",
          WsFrameDecoder.decode(struct.pack(">I", 11) + b'{"x":1}') == {"x": 1}
          or WsFrameDecoder.decode(struct.pack(">I", 7) + b'{"x":1}') is not None)

    glm = GLMBridge(api_key="")
    check("glm: sem key desativado", glm.stats()["active"] is False)
    p = glm.format_prompt({
        "fixture": {"home": "A", "away": "B", "minute": 82, "league": "L",
                    "score": {"home": 1, "away": 0}},
        "stats": {"corners": [5, 4], "attacks": [30, 22]},
    })
    check("glm: prompt tem dados", "A x B" in p and "Escanteios" in p)
    check("glm: get_advisory None", glm.get_advisory({}) is None)
    check("glm: parse_hold", glm.parse_hold("AGUARDA") is True)

    pub: List[dict] = []

    class FakeBus:
        def publish(self, rec, **kw):
            pub.append(rec)
            return True

    b = BrowserAgent(feed_bus=FakeBus(), mode="lite", headless=True,
                     glm=glm, min_publish_interval=0.0)
    check("browser: mode=lite", b.mode == "lite")
    check("browser: launch", b.launch())
    check("browser: start_capture",
          b.start_capture("https://sokkerpro.com/fixture/19703774"))
    time.sleep(1.8)
    st = b.stats()
    check("browser: captures>0", st["captures"] > 0, f"{st['captures']}")
    check("browser: published>0", st["published"] > 0, f"{st['published']}")
    check("browser: mode in stats", st.get("mode") == "lite")
    check("browser: ws_frames>0", st.get("ws_frames", 0) > 0,
          f"ws={st.get('ws_frames')}")

    v = b.extract_view()
    check("agent: extract_view",
          v is not None and v.get("schema") == "cornerai-analyst-1")
    fid = b.get_active_fixture()
    check("agent: get_active_fixture", fid is not None)
    check("agent: ask_glm None sem key", b.ask_glm() is None)

    check("BROWSER: mode lite", BROWSER.mode == "lite")
    check("BROWSER_FULL: mode full", BROWSER_FULL.mode == "full")
    check("browser: _nav_lock existe", hasattr(b, "_nav_lock"))
    check("js: __aura_captured", "__aura_captured" in _SELENIUM_INTERCEPTOR_JS)
    check("js: fetch wrap", "window.fetch" in _SELENIUM_INTERCEPTOR_JS)
    check("js: WS wrap", "WebSocket" in _SELENIUM_INTERCEPTOR_JS)

    if v:
        check("agent: view 10+ stats",
              len(v.get("stats") or {}) >= 10,
              f"{len(v.get('stats') or {})} stats")

    b.close()
    print(f"\nbrowser_agent v4 selftest: {len(errs)} falha(s)")
    print(f"driver: {_select_driver().name}")
    sys.exit(1 if errs else 0)
