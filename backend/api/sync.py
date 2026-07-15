"""Sync API endpoints for manual trigger of data synchronization.

Pipeline (ordine vincolante — vedi design luglio 2026):
1. invoices    — fetch FatturaPro (+ enrichment P.IVA/scadenze), payment
                 detection SOLO se il fetch è completo (mai su fetch parziale)
2. customers   — sync clienti da Shopify
3. matching    — abbinamenti sicuri + suggerimenti in quarantena
4. auto-create — crea clienti SOLO per fatture senza alcun candidato
                 (dopo il matching, mai prima: altrimenti la quarantena
                 non vede mai le fatture e nascono clienti duplicati)
5. order matching — aggancio ordini Shopify
6. cases       — lifecycle pratiche (apertura/aggancio/chiusura; chiusure
                 saltate se il fetch fatture era parziale)
"""

import logging
import csv
import threading
from io import StringIO
from fastapi import APIRouter, BackgroundTasks, UploadFile, File
from datetime import datetime, date, timedelta

from backend.database import (
    get_session_direct, ActivityLog,
    Invoice, Customer, SyncState,
)
from backend.connectors.fatturapro import FatturaProConnector
from backend.connectors.shopify import ShopifyConnector
from backend.engine.matching import run_matching
from backend.engine.cases import update_case_lifecycle
from backend.engine.piva import validate_piva
from backend.scheduler import get_scheduler_status
from backend.config import config
from backend.engine.normalizer import normalize_ragione_sociale

logger = logging.getLogger(__name__)

# Payment detection per assenza: quante volte consecutive una fattura deve
# mancare da un fetch COMPLETO prima di essere marcata pagata. Con 1 sola
# assenza, una riga persa silenziosamente dal fetch diventava una falsa
# "pagata" e spariva da tutti i conteggi scadute.
PAID_ABSENCE_STREAK = 2
router = APIRouter()

# Sync mutex to prevent concurrent syncs — used by ALL sync operations
# (full sync, individual invoice/customer/matching syncs)
_sync_lock = threading.Lock()

# Track last sync results
_sync_status = {
    "invoices": {"last_sync": None, "result": None},
    "customers": {"last_sync": None, "result": None},
    "matching": {"last_sync": None, "result": None},
    "auto_create": {"last_sync": None, "result": None},
    "order_matching": {"last_sync": None, "result": None},
    "cases": {"last_sync": None, "result": None},
}

_sync_loaded = False


def _load_sync_state():
    """Load persisted sync state from DB on first access."""
    global _sync_loaded
    if _sync_loaded:
        return
    try:
        session = get_session_direct()
        rows = session.query(SyncState).all()
        for row in rows:
            if row.key in _sync_status:
                _sync_status[row.key]["last_sync"] = row.last_sync.isoformat() if row.last_sync else None
                _sync_status[row.key]["result"] = row.result
        session.close()
        _sync_loaded = True
        logger.info("Loaded persisted sync state from DB")
    except Exception as e:
        logger.warning(f"Could not load sync state from DB: {e}")
        _sync_loaded = True  # Don't retry on every call


def _persist_sync_status(key: str, result: dict):
    """Update in-memory sync status AND persist to DB."""
    now = datetime.utcnow()
    if key in _sync_status:
        _sync_status[key]["last_sync"] = now.isoformat()
        _sync_status[key]["result"] = result
    try:
        session = get_session_direct()
        existing = session.query(SyncState).filter_by(key=key).first()
        if existing:
            existing.last_sync = now
            existing.result = result
            existing.updated_at = now
        else:
            session.add(SyncState(key=key, last_sync=now, result=result, updated_at=now))
        session.commit()
        session.close()
    except Exception as e:
        logger.warning(f"Could not persist sync state for {key}: {e}")


