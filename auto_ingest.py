from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


class IngestAlreadyRunning(RuntimeError):
    """Raised when another automatic or manual ingest owns the scan slot."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AutomaticIngestService:
    def __init__(
        self,
        scan: Callable[[], dict[str, Any]],
        *,
        enabled: bool,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")

        self._scan = scan
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "enabled": enabled,
            "running": False,
            "active_trigger": None,
            "interval_seconds": interval_seconds,
            "last_started_at": None,
            "last_finished_at": None,
            "next_run_at": None,
            "run_count": 0,
            "skipped_count": 0,
            "consecutive_failures": 0,
            "last_result": None,
            "last_error": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            status = copy.deepcopy(self._state)
            status["thread_alive"] = bool(self._thread and self._thread.is_alive())
            return status

    def run_once(self, trigger: str) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            with self._state_lock:
                self._state["skipped_count"] += 1
            raise IngestAlreadyRunning("regulation ingest already running")

        started_at = utc_now()
        with self._state_lock:
            self._state.update(
                running=True,
                active_trigger=trigger,
                last_started_at=started_at.isoformat(),
                last_error=None,
            )

        try:
            result = self._scan()
        except Exception as exc:
            with self._state_lock:
                self._state["consecutive_failures"] += 1
                self._state["last_error"] = {
                    "type": type(exc).__name__,
                    "message": "automatic regulation scan failed",
                }
            raise
        else:
            result_keys = (
                "new_count",
                "changed_count",
                "unchanged_count",
                "error_count",
                "imported_chunks",
            )
            with self._state_lock:
                self._state["run_count"] += 1
                self._state["consecutive_failures"] = 0
                self._state["last_result"] = {
                    key: result.get(key, 0) for key in result_keys
                }
                self._state["last_error"] = None
            return result
        finally:
            finished_at = utc_now()
            with self._state_lock:
                self._state.update(
                    running=False,
                    active_trigger=None,
                    last_finished_at=finished_at.isoformat(),
                )
            self._run_lock.release()

    def start(self) -> None:
        if not self._enabled:
            return
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._set_next_run()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="regulation-auto-ingest",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        with self._state_lock:
            self._state["next_run_at"] = None

    def _set_next_run(self) -> None:
        if self._stop_event.is_set():
            next_run_at = None
        else:
            next_run_at = (
                utc_now() + timedelta(seconds=self._interval_seconds)
            ).isoformat()
        with self._state_lock:
            self._state["next_run_at"] = next_run_at

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self.run_once("automatic")
            except IngestAlreadyRunning:
                pass
            except Exception as exc:
                print(f"Automatic regulation scan failed: {type(exc).__name__}")
            self._set_next_run()

