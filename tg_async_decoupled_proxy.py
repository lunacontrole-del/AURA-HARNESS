from __future__ import annotations
import asyncio
import base64
import logging
import os
import uuid
from typing import Any, Dict, Optional

from aiohttp import ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile

logger = logging.getLogger("tg_async_proxy")
AURA_VOICE = os.environ.get("AURA_VOICE_SERVER", "http://127.0.0.1:8099")
TOKEN = os.environ.get("AURA_TG_TOKEN", "SEU_TOKEN_AQUI")
ADMIN = int(os.environ.get("AURA_TG_ADMIN_CHAT_ID", "0") or 0)

# In-memory task store (8099 would own this in production)
_TASKS: Dict[str, Dict[str, Any]] = {}

async def talk_async(session: ClientSession, audio_b64: str, text: Optional[str], session_id: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"session_id": session_id}
    if audio_b64:
        payload["audio_base64"] = audio_b64
    if text:
        payload["text"] = text
    task_id = str(uuid.uuid4())
    # Immediate response contract
    try:
        async with session.post(f"{AURA_VOICE}/api/voice/talk", json=payload) as resp:
            data = await resp.json() if resp.status == 200 else {}
    except Exception as e:
        data = {"reply_text": f"AURA offline: {e}"}
    text_out = data.get("reply_text") or data.get("reply") or data.get("text") or "Processando..."
    _TASKS[task_id] = {"text": text_out, "tts_base64": data.get("tts_base64") or data.get("audio_base64"), "raw": data}
    return {"task_id": task_id, "text": text_out}

async def fetch_audio_later(session: ClientSession, task_id: str) -> Optional[bytes]:
    meta = _TASKS.get(task_id) or {}
    b64 = meta.get("tts_base64")
    if b64:
        return base64.b64decode(b64)
    # optional second poll
    try:
        async with session.get(f"{AURA_VOICE}/api/voice/task/{task_id}") as resp:
            if resp.status == 200:
                data = await resp.json()
                b64 = data.get("tts_base64") or data.get("audio_base64")
                if b64:
                    return base64.b64decode(b64)
    except Exception:
        pass
    return None

def build_dp(session_holder: Dict[str, ClientSession]) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def start(m: Message) -> None:
        await m.answer("AURA Async Proxy: texto imediato + áudio em background.")

    @dp.message(F.voice)
    async def on_voice(m: Message) -> None:
        if ADMIN and m.from_user and m.from_user.id != ADMIN:
            return
        session = session_holder["s"]
        file = await m.bot.get_file(m.voice.file_id)
        buf = await m.bot.download_file(file.file_path)
        raw = buf.read() if hasattr(buf, "read") else bytes(buf)
        b64 = base64.b64encode(raw).decode()
        sid = f"tg_{m.from_user.id if m.from_user else 'anon'}"
        result = await talk_async(session, b64, None, sid)
        await m.answer(result["text"])  # instant text

        async def _send_voice() -> None:
            audio = await fetch_audio_later(session, result["task_id"])
            if audio:
                try:
                    await m.answer_voice(BufferedInputFile(audio, filename="aura.ogg"))
                except Exception as e:
                    logger.error("voice send: %s", e)
        asyncio.create_task(_send_voice())

    @dp.message(F.text)
    async def on_text(m: Message) -> None:
        if not m.text or m.text.startswith("/"):
            return
        if ADMIN and m.from_user and m.from_user.id != ADMIN:
            return
        session = session_holder["s"]
        sid = f"tg_{m.from_user.id if m.from_user else 'anon'}"
        result = await talk_async(session, "", m.text, sid)
        await m.answer(result["text"])
        async def _v() -> None:
            audio = await fetch_audio_later(session, result["task_id"])
            if audio:
                await m.answer_voice(BufferedInputFile(audio, filename="aura.ogg"))
        asyncio.create_task(_v())

    return dp

async def main() -> None:
    bot = Bot(token=TOKEN)
    holder: Dict[str, ClientSession] = {}
    holder["s"] = ClientSession(timeout=ClientTimeout(total=60))
    dp = build_dp(holder)
    try:
        await dp.start_polling(bot)
    finally:
        await holder["s"].close()

if __name__ == "__main__":
    from aiogram import Bot
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
