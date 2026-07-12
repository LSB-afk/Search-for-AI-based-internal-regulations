# 천안도시공사 AI 내부규정 검색 시스템 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 프로토타입을 폐쇄망에서 실행되고 승인된 최신 규정과 개정 이력을 시계열로 보여주는 천안도시공사 내부규정 검색 시스템으로 고도화한다.

**Architecture:** Python 표준 라이브러리 기반의 `regulation_registry.py`가 규정 버전·스캔·감사 이벤트를 원자적 JSON 장부로 관리하고, 기존 `server.py`가 문서 파싱·검색·다운로드와 운영 API를 제공한다. 정적 프론트엔드는 한 페이지 안에서 통합 검색, 최신 규정, 업데이트 센터, 개정 이력, 문서함, 권한 시연, 운영 현황 화면을 전환하며 외부 API·CDN 없이 내부 서버에서 동작한다.

**Tech Stack:** Python 3.12, `http.server`, JSON, SHA-256, HTML5, CSS, vanilla JavaScript, Python `unittest`, Playwright smoke test

## Global Constraints

- 제품명은 `천안도시공사 AI 내부규정 검색 시스템`으로 통일한다.
- 운영 실행에는 인터넷 연결이 필요하지 않아야 한다.
- 외부 API, 외부 모델, CDN을 사용하지 않는다.
- 규정 원본과 본문 색인은 내부 디스크에만 저장한다.
- 실제 AD/LDAP/SSO 인증과 실제 운영 권한 강제는 이번 범위에서 제외한다.
- 권한 화면에는 `시연용 권한` 또는 `권한 시뮬레이션` 표시를 항상 노출한다.
- GitHub Pages는 코드·화면 시연용이며 실제 규정 검색 운영 경로로 사용하지 않는다.
- HWP/HWPX/PDF 원본과 `data/*.json`은 Git 커밋에서 제외한다.
- 새 런타임 의존성을 추가하지 않는다.

## File Structure

- Create `regulation_registry.py`: 버전 장부, 상태 전이, 최신본 판정, 감사 이벤트, 폴더 스캔 메타데이터를 담당한다.
- Create `tests/test_regulation_registry.py`: 버전 상태 전이, 중복 감지, 시점별 최신본 판정을 검증한다.
- Create `tests/test_server_api.py`: 임시 데이터 디렉터리에서 운영 API와 검색 최신본 필터를 검증한다.
- Modify `server.py`: 기존 파서·검색기에 버전 식별자를 연결하고 운영 API 및 감사 기록을 제공한다.
- Modify `static/index.html`: 제품명, 전역 내비게이션, 역할 전환, 업무 화면 컨테이너를 정의한다.
- Modify `static/app.js`: 화면 상태, 운영 API 호출, 역할별 내비게이션, 최신 규정·업데이트·이력·감사 화면을 렌더링한다.
- Modify `static/styles.css`: 폐쇄망 업무 콘솔의 밀도 높은 레이아웃, 상태 배지, 타임라인, 권한 계층, 반응형 화면을 정의한다.
- Modify `README.md`: 폐쇄망 실행, 폴더 스캔, 버전 승인, GitHub 시연판 구분을 문서화한다.
- Modify `.github/workflows/smoke.yml`: 레지스트리 및 API 단위 테스트를 CI에 추가한다.

---

### Task 1: 규정 버전 장부와 최신본 판정

**Files:**
- Create: `regulation_registry.py`
- Create: `tests/test_regulation_registry.py`

**Interfaces:**
- Consumes: `Path`, ISO 날짜 문자열, 파일 경로와 SHA-256 해시
- Produces: `RegulationRegistry(path: Path)`, `record_detection(canonical_title, source_path, content_hash, effective_from, chunk_ids, category, change_type) -> dict`, `approve_version(version_id, actor, effective_from, today) -> dict`, `reject_version(version_id, actor, reason) -> dict`, `versions(as_of: str | None, include_history: bool) -> list[dict]`, `events(limit: int = 100) -> list[dict]`

- [ ] **Step 1: 상태 전이와 최신본 판정의 실패 테스트 작성**

