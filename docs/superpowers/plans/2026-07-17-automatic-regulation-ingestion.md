# Automatic Regulation Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continuously scan closed-network regulation folders, register new or changed documents as pending versions, and expose safe scheduler status while keeping the currently approved version searchable and downloadable until audit-lead approval.

## Handoff Status (2026-07-18)

Completed in the current WIP:

- Added `AutomaticIngestService` with periodic execution, single-flight rejection, safe error state, lifecycle stop, and detached status snapshots.
- Added interval validation, shared data locking, dashboard scheduler status, and `409 Conflict` for overlapping manual scans.
- Preserved and included the existing regulation version timeline implementation and tests.
- Verified Python compilation and all 87 discovered unit/integration/static tests.

Next implementation steps:

- Refactor `main()` so `REG_RAG_AUTO_INGEST=1` performs a startup scan, starts periodic polling, and stops the service gracefully. The current `main()` still performs startup-only ingestion.
- Add the approval-gated replacement search/download regression from Task 3.
- Add operations-screen scheduler fields, deployment documentation, JavaScript syntax validation, and browser E2E verification.

**Architecture:** Add a focused `AutomaticIngestService` that owns the background thread, single-flight execution, lifecycle, and public status snapshot. Integrate it with the existing server through one shared data lock so automatic scans, manual scans, registry transitions, index writes, and search snapshots cannot observe partially updated state. Keep the current hash-based registry and approval workflow as the source of truth.

**Tech Stack:** Python 3.12 standard library, `ThreadingHTTPServer`, JSON registry/index storage, vanilla JavaScript, `unittest`

## Global Constraints

- `REG_RAG_AUTO_INGEST=1` enables one startup scan followed by periodic scans.
- `REG_RAG_AUTO_INGEST_INTERVAL_SECONDS` defaults to `60` and rejects production values below `10`.
- New and changed documents must remain `pending` until audit-lead approval.
- An approved current version remains searchable and downloadable until the replacement effective date.
- No new runtime dependency may be added.
- Only one server process may write `data/index.json` and `data/regulation_registry.json`.
- Preserve every pre-existing uncommitted timeline change in `regulation_registry.py`, `server.py`, `static/*`, and `tests/*`.
- Do not automatically remove an approved version when its source file disappears.

---

### Task 1: Automatic Ingest Service

**Files:**
- Create: `auto_ingest.py`
- Create: `tests/test_auto_ingest.py`

**Interfaces:**
- Consumes: a zero-argument scan callback returning a dictionary
- Produces: `IngestAlreadyRunning`, `AutomaticIngestService.run_once(trigger)`, `start()`, `stop()`, and `snapshot()`

- [ ] **Step 1: Write failing lifecycle and status tests**

```python
import time
from threading import Event, Thread
from unittest import TestCase

from auto_ingest import AutomaticIngestService, IngestAlreadyRunning


class AutomaticIngestServiceTest(TestCase):
    def test_periodic_scan_repeats_and_stops(self):
        called = Event()
        calls = []

        def scan():
            calls.append(len(calls) + 1)
            if len(calls) >= 2:
                called.set()
            return {
                "new_count": 0,
                "changed_count": 0,
                "unchanged_count": 1,
                "error_count": 0,
                "imported_chunks": 0,
            }

        service = AutomaticIngestService(scan, enabled=True, interval_seconds=0.01)
        service.start()
        self.assertTrue(called.wait(0.5))
        service.stop(timeout=1)
        stopped_count = len(calls)
        time.sleep(0.03)
        self.assertFalse(service.snapshot()["running"])
        self.assertFalse(service.snapshot()["thread_alive"])
        self.assertEqual(len(calls), stopped_count)

    def test_run_once_rejects_overlapping_execution(self):
        entered = Event()
        release = Event()

        def scan():
            entered.set()
            release.wait(1)
            return {"new_count": 0, "changed_count": 0, "unchanged_count": 0, "error_count": 0}

        service = AutomaticIngestService(scan, enabled=True, interval_seconds=60)
        worker = Thread(target=lambda: service.run_once("automatic"), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(0.5))
        with self.assertRaises(IngestAlreadyRunning):
            service.run_once("manual")
        release.set()
        worker.join(timeout=1)

    def test_failure_is_recorded_and_next_success_recovers(self):
        outcomes = iter([RuntimeError("private path must not leak"), {"new_count": 1, "error_count": 0}])

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
        self.assertNotIn("private path", str(failed["last_error"]))

        service.run_once("automatic")
        recovered = service.snapshot()
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertIsNone(recovered["last_error"])
        self.assertEqual(recovered["last_result"]["new_count"], 1)
```

