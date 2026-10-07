"""Single background worker thread.

TradingView cancels webhooks that take more than 3 seconds, so the endpoint
only validates, stores and enqueues. One worker drains the queue in arrival
order, which also means risk checks never race each other (two alerts cannot
both see "no position" and both open one).
"""

from __future__ import annotations

import logging
import queue
import threading

from app.services.processor import AlertProcessor, QueuedAlert

log = logging.getLogger(__name__)


class AlertWorker:
    def __init__(self, processor: AlertProcessor):
        self._processor = processor
        self._queue: queue.Queue[QueuedAlert | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="alert-worker", daemon=True)
        self._thread.start()

    def submit(self, item: QueuedAlert) -> None:
        self._queue.put(item)

    def depth(self) -> int:
        return self._queue.qsize()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def wait_idle(self) -> None:
        """Block until every submitted alert has been processed (used by tests)."""
        self._queue.join()

    def stop(self, timeout: float = 30) -> None:
        if not self._thread:
            return
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._processor.process(item)
            except Exception:
                log.exception("worker failed to process alert", extra={"alert_pk": getattr(item, "alert_pk", None)})
            finally:
                self._queue.task_done()
