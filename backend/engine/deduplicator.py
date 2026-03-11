"""Deduplicator module - deduplicates invoices from FatturaPro and Fattura24."""

import logging
from typing import List, Dict, Any
from datetime import datetime, timedelta

from sqlalchemy import and_
from sqlalchemy.orm import Session

from backend.database import Invoice

logger = logging.getLogger(__name__)


def deduplicate_invoices(invoices: List[Invoice]) -> List[Invoice]:
    """
    Remove duplicate invoices from a list.

    Duplicates are identified by:
    1. Same invoice_number (exact match) - primary key for duplicates
    2. Same (customer_name_raw + amount + issue_date) combination

    When duplicates are found, FatturaPro source is preferred over Fattura24
    (legacy system) to keep the more recent/reliable data.

    Args:
        invoices: List of Invoice objects to deduplicate

    Returns:
        List of unique invoices with duplicates removed, preferring FatturaPro

    Examples:
        >>> unique = deduplicate_invoices(invoices)
        >>> len(unique) < len(invoices)  # If duplicates existed
        True
    """
    if not invoices:
        return []

    seen_by_number = {}
    seen_by_composite = {}
    unique_invoices = []
    duplicates = []

    logger.debug(f"Deduplicating {len(invoices)} invoices")

    for invoice in invoices:
        # Check 1: Exact invoice number match
        if invoice.invoice_number in seen_by_number:
            existing = seen_by_number[invoice.invoice_number]

            # Prefer FatturaPro over Fattura24
            if existing.source_platform == "fattura24" and \
               invoice.source_platform == "fatturapro":
                # Replace with FatturaPro version
                unique_invoices.remove(existing)
                unique_invoices.append(invoice)
                seen_by_number[invoice.invoice_number] = invoice
                duplicates.append(existing)
                logger.debug(
                    f"Duplicate by invoice_number: {invoice.invoice_number} "
                    f"(preferring {invoice.source_platform})"
                )
            else:
                # Keep existing, mark current as duplicate
                duplicates.append(invoice)
                logger.debug(
                    f"Duplicate by invoice_number: {invoice.invoice_number} "
                    f"(keeping {existing.source_platform})"
                )
            continue

        # Check 2: Composite key (customer_name_raw + amount + issue_date)
        # Only if we have all three pieces of information
        if invoice.customer_name_raw and invoice.amount and invoice.issue_date:
            composite_key = (
                invoice.customer_name_raw.strip().lower(),
                round(invoice.amount, 2),
                invoice.issue_date.isoformat() if isinstance(invoice.issue_date, datetime)
                else invoice.issue_date
            )

            if composite_key in seen_by_composite:
                existing = seen_by_composite[composite_key]

                # Prefer FatturaPro over Fattura24
                if existing.source_platform == "fattura24" and \
                   invoice.source_platform == "fatturapro":
                    unique_invoices.remove(existing)
                    unique_invoices.append(invoice)
                    seen_by_composite[composite_key] = invoice
                    duplicates.append(existing)
                    logger.debug(
                        f"Duplicate by composite key: {invoice.customer_name_raw} / "
                        f"{invoice.amount} / {invoice.issue_date} "
                        f"(preferring {invoice.source_platform})"
                    )
                else:
                    duplicates.append(invoice)
                    logger.debug(
                        f"Duplicate by composite key: {invoice.customer_name_raw} / "
                        f"{invoice.amount} / {invoice.issue_date} "
                        f"(keeping {existing.source_platform})"
                    )
                continue

            seen_by_composite[composite_key] = invoice

        # No duplicate found
        seen_by_number[invoice.invoice_number] = invoice
        unique_invoices.append(invoice)

    logger.info(
        f"Deduplication complete: {len(invoices)} invoices -> {len(unique_invoices)} unique "
        f"({len(duplicates)} duplicates removed)"
    )

    return unique_invoices


def find_duplicates(session: Session) -> List[Dict[str, Any]]:
    """
    Find duplicate invoices in the database.

    Returns a list of duplicate groups, where each group contains the invoices
    that are considered duplicates of each other.

    Args:
        session: Database session

    Returns:
        List of duplicate groups, each with:
        {
            'invoice_numbers': [str],          # All invoice numbers in the group
            'customer_names': [str],           # All customer names in the group
            'duplicate_count': int,            # Number of duplicates found
            'preferred': Invoice,              # The preferred invoice (FatturaPro)
            'duplicates': [Invoice]            # The duplicate invoices
        }

    Examples:
        >>> duplicates = find_duplicates(session)
        >>> for group in duplicates:
        ...     print(f"Found {group['duplicate_count']} duplicates for invoice {group['invoice_numbers']}")
    """
    logger.debug("Searching for duplicate invoices in database")

    all_invoices = session.query(Invoice).all()

    duplicate_groups = []
    processed_ids = set()

    for invoice in all_invoices:
        if invoice.id in processed_ids:
            continue

        # Find all duplicates for this invoice
        duplicates_for_invoice = []

        # Search by invoice number
        same_number = session.query(Invoice).filter(
            and_(
                Invoice.invoice_number == invoice.invoice_number,
                Invoice.id != invoice.id
            )
        ).all()

        if same_number:
            duplicates_for_invoice.extend(same_number)

        # Search by composite key
        if invoice.customer_name_raw and invoice.amount and invoice.issue_date:
            same_composite = session.query(Invoice).filter(
                and_(
                    Invoice.customer_name_raw == invoice.customer_name_raw,
                    Invoice.amount == invoice.amount,
                    Invoice.issue_date == invoice.issue_date,
                    Invoice.id != invoice.id
                )
            ).all()

            # Add if not already found
            for dup in same_composite:
                if dup not in duplicates_for_invoice:
                    duplicates_for_invoice.append(dup)

        # If duplicates found, create a group
        if duplicates_for_invoice:
            # Prefer FatturaPro
            all_in_group = [invoice] + duplicates_for_invoice
            fatturapro = [inv for inv in all_in_group if inv.source_platform == "fatturapro"]
            preferred = fatturapro[0] if fatturapro else all_in_group[0]

            duplicate_groups.append({
                'invoice_numbers': [inv.invoice_number for inv in all_in_group],
                'customer_names': [inv.customer_name_raw for inv in all_in_group],
                'duplicate_count': len(duplicates_for_invoice),
                'preferred': preferred,
                'duplicates': [inv for inv in all_in_group if inv.id != preferred.id]
            })

            # Mark all as processed
            for inv in all_in_group:
                processed_ids.add(inv.id)

    logger.info(f"Found {len(duplicate_groups)} groups of duplicates")

    return duplicate_groups
