from __future__ import annotations
import queue, logging, threading
from typing import Callable, Optional

logger = logging.getLogger("aura.artifact_worker")

class ArtifactWorker:
    def __init__(self, maxsize: int = 2048, process_fn: Optional[Callable[[dict], None]] = None) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="artifact-worker")
        self._process_fn = process_fn
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._thread.start()
        self._started = True

    def submit(self, job: dict) -> None:
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            logger.warning("artifact queue full")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if self._process_fn:
                    self._process_fn(job)
            except Exception as e:
                logger.error("artifact fail: %s", e)
            finally:
                self._queue.task_done()

artifact_worker = ArtifactWorker()
