"""Sync API endpoints for manual trigger of data synchronization."""

import logging
import csv
import threading
from io import StringIO
from fastapi import APIRouter, BackgroundTasks, UploadFile, File, Form
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

# Sync mutex to prevent concurrent syncs
_sync_lock = threading.Lock()

# Track last sync results
_sync_status = {
    "invoices": {"last_sync": None, "result": None},
    "customers": {"last_sync": None, "result": None},
    "matching": {"last_sync": None, "result": None},
    "escalations": {"last_sync": None, "result": None},
}


def _sync_invoices_task() -> dict:
    """Background task to sync invoices from FatturaPro and Fattura24.

    Key behaviors:
    - Fetches currently-overdue invoices from both platforms
    - Updates existing invoices with fresh amount_due values
    - Creates new invoices for newly-found overdue items
    - Detects payments: invoices previously known but no longer in the overdue
      list are marked as 'paid' (amount_due becomes 0)
    - Recalculates days_overdue dynamically for ALL unpaid invoices
    """
    session = get_session()
    result = {
        "fatturapro": {"success": False, "created": 0, "updated": 0, "paid_detected": 0, "error": None},
        "fattura24": {"success": False, "created": 0, "updated": 0, "paid_detected": 0, "error": None},
    }

    try:
        # FatturaPro
        try:
            logger.info("Syncing invoices from FatturaPro...")
            fatturapro = FatturaProConnector()
            if fatturapro.login():
                raw_invoices = fatturapro.fetch_overdue_invoices()
                created, updated = 0, 0

                # Build set of invoice numbers currently overdue in FatturaPro
                fetched_invoice_numbers = set()

                for inv in raw_invoices:
                    inv_num = inv["invoice_number"]
                    fetched_invoice_numbers.add(inv_num)

                    existing = session.query(Invoice).filter_by(
                        invoice_number=inv_num,
                        source_platform="fatturapro"
                    ).first()

                    if existing:
                        existing.amount = inv.get("total", 0)
                        existing.amount_due = inv.get("balance", 0)
                        existing.customer_name_raw = inv.get("customer_name")
                        if inv.get("date"):
                            existing.issue_date = inv["date"]
                        # Keep status as open if it was paid before but reappeared
                        if existing.status == "paid" and inv.get("balance", 0) > 0:
                            existing.status = "open"
                        existing.updated_at = datetime.utcnow()
                        updated += 1
                    else:
                        new_invoice = Invoice(
                            invoice_number=inv_num,
                            amount=inv.get("total", 0),
                            amount_due=inv.get("balance", 0),
                            issue_date=inv.get("date"),
                            customer_name_raw=inv.get("customer_name"),
                            source_platform="fatturapro",
                            source_id=inv.get("doc_id"),
                        )
                        session.add(new_invoice)
                        created += 1

                # PAYMENT DETECTION: find FatturaPro invoices in our DB that are
                # no longer in the overdue list → they have been paid
                paid_detected = 0
                known_fp_invoices = session.query(Invoice).filter(
                    Invoice.source_platform == "fatturapro",
                    Invoice.status != "paid",
                ).all()

                for known_inv in known_fp_invoices:
                    if known_inv.invoice_number not in fetched_invoice_numbers:
                        known_inv.status = "paid"
                        known_inv.amount_due = 0
                        known_inv.updated_at = datetime.utcnow()
                        paid_detected += 1
                        logger.info(
                            f"Payment detected: FatturaPro invoice {known_inv.invoice_number} "
                            f"no longer overdue — marked as paid"
                        )

                session.commit()
                result["fatturapro"]["success"] = True
                result["fatturapro"]["created"] = created
                result["fatturapro"]["updated"] = updated
                result["fatturapro"]["paid_detected"] = paid_detected
                logger.info(f"FatturaPro sync: created={created}, updated={updated}, paid_detected={paid_detected}")
            else:
                result["fatturapro"]["error"] = "Login failed — check FATTURAPRO_USERNAME/PASSWORD env vars and server logs"
                logger.error("FatturaPro login failed — cannot sync invoices")
        except Exception as e:
            result["fatturapro"]["error"] = str(e)
            logger.error(f"Error syncing FatturaPro: {e}", exc_info=True)

        # Fattura24
        try:
            if config.FATTURA24_API_KEY:
                logger.info("Syncing invoices from Fattura24...")
                fattura24 = Fattura24Connector()

                # Fetch ALL invoices (not just overdue) to detect payments
                from datetime import timedelta
                date_to = datetime.now().strftime("%Y-%m-%d")
                date_from = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
                raw_invoices = fattura24.fetch_invoices(date_from, date_to)

                created, updated, paid_detected = 0, 0, 0
                fetched_invoice_numbers = set()

                for inv in raw_invoices:
                    inv_num = inv["invoice_number"]
                    fetched_invoice_numbers.add(inv_num)

                    existing = session.query(Invoice).filter_by(
                        invoice_number=inv_num,
                        source_platform="fatture24"
                    ).first()

                    amount_due = inv.get("amount_due", 0)

                    if existing:
                        existing.amount = inv.get("amount", 0)
                        existing.amount_due = amount_due
                        existing.customer_name_raw = inv.get("customer_name")
                        existing.customer_piva_raw = inv.get("customer_piva")
                        existing.updated_at = datetime.utcnow()

                        # Detect payment: amount_due dropped to 0
                        if amount_due <= 0 and existing.status != "paid":
                            existing.status = "paid"
                            paid_detected += 1
                            logger.info(f"Payment detected: Fattura24 invoice {inv_num} amount_due=0 → paid")
                        elif amount_due > 0 and existing.status == "paid":
                            existing.status = "open"  # Re-opened

                        # Parse dates for existing if missing
                        if not existing.issue_date and inv.get("issue_date"):
                            try:
                                existing.issue_date = datetime.strptime(inv["issue_date"], "%Y-%m-%d").date()
                            except (ValueError, TypeError):
                                pass
                        if not existing.due_date and inv.get("due_date"):
                            try:
                                existing.due_date = datetime.strptime(inv["due_date"], "%Y-%m-%d").date()
                            except (ValueError, TypeError):
                                pass

                        updated += 1
                    else:
                        # Only create if there's something due
                        if amount_due <= 0:
                            continue

                        issue_date = None
                        due_date = None
                        try:
                            if inv.get("issue_date"):
                                issue_date = datetime.strptime(inv["issue_date"], "%Y-%m-%d").date()
                            if inv.get("due_date"):
                                due_date = datetime.strptime(inv["due_date"], "%Y-%m-%d").date()
                        except (ValueError, TypeError):
                            pass

                        new_invoice = Invoice(
                            invoice_number=inv_num,
                            amount=inv.get("amount", 0),
                            amount_due=amount_due,
                            issue_date=issue_date,
                            due_date=due_date,
                            customer_name_raw=inv.get("customer_name"),
                            customer_piva_raw=inv.get("customer_piva"),
                            source_platform="fatture24",
                            source_id=inv.get("source_id"),
                        )
                        session.add(new_invoice)
                        created += 1

                session.commit()
                result["fattura24"]["success"] = True
                result["fattura24"]["created"] = created
                result["fattura24"]["updated"] = updated
                result["fattura24"]["paid_detected"] = paid_detected
                logger.info(f"Fattura24 sync: created={created}, updated={updated}, paid_detected={paid_detected}")
            else:
                logger.debug("Fattura24 not configured")
        except Exception as e:
            result["fattura24"]["error"] = str(e)
            logger.error(f"Error syncing Fattura24: {e}", exc_info=True)

        # RECALCULATE days_overdue for ALL unpaid invoices (dynamic, not stale)
        _recalculate_days_overdue(session)

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


