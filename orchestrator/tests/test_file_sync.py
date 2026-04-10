"""Tests for orchestrator.file_sync."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.file_sync import (
    FileSync,
    is_tracked,
    normalize_path,
    parse_sync_fences,
)


# ── normalize_path ──────────────────────────────────────────────────

def test_normalize_strips_work_prefix():
    assert normalize_path("/work/.claude/kernel/journal/WISDOM.md") == \
        ".claude/kernel/journal/WISDOM.md"


def test_normalize_strips_leading_slash_without_work():
    assert normalize_path("/.claude/kernel/journal/2026-04-10.md") == \
        ".claude/kernel/journal/2026-04-10.md"


def test_normalize_leaves_relative_paths_alone():
    assert normalize_path(".claude/kernel/journal/WISDOM.md") == \
        ".claude/kernel/journal/WISDOM.md"


def test_normalize_handles_work_without_trailing_slash():
    assert normalize_path("/work") == ""


# ── is_tracked ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    ".claude/kernel/journal/WISDOM.md",
    ".claude/kernel/journal/2026-04-10.md",
    ".claude/kernel/nodes/system/domain_planner.md",
    ".claude/kernel/nodes/quads/auth/task_worker.md",
])
def test_is_tracked_positive(path):
    assert is_tracked(path) is True


@pytest.mark.parametrize("path", [
    ".claude/events/inbox.jsonl",
    ".claude/settings.json",
    "CLAUDE.md",
    "PROJECT_DNA.md",
    ".claude/kernel/journal/WISDOM.txt",   # wrong suffix
    "random.md",
])
def test_is_tracked_negative(path):
    assert is_tracked(path) is False


# ── parse_sync_fences ───────────────────────────────────────────────

def test_parse_sync_fences_extracts_single_block():
    text = (
        "Here is the snapshot.\n"
        "```SYNC path=.claude/kernel/journal/WISDOM.md\n"
        "# Wisdom\n"
        "- insight one\n"
        "```\n"
        "Done."
    )
    result = parse_sync_fences(text)
    assert result == [
        (".claude/kernel/journal/WISDOM.md", "# Wisdom\n- insight one"),
    ]


def test_parse_sync_fences_extracts_multiple_blocks():
    text = (
        "```SYNC path=.claude/kernel/journal/WISDOM.md\nA\n```\n"
        "Some prose between blocks.\n"
        "```SYNC path=.claude/kernel/journal/2026-04-10.md\nB\nC\n```\n"
    )
    result = parse_sync_fences(text)
    assert result == [
        (".claude/kernel/journal/WISDOM.md", "A"),
        (".claude/kernel/journal/2026-04-10.md", "B\nC"),
    ]


def test_parse_sync_fences_ignores_non_sync_fences():
    text = (
        "```python\nprint('hi')\n```\n"
        "```SYNC path=.claude/kernel/journal/WISDOM.md\nreal\n```\n"
    )
    result = parse_sync_fences(text)
    assert result == [(".claude/kernel/journal/WISDOM.md", "real")]


def test_parse_sync_fences_empty_on_no_matches():
    assert parse_sync_fences("just prose, no fences") == []


# ── FileSync.handle_write ───────────────────────────────────────────

def _make_db():
    db = MagicMock()
    db.sync_file = MagicMock()
    db.get_synced_file = MagicMock(return_value=None)
    db.log_activity = MagicMock()
    return db


def test_handle_write_syncs_tracked_path():
    db = _make_db()
    fs = FileSync(db)

    fs.handle_write("/work/.claude/kernel/journal/WISDOM.md", "# Wisdom\nA")

    db.sync_file.assert_called_once_with(
        ".claude/kernel/journal/WISDOM.md",
        "# Wisdom\nA",
        synced_from="cdc",
    )


def test_handle_write_skips_untracked_path():
    db = _make_db()
    fs = FileSync(db)

    fs.handle_write("/work/CLAUDE.md", "ignored")
    fs.handle_write("/work/.claude/events/inbox.jsonl", "ignored")

    db.sync_file.assert_not_called()


def test_handle_write_skips_empty_path():
    db = _make_db()
    fs = FileSync(db)
    fs.handle_write("", "content")
    db.sync_file.assert_not_called()


def test_handle_write_accepts_empty_content():
    db = _make_db()
    fs = FileSync(db)

    fs.handle_write("/work/.claude/kernel/journal/WISDOM.md", "")

    db.sync_file.assert_called_once_with(
        ".claude/kernel/journal/WISDOM.md", "", synced_from="cdc"
    )


# ── FileSync.handle_edit ────────────────────────────────────────────

def test_handle_edit_applies_diff_against_cached_content():
    db = _make_db()
    db.get_synced_file.return_value = "hello world\nline two"
    fs = FileSync(db)

    result = fs.handle_edit(
        "/work/.claude/kernel/journal/WISDOM.md",
        old_string="hello world",
        new_string="HELLO WORLD",
    )

    assert result is True
    db.get_synced_file.assert_called_once_with(".claude/kernel/journal/WISDOM.md")
    db.sync_file.assert_called_once_with(
        ".claude/kernel/journal/WISDOM.md",
        "HELLO WORLD\nline two",
        synced_from="cdc",
    )


def test_handle_edit_replaces_only_first_occurrence():
    db = _make_db()
    db.get_synced_file.return_value = "foo bar foo"
    fs = FileSync(db)

    fs.handle_edit(
        "/work/.claude/kernel/nodes/system/planner.md",
        old_string="foo",
        new_string="baz",
    )

    db.sync_file.assert_called_once_with(
        ".claude/kernel/nodes/system/planner.md",
        "baz bar foo",
        synced_from="cdc",
    )


def test_handle_edit_skips_untracked_path():
    db = _make_db()
    fs = FileSync(db)

    result = fs.handle_edit("/work/CLAUDE.md", "a", "b")

    assert result is False
    db.get_synced_file.assert_not_called()
    db.sync_file.assert_not_called()


def test_handle_edit_logs_divergence_when_old_string_absent():
    db = _make_db()
    db.get_synced_file.return_value = "cached body without marker"
    fs = FileSync(db)

    result = fs.handle_edit(
        "/work/.claude/kernel/journal/WISDOM.md",
        old_string="missing marker",
        new_string="replacement",
    )

    assert result is False
    db.sync_file.assert_not_called()
    # Divergence should be reported via log_activity
    assert db.log_activity.called
    call = db.log_activity.call_args
    assert call.kwargs.get("action") == "CDC_DIVERGENCE"
    assert call.kwargs.get("level") == "WARNING"


def test_handle_edit_logs_missing_when_no_prior_content():
    db = _make_db()
    db.get_synced_file.return_value = None
    fs = FileSync(db)

    result = fs.handle_edit(
        "/work/.claude/kernel/journal/2026-04-10.md",
        old_string="x",
        new_string="y",
    )

    assert result is False
    db.sync_file.assert_not_called()
    assert db.log_activity.called
    call = db.log_activity.call_args
    assert call.kwargs.get("action") == "CDC_MISSING_BASE"
