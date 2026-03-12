"""Webhook API endpoints for handling incoming messages."""

import logging
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from backend.database import get_session, Message, Invoice, Conversation, ActivityLog

logger = logging.getLogger(__name__)
router = APIRouter()


def classify_intent(text: str) -> str:
    """
    Classify the intent of an incoming message.

    Looks for keywords to determine the customer's intent.
    """
    text_lower = text.lower()

    # Payment confirmation keywords
    if any(word in text_lower for word in ["pagato", "pago", "paid", "fatto", "pagatore", "bonifico"]):
        return "payment_confirm"

    # Extension request keywords
    if any(word in text_lower for word in ["posso", "puoi", "potremmo", "scadenza", "tempo", "dilazione", "rimandare"]):
        return "extension"

    # Dispute keywords
    if any(word in text_lower for word in ["sbagliato", "inesatto", "non", "disputa", "contestazione", "non devo"]):
        return "dispute"

    # Information request keywords
    if any(word in text_lower for word in ["quanto", "quando", "quale", "dove", "come", "info", "informazioni", "dettagli"]):
        return "info_request"

    # Wrong number keywords
    if any(word in text_lower for word in ["sbagliato", "numero sbagliato", "wrong number", "numero errato"]):
        return "wrong_number"

    # Opt-out keywords
    if any(word in text_lower for word in ["stop", "opt-out", "remove", "cancella", "no", "basta", "non mi contattare"]):
        return "opt_out"

    return "unknown"


@router.post("/twilio")
async def twilio_webhook(
    request: Request,
    session: Session = Depends(get_session),
):
    """
    Handle incoming WhatsApp messages from Twilio.

    This webhook receives messages from Twilio, parses them, finds the associated
    conversation, classifies the intent, and updates the message status accordingly.
    """
    try:
        # Parse Twilio webhook data
        form_data = await request.form()

        # Extract message details
        twilio_message_sid = form_data.get("MessageSid")
        from_number = form_data.get("From", "")  # Phone number format: whatsapp:+1234567890
        message_body = form_data.get("Body", "").strip()
        logger.info(f"Received Twilio message: {twilio_message_sid} from {from_number}")

        if not message_body:
            logger.warning(f"Empty message body in webhook {twilio_message_sid}")
            return {"status": "ok", "message": "Empty message ignored"}

        # Extract phone number (remove whatsapp: prefix)
        phone = from_number.replace("whatsapp:", "").strip()

        # Find the associated message/invoice by phone number
        # This is a simplified approach - in production, you'd need better linking
        messages = session.query(Message).filter(
            Message.status.in_(["sent", "delivered", "read"])
        ).all()

        associated_message = None
        for msg in messages:
            if msg.customer and msg.customer.phone:
                customer_phone = msg.customer.phone.replace("+", "").replace(" ", "")
                incoming_phone = phone.replace("+", "").replace(" ", "")
                if customer_phone.endswith(incoming_phone) or incoming_phone.endswith(customer_phone):
                    associated_message = msg
                    break

        if not associated_message:
            logger.warning(f"No associated message found for phone {phone}")
            return {"status": "ok", "message": "No associated message found"}

        # Classify the intent
        intent = classify_intent(message_body)

        # Create conversation record
        conversation = Conversation(
            message_id=associated_message.id,
            direction="inbound",
            body=message_body,
            timestamp=datetime.utcnow(),
            intent=intent,
        )
        session.add(conversation)

        # Update message status based on intent
        if intent == "payment_confirm":
            # Mark invoice as paid
            associated_message.invoice.status = "paid"
            associated_message.status = "replied"
            logger.info(f"Invoice {associated_message.invoice.invoice_number} marked as paid")

        elif intent == "extension":
            # Mark as promised
            associated_message.invoice.status = "promised"
            associated_message.status = "replied"
            logger.info(f"Invoice {associated_message.invoice.invoice_number} marked as promised")

        elif intent == "dispute":
            # Mark as disputed
            associated_message.invoice.status = "disputed"
            associated_message.status = "replied"
            logger.info(f"Invoice {associated_message.invoice.invoice_number} marked as disputed")

        elif intent == "opt_out":
            # Mark customer as excluded
            if associated_message.customer:
                associated_message.customer.excluded = True
                logger.info(f"Customer {associated_message.customer.ragione_sociale} excluded")
            associated_message.status = "replied"

        else:
            # Generic reply
            associated_message.status = "replied"

        # Update message timestamps
        associated_message.twilio_sid = twilio_message_sid
        associated_message.updated_at = datetime.utcnow()

        # Log the activity
        activity = ActivityLog(
            action="reply_received",
            entity_type="message",
            entity_id=associated_message.id,
            details={
                "message_id": associated_message.id,
                "invoice_id": associated_message.invoice_id,
                "intent": intent,
                "phone": phone,
                "message_body": message_body[:200],  # Log first 200 chars
            }
        )
        session.add(activity)
        session.commit()

        logger.info(
            f"Processed incoming message for invoice {associated_message.invoice.invoice_number} "
            f"with intent: {intent}"
        )

        return {
            "status": "ok",
            "message_id": associated_message.id,
            "intent": intent,
            "invoice_status": associated_message.invoice.status,
        }

    except Exception as e:
        logger.error(f"Error processing Twilio webhook: {e}", exc_info=True)
        # Return 200 OK to Twilio even on error to avoid retries
        return {"status": "error", "message": str(e)}
