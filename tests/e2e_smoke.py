"""Self-contained browser smoke test for the regulation console.

By default the script starts an isolated server with a synthetic HWPX source.
Set REG_RAG_BASE_URL only when deliberately testing an existing server.
Downloaded regulation bytes are never written to disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from playwright.sync_api import Page, expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("REG_RAG_BASE_URL", "").rstrip("/")
DESKTOP_SCREENSHOT = Path("/tmp/cheonan-regulation-desktop.png")
MOBILE_SCREENSHOT = Path("/tmp/cheonan-regulation-mobile.png")

VIEW_HEADINGS = {
    "search": "통합 검색",
    "latest": "최신 규정",
    "updates": "업데이트 센터",
    "history": "개정 이력",
    "library": "규정 문서함",
    "permissions": "권한 관리",
    "operations": "운영 현황",
}


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def create_synthetic_hwpx(path: Path) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<section>
  <text>제1조 징계 규정의 최신 근거 조항</text>
  <text>합성 규정 원본은 브라우저 다운로드 검증에만 사용한다.</text>
  <text>감사팀장은 최신 규정의 시행일과 근거 조항을 확인한다.</text>
</section>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Contents/section0.xml", xml)


def approve_pending_versions(base_url: str) -> None:
    with urlopen(f"{base_url}/api/versions?status=pending", timeout=5) as response:
        versions = json.loads(response.read().decode("utf-8"))["versions"]
    assert versions, "isolated E2E source was not registered"
    for version in versions:
        body = json.dumps(
            {
                "version_id": version["version_id"],
                "effective_from": "2026-05-27",
                "actor": "E2E 감사팀장",
            }
        ).encode("utf-8")
        request = Request(
            f"{base_url}/api/versions/approve",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 200


@contextlib.contextmanager
def application_server():
    external_url = os.environ.get("REG_RAG_BASE_URL", "").rstrip("/")
    if external_url:
        yield external_url
        return

    with tempfile.TemporaryDirectory(prefix="reg-rag-e2e-") as temporary:
        workspace = Path(temporary)
        app_dir = workspace / "app"
        app_dir.mkdir()
        shutil.copy2(ROOT / "server.py", app_dir / "server.py")
        shutil.copy2(ROOT / "regulation_registry.py", app_dir / "regulation_registry.py")
        shutil.copytree(ROOT / "static", app_dir / "static")

        source_dir = workspace / "정관 및 규정"
        source_dir.mkdir()
        create_synthetic_hwpx(source_dir / "E2E_징계규정_2026.05.27.hwpx")

        port = available_port()
        base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update(
            {
                "REG_RAG_SOURCE_DIRS": str(workspace),
                "REG_RAG_AUTO_INGEST": "1",
                "REG_RAG_ENABLE_DEMO_MUTATIONS": "1",
            }
        )
        process = subprocess.Popen(
            [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(port)],
            cwd=app_dir,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.stdout.read() if process.stdout else ""
                    raise RuntimeError(f"isolated server exited early: {output}")
                try:
                    with urlopen(f"{base_url}/api/health", timeout=1) as response:
                        if json.loads(response.read().decode("utf-8")).get("ok") is True:
                            break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("isolated server did not become ready")
            approve_pending_versions(base_url)
            yield base_url
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def attach_error_guards(page: Page, errors: list[str]) -> None:
    page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
    page.on(
        "console",
        lambda message: errors.append(f"console: {message.text}")
        if message.type == "error"
        else None,
    )


def assert_layout(page: Page, label: str) -> None:
    layout = page.evaluate(
        """
        () => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" &&
              rect.width > 0 && rect.height > 0;
          };
          const intersects = (left, right) => {
            const a = left.getBoundingClientRect();
            const b = right.getBoundingClientRect();
            return Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
              Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
          };
          const overflow = document.documentElement.scrollWidth - window.innerWidth;
          const overlapRoots = [
            ".app-header",
            ".sync-rail",
            ".view-heading",
            ".query-actions",
            ".review-actions",
          ];
          const overlaps = [];
          for (const selector of overlapRoots) {
            for (const root of document.querySelectorAll(selector)) {
              if (!visible(root)) continue;
              const children = Array.from(root.children).filter(visible);
              for (let i = 0; i < children.length; i += 1) {
                for (let j = i + 1; j < children.length; j += 1) {
                  if (intersects(children[i], children[j])) {
                    overlaps.push(`${selector}:${children[i].tagName}/${children[j].tagName}`);
                  }
                }
              }
            }
          }
          const clippedControls = Array.from(
            document.querySelectorAll("button, select, input:not([type='file']), textarea")
          ).filter((element) => visible(element) && element.scrollWidth > element.clientWidth + 2)
            .map((element) => element.id || element.textContent.trim().slice(0, 30) || element.tagName);
          return { overflow, overlaps, clippedControls };
        }
        """
    )
    assert layout["overflow"] <= 2, f"{label}: document overflow {layout}"
    assert not layout["overlaps"], f"{label}: overlapping controls {layout}"
    assert not layout["clippedControls"], f"{label}: clipped controls {layout}"


def wait_for_application(page: Page) -> None:
    page.goto(BASE_URL, wait_until="networkidle")
    expect(page.locator("h1")).to_contain_text("천안도시공사 AI 내부규정 검색 시스템")
    expect(page.get_by_text("시연용 권한", exact=True)).to_be_visible()
    expect(page.locator("#closed-network-state")).to_have_text("폐쇄망 운영 중")
    expect(page.locator("#health-text")).to_contain_text("로컬 서버 연결됨")


def verify_roles_and_views(page: Page) -> None:
    role = page.locator("#actor-role")
    expect(role).to_have_value("employee")
    expect(page.locator("#primary-nav .nav-tab")).to_have_count(3)

    role.select_option("audit_lead")
    expect(page.locator("#primary-nav .nav-tab")).to_have_count(len(VIEW_HEADINGS))
    for view, heading in VIEW_HEADINGS.items():
        page.locator(f'[data-view-target="{view}"]').click()
        expect(page.locator(f'[data-view="{view}"]')).to_be_visible()
        expect(page.locator(f'[data-view="{view}"] h2').first).to_have_text(heading)

    role.select_option("department_head")
    expect(page.locator("#primary-nav .nav-tab")).to_have_count(4)
    role.select_option("audit_lead")


def verify_search_and_download(page: Page) -> dict[str, object]:
    page.locator('[data-view-target="search"]').click()
    page.locator("#query-input").fill("징계 규정의 최신 근거 조항")
    page.locator('#search-form button[type="submit"]').click()
    page.wait_for_function(
        """() => {
          const value = document.querySelector('#answer-output')?.textContent || '';
          return value && value !== '검색 중...' && value !== '질문을 입력하고 검색하세요.';
        }"""
    )
    result_count = page.locator("#result-count").inner_text().strip()
    assert re.fullmatch(r"\d+건", result_count), result_count

    probe = page.context.request.post(
        f"{BASE_URL}/api/search",
        data={
            "query": "징계 규정의 최신 근거 조항",
            "role": "admin",
            "as_of": "2026-05-27",
            "limit": 6,
        },
    )
    assert probe.ok, f"search API returned {probe.status}"
    api_payload = probe.json()
    serialized = json.dumps(api_payload, ensure_ascii=False)
    assert "source_path" not in serialized
    assert not re.search(r'"/(?!api/)[^\"]+"', serialized), "search API exposed an absolute path"
    page_markup = page.content()
    assert "/Users/" not in page_markup and "/srv/" not in page_markup

    links = page.locator("#results a.download-button")
    assert links.count() > 0, "search did not render a required source download link"
    href = links.first.get_attribute("href")
    assert href, "download link has no href"
    response = page.context.request.get(urljoin(f"{BASE_URL}/", href))
    assert response.ok, f"download endpoint returned {response.status}"

    return {"result_count": result_count, "download_checked": True}


def verify_viewport(browser, viewport: dict[str, int], screenshot: Path, label: str) -> dict[str, object]:
    errors: list[str] = []
    page = browser.new_page(viewport=viewport)
    attach_error_guards(page, errors)
    wait_for_application(page)
    page.locator("#actor-role").select_option("audit_lead")
    page.locator('[data-view-target="operations"]').click()
    expect(page.locator("#operations-view")).to_be_visible()
    assert_layout(page, label)
    page.locator("#primary-nav").evaluate("element => { element.scrollLeft = 0; }")
    page.screenshot(path=str(screenshot), full_page=True)
    page.close()
    assert not errors, f"{label}: browser errors: {errors}"
    return {"screenshot": str(screenshot), "viewport": viewport}


def main() -> None:
    global BASE_URL
    with application_server() as base_url:
        BASE_URL = base_url
        evidence: dict[str, object] = {"base_url": BASE_URL, "server_managed": not bool(os.environ.get("REG_RAG_BASE_URL"))}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            errors: list[str] = []
            desktop = browser.new_page(viewport={"width": 1440, "height": 1000})
            attach_error_guards(desktop, errors)
            wait_for_application(desktop)
            verify_roles_and_views(desktop)
            evidence["search"] = verify_search_and_download(desktop)
            assert_layout(desktop, "desktop")
            desktop.screenshot(path=str(DESKTOP_SCREENSHOT), full_page=True)
            desktop.close()
            assert not errors, f"desktop: browser errors: {errors}"

            evidence["desktop"] = {
                "screenshot": str(DESKTOP_SCREENSHOT),
                "viewport": {"width": 1440, "height": 1000},
            }
            evidence["mobile"] = verify_viewport(
                browser,
                {"width": 390, "height": 844},
                MOBILE_SCREENSHOT,
                "mobile",
            )
            browser.close()

        print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
