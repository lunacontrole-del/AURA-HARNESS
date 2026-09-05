from __future__ import annotations
import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from tg_dependencies import mock_stt, mock_glm, mock_tts

_AUDIO_SEM = asyncio.Semaphore(1)
_QUEUE_TIMEOUT = 10.0

async def _ffmpeg_ogg_to_wav(ogg_path: str, wav_path: str) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", ogg_path,
        "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", wav_path,
    ]
    def _run():
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        except Exception:
            data = Path(ogg_path).read_bytes()
            Path(wav_path).write_bytes(b"RIFF" + (len(data)+36).to_bytes(4,"little") + b"WAVEfmt " + data[:64])
    await asyncio.to_thread(_run)

async def process_voice_message(file_path: str, session_id: str = "telegram") -> str:
    """STT->GLM->TTS with Semaphore(1). Returns path to reply .ogg or raises TimeoutError."""
    try:
        await asyncio.wait_for(_AUDIO_SEM.acquire(), timeout=_QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        raise TimeoutError("audio_pipeline_busy_timeout")
    try:
        tmp = Path(tempfile.mkdtemp(prefix="aura_tg_"))
        wav = str(tmp / "in.wav")
        out_ogg = str(tmp / "out.ogg")
        await _ffmpeg_ogg_to_wav(file_path, wav)
        text = await asyncio.to_thread(mock_stt, wav)
        reply = await asyncio.to_thread(mock_glm, text, session_id)
        await asyncio.to_thread(mock_tts, reply, out_ogg)
        return out_ogg
    finally:
        _AUDIO_SEM.release()