- [ ] **Step 2: Run the new test file and verify failure**

Run:

```bash
python3 -m unittest tests.test_auto_ingest -v
```

Expected: import failure because `auto_ingest.py` does not exist.

- [ ] **Step 3: Implement the service**

Create `auto_ingest.py` with:

```python
from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


class IngestAlreadyRunning(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AutomaticIngestService:
    def __init__(
        self,
        scan: Callable[[], dict[str, Any]],
        *,
        enabled: bool,
        interval_seconds: float,
    ):
        self._scan = scan
        self._enabled = enabled
        self._interval_seconds = interval_seconds
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = {
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
            snapshot = copy.deepcopy(self._state)
            snapshot["thread_alive"] = bool(self._thread and self._thread.is_alive())
            return snapshot

    def run_once(self, trigger: str) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            with self._state_lock:
                self._state["skipped_count"] += 1
            raise IngestAlreadyRunning("regulation ingest already running")
        started = utc_now()
        with self._state_lock:
            self._state.update(
                running=True,
                active_trigger=trigger,
                last_started_at=started.isoformat(),
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
            summary_keys = (
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
                    key: result.get(key, 0) for key in summary_keys
                }
                self._state["last_error"] = None
            return result
        finally:
            finished = utc_now()
            with self._state_lock:
                self._state.update(
                    running=False,
                    active_trigger=None,
                    last_finished_at=finished.isoformat(),
                )
            self._run_lock.release()

    def start(self) -> None:
        if not self._enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop_event.clear()
        self._schedule_next()
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

    def _schedule_next(self) -> None:
        next_run = utc_now() + timedelta(seconds=self._interval_seconds)
        with self._state_lock:
            self._state["next_run_at"] = next_run.isoformat()

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self.run_once("automatic")
            except IngestAlreadyRunning:
                pass
            except Exception as exc:
                print(f"Automatic regulation scan failed: {type(exc).__name__}")
            self._schedule_next()
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
python3 -m unittest tests.test_auto_ingest -v
```

Expected: all automatic ingest service tests pass.

- [ ] **Step 5: Commit the isolated new files**

```bash
git add auto_ingest.py tests/test_auto_ingest.py
git commit -m "Enable repeatable regulation scans without external services" \
  -m "Constraint: New versions remain pending until audit-lead approval
Rejected: Filesystem event watcher | Network shares can lose events
Confidence: high
Scope-risk: narrow
Tested: python3 -m unittest tests.test_auto_ingest -v
Not-tested: Server integration"
```

---

### Task 2: Server Configuration, Single-Flight Integration, and Dashboard Status

**Files:**
- Modify: `server.py`
- Modify: `tests/test_server_api.py`

**Interfaces:**
- Consumes: `AutomaticIngestService`, `IngestAlreadyRunning`
- Produces: `parse_auto_ingest_interval`, `build_auto_ingest_service`, shared `DATA_LOCK`, global `AUTO_INGEST_SERVICE`, and `dashboard.auto_ingest`

- [ ] **Step 1: Add failing configuration and dashboard tests**

Add to `tests/test_server_api.py`:

```python
class AutoIngestConfigurationTest(unittest.TestCase):
    def test_interval_defaults_to_sixty_seconds(self):
        self.assertEqual(server.parse_auto_ingest_interval(None), 60)

    def test_interval_rejects_invalid_and_too_small_values(self):
        for value in ("bad", "0", "9"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    server.parse_auto_ingest_interval(value)


class FakeAutoIngestService:
    def __init__(self, snapshot, result=None, error=None):
        self._snapshot = snapshot
        self._result = result or {}
        self._error = error

    def snapshot(self):
        return dict(self._snapshot)

    def run_once(self, trigger):
        if self._error:
            raise self._error
        return dict(self._result)
```

