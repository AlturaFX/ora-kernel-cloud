"""Scheduled trigger dispatcher for ORA Kernel Cloud.

Replaces the cron-based inbox.jsonl triggers from the base ORA Kernel
with API calls that send user.message events to a Managed Agent session.
"""

import logging
from datetime import datetime

from anthropic import Anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from orchestrator.config import load_config, get_api_key
from orchestrator.session_manager import SYNC_SNAPSHOT_PROTOCOL

logger = logging.getLogger(__name__)

# Defaults used when config keys are missing
_DEFAULTS = {
    "heartbeat_interval_hours": 2,
    "briefing_time": "08:00",
    "idle_work_hours": [20, 0, 4],
    "consolidation_day": "sunday",
    "consolidation_time": "03:00",
    "sync_snapshot_interval_hours": 6,
}

# Full trigger message for the /sync-snapshot cron job. We send the
# protocol inline on every firing so resumed sessions (bootstrapped
# before the protocol existed) still comply — bootstrap is sent once
# at session creation and we cannot rely on it being present.
SYNC_SNAPSHOT_TRIGGER = (
    "/sync-snapshot\n\n"
    "Respond to this trigger per the protocol below. Do not include any\n"
    "prose outside the fenced blocks.\n\n"
    f"{SYNC_SNAPSHOT_PROTOCOL}"
)


class KernelScheduler:
    """Sends recurring triggers to a Managed Agent session via the Anthropic API."""

    def __init__(self, api_key: str, session_id: str, config: dict = None):
        self.api_key = api_key
        self.session_id = session_id
        self.client = Anthropic(api_key=api_key)

        sched_cfg = (config or {}).get("scheduler", {})
        self.heartbeat_interval = sched_cfg.get(
            "heartbeat_interval_hours", _DEFAULTS["heartbeat_interval_hours"]
        )
        self.briefing_time = sched_cfg.get(
            "briefing_time", _DEFAULTS["briefing_time"]
        )
        self.idle_work_hours = sched_cfg.get(
            "idle_work_hours", _DEFAULTS["idle_work_hours"]
        )
        self.consolidation_day = sched_cfg.get(
            "consolidation_day", _DEFAULTS["consolidation_day"]
        )
        self.consolidation_time = sched_cfg.get(
            "consolidation_time", _DEFAULTS["consolidation_time"]
        )
        self.sync_snapshot_interval = sched_cfg.get(
            "sync_snapshot_interval_hours",
            _DEFAULTS["sync_snapshot_interval_hours"],
        )

        self._scheduler = BackgroundScheduler()
        self._running = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register all scheduled jobs and start the background scheduler."""
        self._add_heartbeat_job()
        self._add_briefing_job()
        self._add_idle_work_jobs()
        self._add_consolidation_job()
        self._add_sync_snapshot_job()

        self._scheduler.start()
        self._running = True
        self._log("Scheduler started")

    def stop(self) -> None:
        """Shut down the background scheduler gracefully."""
        if self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            self._log("Scheduler stopped")

    def send_trigger(self, message: str) -> None:
        """Send *message* as a user.message event to the managed session.

        Logs the trigger and silently handles API errors so the
        scheduler keeps running even if the session is unavailable.
        """
        self._log(f"Sending trigger: {message}")
        try:
            self.client.beta.sessions.events.send(
                self.session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": message}],
                    }
                ],
            )
            self._log(f"Trigger sent successfully: {message}")
        except Exception as exc:
            logger.warning(
                "Failed to send trigger %r to session %s: %s",
                message,
                self.session_id,
                exc,
            )
            self._log(f"WARNING: Failed to send trigger {message!r} — {exc}")

    def send_now(self, message: str) -> None:
        """Send a manual / on-demand trigger (e.g. from a dashboard button)."""
        self.send_trigger(message)

    # ------------------------------------------------------------------
    # Job registration (private)
    # ------------------------------------------------------------------

    def _add_heartbeat_job(self) -> None:
        """Every N hours, weekdays 8am-6pm."""
        self._scheduler.add_job(
            func=self.send_trigger,
            trigger=IntervalTrigger(hours=self.heartbeat_interval),
            args=["/heartbeat"],
            id="heartbeat",
            name="heartbeat",
            # APScheduler jitter keeps things human-ish; the CronTrigger
            # wrapper below enforces work-hour boundaries.
            next_run_time=None,  # don't fire immediately on start
        )
        # Replace with a cron trigger that limits to work hours on weekdays.
        self._scheduler.reschedule_job(
            "heartbeat",
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=f"8-17/{self.heartbeat_interval}",
                minute=0,
            ),
        )

    def _add_briefing_job(self) -> None:
        """Daily at the configured briefing time."""
        hour, minute = (int(p) for p in self.briefing_time.split(":"))
        self._scheduler.add_job(
            func=self.send_trigger,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=["/briefing"],
            id="briefing",
            name="briefing",
        )

    def _add_idle_work_jobs(self) -> None:
        """Trigger /idle-work at each configured off-hour."""
        for idx, hour in enumerate(self.idle_work_hours):
            self._scheduler.add_job(
                func=self.send_trigger,
                trigger=CronTrigger(hour=hour, minute=0),
                args=["/idle-work"],
                id=f"idle-work-{idx}",
                name=f"idle-work-{hour:02d}:00",
            )

    def _add_consolidation_job(self) -> None:
        """Weekly consolidation on the configured day/time."""
        hour, minute = (int(p) for p in self.consolidation_time.split(":"))
        self._scheduler.add_job(
            func=self.send_trigger,
            trigger=CronTrigger(
                day_of_week=self.consolidation_day[:3].lower(),
                hour=hour,
                minute=minute,
            ),
            args=["/consolidate"],
            id="consolidation",
            name="consolidation",
        )

    def _add_sync_snapshot_job(self) -> None:
        """Every N hours, ask the Kernel to emit a SYNC reconciliation snapshot.

        The trigger payload carries the full SYNC protocol inline so this
        works on resumed sessions that were bootstrapped before the
        protocol existed.
        """
        self._scheduler.add_job(
            func=self.send_trigger,
            trigger=IntervalTrigger(hours=self.sync_snapshot_interval),
            args=[SYNC_SNAPSHOT_TRIGGER],
            id="sync-snapshot",
            name="sync-snapshot",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _log(message: str) -> None:
        """Print a timestamped log line to stdout and the module logger."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[KernelScheduler {ts}] {message}"
        print(line)
        logger.info(message)
