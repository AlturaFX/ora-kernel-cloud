# Visibility, HITL, and File Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ORA Kernel Cloud orchestrator usable day-to-day by (1) unblocking visibility into what the Kernel is saying, (2) wiring a stdin-based HITL approval loop, and (3) adding robust change-data-capture file sync so container-side writes to WISDOM/journal/nodes survive container restarts.

**Architecture:**
- **Visibility:** one-line config change (`TEXT_PREVIEW_LEN`) so full agent messages flow to `orch_activity_log`. Terminal-based for now; a WebSocket/dashboard handler will replace the terminal path in Phase 2.
- **HITL:** isolated `StdinHitlHandler` class wired into `EventConsumer` via the existing `on_hitl_needed` callback. Inline/blocking is acceptable — it will be swapped for a dashboard handler later, so we do not over-engineer it.
- **File sync (CDC):** passive change-data-capture from `agent.tool_use` SSE events. When the Kernel calls `Write`/`Edit` on a tracked path (`WISDOM.md`, `journal/*.md`, `kernel/nodes/**/*.md`), the orchestrator extracts content/diff from the event payload and writes to `kernel_files_sync` via `db.sync_file()`. A scheduled `/sync-snapshot` trigger asks the Kernel to emit `` ```SYNC path=... ``` `` fences for current state; the orchestrator parses them from `agent.message` events as a reconciliation backstop. Edit is handled by applying the diff server-side against the cached content in `kernel_files_sync`.

**Tech Stack:** Python 3.10+, psycopg2, anthropic SDK, APScheduler, pytest 9.

**Architectural constraints respected:**
- Container must not speak directly to PostgreSQL (per `session_manager.py:19-25`).
- `kernel-files/CLAUDE.md` is protected (per `protect_core.py`). The `/sync-snapshot` protocol is injected via the orchestrator-owned bootstrap prompt (`session_manager.py:BOOTSTRAP_PROMPT`) instead.
- Axiom 1 (Observable State) is honored: CDC writes tag `synced_from="cdc"`, snapshot writes tag `synced_from="snapshot"`, divergences log an ERROR activity row.

---

## File Structure

**New files:**
- `orchestrator/hitl.py` — `StdinHitlHandler` class. One responsibility: take a tool_confirmation event, prompt the user on stdin, call a response callback.
- `orchestrator/file_sync.py` — path normalization, tracked-path matcher, Write CDC handler, Edit CDC handler with server-side diff apply, SYNC-fence parser, snapshot response handler. Pure functions + one small `FileSync` class holding a `db` reference for dependency-injection in tests.
- `orchestrator/tests/test_file_sync.py` — unit tests for every function in `file_sync.py`.
- `orchestrator/tests/test_hitl.py` — unit tests for `StdinHitlHandler` with mocked stdin.
- `orchestrator/tests/test_event_consumer_cdc.py` — integration test: fake `tool_use` event into `_handle_tool_use`, assert the right `file_sync` call was made.

**Modified files:**
- `orchestrator/event_consumer.py` — bump `TEXT_PREVIEW_LEN`, accept an optional `file_sync` dependency, call into it from `_handle_tool_use` and `_handle_message`.
- `orchestrator/__main__.py` — build `StdinHitlHandler`, build `FileSync`, pass both into `EventConsumer`, register sync-snapshot scheduler job.
- `orchestrator/scheduler.py` — add a `sync_snapshot_interval_hours` config with a new job that sends `/sync-snapshot` to the session.
- `orchestrator/session_manager.py` — extend `BOOTSTRAP_PROMPT` with the `/sync-snapshot` protocol block.
- `config.yaml` — add `scheduler.sync_snapshot_interval_hours` and (optionally) the tracked-path patterns.
- `requirements.txt` — add `pytest` as a dev dependency.

---

## Task 1: Visibility — bump `TEXT_PREVIEW_LEN`

**Files:**
- Modify: `orchestrator/event_consumer.py:28-29`

- [ ] **Step 1: Make the change**

Edit `orchestrator/event_consumer.py`:

```python
TEXT_PREVIEW_LEN = 10_000
INPUT_PREVIEW_LEN = 2_000
```

- [ ] **Step 2: Verify the module still imports**

Run: `python3 -c "from orchestrator.event_consumer import EventConsumer, TEXT_PREVIEW_LEN; assert TEXT_PREVIEW_LEN == 10_000"`
Expected: exit code 0, no output.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/event_consumer.py
git commit -m "chore(orchestrator): raise TEXT_PREVIEW_LEN to 10_000 for full visibility"
```

---

## Task 2: Add `pytest` to requirements

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Append pytest**

Edit `requirements.txt`, appending:

```
pytest>=8.0.0
```

- [ ] **Step 2: Commit**

```bash
git add requirements.txt
git commit -m "chore: add pytest as dev dependency"
```

---

## Task 3: HITL handler — failing test

**Files:**
- Create: `orchestrator/tests/test_hitl.py`

- [ ] **Step 1: Write the failing test**

Create `orchestrator/tests/test_hitl.py`:

```python
"""Tests for StdinHitlHandler."""
from types import SimpleNamespace

