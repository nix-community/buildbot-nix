"""Browser navigation: sidebar, filter form, build details, search."""

from __future__ import annotations

from typing import TYPE_CHECKING

from buildbot_nix.web.queries import PAGE_SIZE

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .support import EngineServer


def test_homepage_lists_project_and_recent_builds(page: Page) -> None:
    page.goto("/")
    assert "buildbot-nix" in page.title()
    page.get_by_role("link", name="acme/widget").first.wait_for()
    # Recent feed shows the newest build.
    assert "#3" in page.content()


def test_navigate_sidebar_to_failed_build(page: Page) -> None:
    page.goto("/")
    page.get_by_role("link", name="acme/widget").first.click()
    page.wait_for_url("**/repos/acme/widget")

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
    page.goto("/repos/acme/widget/builds/2")
    page.locator('a[href$="/builds/3"]').first.click()
    page.wait_for_url("**/builds/3")
    page.locator('a[href$="/builds/2"]').first.click()
    page.wait_for_url("**/builds/2")


def test_project_filter_form(page: Page) -> None:
    page.goto("/repos/acme/widget")
    page.select_option("select[name=status]", "failed")
    page.get_by_role("button", name="filter").click()
    page.wait_for_url("**status=failed**")
    content = page.content()
    assert "#2" in content
    assert ">#1<" not in content

    page.fill("input[name=ref]", "feature")
    page.select_option("select[name=status]", "")
    page.get_by_role("button", name="filter").click()
    page.wait_for_url("**ref=feature**")
    content = page.content()
    assert "#3" in content
    assert ">#2<" not in content

    page.fill("input[name=ref]", "5")
    page.get_by_role("button", name="filter").click()
    page.wait_for_url("**ref=5**")
    content = page.content()
    assert "#3" in content
    assert ">#2<" not in content


def test_attribute_log_prev_next_navigation(page: Page) -> None:
    page.goto("/repos/acme/widget/builds/2/logs/x86_64-linux.bad")
    page.locator('a[rel="prev"]').click()
    page.wait_for_url("**/builds/1/logs/x86_64-linux.bad")
    page.locator('a[rel="next"]').click()
    page.wait_for_url("**/builds/2/logs/x86_64-linux.bad")


def test_project_filter(page: Page) -> None:
    page.goto("/")
    page.fill("#pipeline-filter", "nope-no-such-project")
    page.get_by_text("no matching repositories").wait_for(timeout=15_000)
    page.fill("#pipeline-filter", "widget")
    page.get_by_text("acme/widget").first.wait_for(timeout=15_000)


def test_activity_shows_running_build(page: Page) -> None:
    page.goto("/builds")
    content = page.content()
    assert "acme/widget" in content
    assert "#3" in content


SEEDED_BUILDS = 70


def test_activity_infinite_scroll(page: Page, server: EngineServer) -> None:
    async def seed_history() -> None:
        project_id = await server.pool.fetchval(
            "SELECT id FROM projects WHERE owner = 'acme' AND name = 'widget'"
        )
        await server.pool.executemany(
            """
            INSERT INTO builds (project_id, number, tree_hash, commit_sha,
                                branch, status, created_at, started_at,
                                finished_at)
            VALUES ($1, $2, $3, $4, 'main', 'succeeded', now(), now(), now())
            """,
            [
                (project_id, n, f"tree-{n}", f"sha-{n}{'0' * 30}")
                for n in range(100, 100 + SEEDED_BUILDS)
            ],
        )

    server.run(seed_history())
    try:
        page.goto("/builds")
        rows = page.locator("tr[data-id]")
        first_batch = rows.count()
        assert first_batch == PAGE_SIZE

        # Scrolling to the sentinel loads the next batch.
        page.locator("tr.scroll-sentinel").scroll_into_view_if_needed()
        page.wait_for_function(
            f"document.querySelectorAll('tr[data-id]').length > {PAGE_SIZE}"
        )
        assert rows.count() == SEEDED_BUILDS + 3  # plus the base fixture
    finally:
        server.run(server.pool.execute("DELETE FROM builds WHERE number >= 100"))
