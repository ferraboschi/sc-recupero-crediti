"""Dashboard API endpoints."""

import logging
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_session, Invoice, Customer, Message, ActivityLog

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_dashboard(session: Session = Depends(get_session)):
    """
    Get dashboard overview with key statistics.

    Returns statistics about crediti, positions, and escalations.
    """
    try:
        # Total amount of credit
        total_crediti = session.query(func.sum(Invoice.amount_due)).scalar() or 0.0

        # Total number of positions (invoices)
        total_positions = session.query(func.count(Invoice.id)).scalar() or 0

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
