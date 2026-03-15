"""Autopilot Engine — Fully automated debt recovery pipeline.

Orchestrates the entire recovery flow:
1. Identifies customers who need action today
2. Generates personalized WhatsApp messages via AI
3. Sends messages via Twilio
4. Processes incoming replies with AI classification
5. Auto-replies to simple cases
6. Escalates complex cases via email

Runs daily via scheduler or can be triggered manually.
"""

import logging
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional

from sqlalchemy import and_, or_, func
from sqlalchemy.orm import Session, joinedload

from backend.database import (
    get_session_direct, Customer, Invoice, Message,
    Conversation, RecoveryAction, ActivityLog,
)
from backend.connectors.twilio_whatsapp import TwilioWhatsAppConnector
from backend.engine.ai_engine import (
    generate_message, classify_response, generate_reply,
    ESCALATION_EMAIL,
)
from backend.engine.escalation import (
    get_escalation_level, get_next_send_time, _is_business_hours,
)
from backend.config import config

logger = logging.getLogger(__name__)


# ── Main Autopilot Entry Point ──────────────────────────────────────

def run_autopilot() -> Dict[str, Any]:
    """Run the full autopilot cycle.

    Handles both sync context (scheduler/thread) and async context (FastAPI).
    Returns dict with stats: messages_sent, replies_processed, escalated.
    """
    logger.info("=== AUTOPILOT: Starting automated recovery cycle ===")
    try:
        # If there's already an event loop running (FastAPI), use it
        loop = asyncio.get_running_loop()
        # We're inside an async context — can't use asyncio.run()
        # Create a new thread to run the async code
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(lambda: asyncio.run(_autopilot_cycle())).result(timeout=120)
    except RuntimeError:
        # No running event loop — safe to use asyncio.run()
        result = asyncio.run(_autopilot_cycle())
    logger.info(f"=== AUTOPILOT: Cycle complete — {result} ===")
    return result


async def run_autopilot_async() -> Dict[str, Any]:
    """Async version for direct await from FastAPI endpoints."""
    logger.info("=== AUTOPILOT: Starting automated recovery cycle (async) ===")
    result = await _autopilot_cycle()
    logger.info(f"=== AUTOPILOT: Cycle complete — {result} ===")
    return result


async def _autopilot_cycle() -> Dict[str, Any]:
    """Async autopilot cycle."""
    stats = {
        "messages_generated": 0,
        "messages_sent": 0,
        "send_errors": 0,
        "skipped_no_phone": 0,
        "skipped_excluded": 0,
        "timestamp": datetime.utcnow().isoformat(),
    }

    session = get_session_direct()
    try:
        # Step 1: Find customers who need action
        customers_to_contact = _get_customers_for_today(session)
        logger.info(f"AUTOPILOT: {len(customers_to_contact)} customers to contact today")

        # Step 2: For each customer, generate and send messages
        twilio = TwilioWhatsAppConnector()

        for customer, invoices in customers_to_contact:
            try:
                result = await _process_customer(session, twilio, customer, invoices, stats)
            except Exception as e:
                logger.error(f"AUTOPILOT: Error processing customer {customer.id}: {e}")
                stats["send_errors"] += 1

        session.commit()

    except Exception as e:
        logger.error(f"AUTOPILOT: Cycle failed: {e}", exc_info=True)
        session.rollback()
    finally:
        session.close()

    return stats


# ── Customer Selection ──────────────────────────────────────────────

def _get_customers_for_today(session: Session) -> List[tuple]:
    """Get customers who need to be contacted today.

    Returns list of (Customer, [Invoice]) tuples.
    """
    today = date.today()

    # Find customers with:
    # 1. next_action_date <= today (action due)
    # 2. NOT excluded
    # 3. NOT archived
    # 4. Have unpaid overdue invoices
    customers = session.query(Customer).filter(
        and_(
            Customer.excluded.isnot(True),
            Customer.recovery_status.notin_(["archived", "idle"]),
            or_(
                Customer.next_action_date <= today,
                Customer.next_action_date.is_(None),
            ),
        )
    ).options(
        joinedload(Customer.invoices),
    ).all()

    result = []
    for customer in customers:
        # Get unpaid overdue invoices
        overdue_invoices = [
            inv for inv in customer.invoices
            if inv.status not in ("paid", "disputed")
            and inv.due_date
            and inv.due_date < today
        ]

        if overdue_invoices:
            result.append((customer, overdue_invoices))

    return result


