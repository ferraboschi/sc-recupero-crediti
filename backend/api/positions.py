"""Positions (invoices + customers) API endpoints."""

import logging
import csv
from io import StringIO
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.database import get_session, Invoice, Customer, Message, ActivityLog

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_positions(
    session: Session = Depends(get_session),
    status: str = Query(None),
    escalation_level: int = Query(None),
    min_amount: float = Query(None),
    search: str = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """
    List all positions (invoices joined with customers).

    Supports filtering by status, escalation level, minimum amount, and search.
    """
    try:
        query = session.query(Invoice).outerjoin(Customer)

        # Apply filters
        if status:
            query = query.filter(Invoice.status == status)

        if escalation_level is not None:
            query = query.join(
                Message, Invoice.id == Message.invoice_id, isouter=True
            ).filter(Message.escalation_level == escalation_level)

        if min_amount is not None:
            query = query.filter(Invoice.amount_due >= min_amount)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Customer.ragione_sociale.ilike(search_pattern),
                    Customer.partita_iva.ilike(search_pattern),
                    Invoice.invoice_number.ilike(search_pattern),
                )
            )

        # Total count before pagination
        total = query.count()

        # Get paginated results
        positions = query.offset(skip).limit(limit).all()

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": [
                {
                    "id": pos.id,
                    "invoice_number": pos.invoice_number,
                    "amount": float(pos.amount),
                    "amount_due": float(pos.amount_due),
                    "issue_date": pos.issue_date.isoformat() if pos.issue_date else None,
                    "due_date": pos.due_date.isoformat() if pos.due_date else None,
                    "days_overdue": pos.days_overdue,
                    "status": pos.status,
                    "customer": {
                        "id": pos.customer.id if pos.customer else None,
                        "ragione_sociale": pos.customer.ragione_sociale if pos.customer else pos.customer_name_raw,
                        "partita_iva": pos.customer.partita_iva if pos.customer else pos.customer_piva_raw,
                        "phone": pos.customer.phone if pos.customer else None,
                    } if pos.customer else None,
                }
                for pos in positions
            ],
        }

    except Exception as e:
        logger.error(f"Error listing positions: {e}", exc_info=True)
        raise


@router.get("/{position_id}")
async def get_position_detail(position_id: int, session: Session = Depends(get_session)):
    """Get detailed information for a single position including message history."""
    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")

        # Get all messages for this position
        messages = session.query(Message).filter(
            Message.invoice_id == position_id
        ).order_by(Message.created_at.desc()).all()

        message_list = [
            {
                "id": msg.id,
                "escalation_level": msg.escalation_level,
                "status": msg.status,
                "body": msg.body,
                "template": msg.template,
                "created_at": msg.created_at.isoformat(),
                "approved_at": msg.approved_at.isoformat() if msg.approved_at else None,
                "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
                "approved_by": msg.approved_by,
            }
            for msg in messages
        ]

        return {
            "id": position.id,
            "invoice_number": position.invoice_number,
            "amount": float(position.amount),
            "amount_due": float(position.amount_due),
            "issue_date": position.issue_date.isoformat() if position.issue_date else None,
            "due_date": position.due_date.isoformat() if position.due_date else None,
            "days_overdue": position.days_overdue,
            "status": position.status,
            "source_platform": position.source_platform,
            "customer": {
                "id": position.customer.id if position.customer else None,
                "ragione_sociale": position.customer.ragione_sociale if position.customer else position.customer_name_raw,
                "partita_iva": position.customer.partita_iva if position.customer else position.customer_piva_raw,
                "phone": position.customer.phone if position.customer else None,
                "email": position.customer.email if position.customer else None,
                "excluded": position.customer.excluded if position.customer else None,
            } if position.customer else None,
            "messages": message_list,
            "created_at": position.created_at.isoformat(),
            "updated_at": position.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching position detail: {e}", exc_info=True)
        raise


@router.put("/{position_id}/status")
async def update_position_status(
    position_id: int,
    new_status: str,
    session: Session = Depends(get_session),
):
    """Update the status of a position."""
    valid_statuses = ["open", "contacted", "promised", "paid", "disputed", "escalated"]

    if new_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
        )

    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")

        old_status = position.status
        position.status = new_status
        session.commit()

        # Log activity
        activity = ActivityLog(
            action="status_change",
            entity_type="invoice",
            entity_id=position_id,
            details={
                "old_status": old_status,
                "new_status": new_status,
                "invoice_number": position.invoice_number,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Position {position_id} status changed from {old_status} to {new_status}")

        return {
            "id": position.id,
            "status": position.status,
            "updated_at": position.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating position status: {e}", exc_info=True)
        session.rollback()
        raise


@router.get("/export")
async def export_positions(session: Session = Depends(get_session)):
    """Export all positions as CSV."""
    try:
        positions = session.query(Invoice).outerjoin(Customer).all()

        # Create CSV in memory
        output = StringIO()
        writer = csv.writer(output)

        # Write header
        writer.writerow([
            "Invoice Number",
            "Customer",
            "P.IVA",
            "Amount",
            "Amount Due",
            "Issue Date",
            "Due Date",
            "Days Overdue",
            "Status",
            "Phone",
            "Email",
        ])

        # Write data rows
        for pos in positions:
            writer.writerow([
                pos.invoice_number,
                pos.customer.ragione_sociale if pos.customer else pos.customer_name_raw,
                pos.customer.partita_iva if pos.customer else pos.customer_piva_raw,
                float(pos.amount),
                float(pos.amount_due),
                pos.issue_date.isoformat() if pos.issue_date else "",
                pos.due_date.isoformat() if pos.due_date else "",
                pos.days_overdue,
                pos.status,
                pos.customer.phone if pos.customer else "",
                pos.customer.email if pos.customer else "",
            ])

        # Return as streaming response
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=positions_export.csv"}
        )

    except Exception as e:
        logger.error(f"Error exporting positions: {e}", exc_info=True)
        raise
