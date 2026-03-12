"""Sync API endpoints for manual trigger of data synchronization."""

import logging
from fastapi import APIRouter, BackgroundTasks
from datetime import datetime, date

from backend.database import get_session, ActivityLog, Invoice, Customer
from backend.connectors.fatturapro import FatturaProConnector
from backend.connectors.fatture24 import Fattura24Connector
from backend.connectors.shopify import ShopifyConnector
from backend.engine.matching import run_matching
from backend.engine.escalation import process_escalations
from backend.scheduler import get_scheduler_status
from backend.config import config
from backend.engine.normalizer import normalize_ragione_sociale

logger = logging.getLogger(__name__)
router = APIRouter()

# Track last sync results
_sync_status = {
    "invoices": {"last_sync": None, "result": None},
    "customers": {"last_sync": None, "result": None},
    "matching": {"last_sync": None, "result": None},
    "escalations": {"last_sync": None, "result": None},
}


def _sync_invoices_task() -> dict:
    """Background task to sync invoices from FatturaPro and Fattura24."""
    session = get_session()
    result = {
        "fatturapro": {"success": False, "created": 0, "updated": 0, "error": None},
        "fattura24": {"success": False, "created": 0, "updated": 0, "error": None},
    }

    try:
        # FatturaPro
        try:
            logger.info("Syncing invoices from FatturaPro...")
            fatturapro = FatturaProConnector()
            if fatturapro.login():
                raw_invoices = fatturapro.fetch_overdue_invoices()
                created, updated = 0, 0
                for inv in raw_invoices:
                    # Check if invoice already exists
                    existing = session.query(Invoice).filter_by(
                        invoice_number=inv["invoice_number"],
                        source_platform="fatturapro"
                    ).first()

                    if existing:
                        existing.amount = inv.get("total", 0)
                        existing.amount_due = inv.get("balance", 0)
                        existing.customer_name_raw = inv.get("customer_name")
                        if inv.get("date"):
                            existing.issue_date = inv["date"]
                        existing.days_overdue = (date.today() - inv["date"]).days if inv.get("date") else 0
                        updated += 1
                    else:
                        new_invoice = Invoice(
                            invoice_number=inv["invoice_number"],
                            amount=inv.get("total", 0),
                            amount_due=inv.get("balance", 0),
                            issue_date=inv.get("date"),
                            customer_name_raw=inv.get("customer_name"),
                            source_platform="fatturapro",
                            source_id=inv.get("doc_id"),
                            days_overdue=(date.today() - inv["date"]).days if inv.get("date") else 0,
                        )
                        session.add(new_invoice)
                        created += 1

                session.commit()
                result["fatturapro"]["success"] = True
                result["fatturapro"]["created"] = created
                result["fatturapro"]["updated"] = updated
                logger.info(f"FatturaPro sync: created={created}, updated={updated}")
            else:
                result["fatturapro"]["error"] = "Login failed — check FATTURAPRO_USERNAME/PASSWORD env vars and server logs"
                logger.error("FatturaPro login failed — cannot sync invoices")
                logger.warning("FatturaPro login failed")
        except Exception as e:
            result["fatturapro"]["error"] = str(e)
            logger.error(f"Error syncing FatturaPro: {e}", exc_info=True)

        # Fattura24
        try:
            if config.FATTURA24_API_KEY:
                logger.info("Syncing invoices from Fattura24...")
                fattura24 = Fattura24Connector()
                raw_invoices = fattura24.fetch_overdue_invoices()
                created, updated = 0, 0
                for inv in raw_invoices:
                    existing = session.query(Invoice).filter_by(
                        invoice_number=inv["invoice_number"],
                        source_platform="fatture24"
                    ).first()

                    if existing:
                        existing.amount = inv.get("amount", 0)
                        existing.amount_due = inv.get("amount_due", 0)
                        existing.customer_name_raw = inv.get("customer_name")
                        existing.customer_piva_raw = inv.get("customer_piva")
                        updated += 1
                    else:
                        # Parse dates
                        issue_date = None
                        due_date = None
                        try:
                            if inv.get("issue_date"):
                                issue_date = datetime.strptime(inv["issue_date"], "%Y-%m-%d").date()
                            if inv.get("due_date"):
                                due_date = datetime.strptime(inv["due_date"], "%Y-%m-%d").date()
                        except (ValueError, TypeError):
                            pass

                        days_overdue = 0
                        if due_date:
                            days_overdue = max(0, (date.today() - due_date).days)

                        new_invoice = Invoice(
                            invoice_number=inv["invoice_number"],
                            amount=inv.get("amount", 0),
                            amount_due=inv.get("amount_due", 0),
                            issue_date=issue_date,
                            due_date=due_date,
                            customer_name_raw=inv.get("customer_name"),
                            customer_piva_raw=inv.get("customer_piva"),
                            source_platform="fatture24",
                            source_id=inv.get("source_id"),
                            days_overdue=days_overdue,
                        )
                        session.add(new_invoice)
                        created += 1

                session.commit()
                result["fattura24"]["success"] = True
                result["fattura24"]["created"] = created
                result["fattura24"]["updated"] = updated
                logger.info(f"Fattura24 sync: created={created}, updated={updated}")
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

    except Exception as e:
        logger.error(f"Unexpected error in invoice sync: {e}", exc_info=True)
        result["error"] = str(e)
    finally:
        session.close()

    return result