def _sync_invoices_task() -> dict:
    """Background task to sync invoices from FatturaPro.

    Key behaviors:
    - Fetches currently-overdue invoices (with partial-fetch flag)
    - Enriches new/incomplete invoices with P.IVA + REAL due date from
      the detail pages (single fetch per invoice, capped per run)
    - Updates existing invoices with fresh amount_due values
    - Payment detection (invoice known but no longer in the overdue list →
      paid) runs ONLY on a COMPLETE fetch: a missing invoice in a partial
      list is not evidence of payment.
    - Recalculates days_overdue dynamically for ALL unpaid invoices
    """
    session = get_session_direct()
    result = {
        "fatturapro": {
            "success": False, "created": 0, "updated": 0, "paid_detected": 0,
            "piva_enriched": 0, "due_date_enriched": 0, "partial": False, "error": None,
        },
    }

    try:
        fatturapro = None
        try:
            logger.info("Syncing invoices from FatturaPro...")
            fatturapro = FatturaProConnector()
            if fatturapro.login():
                raw_invoices, partial = fatturapro.fetch_overdue_invoices()
                result["fatturapro"]["partial"] = partial
                created, updated = 0, 0
                piva_enriched = 0
                due_enriched = 0

                # ── ENRICHMENT dettaglio (P.IVA + scadenza reale) ──
                # Serve il dettaglio per le fatture nuove e per quelle già
                # note senza P.IVA o senza scadenza reale.
                existing_rows = session.query(
                    Invoice.invoice_number, Invoice.customer_piva_raw,
                    Invoice.due_date_source, Invoice.detail_attempted_at,
                ).filter(Invoice.source_platform == "fatturapro").all()
                complete_invoice_numbers = {
                    row.invoice_number for row in existing_rows
                    if row.customer_piva_raw and row.due_date_source == "real"
                }
                attempted_at_by_number = {
                    row.invoice_number: row.detail_attempted_at
                    for row in existing_rows
                }

                invoices_needing_detail = [
                    inv for inv in raw_invoices
                    if inv.get("doc_id")
                    and inv["invoice_number"] not in complete_invoice_numbers
                ]

                # Rotazione: prima le fatture mai tentate, poi quelle col
                # tentativo più vecchio. Senza questo ordinamento il cap
                # riprovava per sempre le stesse N fatture in testa alla
                # lista (le più recenti), lasciando le altre a digiuno.
                invoices_needing_detail.sort(
                    key=lambda inv: (
                        attempted_at_by_number.get(inv["invoice_number"]) is not None,
                        attempted_at_by_number.get(inv["invoice_number"]) or datetime.min,
                    )
                )

                # Cap enrichment to avoid long-running syncs
                detail_limit = config.DETAIL_ENRICHMENT_LIMIT
                if invoices_needing_detail:
                    if len(invoices_needing_detail) > detail_limit:
                        logger.info(
                            f"Detail enrichment: {len(invoices_needing_detail)} invoices need it, "
                            f"capping to {detail_limit} per sync cycle"
                        )
                        invoices_needing_detail = invoices_needing_detail[:detail_limit]
                attempted_numbers = set()
                if invoices_needing_detail:
                    fatturapro.enrich_invoices_from_detail(invoices_needing_detail, delay=0.3)

                    # Marca il tentativo (riuscito o no) così il prossimo
                    # ciclo ruota su fatture diverse.
                    attempted_numbers = {
                        inv["invoice_number"] for inv in invoices_needing_detail
                    }
                    session.query(Invoice).filter(
                        Invoice.source_platform == "fatturapro",
                        Invoice.invoice_number.in_(attempted_numbers),
                    ).update(
                        {Invoice.detail_attempted_at: datetime.utcnow()},
                        synchronize_session=False,
                    )

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
                        # Presente nella lista: azzera il conteggio assenze
                        # della payment detection.
                        existing.missing_streak = 0
                        # Mai sovrascrivere il nome raw con un valore vuoto:
                        # è l'evidenza usata dall'audit abbinamenti.
                        if inv.get("customer_name"):
                            existing.customer_name_raw = inv.get("customer_name")
                        if inv.get("date"):
                            existing.issue_date = inv["date"]
                        # Enrich P.IVA if we found one and existing doesn't have it
                        if inv.get("customer_piva") and not existing.customer_piva_raw:
                            existing.customer_piva_raw = inv["customer_piva"]
                            piva_enriched += 1
                            logger.info(
                                f"Enriched existing invoice {inv_num} with P.IVA: "
                                f"{inv['customer_piva']}"
                            )
                        # Scadenza REALE trovata: sovrascrive anche una
                        # scadenza 'assumed' sintetizzata in passato.
                        if inv.get("due_date") and existing.due_date_source != "real":
                            existing.due_date = inv["due_date"]
                            existing.due_date_source = "real"
                            due_enriched += 1
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
                            due_date=inv.get("due_date"),
                            due_date_source="real" if inv.get("due_date") else None,
                            customer_name_raw=inv.get("customer_name"),
                            customer_piva_raw=inv.get("customer_piva"),
                            source_platform="fatturapro",
                            source_id=inv.get("doc_id"),
                            # Il bulk-update dei tentativi gira PRIMA di
                            # questo insert: senza lo stamp qui, le fatture
                            # nuove monopolizzerebbero il cap al prossimo
                            # ciclo pur essendo appena state arricchite.
                            detail_attempted_at=(
                                datetime.utcnow() if inv_num in attempted_numbers else None
                            ),
                        )
                        session.add(new_invoice)
                        created += 1
                        if inv.get("due_date"):
                            due_enriched += 1

                # PAYMENT DETECTION — solo su fetch COMPLETO.
                paid_detected = 0
                if not partial:
                    known_fp_invoices = session.query(Invoice).filter(
                        Invoice.source_platform == "fatturapro",
                        Invoice.status != "paid",
                    ).all()

                    # Ulteriore guardia: se il fetch copre meno della metà
                    # delle fatture aperte note, qualcosa non torna.
                    if known_fp_invoices and len(fetched_invoice_numbers) < len(known_fp_invoices) * 0.5:
                        logger.warning(
                            f"Fetch covers only {len(fetched_invoice_numbers)} of "
                            f"{len(known_fp_invoices)} known open invoices — "
                            f"treating as PARTIAL, skipping payment detection"
                        )
                        result["fatturapro"]["partial"] = True
                    else:
                        for known_inv in known_fp_invoices:
                            if known_inv.invoice_number not in fetched_invoice_numbers:
                                # La "pagata" è un'inferenza per assenza: una
                                # riga persa silenziosamente dal fetch non
                                # deve sparire dai conteggi scadute. Si marca
                                # paid solo alla SECONDA assenza consecutiva
                                # su fetch completi.
                                streak = (known_inv.missing_streak or 0) + 1
                                if streak >= PAID_ABSENCE_STREAK:
                                    known_inv.status = "paid"
                                    known_inv.amount_due = 0
                                    known_inv.missing_streak = 0
                                    known_inv.updated_at = datetime.utcnow()
                                    paid_detected += 1
                                    logger.info(
                                        f"Payment detected: FatturaPro invoice {known_inv.invoice_number} "
                                        f"absent from {streak} consecutive complete fetches — marked as paid"
                                    )
                                else:
                                    known_inv.missing_streak = streak
                                    logger.info(
                                        f"Invoice {known_inv.invoice_number} absent from complete "
                                        f"fetch ({streak}/{PAID_ABSENCE_STREAK}) — awaiting confirmation "
                                        f"before marking paid"
                                    )
                else:
                    logger.warning("PARTIAL fetch — payment detection skipped")

                session.commit()
                result["fatturapro"]["success"] = True
                result["fatturapro"]["created"] = created
                result["fatturapro"]["updated"] = updated
                result["fatturapro"]["paid_detected"] = paid_detected
                result["fatturapro"]["piva_enriched"] = piva_enriched
                result["fatturapro"]["due_date_enriched"] = due_enriched
                logger.info(
                    f"FatturaPro sync: created={created}, updated={updated}, "
                    f"paid_detected={paid_detected}, piva_enriched={piva_enriched}, "
                    f"due_dates={due_enriched}, partial={result['fatturapro']['partial']}"
                )
            else:
                result["fatturapro"]["error"] = (
                    "Login failed — check FATTURAPRO_USERNAME/PASSWORD env vars"
                )
                logger.error("FatturaPro login failed — cannot sync invoices")
        except Exception as e:
            result["fatturapro"]["error"] = str(e)
            result["fatturapro"]["partial"] = True
            logger.error(f"Error syncing FatturaPro: {e}", exc_info=True)
        finally:
            try:
                if fatturapro:
                    fatturapro.close()
            except Exception:
                pass

        # RECALCULATE days_overdue for ALL unpaid invoices (dynamic, not stale)
        _recalculate_days_overdue(session)

        _persist_sync_status("invoices", result)

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
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()

    return result


