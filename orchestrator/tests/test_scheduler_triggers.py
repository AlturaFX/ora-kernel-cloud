"""Tests for KernelScheduler trigger message construction.

These tests cover the *content* of the trigger messages registered as
APScheduler jobs — not the scheduling itself. The goal is to pin
behavior that matters for resume semantics: the /sync-snapshot trigger
must carry the full SYNC protocol inline so it works even when the
session was bootstrapped before the protocol existed.
"""
from __future__ import annotations

from orchestrator.scheduler import (
    KernelScheduler,
    SYNC_SNAPSHOT_TRIGGER,
)
from orchestrator.session_manager import SYNC_SNAPSHOT_PROTOCOL


def test_sync_snapshot_protocol_constant_shape():
    """The protocol constant must describe the fence format the
    orchestrator's parse_sync_fences actually accepts."""
    assert "```SYNC path=" in SYNC_SNAPSHOT_PROTOCOL
    assert ".claude/kernel/journal/WISDOM.md" in SYNC_SNAPSHOT_PROTOCOL
    # Must tell the Kernel to omit missing files rather than emit empty blocks
    assert "omit" in SYNC_SNAPSHOT_PROTOCOL.lower()


def test_sync_snapshot_trigger_carries_command_and_protocol():
    """The trigger message sent on every /sync-snapshot cron firing must
    contain both the command itself and the full protocol, so resumed
    sessions (which didn't see the bootstrap prompt) still comply."""
    assert "/sync-snapshot" in SYNC_SNAPSHOT_TRIGGER
    assert "```SYNC path=" in SYNC_SNAPSHOT_TRIGGER
    # Sanity: if someone ever reduces the trigger to a bare /sync-snapshot,
    # this should fail loudly — that's the regression we're guarding.
    assert len(SYNC_SNAPSHOT_TRIGGER) > 200


def test_scheduler_registers_sync_snapshot_with_full_trigger_message():
    """_add_sync_snapshot_job must register the rich SYNC_SNAPSHOT_TRIGGER
    message as its job args, not the bare '/sync-snapshot' command."""
    scheduler = KernelScheduler("sk-test", "sess_test", {"scheduler": {}})
    # Call the registration method directly without starting the scheduler.
    scheduler._add_sync_snapshot_job()
    job = scheduler._scheduler.get_job("sync-snapshot")
    assert job is not None
    assert job.args == (SYNC_SNAPSHOT_TRIGGER,)


def test_bootstrap_prompt_still_embeds_protocol():
    """Refactoring the protocol into a constant must not break the
    bootstrap prompt — new sessions still need to learn it at boot."""
    from orchestrator.session_manager import BOOTSTRAP_PROMPT

    # BOOTSTRAP_PROMPT is a template that gets .format()ed at send time,
    # so we format it with dummy values before asserting.
    rendered = BOOTSTRAP_PROMPT.format(
        repo_url="https://example.com/repo.git",
        sync_snapshot_protocol=SYNC_SNAPSHOT_PROTOCOL,
        hydration_instructions="",
    )
    assert "```SYNC path=" in rendered
    assert "/sync-snapshot" in rendered
