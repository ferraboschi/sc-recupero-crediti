"""Matching module - matches invoices to customers."""

import logging
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from backend.database import Invoice, Customer
from backend.engine.normalizer import normalize_ragione_sociale, are_similar
from backend.config import config

logger = logging.getLogger(__name__)


def _piva_conflict(invoice: Invoice, customer: Customer) -> bool:
    """
    REGOLA P.IVA IMPRESCINDIBILE: verifica conflitto P.IVA tra fattura e cliente.

    P.IVA è l'identificatore canonico di un'entità commerciale.
    Se sia la fattura che il cliente hanno una P.IVA e queste sono DIVERSE,
    sono entità diverse — il match è VIETATO, indipendentemente dalla
    somiglianza dei nomi.

    Returns True if both have a P.IVA and they don't match — meaning
    the invoice belongs to a different business entity and must NOT
    be assigned to this customer.
    """
    inv_piva = (invoice.customer_piva_raw or "").strip().upper()
    cust_piva = (customer.partita_iva or "").strip().upper()
    if inv_piva and cust_piva and inv_piva != cust_piva:
        logger.debug(
            f"P.IVA CONFLICT: invoice {invoice.invoice_number} has '{inv_piva}', "
            f"customer '{customer.ragione_sociale}' has '{cust_piva}'. Match blocked."
        )
        return True
    return False


def match_invoice_to_customer(
    invoice: Invoice,
    customers: list[Customer],
    session: Session
) -> Optional[Customer]:
    """
    Match an invoice to a customer using multiple strategies.

    Matching priority:
    1. P.IVA exact match (highest priority)
    2. Normalized ragione sociale exact match (blocked if P.IVA conflicts)
    3. Fuzzy ragione sociale match (blocked if P.IVA conflicts)

    CRITICAL RULE: If an invoice has a P.IVA and a candidate customer has
    a DIFFERENT P.IVA, that customer is NEVER a match — even if the names
    are identical after normalization. Different P.IVA = different business.

    Args:
        invoice: The invoice to match
        customers: List of available customers to match against
        session: Database session

    Returns:
        Matched Customer object, or None if no match found
    """
    if not invoice.customer_piva_raw and not invoice.customer_name_raw:
        logger.warning(f"Invoice {invoice.invoice_number} has no customer data")
        return None

    # Strategy 1: P.IVA exact match (highest priority)
    if invoice.customer_piva_raw:
        piva_clean = invoice.customer_piva_raw.strip().upper()
        for customer in customers:
            if customer.partita_iva and customer.partita_iva.strip().upper() == piva_clean:
                logger.info(
                    f"Invoice {invoice.invoice_number} matched to customer {customer.ragione_sociale} "
                    f"by P.IVA: {piva_clean}"
                )
                return customer

    # Strategy 2: Normalized ragione sociale exact match
    if invoice.customer_name_raw:
        invoice_name_normalized = normalize_ragione_sociale(invoice.customer_name_raw)

        for customer in customers:
            # BLOCK: never match if P.IVA values conflict
            if _piva_conflict(invoice, customer):
                continue

            customer_name_normalized = normalize_ragione_sociale(customer.ragione_sociale)

            if invoice_name_normalized and customer_name_normalized and \
               invoice_name_normalized == customer_name_normalized:
                logger.info(
                    f"Invoice {invoice.invoice_number} matched to customer {customer.ragione_sociale} "
                    f"by normalized name"
                )
                return customer

    # Strategy 3: Fuzzy match on original names
    if invoice.customer_name_raw:
        best_match = None
        best_score = 0

        for customer in customers:
            # BLOCK: never match if P.IVA values conflict
            if _piva_conflict(invoice, customer):
                continue

            is_similar, score = are_similar(
                invoice.customer_name_raw,
                customer.ragione_sociale,
                threshold=config.FUZZY_MATCH_THRESHOLD
            )

            if is_similar and score > best_score:
                best_match = customer
                best_score = score

        if best_match:
            logger.info(
                f"Invoice {invoice.invoice_number} matched to customer {best_match.ragione_sociale} "
                f"by fuzzy match (score={best_score})"
            )
            return best_match

    logger.debug(
        f"Invoice {invoice.invoice_number} ({invoice.customer_name_raw}) could not be matched"
    )
    return None


def run_matching(session: Session) -> Dict[str, Any]:
    """
    Batch match all unmatched invoices to customers.

    Processes all invoices with customer_id = NULL and attempts to match them
    to customers using match_invoice_to_customer.

    Unmatched invoices without a customer with P.IVA are likely Shopify sales
    (not real invoices) and are left unmatched intentionally.

    Args:
        session: Database session

    Returns:
        Dictionary with match statistics
    """
    stats = {
        'matched_piva': 0,
        'matched_exact': 0,
        'matched_fuzzy': 0,
        'unmatched': 0,
        'total': 0,
    }

    # Get all unmatched invoices
    unmatched_invoices = session.query(Invoice).filter(
        Invoice.customer_id.is_(None)
    ).all()

    stats['total'] = len(unmatched_invoices)

    # Get all customers
    customers = session.query(Customer).all()

    if not customers:
        logger.warning("No customers found in database for matching")
        stats['unmatched'] = len(unmatched_invoices)
        return stats

    logger.info(f"Starting matching process for {stats['total']} invoices against {len(customers)} customers")

    # Match each invoice
    for invoice in unmatched_invoices:
        matched_customer = match_invoice_to_customer(invoice, customers, session)

        if matched_customer:
            invoice.customer_id = matched_customer.id

            # Track which strategy was used
            if invoice.customer_piva_raw:
                piva_clean = invoice.customer_piva_raw.strip().upper()
                if matched_customer.partita_iva and \
                   matched_customer.partita_iva.strip().upper() == piva_clean:
                    stats['matched_piva'] += 1
                    continue

            # Check if it's exact name match
            if invoice.customer_name_raw:
                invoice_norm = normalize_ragione_sociale(invoice.customer_name_raw)
                customer_norm = normalize_ragione_sociale(matched_customer.ragione_sociale)
                if invoice_norm == customer_norm:
                    stats['matched_exact'] += 1
                    continue

            # Otherwise it's fuzzy match
            stats['matched_fuzzy'] += 1
        else:
            stats['unmatched'] += 1

    session.commit()

    logger.info(
        f"Matching complete. Results: {stats['matched_piva']} P.IVA, "
        f"{stats['matched_exact']} exact, {stats['matched_fuzzy']} fuzzy, "
        f"{stats['unmatched']} unmatched"
    )

    return stats