def _recalculate_days_overdue(session):
    """Recalculate days_overdue for all unpaid invoices based on current date.

    Positive values = days overdue (past due date).
    Negative values = days remaining until due date.

    Se manca la scadenza, ne sintetizza una a 30 giorni dall'emissione e la
    marca ESPLICITAMENTE come 'assumed': la UI la mostra come stimata e il
    messaggio WhatsApp non la spaccia per vera. Una scadenza 'real' non
    viene MAI toccata da questo ricalcolo.
    """
    today = date.today()
    unpaid_invoices = session.query(Invoice).filter(
        Invoice.status != "paid"
    ).all()

    updated = 0
    for inv in unpaid_invoices:
        if inv.due_date:
            # Actual due_date exists — calculate difference (can be negative)
            new_days = (today - inv.due_date).days
            if inv.due_date_source is None:
                # Riga storica non classificata: emissione+30 esatti è il
                # marchio del vecchio ricalcolo → stimata.
                if inv.issue_date and inv.due_date == inv.issue_date + timedelta(days=30):
                    inv.due_date_source = "assumed"
                else:
                    inv.due_date_source = "real"
        elif inv.issue_date:
            # No due_date: assume 30-day payment terms, and SAVE the assumed due_date
            assumed_due = inv.issue_date + timedelta(days=30)
            inv.due_date = assumed_due
            inv.due_date_source = "assumed"
            new_days = (today - assumed_due).days
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

    session.commit()
    if updated > 0:
        logger.info(f"Recalculated days_overdue for {updated} invoices")