```python
# tests/test_regulation_registry.py
import tempfile
import unittest
from pathlib import Path

from regulation_registry import RegulationRegistry


class RegulationRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = RegulationRegistry(Path(self.tmp.name) / "registry.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_approval_supersedes_previous_version(self):
        old = self.registry.record_detection(
            canonical_title="인사규정",
            source_path="/closed/인사규정_2025.01.01.hwp",
            content_hash="old-hash",
            effective_from="2025-01-01",
            chunk_ids=["old-1"],
        )
        self.registry.approve_version(old["version_id"], "감사팀장", "2025-01-01")
        new = self.registry.record_detection(
            canonical_title="인사규정",
            source_path="/closed/인사규정_2026.05.27.hwp",
            content_hash="new-hash",
            effective_from="2026-05-27",
            chunk_ids=["new-1"],
        )
        self.registry.approve_version(new["version_id"], "감사팀장", "2026-05-27")

        current = self.registry.versions(as_of="2026-07-12", include_history=False)
        history = self.registry.versions(as_of="2026-07-12", include_history=True)
        self.assertEqual([item["version_id"] for item in current], [new["version_id"]])
        self.assertEqual({item["status"] for item in history}, {"approved", "superseded"})
        old_version = next(item for item in history if item["version_id"] == old["version_id"])
        self.assertEqual(old_version["effective_to"], "2026-05-26")

    def test_future_version_is_scheduled_until_effective_date(self):
        version = self.registry.record_detection(
            canonical_title="감사규정",
            source_path="/closed/감사규정_2027.01.01.hwp",
            content_hash="future-hash",
            effective_from="2027-01-01",
            chunk_ids=["future-1"],
        )
        approved = self.registry.approve_version(version["version_id"], "감사팀장", "2027-01-01", today="2026-07-12")
        self.assertEqual(approved["status"], "scheduled")
        self.assertEqual(self.registry.versions(as_of="2026-07-12", include_history=False), [])
        self.assertEqual(len(self.registry.versions(as_of="2027-01-01", include_history=False)), 1)

    def test_duplicate_hash_reuses_detected_version(self):
        first = self.registry.record_detection("회계규정", "/closed/a.hwp", "same", "2026-05-27", ["a"])
        second = self.registry.record_detection("회계규정", "/closed/b.hwp", "same", "2026-05-27", ["b"])
        self.assertEqual(first["version_id"], second["version_id"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트가 실패하는지 확인**

Run: `python3 -m unittest tests.test_regulation_registry -v`

Expected: `ModuleNotFoundError: No module named 'regulation_registry'`

- [ ] **Step 3: 원자적 JSON 장부와 상태 전이 구현**

```python
EMPTY_STATE = {
    "schema_version": 1,
    "regulations": {},
    "versions": {},
    "scan_runs": [],
    "events": [],
}


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
```

`RegulationRegistry`는 위 저장 함수를 사용하고 Interfaces에 명시된 메서드를 정확한 이름과 인자로 구현한다.

Implementation requirements:

- JSON top-level keys are `schema_version`, `regulations`, `versions`, `scan_runs`, `events`.
- Writes use `path.with_suffix(path.suffix + ".tmp")` followed by `replace`.
- `canonical_title` is NFC-normalized and strips trailing dates and extension labels.
- Version IDs and event IDs use `uuid.uuid4().hex`.
- `approve_version` sets future dates to `scheduled`; current or past dates to `approved`.
- Approving a newer version sets the previous approved version to `superseded` and its `effective_to` to one day before the new start date.
- Missing version IDs raise `KeyError`; invalid transitions raise `ValueError`.

- [ ] **Step 4: 단위 테스트 통과 확인**

Run: `python3 -m unittest tests.test_regulation_registry -v`

Expected: `Ran 3 tests` and `OK`

- [ ] **Step 5: 첫 구현 단위 커밋**

```bash
git add regulation_registry.py tests/test_regulation_registry.py
git commit -m "Preserve regulation history before selecting the current text" \
  -m "Constraint: The registry must work without a database or network service.\nConfidence: high\nScope-risk: moderate\nTested: python3 -m unittest tests.test_regulation_registry -v"