def _recalculate_days_overdue(session):
    """Recalculate days_overdue for all unpaid invoices based on current date."""
    today = date.today()
    unpaid_invoices = session.query(Invoice).filter(
        Invoice.status != "paid"
    ).all()

    updated = 0
    for inv in unpaid_invoices:
        if inv.due_date:
            new_days = max(0, (today - inv.due_date).days)
        elif inv.issue_date:
            # Assume 30-day payment terms if no due_date
            from datetime import timedelta
            assumed_due = inv.issue_date + timedelta(days=30)
            new_days = max(0, (today - assumed_due).days)
        else:
            new_days = 0

        if inv.days_overdue != new_days:
            inv.days_overdue = new_days
            updated += 1

    # Also zero-out days_overdue for paid invoices
    paid_invoices = session.query(Invoice).filter(
        Invoice.status == "paid",
        Invoice.days_overdue > 0,
    ).all()
    for inv in paid_invoices:
        inv.days_overdue = 0

    if updated > 0:
        session.commit()
        logger.info(f"Recalculated days_overdue for {updated} invoices")


def _sync_customers_task() -> dict:
    """Background task to sync customers from Shopify AND auto-create from invoices.

    Two-phase approach:
    1. Sync from Shopify (primary customer source)
    2. Auto-create customers from unmatched invoices that have P.IVA or name
       but no corresponding Customer record. This ensures customers like
       "F-T SRL" that only exist in FatturaPro/Fattura24 get created.
    """
    session = get_session()
    result = {
        "success": False, "created": 0, "updated": 0,
        "auto_created_from_invoices": 0, "error": None,
    }

    try:
        # Phase 1: Shopify sync
        if config.SHOPIFY_ACCESS_TOKEN:
            logger.info("Syncing customers from Shopify...")
            shopify = ShopifyConnector()
            raw_customers = shopify.fetch_b2b_customers()
            created, updated = 0, 0

            for cust in raw_customers:
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
            result["success"] = True  # Not an error, just unconfigured
            logger.debug("Shopify not configured")

        # Phase 2: Auto-create customers from unmatched invoices
        auto_created = _auto_create_customers_from_invoices(session)
        result["auto_created_from_invoices"] = auto_created

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
        logger.error(f"Error syncing customers: {e}", exc_info=True)
    finally:
        session.close()

    return result