Add API route tests:

```python
def test_dashboard_exposes_auto_ingest_status(self):
    fake = FakeAutoIngestService(
        {
            "enabled": True,
            "running": False,
            "interval_seconds": 60,
            "next_run_at": "2026-07-17T01:01:00+00:00",
            "last_result": {"new_count": 1, "changed_count": 0, "error_count": 0},
            "last_error": None,
        }
    )
    with mock.patch.object(server, "AUTO_INGEST_SERVICE", fake):
        status, payload = self.get_json("/api/dashboard")
    self.assertEqual(status, 200)
    self.assertEqual(payload["auto_ingest"]["enabled"], True)
    self.assertEqual(payload["auto_ingest"]["interval_seconds"], 60)

def test_manual_ingest_returns_conflict_while_scan_is_running(self):
    fake = FakeAutoIngestService(
        {"enabled": True, "running": True, "interval_seconds": 60},
        error=server.IngestAlreadyRunning("regulation ingest already running"),
    )
    with mock.patch.object(server, "AUTO_INGEST_SERVICE", fake):
        status, payload = self.post_json("/api/ingest-local", {})
    self.assertEqual(status, 409)
    self.assertIn("already running", payload["error"])
    self.assertEqual(payload["auto_ingest"]["running"], True)
```

- [ ] **Step 2: Run targeted API tests and verify failure**

Run:

```bash
python3 -m unittest \
  tests.test_server_api.AutoIngestConfigurationTest \
  tests.test_server_api.ApiRoutesTest.test_dashboard_exposes_auto_ingest_status \
  tests.test_server_api.ApiRoutesTest.test_manual_ingest_returns_conflict_while_scan_is_running -v
```

Expected: failures for missing configuration parser, global service, and dashboard field.

- [ ] **Step 3: Add imports, shared lock, and configuration**

At the top of `server.py` add:

```python
from threading import RLock

from auto_ingest import AutomaticIngestService, IngestAlreadyRunning
```

Add module state:

```python
DATA_LOCK = RLock()
AUTO_INGEST_SERVICE: AutomaticIngestService | None = None


def parse_auto_ingest_interval(value: str | None) -> int:
    raw_value = value or "60"
    try:
        interval = int(raw_value)
    except ValueError as exc:
        raise ValueError("REG_RAG_AUTO_INGEST_INTERVAL_SECONDS must be an integer") from exc
    if interval < 10:
        raise ValueError("REG_RAG_AUTO_INGEST_INTERVAL_SECONDS must be at least 10")
    return interval
```

- [ ] **Step 4: Protect index and registry transactions**

Wrap complete read-modify-write operations with the reentrant lock:

```python
def ingest_registered_sources(paths, *, canonical_title=None) -> dict[str, Any]:
    with DATA_LOCK:
        result = REGISTRY.scan_sources(
            paths,
            ingest=ingest_file,
            effective_date=lambda path: date_from_text(path.name),
            canonical_title=canonical_title,
        )
        chunks = result.pop("chunks")
        result["imported_chunks"] = len(chunks)
        indexed_version_ids = {chunk["version_id"] for chunk in chunks if chunk.get("version_id")}
        version_ids = sorted(set(result.pop("version_ids", [])) | indexed_version_ids)
        if chunks:
            add_chunks(chunks)
            REGISTRY.mark_versions_indexed(sorted(indexed_version_ids))
        result["versions"] = [
            public_version(REGISTRY.state["versions"][version_id])
            for version_id in version_ids
            if version_id in REGISTRY.state.get("versions", {})
        ]
        result["documents"] = document_summary()
        return result
```

Wrap each complete search snapshot instead of individual file reads:

```python
def search_index(
    query: str,
    role: str,
    as_of: str | None,
    limit: int = 6,
    include_history: bool = False,
) -> dict[str, Any]:
    with DATA_LOCK:
        return search_index_snapshot(query, role, as_of, limit, include_history)
```

Rename the current `search_index` body to `search_index_snapshot` with the same parameters. Keep its existing timeline logic unchanged. Wrap the complete bodies of `dashboard_payload`, `versions_payload`, and `get_chunk_by_id` with `with DATA_LOCK:`.

Wrap registry mutations at their call sites:

```python
with DATA_LOCK:
    version = REGISTRY.approve_version(str(version_id), actor, effective_from)

with DATA_LOCK:
    version = REGISTRY.reject_version(str(version_id), actor, str(reason))

with DATA_LOCK:
    seed_index(force=True)
    clear_uploads()
    REGISTRY.reset()
    documents = document_summary()

with DATA_LOCK:
    REGISTRY.record_event(
        "SearchExecuted",
        summary=payload["summary"],
        metadata=payload["metadata"],
        actor_role=result["role"],
        actor_name="search-user",
        target_type="regulation_search",
        target_id="search",
    )
```

Extract the existing upload-directory cleanup loop to `clear_uploads() -> None` so reset remains one locked transaction. Keep `DATA_LOCK` reentrant because these functions call `load_index`, `add_chunks`, and registry methods from inside the same transaction.

- [ ] **Step 5: Build the service and expose status**

After `ingest_local_sources` define:

```python
def build_auto_ingest_service(*, enabled: bool, interval_seconds: int) -> AutomaticIngestService:
    return AutomaticIngestService(
        ingest_local_sources,
        enabled=enabled,
        interval_seconds=interval_seconds,
    )


AUTO_INGEST_SERVICE = build_auto_ingest_service(enabled=False, interval_seconds=60)
```

Add to `dashboard_payload`:

```python
"auto_ingest": AUTO_INGEST_SERVICE.snapshot(),
```

Replace the manual route with:

```python
if path == "/api/ingest-local":
    try:
        result = AUTO_INGEST_SERVICE.run_once("manual")
    except IngestAlreadyRunning as exc:
        json_response(
            self,
            HTTPStatus.CONFLICT,
            {
                "error": str(exc),
                "auto_ingest": AUTO_INGEST_SERVICE.snapshot(),
            },
        )
        return
    json_response(self, HTTPStatus.OK, redact_source_paths(result))
    return
```

- [ ] **Step 6: Integrate startup scan, periodic start, and graceful stop**

Refactor `main`:

```python
def main() -> None:
    global AUTO_INGEST_SERVICE

    parser = argparse.ArgumentParser(description="Internal regulation RAG prototype")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--reset", action="store_true", help="reset sample index before serving")
    parser.add_argument("--ingest-local", action="store_true", help="index local regulation source folders before serving")
    args = parser.parse_args()

    auto_enabled = os.environ.get("REG_RAG_AUTO_INGEST") == "1"
    interval_seconds = (
        parse_auto_ingest_interval(os.environ.get("REG_RAG_AUTO_INGEST_INTERVAL_SECONDS"))
        if auto_enabled
        else 60
    )
    AUTO_INGEST_SERVICE = build_auto_ingest_service(
        enabled=auto_enabled,
        interval_seconds=interval_seconds,
    )

    seed_index(force=args.reset)
    if args.ingest_local or auto_enabled:
        result = AUTO_INGEST_SERVICE.run_once("startup")
        print(
            "Local ingest: "
            f"{result['imported_chunks']} chunks, "
            f"{len(result['documents'])} documents, "
            f"{len(result['errors'])} errors"
        )

    server = ThreadingHTTPServer((args.host, args.port), RegRagHandler)
    AUTO_INGEST_SERVICE.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        AUTO_INGEST_SERVICE.stop()
        server.server_close()
```

- [ ] **Step 7: Run server and existing registry tests**

Run:

```bash
python3 -m unittest tests.test_auto_ingest tests.test_regulation_registry tests.test_server_api -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit only after reviewing the pre-existing shared-file diff**

Before staging, run:

```bash
git diff -- server.py tests/test_server_api.py
```

Confirm that the existing timeline changes remain intact. Then stage the shared files only when the full diff is intended:

```bash
git add server.py tests/test_server_api.py
git commit -m "Keep regulation folders synchronized during server operation" \
  -m "Constraint: Automatic scans must not publish pending versions
Rejected: Concurrent manual and automatic scans | They can corrupt JSON state
Confidence: high
Scope-risk: moderate
Directive: Run only one writer server per registry and index
Tested: python3 -m unittest tests.test_auto_ingest tests.test_regulation_registry tests.test_server_api -v
Not-tested: Browser UI"
```

---

### Task 3: Approval-Gated Latest Search and Download Regression

**Files:**
- Modify: `tests/test_server_api.py`
- Modify: `tests/test_regulation_registry.py`
- Modify: `server.py` only if the new regression test exposes inconsistent locking or metadata

**Interfaces:**
- Consumes: `AutomaticIngestService.run_once`, existing `approve_version`, `search_index`, and opaque source download URLs
- Produces: regression coverage proving automatic detection does not replace the current approved version

- [ ] **Step 1: Add a failing end-to-end registry/search test**

Add a test using a temporary source file:

```python
def test_automatic_scan_keeps_pending_replacement_out_of_latest_search(self):
    current = self.registry.record_detection(
        "인사규정",
        "/closed/인사규정_2025.hwp",
        "old-hash",
        "2025-01-01",
        ["old-chunk"],
    )
    self.registry.approve_version(current["version_id"], "감사팀장", "2025-01-01")
    current_chunk = server.make_chunk(
        doc_title="인사규정",
        section_title="징계",
        text="기존 징계 기준",
        source_file="인사규정_2025.hwp",
        source_type="hwp",
        effective_from="2025-01-01",
    )
    current_chunk.update(
        version_id=current["version_id"],
        regulation_id=current["regulation_id"],
        version_status="approved",
    )
    self.write_chunks([current_chunk])

    replacement_path = Path(self.tmp.name) / "인사규정_2026.07.17.hwp"
    replacement_path.write_bytes(b"changed-regulation")
    replacement_chunks = [
        server.make_chunk(
            doc_title="인사규정",
            section_title="징계",
            text="새 징계 기준",
            source_file=replacement_path.name,
            source_type="hwp",
            source_path=str(replacement_path),
        )
    ]

    service = server.build_auto_ingest_service(enabled=False, interval_seconds=60)
    with mock.patch.object(server, "AUTO_INGEST_SERVICE", service), mock.patch.object(
        server, "local_sources", return_value=[replacement_path]
    ), mock.patch.object(server, "ingest_file", return_value=replacement_chunks):
        service.run_once("automatic")

    before_approval = server.search_index("징계 기준", "employee", "2026-07-17")
    self.assertEqual([item["text"] for item in before_approval["results"]], ["기존 징계 기준"])

    pending = next(
        version
        for version in self.registry.state["versions"].values()
        if version["status"] == "pending"
    )
    self.registry.approve_version(pending["version_id"], "감사팀장", "2026-07-17")
    after_approval = server.search_index("징계 기준", "employee", "2026-07-17")
    self.assertEqual(after_approval["results"][0]["text"], "새 징계 기준")
    self.assertEqual(
        after_approval["results"][0]["download"]["source"],
        f"/api/download/source?id={replacement_chunks[0]['id']}",
    )
```

Use the existing isolated registry/index test fixture so `server.REGISTRY`, `INDEX_FILE`, and `AUTO_INGEST_SERVICE` are restored after each test.

- [ ] **Step 2: Run the targeted regression**

Run:

```bash
python3 -m unittest \
  tests.test_server_api.SearchVersionFilterTest.test_automatic_scan_keeps_pending_replacement_out_of_latest_search -v
