const state = {
  activeView: "search",
  actorRole: "employee",
  role: "employee",
  documents: [],
  dashboard: null,
  versions: [],
  events: [],
  selectedRegulationId: null,
  operationOffline: false,
  localEvents: [],
  lastHealth: null,
  activeCategory: "all",
  documentFilter: "",
};

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

const ACTOR_API_ROLES = {
  audit_lead: "admin",
  auditor: "audit",
  department_head: "employee",
  employee: "employee",
};

const ACTOR_LABELS = {
  audit_lead: "감사팀장",
  auditor: "감사담당자",
  department_head: "부서장",
  employee: "일반직원",
};

const REGULATION_TYPES = new Set(["hwp", "hwpx", "pdf"]);
const CATEGORY_FALLBACK_ID = "operations";
const STATUS_LABELS = {
  approved: "현재본",
  scheduled: "시행 예정",
  superseded: "이전 버전",
  rejected: "반려",
  scan_error: "오류",
  detected: "검토 대기",
  pending: "검토 대기",
};
const ROLE_PERMISSIONS = {
  audit_lead: ["search", "latest", "updates", "history", "library", "permissions", "operations"],
  auditor: ["search", "latest", "updates", "history", "library", "operations"],
  department_head: ["search", "latest", "history", "library"],
  employee: ["search", "latest", "library"],
};
const CATEGORIES = [
  {
    id: "all",
    label: "전체",
    title: "전체 규정",
    kicker: "All regulations",
    keywords: [],
    queries: ["정관 사업 범위", "인사규정 징계", "계약심의위원회 심의대상"],
  },
  {
    id: "charter",
    label: "정관·기본",
    title: "정관·기본 규정",
    kicker: "Charter and governance",
    keywords: ["정관", "규정관리", "제규정", "직제", "이사회"],
    queries: ["정관 사업 범위", "이사회 의결사항", "규정 개정 절차"],
  },
  {
    id: "people",
    label: "인사·복무",
    title: "인사·복무 규정",
    kicker: "People and service",
    keywords: ["인사", "복무", "근무", "근로자", "임원", "직원", "평정", "징계", "채용", "공무직", "보수", "여비"],
    queries: ["인사규정 징계", "유연근무제 신청", "공무직 근로자 관리"],
  },
  {
    id: "audit",
    label: "감사·윤리",
    title: "감사·윤리 규정",
    kicker: "Audit and conduct",
    keywords: ["감사", "윤리", "청렴", "행동강령", "부패", "성희롱", "고충", "갑질"],
    queries: ["감사자료 제출 권한", "행동강령 이해충돌", "성희롱 고충심의위원회"],
  },
  {
    id: "finance",
    label: "회계·계약",
    title: "회계·계약 규정",
    kicker: "Finance and contracts",
    keywords: ["회계", "계약", "예산", "입찰", "수입", "지출", "재무", "자금", "물품", "구매"],
    queries: ["계약심의위원회 심의대상", "예산 집행 절차", "물품 구매 계약"],
  },
  {
    id: "security",
    label: "보안·정보",
    title: "보안·정보 규정",
    kicker: "Security and records",
    keywords: ["보안", "정보", "개인정보", "전산", "기록물", "문서", "공공데이터"],
    queries: ["보안업무규정 열람", "개인정보 보호 책임", "기록물 관리 절차"],
  },
  {
    id: "safety",
    label: "안전·재난",
    title: "안전·재난 규정",
    kicker: "Safety and continuity",
    keywords: ["안전", "재난", "위험", "중대재해", "보건", "시설", "비상", "재해"],
    queries: ["재난 상황 지침", "중대재해 안전보건", "시설 안전 관리"],
  },
  {
    id: "operations",
    label: "사무·운영",
    title: "사무·운영 규정",
    kicker: "Office operations",
    keywords: ["사무", "관리", "민원", "홍보", "위임", "전결", "운영", "위원회", "자문", "소송", "업무"],
    queries: ["사무관리규정 문서", "위임전결 사항", "위원회 운영"],
  },
  {
    id: "welfare",
    label: "복지·교육",
    title: "복지·교육 규정",
    kicker: "Welfare and learning",
    keywords: ["복리", "복지", "후생", "동호회", "휴가", "교육훈련", "교육"],
    queries: ["복리후생 지원", "교육훈련 대상", "휴가 기준"],
  },
];

const isGitHubPages = window.location.hostname.endsWith("github.io");
const params = new URLSearchParams(window.location.search);
const defaultApiBase = isGitHubPages ? "http://127.0.0.1:8765" : "";
const apiBase = String(params.get("api") || window.REG_RAG_API_BASE || defaultApiBase).replace(/\/$/, "");
const shouldUseLoopbackTarget = isLoopbackApi(apiBase) && supportsLoopbackTargetAddress();