def _sync_customers_task() -> dict:
    """Background task to sync customers from Shopify.

    L'auto-creazione di clienti dalle fatture NON avviene più qui: gira
    come step dedicato DOPO il matching (vedi _auto_create_task), così le
    fatture con un candidato fuzzy finiscono in quarantena invece di
    generare clienti duplicati.
    """
    session = get_session_direct()
    result = {"success": False, "created": 0, "updated": 0, "error": None}

    try:
        try:
            if config.SHOPIFY_ACCESS_TOKEN or (
                config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET
            ):
                logger.info("Syncing customers from Shopify...")
                shopify = ShopifyConnector()
                raw_customers = shopify.fetch_b2b_customers()
                created, updated, adopted = 0, 0, 0

                # Clienti nati dalle fatture (auto-create, shopify_id NULL):
                # se su Shopify esiste lo stesso esercizio (stessa P.IVA
                # validata), va ADOTTATO — shopify_id + contatti sulla riga
                # esistente — invece di creare un duplicato. Il duplicato
                # renderebbe pure ambiguo il matching per P.IVA (quarantena).
                orphans_by_piva = {}
                for orphan in session.query(Customer).filter(
                    Customer.shopify_id.is_(None),
                    Customer.partita_iva.isnot(None),
                ).all():
                    orphan_piva = validate_piva(orphan.partita_iva)
                    if orphan_piva and orphan_piva not in orphans_by_piva:
                        orphans_by_piva[orphan_piva] = orphan

                for cust in raw_customers:
                    existing = session.query(Customer).filter_by(
                        shopify_id=cust["shopify_id"]
                    ).first()

                    was_adopted = False
                    cust_piva = validate_piva(cust.get("partita_iva"))
                    if not existing:
                        orphan = orphans_by_piva.pop(cust_piva, None) if cust_piva else None
                        if orphan is not None:
                            orphan.shopify_id = cust["shopify_id"]
                            existing = orphan
                            was_adopted = True
                            adopted += 1
                            logger.info(
                                f"Adopted fatturapro-born customer "
                                f"'{orphan.ragione_sociale}' (P.IVA {cust_piva}) "
                                f"→ Shopify {cust['shopify_id']}"
                            )
                    elif cust_piva and cust_piva in orphans_by_piva:
                        # Il cliente Shopify esiste già E c'è un orfano con
                        # la stessa P.IVA: duplicato che manderà le fatture
                        # in quarantena piva_ambiguous. Il merge di due
                        # anagrafiche (fatture/pratiche/azioni) è manuale:
                        # lo si segnala, non lo si improvvisa.
                        dup = orphans_by_piva[cust_piva]
                        logger.warning(
                            f"Duplicate customers with P.IVA {cust_piva}: "
                            f"Shopify '{existing.ragione_sociale}' (id {existing.id}) "
                            f"+ fatturapro-born '{dup.ragione_sociale}' (id {dup.id}) "
                            f"— merge manuale necessario"
                        )

                    if existing:
                        # Mai sovrascrivere un nome buono con uno vuoto (un
                        # profilo Shopify senza company produce ""), e per i
                        # clienti ADOTTATI tenere il nome derivato dalle
                        # fatture: è quello su cui lavora il matching.
                        parsed_name = (cust.get("ragione_sociale") or "").strip()
                        if parsed_name and not (was_adopted and existing.ragione_sociale):
                            existing.ragione_sociale = parsed_name
                            existing.ragione_sociale_normalized = normalize_ragione_sociale(
                                parsed_name
                            )
                        existing.partita_iva = cust.get("partita_iva") or existing.partita_iva
                        existing.codice_fiscale = cust.get("codice_fiscale") or existing.codice_fiscale
                        existing.codice_sdi = cust.get("codice_sdi") or existing.codice_sdi
                        existing.phone = cust.get("phone") or existing.phone
                        existing.email = cust.get("email") or existing.email
                        existing.tags = cust.get("tags") or existing.tags
                        if cust.get("phones"):
                            existing.phones_json = cust["phones"]
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
                            phones_json=cust.get("phones"),
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
                result["adopted"] = adopted
                logger.info(
                    f"Shopify sync: created={created}, updated={updated}, adopted={adopted}"
                )
            else:
                result["success"] = True  # Not an error, just unconfigured
                logger.debug("Shopify not configured")
        except Exception as e:
            logger.error(f"Shopify sync failed: {e}", exc_info=True)
            result["shopify_error"] = str(e)
            try:
                session.rollback()
            except Exception:
                pass

        _persist_sync_status("customers", result)

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
        _persist_sync_status("customers", result)
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()

    return result


