"""Dashboard API endpoints."""

import logging
from datetime import date, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import func, extract
from sqlalchemy.orm import Session, joinedload

from backend.database import get_session, Invoice, Customer, Message, RecoveryAction

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_dashboard(session: Session = Depends(get_session)):
    """
    Get dashboard overview with key statistics.
    Optimized: only returns the stats actually used by the frontend.
    """
    try:
        # Total OVERDUE amount (only invoices past due date)
        total_scaduto = session.query(func.sum(Invoice.amount_due)).filter(
            Invoice.status != "paid",
            Invoice.days_overdue > 0,
        ).scalar() or 0.0

        total_fatture_scadute = session.query(func.count(Invoice.id)).filter(
            Invoice.status != "paid",
            Invoice.days_overdue > 0,
        ).scalar() or 0

        total_clienti_scaduti = session.query(
            func.count(func.distinct(Invoice.customer_id))
        ).filter(
            Invoice.status != "paid",
            Invoice.days_overdue > 0,
            Invoice.customer_id.isnot(None),
        ).scalar() or 0

        # Total number of positions (excluding paid)
        total_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status != "paid"
        ).scalar() or 0

        total_customers = session.query(func.count(Customer.id)).scalar() or 0

        return {
            "total_scaduto": float(total_scaduto),
            "total_fatture_scadute": total_fatture_scadute,
            "total_clienti_scaduti": total_clienti_scaduti,
            "total_positions": total_positions,
            "total_customers": total_customers,
        }

    except Exception as e:
        logger.error(f"Error fetching dashboard data: {e}", exc_info=True)
        raise


@router.get("/search")
async def search_dashboard(
    q: str,
    session: Session = Depends(get_session),
):
    """Search customers by ragione sociale or partita IVA. Returns top 20 matches with overdue stats."""
    from sqlalchemy import case, or_
    try:
        search_term = f"%{q.strip()}%"
        results = (
            session.query(
                Customer,
                func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).label("total_overdue"),
                func.count(case((Invoice.days_overdue > 0, 1), else_=None)).label("overdue_count"),
            )
            .outerjoin(Invoice, (Invoice.customer_id == Customer.id) & (Invoice.status != "paid"))
            .filter(
                Customer.excluded.is_(False),
                or_(
                    Customer.ragione_sociale.ilike(search_term),
                    Customer.partita_iva.ilike(search_term),
                )
            )
            .group_by(Customer.id)
            .order_by(func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).desc())
            .limit(20)
            .all()
        )

        return {
            "results": [
                {
                    "id": c.id,
                    "ragione_sociale": c.ragione_sociale,
                    "partita_iva": c.partita_iva,
                    "phone": c.phone,
                    "recovery_status": c.recovery_status,
                    "total_overdue": float(total_overdue or 0),
                    "overdue_count": int(overdue_count or 0),
                }
                for c, total_overdue, overdue_count in results
            ],
            "total": len(results),
        }
    except Exception as e:
        logger.error(f"Error searching dashboard: {e}", exc_info=True)
        raise