def _sync_customers_task() -> dict:
    """Background task to sync customers from Shopify."""
    session = get_session()
    result = {"success": False, "created": 0, "updated": 0, "error": None}

    try:
        if config.SHOPIFY_ACCESS_TOKEN:
            logger.info("Syncing customers from Shopify...")
            shopify = ShopifyConnector()
            raw_customers = shopify.fetch_b2b_customers()
            created, updated = 0, 0

            for cust in raw_customers:
                # Check if customer already exists by shopify_id
                existing = session.query(Customer).filter_by(
                    shopify_id=cust["shopify_id"]
                ).first()

                if existing:
                    existing.ragione_sociale = cust.get("ragione_sociale", existing.ragione_sociale)
                    existing.ragione_sociale_normalized = normalize_ragione_sociale(
                        cust.get("ragione_sociale", "")
                    )
                    existing.partita_iva = cust.get("partita_iva") or existing.partita_iva
                    existing.codice_fiscale = cust.get("codice_fiscale") or existing.codice_fiscale
                    existing.codice_sdi = cust.get("codice_sdi") or existing.codice_sdi
                    existing.phone = cust.get("phone") or existing.phone
                    existing.email = cust.get("email") or existing.email
                    existing.tags = cust.get("tags") or existing.tags
                    updated += 1
                else:
                    new_customer = Customer(
                        shopify_id=cust["shopify_id"],
                        ragione_sociale=cust.get("ragione_sociale", ""),
                        ragione_sociale_normalized=normalize_ragione_sociale(
                            cust.get("ragione_sociale", "")
                        ),
                        partita_iva=cust.get("partita_iva"),
                        codice_fiscale=cust.get("codice_fiscale"),
                        codice_sdi=cust.get("codice_sdi"),
                        phone=cust.get("phone"),
                        email=cust.get("email"),
                        tags=cust.get("tags"),
                        source="shopify",
                    )
                    session.add(new_customer)
                    created += 1

            session.commit()
            result["success"] = True
            result["created"] = created
            result["updated"] = updated
            logger.info(f"Shopify sync: created={created}, updated={updated}")
        else:
            result["error"] = "Shopify not configured"
            logger.debug("Shopify not configured")

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

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Error syncing Shopify: {e}", exc_info=True)
    finally:
        session.close()

    return result


def _run_matching_task() -> dict:
    """Background task to run invoice-customer matching."""
    session = get_session()
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
    finally:
        session.close()


def _process_escalations_task() -> dict:
    """Background task to process escalations."""
    session = get_session()
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
    finally:
        session.close()


@router.post("/invoices")
async def sync_invoices(background_tasks: BackgroundTasks):
    """Trigger manual sync of invoices from FatturaPro and Fattura24."""
    background_tasks.add_task(_sync_invoices_task)
    return {
        "status": "sync_started",
        "message": "Invoice sync started in background"
    }


@router.post("/customers")
async def sync_customers(background_tasks: BackgroundTasks):
    """Trigger manual sync of customers from Shopify."""
    background_tasks.add_task(_sync_customers_task)
    return {
        "status": "sync_started",
        "message": "Customer sync started in background"
    }


@router.post("/matching")
async def sync_matching(background_tasks: BackgroundTasks):
    """Trigger manual matching run."""
    background_tasks.add_task(_run_matching_task)
    return {
        "status": "sync_started",
        "message": "Matching sync started in background"
    }


@router.post("/escalations")
async def sync_escalations(background_tasks: BackgroundTasks):
    """Trigger manual escalation processing."""
    background_tasks.add_task(_process_escalations_task)
    return {
        "status": "sync_started",
        "message": "Escalation processing started in background"
    }


@router.post("/full")
async def sync_full(background_tasks: BackgroundTasks):
    """Trigger full sync: invoices + customers + matching + escalations."""
    background_tasks.add_task(_sync_invoices_task)
    background_tasks.add_task(_sync_customers_task)
    background_tasks.add_task(_run_matching_task)
    background_tasks.add_task(_process_escalations_task)

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