```

---

### Task 2: 폐쇄망 폴더 스캔과 변경 감지

**Files:**
- Modify: `regulation_registry.py`
- Modify: `tests/test_regulation_registry.py`
- Modify: `server.py`

**Interfaces:**
- Consumes: `server.local_sources() -> list[Path]`, `server.ingest_file(path) -> list[dict]`
- Produces: `sha256_file(path: Path) -> str`, `scan_sources(paths, ingest, registry) -> dict`, `ScanRun` summary with `new_count`, `changed_count`, `unchanged_count`, `error_count`

- [ ] **Step 1: 중복·변경·오류 스캔 실패 테스트 추가**

```python
def test_scan_detects_new_unchanged_and_changed_files(self):
    source = Path(self.tmp.name) / "인사규정_2026.05.27.hwp"
    source.write_bytes(b"version-one")
    first = self.registry.scan_sources([source], lambda path: ["chunk-v1"], effective_date=lambda path: "2026-05-27")
    second = self.registry.scan_sources([source], lambda path: ["chunk-v1"], effective_date=lambda path: "2026-05-27")
    source.write_bytes(b"version-two")
    third = self.registry.scan_sources([source], lambda path: ["chunk-v2"], effective_date=lambda path: "2026-06-01")
    self.assertEqual(first["new_count"], 1)
    self.assertEqual(second["unchanged_count"], 1)
    self.assertEqual(third["changed_count"], 1)
```

- [ ] **Step 2: 새 테스트 실패 확인**

Run: `python3 -m unittest tests.test_regulation_registry.RegulationRegistryTest.test_scan_detects_new_unchanged_and_changed_files -v`

Expected: FAIL with `AttributeError` for `scan_sources`

- [ ] **Step 3: 해시 스캔과 파싱 오류 격리 구현**

```python
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
```

`scan_sources`는 각 파일을 독립적으로 처리하고, 실패 파일은 `scan_error` 버전과 `RegulationVersionScanFailed` 이벤트를 남긴다. 성공한 `pending` 버전의 청크에는 `regulation_id`, `version_id`, `version_status`를 주입한 뒤 기존 색인에 저장할 수 있도록 전체 청크 객체를 반환한다.

- [ ] **Step 4: `server.ingest_local_sources`를 레지스트리 스캔으로 교체**

```python
REGISTRY_FILE = DATA_DIR / "regulation_registry.json"
REGISTRY = RegulationRegistry(REGISTRY_FILE)


def ingest_local_sources() -> dict[str, Any]:
    result = REGISTRY.scan_sources(
        local_sources(),
        ingest=lambda path: ingest_file(path),
        effective_date=lambda path: date_from_text(path.name),
    )
    if result["chunks"]:
        add_chunks(result.pop("chunks"))
    result["documents"] = document_summary()
    return result
```

- [ ] **Step 5: 스캔 테스트와 문법 검사**

Run: `python3 -m unittest tests.test_regulation_registry -v && python3 -m py_compile regulation_registry.py server.py`

Expected: all tests `OK`, compile exits `0`

- [ ] **Step 6: 스캔 단위 커밋**

```bash
git add regulation_registry.py server.py tests/test_regulation_registry.py
git commit -m "Detect local regulation changes without losing approved history" \
  -m "Constraint: Internal source folders remain read-only.\nConfidence: high\nScope-risk: moderate\nTested: registry unit tests and Python compile"
```

---

### Task 3: 운영 API와 최신본 검색 필터

**Files:**
- Create: `tests/test_server_api.py`
- Modify: `server.py`
- Modify: `regulation_registry.py`

**Interfaces:**
- Consumes: registry versions and events, existing `search_index`, `document_summary`
- Produces: `GET /api/dashboard`, `GET /api/versions`, `GET /api/events`, `POST /api/versions/approve`, `POST /api/versions/reject`, extended `POST /api/search`

- [ ] **Step 1: 최신본과 기준일 검색 실패 테스트 작성**

```python
# tests/test_server_api.py
import unittest

import server


