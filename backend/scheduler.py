"""APScheduler integration for scheduled jobs."""

import logging
from datetime import datetime
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from backend.config import config
from backend.database import get_session
from backend.connectors.fatturapro import FatturaProConnector
from backend.connectors.fatture24 import Fattura24Connector
from backend.connectors.shopify import ShopifyConnector
from backend.engine.matching import run_matching
from backend.engine.escalation import process_escalations

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

    Args:
        manual: Whether this is a manual trigger (vs scheduled)

    Returns:
        Dictionary with job results
    """
    logger.info(f"Starting daily job (manual={manual})")
    session = None
    results = {
        "invoices_synced": 0,
        "customers_synced": 0,
        "matches_created": 0,
        "escalations_created": 0,
        "errors": [],
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        session = get_session()

        # Sync invoices from FatturaPro
        logger.info("Syncing invoices from FatturaPro...")
        try:
            fatturapro = FatturaProConnector()
            if fatturapro.login():
                invoices = fatturapro.get_overdue_invoices(session)
                results["invoices_synced"] += invoices.get("created", 0) + invoices.get("updated", 0)
                logger.info(f"FatturaPro sync: {invoices}")
            else:
                logger.warning("FatturaPro login failed")
                results["errors"].append("FatturaPro login failed")
        except Exception as e:
            logger.error(f"Error syncing FatturaPro invoices: {e}", exc_info=True)
            results["errors"].append(f"FatturaPro sync error: {str(e)}")

        # Sync invoices from Fattura24
        logger.info("Syncing invoices from Fattura24...")
        try:
            if config.FATTURA24_API_KEY:
                fattura24 = Fattura24Connector()
                invoices = fattura24.get_overdue_invoices(session)
                results["invoices_synced"] += invoices.get("created", 0) + invoices.get("updated", 0)
                logger.info(f"Fattura24 sync: {invoices}")
            else:
                logger.debug("Fattura24 not configured")
        except Exception as e:
            logger.error(f"Error syncing Fattura24 invoices: {e}", exc_info=True)
            results["errors"].append(f"Fattura24 sync error: {str(e)}")

        # Sync customers from Shopify
        logger.info("Syncing customers from Shopify...")
        try:
            if config.SHOPIFY_ACCESS_TOKEN:
                shopify = ShopifyConnector()
                customers = shopify.sync_customers(session)
                results["customers_synced"] = customers.get("created", 0) + customers.get("updated", 0)
                logger.info(f"Shopify sync: {customers}")
            else:
                logger.debug("Shopify not configured")
        except Exception as e:
            logger.error(f"Error syncing Shopify customers: {e}", exc_info=True)
            results["errors"].append(f"Shopify sync error: {str(e)}")

        # Run matching
        logger.info("Running invoice-customer matching...")
        try:
            match_stats = run_matching(session)
            results["matches_created"] = (
                match_stats.get("matched_piva", 0) +
                match_stats.get("matched_exact", 0) +
                match_stats.get("matched_fuzzy", 0)
            )
            logger.info(f"Matching results: {match_stats}")
        except Exception as e:
            logger.error(f"Error running matching: {e}", exc_info=True)
            results["errors"].append(f"Matching error: {str(e)}")

        # Process escalations
        logger.info("Processing escalations...")
        try:
            escalation_messages = process_escalations(session)
            results["escalations_created"] = len(escalation_messages)
            logger.info(f"Created {len(escalation_messages)} escalation messages")
        except Exception as e:
            logger.error(f"Error processing escalations: {e}", exc_info=True)
            results["errors"].append(f"Escalation error: {str(e)}")

        # Update last sync times
        _last_sync["invoices"] = datetime.utcnow().isoformat()
        _last_sync["customers"] = datetime.utcnow().isoformat()
        _last_sync["matching"] = datetime.utcnow().isoformat()
        _last_sync["escalations"] = datetime.utcnow().isoformat()

        logger.info(f"Daily job completed: {results}")

    except Exception as e:
        logger.error(f"Unexpected error in daily job: {e}", exc_info=True)
        results["errors"].append(f"Unexpected error: {str(e)}")

    finally:
        if session:
            session.close()

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
    global _scheduler, _scheduler_started

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
