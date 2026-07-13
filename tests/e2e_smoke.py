"""Browser smoke test for the closed-network regulation console.

Run this against an already-started local server. The script never writes
downloaded regulation bytes to disk.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page, expect, sync_playwright


BASE_URL = os.environ.get("REG_RAG_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
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

    links = page.locator("#results a.download-button")
    download_checked = False
    if links.count():
        href = links.first.get_attribute("href")
        assert href, "download link has no href"
        response = page.context.request.get(urljoin(f"{BASE_URL}/", href))
        assert response.ok, f"download endpoint returned {response.status}"
        download_checked = True
    else:
        expect(page.locator("#results")).to_be_visible()

    return {"result_count": result_count, "download_checked": download_checked}


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
    evidence: dict[str, object] = {"base_url": BASE_URL}
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
