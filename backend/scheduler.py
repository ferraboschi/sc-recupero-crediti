"""APScheduler integration for scheduled jobs."""

import logging
from datetime import datetime
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from backend.config import config

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional[BackgroundScheduler] = None
_scheduler_started = False

# Track last sync times
_last_sync = {
    "invoices": None,
    "customers": None,
    "matching": None,
    "escalations": None,
}


def run_daily_job(manual: bool = False):
    """
    Daily job that runs the full sequential sync + autopilot.

    Pipeline: invoices → customers → matching → escalations → AUTOPILOT
    The autopilot generates AI messages and sends them via Twilio.

    Args:
        manual: Whether this is a manual trigger (vs scheduled)

    Returns:
        Dictionary with job results
    """
    from backend.api.sync import _full_sync_task

    logger.info(f"Starting daily job (manual={manual})")

    try:
        results = _full_sync_task()
        results["timestamp"] = datetime.utcnow().isoformat()
    except Exception as e:
        logger.error(f"Error in daily job: {e}", exc_info=True)
        results = {"error": str(e), "timestamp": datetime.utcnow().isoformat()}

    # Run autopilot after sync (generate and send messages)
    try:
        import os
        if os.getenv("AUTOPILOT_ENABLED", "false").lower() == "true":
            from backend.engine.autopilot import run_autopilot
            autopilot_result = run_autopilot()
            results["autopilot"] = autopilot_result
            logger.info(f"Autopilot result: {autopilot_result}")
        else:
            results["autopilot"] = {"status": "disabled"}
            logger.info("Autopilot disabled (set AUTOPILOT_ENABLED=true to enable)")
    except Exception as e:
        logger.error(f"Autopilot error: {e}", exc_info=True)
        results["autopilot"] = {"error": str(e)}

    # Update last sync times
    now = datetime.utcnow().isoformat()
    _last_sync["invoices"] = now
    _last_sync["customers"] = now
    _last_sync["matching"] = now
    _last_sync["escalations"] = now

    logger.info(f"Daily job completed: {results}")
    return results


def start_scheduler():
    """Start the background scheduler."""
    global _scheduler, _scheduler_started

    if _scheduler_started:
        logger.warning("Scheduler already started")
        return

    _scheduler = BackgroundScheduler(
        timezone=pytz.timezone(config.TIMEZONE)
    )

    # Schedule daily job at configured time
    try:
        trigger = CronTrigger(
            hour=config.SCHEDULER_HOUR,
            minute=config.SCHEDULER_MINUTE,
            timezone=config.TIMEZONE
        )
        _scheduler.add_job(
            run_daily_job,
            trigger=trigger,
            id="daily_sync_job",
            name="Daily invoice sync and escalation",
            replace_existing=True,
            max_instances=1,  # Prevent concurrent execution
        )
        logger.info(
            f"Scheduled daily job at {config.SCHEDULER_HOUR:02d}:{config.SCHEDULER_MINUTE:02d} "
            f"({config.TIMEZONE})"
        )
    except Exception as e:
        logger.error(f"Failed to schedule daily job: {e}", exc_info=True)
        raise

    # Schedule a startup sync 60 seconds after boot (Render cold start recovery)
    from datetime import timedelta
    _scheduler.add_job(
        run_daily_job,
        trigger="date",
        run_date=datetime.now(pytz.timezone(config.TIMEZONE)) + timedelta(seconds=60),
        id="startup_sync",
        name="Startup sync after cold start",
        replace_existing=True,
        max_instances=1,
    )
    logger.info("Scheduled startup sync in 60 seconds")

    _scheduler.start()
    _scheduler_started = True
    logger.info("Scheduler started")


def stop_scheduler():
    """Stop the background scheduler."""
    global _scheduler_started

    if not _scheduler_started or not _scheduler:
        logger.warning("Scheduler not running")
        return

    try:
        _scheduler.shutdown(wait=True)
        _scheduler_started = False
        logger.info("Scheduler stopped")
    except Exception as e:
        logger.error(f"Error stopping scheduler: {e}", exc_info=True)
        raise


def get_scheduler_status():
    """Get the current scheduler status."""
    return {
        "running": _scheduler_started,
        "scheduler_hour": config.SCHEDULER_HOUR,
        "scheduler_minute": config.SCHEDULER_MINUTE,
        "timezone": config.TIMEZONE,
        "last_sync": _last_sync,
    }
