# Regulation Version Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make real regulation revisions group correctly and expose a searchable, newest-first version timeline with per-version downloads.

**Architecture:** Preserve the existing as-of search results and add a timelines projection only when include_history is enabled. Derive timelines from registry state and existing opaque chunk download endpoints so no source path is exposed and no second search API is required.

**Tech Stack:** Python standard library HTTP server, JSON registry/index files, vanilla JavaScript, CSS, unittest, Playwright.

## Global Constraints

- No new dependencies.
- Keep the existing deterministic as-of answer behavior.
- Keep demo mutation and role-simulation boundaries explicit.
- Do not expose source_path through public APIs or browser markup.
- Add each regression test before its production change and verify the expected failure.

---

### Task 1: Normalize real revision filenames

**Files:**
- Modify: regulation_registry.py
- Test: tests/test_regulation_registry.py

**Interfaces:**
- Consumes: _normalize_title(value: str) -> str
- Produces: stable canonical_title values shared by successive real filenames

- [ ] **Step 1: Write the failing registry test**

Add a test that records these titles with different hashes and dates:

~~~python
first = registry.record_detection(
    "18. 인사규정(개정 2025.12.31.).hwp",
    "/closed/first.hwp",
    "first",
    "2025-12-31",
    ["first-chunk"],
)
second = registry.record_detection(
    "18. 인사규정(개정 2026.4.13.).hwp",
    "/closed/second.hwp",
    "second",
    "2026-04-13",
    ["second-chunk"],
)
self.assertEqual(first["regulation_id"], second["regulation_id"])
self.assertEqual(first["canonical_title"], "18. 인사규정")
~~~

- [ ] **Step 2: Verify RED**

Run:

~~~sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_regulation_registry.RegulationRegistryTest.test_real_revision_filenames_share_canonical_title -v
~~~

Expected: FAIL because the parenthesized revision date remains in canonical_title.

- [ ] **Step 3: Implement the minimal normalization**

Normalize NFC, remove the extension, strip a trailing parenthesized 제정/개정/전부개정 date, then retain the existing bare-date cleanup.

- [ ] **Step 4: Verify GREEN**

Run the targeted test and the complete regulation registry test module. Expected: PASS.

### Task 2: Return version timelines without changing as-of answers

**Files:**
- Modify: server.py
- Test: tests/test_server_api.py

**Interfaces:**
- Produces: version_download(version, chunks) -> dict | None
- Produces: build_version_timelines(regulation_ids, chunks) -> list[dict]
- Extends: search_index(..., include_history=True) with timelines

- [ ] **Step 1: Write failing API/search tests**

Create two approved versions of one regulation with distinct chunks. Assert:

~~~python
result = server.search_index(
    "인사규정 징계",
    "admin",
    "2026-05-27",
    include_history=True,
)
self.assertEqual(
    [item["effective_from"] for item in result["timelines"][0]["versions"]],
    ["2026-05-27", "2025-01-01"],
)
self.assertIn("/api/download/source?id=", result["timelines"][0]["versions"][0]["download"]["source"])
~~~

Also assert include_history=False returns timelines=[], the current results contain only the effective version, and version_status reflects the current registry status after approval.

- [ ] **Step 2: Verify RED**

Run the new tests. Expected: FAIL because timelines is absent and stored chunk status remains pending.

- [ ] **Step 3: Implement the minimal projection**

Keep the current search call for results. When include_history is true, search eligible historical chunks without an effective-date filter only to identify matched regulation ids. Build timeline entries from current registry versions and current index chunks. Hydrate returned result status and effective windows from registry state.

- [ ] **Step 4: Verify GREEN**

Run the targeted search tests and the complete server API test module. Expected: PASS.

### Task 3: Put uploads and reset inside the version workflow

**Files:**
- Modify: regulation_registry.py
- Modify: server.py
- Test: tests/test_server_api.py
- Test: tests/test_regulation_registry.py

