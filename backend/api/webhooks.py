"""Webhook API endpoints for handling incoming messages (Twilio + Shopify)."""

import logging
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from datetime import datetime

from backend.database import get_session, Message, Conversation, ActivityLog

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/twilio")
async def twilio_webhook(
    request: Request,
    session: Session = Depends(get_session),
):
    """Handle incoming WhatsApp messages from Twilio.

    Flow:
    1. Parse Twilio form data
    2. Find associated Message by phone number
    3. Classify reply with AI (autopilot engine)
    4. Auto-reply if appropriate
    5. Escalate to human email if complex
    """
    try:
        form_data = await request.form()

        twilio_message_sid = form_data.get("MessageSid")
        from_number = form_data.get("From", "")
        message_body = form_data.get("Body", "").strip()
        message_status = form_data.get("MessageStatus", "")

        logger.info(f"Twilio webhook: SID={twilio_message_sid} from={from_number} status={message_status}")

        # Handle status callbacks (delivery receipts)
        if message_status and not message_body:
            return await _handle_status_callback(session, twilio_message_sid, message_status)

        if not message_body:
            return {"status": "ok", "message": "Empty message ignored"}

        # Extract phone number
        phone = from_number.replace("whatsapp:", "").strip()

        # Find associated message by phone
        associated_message = _find_message_by_phone(session, phone)

        if not associated_message:
            logger.warning(f"No associated message for phone {phone} — logging as orphan")
            session.add(ActivityLog(
                action="orphan_reply",
                entity_type="message",
                entity_id=0,
                details={
                    "phone": phone,
                    "body": message_body[:500],
                    "twilio_sid": twilio_message_sid,
                },
            ))
            session.commit()
            return {"status": "ok", "message": "No associated conversation found"}

        # Use AI autopilot to process the reply
        try:
            from backend.engine.autopilot import process_incoming_reply
            result = await process_incoming_reply(
                session, associated_message, message_body, phone
            )
        except Exception as e:
            logger.error(f"AI processing failed, using fallback: {e}")
            result = _fallback_process(session, associated_message, message_body)

        session.commit()

        logger.info(
            f"Processed reply from {phone}: intent={result.get('intent')} "
            f"actions={result.get('actions')}"
        )

        return {
            "status": "ok",
            "message_id": associated_message.id,
            "intent": result.get("intent"),
            "needs_human": result.get("needs_human", False),
            "auto_reply_sent": result.get("auto_reply_sent", False),
        }

    except Exception as e:
        logger.error(f"Twilio webhook error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


async def _handle_status_callback(
    session: Session,
    twilio_sid: str,
    status: str,
) -> dict:
    """Handle delivery status updates (sent, delivered, read, failed)."""
    if not twilio_sid:
        return {"status": "ok"}

    message = session.query(Message).filter(
        Message.twilio_sid == twilio_sid
    ).first()

    if message:
        status_map = {
            "sent": "sent",
            "delivered": "delivered",
            "read": "read",
            "failed": "error",
            "undelivered": "error",
        }
        new_status = status_map.get(status)
        if new_status and message.status not in ("replied", "error"):
            message.status = new_status
            message.updated_at = datetime.utcnow()
            session.commit()
            logger.debug(f"Message {message.id} status -> {new_status}")

    return {"status": "ok"}


def _find_message_by_phone(session: Session, phone: str) -> Message:
    """Find the most recent sent message for a phone number."""
    phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")

    messages = session.query(Message).filter(
        Message.status.in_(["sent", "delivered", "read"]),
        Message.sent_at.isnot(None),
    ).order_by(Message.sent_at.desc()).limit(100).all()

    for msg in messages:
        if msg.customer and msg.customer.phone:
            cust_phone = msg.customer.phone.replace("+", "").replace(" ", "").replace("-", "")
            if cust_phone.endswith(phone_clean[-9:]) or phone_clean.endswith(cust_phone[-9:]):
                return msg

    return None


def _fallback_process(session: Session, message: Message, text: str) -> dict:
    """Fallback processing when AI is unavailable."""
    from backend.engine.ai_engine import _fallback_classify

    classification = _fallback_classify(text)
    intent = classification["intent"]

    conv = Conversation(
        message_id=message.id,
        direction="inbound",
        body=text,
        timestamp=datetime.utcnow(),
        intent=intent,
    )
    session.add(conv)

    if intent == "payment_confirm":
        message.invoice.status = "paid"
    elif intent in ("payment_promise", "extension"):
        message.invoice.status = "promised"
    elif intent == "dispute":
        message.invoice.status = "disputed"
    elif intent == "opt_out" and message.customer:
        message.customer.excluded = True

    message.status = "replied"
    message.updated_at = datetime.utcnow()

    session.add(ActivityLog(
        action="reply_processed_fallback",
        entity_type="message",
        entity_id=message.id,
        details={"intent": intent, "text": text[:200]},
    ))

    return {
        "intent": intent,
        "needs_human": classification.get("needs_human", False),
        "actions": [f"status_updated_{intent}"],
        "auto_reply_sent": False,
    }
