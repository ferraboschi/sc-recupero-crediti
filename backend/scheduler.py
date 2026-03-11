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
    Daily job that syncs invoices, runs matching, and processes escalations.

    Reuses the sync task functions from backend.api.sync to avoid duplication.

    Args:
        manual: Whether this is a manual trigger (vs scheduled)

    Returns:
        Dictionary with job results
    """
    from backend.api.sync import (
        _sync_invoices_task,
        _sync_customers_task,
        _run_matching_task,
        _process_escalations_task,
    )

    logger.info(f"Starting daily job (manual={manual})")
    results = {
        "invoices": None,
        "customers": None,
        "matching": None,
        "escalations": None,
        "errors": [],
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        results["invoices"] = _sync_invoices_task()
    except Exception as e:
        logger.error(f"Error in invoice sync: {e}", exc_info=True)
        results["errors"].append(f"Invoice sync: {str(e)}")

    try:
        results["customers"] = _sync_customers_task()
    except Exception as e:
        logger.error(f"Error in customer sync: {e}", exc_info=True)
        results["errors"].append(f"Customer sync: {str(e)}")

    try:
        results["matching"] = _run_matching_task()
    except Exception as e:
        logger.error(f"Error in matching: {e}", exc_info=True)
        results["errors"].append(f"Matching: {str(e)}")

    try:
        results["escalations"] = _process_escalations_task()
    except Exception as e:
        logger.error(f"Error in escalations: {e}", exc_info=True)
        results["errors"].append(f"Escalations: {str(e)}")

    # Update last sync times
    _last_sync["invoices"] = datetime.utcnow().isoformat()
    _last_sync["customers"] = datetime.utcnow().isoformat()
    _last_sync["matching"] = datetime.utcnow().isoformat()
    _last_sync["escalations"] = datetime.utcnow().isoformat()

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
