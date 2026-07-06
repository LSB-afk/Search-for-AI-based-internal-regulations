const state = {
  role: "employee",
  documents: [],
  lastHealth: null,
};

const qs = (selector) => document.querySelector(selector);
const qsa = (selector) => Array.from(document.querySelectorAll(selector));

function showToast(message) {
  const toast = qs("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.setTimeout(() => toast.classList.remove("visible"), 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
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

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    state.lastHealth = health;
    qs("#health-dot").className = "dot ok";
    qs("#health-text").textContent = `로컬 서버 연결됨 · ${health.chunks} chunks`;
    qs("#chunk-count").textContent = health.chunks;
  } catch (error) {
    qs("#health-dot").className = "dot error";
    qs("#health-text").textContent = "서버 연결 실패";
  }
}

async function refreshDocuments() {
  const payload = await api("/api/documents");
  state.documents = payload.documents || [];
  qs("#doc-count").textContent = state.documents.length;
  renderDocuments();
  await refreshHealth();
}

function renderDocuments() {
  const root = qs("#documents");
  if (!state.documents.length) {
    root.innerHTML = `<div class="document-card"><strong>색인 문서 없음</strong><div class="document-meta">샘플 초기화 또는 문서 색인을 실행하세요.</div></div>`;
    return;
  }
  root.innerHTML = state.documents
    .map((doc) => {
      const permissions = (doc.permissions || [])
        .map((permission) => `<span class="badge ${permission}">${permissionLabel(permission)}</span>`)
        .join("");
      const period = [doc.effective_from || "시행일 미상", doc.effective_to ? doc.effective_to : ""]
        .filter(Boolean)
        .join(" ~ ");
      return `
        <article class="document-card">
          <strong>${escapeHtml(doc.doc_title)}</strong>
          <div class="document-meta">
            <span>${doc.chunk_count} chunks · ${(doc.source_types || []).join(", ")}</span>
            <span>${escapeHtml(period)}</span>
            <span class="meta-row">${permissions}</span>
          </div>
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
      const sourceHref = item.download && item.source_path ? item.download.source : "";
      const sourcePdfHref = item.download && item.source_path ? item.download.source_pdf : "";
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
  renderDocuments();
  await refreshHealth();
  showToast(`${payload.imported_chunks}개 청크를 추가했습니다.`);
}

async function ingestLocalFolder() {
  showToast("현재 폴더 문서를 색인하고 있습니다.");
  const payload = await api("/api/ingest-local", { method: "POST", body: "{}" });
  state.documents = payload.documents || [];
  renderDocuments();
  await refreshHealth();
  const errorText = payload.errors && payload.errors.length ? ` · 오류 ${payload.errors.length}건` : "";
  showToast(`${payload.imported_chunks}개 청크를 추가했습니다${errorText}.`);
}

async function resetIndex() {
  const payload = await api("/api/reset", { method: "POST", body: "{}" });
  state.documents = payload.documents || [];
  renderDocuments();
  await refreshHealth();
  qs("#answer-output").textContent = "샘플 데이터로 초기화했습니다.";
  qs("#results").innerHTML = "";
  qs("#result-count").textContent = "0건";
  qs("#filter-summary").textContent = "";
}

function bindEvents() {
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
      qs("#query-input").value = button.dataset.query;
      runSearch().catch((error) => showToast(error.message));
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