@router.get("/todos")
async def get_todos(session: Session = Depends(get_session)):
    """
    Get todo list for the dashboard — pending recovery actions and customers needing attention.
    Groups: overdue (past due), today, upcoming (next 14 days), and idle customers with overdue invoices.

    Optimized: pre-loads all overdue stats in a single query instead of N+1.
    """
    try:
        today = date.today()

        # Pre-load ALL overdue stats per customer in ONE query (avoids N+1)
        overdue_stats_raw = (
            session.query(
                Invoice.customer_id,
                func.count(Invoice.id).label("overdue_count"),
                func.sum(Invoice.amount_due).label("total_overdue"),
                func.min(Invoice.due_date).label("oldest_due_date"),
                func.max(Invoice.days_overdue).label("max_days_overdue"),
            )
            .filter(
                Invoice.status != "paid",
                Invoice.days_overdue > 0,
                Invoice.customer_id.isnot(None),
            )
            .group_by(Invoice.customer_id)
            .all()
        )
        overdue_by_customer = {
            row[0]: {
                "overdue_count": row[1],
                "total_overdue": float(row[2] or 0),
                "oldest_due_date": row[3].isoformat() if row[3] else None,
                "max_days_overdue": row[4] or 0,
            }
            for row in overdue_stats_raw
        }

        # 1. Pending recovery actions (not completed, scheduled within 14 days)
        # Use joinedload to avoid N+1 lazy-load queries with NullPool
        cutoff_date = today + timedelta(days=14)
        pending_actions = (
            session.query(RecoveryAction)
            .join(Customer)
            .options(joinedload(RecoveryAction.customer))
            .filter(
                RecoveryAction.completed_at.is_(None),
                RecoveryAction.scheduled_date.isnot(None),
                RecoveryAction.scheduled_date <= cutoff_date,
                Customer.excluded.is_(False),
            )
            .order_by(RecoveryAction.scheduled_date.asc())
            .all()
        )

        # 2. Idle customers with overdue invoices (need first contact)
        idle_customers_with_overdue = (
            session.query(
                Customer,
                func.count(Invoice.id).label("overdue_count"),
                func.sum(Invoice.amount_due).label("total_overdue"),
            )
            .join(Invoice, Invoice.customer_id == Customer.id)
            .filter(
                Customer.recovery_status == "idle",
                Customer.excluded.is_(False),
                Invoice.status != "paid",
                Invoice.days_overdue > 0,
            )
            .group_by(Customer.id)
            .having(func.count(Invoice.id) > 0)
            .all()
        )

        todos = []
        seen_customer_ids = set()

        # Build action-based todos (NO extra queries — use pre-loaded stats)
        for action in pending_actions:
            cid = action.customer_id
            seen_customer_ids.add(cid)
            stats = overdue_by_customer.get(cid, {
                "overdue_count": 0, "total_overdue": 0,
                "oldest_due_date": None, "max_days_overdue": 0,
            })

            sched = action.scheduled_date
            if sched < today:
                priority = "overdue"
            elif sched == today:
                priority = "today"
            else:
                priority = "upcoming"

            todos.append({
                "id": f"action_{action.id}",
                "type": "action",
                "priority": priority,
                "customer_id": cid,
                "customer_name": action.customer.ragione_sociale,
                "partita_iva": action.customer.partita_iva,
                "phone": action.customer.phone,
                "action_type": action.action_type,
                "scheduled_date": sched.isoformat(),
                "notes": action.notes,
                "overdue_count": stats["overdue_count"],
                "total_overdue": stats["total_overdue"],
                "oldest_due_date": stats["oldest_due_date"],
                "max_days_overdue": stats["max_days_overdue"],
                "recovery_status": action.customer.recovery_status,
            })

        # Build idle-customer todos (need first contact)
        for cust, overdue_count, total_overdue in idle_customers_with_overdue:
            if cust.id in seen_customer_ids:
                continue
            stats = overdue_by_customer.get(cust.id, {
                "oldest_due_date": None, "max_days_overdue": 0,
            })
            todos.append({
                "id": f"idle_{cust.id}",
                "type": "new_contact",
                "priority": "new",
                "customer_id": cust.id,
                "customer_name": cust.ragione_sociale,
                "partita_iva": cust.partita_iva,
                "phone": cust.phone,
                "action_type": "first_contact",
                "scheduled_date": today.isoformat(),
                "notes": None,
                "overdue_count": overdue_count,
                "total_overdue": float(total_overdue or 0),
                "oldest_due_date": stats.get("oldest_due_date"),
                "max_days_overdue": stats.get("max_days_overdue", 0),
                "recovery_status": "idle",
            })

        # Sort: overdue first, then today, then new, then upcoming
        priority_order = {"overdue": 0, "today": 1, "new": 2, "upcoming": 3}
        todos.sort(key=lambda t: (priority_order.get(t["priority"], 9), t.get("scheduled_date", "")))

        return {
            "todos": todos,
            "total": len(todos),
            "counts": {
                "overdue": sum(1 for t in todos if t["priority"] == "overdue"),
                "today": sum(1 for t in todos if t["priority"] == "today"),
                "new": sum(1 for t in todos if t["priority"] == "new"),
                "upcoming": sum(1 for t in todos if t["priority"] == "upcoming"),
            },
        }

    except Exception as e:
        logger.error(f"Error fetching todos: {e}", exc_info=True)
        raise


