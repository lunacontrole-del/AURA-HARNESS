#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AURA Performance Optimizer v1.0
Otimizações de cache, compressão, pooling e batch processing para Bridge e Engine.
"""
import os, sys, json, time, gzip, hashlib, sqlite3, threading
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Optional
import pickle

AURA_ROOT = Path(os.environ.get("AURA_ROOT", os.getcwd()))
CACHE_DIR = AURA_ROOT / "runtime" / "cache"


class SmartCache:
    """Cache em disco com TTL, compressão gzip e invalidação por hash."""

    def __init__(self, namespace: str = "default", ttl_seconds: int = 300, compress: bool = True):
        self.namespace = namespace
        self.ttl = ttl_seconds
        self.compress = compress
        self._lock = threading.RLock()
        self._memory = {}
        self._ns_dir = CACHE_DIR / namespace
        self._ns_dir.mkdir(parents=True, exist_ok=True)

    def _key_path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self._ns_dir / f"{h}.cache"

    def _serialize(self, value: Any) -> bytes:
        data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        if self.compress and len(data) > 1024:
            return gzip.compress(data, compresslevel=6)
        return data

    def _deserialize(self, data: bytes) -> Any:
        try:
            payload = gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
            return pickle.loads(payload)
        except Exception:
            return None

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            # Memória primeiro
            if key in self._memory:
                entry = self._memory[key]
                if entry["expires"] > time.time():
                    return entry["value"]
                del self._memory[key]

            # Disco
            path = self._key_path(key)
            if not path.exists():
                return None

            try:
                data = path.read_bytes()
                meta_len = int.from_bytes(data[:4], "big")
                meta = json.loads(data[4:4+meta_len])

                if meta.get("expires", 0) < time.time():
                    path.unlink(missing_ok=True)
                    return None

                value = self._deserialize(data[4+meta_len:])
                # Promover para memória
                self._memory[key] = {"value": value, "expires": time.time() + min(self.ttl, 60)}
                return value
            except Exception:
                path.unlink(missing_ok=True)
                return None

    def set(self, key: str, value: Any, ttl: int = None):
        with self._lock:
            ttl = ttl or self.ttl
            expires = time.time() + ttl

            # Memória (curto prazo)
            self._memory[key] = {"value": value, "expires": expires}

            # Disco (longo prazo)
            path = self._key_path(key)
            meta = json.dumps({"expires": expires, "created": time.time()}).encode()
            meta_len = len(meta).to_bytes(4, "big")
            data = meta_len + meta + self._serialize(value)
            path.write_bytes(data)

    def invalidate(self, pattern: str = None):
        with self._lock:
            self._memory.clear()
            if pattern:
                for f in self._ns_dir.glob("*.cache"):
                    f.unlink()
            else:
                for f in self._ns_dir.glob("*.cache"):
                    f.unlink()

    def stats(self) -> dict:
        files = list(self._ns_dir.glob("*.cache"))
        total_size = sum(f.stat().st_size for f in files)
        return {
            "namespace": self.namespace,
            "memory_entries": len(self._memory),
            "disk_entries": len(files),
            "disk_size_mb": round(total_size / (1024 * 1024), 2),
            "ttl_seconds": self.ttl,
        }


class ConnectionPool:
    """Pool de conexões HTTP com keep-alive e limitação de slots."""

    def __init__(self, max_size: int = 10, timeout: int = 30):
        self.max_size = max_size
        self.timeout = timeout
        self._pool = []
        self._lock = threading.RLock()
        self._stats = {"created": 0, "reused": 0, "evicted": 0}

    def acquire(self):
        with self._lock:
            while self._pool:
                conn = self._pool.pop()
                if conn["expires"] > time.time():
                    self._stats["reused"] += 1
                    return conn["obj"]
                self._stats["evicted"] += 1
            self._stats["created"] += 1
            return None

    def release(self, obj):
        with self._lock:
            if len(self._pool) < self.max_size:
                self._pool.append({"obj": obj, "expires": time.time() + self.timeout})

    def stats(self) -> dict:
        with self._lock:
            return {**self._stats, "available": len(self._pool), "max_size": self.max_size}


class BatchProcessor:
    """Processamento em batch com janela deslizante e flush automático."""

    def __init__(self, max_size: int = 100, max_wait_ms: int = 500, processor=None):
        self.max_size = max_size
        self.max_wait_ms = max_wait_ms
        self.processor = processor
        self._buffer = []
        self._lock = threading.RLock()
        self._last_flush = time.time()
        self._flush_count = 0
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    def add(self, item: Any):
        with self._lock:
            self._buffer.append(item)
            if len(self._buffer) >= self.max_size:
                self._flush()

    def _flush(self):
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer = []
            self._last_flush = time.time()
            self._flush_count += 1

        if self.processor:
            try:
                self.processor(batch)
            except Exception as e:
                print(f"[BatchProcessor] Erro no processamento: {e}")

    def _flush_loop(self):
        while True:
            time.sleep(self.max_wait_ms / 1000)
            if time.time() - self._last_flush >= self.max_wait_ms / 1000 and self._buffer:
                self._flush()

    def stats(self) -> dict:
        with self._lock:
            return {
                "buffer_size": len(self._buffer),
                "flush_count": self._flush_count,
                "max_size": self.max_size,
                "max_wait_ms": self.max_wait_ms,
            }


def cached(ttl_seconds: int = 60, namespace: str = "api", key_func=None):
    """Decorator para cachear resultados de funções."""
    cache = SmartCache(namespace=namespace, ttl_seconds=ttl_seconds)

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if key_func:
                key = key_func(*args, **kwargs)
            else:
                key = f"{func.__name__}:{str(args)}:{str(kwargs)}"

            result = cache.get(key)
            if result is not None:
                return result

            result = func(*args, **kwargs)
            cache.set(key, result)
            return result

        wrapper.cache = cache
        wrapper.cache_stats = cache.stats
        return wrapper
    return decorator


def optimize_sqlite(db_path: str):
    """Otimiza um banco SQLite com WAL mode, índices e VACUUM."""
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB
        conn.execute("VACUUM")
        conn.commit()
        conn.close()
        return {"ok": True, "path": db_path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    # Teste rápido
    cache = SmartCache(namespace="test", ttl_seconds=10)
    cache.set("key1", {"data": [1, 2, 3], "nested": {"a": True}})
    print("Cache stats:", cache.stats())
    print("Get key1:", cache.get("key1"))
