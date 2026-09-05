from __future__ import annotations
import asyncio
import json
import os
import sqlite3
import subprocess
import time
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Set

import psutil
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, KeyboardButton, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
)
from aiogram.filters import Command

from tg_dependencies import (
    TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, DB_PATH,
    ZMQ_ALERT_PORT, ZMQ_UI_SYNC_PORT,
    make_session_token, verify_session_token, mock_glm,
)
from tg_voice_pipeline import process_voice_message

try:
    import zmq
    ZMQ_OK = True
except ImportError:
    ZMQ_OK = False

# session auth: chat_id -> token
_SESSIONS: Dict[int, str] = {}
_AUTHORIZED: Set[int] = set()

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Monitoramento"), KeyboardButton(text="⚡ Trades")],
            [KeyboardButton(text="🧠 Conversar com AURA"), KeyboardButton(text="⚙️ Controle Admin")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

def monitor_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="CPU/GPU", callback_data="mon:cpu"),
         InlineKeyboardButton(text="Agentes", callback_data="mon:agents")],
        [InlineKeyboardButton(text="Banco de Dados", callback_data="mon:db")],
    ])

def trades_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Abertos", callback_data="tr:open"),
         InlineKeyboardButton(text="Últimos outcomes", callback_data="tr:out")],
    ])

def admin_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Pendente Diretor", callback_data="ad:pending")],
        [InlineKeyboardButton(text="Aprovar (ACK)", callback_data="ad:approve")],
        [InlineKeyboardButton(text="Auth HMAC", callback_data="ad:auth")],
    ])

def metrics_text() -> str:
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory()
    vram = "N/A"
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        mi = pynvml.nvmlDeviceGetMemoryInfo(h)
        vram = f"{mi.used/1024**3:.1f}/{mi.total/1024**3:.1f}GB"
    except Exception:
        pass
    acc = "n/a"
    opens = 0
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2)
        cur = conn.cursor()
        try:
            cur.execute("SELECT result FROM outcomes ORDER BY id DESC LIMIT 50")
            rows = cur.fetchall()
            if rows:
                acc = f"{sum(1 for r in rows if r[0]=='Acertou')/len(rows):.2f}"
        except Exception:
            pass
        try:
            cur.execute("SELECT COUNT(*) FROM paper_trades WHERE COALESCE(labeled,0)=0")
            opens = int(cur.fetchone()[0])
        except Exception:
            pass
        conn.close()
    except Exception:
        pass
    return f"CPU: {cpu:.0f}% | RAM: {ram.percent:.0f}% | VRAM: {vram} | Acurácia: {acc} | Trades Abertos: {opens}"

def is_admin(chat_id: int) -> bool:
    if ADMIN_CHAT_ID and chat_id == ADMIN_CHAT_ID:
        return chat_id in _AUTHORIZED
    return chat_id in _AUTHORIZED

def require_auth(chat_id: int) -> bool:
    tok = _SESSIONS.get(chat_id)
    return bool(tok and verify_session_token(chat_id, tok))

def publish_ui_sync(payload: Dict[str, Any]) -> None:
    if not ZMQ_OK:
        return
    try:
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.PUB)
        sock.setsockopt(zmq.LINGER, 0)
        try:
            sock.bind(f"tcp://127.0.0.1:{ZMQ_UI_SYNC_PORT}")
        except zmq.ZMQError:
            sock.connect(f"tcp://127.0.0.1:{ZMQ_UI_SYNC_PORT}")
        time.sleep(0.05)
        sock.send_string("ui_sync::" + json.dumps(payload, ensure_ascii=False))
        sock.close(0)
    except Exception:
        pass