def _auto_create_task() -> dict:
    """Auto-create Customer records from invoices with NO candidate at all.

    Gira DOPO run_matching: a questo punto ogni fattura senza cliente
    o ha un suggerimento in quarantena (→ si salta, decide l'operatore)
    o non ha davvero nessun candidato (→ si crea il cliente).

    Regole:
    - mai toccare la P.IVA di un cliente esistente (il "poisoning" della
      P.IVA è stata la causa principale dei profili con fatture altrui);
    - P.IVA usata solo se valida (checksum);
    - fatture 'unlinked' (scollegate a mano) escluse per sempre.
    """
    session = get_session_direct()
    result = {"auto_created": 0, "matched_within_run": 0, "skipped_suggested": 0}

    try:
        candidates = session.query(Invoice).filter(
            Invoice.customer_id.is_(None),
            Invoice.status != "paid",
        ).all()

        # Lookup delle entità CREATE IN QUESTA RUN (per non duplicare un
        # cliente che compare su più fatture nello stesso sync).
        run_piva_map = {}
        run_name_map = {}

        for inv in candidates:
            try:
                if inv.suggested_customer_id is not None:
                    result["skipped_suggested"] += 1
                    continue
                if inv.match_method == "unlinked":
                    continue

                piva = validate_piva(inv.customer_piva_raw)
                name = (inv.customer_name_raw or "").strip()
                name_norm = normalize_ragione_sociale(name) if name else ""

                if not name and not piva:
                    continue

                # Già creato in questa run?
                existing_id = None
                if piva and piva in run_piva_map:
                    existing_id = run_piva_map[piva]
                elif name_norm and name_norm in run_name_map:
                    candidate_id, candidate_piva = run_name_map[name_norm]
                    if piva and candidate_piva and piva == candidate_piva:
                        # Stessa P.IVA validata: stessa entità, aggancio sicuro.
                        existing_id = candidate_id
                    elif piva and candidate_piva:
                        # P.IVA in conflitto = entità diverse: si prosegue e
                        # si crea un cliente separato.
                        pass
                    else:
                        # Merge sul SOLO nome normalizzato (una o entrambe le
                        # P.IVA mancanti): il normalizzatore è aggressivo
                        # ('Trattoria X di Mario Rossi' e 'Trattoria X di
                        # Luigi Bianchi' collassano sullo stesso nome), quindi
                        # niente aggancio automatico → suggerimento in
                        # quarantena, decide l'operatore.
                        inv.suggested_customer_id = candidate_id
                        inv.suggested_method = "name_ambiguous"
                        inv.suggested_score = 100
                        result["skipped_suggested"] += 1
                        logger.info(
                            f"Invoice {inv.invoice_number}: name-only merge with "
                            f"run-created customer {candidate_id} degraded to "
                            f"suggestion (P.IVA not verifiable on both sides)"
                        )
                        continue

                if existing_id:
                    inv.customer_id = existing_id
                    inv.match_method = "auto_created"
                    inv.match_score = 100
                    result["matched_within_run"] += 1
                    continue

                new_customer = Customer(
                    ragione_sociale=name if name else f"Cliente P.IVA {piva}",
                    ragione_sociale_normalized=name_norm,
                    partita_iva=piva,
                    source=inv.source_platform,
                )
                session.add(new_customer)
                session.flush()  # Get the ID

                inv.customer_id = new_customer.id
                inv.match_method = "auto_created"
                inv.match_score = 100
                result["auto_created"] += 1

                if piva:
                    run_piva_map[piva] = new_customer.id
                if name_norm:
                    run_name_map[name_norm] = (new_customer.id, piva)

                logger.info(
                    f"Auto-created customer '{new_customer.ragione_sociale}' "
                    f"(P.IVA: {piva or 'N/A'}) from {inv.source_platform} invoice {inv.invoice_number}"
                )
            except Exception as e:
                logger.warning(f"Error processing invoice {inv.invoice_number} for auto-create: {e}")
                continue

        session.commit()
        _persist_sync_status("auto_create", result)

        session.add(ActivityLog(
            action="auto_create",
            entity_type="customer",
            details=result,
        ))
        session.commit()

        logger.info(f"Auto-create complete: {result}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Error in auto-create: {e}", exc_info=True)
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()

    return result


