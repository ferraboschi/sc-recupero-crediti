"""Sync API endpoints for manual trigger of data synchronization."""

import logging
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from datetime import datetime

from backend.database import get_session, ActivityLog
from backend.connectors.fatturapro import FatturaProConnector
from backend.connectors.fatture24 import Fattura24Connector
from backend.connectors.shopify import ShopifyConnector
from backend.engine.matching import run_matching
from backend.engine.escalation import process_escalations
from backend.scheduler import get_scheduler_status
from backend.config import config

logger = logging.getLogger(__name__)
router = APIRouter()

# Track last sync results
_sync_status = {
    "invoices": {"last_sync": None, "result": None},
    "customers": {"last_sync": None, "result": None},
    "matching": {"last_sync": None, "result": None},
    "escalations": {"last_sync": None, "result": None},
}


def _sync_invoices_task(session: Session) -> dict:
    """Background task to sync invoices from FatturaPro and Fattura24."""
    result = {
        "fatturapro": {"success": False, "created": 0, "updated": 0, "error": None},
        "fattura24": {"success": False, "created": 0, "updated": 0, "error": None},
    }

    # FatturaPro
    try:
        logger.info("Syncing invoices from FatturaPro...")
        fatturapro = FatturaProConnector()
        if fatturapro.login():
            data = fatturapro.get_overdue_invoices(session)
            result["fatturapro"]["success"] = True
            result["fatturapro"]["created"] = data.get("created", 0)
            result["fatturapro"]["updated"] = data.get("updated", 0)
            logger.info(f"FatturaPro sync: {data}")
        else:
            result["fatturapro"]["error"] = "Login failed"
            logger.warning("FatturaPro login failed")
    except Exception as e:
        result["fatturapro"]["error"] = str(e)
        logger.error(f"Error syncing FatturaPro: {e}", exc_info=True)

    # Fattura24
    try:
        if config.FATTURA24_API_KEY:
            logger.info("Syncing invoices from Fattura24...")
            fattura24 = Fattura24Connector()
            data = fattura24.get_overdue_invoices(session)
            result["fattura24"]["success"] = True
            result["fattura24"]["created"] = data.get("created", 0)
            result["fattura24"]["updated"] = data.get("updated", 0)
            logger.info(f"Fattura24 sync: {data}")
        else:
            logger.debug("Fattura24 not configured")
    except Exception as e:
        result["fattura24"]["error"] = str(e)
        logger.error(f"Error syncing Fattura24: {e}", exc_info=True)

    _sync_status["invoices"]["last_sync"] = datetime.utcnow().isoformat()
    _sync_status["invoices"]["result"] = result

    # Log activity
    activity = ActivityLog(
        action="sync",
        entity_type="invoice",
        details=result
    )
    session.add(activity)
    session.commit()

    return result


def _sync_customers_task(session: Session) -> dict:
    """Background task to sync customers from Shopify."""
    result = {"success": False, "created": 0, "updated": 0, "error": None}

    try:
        if config.SHOPIFY_ACCESS_TOKEN:
            logger.info("Syncing customers from Shopify...")
            shopify = ShopifyConnector()
            data = shopify.sync_customers(session)
            result["success"] = True
            result["created"] = data.get("created", 0)
            result["updated"] = data.get("updated", 0)
            logger.info(f"Shopify sync: {data}")
        else:
            result["error"] = "Shopify not configured"
            logger.debug("Shopify not configured")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Error syncing Shopify: {e}", exc_info=True)

    _sync_status["customers"]["last_sync"] = datetime.utcnow().isoformat()
    _sync_status["customers"]["result"] = result

    # Log activity
    activity = ActivityLog(
        action="sync",
        entity_type="customer",
        details=result
    )
    session.add(activity)
    session.commit()

    return result


def _run_matching_task(session: Session) -> dict:
    """Background task to run invoice-customer matching."""
    try:
        logger.info("Running invoice-customer matching...")
        result = run_matching(session)
        logger.info(f"Matching result: {result}")

        _sync_status["matching"]["last_sync"] = datetime.utcnow().isoformat()
        _sync_status["matching"]["result"] = result

        # Log activity
        activity = ActivityLog(
            action="match",
            entity_type="invoice",
            details=result
        )
        session.add(activity)
        session.commit()

        return result
    except Exception as e:
        logger.error(f"Error running matching: {e}", exc_info=True)
        error_result = {"error": str(e)}
        _sync_status["matching"]["result"] = error_result
        return error_result


def _process_escalations_task(session: Session) -> dict:
    """Background task to process escalations."""
    try:
        logger.info("Processing escalations...")
        messages = process_escalations(session)
        result = {
            "escalations_created": len(messages),
            "message_ids": [msg.id for msg in messages]
        }
        logger.info(f"Escalations processed: {result}")

        _sync_status["escalations"]["last_sync"] = datetime.utcnow().isoformat()
        _sync_status["escalations"]["result"] = result

        # Log activity
        activity = ActivityLog(
            action="escalation",
            details=result
        )
        session.add(activity)
        session.commit()

        return result
    except Exception as e:
        logger.error(f"Error processing escalations: {e}", exc_info=True)
        error_result = {"error": str(e)}
        _sync_status["escalations"]["result"] = error_result
        return error_result


@router.post("/invoices")
async def sync_invoices(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Trigger manual sync of invoices from FatturaPro and Fattura24."""
    background_tasks.add_task(_sync_invoices_task, session)
    return {
        "status": "sync_started",
        "message": "Invoice sync started in background"
    }


@router.post("/customers")
async def sync_customers(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Trigger manual sync of customers from Shopify."""
    background_tasks.add_task(_sync_customers_task, session)
    return {
        "status": "sync_started",
        "message": "Customer sync started in background"
    }


@router.post("/matching")
async def sync_matching(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Trigger manual matching run."""
    background_tasks.add_task(_run_matching_task, session)
    return {
        "status": "sync_started",
        "message": "Matching sync started in background"
    }


@router.post("/escalations")
async def sync_escalations(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Trigger manual escalation processing."""
    background_tasks.add_task(_process_escalations_task, session)
    return {
        "status": "sync_started",
        "message": "Escalation processing started in background"
    }


@router.post("/full")
async def sync_full(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Trigger full sync: invoices + customers + matching + escalations."""
    background_tasks.add_task(_sync_invoices_task, session)
    background_tasks.add_task(_sync_customers_task, session)
    background_tasks.add_task(_run_matching_task, session)
    background_tasks.add_task(_process_escalations_task, session)

    return {
        "status": "sync_started",
        "message": "Full sync started in background"
    }


@router.get("/status")
async def get_sync_status():
    """Get the last sync timestamps and results."""
    return {
        "last_sync": _sync_status,
        "scheduler": get_scheduler_status(),
    }
