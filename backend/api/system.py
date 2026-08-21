"""System health and diagnostics API endpoint."""

import logging
from datetime import datetime, date
from fastapi import APIRouter, Query
from sqlalchemy import func, text

from backend.database import get_session_direct, Customer, Invoice, RecoveryCase, ActivityLog
from backend.config import config
from backend.scheduler import get_scheduler_status
from backend.api.sync import _sync_status, _load_sync_state

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_system_status():
    """
    Comprehensive system health and alignment diagnostics.

    Returns:
        - connectors: status of each external connector (configured, last error)
        - database: connection health, table counts
        - sync: last sync times, results, staleness
        - data_integrity: checks for orphaned invoices, missing matches, etc.
        - scheduler: cron status
        - alerts: list of issues requiring attention
    """
    # Load persisted sync state from DB (once)
    _load_sync_state()

    session = get_session_direct()
    alerts = []

    try:
        # --- 1. Database Health ---
        db_ok = False
        db_latency_ms = 0
        try:
            t0 = datetime.utcnow()
            session.execute(text("SELECT 1"))
            db_latency_ms = (datetime.utcnow() - t0).total_seconds() * 1000
            db_ok = True
        except Exception as e:
            alerts.append({
                "level": "critical",
                "component": "database",
                "message": f"Database connection failed: {e}"
            })

        # Table counts (esclusi i clienti fusi: sono nascosti dagli elenchi)
        total_customers = session.query(func.count(Customer.id)).filter(
            Customer.merged_into.is_(None)
        ).scalar() or 0
        total_invoices = session.query(func.count(Invoice.id)).scalar() or 0
        open_cases = session.query(func.count(RecoveryCase.id)).filter(
            RecoveryCase.status == "open"
        ).scalar() or 0

        customers_shopify = session.query(func.count(Customer.id)).filter(
            Customer.source == "shopify",
            Customer.merged_into.is_(None),
        ).scalar() or 0
        customers_auto = total_customers - customers_shopify

        invoices_open = session.query(func.count(Invoice.id)).filter(
            Invoice.status != "paid"
        ).scalar() or 0
        invoices_paid = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "paid"
        ).scalar() or 0
        invoices_matched = session.query(func.count(Invoice.id)).filter(
            Invoice.customer_id.isnot(None)
        ).scalar() or 0
        invoices_unmatched = session.query(func.count(Invoice.id)).filter(
            Invoice.customer_id.is_(None)
        ).scalar() or 0

        invoices_fp = session.query(func.count(Invoice.id)).filter(
            Invoice.source_platform == "fatturapro"
        ).scalar() or 0
        invoices_f24 = session.query(func.count(Invoice.id)).filter(
            Invoice.source_platform == "fatture24"
        ).scalar() or 0

        total_crediti = session.query(func.sum(Invoice.amount_due)).filter(
            Invoice.status != "paid"
        ).scalar() or 0.0

        # --- 2. Connectors Status ---
        creds = config.validate()

        connectors = {
            "fatturapro": {
                "configured": bool(config.FATTURAPRO_USERNAME and config.FATTURAPRO_PASSWORD),
                "status": "unknown",
                "last_result": None,
            },
            "fattura24": {
                "configured": False,  # API dismessa (abbonamento scaduto): dati importati via CSV
                "status": "unknown",
                "last_result": None,
            },
            "shopify": {
                "configured": creds.get("shopify", False),
                "api_version": config.SHOPIFY_API_VERSION,
                "status": "unknown",
                "last_result": None,
            },
        }

        # Fattura24: API dismessa — se ci sono fatture in DB sono importate via CSV
        if invoices_f24 > 0:
            connectors["fattura24"]["status"] = "imported"
            connectors["fattura24"]["last_result"] = {
                "success": True,
                "imported_count": invoices_f24,
            }
        else:
            connectors["fattura24"]["status"] = "dismissed"

        # Enrich from last sync
        inv_result = _sync_status.get("invoices", {}).get("result")
        if inv_result:
            fp = inv_result.get("fatturapro", {})
            connectors["fatturapro"]["status"] = "ok" if fp.get("success") else "error"
            connectors["fatturapro"]["last_result"] = {
                "success": fp.get("success"),
                "created": fp.get("created", 0),
                "updated": fp.get("updated", 0),
                "paid_detected": fp.get("paid_detected", 0),
                "error": fp.get("error"),
            }

        cust_result = _sync_status.get("customers", {}).get("result")
        if not connectors["shopify"]["configured"]:
            # Senza credenziali il task clienti "riesce" senza far nulla
            # (success=True): il connettore NON deve risultare 'ok'.
            connectors["shopify"]["status"] = "unconfigured"
        elif cust_result:
            shopify_err = cust_result.get("shopify_error")
            if shopify_err:
                connectors["shopify"]["status"] = "error"
                connectors["shopify"]["error"] = shopify_err
            elif cust_result.get("success"):
                connectors["shopify"]["status"] = "ok"

        # --- 3. Sync Status ---
        sync_info = {}
        for key in ["invoices", "customers", "matching", "order_matching", "cases"]:
            s = _sync_status.get(key, {})
            last = s.get("last_sync")
            stale = False
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    hours_ago = (datetime.utcnow() - last_dt).total_seconds() / 3600
                    stale = hours_ago > 25  # More than 25 hours = stale
                except (ValueError, TypeError):
                    pass

            sync_info[key] = {
                "last_sync": last,
                "stale": stale,
                "result_summary": _summarize_sync_result(key, s.get("result")),
            }

        # --- 4. Data Integrity Checks ---
        integrity = {}

        # Check: invoices without customer match
        integrity["invoices_unmatched"] = {
            "count": invoices_unmatched,
            "status": "warning" if invoices_unmatched > 50 else "ok",
            "description": "Fatture senza cliente associato",
        }

        # Check: customers without invoices
        customers_no_invoices = session.query(func.count(Customer.id)).filter(
            Customer.merged_into.is_(None),
            ~Customer.id.in_(
                session.query(Invoice.customer_id).filter(
                    Invoice.customer_id.isnot(None)
                ).distinct()
            )
        ).scalar() or 0
        integrity["customers_no_invoices"] = {
            "count": customers_no_invoices,
            "status": "info",
            "description": "Clienti senza fatture associate",
        }

        # Check: invoices with days_overdue = 0 but actually overdue
        stale_overdue = session.query(func.count(Invoice.id)).filter(
            Invoice.status != "paid",
            Invoice.days_overdue == 0,
            Invoice.due_date.isnot(None),
            Invoice.due_date < date.today(),
        ).scalar() or 0
        integrity["stale_days_overdue"] = {
            "count": stale_overdue,
            "status": "warning" if stale_overdue > 0 else "ok",
            "description": "Fatture scadute con days_overdue=0 (calcolo non aggiornato)",
        }

        # Check: customers in active recovery with NO remaining overdue
        # (all overdue invoices paid → status should be updated)
        active_cust_ids = session.query(Customer.id).filter(
            Customer.excluded.is_(False),
            Customer.merged_into.is_(None),
            Customer.recovery_status.in_(
                ["first_contact", "second_contact", "lawyer"]
            ),
        ).all()
        fully_resolved_count = 0
        for (cid,) in active_cust_ids:
            remaining = session.query(func.count(Invoice.id)).filter(
                Invoice.customer_id == cid,
                Invoice.status != "paid",
                Invoice.days_overdue > 0,
            ).scalar() or 0
            if remaining == 0:
                fully_resolved_count += 1
        integrity["paid_in_active_recovery"] = {
            "count": fully_resolved_count,
            "status": (
                "warning" if fully_resolved_count > 0 else "ok"
            ),
            "description": (
                "Clienti in recupero attivo senza fatture scadute "
                "residue (stato da aggiornare)"
            ),
        }

        # --- 5. Scheduler ---
        scheduler = get_scheduler_status()

        # --- 6. Generate Alerts ---
        # Connector alerts
        if not connectors["fatturapro"]["configured"]:
            alerts.append({
                "level": "critical",
                "component": "fatturapro",
                "message": "FatturaPro non configurato — impostare FATTURAPRO_USERNAME e FATTURAPRO_PASSWORD su Render"
            })
        elif connectors["fatturapro"]["status"] == "error":
            alerts.append({
                "level": "error",
                "component": "fatturapro",
                "message": f"FatturaPro errore: {connectors['fatturapro']['last_result'].get('error', 'sconosciuto')}"
            })

        shopify_err = connectors["shopify"].get("error")
        if shopify_err:
            if "401" in str(shopify_err):
                alerts.append({
                    "level": "error",
                    "component": "shopify",
                    "message": "Shopify non raggiungibile — verificare "
                    "SHOPIFY_CLIENT_ID e SHOPIFY_CLIENT_SECRET su Render"
                })
            else:
                alerts.append({
                    "level": "error",
                    "component": "shopify",
                    "message": f"Shopify errore: {shopify_err}"
                })
        elif not connectors["shopify"]["configured"]:
            alerts.append({
                "level": "warning",
                "component": "shopify",
                "message": "Shopify non configurato"
            })

        # Sync staleness alerts
        for key, info in sync_info.items():
            if info["stale"]:
                alerts.append({
                    "level": "warning",
                    "component": f"sync_{key}",
                    "message": f"Sync {key} non eseguito da più di 24 ore"
                })

        # Data integrity alerts
        for check_name, check in integrity.items():
            if check["status"] == "warning":
                alerts.append({
                    "level": "warning",
                    "component": f"data_{check_name}",
                    "message": f"{check['description']}: {check['count']}"
                })

        # Last activity log
        last_activity = session.query(ActivityLog).order_by(
            ActivityLog.timestamp.desc()
        ).first()

        return {
            "status": "healthy" if db_ok and not any(
                a["level"] == "critical" for a in alerts
            ) else "degraded",
            "timestamp": datetime.utcnow().isoformat(),
            "database": {
                "connected": db_ok,
                "latency_ms": round(db_latency_ms, 1),
                "tables": {
                    "customers": {
                        "total": total_customers,
                        "shopify": customers_shopify,
                        "auto_created": customers_auto,
                    },
                    "invoices": {
                        "total": total_invoices,
                        "open": invoices_open,
                        "paid": invoices_paid,
                        "matched": invoices_matched,
                        "unmatched": invoices_unmatched,
                        "fatturapro": invoices_fp,
                        "fattura24": invoices_f24,
                    },
                    "cases": {"open": open_cases},
                },
                "totals": {
                    "crediti_aperti": round(total_crediti, 2),
                },
            },
            "connectors": connectors,
            "sync": sync_info,
            "integrity": integrity,
            "scheduler": scheduler,
            "alerts": alerts,
            "last_activity": {
                "timestamp": last_activity.timestamp.isoformat() if last_activity else None,
                "action": last_activity.action if last_activity else None,
            } if last_activity else None,
        }

    except Exception as e:
        logger.error(f"Error in system diagnostics: {e}", exc_info=True)
        return {
            "status": "error",
            "timestamp": datetime.utcnow().isoformat(),
            "error": str(e),
            "alerts": alerts,
        }
    finally:
        session.close()