def _match_orders_task() -> dict:
    """Match invoices to Shopify orders by customer + amount + date.

    For each customer with a shopify_id, fetches their Shopify orders
    and matches unlinked invoices by amount (±1% tolerance) and date
    proximity (invoice issue_date within 7 days of order created_at).
    """
    session = get_session_direct()
    result = {
        "matched": 0, "customers_processed": 0,
        "errors": [], "already_matched": 0,
    }
    try:
        # Get all customers that have a shopify_id
        customers = session.query(Customer).filter(
            Customer.shopify_id.isnot(None),
        ).all()

        if not customers:
            result["message"] = "No customers with shopify_id"
            return result

        shopify = ShopifyConnector()

        for cust in customers:
            # Get unmatched invoices for this customer
            unmatched_invoices = session.query(Invoice).filter(
                Invoice.customer_id == cust.id,
                Invoice.shopify_order_id.is_(None),
            ).all()

            if not unmatched_invoices:
                continue

            result["customers_processed"] += 1

            try:
                orders = shopify.fetch_customer_orders(
                    cust.shopify_id
                )
            except Exception as e:
                result["errors"].append(
                    f"{cust.ragione_sociale}: {e}"
                )
                continue

            if not orders:
                continue

            # Build order lookup for matching
            for inv in unmatched_invoices:
                best_match = _find_best_order_match(
                    inv, orders
                )
                if best_match:
                    inv.shopify_order_id = best_match["id"]
                    inv.shopify_order_number = (
                        best_match["name"]
                    )
                    result["matched"] += 1
                    logger.info(
                        f"Matched invoice {inv.invoice_number}"
                        f" → order {best_match['name']}"
                    )

        session.commit()

        _persist_sync_status("order_matching", result)
        activity = ActivityLog(
            action="order_matching",
            entity_type="invoice",
            details=result,
        )
        session.add(activity)
        session.commit()

        logger.info(f"Order matching complete: {result}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(
            f"Error in order matching: {e}", exc_info=True
        )
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()

    return result


