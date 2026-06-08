"""Tests for live warning deduplication."""

# ruff: noqa: PLR2004

from __future__ import annotations

from nixbot.live_warnings import LiveWarningAggregator, normalize_warning


def test_normalize_collapses_retry_spam() -> None:
    a = (
        "warning: unable to download 'https://nix-community.cachix.org/"
        "6qsj3m683ix5xdj92sq4f55gbv6srdr7.narinfo': HTTP error 524; "
        "retrying in 120081 ms (attempt 1/5)"
    )
    b = (
        "warning: unable to download 'https://nix-community.cachix.org/"
        "9a93i56z2vly62nx7cnsmi532zbb16wa.narinfo': HTTP error 524; "
        "retrying in 240120 ms (attempt 2/5)"
    )
    assert normalize_warning(a) == normalize_warning(b)
    level, msg = normalize_warning(a)
    assert level == "warning"
    assert "nix-community.cachix.org" in msg
    assert "120081" not in msg
    assert "attempt" not in msg


def test_normalize_levels_and_prefix() -> None:
    assert normalize_warning("error: builder failed")[0] == "error"
    assert normalize_warning("evaluation warning: deprecated thing") == (
        "warning",
        "deprecated thing",
    )
    # "error:" inside the message of a warning stays a warning.
    assert normalize_warning("warning: unable to download: HTTP error 524")[0] == (
        "warning"
    )


def test_aggregator_counts_and_order() -> None:
    agg = LiveWarningAggregator()
    assert not agg
    agg.add("warning: foo 'https://h/a.narinfo': HTTP error 524; retrying in 1 ms")
    agg.add("warning: foo 'https://h/b.narinfo': HTTP error 524; retrying in 2 ms")
    agg.add("error: gave up on bar")
    snap = agg.snapshot()
    assert snap[0]["level"] == "error"  # errors sort first
    assert snap[1]["count"] == 2
    assert agg


def test_long_messages_truncated() -> None:
    level, msg = normalize_warning("warning: " + "x" * 2000)
    assert level == "warning"
    assert len(msg) == 500
    assert msg.endswith("…")


def test_aggregator_bounded() -> None:
    agg = LiveWarningAggregator(max_groups=2)
    assert agg.add("warning: one")
    assert agg.add("warning: two")
    assert not agg.add("warning: three")
    assert len(agg.snapshot()) == 2