class SearchVersionFilterTest(unittest.TestCase):
    def test_search_uses_only_version_ids_allowed_by_registry(self):
        chunks = [
            server.make_chunk(doc_title="인사규정", section_title="구버전", text="징계 구 기준", effective_from="2025-01-01"),
            server.make_chunk(doc_title="인사규정", section_title="최신본", text="징계 최신 기준", effective_from="2026-05-27"),
        ]
        chunks[0]["version_id"] = "old"
        chunks[1]["version_id"] = "new"
        result = server.search_chunks(chunks, "징계 기준", "employee", "2026-07-12", 6, allowed_version_ids={"new"})
        self.assertEqual([item["version_id"] for item in result["results"]], ["new"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 검색 테스트 실패 확인**

Run: `python3 -m unittest tests.test_server_api -v`

Expected: FAIL because `search_chunks` does not exist

- [ ] **Step 3: 검색 코어를 주입 가능한 함수로 분리**

기존 `search_index`의 점수 계산 본문을 `search_chunks`로 이동하고 다음 시그니처와 필터를 점수 계산 전에 넣는다.

```python
def search_chunks(
    chunks: list[dict[str, Any]],
    query: str,
    role: str,
    as_of: str | None,
    limit: int,
    allowed_version_ids: set[str] | None = None,
    include_history: bool = False,
) -> dict[str, Any]:
    explicit_or_detected_date = detect_date(query, as_of)
    query_terms = expand_terms(tokenize(query))
    idf = build_idf(chunks)
    real_source_available = any(chunk.get("source_type") != "sample" for chunk in chunks)
    blocked_count = 0
    date_filtered_count = 0
    scored: list[dict[str, Any]] = []

    for chunk in chunks:
        version_id = chunk.get("version_id")
        if version_id and allowed_version_ids is not None and version_id not in allowed_version_ids:
            date_filtered_count += 1
            continue
        # 이 지점 아래에 기존 접근권한, 시행일, 점수 계산, 정렬, 응답 조립 코드를 이동한다.
```

```python
version_id = chunk.get("version_id")
if version_id and allowed_version_ids is not None and version_id not in allowed_version_ids:
    date_filtered_count += 1
    continue
```

`search_index`는 `REGISTRY.versions(as_of, include_history)`에서 허용 버전 ID를 구하고 `search_chunks`를 호출한다. 샘플 청크처럼 `version_id`가 없는 데이터는 회귀 시연을 위해 허용한다.

- [ ] **Step 4: 운영 API 라우트 구현**

```python
# GET 응답 계약
GET /api/dashboard -> {"total_regulations": int, "current_count": int, "pending_count": int, "error_count": int, "last_scan": dict | None, "offline": True}
GET /api/versions?status=pending&regulation_id={regulation_id} -> {"versions": list}
GET /api/events?limit=100 -> {"events": list}

# POST 요청 계약
POST /api/versions/approve {"version_id": str, "effective_from": "YYYY-MM-DD", "actor": "감사팀장"}
POST /api/versions/reject {"version_id": str, "reason": str, "actor": "감사팀장"}
POST /api/search adds {"include_history": bool}
```

승인·반려 요청은 `actor` 기본값을 `감사팀장(시연)`으로 설정하고 응답에 `simulation: true`를 포함한다.

- [ ] **Step 5: API·검색 테스트와 전체 컴파일**

Run: `python3 -m unittest discover -s tests -v && python3 -m py_compile regulation_registry.py server.py`

Expected: all tests `OK`, compile exits `0`

- [ ] **Step 6: 운영 API 단위 커밋**

```bash
git add regulation_registry.py server.py tests/test_server_api.py
git commit -m "Make approved effective versions the default search corpus" \
  -m "Constraint: Sample data remains searchable before the first local approval.\nConfidence: high\nScope-risk: broad\nTested: all unittest cases and Python compile"
```

---

### Task 4: 제품 셸과 역할별 화면 전환

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`
- Modify: `static/styles.css`

**Interfaces:**
- Consumes: existing search API and new dashboard API
- Produces: `state.activeView`, `state.actorRole`, `renderShell()`, `renderActiveView()`, role-aware navigation

- [ ] **Step 1: 제품명과 전역 내비게이션 마크업 추가**

```html
<header class="app-header">
  <div class="brand-block">
    <span class="network-mark"><span class="dot ok"></span>폐쇄망 운영</span>
    <h1>천안도시공사 AI 내부규정 검색 시스템</h1>
  </div>
  <div class="identity-switcher">
    <span class="simulation-badge">시연용 권한</span>
    <select id="actor-role" aria-label="시연 사용자 권한">
      <option value="audit_lead">감사팀장</option>
      <option value="auditor">감사담당자</option>
      <option value="department_head">부서장</option>
      <option value="employee">일반직원</option>
    </select>
  </div>
</header>
<nav id="primary-nav" class="primary-nav" aria-label="주요 업무"></nav>
<main id="view-root" class="view-root"></main>
```

기존 검색, 문서함, 항목별 화면은 삭제하지 않고 각각 `data-view="search"`, `data-view="library"` 컨테이너로 이동한다.

- [ ] **Step 2: 역할별 메뉴 계약 구현**

```javascript
const ROLE_VIEWS = {
  audit_lead: ["search", "latest", "updates", "history", "library", "permissions", "operations"],
  auditor: ["search", "latest", "updates", "history", "library", "operations"],
  department_head: ["search", "latest", "history", "library"],
  employee: ["search", "latest", "library"],
};

const VIEW_LABELS = {
  search: "통합 검색",
  latest: "최신 규정",
  updates: "업데이트 센터",
  history: "개정 이력",
  library: "규정 문서함",
  permissions: "권한 관리",
  operations: "운영 현황",
};
```

역할 변경 시 허용되지 않은 현재 화면이면 `search`로 이동하고, `RoleSimulationChanged`를 로컬 화면 이벤트 목록에 추가한다.

- [ ] **Step 3: 폐쇄망 업무 콘솔 스타일 적용**

색상 토큰은 다음 값으로 고정한다.

```css
:root {
  --canvas: #eef1ed;
  --surface: #ffffff;
  --ink: #17201c;
  --muted: #66726c;
  --line: #cbd3ce;
  --corporate-green: #176b52;
  --signal-blue: #315b78;
  --warning: #a56b18;
  --danger: #a13f48;
}
```

서명 요소는 헤더 아래의 `규정 동기화 레일`이다. 마지막 스캔, 검토 대기, 최신 시행일을 한 줄에 표시하며 장식이 아닌 실제 상태를 전달한다.

- [ ] **Step 4: 정적 문법과 접근성 기본 검사**

Run: `node --check static/app.js && python3 -m py_compile server.py`

Expected: both commands exit `0`

- [ ] **Step 5: 제품 셸 커밋**

```bash
git add static/index.html static/app.js static/styles.css
git commit -m "Center the interface on Cheonan's offline regulation workflow" \
  -m "Constraint: Role switching is visibly marked as a simulation.\nConfidence: high\nScope-risk: moderate\nTested: JavaScript syntax and Python compile"
```

---

### Task 5: 최신 규정·업데이트·이력·권한·운영 화면

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`
- Modify: `static/styles.css`

**Interfaces:**
- Consumes: `/api/dashboard`, `/api/versions`, `/api/events`, `/api/ingest-local`, `/api/versions/approve`, `/api/versions/reject`
- Produces: `refreshOperations()`, `renderLatestView()`, `renderUpdatesView()`, `renderHistoryView()`, `renderPermissionsView()`, `renderOperationsView()`

- [ ] **Step 1: 운영 데이터 로딩과 상태 모델 구현**

```javascript
Object.assign(state, {
  activeView: "search",
  actorRole: "audit_lead",
  dashboard: null,
  versions: [],
  events: [],
  selectedRegulationId: null,
});

async function refreshOperations() {
  const [dashboard, versions, events] = await Promise.all([
    api("/api/dashboard"),
    api("/api/versions"),
    api("/api/events?limit=100"),
  ]);
  state.dashboard = dashboard;
  state.versions = versions.versions || [];
  state.events = events.events || [];
  renderShell();
  renderActiveView();
}
```

- [ ] **Step 2: 최신 규정 화면 구현**

`approved`와 기준일이 도래한 `scheduled`만 최신 목록에 포함하고 `effective_from` 내림차순으로 표시한다. 각 행은 규정명, 시행일, 상태, 버전 수, 원본 검색 버튼을 제공한다.

- [ ] **Step 3: 업데이트 센터 승인·반려 흐름 구현**

```javascript
async function approveVersion(versionId, effectiveFrom) {
  await api("/api/versions/approve", {
    method: "POST",
    body: JSON.stringify({ version_id: versionId, effective_from: effectiveFrom, actor: "감사팀장(시연)" }),
  });
  showToast("최신 규정으로 승인했습니다. 시연용 상태 변경입니다.");
  await refreshOperations();
}

async function rejectVersion(versionId) {
  await api("/api/versions/reject", {
    method: "POST",
    body: JSON.stringify({ version_id: versionId, reason: "시행일 또는 규정명 재확인", actor: "감사팀장(시연)" }),
  });
  showToast("검토 항목을 반려했습니다.");
  await refreshOperations();
}
```

감사팀장 이외 역할에서는 승인·반려 버튼을 렌더링하지 않는다.

- [ ] **Step 4: 개정 이력 타임라인 구현**

같은 `regulation_id`의 버전을 `effective_from` 내림차순으로 정렬하고 현재본, 시행 예정, 이전 버전, 반려, 오류 상태를 색상과 텍스트로 함께 표시한다. 해시는 앞 10자리만 표시하되 전체 값은 `title` 속성에 넣는다.

- [ ] **Step 5: 권한 계층과 운영 현황 구현**

권한 화면은 감사팀장 → 감사담당자/부서장 → 일반직원 계층을 보여주고 각 역할 카드에 허용 화면을 나열한다. 운영 현황은 전체 규정, 최신본, 검토 대기, 오류, 마지막 스캔을 표시하고 아래에 감사 이벤트 표를 렌더링한다.

- [ ] **Step 6: 역할별 잠금 표시와 검색 옵션 연결**

검색 화면에 `최신본만` 기본 체크박스를 추가한다. 체크를 해제하면 요청 본문의 `include_history`를 `true`로 전송한다. 일반직원에게 제한된 문서는 본문과 다운로드 대신 자물쇠 아이콘, 규정명, `상위 권한 필요` 문구를 표시한다.

- [ ] **Step 7: 프론트엔드 문법 검사**

Run: `node --check static/app.js`

Expected: exit `0`

- [ ] **Step 8: 업무 화면 커밋**

```bash
git add static/index.html static/app.js static/styles.css
git commit -m "Expose regulation updates as a reviewable timeline" \
  -m "Constraint: Approval and role behavior are demonstrative, not security controls.\nConfidence: high\nScope-risk: broad\nTested: JavaScript syntax check"
```

---

### Task 6: 감사 이벤트와 다운로드 추적

**Files:**
- Modify: `server.py`
- Modify: `regulation_registry.py`
- Modify: `tests/test_server_api.py`

**Interfaces:**
- Consumes: search requests, source download requests, registry event writer
- Produces: `SearchExecuted`, `SourceDownloaded`, `SourceDownloadFailed` events without storing document bodies or full query text

- [ ] **Step 1: 감사 이벤트 비식별 테스트 추가**

```python
def test_search_audit_event_does_not_store_full_query(self):
    event = server.search_audit_payload("개인정보가 포함될 수 있는 긴 질의", "employee", "2026-07-12", 3)
    self.assertNotIn("개인정보가 포함될 수 있는 긴 질의", event["summary"])
    self.assertEqual(event["metadata"]["query_length"], 19)
    self.assertEqual(event["metadata"]["result_count"], 3)
```

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_server_api.SearchVersionFilterTest.test_search_audit_event_does_not_store_full_query -v`

Expected: FAIL because `search_audit_payload` does not exist

- [ ] **Step 3: 검색·다운로드 이벤트 기록 구현**

```python
def search_audit_payload(query: str, role: str, as_of: str | None, result_count: int) -> dict[str, Any]:
    return {
        "summary": f"{ROLE_LABEL.get(role, role)} 권한으로 규정 검색 실행",
        "metadata": {
            "query_length": len(query),
            "as_of": as_of,
            "result_count": result_count,
        },
    }
```

`POST /api/search` 성공 후 `SearchExecuted`를 기록한다. 원본 다운로드 성공·실패는 문서 본문 없이 `source_file`, `version_id`, 결과만 기록한다.

- [ ] **Step 4: 전체 테스트 실행**

Run: `python3 -m unittest discover -s tests -v && python3 -m py_compile regulation_registry.py server.py && node --check static/app.js`

Expected: all tests `OK`, all syntax checks exit `0`

- [ ] **Step 5: 감사 이벤트 커밋**

```bash
git add regulation_registry.py server.py tests/test_server_api.py
git commit -m "Record review actions without retaining regulation text or full queries" \
  -m "Constraint: Audit logs retain identifiers and outcomes only.\nConfidence: high\nScope-risk: moderate\nTested: all unit tests and syntax checks"
```

---

### Task 7: 폐쇄망 실행 문서, E2E 검증, 배포

**Files:**
- Modify: `README.md`
- Modify: `.github/workflows/smoke.yml`
- Create: `tests/e2e_smoke.py`

**Interfaces:**
- Consumes: complete local application at `http://127.0.0.1:8765`
- Produces: reproducible offline runbook and desktop/mobile browser evidence

- [ ] **Step 1: CI에 단위 테스트 추가**

```yaml
- name: Unit tests
  run: python -m unittest discover -s tests -v
- name: Compile
  run: python -m py_compile regulation_registry.py server.py
```

- [ ] **Step 2: README 폐쇄망 운영 절차 작성**

문서에 다음 실제 명령을 포함한다.

```bash
python3 -m pip install --no-index --find-links ./wheelhouse -r requirements.txt
REG_RAG_SOURCE_DIRS="/srv/cheonan/regulations" \
REG_RAG_AUTO_INGEST=1 \
python3 server.py --host 0.0.0.0 --port 8765
```

GitHub Pages는 시연판이며 내부 원문을 제공하지 않는다는 설명과, 운영판은 내부 서버에서만 실행한다는 설명을 서로 다른 절로 분리한다.

- [ ] **Step 3: Playwright E2E 스크립트 작성**

```python
# tests/e2e_smoke.py
from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")
    assert "천안도시공사 AI 내부규정 검색 시스템" in page.locator("h1").inner_text()
    assert page.get_by_text("시연용 권한").is_visible()
    page.get_by_role("button", name="업데이트 센터").click()
    assert page.get_by_text("검토 대기").first.is_visible()
    page.screenshot(path="/tmp/cheonan-regulation-desktop.png", full_page=True)
    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile.goto("http://127.0.0.1:8765", wait_until="networkidle")
    mobile.screenshot(path="/tmp/cheonan-regulation-mobile.png", full_page=True)
    browser.close()
```

- [ ] **Step 4: 전체 서버와 브라우저 검증 실행**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile regulation_registry.py server.py
node --check static/app.js
python3 server.py --host 127.0.0.1 --port 8765
python3 tests/e2e_smoke.py
```

Expected:

- 모든 단위 테스트 `OK`
- Python·JavaScript 문법 검사 종료 코드 `0`
- `/api/health`가 `ok: true`, `offline: true` 반환
- 데스크톱과 모바일 스크린샷에 빈 화면, 겹침, 잘린 버튼이 없음
- 역할 전환, 업데이트 센터, 최신 규정, 개정 이력, 검색, 원본 다운로드가 동작

- [ ] **Step 5: 최종 문서와 검증 커밋**

```bash
git add README.md .github/workflows/smoke.yml tests/e2e_smoke.py
git commit -m "Make the offline deployment reproducible and verifiable" \
  -m "Constraint: Runtime verification cannot depend on public network access.\nConfidence: high\nScope-risk: narrow\nTested: unit, compile, JavaScript, API, desktop, and mobile smoke checks"
```

- [ ] **Step 6: 원격 배포 전 최종 상태 확인**

Run: `git status --short --branch && git log -8 --oneline`

Expected: clean `main` branch with the seven implementation commits after the approved design commit.

- [ ] **Step 7: GitHub에 코드와 시연판 배포**

Run: `git push origin main`

Expected: `smoke`, `docker-publish`, `pages` workflows complete successfully; no HWP/HWPX/PDF or `data/*.json` appears in the pushed commit.