@router.get("/calendar")
async def get_calendar(
    session: Session = Depends(get_session),
    year: int = None,
    month: int = None,
):
    """
    Get calendar data for recovery actions.
    Returns actions grouped by date for a given month.
    Also returns overdue counts (past actions not completed).
    """
    try:
        today = date.today()
        if not year:
            year = today.year
        if not month:
            month = today.month

        # Get first and last day of month (with buffer for display)
        first_day = date(year, month, 1)
        if month == 12:
            last_day = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(year, month + 1, 1) - timedelta(days=1)

        # Extend range to show surrounding weeks
        start = first_day - timedelta(days=first_day.weekday())  # Monday of first week
        end = last_day + timedelta(days=(6 - last_day.weekday()))  # Sunday of last week

        # Get all scheduled actions in range (both pending and completed)
        # Use joinedload to avoid N+1 lazy-load queries with NullPool
        actions = (
            session.query(RecoveryAction)
            .join(Customer)
            .options(joinedload(RecoveryAction.customer))
            .filter(
                RecoveryAction.scheduled_date >= start,
                RecoveryAction.scheduled_date <= end,
                Customer.excluded.is_(False),
            )
            .order_by(RecoveryAction.scheduled_date.asc())
            .all()
        )

        # Pre-load overdue stats
        overdue_stats_raw = (
            session.query(
                Invoice.customer_id,
                func.count(Invoice.id).label("cnt"),
                func.sum(Invoice.amount_due).label("tot"),
            )
            .filter(
                Invoice.status != "paid",
                Invoice.days_overdue > 0,
                Invoice.customer_id.isnot(None),
            )
            .group_by(Invoice.customer_id)
            .all()
        )
        overdue_map = {r[0]: {"count": r[1], "total": float(r[2] or 0)} for r in overdue_stats_raw}

        # Group by date
        by_date = {}
        for a in actions:
            d = a.scheduled_date.isoformat()
            if d not in by_date:
                by_date[d] = []
            stats = overdue_map.get(a.customer_id, {"count": 0, "total": 0})
            by_date[d].append({
                "id": a.id,
                "customer_id": a.customer_id,
                "customer_name": a.customer.ragione_sociale,
                "phone": a.customer.phone,
                "action_type": a.action_type,
                "notes": a.notes,
                "recovery_status": a.customer.recovery_status,
                "overdue_count": stats["count"],
                "total_overdue": stats["total"],
                "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                "outcome": a.outcome,
            })

        # Count overdue actions (scheduled before today, not completed)
        overdue_count = (
            session.query(func.count(RecoveryAction.id))
            .join(Customer)
            .filter(
                RecoveryAction.scheduled_date < today,
                RecoveryAction.completed_at.is_(None),
                Customer.excluded.is_(False),
            )
            .scalar() or 0
        )

        return {
            "year": year,
            "month": month,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "overdue_count": overdue_count,
            "days": by_date,
        }

    except Exception as e:
        logger.error(f"Error fetching calendar: {e}", exc_info=True)
        raise


@router.get("/stats")
async def get_stats(session: Session = Depends(get_session)):
    """
    Get summary statistics for the dashboard.

    Returns simple counts and totals for quick display.
    """
    try:
        total_crediti = session.query(func.sum(Invoice.amount_due)).scalar() or 0.0
        total_positions = session.query(func.count(Invoice.id)).scalar() or 0
        total_customers = session.query(func.count(Customer.id)).scalar() or 0
        total_messages = session.query(func.count(Message.id)).scalar() or 0

        # Count by status
        open_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "open"
        ).scalar() or 0

        contacted_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "contacted"
        ).scalar() or 0

        escalated_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "escalated"
        ).scalar() or 0

        paid_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "paid"
        ).scalar() or 0

        # Messages by status
        draft_messages = session.query(func.count(Message.id)).filter(
            Message.status == "draft"
        ).scalar() or 0

        sent_messages = session.query(func.count(Message.id)).filter(
            Message.status.in_(["sent", "delivered", "read", "replied"])
        ).scalar() or 0

        return {
            "total_crediti": float(total_crediti),
            "total_positions": total_positions,
            "total_customers": total_customers,
            "total_messages": total_messages,
            "open_positions": open_positions,
            "contacted_positions": contacted_positions,
            "escalated_positions": escalated_positions,
            "paid_positions": paid_positions,
            "draft_messages": draft_messages,
            "sent_messages": sent_messages,
        }

    except Exception as e:
        logger.error(f"Error fetching statistics: {e}", exc_info=True)
        raise