def _summarize_sync_result(key: str, result: dict) -> str:
    """Return a human-readable summary of a sync result."""
    if not result:
        return "Mai eseguito"

    if key == "invoices":
        fp = result.get("fatturapro", {})
        if fp.get("success"):
            summary = (
                f"FP: {fp.get('updated', 0)} agg, {fp.get('created', 0)} nuove, "
                f"{fp.get('paid_detected', 0)} pagate"
            )
            if fp.get("due_date_enriched"):
                summary += f", {fp['due_date_enriched']} scadenze reali"
            if fp.get("partial"):
                summary += " (PARZIALE)"
            return summary
        return "FP: errore"

    if key == "customers":
        if result.get("unconfigured"):
            return "Shopify non configurato"
        created = result.get("created", 0)
        shopify_err = result.get("shopify_error")
        parts = []
        if created > 0:
            parts.append(f"Shopify: {created} nuovi")
        if shopify_err:
            parts.append("Shopify: errore token")
        return " | ".join(parts) if parts else "Nessuna modifica"

    if key == "matching":
        total = result.get("total", 0)
        if total == 0:
            return "Tutte le fatture già associate"
        exact = result.get("matched_exact", 0) + result.get("matched_piva", 0)
        suggested = result.get("suggested", 0)
        unm = result.get("unmatched", 0)
        return f"{exact} sicure, {suggested} da confermare, {unm} non associate"

    if key == "order_matching":
        if result.get("error"):
            return f"Errore: {result['error']}"
        matched = result.get("matched", 0)
        errors = result.get("errors") or []
        near = result.get("near_misses") or []
        parts = [f"{matched} fatture agganciate a ordini Shopify"]
        if errors:
            parts.append(f"{len(errors)} clienti in errore")
        if near:
            parts.append(f"{len(near)} near-miss")
        return ", ".join(parts)

    if key == "cases":
        opened = result.get("opened", 0) + result.get("reopened", 0)
        closed = result.get("closed", 0)
        if opened == 0 and closed == 0:
            return "Nessuna variazione pratiche"
        return f"{opened} pratiche aperte, {closed} chiuse"

    return str(result)