def _auto_create_customers_from_invoices(session) -> int:
    """Auto-create Customer records from unmatched invoices.

    For invoices that have customer_name_raw or customer_piva_raw but no
    customer_id, check if a matching Customer exists. If not, create one.
    This ensures customers that only exist in FatturaPro/Fattura24 are captured.

    Returns:
        Number of customers auto-created
    """
    from backend.engine.matching import match_invoice_to_customer

    unmatched = session.query(Invoice).filter(
        Invoice.customer_id.is_(None),
        Invoice.status != "paid",
    ).all()

    if not unmatched:
        return 0

    all_customers = session.query(Customer).all()
    auto_created = 0
    # Track names/PIVAs we've already created in this run to avoid duplicates
    created_pivas = set()
    created_names_normalized = set()

    for inv in unmatched:
        # Skip if no identifying info
        if not inv.customer_name_raw and not inv.customer_piva_raw:
            continue

        # First try to match against existing customers (including any just created)
        if all_customers:
            match = match_invoice_to_customer(inv, all_customers, session)
            if match:
                inv.customer_id = match.id
                continue

        # Check if we already have this customer by P.IVA
        piva = (inv.customer_piva_raw or "").strip().upper()
        if piva and piva in created_pivas:
            continue
        if piva:
            existing_by_piva = session.query(Customer).filter(
                Customer.partita_iva == piva
            ).first()
            if existing_by_piva:
                inv.customer_id = existing_by_piva.id
                continue

        # Check if we already have this customer by normalized name
        name = inv.customer_name_raw or ""
        name_norm = normalize_ragione_sociale(name)
        if name_norm and name_norm in created_names_normalized:
            continue
        if name_norm:
            existing_by_name = session.query(Customer).filter(
                Customer.ragione_sociale_normalized == name_norm
            ).first()
            if existing_by_name:
                inv.customer_id = existing_by_name.id
                continue

        # Create new customer
        new_customer = Customer(
            ragione_sociale=name.strip() if name else f"Cliente P.IVA {piva}",
            ragione_sociale_normalized=name_norm,
            partita_iva=piva if piva else None,
            source=inv.source_platform,  # fatturapro / fatture24
        )
        session.add(new_customer)
        session.flush()  # Get the ID

        inv.customer_id = new_customer.id
        all_customers.append(new_customer)  # Add to local list for future matching
        auto_created += 1

        if piva:
            created_pivas.add(piva)
        if name_norm:
            created_names_normalized.add(name_norm)

        logger.info(
            f"Auto-created customer '{new_customer.ragione_sociale}' "
            f"(P.IVA: {piva or 'N/A'}) from {inv.source_platform} invoice {inv.invoice_number}"
        )

    if auto_created > 0:
        session.commit()
        logger.info(f"Auto-created {auto_created} customers from unmatched invoices")

    return auto_created


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


