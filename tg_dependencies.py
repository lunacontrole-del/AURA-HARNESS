from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

TELEGRAM_BOT_TOKEN = os.environ.get("AURA_TG_TOKEN", "SEU_TOKEN_AQUI")
ADMIN_CHAT_ID = int(os.environ.get("AURA_TG_ADMIN_CHAT_ID", "0") or 0)
_hmac_raw = (os.environ.get("AURA_TG_HMAC_SECRET") or "").strip()
HMAC_CONFIGURED = bool(_hmac_raw)
HMAC_SECRET = _hmac_raw.encode() if HMAC_CONFIGURED else secrets.token_bytes(32)
SESSION_TTL_SEC = 3600
DB_PATH = os.environ.get("AURA_DB", "aura_quant_x.db")
ZMQ_ALERT_PORT = 5558
ZMQ_UI_SYNC_PORT = 5559

def mock_stt(wav_path: str) -> str:
    return "status do sistema e risco atual"

def mock_glm(text: str, session_id: str = "telegram") -> str:
    return f"[AURA/{session_id}] Recebido: {text[:180]}. Canal cognitivo operacional."

def mock_tts(text: str, out_ogg: str) -> str:
    with open(out_ogg, "wb") as f:
        f.write(b"OggS\x00\x00fake-opus-payload-" + text[:40].encode("utf-8", errors="ignore"))
    return out_ogg

def make_session_token(chat_id: int, ts: Optional[int] = None) -> str:
    ts = ts or int(time.time())
    msg = f"{chat_id}:{ts}".encode()
    sig = hmac.new(HMAC_SECRET, msg, hashlib.sha256).hexdigest()[:16]
    return f"{ts}.{sig}"

def verify_session_token(chat_id: int, token: str) -> bool:
    if not HMAC_CONFIGURED:
        return False
    try:
        ts_s, sig = token.split(".", 1)
        ts = int(ts_s)
        if abs(time.time() - ts) > SESSION_TTL_SEC:
            return False
        expect = hmac.new(HMAC_SECRET, f"{chat_id}:{ts}".encode(), hashlib.sha256).hexdigest()[:16]
        return hmac.compare_digest(expect, sig)
    except Exception:
        return False
