const state = {
  role: "employee",
  documents: [],
  lastHealth: null,
  activeCategory: "all",
  documentFilter: "",
};

const REGULATION_TYPES = new Set(["hwp", "hwpx", "pdf"]);
const CATEGORY_FALLBACK_ID = "operations";
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

function permissionSummary(docs) {
  const permissions = new Set(docs.flatMap((doc) => doc.permissions || []));
  if (!permissions.size) return "권한 없음";
  return Array.from(permissions)
    .sort((a, b) => permissionLabel(a).localeCompare(permissionLabel(b), "ko"))
    .map(permissionLabel)
    .join(", ");
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    state.lastHealth = health;
    qs("#health-dot").className = "dot ok";
    qs("#health-text").textContent = `로컬 서버 연결됨 · ${health.chunks} chunks`;
    qs("#chunk-count").textContent = health.chunks;
  } catch (error) {
    qs("#health-dot").className = "dot error";
    qs("#health-text").textContent = isGitHubPages
      ? "로컬 검색 서버 연결 실패 · 127.0.0.1:8765 확인 필요"
      : "서버 연결 실패";
  }
}

async function refreshDocuments() {
  const payload = await api("/api/documents");
  state.documents = payload.documents || [];
  renderDocumentViews();
  await refreshHealth();
}

function renderDocumentViews() {
  const regs = regulationDocuments();
  qs("#doc-count").textContent = state.documents.length;
  qs("#regulation-metric").textContent = regs.length;
  qs("#regulation-count").textContent = `${regs.length}건`;
  renderCategoryRail();
  renderDocuments();
  renderCategoryStage();
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
  showToast(`${payload.imported_chunks}개 청크를 추가했습니다.`);
}

async function ingestLocalFolder() {
  showToast("현재 폴더 문서를 색인하고 있습니다.");
  const payload = await api("/api/ingest-local", { method: "POST", body: "{}" });
  state.documents = payload.documents || [];
  renderDocumentViews();
  await refreshHealth();
  const errorText = payload.errors && payload.errors.length ? ` · 오류 ${payload.errors.length}건` : "";
  showToast(`${payload.imported_chunks}개 청크를 추가했습니다${errorText}.`);
}

async function resetIndex() {
  const payload = await api("/api/reset", { method: "POST", body: "{}" });
  state.documents = payload.documents || [];
  renderDocumentViews();
  await refreshHealth();
  qs("#answer-output").textContent = "샘플 데이터로 초기화했습니다.";
  qs("#results").innerHTML = "";
  qs("#result-count").textContent = "0건";
  qs("#filter-summary").textContent = "";
}

function setQueryAndSearch(query) {
  qs("#query-input").value = query;
  runSearch().catch((error) => showToast(error.message));
}

function bindEvents() {
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

  qsa("#documents, #category-queries, #category-documents").forEach((root) => {
    root.addEventListener("click", (event) => {
      const button = event.target.closest("[data-query]");
      if (!button) return;
      setQueryAndSearch(button.dataset.query);
    });
  });

  qsa(".segment").forEach((button) => {
    button.addEventListener("click", () => {
      qsa(".segment").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.role = button.dataset.role;
      runSearch().catch((error) => showToast(error.message));
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
}

async function boot() {
  bindEvents();
  await refreshDocuments();
  await runSearch();
}

boot().catch((error) => showToast(error.message));