def _full_sync_task() -> dict:
    """Run full sync sequentially: invoices → customers → matching → escalations.

    Uses a mutex to prevent concurrent full syncs from corrupting data.
    """
    if not _sync_lock.acquire(blocking=False):
        logger.warning("Full sync already in progress, skipping")
        return {"error": "Sync already in progress"}

    try:
        logger.info("Starting full sync (sequential)...")
        results = {}

        # Step 1: Sync invoices first (gets latest data from platforms)
        try:
            results["invoices"] = _sync_invoices_task()
        except Exception as e:
            logger.error(f"Invoice sync failed: {e}", exc_info=True)
            results["invoices"] = {"error": str(e)}

        # Step 2: Sync customers (including auto-create from invoices)
        try:
            results["customers"] = _sync_customers_task()
        except Exception as e:
            logger.error(f"Customer sync failed: {e}", exc_info=True)
            results["customers"] = {"error": str(e)}

        # Step 3: Run matching (now that we have fresh invoices + customers)
        try:
            results["matching"] = _run_matching_task()
        except Exception as e:
            logger.error(f"Matching failed: {e}", exc_info=True)
            results["matching"] = {"error": str(e)}

        # Step 4: Process escalations (depends on matched invoices)
        try:
            results["escalations"] = _process_escalations_task()
        except Exception as e:
            logger.error(f"Escalations failed: {e}", exc_info=True)
            results["escalations"] = {"error": str(e)}

        logger.info(f"Full sync completed: {results}")
        return results
    finally:
        _sync_lock.release()


@router.post("/full")
async def sync_full(background_tasks: BackgroundTasks):
    """Trigger full sync: invoices → customers → matching → escalations (sequential)."""
    background_tasks.add_task(_full_sync_task)

    return {
        "status": "sync_started",
        "message": "Full sync started in background (sequential: invoices → customers → matching → escalations)"
    }


@router.get("/status")
async def get_sync_status():
    """Get the last sync timestamps and results."""
    return {
        "last_sync": _sync_status,
        "scheduler": get_scheduler_status(),
    }