const qs = (selector) => document.querySelector(selector);
const qsa = (selector) => Array.from(document.querySelectorAll(selector));

function isLoopbackApi(base) {
  if (!base) return false;
  try {
    const hostname = new URL(base).hostname;
    return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
  } catch (error) {
    return false;
  }
}

function supportsLoopbackTargetAddress() {
  if (typeof Request === "undefined") return false;
  try {
    return new Request("http://127.0.0.1", { targetAddressSpace: "loopback" }).targetAddressSpace === "loopback";
  } catch (error) {
    return false;
  }
}

function apiFailureMessage() {
  if (isGitHubPages && isLoopbackApi(apiBase)) {
    return "로컬 검색 서버에 연결하지 못했습니다. 127.0.0.1:8765 서버를 실행하고, 브라우저가 묻는 로컬 네트워크 접근 권한을 허용하세요.";
  }
  return "검색 서버 연결에 실패했습니다.";
}

function showToast(message) {
  const toast = qs("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.setTimeout(() => toast.classList.remove("visible"), 2600);
}

async function api(path, options = {}) {
  const url = apiBase ? `${apiBase}${path}` : path;
  const fetchOptions = {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  };
  if (shouldUseLoopbackTarget) {
    fetchOptions.targetAddressSpace = "loopback";
  }
  const response = await fetch(url, fetchOptions);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function apiUrl(path) {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;
  return apiBase ? `${apiBase}${path}` : path;
}

function permissionLabel(permission) {
  return { public: "공개", internal: "내부", audit: "감사", admin: "관리자" }[permission] || permission;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function normalizeText(value) {
  return String(value ?? "").normalize("NFC");
}

function selectorValue(value) {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(String(value));
  }
  return String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

function sourceTypes(doc) {
  return (doc.source_types || []).map((type) => String(type).toLowerCase());
}

function documentTitle(doc) {
  return normalizeText(doc.doc_title || "문서");
}

function regulationDocuments() {
  return state.documents.filter((doc) => {
    const types = sourceTypes(doc);
    const title = documentTitle(doc);
    return types.some((type) => REGULATION_TYPES.has(type)) || /규정|내규|정관|지침/.test(title);
  });
}

function categoryById(categoryId) {
  return CATEGORIES.find((category) => category.id === categoryId) || CATEGORIES[0];
}

function categoryForDocument(doc) {
  const title = documentTitle(doc);
  const matched = CATEGORIES.find(
    (category) => category.id !== "all" && category.keywords.some((keyword) => title.includes(keyword)),
  );
  return matched || categoryById(CATEGORY_FALLBACK_ID);
}

function documentsForCategory(categoryId) {
  const docs = regulationDocuments();
  if (categoryId === "all") return docs;
  return docs.filter((doc) => categoryForDocument(doc).id === categoryId);
}

function filteredHarnessDocuments() {
  const filter = normalizeText(state.documentFilter).trim();
  const docs = regulationDocuments();
  if (!filter) return docs;
  return docs.filter((doc) => documentTitle(doc).includes(filter));
}

function totalChunks(docs) {
  return docs.reduce((sum, doc) => sum + Number(doc.chunk_count || 0), 0);
}

function latestEffectiveFrom(docs) {
  const dates = docs.map((doc) => doc.effective_from).filter(Boolean).sort();
  return dates.length ? dates[dates.length - 1] : "시행일 미상";
}

function operationAsOf() {
  return qs("#as-of")?.value || new Date().toISOString().slice(0, 10);
}

function versionTitle(version) {
  return normalizeText(version.canonical_title || version.doc_title || "규정명 미상");
}

function versionStatusLabel(version) {
  return STATUS_LABELS[version.status] || version.status || "상태 미상";
}

function versionsByRegulation() {
  const groups = new Map();
  state.versions.forEach((version) => {
    const key = version.regulation_id || versionTitle(version);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(version);
  });
  return groups;
}

function sortedVersions(versions) {
  return [...versions].sort((a, b) => {
    const dateCompare = String(b.effective_from || "").localeCompare(String(a.effective_from || ""));
    if (dateCompare) return dateCompare;
    return String(b.version_id || "").localeCompare(String(a.version_id || ""));
  });
}

function latestCurrentVersions() {
  const asOf = operationAsOf();
  const rows = [];
  versionsByRegulation().forEach((versions) => {
    const eligible = versions.filter((version) => {
      if (version.status === "approved") return true;
      return version.status === "scheduled" && String(version.effective_from || "") <= asOf;
    });
    if (!eligible.length) return;
    const latest = sortedVersions(eligible)[0];
    rows.push({ ...latest, version_count: versions.length });
  });
  return rows.sort((a, b) => String(b.effective_from || "").localeCompare(String(a.effective_from || "")));
}

function pendingVersions() {
  return sortedVersions(state.versions.filter((version) => ["detected", "pending"].includes(version.status)));
}

function scanErrorVersions() {
  return sortedVersions(state.versions.filter((version) => version.status === "scan_error"));
}

function offlineNotice() {
  if (!state.operationOffline) return "";
  return `
    <div class="offline-state" role="status">
      <strong>로컬 운영 서버 연결 필요</strong>
      <span>GitHub Pages 또는 정적 파일에서는 화면을 유지하며, 127.0.0.1:8765 API 연결 후 최신 운영 데이터를 불러옵니다.</span>
    </div>
  `;
}

function formatScanTime(scan) {
  const value = scan && (scan.finished_at || scan.started_at);
  if (!value) return "스캔 기록 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function roleViews() {
  return ROLE_VIEWS[state.actorRole] || ROLE_VIEWS.employee;
}

function setActiveView(viewId) {
  state.activeView = roleViews().includes(viewId) ? viewId : "search";
  renderActiveView();
}

function permissionSummary(docs) {
  const permissions = new Set(docs.flatMap((doc) => doc.permissions || []));
  if (!permissions.size) return "권한 없음";
  return Array.from(permissions)
    .sort((a, b) => permissionLabel(a).localeCompare(permissionLabel(b), "ko"))
    .map(permissionLabel)
    .join(", ");
}

function renderShell() {
  const actorSelect = qs("#actor-role");
  if (actorSelect && actorSelect.value !== state.actorRole) {
    actorSelect.value = state.actorRole;
  }
  const nav = qs("#primary-nav");
  nav.innerHTML = roleViews()
    .map((viewId) => {
      const isActive = viewId === state.activeView;
      return `
        <button class="nav-tab${isActive ? " active" : ""}" type="button" data-view-target="${viewId}" aria-current="${isActive ? "page" : "false"}">
          ${escapeHtml(VIEW_LABELS[viewId])}
        </button>
      `;
    })
    .join("");
}

function renderActiveView() {
  if (!roleViews().includes(state.activeView)) {
    state.activeView = "search";
  }
  renderShell();
  qsa("[data-view]").forEach((section) => {
    section.hidden = section.dataset.view !== state.activeView;
  });
  renderLatestView();
  renderUpdatesView();
  renderHistoryView();
  renderPermissionsView();
  renderOperationsView();
}

function statusCard(label, value, detail = "") {
  return `
    <article class="status-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      ${detail ? `<p>${escapeHtml(detail)}</p>` : ""}
    </article>
  `;
}

function renderLatestView() {
  const rows = latestCurrentVersions();
  const root = qs("#latest-view");
  const cards = [
    statusCard("최신본", `${rows.length}건`, "승인본과 기준일이 도래한 시행 예정본"),
    statusCard("시행 기준일", operationAsOf(), "통합 검색 기준일과 동일하게 표시"),
    statusCard("권한 기준", ACTOR_LABELS[state.actorRole], "시연용 권한으로 검색 범위가 조정됩니다."),
  ];
  const list = rows.length
    ? `
      <div class="operation-table">
        ${rows
          .map(
            (version) => `
              <article class="operation-row">
                <div class="operation-main">
                  <strong>${escapeHtml(versionTitle(version))}</strong>
                  <span>시행일 ${escapeHtml(version.effective_from || "시행일 미상")}</span>
                </div>
                <span class="status-pill ${escapeHtml(version.status || "")}">${escapeHtml(versionStatusLabel(version))}</span>
                <span>${Number(version.version_count || 1)}개 버전</span>
                <button class="mini-action" data-query="${escapeHtml(versionTitle(version))}" type="button">원본 검색</button>
              </article>
            `,
          )
          .join("")}
      </div>
    `
    : `<div class="empty-state">표시할 최신 규정이 없습니다.</div>`;
  root.innerHTML = `${offlineNotice()}${cards.join("")}${list}`;
}

function renderUpdatesView() {
  const root = qs("#updates-view");
  const pending = pendingVersions();
  const errors = scanErrorVersions();
  const canReview = state.actorRole === "audit_lead";
  const cards = [
    statusCard("검토 대기", `${pending.length}건`, "스캔 후 승인 전 규정"),
    statusCard("오류", `${errors.length}건`, "재처리 또는 원본 확인 필요"),
    statusCard("검토 권한", canReview ? "감사팀장" : "조회 전용", "모든 역할 표시는 시연용입니다."),
  ];
  const pendingList = pending.length
    ? pending
        .map((version) => {
          const effectiveFrom = version.effective_from || operationAsOf();
          const controls = canReview
            ? `
              <div class="review-actions">
                <input class="date-input compact-date" type="date" value="${escapeHtml(effectiveFrom)}" data-effective-for="${escapeHtml(version.version_id)}" aria-label="승인 시행일" />
                <button class="mini-action" type="button" data-approve-version="${escapeHtml(version.version_id)}">승인</button>
                <button class="mini-action danger-action" type="button" data-reject-version="${escapeHtml(version.version_id)}">반려</button>
              </div>
            `
            : `<span class="simulation-note">시연용 조회 역할: 승인·반려 동작 없음</span>`;
          return `
            <article class="review-row">
              <div class="operation-main">
                <strong>${escapeHtml(versionTitle(version))}</strong>
                <span>${escapeHtml(version.source_path || "원본 경로 미상")}</span>
              </div>
              <span class="status-pill ${escapeHtml(version.status || "")}">${escapeHtml(versionStatusLabel(version))}</span>
              ${controls}
            </article>
          `;
        })
        .join("")
    : `<div class="empty-state">검토 대기 항목이 없습니다.</div>`;
  const errorList = errors.length
    ? `
      <div class="operation-table">
        ${errors
          .map(
            (version) => `
              <article class="operation-row">
                <div class="operation-main">
                  <strong>${escapeHtml(versionTitle(version))}</strong>
                  <span>${escapeHtml(version.source_path || "오류 원본 미상")}</span>
                </div>
                <span class="status-pill scan_error">오류</span>
              </article>
            `,
          )
          .join("")}
      </div>
    `
    : "";
  root.innerHTML = `${offlineNotice()}${cards.join("")}<div class="review-list">${pendingList}</div>${errorList}`;
}

function renderHistoryView() {
  const root = qs("#history-view");
  const groups = versionsByRegulation();
  const regulationIds = Array.from(groups.keys()).sort((a, b) => versionTitle(groups.get(a)[0]).localeCompare(versionTitle(groups.get(b)[0]), "ko"));
  if (!regulationIds.length) {
    root.innerHTML = `${offlineNotice()}<div class="empty-state">개정 이력이 없습니다.</div>`;
    return;
  }
  if (!state.selectedRegulationId || !groups.has(state.selectedRegulationId)) {
    state.selectedRegulationId = regulationIds[0];
  }
  const selector = `
    <div class="history-selector">
      ${regulationIds
        .map((regulationId) => {
          const selected = regulationId === state.selectedRegulationId;
          return `<button class="category-tab${selected ? " active" : ""}" type="button" data-history-regulation="${escapeHtml(regulationId)}">${escapeHtml(versionTitle(groups.get(regulationId)[0]))}</button>`;
        })
        .join("")}
    </div>
  `;
  const timeline = sortedVersions(groups.get(state.selectedRegulationId)).map((version) => {
    const hash = version.content_hash || "";
    return `
      <article class="timeline-item ${escapeHtml(version.status || "")}">
        <div class="timeline-stem"></div>
        <div class="timeline-content">
          <div class="result-title">
            <div>
              <strong>${escapeHtml(versionTitle(version))}</strong>
              <span>${escapeHtml(version.effective_from || "시행일 미상")} ${version.effective_to ? `~ ${escapeHtml(version.effective_to)}` : ""}</span>
            </div>
            <span class="status-pill ${escapeHtml(version.status || "")}">${escapeHtml(versionStatusLabel(version))}</span>
          </div>
          <div class="meta-row">
            <span class="badge" title="${escapeHtml(version.content_hash || "")}">${escapeHtml(hash ? hash.slice(0, 10) : "hash 없음")}</span>
            <span class="badge">${escapeHtml(version.change_type || "변경 유형 미상")}</span>
            <span class="badge">${escapeHtml(version.version_id || "version 없음")}</span>
          </div>
        </div>
      </article>
    `;
  });
  root.innerHTML = `${offlineNotice()}${statusCard("개정 이력", `${regulationIds.length}개 규정`, "현재본, 시행 예정, 이전 버전, 반려, 오류 상태를 함께 표시합니다.")}${selector}<div class="timeline">${timeline.join("")}</div>`;
}

function renderPermissionsView() {
  const root = qs("#permissions-view");
  const hierarchy = ["audit_lead", "auditor", "department_head", "employee"];
  root.innerHTML = `
    ${offlineNotice()}
    <div class="permission-tree" aria-label="권한 계층">
      ${hierarchy
        .map(
          (role, index) => `
            <article class="permission-level level-${index}">
              <div>
                <strong>${escapeHtml(ACTOR_LABELS[role])}</strong>
                <span>권한 시뮬레이션</span>
              </div>
              <p>허용 화면</p>
              <div class="meta-row">
                ${(ROLE_PERMISSIONS[role] || []).map((view) => `<span class="badge">${escapeHtml(VIEW_LABELS[view])}</span>`).join("")}
              </div>
            </article>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderOperationsView() {
  const dashboard = state.dashboard || {};
  const regs = regulationDocuments();
  const pending = Number(dashboard.pending_count || 0);
  const errorCount = Number(dashboard.error_count || 0);
  const lastScan = formatScanTime(dashboard.last_scan);
  const currentCount = String(dashboard.current_count ?? regs.length);
  const totalRegulations = String(dashboard.total_regulations ?? regs.length);
  const events = [...state.localEvents, ...state.events].slice(0, 100);
  const eventRows = events.length
    ? events
        .map(
          (event) => `
            <tr>
              <td>${escapeHtml(event.occurred_at || event.created_at || "시각 미상")}</td>
              <td>${escapeHtml(event.event_type || "감사 이벤트")}</td>
              <td>${escapeHtml(event.version_id || event.actor_role || "")}</td>
            </tr>
          `,
        )
        .join("")
    : `<tr><td colspan="3">감사 이벤트가 없습니다.</td></tr>`;
  qs("#operations-view").innerHTML = `
    ${offlineNotice()}
    ${[
      statusCard("전체 규정", `${totalRegulations}건`, "레지스트리 기준"),
      statusCard("최신본", `${currentCount}건`, "현재 검색 기본 범위"),
      statusCard("검토 대기", `${pending}건`, "승인 전 항목"),
      statusCard("오류", `${errorCount}건`, "스캔 실패 또는 원본 확인 필요"),
      statusCard("마지막 스캔", lastScan, "폐쇄망 폴더 기준"),
      statusCard("검색 서버", state.lastHealth ? `${state.lastHealth.chunks} chunks` : "연결 확인 중", "로컬 API 상태"),
    ].join("")}
    <section class="audit-events">
      <h3>감사 이벤트</h3>
      <table>
        <thead><tr><th>시각</th><th>이벤트</th><th>대상</th></tr></thead>
        <tbody>${eventRows}</tbody>
      </table>
    </section>
  `;
}

function renderDashboardViews() {
  renderLatestView();
  renderUpdatesView();
  renderHistoryView();
  renderPermissionsView();
  renderOperationsView();
}

function renderSyncRail() {
  const dashboard = state.dashboard || {};
  const regs = regulationDocuments();
  qs("#closed-network-state").textContent = dashboard.offline === false ? "폐쇄망 상태 확인 필요" : "폐쇄망 운영 중";
  qs("#last-scan").textContent = formatScanTime(dashboard.last_scan);
  qs("#pending-count").textContent = `${Number(dashboard.pending_count || 0)}건`;
  qs("#latest-effective-date").textContent = latestCurrentVersions()[0]?.effective_from || latestEffectiveFrom(regs);
}

async function refreshDashboard() {
  try {
    state.dashboard = await api("/api/dashboard");
  } catch (error) {
    state.dashboard = { offline: true, pending_count: 0, last_scan: null };
  }
  renderSyncRail();
  renderDashboardViews();
}

async function refreshOperations() {
  try {
    const [dashboard, versions, events] = await Promise.all([
      api("/api/dashboard"),
      api("/api/versions"),
      api("/api/events?limit=100"),
    ]);
    state.dashboard = dashboard;
    state.versions = versions.versions || [];
    state.events = events.events || [];
    state.operationOffline = false;
  } catch (error) {
    state.operationOffline = true;
    state.dashboard = state.dashboard || { offline: true, pending_count: 0, error_count: 0, last_scan: null };
  }
  renderSyncRail();
  renderShell();
  renderActiveView();
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    state.lastHealth = health;
    qs("#health-dot").className = "dot ok";
    qs("#health-text").textContent = `로컬 서버 연결됨 · ${health.chunks} chunks`;
    qs("#chunk-count").textContent = health.chunks;
    renderDashboardViews();
  } catch (error) {
    qs("#health-dot").className = "dot error";
    qs("#health-text").textContent = isGitHubPages
      ? "로컬 검색 서버 연결 실패 · 127.0.0.1:8765 확인 필요"
      : "서버 연결 실패";
  }
}

async function refreshDocuments() {
  try {
    const payload = await api("/api/documents");
    state.documents = payload.documents || [];
    renderDocumentViews();
    await refreshHealth();
    await refreshOperations();
  } catch (error) {
    state.documents = [];
    renderDocumentViews();
    await refreshHealth();
    await refreshOperations();
    throw new Error(apiFailureMessage());
  }
}

function renderDocumentViews() {
  const regs = regulationDocuments();
  qs("#doc-count").textContent = state.documents.length;
  qs("#regulation-metric").textContent = regs.length;
  qs("#regulation-count").textContent = `${regs.length}건`;
  renderCategoryRail();
  renderDocuments();
  renderCategoryStage();
  renderSyncRail();
  renderDashboardViews();
}

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

function renderCategoryRail() {
  const root = qs("#category-rail");
  const counts = new Map(CATEGORIES.map((category) => [category.id, documentsForCategory(category.id).length]));
  root.innerHTML = CATEGORIES.map((category) => {
    const isActive = category.id === state.activeCategory;
    return `
      <button class="category-tab${isActive ? " active" : ""}" data-category="${category.id}" type="button">
        <span>${escapeHtml(category.label)}</span>
        <strong>${counts.get(category.id) || 0}</strong>
      </button>
    `;
  }).join("");
}

function renderDocuments() {
  const root = qs("#documents");
  const docs = filteredHarnessDocuments();
  const regs = regulationDocuments();
  const hwpCount = regs.filter((doc) => sourceTypes(doc).includes("hwp")).length;
  qs("#harness-summary").innerHTML = `
    <div><strong>${regs.length}</strong><span>규정 문서</span></div>
    <div><strong>${hwpCount}</strong><span>HWP</span></div>
    <div><strong>${latestEffectiveFrom(regs)}</strong><span>최근 시행일</span></div>
  `;
  if (!docs.length) {
    root.innerHTML = `<div class="document-card"><strong>규정 문서 없음</strong><div class="document-meta">검색 조건에 맞는 문서가 없습니다.</div></div>`;
    return;
  }
  root.innerHTML = docs
    .map((doc) => {
      const title = documentTitle(doc);
      const category = categoryForDocument(doc);
      const permissions = (doc.permissions || [])
        .map((permission) => `<span class="badge ${permission}">${permissionLabel(permission)}</span>`)
        .join("");
      const period = [doc.effective_from || "시행일 미상", doc.effective_to ? doc.effective_to : ""]
        .filter(Boolean)
        .join(" ~ ");
      return `
        <article class="document-card">
          <strong>${escapeHtml(title)}</strong>
          <div class="document-meta">
            <span class="document-category">${escapeHtml(category.label)}</span>
            <span>${doc.chunk_count} chunks · ${(doc.source_types || []).join(", ")}</span>
            <span>${escapeHtml(period)}</span>
            <span class="meta-row">${permissions}</span>
          </div>
          <button class="mini-action" data-query="${escapeHtml(title)}" type="button">문서 검색</button>
        </article>
      `;
    })
    .join("");
}

function renderCategoryStage() {
  const category = categoryById(state.activeCategory);
  const docs = documentsForCategory(category.id);
  qs("#category-kicker").textContent = category.kicker;
  qs("#category-title").textContent = category.title;
  qs("#category-count").textContent = `${docs.length}건`;
  qs("#category-snapshot").innerHTML = `
    <div><span>문서</span><strong>${docs.length}</strong></div>
    <div><span>조항 청크</span><strong>${totalChunks(docs)}</strong></div>
    <div><span>최근 시행일</span><strong>${escapeHtml(latestEffectiveFrom(docs))}</strong></div>
    <div><span>권한</span><strong>${escapeHtml(permissionSummary(docs))}</strong></div>
  `;
  qs("#category-queries").innerHTML = category.queries
    .map((query) => `<button type="button" data-query="${escapeHtml(query)}">${escapeHtml(query)}</button>`)
    .join("");
  const visibleDocs = docs.slice(0, 18);
  if (!visibleDocs.length) {
    qs("#category-documents").innerHTML = `<article class="category-doc"><strong>해당 항목 문서 없음</strong><span>분류된 규정 문서가 없습니다.</span></article>`;
    return;
  }
  qs("#category-documents").innerHTML = visibleDocs
    .map((doc) => {
      const title = documentTitle(doc);
      const period = [doc.effective_from || "시행일 미상", doc.effective_to ? doc.effective_to : ""]
        .filter(Boolean)
        .join(" ~ ");
      return `
        <article class="category-doc">
          <strong>${escapeHtml(title)}</strong>
          <span>${doc.chunk_count} chunks · ${escapeHtml(period)}</span>
          <button class="mini-action" data-query="${escapeHtml(title)}" type="button">검색</button>
        </article>
      `;
    })
    .join("");
}

function isRestrictedResult(item) {
  return item.restricted === true || item.blocked === true || item.allowed === false || item.access === "restricted";
}

function renderResults(payload) {
  qs("#answer-output").textContent = payload.answer || "";
  qs("#result-count").textContent = `${(payload.results || []).length}건`;
  const filterParts = [];
  if (payload.as_of) filterParts.push(payload.as_of);
  filterParts.push(payload.role_label || state.role);
  filterParts.push(`권한 제외 ${payload.blocked_count}`);
  filterParts.push(`시점 제외 ${payload.date_filtered_count}`);
  qs("#filter-summary").textContent = filterParts.join(" · ");

  const root = qs("#results");
  if (!payload.results || !payload.results.length) {
    root.innerHTML = `<article class="result-card"><p class="snippet">검색 가능한 근거 조항이 없습니다.</p></article>`;
    return;
  }
  root.innerHTML = payload.results
    .map((item) => {
      const period = [item.effective_from || "시행일 미상", item.effective_to || ""].filter(Boolean).join(" ~ ");
      const page = item.page ? `p.${item.page}` : "page 없음";
      const sourceHref = item.download && item.source_path ? apiUrl(item.download.source) : "";
      const sourcePdfHref = item.download && item.source_path ? apiUrl(item.download.source_pdf) : "";
      if (isRestrictedResult(item)) {
        return `
          <article class="result-card restricted-result">
            <div class="result-title">
              <div>
                <strong>${escapeHtml(item.doc_title || "제한 문서")}</strong>
                <span>${escapeHtml(item.section_title || "권한 제한")}</span>
              </div>
              <span class="lock-mark" aria-hidden="true">잠금</span>
            </div>
            <p class="restricted-copy">상위 권한 필요</p>
          </article>
        `;
      }
      return `
        <article class="result-card">
          <div class="result-title">
            <div>
              <strong>${escapeHtml(item.doc_title)}</strong>
              <span>${escapeHtml(item.section_title)}</span>
            </div>
            <span class="score">${Number(item.score).toFixed(2)}</span>
          </div>
          <div class="summary-box">
            <span>요약</span>
            <pre>${escapeHtml(item.summary || "요약을 만들 수 없습니다.")}</pre>
          </div>
          <p class="snippet">${escapeHtml(item.snippet || item.text)}</p>
          <div class="meta-row">
            <span class="badge ${item.permission}">${permissionLabel(item.permission)}</span>
            <span class="badge">${escapeHtml(period)}</span>
            <span class="badge">${escapeHtml(page)}</span>
            <span class="badge">${escapeHtml(item.source_type || "sample")}</span>
          </div>
          <div class="download-row">
            ${sourceHref ? `<a class="download-button" href="${sourceHref}">원본 ${escapeHtml((item.source_type || "").toUpperCase())}</a>` : ""}
            ${sourcePdfHref ? `<a class="download-button" href="${sourcePdfHref}">원본 PDF</a>` : ""}
          </div>
        </article>
      `;
    })
    .join("");
}

async function runSearch() {
  const query = qs("#query-input").value.trim();
  if (!query) {
    showToast("질문을 입력하세요.");
    return;
  }
  qs("#answer-output").textContent = "검색 중...";
  try {
    const payload = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        query,
        role: state.role,
        as_of: qs("#as-of").value,
        limit: 6,
        include_history: !qs("#latest-only").checked,
      }),
    });
    renderResults(payload);
  } catch (error) {
    const message = apiFailureMessage(error);
    qs("#answer-output").textContent = message;
    qs("#result-count").textContent = "0건";
    qs("#filter-summary").textContent = "";
    qs("#results").innerHTML = "";
    throw new Error(message);
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function uploadSelectedFile() {
  const fileInput = qs("#file-input");
  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    showToast("색인할 파일을 선택하세요.");
    return;
  }
  showToast("문서를 색인하고 있습니다.");
  const contentBase64 = await fileToBase64(file);
  const payload = await api("/api/upload", {
    method: "POST",
    body: JSON.stringify({ filename: file.name, content_base64: contentBase64 }),
  });
  state.documents = payload.documents || [];
  renderDocumentViews();
  await refreshHealth();
  await refreshOperations();
  showToast(`${payload.imported_chunks}개 청크를 추가했습니다.`);
}

