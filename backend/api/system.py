"""System health and diagnostics API endpoint."""

import logging
from datetime import datetime, date, timedelta
from fastapi import APIRouter
from sqlalchemy import func, text

from backend.database import get_session, get_engine, Customer, Invoice, Message, ActivityLog
from backend.config import config
from backend.scheduler import get_scheduler_status
from backend.api.sync import _sync_status, _load_sync_state

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/migrate")
@router.get("/migrate")
async def run_migrations():
    """Run schema migrations to add missing columns."""
    results = []
    try:
        from sqlalchemy import create_engine as _ce
        from sqlalchemy.pool import NullPool
        # Use the same pooler URL but with short timeouts
        migration_engine = _ce(
            config.DATABASE_URL,
            poolclass=NullPool,
            connect_args={"connect_timeout": 10, "options": "-c statement_timeout=15000"},
        )
        with migration_engine.begin() as conn:
            try:
                conn.execute(text('ALTER TABLE recovery_actions ADD COLUMN outcome VARCHAR'))
                results.append("outcome: added successfully")
            except Exception as e:
                err = str(e).lower()
                if 'already exists' in err or 'duplicate' in err:
                    results.append("outcome: already exists")
                else:
                    results.append(f"outcome error: {str(e)[:200]}")
        migration_engine.dispose()
    except Exception as e:
        results.append(f"error: {str(e)[:200]}")
    return {"migrations": results}


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

    session = get_session()
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

        # Table counts
        total_customers = session.query(func.count(Customer.id)).scalar() or 0
        total_invoices = session.query(func.count(Invoice.id)).scalar() or 0
        total_messages = session.query(func.count(Message.id)).scalar() or 0

        customers_shopify = session.query(func.count(Customer.id)).filter(
            Customer.source == "shopify"
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
                "configured": creds.get("fattura24", False),
                "status": "unknown",
                "last_result": None,
            },
            "shopify": {
                "configured": creds.get("shopify", False),
                "api_version": config.SHOPIFY_API_VERSION,
                "status": "unknown",
                "last_result": None,
            },
            "twilio": {
                "configured": creds.get("twilio", False),
                "status": "configured" if creds.get("twilio") else "not_configured",
            },
        }

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

            f24 = inv_result.get("fattura24", {})
            if config.FATTURA24_API_KEY:
                connectors["fattura24"]["status"] = "ok" if f24.get("success") else "error"
            else:
                connectors["fattura24"]["status"] = "not_configured"
            connectors["fattura24"]["last_result"] = {
                "success": f24.get("success"),
                "error": f24.get("error"),
            }

        cust_result = _sync_status.get("customers", {}).get("result")
        if cust_result:
            shopify_err = cust_result.get("shopify_error")
            if shopify_err:
                connectors["shopify"]["status"] = "error"
                connectors["shopify"]["error"] = shopify_err
            elif cust_result.get("success"):
                connectors["shopify"]["status"] = "ok"

        # --- 3. Sync Status ---
        sync_info = {}
        for key in ["invoices", "customers", "matching", "escalations"]:
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

        # Check: paid invoices still linked to active recovery
        paid_in_active = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "paid",
            Invoice.customer_id.isnot(None),
        ).join(Customer).filter(
            Customer.recovery_status.in_(["first_contact", "second_contact"]),
        ).scalar() or 0
        integrity["paid_in_active_recovery"] = {
            "count": paid_in_active,
            "status": "warning" if paid_in_active > 5 else "ok",
            "description": "Fatture pagate con cliente ancora in recupero attivo",
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

        if not connectors["fattura24"]["configured"]:
            alerts.append({
                "level": "warning",
                "component": "fattura24",
                "message": "Fattura24 non configurato — impostare FATTURA24_API_KEY su Render"
            })

        shopify_err = connectors["shopify"].get("error")
        if shopify_err:
            if "401" in str(shopify_err):
                alerts.append({
                    "level": "error",
                    "component": "shopify",
                    "message": "Shopify non raggiungibile — verificare SHOPIFY_CLIENT_ID e SHOPIFY_CLIENT_SECRET su Render"
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
                    "messages": {"total": total_messages},
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
        f24 = result.get("fattura24", {})
        parts = []
        if fp.get("success"):
            parts.append(f"FP: {fp.get('updated', 0)} agg, {fp.get('created', 0)} nuove, {fp.get('paid_detected', 0)} pagate")
        else:
            parts.append("FP: errore")
        if f24.get("success"):
            parts.append(f"F24: {f24.get('updated', 0)} agg, {f24.get('created', 0)} nuove")
        else:
            parts.append("F24: non attivo")
        return " | ".join(parts)

    if key == "customers":
        auto = result.get("auto_created_from_invoices", 0)
        created = result.get("created", 0)
        shopify_err = result.get("shopify_error")
        parts = []
        if created > 0:
            parts.append(f"Shopify: {created} nuovi")
        if shopify_err:
            parts.append("Shopify: errore token")
        if auto > 0:
            parts.append(f"Auto-creati: {auto}")
        return " | ".join(parts) if parts else "Nessuna modifica"

    if key == "matching":
        total = result.get("total", 0)
        if total == 0:
            return "Tutte le fatture già associate"
        exact = result.get("matched_exact", 0)
        fuzzy = result.get("matched_fuzzy", 0)
        unm = result.get("unmatched", 0)
        return f"{exact} esatte, {fuzzy} fuzzy, {unm} non associate"

    if key == "escalations":
        n = result.get("escalations_created", 0)
        return f"{n} escalation create" if n > 0 else "Nessuna nuova escalation"

    return str(result)
