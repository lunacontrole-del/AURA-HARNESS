#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desktop_controller.py — MAOS do assistente: teclado, mouse, clipboard,
screenshot (GDI/stdlib) e macros (skills), com kill-switch ESC e gate de mesa.

FRONTEIRAS (leia antes de ampliar):
    - GATE DE MESA: comandos vindos do TELEGRAM so controlam o desktop com
      sessao liberada (/liberar PIN, TTL 10 min, PIN em AURA_DESK_PIN).
      Comandos locais (voz — servidor localhost) sempre podem. Sem PIN
      configurado: controle remoto permanentemente desativado.
    - KILL-SWITCH FISICO: ESC pressionado aborta qualquer macro entre passos.
    - Toda macro: PLANO + 'sim' (padrao do CommandCenter).
    - Teclado-first: atalhos sao estaveis; cliques exigem coordenadas ditadas
      ou salvas em macro (sem visao, mouse as cegas e fragil).
    - Screenshot NAO grava video nem envia sozinho; salva em
      engine/data/screenshots/ e o caller decide (Telegram envia sob ordem).

IMPLANTACAO: ctypes puro (user32/kernel32/gdi32) — zero dependencias.
Windows only; em outros SOs o modulo carrega e DEGRADA (self-test SKIPa
hardware, logica pura roda).

INTEGRACAO: hunks na resposta — voice server (startup), command_center
(parser na cadeia externa), telegram_employee usa o mesmo gate.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import struct
import threading
import time
import unicodedata
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("aura.desktop")

__version__ = "1.0.0"
_PROJ_ROOT = Path(__file__).resolve().parents[2]
_IS_WINDOWS = os.name == "nt"

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
VK_ESCAPE = 0x1B
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SRCCOPY = 0x00CC0020

_VK_MAP: Dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "enter": 0x0D, "return": 0x0D, "esc": VK_ESCAPE, "escape": VK_ESCAPE,
    "tab": 0x09, "backspace": 0x08, "bs": 0x08, "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "win": 0x5B, "super": 0x5B, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "insert": 0x2D,
}
for _i in range(1, 13):
    _VK_MAP["f%d" % _i] = 0x70 + _i - 1
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    _VK_MAP[_c] = ord(_c.upper())


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_hotkey(spec: str) -> List[int]:
    """'ctrl+shift+s' -> [VK_CONTROL, VK_SHIFT, ord('S')]; [] se invalido."""
    keys = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not keys:
        return []
    vks: List[int] = []
    for k in keys:
        if k in _VK_MAP:
            vks.append(_VK_MAP[k])
        elif len(k) == 1 and k.isalnum():
            vks.append(ord(k.upper()))
        else:
            return []
    return vks


class MacroStep(dict):
    """Passo: {"press": "ctrl+s"} | {"type": "texto"} | {"key": "enter"} |
    {"click": [x, y]} | {"move": [x, y]} | {"wait": 0.3} |
    {"focus": "whatsapp"} | {"clipboard": "texto"}"""


def validate_steps(steps: List[dict]) -> Optional[str]:
    """Devolve motivo de invalidade ou None se ok."""
    if not isinstance(steps, list) or not steps:
        return "macro sem passos"
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or len(s) != 1:
            return "passo %d: dict com uma unica acao" % (i + 1)
        op, val = next(iter(s.items()))
        if op == "press":
            if not parse_hotkey(str(val)):
                return "passo %d: hotkey invalida '%s'" % (i + 1, val)
        elif op in ("type", "clipboard"):
            if not isinstance(val, str):
                return "passo %d: %s exige texto" % (i + 1, op)
        elif op == "key":
            if not parse_hotkey(str(val)):
                return "passo %d: tecla invalida" % (i + 1)
        elif op in ("click", "move"):
            if (not isinstance(val, (list, tuple)) or len(val) != 2
                    or not all(isinstance(v, (int, float)) for v in val)):
                return "passo %d: %s exige [x, y]" % (i + 1, op)
        elif op == "wait":
            if not isinstance(val, (int, float)) or not (0 <= val <= 30):
                return "passo %d: wait 0 a 30s" % (i + 1)
        elif op == "focus":
            if not isinstance(val, str) or not val.strip():
                return "passo %d: focus exige titulo" % (i + 1)
        else:
            return "passo %d: acao desconhecida '%s'" % (i + 1, op)
    return None


# ---------------------------------------------------------------------------
# estruturas Win32 (ctypes)
# ---------------------------------------------------------------------------
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class _INPUTunion(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTunion))


