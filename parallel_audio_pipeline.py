# -*- coding: utf-8 -*-
"""
PILAR 4 - Pipeline de Áudio Paralelizado
AURA QUANT-X v12.7.0-RECONSOLIDADO

STT e prewarm em paralelo, streaming de resposta, TTS concorrente e contrato
compatível com o servidor de voz e com os testes dos anexos.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("aura.pilar4.audio_pipeline")
FIRST_SEGMENT_TIMEOUT_MS = 320
VOICE_MAX_TOKENS = 72
CHUNK_DISCARD_THRESHOLD = 10
PUNCTUATION = set(".!?\n")


class PipelineState(Enum):
    IDLE = "idle"
    STT_RUNNING = "stt_running"
    LLM_STREAMING = "llm_streaming"
    TTS_PLAYING = "tts_playing"
    DONE = "done"
    ERROR = "error"


@dataclass
class AudioChunk:
    audio_base64: Any
    session_id: str = "default"
    is_final: bool = False
    fixture_id: Optional[str] = None
    mood: str = "medium"
    market_stats: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AudioSegment:
    session_id: str
    text: str
    audio: Any = None
    is_first: bool = False
    is_final: bool = False
    created_at: float = field(default_factory=time.time)


@dataclass
class AudioSession:
    session_id: str
    created_at: float = field(default_factory=time.time)
    first_segment_time: Optional[float] = None
    state: PipelineState = PipelineState.IDLE
    match_context: Dict[str, Any] = field(default_factory=dict)
    tokens_generated: int = 0
    fixture_id: Optional[str] = None
    mood: str = "medium"


class ParallelAudioPipeline:
    def __init__(
        self,
        stt_fn: Optional[Callable] = None,
        llm_stream_fn: Optional[Callable] = None,
        tts_fn: Optional[Callable] = None,
        prewarm_fn: Optional[Callable] = None,
        on_segment_ready: Optional[Callable[[AudioSegment], None]] = None,
    ):
        self._input_queue: queue.Queue = queue.Queue(maxsize=50)
        self._tts_queue: queue.Queue = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._sessions: Dict[str, AudioSession] = {}
        self._lock = threading.RLock()
        self._sessions_lock = self._lock
        self._started = False
        self._on_segment_ready = on_segment_ready
        self.stt_fn = stt_fn or self._dummy_stt
        self.llm_stream_fn = llm_stream_fn or self._dummy_llm_stream
        self.tts_fn = tts_fn or self._dummy_tts
        self.prewarm_fn = prewarm_fn or self._dummy_prewarm
        self._stats = {
            "first_segment_ms_avg": 0.0,
            "first_segment_ms_count": 0,
            "sessions": 0,
            "total_sessions": 0,
            "total_chunks_processed": 0,
            "total_segments_generated": 0,
        }
        # V23 BLOCO 6: barge-in
        self._is_speaking = False
        self._stop_speaking_flag = threading.Event()
        self._worker = threading.Thread(target=self._main_loop, name="AuraAudioPipeline", daemon=True)
        self._tts_worker = threading.Thread(target=self._tts_loop, name="AuraTTSWorker", daemon=True)
        self.start()

    def start(self) -> bool:
        if self._started and self._worker.is_alive() and self._tts_worker.is_alive():
            return True
        if self._stop.is_set():
            self._stop.clear()
        if not self._worker.is_alive():
            self._worker = threading.Thread(target=self._main_loop, name="AuraAudioPipeline", daemon=True)
            self._worker.start()
        if not self._tts_worker.is_alive():
            self._tts_worker = threading.Thread(target=self._tts_loop, name="AuraTTSWorker", daemon=True)
            self._tts_worker.start()
        self._started = True
        logger.info("Pipeline de áudio paralelizado iniciado | target_first_segment=%dms", FIRST_SEGMENT_TIMEOUT_MS)
        return True

    def _dummy_stt(self, audio_chunk: Any, *_: Any) -> str:
        time.sleep(0.05)
        return "analisar corner"

    def _dummy_prewarm(self, match_context: Dict[str, Any]) -> None:
        time.sleep(0.02)

    def _dummy_llm_stream(self, prompt: str, *args: Any, max_tokens: int = VOICE_MAX_TOKENS, **kwargs: Any):
        text = "Sinal de entrada detectado com edge positivo. Recomendo observação."
        for ch in text:
            yield ch
            time.sleep(0.008)

    def _dummy_tts(self, text: str, *_: Any) -> None:
        time.sleep(0.03)

    def _invoke(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Invoca mocks/implementações com assinaturas antiga e nova."""
        try:
            return fn(*args, **kwargs)
        except TypeError:
            if len(args) >= 3:
                try:
                    return fn(args[0], args[1])
                except TypeError:
                    return fn(args[0])
            if len(args) >= 2:
                try:
                    return fn(args[0], args[1])
                except TypeError:
                    return fn(args[0])
            return fn(*args)

    def submit_audio(self, *args: Any, session_id: Optional[str] = None, audio_chunk: Any = None, audio_base64: Any = None, match_context: Optional[Dict[str, Any]] = None, is_final: bool = False, fixture_id: Optional[str] = None, mood: str = "medium", market_stats: Optional[Dict[str, Any]] = None) -> bool:
        # API moderna: submit_audio(session_id, audio_bytes, context)
        # API do anexo: submit_audio(audio_base64, session_id, is_final=...)
        if args:
            if len(args) >= 2 and isinstance(args[1], (bytes, bytearray)):
                session_id = session_id or str(args[0])
                audio_chunk = args[1] if audio_chunk is None else audio_chunk
                if len(args) >= 3 and match_context is None and isinstance(args[2], dict):
                    match_context = args[2]
            elif len(args) >= 2:
                audio_base64 = args[0] if audio_base64 is None else audio_base64
                session_id = session_id or str(args[1])
            elif len(args) == 1:
                audio_base64 = args[0] if audio_base64 is None else audio_base64
        sid = str(session_id or "default")
        audio = audio_chunk if audio_chunk is not None else audio_base64
        if audio is None:
            audio = b""
        context = dict(match_context or {})
        context.update({"fixture_id": fixture_id, "mood": mood, **(market_stats or {})})
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                session = AudioSession(session_id=sid)
                self._sessions[sid] = session
                self._stats["sessions"] += 1
                self._stats["total_sessions"] = self._stats["sessions"]
            session.match_context.update(context)
            session.fixture_id = fixture_id or session.fixture_id
            session.mood = mood
        try:
            self._input_queue.put_nowait({"session_id": sid, "audio": audio, "context": context, "is_final": bool(is_final)})
            return True
        except queue.Full:
            while self._input_queue.qsize() > CHUNK_DISCARD_THRESHOLD:
                try:
                    self._input_queue.get_nowait()
                except queue.Empty:
                    break
            logger.warning("Fila de áudio cheia — chunks antigos descartados")
            return False

    def _call_stt(self, audio: Any, session_id: str) -> str:
        return str(self._invoke(self.stt_fn, audio, session_id) or "")

    def _call_llm(self, text: str, session_id: str, context: Dict[str, Any]):
        try:
            return self.llm_stream_fn(text, session_id, context)
        except TypeError:
            return self.llm_stream_fn(text, max_tokens=VOICE_MAX_TOKENS)

    def _call_tts(self, text: str, mood: str = "medium") -> Any:
        return self._invoke(self.tts_fn, text, mood)

    def _emit_segment(self, session: AudioSession, text: str, is_first: bool, is_final: bool = False) -> None:
        segment = AudioSegment(session_id=session.session_id, text=text, is_first=is_first, is_final=is_final)
        with self._lock:
            self._stats["total_segments_generated"] += 1
        if self._on_segment_ready is not None:
            try:
                self._on_segment_ready(segment)
            except Exception as exc:
                logger.warning("Callback de segmento falhou: %s", exc)
        try:
            self._tts_queue.put_nowait({"session_id": session.session_id, "text": text, "mood": session.mood, "is_first": is_first})
        except queue.Full:
            logger.warning("Fila TTS cheia; segmento descartado")

    def _main_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._input_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            sid = item["session_id"]
            audio = item["audio"]
            with self._lock:
                session = self._sessions.get(sid)
                if session is None:
                    continue
                session.state = PipelineState.STT_RUNNING
                ctx = dict(session.match_context)
            # V23: concurrent STT + prewarm without blocking join on the hot path
            stt_result: List[Optional[str]] = [None]
            def run_stt() -> None:
                try:
                    stt_result[0] = self._call_stt(audio, sid)
                except Exception as exc:
                    logger.warning("STT do Pilar 4 falhou: %s", exc)
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="AuraSTTPre") as pool:
                fut_stt = pool.submit(run_stt)
                if self.prewarm_fn:
                    pool.submit(self.prewarm_fn, ctx)
                try:
                    fut_stt.result(timeout=2.0)
                except Exception as exc:
                    logger.warning("STT timeout/fail: %s", exc)
            text = stt_result[0] or ""
            if not text.strip():
                continue
            with self._lock:
                session.state = PipelineState.LLM_STREAMING
            try:
                raw = self._call_llm(text, sid, ctx)
                chars: List[str] = []
                for idx, token in enumerate(raw):
                    if idx >= VOICE_MAX_TOKENS * 4:
                        break
                    chars.append(str(token))
                reply = "".join(chars).strip()
                if not reply:
                    reply = text.strip()
                pieces = [p.strip() for p in re.split(r"(?<=[.!?])\s+", reply) if p.strip()] or [reply]
                for idx, piece in enumerate(pieces[:8]):
                    self._emit_segment(session, piece, is_first=(idx == 0), is_final=(idx == len(pieces[:8]) - 1))
                with self._lock:
                    session.state = PipelineState.DONE
                    session.tokens_generated = len(chars)
                    self._stats["total_chunks_processed"] += 1
            except Exception as exc:
                with self._lock:
                    session.state = PipelineState.ERROR
                logger.exception("Pipeline de áudio falhou: %s", exc)

    def _tts_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._tts_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._call_tts(item["text"], item.get("mood", "medium"))
            except Exception as exc:
                logger.error("Erro TTS: %s", exc)

    def process_text(self, session_id: str, text: str, match_context: Optional[Dict[str, Any]] = None, llm_stream_fn: Optional[Callable] = None, tts_fn: Optional[Callable] = None) -> Dict[str, Any]:
        started = time.time()
        sid = str(session_id or "default")
        with self._lock:
            session = self._sessions.setdefault(sid, AudioSession(session_id=sid))
            session.match_context = dict(match_context or {})
            session.state = PipelineState.LLM_STREAMING
            self._stats["sessions"] = max(self._stats["sessions"], len(self._sessions))
            self._stats["total_sessions"] = self._stats["sessions"]
        threading.Thread(target=self.prewarm_fn, args=(dict(match_context or {}),), daemon=True).start()
        stream = llm_stream_fn or self.llm_stream_fn
        try:
            raw = stream(str(text), max_tokens=VOICE_MAX_TOKENS)
        except TypeError:
            raw = stream(str(text), sid, dict(match_context or {}))
        chars: List[str] = []
        for idx, char in enumerate(raw):
            if idx >= VOICE_MAX_TOKENS * 4:
                break
            chars.append(str(char))
        reply = "".join(chars).strip() or str(text).strip()
        pieces = [part.strip() for part in re.split(r"(?<=[.!?])\s+", reply) if part.strip()] or [reply]
        first_ms = int((time.time() - started) * 1000)
        with self._lock:
            session.first_segment_time = time.time()
            self._stats["first_segment_ms_count"] += 1
            n = self._stats["first_segment_ms_count"]
            self._stats["first_segment_ms_avg"] = ((self._stats["first_segment_ms_avg"] * (n - 1)) + first_ms) / n
        tts = tts_fn or self.tts_fn
        outputs: List[Any] = [None] * min(len(pieces), 8)
        with ThreadPoolExecutor(max_workers=min(4, len(outputs) or 1), thread_name_prefix="AuraPillar4TTS") as executor:
            futures = {executor.submit(self._invoke, tts, piece, session.mood): idx for idx, piece in enumerate(pieces[:8])}
            for future, idx in futures.items():
                try:
                    value = future.result(timeout=15)
                    outputs[idx] = value
                except Exception as exc:
                    logger.warning("Pilar 4 TTS paralelo falhou no segmento %d: %s", idx, exc)
        with self._lock:
            session.state = PipelineState.DONE
            session.tokens_generated = len(chars)
            self._stats["total_chunks_processed"] += 1
            self._stats["total_segments_generated"] += len(pieces[:8])
        return {"session_id": sid, "reply": reply, "segments": [{"text": p, "audio": outputs[i]} for i, p in enumerate(pieces[:8])], "first_segment_ms": first_ms, "latency_ms": int((time.time() - started) * 1000), "parallel": True}

    def get_session_stats(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if session is None:
                return None
            return {"session_id": session.session_id, "fixture_id": session.fixture_id or session.match_context.get("fixture_id"), "mood": session.mood, "state": session.state.value, "tokens_generated": session.tokens_generated, "first_segment_time": session.first_segment_time}

    def reset_session(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(str(session_id), None) is not None

    def cleanup_old_sessions(self, max_age_seconds: float = 3600.0) -> int:
        cutoff = time.time() - float(max_age_seconds)
        with self._lock:
            old = [sid for sid, session in self._sessions.items() if session.created_at < cutoff]
            for sid in old:
                self._sessions.pop(sid, None)
            return len(old)

    def get_global_stats(self) -> Dict[str, Any]:
        with self._lock:
            stats = dict(self._stats)
        stats.update({"state": "idle" if not self._sessions else "active", "input_queue_size": self._input_queue.qsize(), "output_queue_size": self._tts_queue.qsize()})
        return stats

    def get_stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def stop(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set() and not self._started:
            return
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)
        if self._tts_worker.is_alive():
            self._tts_worker.join(timeout=2.0)
        self._started = False
        logger.info("Pipeline de áudio encerrado")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    pipeline = ParallelAudioPipeline()
    pipeline.submit_audio("sess_001", b"\x00" * 1600, {"match_id": "TEST", "odds": 1.95})
    time.sleep(1.5)
    print(pipeline.get_stats())
    pipeline.shutdown()
    # --- V23 BLOCO 6: Barge-in ---
    def mark_speaking(self, speaking: bool = True) -> None:
        with self._lock:
            self._is_speaking = bool(speaking)
            if speaking:
                self._stop_speaking_flag.clear()

    def interrupt_speech(self) -> bool:
        """Chamado pelo STT/VAD quando o usuario fala durante TTS."""
        with self._lock:
            if not getattr(self, "_is_speaking", False):
                return False
            if not hasattr(self, "_stop_speaking_flag"):
                import threading
                self._stop_speaking_flag = threading.Event()
            self._stop_speaking_flag.set()
            try:
                logger.info("BARGE-IN: usuario falou, interrompendo TTS")
            except Exception:
                pass
            return True

    def should_stop_speaking(self) -> bool:
        return bool(getattr(self, "_stop_speaking_flag", None) and self._stop_speaking_flag.is_set())


