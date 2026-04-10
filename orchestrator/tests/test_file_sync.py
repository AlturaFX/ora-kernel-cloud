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
