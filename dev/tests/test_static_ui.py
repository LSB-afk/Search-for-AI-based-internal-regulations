import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")


class StaticParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.options = []
        self._current_option = None

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if tag == "option":
            self._current_option = {"value": attr_map.get("value"), "label": ""}

    def handle_data(self, data):
        if self._current_option is not None:
            self._current_option["label"] += data

    def handle_endtag(self, tag):
        if tag == "option" and self._current_option is not None:
            self.options.append(
                {
                    "value": self._current_option["value"],
                    "label": self._current_option["label"].strip(),
                }
            )
            self._current_option = None


class ProductShellStaticTest(unittest.TestCase):
    def test_product_title_and_network_mark_are_visible(self):
        self.assertIn("천안도시공사 AI 내부규정 검색 시스템", HTML)
        self.assertIn('class="network-mark"', HTML)
        self.assertIn("폐쇄망 운영", HTML)
        self.assertIn("시연용 권한", HTML)

    def test_role_switcher_has_four_contract_options(self):
        parser = StaticParser()
        parser.feed(HTML)
        role_options = [option for option in parser.options if option["value"] in ROLE_VALUES]
        self.assertEqual(
            role_options,
            [
                {"value": "audit_lead", "label": "감사팀장"},
                {"value": "auditor", "label": "감사담당자"},
                {"value": "department_head", "label": "부서장"},
                {"value": "employee", "label": "일반직원"},
            ],
        )

    def test_role_navigation_contract_has_seven_view_labels(self):
        self.assertIn("const ROLE_VIEWS", JS)
        self.assertIn("const VIEW_LABELS", JS)
        for view_id, label in VIEW_LABELS.items():
            with self.subTest(view_id=view_id):
                self.assertRegex(JS, rf"{view_id}:\s*\"{label}\"")

    def test_shell_exposes_navigation_root_and_state_fields(self):
        self.assertIn('id="primary-nav"', HTML)
        self.assertIn('id="view-root"', HTML)
        self.assertRegex(JS, r"activeView:\s*\"search\"")
        self.assertRegex(JS, r"actorRole:\s*\"employee\"")
        self.assertIn("RoleSimulationChanged", JS)

    def test_synchronization_rail_surfaces_real_status_fields(self):
        self.assertIn('class="sync-rail"', HTML)
        self.assertIn("규정 동기화 레일", HTML)
        for marker in ("closed-network-state", "last-scan", "pending-count", "latest-effective-date"):
            with self.subTest(marker=marker):
                self.assertIn(marker, HTML)

    def test_required_color_tokens_are_fixed(self):
        for name, value in {
            "--canvas": "#eef1ed",
            "--surface": "#ffffff",
            "--ink": "#17201c",
            "--muted": "#66726c",
            "--line": "#cbd3ce",
            "--corporate-green": "#176b52",
            "--signal-blue": "#315b78",
            "--warning": "#a56b18",
            "--danger": "#a13f48",
        }.items():
            with self.subTest(name=name):
                self.assertRegex(CSS, rf"{re.escape(name)}:\s*{re.escape(value)};")

    def test_operations_loader_uses_required_endpoints_and_offline_state(self):
        self.assertIn("async function refreshOperations()", JS)
        for endpoint in ('"/api/dashboard"', '"/api/versions"', '"/api/events?limit=100"'):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, JS)
        self.assertRegex(JS, r"state\.versions\s*=\s*versions\.versions\s*\|\|\s*\[\]")
        self.assertRegex(JS, r"state\.events\s*=\s*events\.events\s*\|\|\s*\[\]")
        self.assertIn("operationOffline", JS)
        self.assertIn("로컬 운영 서버 연결 필요", JS)
        self.assertIn("renderActiveView();", JS)

    def test_required_operations_view_renderers_exist(self):
        for function_name in (
            "renderLatestView",
            "renderUpdatesView",
            "renderHistoryView",
            "renderPermissionsView",
            "renderOperationsView",
        ):
            with self.subTest(function_name=function_name):
                self.assertIn(f"function {function_name}()", JS)
        for view_id in ("latest-view", "updates-view", "history-view", "permissions-view", "operations-view"):
            with self.subTest(view_id=view_id):
                self.assertIn(f'#{view_id}', JS)

    def test_latest_only_checkbox_controls_include_history(self):
        self.assertIn('id="latest-only"', HTML)
        self.assertIn("최신본만", HTML)
        self.assertRegex(HTML, r'id="latest-only"[^>]*checked')
        self.assertIn("include_history: !qs(\"#latest-only\").checked", JS)

    def test_search_timeline_surface_sorting_and_download_controls_exist(self):
        self.assertIn('id="version-timeline-section"', HTML)
        self.assertIn('id="version-timelines"', HTML)
        self.assertIn('id="timeline-sort"', HTML)
        self.assertIn('value="desc"', HTML)
        self.assertIn('value="asc"', HTML)
        self.assertIn("최신순", HTML)
        self.assertIn("과거순", HTML)
        self.assertIn("function renderVersionTimelines(timelines, includeHistory)", JS)
        self.assertIn("payload.timelines || []", JS)
        self.assertIn("version.download?.source", JS)
        self.assertIn("version.download?.source_pdf", JS)
        self.assertNotIn("version.source_path", JS)

    def test_search_clears_stale_timeline_for_latest_only_and_failures(self):
        self.assertIn("function clearVersionTimelines()", JS)
        self.assertRegex(JS, r"if\s*\(!includeHistory\)\s*\{\s*clearVersionTimelines\(\)")
        run_search = re.search(r"async function runSearch\(\).*?\n\}", JS, re.DOTALL)
        self.assertIsNotNone(run_search)
        self.assertIn("clearVersionTimelines();", run_search.group(0))

    def test_approval_rejection_flow_is_audit_lead_only(self):
        self.assertIn("async function approveVersion(versionId, effectiveFrom)", JS)
        self.assertIn("async function rejectVersion(versionId)", JS)
        self.assertIn('"/api/versions/approve"', JS)
        self.assertIn('"/api/versions/reject"', JS)
        self.assertIn('actor: "감사팀장(시연)"', JS)
        self.assertRegex(JS, r"state\.actorRole\s*===\s*\"audit_lead\"")
        self.assertIn("승인", JS)
        self.assertIn("반려", JS)

    def test_restricted_results_hide_body_and_download(self):
        self.assertIn("function isRestrictedResult(item)", JS)
        self.assertIn("restricted-result", JS)
        self.assertIn("상위 권한 필요", JS)
        self.assertIn("lock-mark", CSS)
        self.assertRegex(JS, r"if\s*\(\s*isRestrictedResult\(item\)\s*\)")
        restricted_branch = re.search(
            r"if\s*\(\s*isRestrictedResult\(item\)\s*\)\s*\{(?P<branch>.*?)\n\s*\}\n\s*return\s*`",
            JS,
            re.DOTALL,
        )
        self.assertIsNotNone(restricted_branch)
        restricted_markup = restricted_branch.group("branch")
        self.assertIn('item.doc_title || "제한 문서"', restricted_markup)
        self.assertNotIn("item.section_title", restricted_markup)
        self.assertNotIn("item.summary", restricted_markup)
        self.assertNotIn("item.snippet", restricted_markup)
        self.assertNotIn("item.text", restricted_markup)
        self.assertNotIn("item.download", restricted_markup)
        self.assertNotIn("download-button", restricted_markup)

    def test_browser_uses_opaque_download_urls_without_source_paths(self):
        self.assertNotIn("item.source_path", JS)
        self.assertNotIn("version.source_path", JS)
        self.assertIn("item.download", JS)

    def test_operations_refresh_clears_stale_versions_and_events_on_failure(self):
        catch_branch = re.search(
            r"async function refreshOperations\(\).*?catch\s*\(error\)\s*\{(?P<branch>.*?)\n\s*\}\n\s*renderSyncRail\(\);",
            JS,
            re.DOTALL,
        )
        self.assertIsNotNone(catch_branch)
        failure_handling = catch_branch.group("branch")
        self.assertRegex(failure_handling, r"state\.versions\s*=\s*\[\]")
        self.assertRegex(failure_handling, r"state\.events\s*=\s*\[\]")

    def test_timeline_status_copy_hashes_and_empty_states_are_present(self):
        for copy in ("현재본", "시행 예정", "이전 버전", "반려", "오류", "검토 대기"):
            with self.subTest(copy=copy):
                self.assertIn(copy, JS)
        self.assertIn(".slice(0, 10)", JS)
        self.assertIn('title="${escapeHtml(version.content_hash || "")}"', JS)
        for empty_copy in ("표시할 최신 규정이 없습니다.", "검토 대기 항목이 없습니다.", "개정 이력이 없습니다."):
            with self.subTest(empty_copy=empty_copy):
                self.assertIn(empty_copy, JS)

    def test_permissions_hierarchy_and_operations_density_are_rendered(self):
        for copy in ("감사팀장", "감사담당자", "부서장", "일반직원", "허용 화면"):
            with self.subTest(copy=copy):
                self.assertIn(copy, JS)
        self.assertIn("permission-level", CSS)
        for metric in ("전체 규정", "최신본", "검토 대기", "오류", "마지막 스캔", "감사 이벤트"):
            with self.subTest(metric=metric):
                self.assertIn(metric, JS)

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

    def test_long_names_and_mobile_controls_wrap_without_overlap(self):
        for marker in ("overflow-wrap: anywhere", "min-width: 0", "grid-template-columns: 1fr"):
            with self.subTest(marker=marker):
                self.assertIn(marker, CSS)


ROLE_VALUES = {"audit_lead", "auditor", "department_head", "employee"}

VIEW_LABELS = {
    "search": "통합 검색",
    "latest": "최신 규정",
    "updates": "업데이트 센터",
    "history": "개정 이력",
    "library": "규정 문서함",
    "permissions": "권한 관리",
    "operations": "운영 현황",
}