async function ingestLocalFolder() {
  showToast("현재 폴더 문서를 색인하고 있습니다.");
  const payload = await api("/api/ingest-local", { method: "POST", body: "{}" });
  state.documents = payload.documents || [];
  renderDocumentViews();
  await refreshHealth();
  await refreshOperations();
  const errorText = payload.errors && payload.errors.length ? ` · 오류 ${payload.errors.length}건` : "";
  showToast(`${payload.imported_chunks}개 청크를 추가했습니다${errorText}.`);
}

async function resetIndex() {
  const payload = await api("/api/reset", { method: "POST", body: "{}" });
  state.documents = payload.documents || [];
  renderDocumentViews();
  await refreshHealth();
  await refreshOperations();
  qs("#answer-output").textContent = "샘플 데이터로 초기화했습니다.";
  qs("#results").innerHTML = "";
  qs("#result-count").textContent = "0건";
  qs("#filter-summary").textContent = "";
}

function setQueryAndSearch(query) {
  qs("#query-input").value = query;
  setActiveView("search");
  runSearch().catch((error) => showToast(error.message));
}

function bindEvents() {
  qs("#primary-nav").addEventListener("click", (event) => {
    const button = event.target.closest("[data-view-target]");
    if (!button) return;
    setActiveView(button.dataset.viewTarget);
  });

  qs("#actor-role").addEventListener("change", (event) => {
    const nextRole = event.target.value;
    state.actorRole = nextRole;
    state.role = ACTOR_API_ROLES[nextRole] || "employee";
    state.localEvents.unshift({
      event_type: "RoleSimulationChanged",
      actor_role: nextRole,
      occurred_at: new Date().toISOString(),
    });
    if (!roleViews().includes(state.activeView)) {
      state.activeView = "search";
    }
    renderActiveView();
    showToast(`시연용 권한: ${ACTOR_LABELS[nextRole]}`);
  });

  qs("#category-rail").addEventListener("click", (event) => {
    const button = event.target.closest("[data-category]");
    if (!button) return;
    state.activeCategory = button.dataset.category;
    renderCategoryRail();
    renderCategoryStage();
    qs(".category-stage").scrollIntoView({ block: "start", behavior: "smooth" });
  });

  qs("#document-filter").addEventListener("input", (event) => {
    state.documentFilter = event.target.value;
    renderDocuments();
  });

  qs("#as-of").addEventListener("change", () => {
    renderSyncRail();
    renderDashboardViews();
  });

  qs("#latest-only").addEventListener("change", () => {
    qs("#filter-summary").textContent = qs("#latest-only").checked ? "최신본만" : "개정 이력 포함";
  });

  qsa("#documents, #category-queries, #category-documents").forEach((root) => {
    root.addEventListener("click", (event) => {
      const button = event.target.closest("[data-query]");
      if (!button) return;
      setQueryAndSearch(button.dataset.query);
    });
  });

  qsa(".quick-queries button").forEach((button) => {
    button.addEventListener("click", () => {
      setQueryAndSearch(button.dataset.query);
    });
  });

  qs("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch().catch((error) => showToast(error.message));
  });

  qs("#upload-button").addEventListener("click", () => {
    uploadSelectedFile().catch((error) => showToast(error.message));
  });

  qs("#local-ingest-button").addEventListener("click", () => {
    ingestLocalFolder().catch((error) => showToast(error.message));
  });

  qs("#reset-button").addEventListener("click", () => {
    resetIndex().catch((error) => showToast(error.message));
  });

  qsa("#latest-view, #updates-view, #history-view").forEach((root) => {
    root.addEventListener("click", (event) => {
      const queryButton = event.target.closest("[data-query]");
      if (queryButton) {
        setQueryAndSearch(queryButton.dataset.query);
        return;
      }

      const historyButton = event.target.closest("[data-history-regulation]");
      if (historyButton) {
        state.selectedRegulationId = historyButton.dataset.historyRegulation;
        renderHistoryView();
        return;
      }

      const approveButton = event.target.closest("[data-approve-version]");
      if (approveButton) {
        if (state.actorRole !== "audit_lead") return;
        const versionId = approveButton.dataset.approveVersion;
        const input = qs(`[data-effective-for="${selectorValue(versionId)}"]`);
        approveVersion(versionId, input?.value || operationAsOf()).catch((error) => showToast(error.message));
        return;
      }

      const rejectButton = event.target.closest("[data-reject-version]");
      if (rejectButton) {
        if (state.actorRole !== "audit_lead") return;
        rejectVersion(rejectButton.dataset.rejectVersion).catch((error) => showToast(error.message));
      }
    });
  });
}

async function boot() {
  state.role = ACTOR_API_ROLES[state.actorRole] || "employee";
  renderShell();
  renderActiveView();
  bindEvents();
  renderDocumentViews();
  await refreshDocuments();
  await refreshOperations();
  await runSearch();
}

boot().catch((error) => showToast(error.message));