class _BMIH(ctypes.Structure):
    _fields_ = (("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD))


class _BMPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", _BMIH), ("bmiColors", wintypes.DWORD * 3))


class DeskLockedError(PermissionError):
    pass


# ---------------------------------------------------------------------------
# gate de mesa — remote (Telegram) so com sessao liberada por PIN
# ---------------------------------------------------------------------------
class DeskSession:
    """Gate de controle de desktop. Origin thread-local: 'local' (voz) ou
    'remote' (Telegram). Remoto exige AURA_DESK_PIN + liberacao (TTL 10 min)."""

    TTL = 600.0

    def __init__(self, pin: Optional[str] = None):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._until = 0.0
        self._pin = (pin if pin is not None
                     else os.environ.get("AURA_DESK_PIN", "")).strip()
        self.counts = {"remote_blocked": 0, "remote_granted": 0,
                       "remote_rejected_pin": 0}

    def set_origin(self, origin: str) -> None:
        self._local.origin = "remote" if origin == "remote" else "local"

    @property
    def origin(self) -> str:
        return getattr(self._local, "origin", "local")

    @property
    def remote_enabled(self) -> bool:
        return bool(self._pin)

    def authorize(self, pin: str) -> dict:
        if not self._pin:
            return {"ok": False, "speech": "Controle remoto de mesa esta "
                    "DESATIVADO (AURA_DESK_PIN nao configurado no PC)."}
        if (pin or "").strip() != self._pin:
            self.counts["remote_rejected_pin"] += 1
            return {"ok": False, "speech": "PIN incorreto."}
        with self._lock:
            self._until = time.time() + self.TTL
            self.counts["remote_granted"] += 1
        return {"ok": True, "speech": "Mesa liberada por 10 minutos para "
                "comandos do Telegram."}

    def ensure(self) -> None:
        """Levanta DeskLockedError se origem remota sem sessao ativa."""
        if self.origin == "local":
            return
        with self._lock:
            until = self._until
        if time.time() < until:
            return
        self.counts["remote_blocked"] += 1
        raise DeskLockedError(
            "Controle de mesa bloqueado para o Telegram. "
            "Use /liberar <PIN> (PIN configurado em AURA_DESK_PIN no PC).")

    def stats(self) -> dict:
        with self._lock:
            return {"desk_session": {
                "origin_now": self.origin,
                "remote_enabled": self.remote_enabled,
                "active": time.time() < self._until,
                **self.counts}}


# ---------------------------------------------------------------------------
# controller
# ---------------------------------------------------------------------------
class DesktopController:
    STEP_GAP = 0.04          # pausa minima entre eventos (seguranca)
    TYPE_DELAY = 0.012       # por caractere digitado

    def __init__(self, gate: Optional[DeskSession] = None,
                 screenshots_dir: Optional[Any] = None,
                 send_input=None, get_cursor_pos=None, set_cursor_pos=None,
                 get_async_key_state=None, focus_fn=None,
                 clipboard_set=None, clipboard_get=None, screenshot_fn=None,
                 active_title_fn=None):
        self.gate = gate or DeskSession()
        self._shots = (Path(screenshots_dir) if screenshots_dir is not None
                       else _PROJ_ROOT / "engine" / "data" / "screenshots")
        # injecao p/ testes; None -> ctypes real (Windows)
        self._send_input = send_input
        self._get_cursor = get_cursor_pos
        self._set_cursor = set_cursor_pos
        self._get_key_state = get_async_key_state
        self._focus = focus_fn
        self._cb_set = clipboard_set
        self._cb_get = clipboard_get
        self._shot = screenshot_fn
        self._title = active_title_fn
        self.counts = {"keys": 0, "typed_chars": 0, "clicks": 0,
                       "moves": 0, "macros_run": 0, "macros_aborted": 0,
                       "screenshots": 0, "clipboard_sets": 0}
        if _IS_WINDOWS and self._send_input is None:
            self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        else:
            self._user32 = None

    # ------------------------------------------------------------ primitivas
    def _win(self) -> bool:
        return _IS_WINDOWS or self._send_input is not None

    def _send_key_vk(self, vk: int, up: bool) -> None:
        if self._send_input is not None:
            self._send_input(vk, up)  # injetado (teste)
            return
        if not _IS_WINDOWS:
            raise RuntimeError("teclado: so no Windows")
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
        self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        self.counts["keys"] += 1

    def _send_unicode(self, ch: str, up: bool) -> None:
        if self._send_input is not None:
            self._send_input(ord(ch), up)
            return
        if not _IS_WINDOWS:
            raise RuntimeError("teclado unicode: so no Windows")
        inp = _INPUT(type=INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(0, ord(ch),
                             KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                             0, None)
        self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def _mouse_flags(self, flags: int) -> None:
        inp = _INPUT(type=INPUT_MOUSE)
        inp.mi = _MOUSEINPUT(0, 0, 0, flags, 0, None)
        self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def _check_kill(self) -> bool:
        """True se ESC pressionado (kill-switch)."""
        if self._get_key_state is not None:
            return bool(self._get_key_state(VK_ESCAPE))
        if not _IS_WINDOWS:
            return False
        return bool(self._user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)

    # ------------------------------------------------------------ api publica
    def press(self, hotkey: str) -> dict:
        vks = parse_hotkey(hotkey)
        if not vks:
            return {"ok": False, "speech": "Hotkey invalida: %s" % hotkey}
        self.gate.ensure()
        for vk in vks:
            self._send_key_vk(vk, up=False)
            time.sleep(self.STEP_GAP)
        for vk in reversed(vks):
            self._send_key_vk(vk, up=True)
            time.sleep(self.STEP_GAP)
        return {"ok": True, "speech": "Apertei %s." % hotkey}

    def type_text(self, text: str) -> dict:
        text = str(text or "")
        if not text:
            return {"ok": False, "speech": "Nada para digitar."}
        if len(text) > 2000:
            return {"ok": False, "speech": "Texto longo demais para digitar."}
        self.gate.ensure()
        if not _IS_WINDOWS and self._send_input is None:
            return {"ok": False, "speech": "Digitacao: so no Windows."}
        for ch in text:
            if self._check_kill():
                return {"ok": False, "speech": "Digitacao abortada pelo ESC."}
            self._send_unicode(ch, up=False)
            self._send_unicode(ch, up=True)
            self.counts["typed_chars"] += 1
            time.sleep(self.TYPE_DELAY)
        return {"ok": True, "speech": "Digitei %d caracteres." % len(text)}

    def cursor_position(self) -> Tuple[int, int]:
        if self._get_cursor is not None:
            return self._get_cursor()
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def move_to(self, x: float, y: float, smooth: bool = True) -> dict:
        if not self._win():
            return {"ok": False, "speech": "Mouse: so no Windows."}
        self.gate.ensure()
        x, y = int(x), int(y)
        if smooth:
            sx, sy = self.cursor_position()
            steps = 12
            for i in range(1, steps + 1):
                if self._check_kill():
                    return {"ok": False, "speech": "Movimento abortado."}
                nx = sx + (x - sx) * i // steps
                ny = sy + (y - sy) * i // steps
                self._raw_set_cursor(nx, ny)
                time.sleep(0.012)
        else:
            self._raw_set_cursor(x, y)
        self.counts["moves"] += 1
        return {"ok": True, "speech": "Mouse em %d, %d." % (x, y)}

    def _raw_set_cursor(self, x: int, y: int) -> None:
        if self._set_cursor is not None:
            self._set_cursor(x, y)
            return
        ctypes.windll.user32.SetCursorPos(x, y)

    def click(self, x: Optional[float] = None, y: Optional[float] = None,
              button: str = "left", double: bool = False) -> dict:
        if not self._win():
            return {"ok": False, "speech": "Mouse: so no Windows."}
        self.gate.ensure()
        if x is not None and y is not None:
            r = self.move_to(x, y)
            if not r.get("ok"):
                return r
        if self._check_kill():
            return {"ok": False, "speech": "Clique abortado pelo ESC."}
        down, up = ((MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
                    if button == "left"
                    else (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP))
        for _ in range(2 if double else 1):
            self._mouse_flags(down)
            time.sleep(0.03)
            self._mouse_flags(up)
            time.sleep(0.08)
        self.counts["clicks"] += 1
        where = "" if x is None else " em %d, %d" % (int(x), int(y))
        return {"ok": True, "speech": "Clique %s%s%s."
                % (button, where, " (duplo)" if double else "")}

    def clipboard_set(self, text: str) -> dict:
        self.gate.ensure()
        if self._cb_set is not None:
            self._cb_set(text)
            self.counts["clipboard_sets"] += 1
            return {"ok": True, "speech": "Texto no clipboard."}
        if not _IS_WINDOWS:
            return {"ok": False, "speech": "Clipboard: so no Windows."}
        data = (str(text) + "\0").encode("utf-16-le")
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return {"ok": False, "speech": "Clipboard: alocacao falhou."}
        locked = k32.GlobalLock(h)
        ctypes.memmove(locked, data, len(data))
        k32.GlobalUnlock(h)
        for _ in range(5):
            if u32.OpenClipboard(0):
                try:
                    u32.EmptyClipboard()
                    if u32.SetClipboardData(CF_UNICODETEXT, h):
                        self.counts["clipboard_sets"] += 1
                        return {"ok": True,
                                "speech": "Texto no clipboard."}
                    return {"ok": False, "speech": "Clipboard: falha ao setar."}
                finally:
                    u32.CloseClipboard()
            time.sleep(0.05)
        return {"ok": False, "speech": "Clipboard ocupado; tente de novo."}

    def clipboard_get(self) -> str:
        if self._cb_get is not None:
            return self._cb_get()
        if not _IS_WINDOWS:
            return ""
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        for _ in range(5):
            if u32.OpenClipboard(0):
                try:
                    h = u32.GetClipboardData(CF_UNICODETEXT)
                    if not h:
                        return ""
                    locked = k32.GlobalLock(h)
                    try:
                        return ctypes.wstring_at(locked)
                    finally:
                        k32.GlobalUnlock(h)
                finally:
                    u32.CloseClipboard()
            time.sleep(0.05)
        return ""

    def active_window_title(self) -> str:
        if self._title is not None:
            return self._title()
        if not _IS_WINDOWS:
            return ""
        u32 = ctypes.windll.user32
        hwnd = u32.GetForegroundWindow()
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def focus_window(self, needle: str) -> dict:
        if self._focus is not None:
            ok = self._focus(needle)
            return ({"ok": True, "speech": "Foco em %s." % needle} if ok
                    else {"ok": False,
                          "speech": "Janela com '%s' nao encontrada." % needle})
        if not _IS_WINDOWS:
            return {"ok": False, "speech": "Foco de janela: so no Windows."}
        u32 = ctypes.windll.user32
        needle_l = _norm(needle)
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _l):
            if u32.IsWindowVisible(hwnd):
                n = u32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u32.GetWindowTextW(hwnd, buf, n + 1)
                    if needle_l in _norm(buf.value):
                        found.append(hwnd)
                        return False
            return True

        u32.EnumWindows(cb, 0)
        if not found:
            return {"ok": False,
                    "speech": "Janela com '%s' nao encontrada." % needle}
        hwnd = found[0]
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.35)
        return {"ok": True, "speech": "Foco na janela %s." % needle}

    def screenshot(self, prefix: str = "tela") -> dict:
        """Salva BMP (stdlib GDI). Nao envia a lugar nenhum."""
        self.gate.ensure()
        if self._shot is not None:
            path = self._shot(prefix)
            self.counts["screenshots"] += 1
            return {"ok": True, "speech": "Print salvo.", "path": str(path)}
        if not _IS_WINDOWS:
            return {"ok": False, "speech": "Screenshot: so no Windows."}
        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32
        w = u32.GetSystemMetrics(0)
        h = u32.GetSystemMetrics(1)
        hdc = u32.GetDC(0)
        mem = g32.CreateCompatibleDC(hdc)
        bmp = g32.CreateCompatibleBitmap(hdc, w, h)
        g32.SelectObject(mem, bmp)
        g32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, SRCCOPY)
        bmi = _BMPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = h  # bottom-up
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        bmi.bmiHeader.biCompression = 0
        row = (w * 3 + 3) & ~3
        buf = ctypes.create_string_buffer(row * h)
        g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(0, hdc)
        self._shots.mkdir(parents=True, exist_ok=True)
        path = self._shots / ("%s_%s.bmp"
                              % (_norm(prefix).replace(" ", "_")[:20],
                                 time.strftime("%Y%m%d_%H%M%S")))
        data_size = row * h
        header = struct.pack("<2sIHHI", b"BM", 54 + data_size, 0, 0, 54)
        info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, data_size,
                           2835, 2835, 0, 0)
        path.write_bytes(header + info + bytes(buf))
        self.counts["screenshots"] += 1
        return {"ok": True, "speech": "Print da tela salvo.",
                "path": str(path), "size": [w, h]}

    # ------------------------------------------------------------ macros
    def describe_steps(self, steps: List[dict]) -> str:
        parts = []
        for s in steps:
            op, val = next(iter(s.items()))
            if op == "press":
                parts.append("apertar %s" % val)
            elif op == "type":
                parts.append("digitar '%s...'" % str(val)[:40])
            elif op == "key":
                parts.append("tecla %s" % val)
            elif op == "click":
                parts.append("clicar em %d,%d" % (val[0], val[1]))
            elif op == "move":
                parts.append("mover mouse para %d,%d" % (val[0], val[1]))
            elif op == "wait":
                parts.append("aguardar %ss" % val)
            elif op == "focus":
                parts.append("focar janela %s" % val)
            elif op == "clipboard":
                parts.append("colocar '%s...' no clipboard" % str(val)[:30])
        return "; ".join(parts)

    def run_steps(self, steps: List[dict]) -> dict:
        err = validate_steps(steps)
        if err:
            return {"ok": False, "speech": "Macro invalida: %s" % err}
        self.gate.ensure()
        self.counts["macros_run"] += 1
        done = 0
        for s in steps:
            if self._check_kill():
                self.counts["macros_aborted"] += 1
                return {"ok": False,
                        "speech": "Macro abortada pelo ESC apos %d passo(s)."
                        % done}
            op, val = next(iter(s.items()))
            if op == "press" or op == "key":
                r = self.press(str(val))
            elif op == "type":
                r = self.type_text(str(val))
            elif op == "click":
                r = self.click(val[0], val[1])
            elif op == "move":
                r = self.move_to(val[0], val[1], smooth=False)
            elif op == "wait":
                time.sleep(float(val))
                r = {"ok": True}
            elif op == "focus":
                r = self.focus_window(str(val))
            elif op == "clipboard":
                r = self.clipboard_set(str(val))
            else:
                r = {"ok": False, "speech": "acao desconhecida"}
            if not r.get("ok"):
                return {"ok": False,
                        "speech": "Macro parou no passo %d: %s"
                        % (done + 1, r.get("speech"))}
            done += 1
            time.sleep(self.STEP_GAP)
        return {"ok": True,
                "speech": "Macro completa: %d passo(s) executados." % done}

    def stats(self) -> dict:
        out = {"desktop_controller": dict(self.counts)}
        out.update(self.gate.stats())
        return out


