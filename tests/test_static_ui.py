import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
