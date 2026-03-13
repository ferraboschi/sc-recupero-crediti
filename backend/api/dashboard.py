"""Dashboard API endpoints."""

import logging
from datetime import date, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from backend.database import get_session, Invoice, Customer, Message, ActivityLog, RecoveryAction

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_dashboard(session: Session = Depends(get_session)):
    """
    Get dashboard overview with key statistics.

    Returns statistics about crediti, positions, and escalations.
    """
    try:
        # Total amount of credit (excluding paid invoices)
        total_crediti = session.query(func.sum(Invoice.amount_due)).filter(
            Invoice.status != "paid"
        ).scalar() or 0.0

        # Total OVERDUE amount (only invoices past due date)
        total_scaduto = session.query(func.sum(Invoice.amount_due)).filter(
            Invoice.status != "paid",
            Invoice.days_overdue > 0,
        ).scalar() or 0.0

        total_fatture_scadute = session.query(func.count(Invoice.id)).filter(
            Invoice.status != "paid",
            Invoice.days_overdue > 0,
        ).scalar() or 0

        total_clienti_scaduti = session.query(func.count(func.distinct(Invoice.customer_id))).filter(
            Invoice.status != "paid",
            Invoice.days_overdue > 0,
            Invoice.customer_id.isnot(None),
        ).scalar() or 0

        # Total number of positions (excluding paid)
        total_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status != "paid"
        ).scalar() or 0

        # Positions by status
        positions_by_status = session.query(
            Invoice.status,
            func.count(Invoice.id).label("count"),
            func.sum(Invoice.amount_due).label("amount")
        ).group_by(Invoice.status).all()

        status_breakdown = {
            row[0]: {
                "count": row[1],
                "amount": float(row[2]) if row[2] else 0.0
            }
            for row in positions_by_status
        }

        # Positions by escalation level
        positions_by_escalation = session.query(
            Message.escalation_level,
            func.count(func.distinct(Invoice.id)).label("count"),
            func.sum(Invoice.amount_due).label("amount")
        ).join(Invoice, Message.invoice_id == Invoice.id).group_by(
            Message.escalation_level
        ).all()

        escalation_breakdown = {
            row[0]: {
                "count": row[1],
                "amount": float(row[2]) if row[2] else 0.0
            }
            for row in positions_by_escalation
        }

        # Recent activity (last 10 items)
        recent_activity = session.query(ActivityLog).order_by(
            ActivityLog.timestamp.desc()
        ).limit(10).all()

        activity_list = [
            {
                "id": activity.id,
                "timestamp": activity.timestamp.isoformat(),
                "action": activity.action,
                "entity_type": activity.entity_type,
                "entity_id": activity.entity_id,
                "details": activity.details or {},
            }
            for activity in recent_activity
        ]

        # Additional stats for dashboard cards
        total_customers = session.query(func.count(Customer.id)).scalar() or 0
        draft_messages = session.query(func.count(Message.id)).filter(
            Message.status == "draft"
        ).scalar() or 0

        return {
            "total_crediti": float(total_crediti),
            "total_scaduto": float(total_scaduto),
            "total_fatture_scadute": total_fatture_scadute,
            "total_clienti_scaduti": total_clienti_scaduti,
            "total_positions": total_positions,
            "total_customers": total_customers,
            "draft_messages": draft_messages,
            "positions_by_status": status_breakdown,
            "positions_by_escalation_level": escalation_breakdown,
            "recent_activity": activity_list,
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
            row[0]: {"overdue_count": row[1], "total_overdue": float(row[2] or 0)}
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
            stats = overdue_by_customer.get(cid, {"overdue_count": 0, "total_overdue": 0})

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
                "recovery_status": action.customer.recovery_status,
            })

        # Build idle-customer todos (need first contact)
        for cust, overdue_count, total_overdue in idle_customers_with_overdue:
            if cust.id in seen_customer_ids:
                continue
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
    from sqlalchemy import extract, case
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
                "outcome": getattr(a, 'outcome', None),
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