# ── Audit abbinamenti fatture→clienti ────────────────────────────────

@router.get("/match-audit")
async def match_audit(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    only_problems: bool = Query(True, description="Solo esiti warn/bad"),
    include_paid: bool = Query(False, description="Audita anche le fatture pagate"),
    include_reviewed: bool = Query(
        False, description="Includi anche le fatture già segnate verificate"
    ),
):
    """Audit degli abbinamenti fattura→cliente, RAGGRUPPATO per cliente.

    Per ogni fattura abbinata confronta il destinatario della fattura con la
    ragione sociale del cliente e l'accordo P.IVA:
    - bad:  P.IVA in conflitto, nomi del tutto dissimili (score < 40), o
            P.IVA coincidente ma nomi dissimili (possibile P.IVA avvelenata
            sul cliente dal vecchio motore)
    - warn: nomi poco simili (score < 75), P.IVA fattura assente sul cliente,
            o nome fattura assente (verifica impossibile)
    - ok:   il resto

    Serve a trovare le fatture finite nel profilo sbagliato (i casi
    QOQA/Rooftop). Da lì: "Scollega", riassegnazione, "Assegna P.IVA al
    cliente" (quando la fattura ha una P.IVA valida e il cliente no) o
    "Segna verificato". Con include_paid=true copre anche le pagate (che
    inquinano i totali del profilo pur non contando nelle scadute).

    Le fatture già verificate a mano (audit_reviewed_at valorizzato) escono
    dai problemi, salvo include_reviewed=true.

    Risposta:
    - counts / total_audited / total_problems: conteggi per FATTURA (su tutte
      le fatture auditate, retrocompat);
    - reviewed_count: quante fatture problematiche sono già state verificate;
    - items: elenco PIATTO dei problemi (retrocompat);
    - groups: problemi RAGGRUPPATI per cliente (per l'indagine dall'operatore).
    """
    from backend.engine.verify import verify_invoice_customer
    from backend.engine.piva import validate_piva

    session = get_session_direct()
    try:
        query = session.query(Invoice).filter(
            Invoice.customer_id.isnot(None),
        )
        if not include_paid:
            query = query.filter(Invoice.status != "paid")
        invoices = query.order_by(Invoice.id).all()

        cust_ids = {inv.customer_id for inv in invoices}
        customers = {}
        if cust_ids:
            for c in session.query(Customer).filter(Customer.id.in_(cust_ids)).all():
                customers[c.id] = c

        # Totale fatture per cliente (una sola query aggregata): serve a dire
        # se il problema è 1 su N o riguarda tutte le fatture. DEVE coprire lo
        # stesso universo dell'audit (rispetta include_paid), altrimenti il
        # numeratore (problem_count, che include le pagate con include_paid)
        # può superare il denominatore → "2 fatture su 0".
        counts_query = session.query(
            Invoice.customer_id, func.count(Invoice.id)
        ).filter(Invoice.customer_id.isnot(None))
        if not include_paid:
            counts_query = counts_query.filter(Invoice.status != "paid")
        counts_by_customer = dict(
            counts_query.group_by(Invoice.customer_id).all()
        )

        results = []          # elenco piatto (retrocompat)
        groups_map = {}       # customer_id -> gruppo (solo clienti con problemi)
        counts = {"ok": 0, "warn": 0, "bad": 0}
        reviewed_count = 0
        for inv in invoices:
            cust = customers.get(inv.customer_id)
            if not cust:
                continue

            # Fonte di verità unica: lo stesso controllo del semaforo sul
            # profilo cliente (backend/engine/verify.py).
            v = verify_invoice_customer(inv, cust)
            verdict = v["verdict"]
            counts[verdict] += 1

            is_problem = verdict in ("warn", "bad")
            is_reviewed = inv.audit_reviewed_at is not None

            # Le fatture già verificate a mano escono dai problemi.
            if is_problem and is_reviewed:
                reviewed_count += 1
                if not include_reviewed:
                    continue

            if only_problems and verdict == "ok":
                continue

            record = {
                "invoice_id": inv.id,
                "invoice_number": inv.invoice_number,
                "amount_due": float(inv.amount_due),
                "status": inv.status,
                "customer_id": cust.id,
                "customer_name": cust.ragione_sociale,
                "customer_name_raw": inv.customer_name_raw,
                "customer_piva": cust.partita_iva,
                "customer_piva_raw": inv.customer_piva_raw,
                "match_method": inv.match_method,
                "match_score": inv.match_score,
                "name_score": v["name_score"],
                "verdict": verdict,
                "reasons": [v["message"]],
                "verification": v,
                "reviewed": is_reviewed,
                # La fattura ha una P.IVA valida e il cliente no: si può
                # copiare la P.IVA della fattura sul cliente con un click.
                "can_assign_piva": (
                    validate_piva(inv.customer_piva_raw) is not None
                    and validate_piva(cust.partita_iva) is None
                ),
            }
            results.append(record)

            # I gruppi contengono SOLO i problemi (mai gli ok).
            if is_problem:
                g = groups_map.get(cust.id)
                if g is None:
                    g = {
                        "customer_id": cust.id,
                        "customer_name": cust.ragione_sociale,
                        "customer_piva": cust.partita_iva,
                        "total_invoices": counts_by_customer.get(cust.id, 0),
                        "problem_count": 0,
                        "worst_verdict": "warn",
                        "problems_amount_due": 0.0,
                        "items": [],
                    }
                    groups_map[cust.id] = g
                g["items"].append(record)
                g["problem_count"] += 1
                g["problems_amount_due"] += float(inv.amount_due)
                if verdict == "bad":
                    g["worst_verdict"] = "bad"

        groups = list(groups_map.values())
        for g in groups:
            g["problems_amount_due"] = round(g["problems_amount_due"], 2)
        # Prima i clienti con almeno un critico, poi per numero di problemi.
        groups.sort(
            key=lambda g: (0 if g["worst_verdict"] == "bad" else 1, -g["problem_count"])
        )

        page = results[skip:skip + limit]
        return {
            "counts": counts,
            "total_audited": len(invoices),
            "total_problems": counts["warn"] + counts["bad"],
            "reviewed_count": reviewed_count,
            "skip": skip,
            "limit": limit,
            "items": page,
            "groups": groups,
        }
    finally:
        session.close()