```

Expected: fail if automatic ingestion leaks pending chunks or stale version metadata.

- [ ] **Step 3: Make the smallest consistency fix**

If the failure is caused by a stale chunk copy, reuse the existing registry hydration path:

```python
chunks = [hydrate_chunk_version_metadata(chunk) for chunk in payload.get("chunks", [])]
```

Do not relax the allowed version ID filter. Pending, rejected, and scan-error version IDs must remain absent from `allowed_version_ids`.

- [ ] **Step 4: Run registry and search suites**

Run:

```bash
python3 -m unittest tests.test_regulation_registry tests.test_server_api.SearchVersionFilterTest -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the regression coverage with its minimal fix**

```bash
git add tests/test_server_api.py tests/test_regulation_registry.py server.py
git commit -m "Keep approved regulations active until replacement approval" \
  -m "Constraint: Automatic detection cannot change the current searchable version
Confidence: high
Scope-risk: moderate
Directive: Never include pending version IDs in latest search
Tested: python3 -m unittest tests.test_regulation_registry tests.test_server_api.SearchVersionFilterTest -v
Not-tested: Production HWP parser"
```

---

### Task 4: Operations UI for Automatic Refresh

**Files:**
- Modify: `static/app.js`
- Modify: `static/styles.css` only if existing status-card layout cannot fit the new fields
- Modify: `tests/test_static_ui.py`

**Interfaces:**
- Consumes: `dashboard.auto_ingest`
- Produces: visible automatic refresh status, interval, next run, last result, and last error

- [ ] **Step 1: Add failing static UI assertions**

Add to `tests/test_static_ui.py`:

```python
def test_operations_view_surfaces_automatic_ingest_status(self):
    self.assertIn("dashboard.auto_ingest", JS)
    self.assertIn("자동 갱신", JS)
    self.assertIn("갱신 주기", JS)
    self.assertIn("다음 스캔", JS)
    self.assertIn("최근 자동 결과", JS)
    self.assertIn("최근 갱신 오류", JS)
    self.assertIn("interval_seconds", JS)
    self.assertIn("next_run_at", JS)
    self.assertIn("last_result", JS)
    self.assertIn("last_error", JS)
```

- [ ] **Step 2: Run the targeted static test and verify failure**

Run:

```bash
python3 -m unittest \
  tests.test_static_ui.ProductShellStaticTest.test_operations_view_surfaces_automatic_ingest_status -v
```

Expected: failure because automatic refresh copy and fields are absent.

- [ ] **Step 3: Render scheduler status in the operations view**

Add helpers in `static/app.js`:

```javascript
function autoIngestState(autoIngest) {
  if (!autoIngest?.enabled) return "사용 안 함";
  if (autoIngest.running) return "스캔 중";
  return "대기";
}

function autoIngestResult(autoIngest) {
  const result = autoIngest?.last_result;
  if (!result) return "실행 기록 없음";
  return `신규 ${Number(result.new_count || 0)} · 변경 ${Number(result.changed_count || 0)} · 오류 ${Number(result.error_count || 0)}`;
}
```

Update `renderOperationsView`:

```javascript
const autoIngest = dashboard.auto_ingest || {};
const interval = autoIngest.enabled ? `${Number(autoIngest.interval_seconds || 0)}초` : "설정 없음";
const nextRun = autoIngest.next_run_at
  ? formatScanTime({ finished_at: autoIngest.next_run_at })
  : "예정 없음";
const lastError = autoIngest.last_error?.message || "없음";
```

Append status cards:

```javascript
statusCard("자동 갱신", autoIngestState(autoIngest), autoIngest.running ? "규정 폴더 검사 중" : "승인 전 검토 대기 등록"),
statusCard("갱신 주기", interval, "환경변수 설정"),
statusCard("다음 스캔", nextRun, "폐쇄망 폴더 기준"),
statusCard("최근 자동 결과", autoIngestResult(autoIngest), "신규·변경·오류"),
statusCard("최근 갱신 오류", lastError, "오류 후 다음 주기 재시도"),
```

- [ ] **Step 4: Run static UI tests**

Run:

```bash
python3 -m unittest tests.test_static_ui -v
```

Expected: all static UI tests pass.

- [ ] **Step 5: Commit UI status changes after reviewing existing timeline markup**