def _find_best_order_match(
    invoice, orders
):
    """Find the best Shopify order match for an invoice.

    Matching criteria:
    1. Amount match: order total within 1% of invoice amount
    2. Date proximity: order within 30 days of invoice date
       (invoices are often issued days/weeks after the order)

    Returns the best matching order or None.
    """
    if not invoice.issue_date:
        # Senza data il match per solo importo è troppo fragile (il primo
        # ordine entro tolleranza vince e il link è sticky: mai più
        # ricontrollato). Meglio nessun match: la data arriva col sync.
        return None

    best = None
    best_score = float("inf")

    for order in orders:
        # Amount check (1% tolerance, minimum €0.50)
        amt_diff = abs(
            order["total_price"] - invoice.amount
        )
        tolerance = max(invoice.amount * 0.01, 0.50)
        if amt_diff > tolerance:
            continue

        # Date check — parse order date
        try:
            order_date_str = order["created_at"][:10]
            order_date = datetime.strptime(
                order_date_str, "%Y-%m-%d"
            ).date()
        except (ValueError, TypeError):
            continue

        day_diff = abs(
            (invoice.issue_date - order_date).days
        )
        if day_diff > 30:
            continue

        # Score: lower is better (prefer closer date +
        # closer amount)
        score = day_diff + (amt_diff / max(tolerance, 1))
        if score < best_score:
            best_score = score
            best = order

    return best


def _run_matching_task() -> dict:
    """Background task to run invoice-customer matching."""
    session = get_session_direct()
    try:
        logger.info("Running invoice-customer matching...")
        result = run_matching(session)
        logger.info(f"Matching result: {result}")

        _persist_sync_status("matching", result)

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
        _persist_sync_status("matching", error_result)
        try:
            session.rollback()
        except Exception:
            pass
        return error_result
    finally:
        session.close()


def _case_lifecycle_task(allow_close: bool = True) -> dict:
    """Background task: apre/aggancia/chiude le pratiche di recupero."""
    session = get_session_direct()
    try:
        logger.info(f"Updating case lifecycle (allow_close={allow_close})...")
        result = update_case_lifecycle(session, allow_close=allow_close)
        result["allow_close"] = allow_close
        _persist_sync_status("cases", result)

        session.add(ActivityLog(
            action="case_lifecycle",
            entity_type="case",
            details=result,
        ))
        session.commit()
        return result
    except Exception as e:
        logger.error(f"Error in case lifecycle: {e}", exc_info=True)
        error_result = {"error": str(e)}
        _persist_sync_status("cases", error_result)
        try:
            session.rollback()
        except Exception:
            pass
        return error_result
    finally:
        session.close()


def _locked_task(task_fn):
    """Wrapper: run a sync task under the global _sync_lock.

    If the lock is already held (another sync is running), the task
    is skipped and an error result is returned instead of blocking.
    """
    def wrapper():
        if not _sync_lock.acquire(blocking=False):
            logger.warning(f"Skipping {task_fn.__name__}: another sync is already running")
            return {"error": "Another sync operation is already running. Try again later."}
        try:
            return task_fn()
        finally:
            _sync_lock.release()
    wrapper.__name__ = task_fn.__name__
    return wrapper


@router.post("/invoices")
async def sync_invoices(background_tasks: BackgroundTasks):
    """Trigger manual sync of invoices from FatturaPro."""
    background_tasks.add_task(_locked_task(_sync_invoices_task))
    return {
        "status": "sync_started",
        "message": "Invoice sync started in background"
    }


@router.post("/customers")
async def sync_customers(background_tasks: BackgroundTasks):
    """Trigger manual sync of customers from Shopify."""
    background_tasks.add_task(_locked_task(_sync_customers_task))
    return {
        "status": "sync_started",
        "message": "Customer sync started in background"
    }


@router.post("/matching")
async def sync_matching(background_tasks: BackgroundTasks):
    """Trigger manual matching run."""
    background_tasks.add_task(_locked_task(_run_matching_task))
    return {
        "status": "sync_started",
        "message": "Matching sync started in background"
    }


@router.post("/order-matching")
async def sync_order_matching(background_tasks: BackgroundTasks):
    """Match invoices to Shopify orders by amount + date."""
    background_tasks.add_task(_locked_task(_match_orders_task))
    return {
        "status": "sync_started",
        "message": "Order matching started in background",
    }