**Interfaces:**
- Extends: RegulationRegistry.scan_sources with an optional canonical-title callback
- Produces: RegulationRegistry.reset() -> None
- Changes: POST /api/upload returns a pending version and indexed document summary

- [ ] **Step 1: Write failing upload and reset tests**

Upload a synthetic HWPX. Assert the response exposes status pending, search returns no uploaded result before approval, and the result appears after approval. Populate registry state, call reset, and assert versions/events are empty.

- [ ] **Step 2: Verify RED**

Run the targeted tests. Expected: FAIL because upload bypasses the registry and reset preserves registry state.

- [ ] **Step 3: Implement the minimal workflow reuse**

Allow scan_sources to derive canonical_title from a callback. Upload into a unique directory while preserving a sanitized original basename, scan it through the registry, append chunks, acknowledge indexed versions, and return the pending version metadata. Add reset to replace registry state with a deep copy of EMPTY_STATE and persist it.

- [ ] **Step 4: Verify GREEN**

Run targeted upload/reset tests and all registry/API tests. Expected: PASS.

### Task 4: Render searchable version timelines

**Files:**
- Modify: static/index.html
- Modify: static/app.js
- Modify: static/styles.css
- Test: tests/test_static_ui.py

**Interfaces:**
- Consumes: search response timelines array
- Produces: renderVersionTimelines(timelines, includeHistory)

- [ ] **Step 1: Write failing static UI tests**

Assert the page contains timeline result roots and app.js:

- calls renderVersionTimelines from renderResults
- uses version.download.source and version.download.source_pdf
- displays status, effective period, source file, and an unavailable state
- clears or hides stale timeline content on a latest-only search or error

- [ ] **Step 2: Verify RED**

Run the new static UI tests. Expected: FAIL because the timeline result surface is absent.

- [ ] **Step 3: Implement the minimal UI**

Add a hidden version timeline section below evidence results. Render regulation groups and newest-first version rows using escaped API data and apiUrl for links. Hide and clear the section when include_history is false or the request fails.

- [ ] **Step 4: Verify GREEN**

Run tests/test_static_ui.py and node --check static/app.js. Expected: PASS.

### Task 5: Exercise two real revision versions in browser E2E

**Files:**
- Modify: tests/e2e_smoke.py

**Interfaces:**
- Produces: synthetic HWPX revisions with actual parenthesized file naming
- Verifies: timeline ordering and both download links

- [ ] **Step 1: Extend the E2E fixture**

Create 2025 and 2026 revisions of the same synthetic regulation. Approve each using its detected effective_from value.

- [ ] **Step 2: Add timeline browser assertions**

Disable latest-only, run the search, assert two version rows appear newest first, and request both original download links successfully.

- [ ] **Step 3: Verify the E2E**

Run:

~~~sh
PYTHONDONTWRITEBYTECODE=1 /Users/leeseungbo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 tests/e2e_smoke.py
~~~

Expected: search, timeline downloads, desktop, and mobile checks pass with no browser errors.

### Task 6: Final verification and quality gates

**Files:**
- Review all changed source and test files.

- [ ] **Step 1: Run targeted and full verification**

Run registry, API, static UI, node syntax, full unittest discovery, browser E2E, and a real local regulation parsing/search smoke.

- [ ] **Step 2: Run ai-slop-cleaner on changed files**

Remove only behavior-neutral duplication or verbosity. Add no abstractions or dependencies.

- [ ] **Step 3: Rerun all verification**

The post-cleaner suite must match or exceed the pre-cleaner evidence.

- [ ] **Step 4: Run code review**

The final review must report recommendation APPROVE and architect status CLEAR. Resolve every blocking finding before completing the Goal.

- [ ] **Step 5: Complete and checkpoint Ultragoal**

Only after the quality gate is clean, mark the aggregate Codex Goal complete and checkpoint G001 with test, cleanup, and review evidence.