@router.post("/import-csv")
async def import_csv(file: UploadFile = File(...)):
    """
    Import invoices from a CSV file (e.g. exported from Fattura24).

    Expected CSV columns (flexible matching, case-insensitive):
    - Invoice Number: numero, invoice_number, numero_fattura, n_documento
    - Customer Name: cliente, customer, ragione_sociale, destinatario
    - P.IVA: partita_iva, piva, p_iva, vat
    - Amount: importo, amount, totale, total
    - Amount Due: saldo, amount_due, da_incassare, balance
    - Issue Date: data, issue_date, data_emissione, data_documento
    - Due Date: scadenza, due_date, data_scadenza

    Returns import statistics.
    """
    session = get_session()
    result = {"created": 0, "updated": 0, "skipped": 0, "errors": [], "total_rows": 0}

    try:
        content = await file.read()
        text = content.decode("utf-8-sig")  # Handle BOM

        # Auto-detect delimiter
        first_line = text.split("\n")[0]
        if ";" in first_line and "," not in first_line:
            reader = csv.DictReader(StringIO(text), delimiter=";")
        elif "\t" in first_line:
            reader = csv.DictReader(StringIO(text), delimiter="\t")
        else:
            reader = csv.DictReader(StringIO(text), delimiter=",")

        # Column mapping - map common Italian/English column names to our fields
        COLUMN_MAP = {
            # invoice_number
            "numero": "invoice_number",
            "invoice_number": "invoice_number",
            "numero_fattura": "invoice_number",
            "n_documento": "invoice_number",
            "numero documento": "invoice_number",
            "n. documento": "invoice_number",
            "documento": "invoice_number",
            # customer_name
            "cliente": "customer_name",
            "customer": "customer_name",
            "ragione_sociale": "customer_name",
            "ragione sociale": "customer_name",
            "destinatario": "customer_name",
            "nome": "customer_name",
            # piva
            "partita_iva": "piva",
            "partita iva": "piva",
            "piva": "piva",
            "p_iva": "piva",
            "p.iva": "piva",
            "p. iva": "piva",
            "vat": "piva",
            "codice fiscale": "piva",
            # amount
            "importo": "amount",
            "amount": "amount",
            "totale": "amount",
            "total": "amount",
            # amount_due
            "saldo": "amount_due",
            "amount_due": "amount_due",
            "da_incassare": "amount_due",
            "da incassare": "amount_due",
            "balance": "amount_due",
            "residuo": "amount_due",
            # issue_date
            "data": "issue_date",
            "issue_date": "issue_date",
            "data_emissione": "issue_date",
            "data emissione": "issue_date",
            "data_documento": "issue_date",
            "data documento": "issue_date",
            # due_date
            "scadenza": "due_date",
            "due_date": "due_date",
            "data_scadenza": "due_date",
            "data scadenza": "due_date",
        }

        def map_row(row):
            """Map CSV columns to our field names."""
            mapped = {}
            for csv_col, value in row.items():
                if csv_col is None:
                    continue
                key = COLUMN_MAP.get(csv_col.strip().lower())
                if key and value and value.strip():
                    mapped[key] = value.strip()
            return mapped

        def parse_amount(s):
            """Parse Italian or English formatted currency amount."""
            if not s:
                return 0.0
            # Remove currency symbols and spaces
            s = s.replace("€", "").replace("$", "").strip()
            # Handle Italian format: 1.234,56
            if "," in s and "." in s:
                if s.index(",") > s.index("."):
                    # Italian: 1.234,56
                    s = s.replace(".", "").replace(",", ".")
                # else English: 1,234.56
                else:
                    s = s.replace(",", "")
            elif "," in s:
                # Could be Italian decimal: 123,45
                s = s.replace(",", ".")
            try:
                return float(s)
            except (ValueError, TypeError):
                return 0.0

        def parse_date(s):
            """Parse date in various formats."""
            if not s:
                return None
            # Try common formats
            for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"]:
                try:
                    return datetime.strptime(s, fmt).date()
                except ValueError:
                    continue
            return None

        for row_num, row in enumerate(reader, start=1):
            result["total_rows"] += 1
            mapped = map_row(row)

            if not mapped.get("invoice_number"):
                result["skipped"] += 1
                result["errors"].append(f"Row {row_num}: missing invoice number")
                continue

            inv_num = mapped["invoice_number"]

            # Check if already exists
            existing = session.query(Invoice).filter_by(
                invoice_number=inv_num,
                source_platform="fatture24"
            ).first()

            amount = parse_amount(mapped.get("amount", "0"))
            amount_due = parse_amount(mapped.get("amount_due", "0"))
            if amount_due == 0 and amount > 0:
                amount_due = amount  # If no separate balance, assume full amount due

            issue_date = parse_date(mapped.get("issue_date"))
            due_date = parse_date(mapped.get("due_date"))

            days_overdue = 0
            if due_date:
                days_overdue = max(0, (date.today() - due_date).days)

            if existing:
                existing.amount = amount
                existing.amount_due = amount_due
                existing.issue_date = issue_date or existing.issue_date
                existing.due_date = due_date or existing.due_date
                existing.customer_name_raw = mapped.get("customer_name") or existing.customer_name_raw
                existing.customer_piva_raw = mapped.get("piva") or existing.customer_piva_raw
                existing.days_overdue = days_overdue
                result["updated"] += 1
            else:
                new_invoice = Invoice(
                    invoice_number=inv_num,
                    amount=amount,
                    amount_due=amount_due,
                    issue_date=issue_date,
                    due_date=due_date,
                    customer_name_raw=mapped.get("customer_name"),
                    customer_piva_raw=mapped.get("piva"),
                    source_platform="fatture24",
                    days_overdue=days_overdue,
                )
                session.add(new_invoice)
                result["created"] += 1

        session.commit()

        # Log activity
        activity = ActivityLog(
            action="csv_import",
            entity_type="invoice",
            details={
                "filename": file.filename,
                "source": "fatture24",
                **result
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"CSV import complete: {result}")
        return result

    except Exception as e:
        logger.error(f"Error importing CSV: {e}", exc_info=True)
        session.rollback()
        result["errors"].append(str(e))
        return result
    finally:
        session.close()