async def execute_director_with_ack_fill(message: Message) -> None:
    chat_id = message.chat.id
    if not require_auth(chat_id):
        await message.answer("🔒 Auth HMAC necessária. Use ⚙️ Controle Admin → Auth HMAC e envie AUTH &lt;token&gt;.")
        return
    await message.answer("✅ <b>ACK</b> — aprovação recebida. Executando em background…", parse_mode="HTML")
    path = Path("director_pending_actions.json")
    if not path.exists():
        await message.answer("❌ <b>REJECTED</b> — nenhuma ação pendente.", parse_mode="HTML")
        return

    def _run() -> Dict[str, Any]:
        try:
            action = json.loads(path.read_text(encoding="utf-8"))
            if action.get("tipo_acao") == "python_patch":
                return {"status": "PAPER_BLOCKED", "error": "python_patch_disabled"}
            # generic approve via director module
            try:
                from aura_director_agent import AuraDirectorAgentV2
                res = AuraDirectorAgentV2.execute_approved_action()
                return {"status": "FILL", "result": res}
            except Exception as e:
                return {"status": "REJECTED", "error": str(e)}
        except Exception as e:
            return {"status": "REJECTED", "error": str(e)}

    result = await asyncio.to_thread(_run)
    if result.get("status") == "FILL":
        await message.answer(f"✅ <b>FILL</b> — executado com sucesso.\n<code>{json.dumps(result)[:500]}</code>", parse_mode="HTML")
        publish_ui_sync({"source": "telegram", "event": "director_approved", "result": result})
    else:
        await message.answer(f"❌ <b>REJECTED</b> — {result.get('error')}", parse_mode="HTML")
        publish_ui_sync({"source": "telegram", "event": "director_rejected", "result": result})

def build_bot() -> tuple:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        await message.answer(
            "AURA QUANT-X Command Center online.\nEscolha um módulo:",
            reply_markup=main_keyboard(),
        )

    @dp.message(F.text == "📊 Monitoramento")
    async def menu_mon(message: Message):
        await message.answer("Monitoramento:", reply_markup=monitor_inline())

    @dp.message(F.text == "⚡ Trades")
    async def menu_tr(message: Message):
        await message.answer("Trades:", reply_markup=trades_inline())

    @dp.message(F.text == "🧠 Conversar com AURA")
    async def menu_chat(message: Message):
        await message.answer("Envie texto ou áudio. Sessão RAG isolada: source=telegram")

    @dp.message(F.text == "⚙️ Controle Admin")
    async def menu_admin(message: Message):
        await message.answer("Admin:", reply_markup=admin_inline())

    @dp.callback_query(F.data.startswith("mon:"))
    async def cb_mon(cq: CallbackQuery):
        kind = cq.data.split(":")[1]
        if kind == "cpu":
            await cq.message.answer(metrics_text())
        elif kind == "agents":
            await cq.message.answer("Agentes: Director V2 | Janitor | Surrogate | Bet365 Stabilizer | Cognitive V3")
        else:
            await cq.message.answer(f"DB: {DB_PATH} | existe={Path(DB_PATH).exists()}")
        await cq.answer()

    @dp.callback_query(F.data.startswith("tr:"))
    async def cb_tr(cq: CallbackQuery):
        await cq.message.answer(metrics_text())
        await cq.answer()

    @dp.callback_query(F.data.startswith("ad:"))
    async def cb_ad(cq: CallbackQuery):
        kind = cq.data.split(":")[1]
        if kind == "pending":
            p = Path("director_pending_actions.json")
            if p.exists():
                await cq.message.answer(f"<pre>{p.read_text(encoding='utf-8')[:1500]}</pre>", parse_mode="HTML")
            else:
                await cq.message.answer("Nenhuma ação pendente.")
        elif kind == "approve":
            await execute_director_with_ack_fill(cq.message)
        elif kind == "auth":
            tok = make_session_token(cq.message.chat.id)
            _SESSIONS[cq.message.chat.id] = tok
            _AUTHORIZED.add(cq.message.chat.id)
            await cq.message.answer(
                f"HMAC session criada (TTL 1h).\nEnvie: <code>AUTH {tok}</code>\nou use o token já aplicado nesta sessão.",
                parse_mode="HTML",
            )
        await cq.answer()

    @dp.message(F.text.regexp(r"(?i)^AUTH\s+\S+"))
    async def auth_msg(message: Message):
        parts = message.text.split()
        if len(parts) >= 2 and verify_session_token(message.chat.id, parts[1]):
            _SESSIONS[message.chat.id] = parts[1]
            _AUTHORIZED.add(message.chat.id)
            await message.answer("🔓 Sessão autenticada (HMAC OK).")
        else:
            await message.answer("HMAC inválido ou expirado.")

    @dp.message(F.text.regexp(r"(?i)^APROVAR$"))
    async def approve_text(message: Message):
        await execute_director_with_ack_fill(message)

    @dp.message(F.voice)
    async def on_voice(message: Message):
        await message.answer("🎙️ Processando voz (fila Semaphore=1)…")
        try:
            file = await message.bot.get_file(message.voice.file_id)
            dest = Path(tempfile_dir()) / f"in_{message.voice.file_id}.ogg"
            await message.bot.download_file(file.file_path, destination=dest)
            out = await process_voice_message(str(dest), session_id="telegram")
            data = Path(out).read_bytes()
            await message.answer_voice(BufferedInputFile(data, filename="aura_reply.ogg"))
        except TimeoutError:
            await message.answer("⏳ Pipeline de áudio ocupado (timeout 10s). Tente novamente.")
        except Exception as e:
            await message.answer(f"Falha voz: {e}")

    @dp.message(F.text)
    async def on_text(message: Message):
        if message.text in ("📊 Monitoramento", "⚡ Trades", "🧠 Conversar com AURA", "⚙️ Controle Admin"):
            return
        if message.text.upper().startswith("AUTH"):
            return
        reply = mock_glm(message.text or "", session_id="telegram")
        await message.answer(reply)
        publish_ui_sync({"source": "telegram", "event": "chat", "text": message.text[:200]})

    return bot, dp