```bash
git diff -- static/app.js static/styles.css tests/test_static_ui.py
git add static/app.js static/styles.css tests/test_static_ui.py
git commit -m "Make regulation refresh state visible to operators" \
  -m "Constraint: Operators must see the next scan and recent failures without exposing source paths
Confidence: high
Scope-risk: narrow
Directive: Keep automatic approval out of the operations UI
Tested: python3 -m unittest tests.test_static_ui -v
Not-tested: Browser screenshot"
```

---

### Task 5: Deployment Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `tests/e2e_smoke.py` only when the existing E2E fixture can assert dashboard scheduler fields without requiring production timing

**Interfaces:**
- Consumes: environment variables and dashboard contract from Tasks 1-4
- Produces: closed-network and Docker run instructions with a configurable polling interval

- [ ] **Step 1: Update deployment documentation**

Replace the old startup-only description with:

```markdown
- `REG_RAG_AUTO_INGEST=1`: 서버 시작 시 최초 스캔을 실행하고 서버 운영 중에도 주기적으로 규정 폴더를 다시 검사합니다.
- `REG_RAG_AUTO_INGEST_INTERVAL_SECONDS=60`: 자동 스캔 주기입니다. 운영 환경에서는 10초 이상의 정수를 사용합니다.
- 새 파일과 변경 파일은 자동으로 색인되지만 `검토 대기`로 등록되며, 감사팀장 승인 전에는 현재 검색·다운로드 결과를 변경하지 않습니다.
```

Update the internal server command:

```bash
REG_RAG_SOURCE_DIRS="/srv/cheonan/regulations" \
REG_RAG_AUTO_INGEST=1 \
REG_RAG_AUTO_INGEST_INTERVAL_SECONDS=60 \
python3 server.py --host 0.0.0.0 --port 8765
```

Update the Docker command:

```bash
docker run --rm -p 8765:8765 \
  -e REG_RAG_SOURCE_DIRS=/sources \
  -e REG_RAG_AUTO_INGEST=1 \
  -e REG_RAG_AUTO_INGEST_INTERVAL_SECONDS=60 \
  -v "/srv/cheonan/regulations":/sources:ro \
  ghcr.io/lsb-afk/search-for-ai-based-internal-regulations:latest
```

Document that one writer container must own the JSON index and registry.

- [ ] **Step 2: Run the complete unit and integration suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 3: Run syntax checks**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/reg-rag-pycache python3 -m py_compile \
  auto_ingest.py regulation_registry.py server.py
```

Expected: exit code 0.

Run:

```bash
node --check static/app.js
```

Expected: exit code 0.

- [ ] **Step 4: Run the browser E2E when Playwright is available**

Run:

```bash
python3 tests/e2e_smoke.py
```

Expected: desktop and mobile smoke tests pass. If Python Playwright is unavailable, record the exact missing dependency and do not claim browser E2E completion.

- [ ] **Step 5: Run a short live scheduler smoke test**

Use a temporary copied app and a test-only service interval below the production validation floor through direct class construction. Verify:

- two automatic callbacks occur;
- the service snapshot reports `run_count >= 2`;
- `stop()` clears `next_run_at`;
- no callback occurs after stop.

The automated `tests/test_auto_ingest.py` test is the source of truth for this behavior.

- [ ] **Step 6: Review the final diff and record remaining boundaries**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Confirm:

- no approved version is automatically replaced;
- no source path is added to dashboard responses;
- existing timeline work remains present;
- no dependency was added;
- README no longer describes `REG_RAG_AUTO_INGEST` as startup-only;
- the single-writer deployment limitation is explicit.

- [ ] **Step 7: Commit documentation and final verification adjustments**

```bash
git add README.md tests/e2e_smoke.py
git commit -m "Document continuous closed-network regulation refresh" \
  -m "Constraint: Deployments must run one writer process against the JSON registry
Confidence: high
Scope-risk: narrow
Directive: Preserve approval-gated publication when changing deployment settings
Tested: Full unittest suite, Python compile, JavaScript syntax, scheduler smoke
Not-tested: State the Playwright result exactly"
```