# ---------------------------------------------------------------------------
# MacroStore — skills nomeadas (JSON, editavel a mao pelo dono)
# ---------------------------------------------------------------------------
class MacroStore:
    DEFAULTS: Dict[str, dict] = {
        "whatsapp_enviar_texto": {
            "desc": "Colocar texto no clipboard, focar WhatsApp e enviar",
            "steps": [
                {"clipboard": "__TEXTO__"},
                {"focus": "whatsapp"},
                {"wait": 0.5},
                {"press": "ctrl+v"},
                {"wait": 0.3},
                {"key": "enter"},
            ],
            "params": {"__TEXTO__": "texto a enviar"},
        },
        "salvar_arquivo": {
            "desc": "Salvar (Ctrl+S) na janela ativa",
            "steps": [{"press": "ctrl+s"}, {"wait": 0.5}],
        },
        "capturar_area_print": {
            "desc": "Print da tela inteira para o clipboard (tecla PrintScreen)",
            "steps": [{"key": "f13"}],  # placeholder: use 'print' se mapeado
        },
    }

    def __init__(self, path: Optional[Any] = None):
        self._path = (Path(path) if path is not None
                      else _PROJ_ROOT / "engine" / "data" / "desktop_macros.json")
        self._lock = threading.RLock()
        self._macros: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.is_file():
            try:
                data = json_loads_safe(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._macros = {k: v for k, v in data.items()
                                    if isinstance(v, dict)}
                    return
            except Exception:
                logger.exception("macros: arquivo ilegivel — defaults")
        self._macros = {k: dict(v) for k, v in self.DEFAULTS.items()}
        self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json_dumps_safe(self._macros), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            logger.exception("macros: falha ao gravar")

    def list_macros(self) -> List[dict]:
        with self._lock:
            return [{"name": k, "desc": v.get("desc", ""),
                     "steps": len(v.get("steps", []))}
                    for k, v in sorted(self._macros.items())]

    def get(self, name: str) -> Optional[dict]:
        with self._lock:
            return self._macros.get(_norm(name).replace(" ", "_"))

    def define(self, name: str, desc: str, steps: List[dict]) -> dict:
        err = validate_steps(steps)
        if err:
            return {"ok": False, "speech": "Macro invalida: %s" % err}
        key = _norm(name).replace(" ", "_")[:40]
        if not key:
            return {"ok": False, "speech": "Nome de macro invalido."}
        with self._lock:
            self._macros[key] = {"desc": desc[:120], "steps": steps}
            self._save()
        return {"ok": True, "speech": "Skill '%s' salva (%d passos)."
                % (key, len(steps))}

    def forget(self, name: str) -> dict:
        key = _norm(name).replace(" ", "_")
        with self._lock:
            if key not in self._macros:
                return {"ok": False, "speech": "Skill '%s' nao existe." % name}
            del self._macros[key]
            self._save()
        return {"ok": True, "speech": "Skill '%s' apagada." % key}

    def materialize(self, name: str, params: Optional[Dict[str, str]] = None
                    ) -> Optional[List[dict]]:
        m = self.get(name)
        if m is None:
            return None
        params = params or {}
        steps: List[dict] = []
        for s in m.get("steps", []):
            op, val = next(iter(s.items()))
            if isinstance(val, str):
                for k, v in params.items():
                    val = val.replace(k, str(v))
            steps.append({op: val})
        return steps


def json_loads_safe(text: str):
    import json
    return json.loads(text)


def json_dumps_safe(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# gramatica
# ---------------------------------------------------------------------------
def parse_desktop(utterance: str):
    raw = (utterance or "").strip()
    t = _norm(raw)
    if not t:
        return None
    m = re.search(r"\b(?:digita|digitar|escreve no teclado)\s+(.+)$", t)
    if m:
        raw_m = re.search(r"\b(?:digita|digitar|escreve no teclado)\s+(.+)$",
                          raw, re.IGNORECASE)
        return ("teclado_digitar", {"texto": (raw_m.group(1).strip()
                if raw_m else m.group(1).strip())})
    m = re.search(r"\b(?:aperta|apertar|pressiona|pressionar)\s+"
                  r"(?:a\s+tecla\s+|as\s+teclas\s+|o\s+)?([\w+]+)$", t)
    if m and "+" in m.group(1) or m and m.group(1) in _VK_MAP:
        return ("teclado_apertar", {"tecla": m.group(1)})
    m = re.search(r"\b(?:clica|clicar)\s+(?:em|no|na)?\s*(\d{1,4})\s*[, ]\s*(\d{1,4})", t)
    if m:
        return ("mouse_clicar", {"x": m.group(1), "y": m.group(2)})
    if re.search(r"\b(?:print|captura)\s+(?:da\s+)?tela\b|\bscreenshot\b", t):
        return ("tela_print", {})
    m = re.search(r"\b(?:roda|rodar|executa|executar|aplica|aplicar)\s+"
                  r"(?:a\s+)?(?:macro|skill)\s+(\w[\w ]*)$", t)
    if m:
        return ("macro_rodar", {"nome": m.group(1).strip()})
    m = re.search(r"\b(?:quais|liste?|lista)\s+(?:macros?|skills?)\b", t)
    if m:
        return ("macro_listar", {})
    m = re.search(r"\b(?:envia|enviar|manda|mendar)\s+(?:o\s+texto\s+)?"
                  r"['\"]?(.+?)['\"]?\s+(?:no|pelo|via)\s+whatsapp$", t)
    if m:
        raw_m = re.search(r"\b(?:envia|enviar|manda|mendar)\s+(?:o\s+texto\s+)?"
                          r"['\"]?(.+?)['\"]?\s+(?:no|pelo|via)\s+whatsapp$",
                          raw, re.IGNORECASE)
        return ("whatsapp_enviar", {"texto": (raw_m.group(1).strip()
                if raw_m else m.group(1).strip()), "janela": "whatsapp"})
    m = re.search(r"\bfoca\s+(?:na\s+janela\s+|no\s+)?(.+)$", t)
    if m:
        return ("janela_focar", {"janela": m.group(1).strip()})
    return None


# ---------------------------------------------------------------------------
# tools no CommandCenter
# ---------------------------------------------------------------------------
def build_desktop_tools(cc, dc: DesktopController, macros: MacroStore,
                        gate: DeskSession) -> None:
    import inspect
    _csf = "confirm_speech_fn" in inspect.signature(cc.register).parameters

    def gated(fn):
        def wrapped(args, session):
            try:
                gate.ensure()
            except DeskLockedError as exc:
                return {"ok": False, "speech": str(exc)}
            return fn(args, session)
        return wrapped

    def t_digitar_plan(args):
        texto = str(args.get("texto", ""))
        return ("Vou digitar '%s...' (%d caracteres) na janela ativa. "
                "Diga sim para executar." % (texto[:60], len(texto)))

    @gated
    def t_digitar(args, session):
        return dc.type_text(str(args.get("texto", "")))

    def t_apertar_plan(args):
        return ("Vou apertar %s. Diga sim para executar."
                % args.get("tecla", "?"))

    @gated
    def t_apertar(args, session):
        return dc.press(str(args.get("tecla", "")))

    def t_clicar_plan(args):
        return ("Vou clicar em %s, %s (mouse se move visivelmente). "
                "Diga sim para executar." % (args.get("x"), args.get("y")))

    @gated
    def t_clicar(args, session):
        return dc.click(_num(args.get("x")), _num(args.get("y")))

    def t_print(args, session):
        return dc.screenshot("tela")

    def t_macro_plan(args):
        nome = str(args.get("nome", ""))
        m = macros.get(nome)
        if m is None:
            return "Skill '%s' nao existe." % nome
        steps = macros.materialize(nome) or []
        return ("Vou rodar a skill %s (%d passos): %s. ESC aborta. "
                "Diga sim para executar."
                % (nome, len(steps), dc.describe_steps(steps)[:200]))

    @gated
    def t_macro(args, session):
        nome = str(args.get("nome", ""))
        steps = macros.materialize(nome)
        if steps is None:
            return {"ok": False, "speech": "Skill '%s' nao existe." % nome}
        return dc.run_steps(steps)

    def t_macro_listar(args, session):
        lst = macros.list_macros()
        if not lst:
            return {"ok": True, "speech": "Nenhuma skill definida."}
        return {"ok": True, "speech": "Skills: %s." % ", ".join(
            "%s (%s)" % (m["name"], m["desc"][:30]) for m in lst[:6])}

    def t_whatsapp_plan(args):
        texto = str(args.get("texto", ""))
        return ("Vou enviar no WhatsApp o texto: '%s...'. A janela do "
                "WhatsApp precisa estar ABERTA (vou focar nela e colar). "
                "Diga sim para enviar." % texto[:80])

    @gated
    def t_whatsapp(args, session):
        texto = str(args.get("texto", ""))
        janela = str(args.get("janela", "whatsapp"))
        steps = [
            {"clipboard": texto},
            {"focus": janela},
            {"wait": 0.6},
            {"press": "ctrl+v"},
            {"wait": 0.4},
            {"key": "enter"},
        ]
        return dc.run_steps(steps)

    def t_focar(args, session):
        return dc.focus_window(str(args.get("janela", "")))

    def t_definir(args, session):
        # definir skill via args: nome, passos em texto "press:ctrl+s|type:ola"
        nome = str(args.get("nome", ""))
        spec = str(args.get("passos", ""))
        steps = []
        for part in spec.split("|"):
            part = part.strip()
            if not part:
                continue
            if ":" not in part:
                return {"ok": False,
                        "speech": "Passo invalido: %s (use acao:valor)" % part}
            op, val = part.split(":", 1)
            op = op.strip().lower()
            if op in ("click", "move"):
                xy = [v.strip() for v in val.split(",")]
                if len(xy) != 2:
                    return {"ok": False, "speech": "click/move exige x,y"}
                steps.append({op: [_num(xy[0]), _num(xy[1])]})
            elif op == "wait":
                steps.append({"wait": float(val)})
            else:
                steps.append({op: val.strip()})
        return macros.define(nome, str(args.get("desc", "")), steps)

    def _reg(name, desc, handler, risk, args=None, confirm=False, csf=None):
        if _csf:
            cc.register(name, desc, handler, risk, args=args,
                        confirm=confirm, confirm_speech_fn=csf)
        else:
            cc.register(name, desc, handler, risk, args=args, confirm=confirm)

    _reg("teclado_digitar", "digitar texto na janela ativa", t_digitar,
         "control", args={"texto": "texto"}, confirm=True, csf=t_digitar_plan)
    _reg("teclado_apertar", "apertar combinação de teclas", t_apertar,
         "control", args={"tecla": "ctrl+s"}, confirm=True, csf=t_apertar_plan)
    _reg("mouse_clicar", "clicar em coordenadas", t_clicar, "control",
         args={"x": "px", "y": "px"}, confirm=True, csf=t_clicar_plan)
    _reg("tela_print", "capturar print da tela", t_print, "control",
         confirm=False)
    _reg("macro_rodar", "rodar skill/macro salva", t_macro, "control",
         args={"nome": "skill"}, confirm=True, csf=t_macro_plan)
    _reg("macro_listar", "listar skills definidas", t_macro_listar, "read")
    _reg("whatsapp_enviar", "enviar texto via WhatsApp Desktop", t_whatsapp,
         "control", args={"texto": "mensagem", "janela": "whatsapp"},
         confirm=True, csf=t_whatsapp_plan)
    _reg("janela_focar", "focar uma janela pelo título", t_focar, "control",
         args={"janela": "título"}, confirm=False)
    _reg("macro_definir", "definir nova skill (passos acao:valor | ...)",
         t_definir, "control",
         args={"nome": "nome", "desc": "descrição",
               "passos": "press:ctrl+s|wait:0.5"}, confirm=False)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


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

    # --- parse_hotkey ---
    check("hotkey: ctrl+shift+s", parse_hotkey("ctrl+shift+s") ==
          [0x11, 0x10, ord("S")])
    check("hotkey: enter", parse_hotkey("enter") == [0x0D])
    check("hotkey: f5", parse_hotkey("f5") == [0x74])
    check("hotkey: invalida", parse_hotkey("ctrl+batata") == [])

    # --- validate_steps ---
    check("steps: valido",
          validate_steps([{"press": "ctrl+s"}, {"wait": 0.5},
                          {"type": "oi"}]) is None)
    check("steps: hotkey ruim",
          validate_steps([{"press": "zzz"}]) is not None)
    check("steps: click sem par",
          validate_steps([{"click": [1]}]) is not None)
    check("steps: wait fora de faixa",
          validate_steps([{"wait": 99}]) is not None)
    check("steps: acao desconhecida",
          validate_steps([{"explodir": 1}]) is not None)

    # --- DeskSession ---
    gate = DeskSession(pin="1234")
    gate.set_origin("local")
    try:
        gate.ensure()
        ok_local = True
    except DeskLockedError:
        ok_local = False
    check("gate: local sempre pode", ok_local)
    gate.set_origin("remote")
    try:
        gate.ensure()
        ok_remote = False
    except DeskLockedError:
        ok_remote = True
    check("gate: remoto bloqueado por padrao", ok_remote)
    r = gate.authorize("9999")
    check("gate: PIN errado negado", r["ok"] is False)
    r = gate.authorize("1234")
    check("gate: PIN certo libera", r["ok"] is True)
    gate.ensure()
    check("gate: remoto passa apos liberar", True)
    g2 = DeskSession(pin="")
    check("gate: sem PIN -> remoto desativado",
          g2.authorize("qq")["ok"] is False
          and g2.remote_enabled is False)

    # --- MacroStore ---
    with tempfile.TemporaryDirectory(prefix="aura_dc_st_") as td:
        ms = MacroStore(path=Path(td) / "macros.json")
        check("macros: defaults carregados", len(ms.list_macros()) >= 2)
        r = ms.define("teste_skill", "apagar tudo", [{"press": "ctrl+z"}])
        check("macros: definir valida e salva", r["ok"] is True)
        steps = ms.materialize("teste_skill")
        check("macros: materializar", steps == [{"press": "ctrl+z"}])
        # params substituicao
        ms.define("whats_teste", "x", [{"clipboard": "__TXT__"},
                                       {"key": "enter"}])
        st = ms.materialize("whats_teste", {"__TXT__": "ola"})
        check("macros: params substituidos",
              st[0]["clipboard"] == "ola")
        r = ms.forget("teste_skill")
        check("macros: esquecer", r["ok"] is True
              and ms.get("teste_skill") is None)
        # persistencia
        ms2 = MacroStore(path=Path(td) / "macros.json")
        check("macros: persiste em disco", ms2.get("whats_teste") is not None)

    # --- controller com injecao (sem hardware) ---
    sent: List[Tuple[int, bool]] = []

    class FakeDC(DesktopController):
        pass

    dc = DesktopController(
        gate=DeskSession(pin="x"),
        send_input=lambda vk, up: sent.append((vk, up)),
        get_cursor_pos=lambda: (100, 100),
        set_cursor_pos=lambda x, y: sent.append(("move", (x, y))),
        get_async_key_state=lambda vk: 0,
        focus_fn=lambda n: True,
        clipboard_set=lambda t: sent.append(("clip", t)),
        clipboard_get=lambda: "lido",
        active_title_fn=lambda: "Bloco de notas",
        screenshot_fn=lambda p: Path("fake.bmp"))
    gate_local = DeskSession(pin="")
    gate_local.set_origin("local")
    dc2 = DesktopController(
        gate=gate_local,
        send_input=lambda vk, up: sent.append((vk, up)),
        get_cursor_pos=lambda: (0, 0),
        set_cursor_pos=lambda x, y: sent.append(("move", (x, y))),
        get_async_key_state=lambda vk: 0,
        focus_fn=lambda n: True,
        clipboard_set=lambda t: sent.append(("clip", t)),
        clipboard_get=lambda: "",
        active_title_fn=lambda: "",
        screenshot_fn=lambda p: Path("fake.bmp"))

    r = dc2.press("ctrl+s")
    check("press: sequencia down/up correta",
          r["ok"] is True and sent[0] == (0x11, False)
          and sent[1] == (ord("S"), False)
          and sent[2] == (ord("S"), True) and sent[3] == (0x11, True))
    r = dc2.clipboard_set("texto de teste")
    check("clipboard: set", r["ok"] is True
          and ("clip", "texto de teste") in sent)
    r = dc2.focus_window("whatsapp")
    check("focus: ok", r["ok"] is True)
    r = dc2.screenshot()
    check("screenshot: ok e path", r["ok"] is True
          and r["path"].endswith(".bmp"))
    r = dc2.run_steps([{"press": "ctrl+a"}, {"wait": 0.01},
                       {"type": "ola"}])
    check("run_steps: executa sequencia", r["ok"] is True
          and "3 passo" in r["speech"])

    # kill-switch: ESC pressionado aborta
    esc_state = {"on": False}
    dc3 = DesktopController(
        gate=gate_local,
        send_input=lambda vk, up: None,
        get_cursor_pos=lambda: (0, 0),
        set_cursor_pos=lambda x, y: None,
        get_async_key_state=lambda vk: 1 if esc_state["on"] else 0,
        focus_fn=lambda n: True, clipboard_set=lambda t: None,
        clipboard_get=lambda: "", active_title_fn=lambda: "",
        screenshot_fn=lambda p: Path("f.bmp"))
    esc_state["on"] = True
    r = dc3.run_steps([{"press": "ctrl+s"}, {"press": "ctrl+a"}])
    check("kill-switch: ESC aborta macro", r["ok"] is False
          and "ESC" in r["speech"])
    r = dc3.click(10, 10)
    check("kill-switch: ESC bloqueia clique", r["ok"] is False)

    # remoto bloqueado sem liberar
    gate_r = DeskSession(pin="1234")
    gate_r.set_origin("remote")
    dc4 = DesktopController(gate=gate_r, send_input=lambda vk, up: None,
                            get_cursor_pos=lambda: (0, 0),
                            set_cursor_pos=lambda x, y: None,
                            get_async_key_state=lambda vk: 0,
                            focus_fn=lambda n: True,
                            clipboard_set=lambda t: None,
                            clipboard_get=lambda: "",
                            active_title_fn=lambda: "",
                            screenshot_fn=lambda p: Path("f.bmp"))
    try:
        r = dc4.press("ctrl+s")
    except DeskLockedError as exc:
        r = {"ok": False, "speech": str(exc)}
    check("gate: remoto sem sessao -> press negado",
          r["ok"] is False and "bloqueado" in r["speech"])
    gate_r.authorize("1234")
    r = dc4.press("ctrl+s")
    check("gate: apos liberar, press funciona", r["ok"] is True)

    # --- gramatica ---
    g = parse_desktop("digita olá mundo como vai")
    check("gram: digitar", g == ("teclado_digitar",
                                 {"texto": "olá mundo como vai"}))
    g = parse_desktop("aperta ctrl+shift+s")
    check("gram: apertar", g == ("teclado_apertar", {"tecla": "ctrl+shift+s"}))
    g = parse_desktop("clica em 400 300")
    check("gram: clicar", g == ("mouse_clicar", {"x": "400", "y": "300"}))
    check("gram: print", parse_desktop("tira um print da tela") ==
          ("tela_print", {}))
    g = parse_desktop("roda a macro whatsapp_enviar_texto")
    check("gram: rodar macro", g == ("macro_rodar",
                                     {"nome": "whatsapp_enviar_texto"}))
    check("gram: listar skills", parse_desktop("quais skills você tem?")
          == ("macro_listar", {}))
    g = parse_desktop("envia 'reunião às 15h' no whatsapp")
    check("gram: whatsapp", g is not None
          and g[0] == "whatsapp_enviar" and "reunião" in g[1]["texto"])
    check("gram: conversa comum", parse_desktop("bom dia") is None)

    # --- hardware real (so Windows) ---
    if _IS_WINDOWS:
        print("[INFO] Windows detectado — testes de hardware reais:")
        real = DesktopController(gate=gate_local)
        r = real.clipboard_set("aura_teste_123")
        check("hw: clipboard set", r["ok"] is True)
        check("hw: clipboard round-trip",
              "aura_teste_123" in real.clipboard_get())
        x0, y0 = real.cursor_position()
        r = real.move_to(x0 + 5, y0 + 5, smooth=False)
        x1, y1 = real.cursor_position()
        r2 = real.move_to(x0, y0, smooth=False)
        check("hw: mouse move e volta", r["ok"] is True
              and abs(x1 - (x0 + 5)) <= 2)
        check("hw: janela ativa tem título", len(real.active_window_title()) >= 0)
        with tempfile.TemporaryDirectory() as td:
            r = real.screenshot()
            p = Path(r.get("path", ""))
            check("hw: screenshot BMP salvo", p.is_file()
                  and p.read_bytes()[:2] == b"BM"
                  and p.stat().st_size > 10000)
    else:
        print("[SKIP] nao-Windows: hardware (mouse/teclado/clip/GDI) nao testado")

    # --- integracao CommandCenter ---
    try:
        from jarvis_command_center import CommandCenter
    except Exception:
        CommandCenter = None  # type: ignore
    if CommandCenter is None:
        print("[SKIP] jarvis_command_center nao importavel aqui")
    else:
        cc = CommandCenter()
        gate_cc = DeskSession(pin="")
        gate_cc.set_origin("local")
        build_desktop_tools(cc, dc2, MacroStore(
            path=Path(tempfile.mkdtemp()) / "m.json"), gate_cc)
        r = cc.execute("teclado_apertar", {"tecla": "ctrl+s"}, "u1")
        check("cc: apertar pede confirmacao com plano",
              r.get("awaiting_confirmation") is True
              and "ctrl+s" in r["speech"])
        r2 = cc.handle_utterance("sim", "u1")
        check("cc: sim executa o apertar", r2 is not None
              and r2.get("ok") is True)
        r = cc.execute("whatsapp_enviar", {"texto": "teste"}, "u2")
        check("cc: whatsapp pede confirmacao com texto exato",
              r.get("awaiting_confirmation") is True
              and "teste" in r["speech"])
        # remoto travado via tool
        gate_rem = DeskSession(pin="9999")
        gate_rem.set_origin("remote")
        cc2 = CommandCenter()
        build_desktop_tools(cc2, dc4, MacroStore(
            path=Path(tempfile.mkdtemp()) / "m.json"), gate_rem)
        r = cc2.execute("teclado_apertar", {"tecla": "ctrl+a"}, "u3")
        r2 = cc2.handle_utterance("sim", "u3")
        check("cc: remoto sem liberar -> tool nega",
              r2 is not None and r2.get("ok") is False
              and "bloqueado" in r2.get("speech", ""))

    if fails:
        print("SELF-TEST FALHOU: %d verificacao(oes): %s"
              % (len(fails), ", ".join(fails)))
        return 1
    print("ALL TESTS PASSED - desktop_controller.py")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
