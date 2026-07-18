import time
import unittest
from threading import Event, Thread

from auto_ingest import AutomaticIngestService, IngestAlreadyRunning


class AutomaticIngestServiceTest(unittest.TestCase):
    def test_periodic_scan_repeats_and_stops(self):
        repeated = Event()
        calls = []

        def scan():
            calls.append(len(calls) + 1)
            if len(calls) >= 2:
                repeated.set()
            return {
                "new_count": 0,
                "changed_count": 0,
                "unchanged_count": 1,
                "error_count": 0,
                "imported_chunks": 0,
            }

        service = AutomaticIngestService(scan, enabled=True, interval_seconds=0.01)
        service.start()
        self.assertTrue(repeated.wait(0.5))

        service.stop(timeout=1)
        stopped_count = len(calls)
        time.sleep(0.03)

        status = service.snapshot()
        self.assertFalse(status["running"])
        self.assertFalse(status["thread_alive"])
        self.assertIsNone(status["next_run_at"])
        self.assertEqual(len(calls), stopped_count)
        self.assertGreaterEqual(status["run_count"], 2)

    def test_run_once_rejects_overlapping_execution(self):
        entered = Event()
        release = Event()

        def scan():
            entered.set()
            release.wait(1)
            return {
                "new_count": 0,
                "changed_count": 0,
                "unchanged_count": 0,
                "error_count": 0,
                "imported_chunks": 0,
            }

        service = AutomaticIngestService(scan, enabled=True, interval_seconds=60)
        worker = Thread(target=lambda: service.run_once("automatic"), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(0.5))

        with self.assertRaises(IngestAlreadyRunning):
            service.run_once("manual")

        release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(service.snapshot()["skipped_count"], 1)

    def test_failure_is_recorded_and_next_success_recovers(self):
        outcomes = iter(
            [
                RuntimeError("private source path must not leak"),
                {
                    "new_count": 1,
                    "changed_count": 0,
                    "unchanged_count": 2,
                    "error_count": 0,
                    "imported_chunks": 3,
                },
            ]
        )

        def scan():
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        service = AutomaticIngestService(scan, enabled=True, interval_seconds=60)
        with self.assertRaises(RuntimeError):
            service.run_once("automatic")

        failed = service.snapshot()
        self.assertEqual(failed["consecutive_failures"], 1)
        self.assertEqual(failed["last_error"]["type"], "RuntimeError")
        self.assertNotIn("private source path", str(failed["last_error"]))

        result = service.run_once("automatic")
        recovered = service.snapshot()
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertIsNone(recovered["last_error"])
        self.assertEqual(recovered["last_result"]["imported_chunks"], 3)

    def test_snapshot_is_detached_from_internal_state(self):
        service = AutomaticIngestService(
            lambda: {"new_count": 1, "error_count": 0},
            enabled=False,
            interval_seconds=60,
        )
        service.run_once("manual")

        snapshot = service.snapshot()
        snapshot["last_result"]["new_count"] = 999

        self.assertEqual(service.snapshot()["last_result"]["new_count"], 1)


if __name__ == "__main__":
    unittest.main()