@router.get("/attivita")
async def get_attivita(session: Session = Depends(get_session)):
    """
    Get data for the Attività page:
    1. contacted: customers with recovery actions (not idle/archived)
    2. incassati: ONLY customers who received recovery actions AND then paid

    Performance: all data fetched in batch queries (no N+1).
    """
    from sqlalchemy import case, cast, Date  # noqa: F811
    from sqlalchemy.orm import aliased
    try:
        # ── CONTACTED ACCOUNTS ──
        contacted_raw = (
            session.query(
                Customer,
                func.count(func.distinct(
                    case((
                        (Invoice.status != "paid") & (Invoice.days_overdue > 0),
                        Invoice.id
                    ), else_=None)
                )).label("overdue_count"),
                func.sum(
                    case((
                        (Invoice.status != "paid") & (Invoice.days_overdue > 0),
                        Invoice.amount_due
                    ), else_=0)
                ).label("total_overdue"),
            )
            .outerjoin(Invoice, Invoice.customer_id == Customer.id)
            .filter(
                Customer.excluded.is_(False),
                Customer.recovery_status.notin_(["idle", "archived"]),
            )
            .group_by(Customer.id)
            .order_by(Customer.next_action_date.asc().nullslast())
            .all()
        )

        # Batch: get last recovery action per customer (avoid N+1)
        contacted_ids = [c.id for c, _, _ in contacted_raw]
        last_actions_map = {}
        if contacted_ids:
            # Subquery: max created_at per customer for contact actions
            from sqlalchemy import and_
            ra_sub = (
                session.query(
                    RecoveryAction.customer_id,
                    func.max(RecoveryAction.created_at).label("max_created"),
                )
                .filter(
                    RecoveryAction.customer_id.in_(contacted_ids),
                    RecoveryAction.action_type.in_(
                        ["first_contact", "second_contact", "lawyer"]
                    ),
                )
                .group_by(RecoveryAction.customer_id)
                .subquery()
            )
            last_actions_raw = (
                session.query(RecoveryAction)
                .join(
                    ra_sub,
                    and_(
                        RecoveryAction.customer_id == ra_sub.c.customer_id,
                        RecoveryAction.created_at == ra_sub.c.max_created,
                    ),
                )
                .all()
            )
            for la in last_actions_raw:
                last_actions_map[la.customer_id] = la

        contacted = []
        for cust, overdue_count, total_overdue in contacted_raw:
            last_action = last_actions_map.get(cust.id)
            last_date = None
            if last_action:
                if last_action.completed_at:
                    last_date = last_action.completed_at.strftime("%Y-%m-%d")
                else:
                    last_date = last_action.created_at.strftime("%Y-%m-%d")

            contacted.append({
                "id": cust.id,
                "ragione_sociale": cust.ragione_sociale,
                "partita_iva": cust.partita_iva,
                "phone": cust.phone,
                "recovery_status": cust.recovery_status,
                "next_action_date": (
                    cust.next_action_date.isoformat()
                    if cust.next_action_date else None
                ),
                "next_action_type": cust.next_action_type,
                "last_contact_date": last_date,
                "last_action_type": (
                    last_action.action_type if last_action else None
                ),
                "last_outcome": (
                    last_action.outcome if last_action else None
                ),
                "overdue_count": int(overdue_count or 0),
                "total_overdue": float(total_overdue or 0),
            })

        # ── INCASSATI ──
        # ONLY customers who:
        # 1. Had at least one recovery action (first_contact/second_contact/lawyer)
        # 2. Have paid invoices that were overdue (due_date < payment date)
        overdue_paid_filter = (
            (Invoice.status == "paid")
            & (Invoice.due_date.isnot(None))
            & (Invoice.due_date < cast(Invoice.updated_at, Date))
        )

        # Subquery: customer IDs that had recovery actions
        recovered_customer_ids = (
            session.query(func.distinct(RecoveryAction.customer_id))
            .filter(
                RecoveryAction.action_type.in_(
                    ["first_contact", "second_contact", "lawyer"]
                ),
            )
            .subquery()
        )

        incassati_raw = (
            session.query(
                Customer.id,
                Customer.ragione_sociale,
                Customer.partita_iva,
                Customer.recovery_status,
                func.count(Invoice.id).label("paid_count"),
                func.sum(Invoice.amount).label("total_paid"),
                func.max(Invoice.updated_at).label("last_payment"),
            )
            .join(Invoice, Invoice.customer_id == Customer.id)
            .filter(
                overdue_paid_filter,
                Customer.excluded.is_(False),
                Customer.id.in_(recovered_customer_ids),
            )
            .group_by(
                Customer.id, Customer.ragione_sociale,
                Customer.partita_iva, Customer.recovery_status,
            )
            .order_by(func.max(Invoice.updated_at).desc())
            .all()
        )

        # Batch: remaining overdue per customer (avoid N+1)
        incassati_ids = [row[0] for row in incassati_raw]
        remaining_map = {}
        if incassati_ids:
            remaining_raw = (
                session.query(
                    Invoice.customer_id,
                    func.count(Invoice.id),
                )
                .filter(
                    Invoice.customer_id.in_(incassati_ids),
                    Invoice.status != "paid",
                    Invoice.days_overdue > 0,
                )
                .group_by(Invoice.customer_id)
                .all()
            )
            for cid, cnt in remaining_raw:
                remaining_map[cid] = cnt

        incassati = []
        for row in incassati_raw:
            remaining = remaining_map.get(row[0], 0)
            incassati.append({
                "id": row[0],
                "ragione_sociale": row[1],
                "partita_iva": row[2],
                "recovery_status": row[3],
                "paid_count": row[4],
                "total_paid": float(row[5] or 0),
                "last_payment": (
                    row[6].strftime("%Y-%m-%d") if row[6] else None
                ),
                "fully_resolved": remaining == 0,
            })

        return {
            "contacted": contacted,
            "incassati": incassati,
            "summary": {
                "total_contacted": len(contacted),
                "total_incassati": len(incassati),
                "total_recovered": sum(
                    i["total_paid"] for i in incassati
                ),
                "fully_resolved": sum(
                    1 for i in incassati if i["fully_resolved"]
                ),
            },
        }

    except Exception as e:
        logger.error(f"Error fetching attivita data: {e}", exc_info=True)
        raise