def tempfile_dir() -> str:
    return tempfile.mkdtemp(prefix="tg_voice_")

async def background_pusher(bot: Bot) -> None:
    if not ZMQ_OK:
        return
    await asyncio.sleep(1)
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.connect(f"tcp://127.0.0.1:{ZMQ_ALERT_PORT}")
    loop = asyncio.get_event_loop()
    while True:
        try:
            if sub.poll(500):
                msg = sub.recv_string(flags=zmq.NOBLOCK)
                payload = msg.split("::", 1)[-1] if "::" in msg else msg
                chat = ADMIN_CHAT_ID or next(iter(_AUTHORIZED), None)
                if chat:
                    await bot.send_message(
                        chat,
                        f"🚨 <b>ASIAN STABLE ALERT</b>\n<code>{payload[:1500]}</code>",
                        parse_mode="HTML",
                    )
        except Exception:
            await asyncio.sleep(1)
        await asyncio.sleep(0.05)

def _telegram_optin_ok() -> bool:
    kill = (os.environ.get("AURA_TELEGRAM_KILL_SWITCH") or "").strip().lower()
    if kill in {"1", "true", "on", "yes"}:
        return False
    kill_file = Path(__file__).resolve().parents[2] / "data" / "telegram_intel" / "kill_switch.on"
    if kill_file.is_file():
        return False
    token = (TELEGRAM_BOT_TOKEN or "").strip()
    if not token or token == "SEU_TOKEN_AQUI":
        return False
    optin = (os.environ.get("AURA_TELEGRAM_OPTIN") or "").strip().lower()
    return optin in {"1", "true", "yes", "on"}


async def main() -> None:
    if not _telegram_optin_ok():
        print("[TG] Recusado: precisa AURA_TELEGRAM_OPTIN=1 + AURA_TG_TOKEN real e kill switch off.")
        return
    bot, dp = build_bot()
    await asyncio.gather(
        dp.start_polling(bot),
        background_pusher(bot),
    )

if __name__ == "__main__":
    import tempfile  # ensure available for voice path helper
    # fix tempfile_dir
    globals()["tempfile"] = tempfile
    asyncio.run(main())