from orchestrator.hitl import StdinHitlHandler


def _make_event(tool_use_id="tu_123", tool_name="Write", raw_input=None):
    return SimpleNamespace(
        tool_use_id=tool_use_id,
        name=tool_name,
        input=raw_input or {"file_path": "/work/foo.md", "content": "hi"},
    )


def test_approves_when_user_answers_yes(monkeypatch, capsys):
    calls = []

    def fake_send(tool_use_id, approved, reason):
        calls.append((tool_use_id, approved, reason))

    answers = iter(["y", "looks fine"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    handler = StdinHitlHandler(send_response=fake_send)
    handler.handle(_make_event())

    assert calls == [("tu_123", True, "looks fine")]
    out = capsys.readouterr().out
    assert "HITL APPROVAL REQUESTED" in out
    assert "Write" in out


def test_denies_when_user_answers_no(monkeypatch):
    calls = []
    answers = iter(["n", "too risky"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    handler = StdinHitlHandler(send_response=lambda *a: calls.append(a))
    handler.handle(_make_event(tool_use_id="tu_999"))

    assert calls == [("tu_999", False, "too risky")]


def test_reprompts_on_invalid_answer(monkeypatch):
    calls = []
    answers = iter(["huh?", "y", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    handler = StdinHitlHandler(send_response=lambda *a: calls.append(a))
    handler.handle(_make_event())

    assert calls == [("tu_123", True, "")]


def test_eof_denies(monkeypatch):
    calls = []

    def raises_eof(_prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raises_eof)
    handler = StdinHitlHandler(send_response=lambda *a: calls.append(a))
    handler.handle(_make_event())

    assert calls == [("tu_123", False, "stdin closed")]
```

- [ ] **Step 2: Run the test — expect ImportError**

Run: `python3 -m pytest orchestrator/tests/test_hitl.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'orchestrator.hitl'`.

---

## Task 4: HITL handler — implementation

**Files:**
- Create: `orchestrator/hitl.py`

- [ ] **Step 1: Write the module**

Create `orchestrator/hitl.py`:

```python
"""Stdin-based HITL (Human-In-The-Loop) approval handler.

Receives a tool_confirmation event, prompts the operator on stdin, and
invokes a caller-supplied response callback. Designed to be swapped for
a WebSocket/dashboard handler in Phase 2 of the cloud spec — keep this
file small and single-purpose.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

SendResponse = Callable[[str, bool, str], None]


class StdinHitlHandler:
    """Prompt the operator on stdin to approve or deny a tool call.

    The handler is intentionally blocking: it is called from the SSE
    dispatch loop in EventConsumer. While a prompt is open, new events
    will queue server-side until the operator responds. This is fine
    for a single-operator terminal bridge and will be replaced by an
    async WebSocket handler when the dashboard integration lands.

    Parameters
    ----------
    send_response : callable
        ``send_response(tool_use_id, approved, reason)`` — typically
        ``SessionManager.send_tool_confirmation``.
    """

    def __init__(self, send_response: SendResponse):
        self.send_response = send_response

    def handle(self, event: Any) -> None:
        tool_use_id = getattr(event, "tool_use_id", None) or getattr(event, "id", "")
        tool_name = getattr(event, "name", "unknown")
        raw_input = getattr(event, "input", {})

        print("\n" + "=" * 60, flush=True)
        print("HITL APPROVAL REQUESTED", flush=True)
        print(f"Tool: {tool_name}", flush=True)
        print(f"Input: {raw_input}", flush=True)
        print("=" * 60, flush=True)

        while True:
            try:
                answer = input("Approve? [y/n]: ").strip().lower()
            except EOFError:
                logger.warning("EOF on stdin — defaulting to deny")
                self.send_response(tool_use_id, False, "stdin closed")
                return

            if answer in ("y", "yes"):
                try:
                    reason = input("Reason (optional): ").strip()
                except EOFError:
                    reason = ""
                self.send_response(tool_use_id, True, reason)
                return
            if answer in ("n", "no"):
                try:
                    reason = input("Reason: ").strip()
                except EOFError:
                    reason = ""
                self.send_response(tool_use_id, False, reason)
                return
            print("Please answer y or n.", flush=True)
```

- [ ] **Step 2: Run the tests — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_hitl.py -v`
Expected: 4 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/hitl.py orchestrator/tests/test_hitl.py
git commit -m "feat(orchestrator): add stdin-based HITL handler"
```

---

## Task 5: File sync — failing tests for path matching & normalization

**Files:**
- Create: `orchestrator/tests/test_file_sync.py`

- [ ] **Step 1: Write the failing tests**

Create `orchestrator/tests/test_file_sync.py`:

```python
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
```

- [ ] **Step 2: Run the tests — expect collection error**

Run: `python3 -m pytest orchestrator/tests/test_file_sync.py -v`
Expected: `ModuleNotFoundError: No module named 'orchestrator.file_sync'`.

---

## Task 6: File sync — minimal module (path + fences)

**Files:**
- Create: `orchestrator/file_sync.py`

- [ ] **Step 1: Write the minimal module**

Create `orchestrator/file_sync.py`:

```python
"""Change-data-capture file sync for ORA Kernel Cloud.

Watches agent tool_use events for writes to tracked paths (WISDOM.md,
journal entries, node specs) and mirrors them to the kernel_files_sync
table in postgres so the content survives ephemeral container restarts.

Design:
- Write tool calls contain full content in the event payload → store directly.
- Edit tool calls contain only a diff → apply it server-side against the
  cached content, store the result, log divergence if the diff can't apply.
- A scheduled /sync-snapshot trigger asks the Kernel to emit SYNC fences
  (``` ```SYNC path=... ``` ```) so we can reconcile anything CDC missed.
"""
from __future__ import annotations

import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)

TRACKED_PREFIXES: Tuple[str, ...] = (
    ".claude/kernel/journal/",
    ".claude/kernel/nodes/",
)

_SYNC_FENCE_RE = re.compile(
    r"```SYNC\s+path=(?P<path>\S+)\s*\n(?P<body>.*?)(?:\n)?```",
    re.DOTALL,
)


# ── Path helpers ────────────────────────────────────────────────────

def normalize_path(file_path: str) -> str:
    """Return a relative path for storage in kernel_files_sync.

    The container uses /work as its root, so /work/.claude/... becomes
    .claude/... for storage. Already-relative paths pass through.
    """
    if not file_path:
        return ""
    if file_path.startswith("/work/"):
        return file_path[len("/work/"):]
    if file_path == "/work":
        return ""
    if file_path.startswith("/"):
        return file_path.lstrip("/")
    return file_path


def is_tracked(normalized_path: str) -> bool:
    """Return True if ``normalized_path`` should be mirrored to postgres."""
    if not normalized_path.endswith(".md"):
        return False
    return any(normalized_path.startswith(p) for p in TRACKED_PREFIXES)


# ── SYNC fence parsing ──────────────────────────────────────────────

def parse_sync_fences(text: str) -> List[Tuple[str, str]]:
    """Extract (path, content) pairs from ```SYNC path=...``` fences."""
    results: List[Tuple[str, str]] = []
    for match in _SYNC_FENCE_RE.finditer(text):
        path = match.group("path").strip()
        body = match.group("body").rstrip("\n")
        if path:
            results.append((path, body))
    return results


# ── FileSync facade (stateful wrapper around db) ────────────────────

class FileSync:
    """Thin façade over ``Database`` that implements the CDC + snapshot flow."""

    def __init__(self, db):
        self.db = db
```

- [ ] **Step 2: Run the tests — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_file_sync.py -v`
Expected: 12 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/file_sync.py orchestrator/tests/test_file_sync.py
git commit -m "feat(orchestrator): scaffold file_sync module (paths + fences)"
```

---

## Task 7: File sync — failing tests for Write CDC

**Files:**
- Modify: `orchestrator/tests/test_file_sync.py`

- [ ] **Step 1: Append Write CDC tests**

Append to `orchestrator/tests/test_file_sync.py`:

```python
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
```

- [ ] **Step 2: Run — expect 4 new failures**

Run: `python3 -m pytest orchestrator/tests/test_file_sync.py -v`
Expected: 4 failures (`AttributeError: 'FileSync' object has no attribute 'handle_write'`).

---

## Task 8: File sync — implement Write CDC

**Files:**
- Modify: `orchestrator/file_sync.py`

- [ ] **Step 1: Add `handle_write`**

Append to `FileSync` in `orchestrator/file_sync.py`:

```python
    def handle_write(self, file_path: str, content: str) -> bool:
        """Mirror a Write tool call to kernel_files_sync.

        Returns True if the file was synced, False if it was filtered
        out (untracked path or empty path).
        """
        normalized = normalize_path(file_path)
        if not normalized or not is_tracked(normalized):
            return False
        self.db.sync_file(normalized, content or "", synced_from="cdc")
        logger.debug("cdc write synced: %s (%d bytes)", normalized, len(content or ""))
        return True
```

- [ ] **Step 2: Run — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_file_sync.py -v`
Expected: 16 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/file_sync.py orchestrator/tests/test_file_sync.py
git commit -m "feat(file_sync): CDC handler for Write tool calls"
```

---

## Task 9: File sync — failing tests for Edit CDC (diff apply)

**Files:**
- Modify: `orchestrator/tests/test_file_sync.py`

- [ ] **Step 1: Append Edit tests**

Append to `orchestrator/tests/test_file_sync.py`:

```python
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
```

- [ ] **Step 2: Run — expect 5 new failures**

Run: `python3 -m pytest orchestrator/tests/test_file_sync.py -v`
Expected: 5 failures (`AttributeError: ... 'handle_edit'`).

---

## Task 10: File sync — implement Edit CDC

**Files:**
- Modify: `orchestrator/file_sync.py`

- [ ] **Step 1: Add `handle_edit`**

Append to `FileSync` in `orchestrator/file_sync.py`:

```python
    def handle_edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
    ) -> bool:
        """Apply an Edit tool diff against cached content and resync.

        Returns True on successful apply, False if filtered, missing,
        or divergent. Divergences are logged to orch_activity_log so
        they are observable (Axiom 1).
        """
        normalized = normalize_path(file_path)
        if not normalized or not is_tracked(normalized):
            return False

        cached = self.db.get_synced_file(normalized)
        if cached is None:
            logger.warning("cdc edit has no cached base: %s", normalized)
            self.db.log_activity(
                session_id=None,
                agent_id=None,
                level="WARNING",
                event_source="file_sync",
                action="CDC_MISSING_BASE",
                details={"file_path": normalized},
            )
            return False

        if old_string not in cached:
            logger.error("cdc edit divergence on %s: old_string absent", normalized)
            self.db.log_activity(
                session_id=None,
                agent_id=None,
                level="WARNING",
                event_source="file_sync",
                action="CDC_DIVERGENCE",
                details={
                    "file_path": normalized,
                    "reason": "old_string not in cached content",
                },
            )
            return False

        updated = cached.replace(old_string, new_string, 1)
        self.db.sync_file(normalized, updated, synced_from="cdc")
        logger.debug("cdc edit applied: %s", normalized)
        return True
```

- [ ] **Step 2: Run — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_file_sync.py -v`
Expected: 21 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/file_sync.py orchestrator/tests/test_file_sync.py
git commit -m "feat(file_sync): CDC Edit handler with server-side diff apply"
```

---

## Task 11: File sync — failing tests for snapshot response

**Files:**
- Modify: `orchestrator/tests/test_file_sync.py`

- [ ] **Step 1: Append snapshot tests**

Append to `orchestrator/tests/test_file_sync.py`:

```python
# ── FileSync.handle_snapshot_response ───────────────────────────────

def test_snapshot_syncs_tracked_fences():
    db = _make_db()
    fs = FileSync(db)
    text = (
        "Snapshot follows.\n"
        "```SYNC path=.claude/kernel/journal/WISDOM.md\nfresh wisdom\n```\n"
        "```SYNC path=.claude/kernel/journal/2026-04-10.md\ntoday\n```\n"
    )

    count = fs.handle_snapshot_response(text)

    assert count == 2
    db.sync_file.assert_any_call(
        ".claude/kernel/journal/WISDOM.md", "fresh wisdom", synced_from="snapshot"
    )
    db.sync_file.assert_any_call(
        ".claude/kernel/journal/2026-04-10.md", "today", synced_from="snapshot"
    )


def test_snapshot_ignores_untracked_fence_paths():
    db = _make_db()
    fs = FileSync(db)
    text = "```SYNC path=CLAUDE.md\nshould not sync\n```"

    count = fs.handle_snapshot_response(text)

    assert count == 0
    db.sync_file.assert_not_called()


def test_snapshot_no_fences_returns_zero():
    db = _make_db()
    fs = FileSync(db)
    assert fs.handle_snapshot_response("just normal prose") == 0
    db.sync_file.assert_not_called()
```

- [ ] **Step 2: Run — expect 3 failures**

Run: `python3 -m pytest orchestrator/tests/test_file_sync.py -v`
Expected: 3 failures.

---

## Task 12: File sync — implement snapshot response

**Files:**
- Modify: `orchestrator/file_sync.py`

- [ ] **Step 1: Add `handle_snapshot_response`**

Append to `FileSync` in `orchestrator/file_sync.py`:

```python
    def handle_snapshot_response(self, message_text: str) -> int:
        """Parse ```SYNC``` fences in an agent message and sync tracked ones.

        Returns the number of files written to kernel_files_sync.
        """
        if not message_text:
            return 0
        synced = 0
        for raw_path, content in parse_sync_fences(message_text):
            normalized = normalize_path(raw_path)
            if not is_tracked(normalized):
                logger.debug("snapshot fence ignored (untracked): %s", raw_path)
                continue
            self.db.sync_file(normalized, content, synced_from="snapshot")
            synced += 1
        if synced:
            logger.info("snapshot sync: %d file(s) mirrored", synced)
        return synced
```

- [ ] **Step 2: Run — expect all pass**

Run: `python3 -m pytest orchestrator/tests/test_file_sync.py -v`
Expected: 24 passed.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/file_sync.py orchestrator/tests/test_file_sync.py
git commit -m "feat(file_sync): snapshot reconciliation from SYNC fences"
```

---

## Task 13: Wire file_sync into EventConsumer — failing test

**Files:**
- Create: `orchestrator/tests/test_event_consumer_cdc.py`

- [ ] **Step 1: Write the failing test**

Create `orchestrator/tests/test_event_consumer_cdc.py`:

```python
"""Integration tests: EventConsumer routes file ops to FileSync."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.event_consumer import EventConsumer


def _consumer(file_sync):
    db = MagicMock()
    db.log_activity = MagicMock()
    return EventConsumer(
        db=db,
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
        file_sync=file_sync,
    )


def _write_event(file_path, content):
    return SimpleNamespace(
        type="agent.tool_use",
        name="Write",
        input={"file_path": file_path, "content": content},
    )


def _edit_event(file_path, old, new):
    return SimpleNamespace(
        type="agent.tool_use",
        name="Edit",
        input={"file_path": file_path, "old_string": old, "new_string": new},
    )


def _message_event(text):
    return SimpleNamespace(
        type="agent.message",
        content=[SimpleNamespace(text=text)],
    )


def test_write_event_invokes_file_sync():
    fs = MagicMock()
    consumer = _consumer(fs)

    event = _write_event("/work/.claude/kernel/journal/WISDOM.md", "body")
    consumer._handle_tool_use("sess_1", event)

    fs.handle_write.assert_called_once_with(
        "/work/.claude/kernel/journal/WISDOM.md", "body"
    )
    fs.handle_edit.assert_not_called()


def test_edit_event_invokes_file_sync():
    fs = MagicMock()
    consumer = _consumer(fs)

    event = _edit_event(
        "/work/.claude/kernel/journal/WISDOM.md", "a", "b"
    )
    consumer._handle_tool_use("sess_1", event)

    fs.handle_edit.assert_called_once_with(
        "/work/.claude/kernel/journal/WISDOM.md", "a", "b"
    )
    fs.handle_write.assert_not_called()


def test_other_tool_does_not_invoke_file_sync():
    fs = MagicMock()
    consumer = _consumer(fs)

    event = SimpleNamespace(
        type="agent.tool_use",
        name="Bash",
        input={"command": "ls"},
    )
    consumer._handle_tool_use("sess_1", event)

    fs.handle_write.assert_not_called()
    fs.handle_edit.assert_not_called()


def test_message_event_invokes_snapshot_parser():
    fs = MagicMock()
    consumer = _consumer(fs)

    event = _message_event("```SYNC path=.claude/kernel/journal/WISDOM.md\nhi\n```")
    consumer._handle_message("sess_1", event)

    fs.handle_snapshot_response.assert_called_once()
    call_text = fs.handle_snapshot_response.call_args.args[0]
    assert "SYNC path=" in call_text


def test_file_sync_is_optional():
    # No file_sync passed — should not crash on tool_use or message.
    consumer = EventConsumer(
        db=MagicMock(),
        api_key="sk-test",
        agent_id="agent_test",
        environment_id="env_test",
    )
    consumer._handle_tool_use(
        "sess_1",
        _write_event("/work/.claude/kernel/journal/WISDOM.md", "x"),
    )
    consumer._handle_message("sess_1", _message_event("no fences"))
```

- [ ] **Step 2: Run — expect collection error**

Run: `python3 -m pytest orchestrator/tests/test_event_consumer_cdc.py -v`
Expected: `TypeError: EventConsumer.__init__() got an unexpected keyword argument 'file_sync'`.

---

## Task 14: Wire file_sync into EventConsumer — implementation

**Files:**
- Modify: `orchestrator/event_consumer.py`

- [ ] **Step 1: Extend `__init__` signature**

In `orchestrator/event_consumer.py`, update the `EventConsumer.__init__` signature:

```python
    def __init__(
        self,
        db: Database,
        api_key: str,
        agent_id: str,
        environment_id: str,
        on_event: Optional[Callable[[Any], None]] = None,
        on_hitl_needed: Optional[Callable[[Any], None]] = None,
        file_sync: Optional["FileSync"] = None,
    ):
        self.db = db
        self.client = Anthropic(api_key=api_key)
        self.agent_id = agent_id
        self.environment_id = environment_id
        self.on_event = on_event
        self.on_hitl_needed = on_hitl_needed
        self.file_sync = file_sync
        self.totals = SessionTotals()
```

Add this import near the top of the file (use `TYPE_CHECKING` to avoid a cycle):

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from orchestrator.file_sync import FileSync
```

- [ ] **Step 2: Route Write/Edit in `_handle_tool_use`**

Replace the body of `_handle_tool_use` in `orchestrator/event_consumer.py` with:

```python
    def _handle_tool_use(self, session_id: str, event: Any) -> None:
        tool_name = getattr(event, "name", "unknown")
        raw_input = getattr(event, "input", {})
        input_preview = _truncate(
            json.dumps(raw_input, default=str) if isinstance(raw_input, dict) else str(raw_input),
            INPUT_PREVIEW_LEN,
        )
        self.db.log_activity(
            session_id=session_id,
            agent_id=self.agent_id,
            level="INFO",
            event_source="sse",
            action="TOOL_USE",
            details={"tool_name": tool_name, "input": input_preview},
        )

        # CDC file sync from tool_use events
        if self.file_sync is not None and isinstance(raw_input, dict):
            if tool_name == "Write":
                self.file_sync.handle_write(
                    raw_input.get("file_path", ""),
                    raw_input.get("content", ""),
                )
            elif tool_name == "Edit":
                self.file_sync.handle_edit(
                    raw_input.get("file_path", ""),
                    raw_input.get("old_string", ""),
                    raw_input.get("new_string", ""),
                )

        # Detect HITL confirmation requests
        if tool_name == "tool_confirmation" and self.on_hitl_needed is not None:
            self.on_hitl_needed(event)
```

- [ ] **Step 3: Route `_handle_message` to snapshot parser**

Replace the body of `_handle_message`:

```python
    def _handle_message(self, session_id: str, event: Any) -> None:
        text_parts = []
        for block in getattr(event, "content", []):
            if hasattr(block, "text"):
                text_parts.append(block.text)
        full_text = " ".join(text_parts)
        preview = _truncate(full_text, TEXT_PREVIEW_LEN) if full_text else ""
        self.db.log_activity(
            session_id=session_id,
            agent_id=self.agent_id,
            level="INFO",
            event_source="sse",
            action="MESSAGE",
            details={"text": preview},
        )
        if self.file_sync is not None and full_text:
            try:
                self.file_sync.handle_snapshot_response(full_text)
            except Exception:
                logger.exception("file_sync snapshot handler failed")
```

- [ ] **Step 4: Run all event_consumer tests — expect pass**

Run: `python3 -m pytest orchestrator/tests/test_event_consumer_cdc.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full test suite — make sure nothing else broke**

Run: `python3 -m pytest orchestrator/tests/ -v`
Expected: all tests green (29 passed so far).

- [ ] **Step 6: Commit**

```bash
git add orchestrator/event_consumer.py orchestrator/tests/test_event_consumer_cdc.py
git commit -m "feat(event_consumer): route Write/Edit/messages to file_sync"
```

---

## Task 15: Extend bootstrap prompt with `/sync-snapshot` protocol

**Files:**
- Modify: `orchestrator/session_manager.py:19-64`

- [ ] **Step 1: Update `BOOTSTRAP_PROMPT`**

In `orchestrator/session_manager.py`, replace the `BOOTSTRAP_PROMPT` constant with:

```python
BOOTSTRAP_PROMPT = """Bootstrap: Set up the ORA Kernel workspace.

IMPORTANT: You are running inside a cloud container. You do NOT have
direct network access to the user's local PostgreSQL database. All
postgres writes are handled by the orchestrator on the user's machine
which consumes the event stream. You do not need to connect to postgres.

Steps:

1. Change to /work directory:
   cd /work

2. Clone the ORA Kernel repo (explicit command — do not skip):
   git clone {repo_url} /work/ora-kernel

3. Verify the clone worked — check for key files:
   ls /work/ora-kernel/kernel-files/CLAUDE.md
   ls /work/ora-kernel/kernel-files/.claude/kernel/

4. Copy the kernel files into /work so they're at the expected paths:
   cp /work/ora-kernel/kernel-files/CLAUDE.md /work/CLAUDE.md
   cp -r /work/ora-kernel/kernel-files/.claude /work/.claude
   cp /work/ora-kernel/kernel-files/PROJECT_DNA.md /work/PROJECT_DNA.md

5. Verify the workspace is set up:
   ls /work/.claude/kernel/nodes/system/
   ls /work/.claude/kernel/journal/

6. Read your operating instructions:
   cat /work/CLAUDE.md

7. Report ready status with a brief summary:
   - Number of node spec files found in .claude/kernel/nodes/
   - Whether WISDOM.md exists (it may be empty on first boot)
   - Confirmation that you have loaded the Constitution and axioms

After bootstrap, you will receive periodic triggers (/heartbeat, /briefing,
/idle-work, /consolidate, /sync-snapshot) from the user's scheduler.
Respond to them per your operating instructions in CLAUDE.md.

=== /sync-snapshot protocol ===

When you receive /sync-snapshot, the orchestrator is asking you to emit a
reconciliation snapshot of your operational-memory files so their contents
are persisted to postgres outside the ephemeral container. This is how
WISDOM.md and journal entries survive container restarts.

Your response MUST contain fenced blocks in this exact form:

```SYNC path=<relative path from /work>
<full current file contents>
```

Emit one fenced block per file. Include these files if they exist:
- .claude/kernel/journal/WISDOM.md
- Today's journal entry (.claude/kernel/journal/YYYY-MM-DD.md)

If a file does not exist, omit its block — do not emit empty blocks.
Do not emit ```SYNC blocks for any other files. Do not modify file content
during the snapshot — read and echo it verbatim. No explanatory prose is
required; the orchestrator only parses the fenced blocks.

=== end /sync-snapshot protocol ===

DO NOT attempt to contact a PostgreSQL database — that is handled by the
orchestrator outside the container via the event stream. Your job is to
reason and act; the orchestrator records everything.

{hydration_instructions}
"""
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `python3 -c "from orchestrator.session_manager import BOOTSTRAP_PROMPT; assert '/sync-snapshot' in BOOTSTRAP_PROMPT"`
Expected: exit code 0.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/session_manager.py
git commit -m "feat(session_manager): document /sync-snapshot protocol in bootstrap"
```

---

## Task 16: Scheduler — add sync-snapshot job

**Files:**
- Modify: `orchestrator/scheduler.py`
- Modify: `config.yaml`

- [ ] **Step 1: Add config default**

In `orchestrator/scheduler.py`, add to `_DEFAULTS`:

```python
_DEFAULTS = {
    "heartbeat_interval_hours": 2,
    "briefing_time": "08:00",
    "idle_work_hours": [20, 0, 4],
    "consolidation_day": "sunday",
    "consolidation_time": "03:00",
    "sync_snapshot_interval_hours": 6,
}
```

- [ ] **Step 2: Read the config in `__init__`**

In `KernelScheduler.__init__`, below the existing config reads, add:

```python
        self.sync_snapshot_interval = sched_cfg.get(
            "sync_snapshot_interval_hours",
            _DEFAULTS["sync_snapshot_interval_hours"],
        )
```

- [ ] **Step 3: Register the job in `start()`**

In `KernelScheduler.start`, after `self._add_consolidation_job()`, add:

```python
        self._add_sync_snapshot_job()
```

Then add the method near the other `_add_*_job` methods:

```python
    def _add_sync_snapshot_job(self) -> None:
        """Every N hours, ask the Kernel to emit a SYNC reconciliation snapshot."""
        self._scheduler.add_job(
            func=self.send_trigger,
            trigger=IntervalTrigger(hours=self.sync_snapshot_interval),
            args=["/sync-snapshot"],
            id="sync-snapshot",
            name="sync-snapshot",
        )
```

- [ ] **Step 4: Add the config entry**

Edit `config.yaml`, under `scheduler:`:

```yaml
scheduler:
  heartbeat_interval_hours: 2
  briefing_time: "08:00"
  idle_work_hours: [20, 0, 4]  # 8pm, midnight, 4am
  consolidation_day: "sunday"
  consolidation_time: "03:00"
  sync_snapshot_interval_hours: 6
```

- [ ] **Step 5: Smoke check — scheduler constructs without error**

Run: `python3 -c "from orchestrator.scheduler import KernelScheduler; s = KernelScheduler('sk-test', 'sess_test', {'scheduler': {}}); print('ok')"`
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/scheduler.py config.yaml
git commit -m "feat(scheduler): periodic /sync-snapshot trigger for CDC reconciliation"
```

---

## Task 17: Wire everything into `__main__.py`

**Files:**
- Modify: `orchestrator/__main__.py`

- [ ] **Step 1: Add imports**

Near the top of `orchestrator/__main__.py`, add:

```python
from orchestrator.file_sync import FileSync
from orchestrator.hitl import StdinHitlHandler
```

- [ ] **Step 2: Build `FileSync` and `StdinHitlHandler` before the consumer**

In `orchestrator/__main__.py`, replace the line that creates the consumer:

```python
    # Event consumer
    consumer = EventConsumer(db=db, api_key=api_key, agent_id=agent_id, environment_id=env_id)
```

with:

```python
    # File sync (change-data-capture + snapshot reconciliation)
    file_sync = FileSync(db)

    # HITL handler — stdin prompt, hot-swappable for dashboard later
    hitl = StdinHitlHandler(send_response=session_mgr.send_tool_confirmation)

    # Event consumer
    consumer = EventConsumer(
        db=db,
        api_key=api_key,
        agent_id=agent_id,
        environment_id=env_id,
        on_hitl_needed=hitl.handle,
        file_sync=file_sync,
    )
```

- [ ] **Step 3: Update the restart path so the new consumer keeps the same wiring**

In `orchestrator/__main__.py`, inside the `while running:` loop, replace:

```python
                    consumer = EventConsumer(db=db, api_key=api_key, agent_id=agent_id, environment_id=env_id)
```

with:

```python
                    consumer = EventConsumer(
                        db=db,
                        api_key=api_key,
                        agent_id=agent_id,
                        environment_id=env_id,
                        on_hitl_needed=hitl.handle,
                        file_sync=file_sync,
                    )
```

- [ ] **Step 4: Import-check the module**

Run: `python3 -c "import orchestrator.__main__"`
Expected: exit code 0 (no import errors).

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest orchestrator/tests/ -v`
Expected: all tests green.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/__main__.py
git commit -m "feat(orchestrator): wire HITL handler and file_sync into main loop"
```

---

## Task 18: End-to-end smoke test (manual)

This task is not runnable as a script; it is a manual checklist for the operator.

- [ ] **Step 1: Start the orchestrator**

Run: `python3 -m orchestrator`
Expected: session created (or resumed), bootstrap event sent, "ORA Kernel Cloud — Running" banner appears.

- [ ] **Step 2: Verify full messages reach postgres**

In a second terminal, run:

```
psql -d ora_kernel -c "SELECT details->'text' FROM orch_activity_log WHERE action='MESSAGE' ORDER BY id DESC LIMIT 3;"
```

Expected: full agent messages up to 10_000 chars, not truncated at 200.

- [ ] **Step 3: Send a trivial task that writes WISDOM**

Run: `python3 -m orchestrator --send "Please add a one-line test entry to .claude/kernel/journal/WISDOM.md that says 'cdc smoke test'. Use the Write tool with the full file content."`

- [ ] **Step 4: Verify CDC captured the write**

```
psql -d ora_kernel -c "SELECT file_path, synced_from, length(content), updated_at FROM kernel_files_sync WHERE file_path='.claude/kernel/journal/WISDOM.md';"
```

Expected: one row with `synced_from='cdc'` and recent `updated_at`.

- [ ] **Step 5: Trigger a snapshot**

Run: `python3 -m orchestrator --send "/sync-snapshot"`
Wait ~30s for the Kernel to respond.

- [ ] **Step 6: Verify snapshot wrote at least one `synced_from='snapshot'` row**

```
psql -d ora_kernel -c "SELECT file_path, synced_from, updated_at FROM kernel_files_sync ORDER BY updated_at DESC LIMIT 5;"
```

Expected: at least one row with `synced_from='snapshot'`.

- [ ] **Step 7: Exercise the HITL prompt**

Provoke a tool that will require confirmation (e.g. a protected file write). When the terminal displays `HITL APPROVAL REQUESTED`, answer `n` + a reason. Confirm the session proceeds.

- [ ] **Step 8: Record results**

If every step above passed, the Track 1 bridge is operational. Note any observations (missed paths, divergence warnings, prompt weaknesses) for follow-up tasks — especially anything that would shape the dashboard integration work.

---

## Self-Review Notes

- Every spec requirement from the conversation is covered: visibility (Task 1), HITL (Tasks 3–4, 17), CDC Write (Tasks 7–8), CDC Edit (Tasks 9–10), snapshot reconciliation (Tasks 11–12, 15, 16), wiring (Tasks 14, 17), smoke test (Task 18).
- No placeholders, TBDs, or "similar to above" shortcuts.
- Type/naming consistency: `FileSync.handle_write`, `handle_edit`, `handle_snapshot_response` are used identically across the `file_sync`, `event_consumer`, and test files. `StdinHitlHandler.handle` takes an event; `send_response(tool_use_id, approved, reason)` matches the existing `SessionManager.send_tool_confirmation` signature at `session_manager.py:200`.
- CLAUDE.md is respected — the `/sync-snapshot` protocol is taught via the orchestrator-owned bootstrap prompt, not by editing the protected `kernel-files/CLAUDE.md`.
- Commit cadence: one commit per landed task, consistent with the project's existing small-fix commit style.