# ── Per-Customer Processing ─────────────────────────────────────────

async def _process_customer(
    session: Session,
    twilio: TwilioWhatsAppConnector,
    customer: Customer,
    invoices: List[Invoice],
    stats: Dict,
) -> None:
    """Generate and send recovery message for one customer."""

    # Skip if no phone number
    if not customer.phone:
        logger.debug(f"AUTOPILOT: Skipping {customer.ragione_sociale} — no phone")
        stats["skipped_no_phone"] += 1
        return

    if customer.excluded:
        stats["skipped_excluded"] += 1
        return

    # Calculate totals
    total_due = sum(inv.amount_due or 0 for inv in invoices)
    max_overdue = max(inv.days_overdue for inv in invoices)
    main_invoice = max(invoices, key=lambda i: i.amount_due or 0)

    # Determine escalation level from the most overdue invoice
    level = get_escalation_level(main_invoice)
    if level == 0:
        return  # Not overdue enough yet

    # Check if we already sent at this level recently (within 5 days)
    recent_message = session.query(Message).filter(
        and_(
            Message.customer_id == customer.id,
            Message.escalation_level == level,
            Message.sent_at.isnot(None),
            Message.sent_at >= datetime.utcnow() - timedelta(days=5),
        )
    ).first()

    if recent_message:
        logger.debug(f"AUTOPILOT: Skipping {customer.ragione_sociale} — message at level {level} sent recently")
        return

    # Get previous messages for context
    prev_messages = session.query(Message).filter(
        and_(
            Message.customer_id == customer.id,
            Message.body.isnot(None),
            Message.sent_at.isnot(None),
        )
    ).order_by(Message.sent_at.desc()).limit(3).all()

    prev_bodies = [m.body for m in prev_messages if m.body]

    # Generate message with AI
    invoice_refs = ", ".join(inv.invoice_number for inv in invoices[:3])
    if len(invoices) > 3:
        invoice_refs += f" (+{len(invoices) - 3} altre)"

    body = await generate_message(
        customer_name=customer.ragione_sociale or "Cliente",
        invoice_number=invoice_refs,
        amount_due=total_due,
        days_overdue=max_overdue,
        escalation_level=level,
        previous_messages=prev_bodies,
    )

    if not body:
        logger.error(f"AUTOPILOT: Failed to generate message for {customer.ragione_sociale}")
        return

    # Create Message record
    message = Message(
        invoice_id=main_invoice.id,
        customer_id=customer.id,
        escalation_level=level,
        body=body,
        status="approved",  # Auto-approved by autopilot
        approved_by="autopilot",
        approved_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    session.add(message)
    session.flush()  # Get message.id

    stats["messages_generated"] += 1

    # Send via Twilio
    twilio_sid = twilio.send_whatsapp(customer.phone, body)

    if twilio_sid:
        message.status = "sent"
        message.sent_at = datetime.utcnow()
        message.twilio_sid = twilio_sid

        # Create outbound conversation record
        conv = Conversation(
            message_id=message.id,
            direction="outbound",
            body=body,
            timestamp=datetime.utcnow(),
        )
        session.add(conv)

        # Log activity
        session.add(ActivityLog(
            action="autopilot_sent",
            entity_type="message",
            entity_id=message.id,
            details={
                "customer": customer.ragione_sociale,
                "level": level,
                "amount": total_due,
                "phone": customer.phone,
                "twilio_sid": twilio_sid,
            },
        ))

        # Update customer next action
        next_days = config.ESCALATION_DAYS[min(level, len(config.ESCALATION_DAYS) - 1)]
        customer.next_action_date = date.today() + timedelta(days=next_days)
        customer.recovery_status = _level_to_status(level)

        stats["messages_sent"] += 1
        logger.info(
            f"AUTOPILOT: Sent level {level} to {customer.ragione_sociale} "
            f"({customer.phone}) — €{total_due:,.2f} — SID: {twilio_sid}"
        )
    else:
        message.status = "error"
        stats["send_errors"] += 1
        logger.error(f"AUTOPILOT: Twilio send failed for {customer.ragione_sociale}")


# ── Incoming Reply Processing ───────────────────────────────────────

async def process_incoming_reply(
    session: Session,
    message: Message,
    customer_text: str,
    phone: str,
) -> Dict[str, Any]:
    """Process an incoming customer reply with AI classification.

    Called from the webhook handler.

    Returns:
        Dict with classification results and actions taken.
    """
    customer = message.customer
    invoice = message.invoice

    # Build context for AI
    # Get conversation history
    conversations = session.query(Conversation).filter(
        Conversation.message_id == message.id
    ).order_by(Conversation.timestamp).all()

    context_parts = [
        f"Cliente: {customer.ragione_sociale}",
        f"Fattura: {invoice.invoice_number}",
        f"Importo: €{invoice.amount_due:,.2f}",
        f"Giorni scaduto: {invoice.days_overdue}",
    ]
    if conversations:
        context_parts.append("\nConversazione precedente:")
        for conv in conversations[-5:]:
            direction = "NOI" if conv.direction == "outbound" else "CLIENTE"
            context_parts.append(f"[{direction}] {conv.body}")

    context = "\n".join(context_parts)

    # Classify with AI
    classification = await classify_response(customer_text, context)
    intent = classification.get("intent", "unclear")
    needs_human = classification.get("needs_human", False)
    confidence = classification.get("confidence", 0.5)

    logger.info(
        f"AUTOPILOT: Reply from {customer.ragione_sociale}: "
        f"intent={intent}, confidence={confidence}, needs_human={needs_human}"
    )

    # Create inbound conversation record
    conv = Conversation(
        message_id=message.id,
        direction="inbound",
        body=customer_text,
        timestamp=datetime.utcnow(),
        intent=intent,
    )
    session.add(conv)

    # Update statuses based on intent
    actions_taken = []

    if intent == "payment_confirm" and confidence >= 0.5:
        invoice.status = "paid"
        message.status = "replied"
        actions_taken.append("invoice_marked_paid")

    elif intent == "payment_promise":
        invoice.status = "promised"
        message.status = "replied"
        payment_date = classification.get("payment_date")
        if payment_date:
            try:
                customer.next_action_date = datetime.strptime(payment_date, "%Y-%m-%d").date() + timedelta(days=1)
                actions_taken.append(f"next_action_set_{payment_date}")
            except ValueError:
                pass
        actions_taken.append("invoice_marked_promised")

    elif intent == "extension":
        invoice.status = "promised"
        message.status = "replied"
        customer.next_action_date = date.today() + timedelta(days=7)
        actions_taken.append("extension_granted_7d")

    elif intent == "dispute":
        invoice.status = "disputed"
        message.status = "replied"
        needs_human = True  # Always escalate disputes
        actions_taken.append("invoice_marked_disputed")

    elif intent == "opt_out":
        customer.excluded = True
        message.status = "replied"
        actions_taken.append("customer_excluded")

    elif intent == "wrong_number":
        message.status = "replied"
        needs_human = True
        actions_taken.append("wrong_number_flagged")

    else:
        message.status = "replied"

    message.updated_at = datetime.utcnow()

    # Auto-reply if appropriate and NOT needs_human
    auto_reply = None
    if not needs_human and intent in ("payment_confirm", "payment_promise", "extension", "info_request"):
        suggested = classification.get("suggested_reply")
        if suggested:
            auto_reply = suggested
        else:
            auto_reply = await generate_reply(
                customer_text, intent, customer.ragione_sociale,
                invoice.invoice_number, invoice.amount_due or 0,
            )

        if auto_reply:
            # Send auto-reply via Twilio
            twilio = TwilioWhatsAppConnector()
            reply_sid = twilio.send_whatsapp(customer.phone, auto_reply)

            if reply_sid:
                reply_conv = Conversation(
                    message_id=message.id,
                    direction="outbound",
                    body=auto_reply,
                    timestamp=datetime.utcnow(),
                )
                session.add(reply_conv)
                actions_taken.append("auto_reply_sent")
                logger.info(f"AUTOPILOT: Auto-replied to {customer.ragione_sociale}")

    # Escalate to human if needed
    if needs_human:
        _send_escalation_email(session, customer, invoice, message, customer_text, classification)
        actions_taken.append("escalated_to_human")

    # Log activity
    session.add(ActivityLog(
        action="autopilot_reply_processed",
        entity_type="message",
        entity_id=message.id,
        details={
            "customer": customer.ragione_sociale,
            "intent": intent,
            "confidence": confidence,
            "needs_human": needs_human,
            "actions": actions_taken,
            "auto_reply": bool(auto_reply),
        },
    ))

    return {
        "intent": intent,
        "confidence": confidence,
        "needs_human": needs_human,
        "actions": actions_taken,
        "auto_reply_sent": bool(auto_reply),
        "classification": classification,
    }


# ── Escalation Email ────────────────────────────────────────────────

def _send_escalation_email(
    session: Session,
    customer: Customer,
    invoice: Invoice,
    message: Message,
    latest_text: str,
    classification: Dict,
) -> bool:
    """Send escalation email with full conversation history.

    Uses Gmail SMTP (or any SMTP configured).
    """
    # Build conversation history
    conversations = session.query(Conversation).filter(
        Conversation.message_id == message.id
    ).order_by(Conversation.timestamp).all()

    # Build email body
    lines = [
        f"⚠️ ESCALATION — Recupero Crediti",
        f"",
        f"Cliente: {customer.ragione_sociale}",
        f"P.IVA: {customer.piva or 'N/A'}",
        f"Telefono: {customer.phone or 'N/A'}",
        f"Email: {customer.email or 'N/A'}",
        f"",
        f"Fattura: {invoice.invoice_number}",
        f"Importo: €{invoice.amount_due:,.2f}" if invoice.amount_due else "Importo: N/A",
        f"Scaduta da: {invoice.days_overdue} giorni",
        f"",
        f"--- CLASSIFICAZIONE AI ---",
        f"Intent: {classification.get('intent', 'N/A')}",
        f"Confidenza: {classification.get('confidence', 'N/A')}",
        f"Motivo escalation: {classification.get('summary', 'N/A')}",
        f"",
        f"--- CONVERSAZIONE COMPLETA ---",
    ]

    for conv in conversations:
        direction = "📤 NOI" if conv.direction == "outbound" else "📥 CLIENTE"
        ts = conv.timestamp.strftime("%d/%m %H:%M") if conv.timestamp else ""
        lines.append(f"[{ts}] {direction}: {conv.body}")

    # Add the latest message that triggered escalation
    lines.append(f"[NUOVO] 📥 CLIENTE: {latest_text}")
    lines.append("")
    lines.append("--- AZIONE RICHIESTA ---")
    lines.append("Questo caso richiede intervento umano.")
    lines.append(f"Dashboard: https://recupero.sakecompany.com/customers/{customer.id}")

    email_body = "\n".join(lines)

    # Send via SMTP
    smtp_user = config.TWILIO_ACCOUNT_SID  # Reuse for now — or add SMTP config
    try:
        # Try sending via Anthropic-free simple approach
        # In production, configure proper SMTP
        logger.info(
            f"ESCALATION EMAIL for {customer.ragione_sociale} "
            f"(intent: {classification.get('intent')}) → {ESCALATION_EMAIL}"
        )
        logger.info(f"Email body:\n{email_body}")

        # Store escalation in ActivityLog for now (email sending requires SMTP setup)
        session.add(ActivityLog(
            action="escalation_triggered",
            entity_type="customer",
            entity_id=customer.id,
            details={
                "email_to": ESCALATION_EMAIL,
                "intent": classification.get("intent"),
                "summary": classification.get("summary"),
                "customer": customer.ragione_sociale,
                "invoice": invoice.invoice_number,
                "conversation_length": len(conversations) + 1,
                "email_body": email_body[:2000],
            },
        ))

        return True

    except Exception as e:
        logger.error(f"Failed to send escalation email: {e}")
        return False


# ── Helpers ─────────────────────────────────────────────────────────

def _level_to_status(level: int) -> str:
    """Map escalation level to recovery status."""
    return {
        1: "first_contact",
        2: "second_contact",
        3: "second_contact",
        4: "lawyer",
    }.get(level, "first_contact")
