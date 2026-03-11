"""Escalation module - 4-level escalation system for debt recovery."""

import logging
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any

import pytz
from sqlalchemy import and_
from sqlalchemy.orm import Session

from backend.database import Invoice, Message
from backend.config import config

logger = logging.getLogger(__name__)


# Italian national holidays (YYYY-MM-DD format)
ITALIAN_HOLIDAYS = [
    # Fixed holidays
    "01-01",  # New Year's Day
    "01-06",  # Epiphany
    "04-25",  # Liberation Day
    "05-01",  # Labour Day
    "06-02",  # Republic Day
    "08-15",  # Assumption of Mary
    "11-01",  # All Saints' Day
    "12-08",  # Immaculate Conception
    "12-25",  # Christmas Day
    "12-26",  # Saint Stephen's Day
]

# Timezone for business hours
ROME_TZ = pytz.timezone("Europe/Rome")


def _is_italian_holiday(date: datetime) -> bool:
    """
    Check if a given date is an Italian national holiday.

    Args:
        date: The date to check

    Returns:
        True if the date is a holiday, False otherwise
    """
    month_day = date.strftime("%m-%d")
    return month_day in ITALIAN_HOLIDAYS


def _is_business_day(date: datetime) -> bool:
    """
    Check if a given date is a business day (Mon-Fri, not a holiday).

    Args:
        date: The date to check (can be naive or timezone-aware)

    Returns:
        True if the date is a business day, False otherwise
    """
    # 0=Monday, 6=Sunday
    if date.weekday() >= 5:  # Saturday or Sunday
        return False

    if _is_italian_holiday(date):
        return False

    return True


def _is_business_hours(time: datetime) -> bool:
    """
    Check if a given time is within business hours.

    Business hours: BUSINESS_HOURS_START to BUSINESS_HOURS_END (default: 9-18)

    Args:
        time: The time to check (can be naive or timezone-aware)

    Returns:
        True if the time is within business hours, False otherwise
    """
    if not _is_business_day(time):
        return False

    hour = time.hour
    return config.BUSINESS_HOURS_START <= hour < config.BUSINESS_HOURS_END


def get_escalation_level(invoice: Invoice) -> int:
    """
    Determine the current escalation level for an invoice.

    Levels:
    - 0: Not overdue yet
    - 1: Overdue 7+ days (friendly reminder)
    - 2: Overdue 14+ days (professional follow-up)
    - 3: Overdue 21+ days (formal notice)
    - 4: Overdue 30+ days (final warning)

    Args:
        invoice: The invoice to check

    Returns:
        Escalation level (0-4)
    """
    if invoice.days_overdue <= 0:
        return 0

    for i, threshold in enumerate(config.ESCALATION_DAYS, start=1):
        if invoice.days_overdue < threshold:
            return i - 1 if i > 1 else 1

    return 4


def should_escalate(invoice: Invoice, session: Session) -> Tuple[bool, int]:
    """
    Check if an invoice needs escalation.

    An invoice should escalate if:
    1. It's not marked as paid/disputed
    2. It has reached the next escalation threshold
    3. No message has been sent at the current level yet, OR
    4. The previous level message was sent more than the escalation days ago

    Args:
        invoice: The invoice to check
        session: Database session

    Returns:
        Tuple of (should_escalate: bool, next_level: int)
        If should_escalate is False, next_level is -1
    """
    # Don't escalate if already resolved
    if invoice.status in ("paid", "disputed"):
        return False, -1

    current_level = get_escalation_level(invoice)

    if current_level == 0:
        return False, -1

    # Check if we've already sent a message at this level
    existing_message = session.query(Message).filter(
        and_(
            Message.invoice_id == invoice.id,
            Message.escalation_level == current_level
        )
    ).order_by(Message.sent_at.desc()).first()

    if not existing_message:
        # No message sent at this level yet
        return True, current_level

    if not existing_message.sent_at:
        # Message was drafted but not sent, don't re-escalate
        return False, -1

    # Check if enough time has passed since the message was sent
    if current_level < len(config.ESCALATION_DAYS):
        days_since_sent = (datetime.utcnow() - existing_message.sent_at).days
        next_threshold = config.ESCALATION_DAYS[current_level]

        if days_since_sent >= next_threshold:
            return True, current_level + 1

    return False, -1


def get_next_send_time(base_time: datetime = None) -> datetime:
    """
    Calculate the next valid time to send a message within business hours.

    Finds the next date and time that falls within business hours
    (BUSINESS_HOURS_START to BUSINESS_HOURS_END) on a business day
    (Monday-Friday, not a holiday).

    Args:
        base_time: The time to start from (default: current time in Rome timezone)

    Returns:
        A datetime object representing the next valid send time

    Examples:
        >>> next_time = get_next_send_time()
        >>> print(next_time.strftime("%A %H:%M"))
        Monday 09:00
    """
    if base_time is None:
        base_time = datetime.now(ROME_TZ)
    elif base_time.tzinfo is None:
        # Assume UTC if naive
        base_time = pytz.UTC.localize(base_time).astimezone(ROME_TZ)
    else:
        base_time = base_time.astimezone(ROME_TZ)

    # Start checking from the next minute
    current = base_time.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # Check up to 30 days in the future
    max_iterations = 30 * 24 * 60
    iteration = 0

    while iteration < max_iterations:
        if _is_business_hours(current):
            # Found a valid time
            return current

        # Move to the next hour
        if current.hour < config.BUSINESS_HOURS_END - 1:
            current = current.replace(minute=0) + timedelta(hours=1)
        else:
            # Move to start of next business day
            current = current.replace(hour=config.BUSINESS_HOURS_START, minute=0)
            current += timedelta(days=1)

            # Skip to next business day if needed
            while not _is_business_day(current):
                current += timedelta(days=1)

        iteration += 1

    logger.warning(f"Could not find next business hour time within 30 days from {base_time}")
    return current


def process_escalations(session: Session) -> List[Message]:
    """
    Process escalations for all invoices that need them.

    Creates draft Message records for invoices that have reached
    a new escalation level. Messages are marked as draft and require
    approval before sending.

    Args:
        session: Database session

    Returns:
        List of newly created Message objects (in draft status)
    """
    logger.info("Starting escalation processing")

    # Get all invoices that need escalation
    all_invoices = session.query(Invoice).filter(
        Invoice.status.notin_(["paid", "disputed"])
    ).all()

    new_messages = []
    escalation_count = 0

    for invoice in all_invoices:
        should_escalate_invoice, next_level = should_escalate(invoice, session)

        if not should_escalate_invoice:
            continue

        # Create a draft message for this escalation
        message = Message(
            invoice_id=invoice.id,
            customer_id=invoice.customer_id,
            escalation_level=next_level,
            status="draft",
            created_at=datetime.utcnow()
        )

        session.add(message)
        new_messages.append(message)
        escalation_count += 1

        logger.debug(
            f"Created escalation {next_level} for invoice {invoice.invoice_number} "
            f"({invoice.days_overdue} days overdue)"
        )

    # Commit all new messages
    if new_messages:
        session.commit()
        logger.info(f"Created {escalation_count} new escalation messages")
    else:
        logger.debug("No invoices needed escalation")

    return new_messages
