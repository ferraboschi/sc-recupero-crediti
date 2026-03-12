"""Messages API endpoints."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_
from sqlalchemy.orm import Session
from datetime import datetime

from backend.database import get_session, Message, Invoice, Customer, ActivityLog

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_messages(
    session: Session = Depends(get_session),
    status: str = Query(None),
    escalation_level: int = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """
    List messages with optional filters by status and escalation level.

    Status values: draft, approved, sent, delivered, read, replied
    """
    try:
        query = session.query(Message)

        if status:
            if status == "approved":
                query = query.filter(Message.status == "approved")
            else:
                query = query.filter(Message.status == status)

        if escalation_level is not None:
            query = query.filter(Message.escalation_level == escalation_level)

        total = query.count()
        messages = query.order_by(Message.created_at.desc()).offset(skip).limit(limit).all()

        # Pre-fetch related customer and invoice data
        msg_invoice_ids = [msg.invoice_id for msg in messages if msg.invoice_id]
        msg_customer_ids = [msg.customer_id for msg in messages if msg.customer_id]

        invoices_map = {}
        if msg_invoice_ids:
            invoices = session.query(Invoice).filter(Invoice.id.in_(msg_invoice_ids)).all()
            invoices_map = {inv.id: inv for inv in invoices}

        customers_map = {}
        if msg_customer_ids:
            customers = session.query(Customer).filter(Customer.id.in_(msg_customer_ids)).all()
            customers_map = {c.id: c for c in customers}

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": [
                {
                    "id": msg.id,
                    "invoice_id": msg.invoice_id,
                    "customer_id": msg.customer_id,
                    "escalation_level": msg.escalation_level,
                    "status": msg.status,
                    "body": msg.body,
                    "template": msg.template,
                    "created_at": msg.created_at.isoformat(),
                    "approved_at": msg.approved_at.isoformat() if msg.approved_at else None,
                    "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
                    "approved_by": msg.approved_by,
                    "customer_name": (
                        customers_map.get(msg.customer_id).ragione_sociale
                        if msg.customer_id and customers_map.get(msg.customer_id)
                        else None
                    ),
                    "invoice_number": (
                        invoices_map.get(msg.invoice_id).invoice_number
                        if msg.invoice_id and invoices_map.get(msg.invoice_id)
                        else None
                    ),
                    "invoice_amount": (
                        float(invoices_map.get(msg.invoice_id).amount_due)
                        if msg.invoice_id and invoices_map.get(msg.invoice_id)
                        else None
                    ),
                    "invoice_due_date": (
                        invoices_map.get(msg.invoice_id).due_date.isoformat()
                        if msg.invoice_id and invoices_map.get(msg.invoice_id) and invoices_map.get(msg.invoice_id).due_date
                        else None
                    ),
                }
                for msg in messages
            ],
        }

    except Exception as e:
        logger.error(f"Error listing messages: {e}", exc_info=True)
        raise


@router.get("/{message_id}")
async def get_message(message_id: int, session: Session = Depends(get_session)):
    """Get a single message with its conversation history."""
    try:
        message = session.query(Message).filter(Message.id == message_id).first()
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        # Get associated invoice
        invoice = session.query(Invoice).filter(Invoice.id == message.invoice_id).first()

        # Get conversation history
        conversations = session.query(Message).filter(
            Message.invoice_id == message.invoice_id
        ).order_by(Message.created_at.asc()).all()

        return {
            "id": message.id,
            "invoice_id": message.invoice_id,
            "invoice_number": invoice.invoice_number if invoice else None,
            "customer_id": message.customer_id,
            "escalation_level": message.escalation_level,
            "status": message.status,
            "body": message.body,
            "template": message.template,
            "created_at": message.created_at.isoformat(),
            "approved_at": message.approved_at.isoformat() if message.approved_at else None,
            "sent_at": message.sent_at.isoformat() if message.sent_at else None,
            "approved_by": message.approved_by,
            "conversation": [
                {
                    "id": m.id,
                    "status": m.status,
                    "escalation_level": m.escalation_level,
                    "body": m.body,
                    "created_at": m.created_at.isoformat(),
                    "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                }
                for m in conversations
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching message: {e}", exc_info=True)
        raise


@router.post("/{message_id}/approve")
async def approve_message(
    message_id: int,
    approved_by: str = "system",
    session: Session = Depends(get_session),
):
    """Approve a draft message."""
    try:
        message = session.query(Message).filter(Message.id == message_id).first()
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        if message.status != "draft":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve message with status '{message.status}'"
            )

        message.status = "approved"
        message.approved_at = datetime.utcnow()
        message.approved_by = approved_by
        session.commit()

        logger.info(f"Message {message_id} approved by {approved_by}")

        return {
            "id": message.id,
            "status": message.status,
            "approved_at": message.approved_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error approving message: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{message_id}/send")
async def send_message(message_id: int, session: Session = Depends(get_session)):
    """Send an approved message via WhatsApp."""
    try:
        message = session.query(Message).filter(Message.id == message_id).first()
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        if message.status not in ["draft", "approved"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot send message with status '{message.status}'"
            )

        # Mark as sent (in production, would integrate with Twilio)
        message.status = "sent"
        message.sent_at = datetime.utcnow()
        session.commit()

        # Log activity
        activity = ActivityLog(
            action="message_sent",
            entity_type="message",
            entity_id=message_id,
            details={
                "message_id": message_id,
                "invoice_id": message.invoice_id,
                "escalation_level": message.escalation_level,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Message {message_id} sent")

        return {
            "id": message.id,
            "status": message.status,
            "sent_at": message.sent_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending message: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/bulk-approve")
async def bulk_approve_messages(
    message_ids: list[int],
    approved_by: str = "system",
    session: Session = Depends(get_session),
):
    """Approve multiple draft messages at once."""
    try:
        messages = session.query(Message).filter(
            and_(
                Message.id.in_(message_ids),
                Message.status == "draft"
            )
        ).all()

        if not messages:
            raise HTTPException(status_code=404, detail="No draft messages found with given IDs")

        now = datetime.utcnow()
        for msg in messages:
            msg.status = "approved"
            msg.approved_at = now
            msg.approved_by = approved_by

        session.commit()

        logger.info(f"Bulk approved {len(messages)} messages")

        return {
            "approved_count": len(messages),
            "message_ids": [msg.id for msg in messages],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error bulk approving messages: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/bulk-send")
async def bulk_send_messages(
    message_ids: list[int],
    session: Session = Depends(get_session),
):
    """Send multiple approved messages at once."""
    try:
        messages = session.query(Message).filter(
            and_(
                Message.id.in_(message_ids),
                Message.status.in_(["draft", "approved"])
            )
        ).all()

        if not messages:
            raise HTTPException(status_code=404, detail="No messages found with given IDs")

        now = datetime.utcnow()
        for msg in messages:
            msg.status = "sent"
            msg.sent_at = now

            # Log activity
            activity = ActivityLog(
                action="message_sent",
                entity_type="message",
                entity_id=msg.id,
                details={
                    "message_id": msg.id,
                    "invoice_id": msg.invoice_id,
                    "escalation_level": msg.escalation_level,
                }
            )
            session.add(activity)

        session.commit()

        logger.info(f"Bulk sent {len(messages)} messages")

        return {
            "sent_count": len(messages),
            "message_ids": [msg.id for msg in messages],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error bulk sending messages: {e}", exc_info=True)
        session.rollback()
        raise