@router.get("/pipeline")
async def get_pipeline(session: Session = Depends(get_session)):
    """
    Get pipeline/funnel data for the Attività page.
    Shows customers at each recovery stage — only those with overdue invoices.
    Resolved = ONLY customers who had recovery actions AND then paid.
    """
    try:
        from sqlalchemy import case, cast, Date  # noqa: F811

        # Only count customers who actually have overdue invoices (INNER join)
        pipeline_raw = (
            session.query(
                Customer.recovery_status,
                func.count(func.distinct(Customer.id)).label("count"),
                func.sum(
                    case((
                        (Invoice.status != "paid") & (Invoice.days_overdue > 0),
                        Invoice.amount_due
                    ), else_=0)
                ).label("total_overdue"),
            )
            .join(Invoice, Invoice.customer_id == Customer.id)
            .filter(
                Customer.excluded.is_(False),
                Invoice.status != "paid",
                Invoice.days_overdue > 0,
            )
            .group_by(Customer.recovery_status)
            .all()
        )

        stages = {
            "idle": {"label": "Da Gestire", "count": 0, "amount": 0},
            "first_contact": {
                "label": "I Contatto", "count": 0, "amount": 0,
            },
            "second_contact": {
                "label": "II Contatto", "count": 0, "amount": 0,
            },
            "lawyer": {"label": "Avvocato", "count": 0, "amount": 0},
            "waiting": {"label": "In Attesa", "count": 0, "amount": 0},
            "archived": {"label": "Archiviato", "count": 0, "amount": 0},
        }

        for status, count, total in pipeline_raw:
            if status in stages:
                stages[status]["count"] = count or 0
                stages[status]["amount"] = float(total or 0)

        # Resolved: ONLY customers with recovery actions who paid
        overdue_paid_filter = (
            (Invoice.status == "paid")
            & (Invoice.due_date.isnot(None))
            & (Invoice.due_date < cast(Invoice.updated_at, Date))
        )

        # Subquery: customer IDs that had recovery actions
        recovered_customer_ids = (
            session.query(func.distinct(RecoveryAction.customer_id))
            .filter(
                RecoveryAction.action_type.in_(
                    ["first_contact", "second_contact", "lawyer"]
                ),
            )
            .subquery()
        )

        resolved_count = (
            session.query(
                func.count(func.distinct(Invoice.customer_id))
            )
            .filter(
                overdue_paid_filter,
                Invoice.customer_id.in_(recovered_customer_ids),
            )
            .scalar() or 0
        )
        resolved_amount = (
            session.query(func.sum(Invoice.amount))
            .filter(
                overdue_paid_filter,
                Invoice.customer_id.in_(recovered_customer_ids),
            )
            .scalar() or 0
        )

        stages["resolved"] = {
            "label": "Incassato",
            "count": resolved_count,
            "amount": float(resolved_amount),
        }

        # Total customers with overdue
        total_with_overdue = (
            session.query(func.count(func.distinct(Invoice.customer_id)))
            .filter(
                Invoice.status != "paid",
                Invoice.days_overdue > 0,
                Invoice.customer_id.isnot(None),
            )
            .scalar() or 0
        )

        return {
            "stages": stages,
            "total_with_overdue": total_with_overdue,
        }

    except Exception as e:
        logger.error(f"Error fetching pipeline data: {e}", exc_info=True)
        raise


