"""Browser navigation: sidebar, filter form, build details, search."""

from __future__ import annotations

from typing import TYPE_CHECKING

from buildbot_nix.web.queries import PAGE_SIZE

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .support import TestServer


def test_homepage_lists_project_and_recent_builds(page: Page) -> None:
    page.goto("/")
    assert "buildbot-nix" in page.title()
    page.get_by_role("link", name="acme/widget").first.wait_for()
    # Recent feed shows the newest build.
    assert "#3" in page.content()


def test_navigate_sidebar_to_failed_build(page: Page) -> None:
    page.goto("/")
    page.get_by_role("link", name="acme/widget").first.click()
    page.wait_for_url("**/repos/github/acme/widget")

    page.locator('a[href$="/builds/2"]').first.click()
    page.wait_for_url("**/builds/2")
    content = page.content()
    # Failures render inline, the succeeded bulk is collapsed.
    assert "x86_64-linux.bad" in content
    assert "x86_64-linux.ok" not in content
    # Inline error excerpt under the failed attribute.
    assert "builder failed loudly" in content
    # Commit links back to the forge.
    assert "https://github.com/acme/widget/commit/sha-2" in content

    # Expanding the group lazy-loads its rows.
    page.get_by_text("2 succeeded").click()
    page.locator('tr[data-attr="x86_64-linux.ok"]').wait_for()


def test_build_prev_next_navigation(page: Page) -> None:
    page.goto("/repos/github/acme/widget/builds/2")
    page.locator('a[href$="/builds/3"]').first.click()
    page.wait_for_url("**/builds/3")
    page.locator('a[href$="/builds/2"]').first.click()
    page.wait_for_url("**/builds/2")


def test_project_filter_form(page: Page) -> None:
    page.goto("/repos/github/acme/widget")
    # No filter button: the select applies itself, the ref input
    # submits on enter.
    page.select_option("select[name=status]", "failed")
    page.wait_for_url("**status=failed**")
    content = page.content()
    assert "#2" in content
    assert ">#1<" not in content

    page.select_option("select[name=status]", "")
    page.wait_for_url("**status=**")
    page.fill("input[name=ref]", "feature")
    page.press("input[name=ref]", "Enter")
    page.wait_for_url("**ref=feature**")
    content = page.content()
    assert "#3" in content
    assert ">#2<" not in content

    page.fill("input[name=ref]", "5")
    page.press("input[name=ref]", "Enter")
    page.wait_for_url("**ref=5**")
    content = page.content()
    assert "#3" in content
    assert ">#2<" not in content


def test_attribute_log_prev_next_navigation(page: Page) -> None:
    page.goto("/repos/github/acme/widget/builds/2/logs/x86_64-linux.bad")
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


def test_activity_infinite_scroll(page: Page, server: TestServer) -> None:
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


def test_no_horizontal_overflow_on_mobile(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    for path in ("/", "/builds", "/repos/github/acme/widget/builds/2"):
        page.goto(path)
        overflow = page.evaluate(
            "document.documentElement.scrollWidth"
            " - document.documentElement.clientWidth"
        )
        assert overflow == 0, f"{path} overflows by {overflow}px"


def test_attribute_search_filters_groups(page: Page) -> None:
    page.goto("/repos/github/acme/widget/builds/2")
    page.fill("#attr-filter", "other")
    group = page.locator("details.attr-group[data-group=succeeded]")
    group.locator("text=1 succeeded").wait_for()
    assert group.get_attribute("open") is not None
    assert group.locator("text=aarch64-linux.other").is_visible()
    page.fill("#attr-filter", "")
    page.locator("text=2 succeeded").wait_for()


def test_groups_keep_identity_when_one_appears(page: Page, server: TestServer) -> None:
    """A new group shifts the list; positional morphing used to merge
    one group's rows into another."""

    async def seed() -> None:
        bid = await server.pool.fetchval("SELECT id FROM builds WHERE number = 3")
        await server.pool.executemany(
            "INSERT INTO build_attributes (build_id, attr, system, status)"
            " VALUES ($1, $2, 'aarch64-linux', $3)",
            [(bid, f"aarch64-linux.b-{i}", "building") for i in range(5)]
            + [(bid, f"aarch64-linux.p-{i}", "pending") for i in range(50)],
        )

    async def add_failed() -> None:
        bid = await server.pool.fetchval("SELECT id FROM builds WHERE number = 3")
        await server.pool.execute(
            "INSERT INTO build_attributes (build_id, attr, system, status, error)"
            " VALUES ($1, 'aarch64-linux.boom', 'aarch64-linux', 'failed', 'kaput')",
            bid,
        )

    server.run(seed())
    try:
        page.goto("/repos/github/acme/widget/builds/3")
        building = page.locator("details.attr-group[data-group='building']")
        building.locator("text=aarch64-linux.b-0").wait_for()
        server.run(add_failed())
        page.locator("details.attr-group[data-group='failed']").wait_for()
        first = building.locator("td.attr-name code").first.text_content()
        assert first == "aarch64-linux.b-0"
    finally:
        server.run(
            server.pool.execute(
                "DELETE FROM build_attributes WHERE attr LIKE 'aarch64-linux.%'"
            )
        )
