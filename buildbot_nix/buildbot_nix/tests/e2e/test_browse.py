"""Browser navigation: sidebar, filter form, build details, search."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


def test_homepage_lists_project_and_recent_builds(page: Page) -> None:
    page.goto("/")
    assert "buildbot-nix" in page.title()
    page.get_by_role("link", name="acme/widget").first.wait_for()
    # Recent feed shows the newest build.
    assert "#3" in page.content()


def test_navigate_sidebar_to_failed_build(page: Page) -> None:
    page.goto("/")
    page.get_by_role("link", name="acme/widget").first.click()
    page.wait_for_url("**/projects/acme/widget")

    page.locator('a[href$="/builds/2"]').first.click()
    page.wait_for_url("**/builds/2")
    content = page.content()
    # Failed attributes sort first within their system group.
    assert content.index("x86_64-linux.bad") < content.index("x86_64-linux.ok")
    # Inline error excerpt under the failed attribute.
    assert "builder failed loudly" in content
    # Commit links back to the forge.
    assert "https://github.com/acme/widget/commit/sha-2" in content


def test_build_prev_next_navigation(page: Page) -> None:
    page.goto("/projects/acme/widget/builds/2")
    page.locator('a[href$="/builds/3"]').first.click()
    page.wait_for_url("**/builds/3")
    page.locator('a[href$="/builds/2"]').first.click()
    page.wait_for_url("**/builds/2")


def test_project_filter_form(page: Page) -> None:
    page.goto("/projects/acme/widget")
    page.select_option("select[name=status]", "failed")
    page.get_by_role("button", name="filter").click()
    page.wait_for_url("**status=failed**")
    content = page.content()
    assert "#2" in content
    assert ">#1<" not in content

    page.fill("input[name=branch]", "feature")
    page.select_option("select[name=status]", "")
    page.get_by_role("button", name="filter").click()
    page.wait_for_url("**branch=feature**")
    content = page.content()
    assert "#3" in content
    assert ">#2<" not in content


def test_attribute_history_via_build_page(page: Page) -> None:
    page.goto("/projects/acme/widget/builds/2")
    page.locator('a[href$="/attrs/x86_64-linux.bad"]').first.click()
    page.wait_for_url("**/attrs/x86_64-linux.bad")
    content = page.content()
    # All three builds ran this attribute.
    for number in (1, 2, 3):
        assert f"/builds/{number}" in content


def test_header_search(page: Page) -> None:
    page.goto("/")
    page.fill("input[name=q]", "widget")
    page.press("input[name=q]", "Enter")
    page.wait_for_url("**/search?q=widget")
    page.get_by_role("link", name="acme/widget").first.wait_for()


def test_queue_shows_running_build(page: Page) -> None:
    page.goto("/queue")
    content = page.content()
    assert "acme/widget" in content
    assert "#3" in content