@router.get("/incassato")
async def get_incassato_per_anno(session: Session = Depends(get_session)):
    """
    Get collected amounts grouped by year.
    ONLY counts invoices for customers who had recovery actions
    (first_contact/second_contact/lawyer) AND the invoice was overdue when paid.
    """
    try:
        from sqlalchemy import cast, Date

        overdue_paid_filter = (
            (Invoice.status == "paid")
            & (Invoice.due_date.isnot(None))
            & (Invoice.due_date < cast(Invoice.updated_at, Date))
        )

        # Subquery: customer IDs that had recovery actions
        recovered_customer_ids = (
            session.query(func.distinct(RecoveryAction.customer_id))
            .filter(
                RecoveryAction.action_type.in_(
                    ["first_contact", "second_contact", "lawyer"]
                ),
            )
            .subquery()
        )

        yearly_raw = (
            session.query(
                extract('year', Invoice.updated_at).label("year"),
                func.count(Invoice.id).label("count"),
                func.sum(Invoice.amount).label("total"),
            )
            .filter(
                overdue_paid_filter,
                Invoice.customer_id.in_(recovered_customer_ids),
            )
            .group_by(extract('year', Invoice.updated_at))
            .order_by(extract('year', Invoice.updated_at).asc())
            .all()
        )

        yearly = {}
        grand_total = 0.0
        for row in yearly_raw:
            y = int(row[0]) if row[0] else 0
            amount = float(row[2] or 0)
            yearly[y] = {
                "count": row[1] or 0,
                "total": amount,
            }
            grand_total += amount

        # Ensure years 2022-2026 are always present
        for y in range(2022, 2027):
            if y not in yearly:
                yearly[y] = {"count": 0, "total": 0.0}

        # Recent recovered payments — only from recovery-actioned customers
        recent_paid = (
            session.query(Invoice)
            .filter(
                overdue_paid_filter,
                Invoice.customer_id.in_(recovered_customer_ids),
            )
            .order_by(Invoice.updated_at.desc())
            .limit(20)
            .all()
        )

        # Batch customer names (avoid N+1)
        cust_ids = list({
            inv.customer_id for inv in recent_paid if inv.customer_id
        })
        cust_names = {}
        if cust_ids:
            for cid, name in (
                session.query(Customer.id, Customer.ragione_sociale)
                .filter(Customer.id.in_(cust_ids))
                .all()
            ):
                cust_names[cid] = name

        recent_list = []
        for inv in recent_paid:
            customer_name = (
                cust_names.get(inv.customer_id)
                or inv.customer_name_raw
            )
            recent_list.append({
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "amount": float(inv.amount),
                "customer_name": customer_name,
                "customer_id": inv.customer_id,
                "paid_date": (
                    inv.updated_at.isoformat() if inv.updated_at else None
                ),
                "source_platform": inv.source_platform,
            })

        return {
            "yearly": yearly,
            "grand_total": grand_total,
            "recent_paid": recent_list,
        }

    except Exception as e:
        logger.error(f"Error fetching incassato data: {e}", exc_info=True)
        raise