@router.post("/cases")
async def sync_cases(background_tasks: BackgroundTasks):
    """Trigger manual case lifecycle update."""
    background_tasks.add_task(_locked_task(_case_lifecycle_task))
    return {
        "status": "sync_started",
        "message": "Case lifecycle update started in background",
    }


def _full_sync_task() -> dict:
    """Run full sync sequentially:
    invoices → customers → matching → auto-create → order matching → cases.

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

        # Step 2: Sync customers from Shopify
        try:
            results["customers"] = _sync_customers_task()
        except Exception as e:
            logger.error(f"Customer sync failed: {e}", exc_info=True)
            results["customers"] = {"error": str(e)}

        # Step 3: Matching (abbinamenti sicuri + quarantena suggerimenti)
        try:
            results["matching"] = _run_matching_task()
        except Exception as e:
            logger.error(f"Matching failed: {e}", exc_info=True)
            results["matching"] = {"error": str(e)}

        # Step 4: Auto-create clienti SOLO per fatture senza alcun candidato
        # (deve girare DOPO il matching, mai prima)
        try:
            results["auto_create"] = _auto_create_task()
        except Exception as e:
            logger.error(f"Auto-create failed: {e}", exc_info=True)
            results["auto_create"] = {"error": str(e)}

        # Step 5: Match invoices to Shopify orders
        try:
            results["order_matching"] = _match_orders_task()
        except Exception as e:
            logger.error(
                f"Order matching failed: {e}", exc_info=True
            )
            results["order_matching"] = {"error": str(e)}

        # Step 6: Case lifecycle. Con fetch fatture PARZIALE la payment
        # detection non è affidabile → niente chiusure (solo aperture).
        try:
            invoices_result = results.get("invoices", {})
            fp = invoices_result.get("fatturapro", {}) if isinstance(invoices_result, dict) else {}
            fetch_ok = bool(fp.get("success")) and not fp.get("partial")
            results["cases"] = _case_lifecycle_task(allow_close=fetch_ok)
        except Exception as e:
            logger.error(f"Case lifecycle failed: {e}", exc_info=True)
            results["cases"] = {"error": str(e)}

        logger.info(f"Full sync completed: {results}")
        return results
    finally:
        _sync_lock.release()


@router.post("/full")
async def sync_full(background_tasks: BackgroundTasks):
    """Trigger full sync (sequential):
    invoices → customers → matching → auto-create → order matching → cases."""
    background_tasks.add_task(_full_sync_task)

    return {
        "status": "sync_started",
        "message": "Full sync started in background"
    }


@router.get("/status")
async def get_sync_status():
    """Get the last sync timestamps and results."""
    _load_sync_state()
    return {
        "last_sync": _sync_status,
        "scheduler": get_scheduler_status(),
    }


@router.post("/cleanup-stale-f24")
async def cleanup_stale_f24():
    """Mark all Fattura24 invoices as paid (cleanup for stale data when F24 API is unavailable).

    This is a one-off maintenance endpoint. Use CSV import to re-add F24 invoices if needed.
    """
    session = get_session_direct()
    try:
        stale = session.query(Invoice).filter(
            Invoice.source_platform == "fatture24",
            Invoice.status != "paid",
        ).all()

        count = 0
        for inv in stale:
            inv.status = "paid"
            inv.amount_due = 0
            inv.days_overdue = 0
            count += 1

        session.commit()

        # Log activity
        activity = ActivityLog(
            action="cleanup_stale_f24",
            entity_type="invoice",
            details={"marked_paid": count}
        )
        session.add(activity)
        session.commit()

        logger.info(f"Cleanup: marked {count} stale Fattura24 invoices as paid")
        return {"marked_paid": count, "message": f"Marked {count} Fattura24 invoices as paid"}
    except Exception as e:
        logger.error(f"Error in F24 cleanup: {e}", exc_info=True)
        session.rollback()
        return {"error": str(e)}
    finally:
        session.close()


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
    session = get_session_direct()
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
                if due_date:
                    existing.due_date = due_date
                    existing.due_date_source = "real"
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
                    due_date_source="real" if due_date else None,
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
