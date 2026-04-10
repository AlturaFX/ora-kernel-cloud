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
