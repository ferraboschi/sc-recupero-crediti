"""Customers API endpoints."""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from datetime import datetime

from backend.database import get_session, Customer, Invoice, ActivityLog, RecoveryAction
from backend.engine.cases import get_open_case, contact_count, business_day_start
from backend.engine.verify import verify_invoice_customer

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_customers(
    session: Session = Depends(get_session),
    search: str = Query(None),
    excluded: bool = Query(None),
    only_overdue: bool = Query(False, description="Show only customers with overdue invoices"),
    sort_by: str = Query(None, description="Sort field: total_overdue, overdue_count, ragione_sociale"),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """
    List customers with optional search and filter by excluded status.
    Supports filtering to only_overdue customers and sorting by overdue amounts.
    """
    try:
        # Step 1: Get invoice stats using SQL aggregation (much faster than loading all rows)
        from sqlalchemy import case
        raw_stats = (
            session.query(
                Invoice.customer_id,
                func.count(Invoice.id).label("invoice_count"),
                func.sum(Invoice.amount_due).label("total_due"),
                func.sum(case((Invoice.days_overdue > 0, 1), else_=0)).label("overdue_count"),
                func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).label("total_overdue"),
                func.min(case((Invoice.days_overdue > 0, Invoice.due_date), else_=None)).label("earliest_due_date"),
            )
            .filter(Invoice.status != "paid", Invoice.customer_id.isnot(None))
            .group_by(Invoice.customer_id)
            .all()
        )

        invoice_stats = {}
        for row in raw_stats:
            invoice_stats[row[0]] = {
                "invoice_count": row[1] or 0,
                "total_due": float(row[2] or 0),
                "overdue_count": row[3] or 0,
                "total_overdue": float(row[4] or 0),
                "earliest_due_date": row[5],
            }

        # Step 2: Query customers with basic filters
        query = session.query(Customer)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Customer.ragione_sociale.ilike(search_pattern),
                    Customer.partita_iva.ilike(search_pattern),
                    Customer.email.ilike(search_pattern),
                )
            )

        if excluded is not None:
            query = query.filter(Customer.excluded == excluded)

        all_customers = query.all()

        # Step 3: Build enriched list
        enriched = []
        for cust in all_customers:
            stats = invoice_stats.get(cust.id, {"invoice_count": 0, "total_due": 0.0, "overdue_count": 0, "total_overdue": 0.0, "earliest_due_date": None})
            enriched.append({"customer": cust, **stats})

        # Step 4: Filter only_overdue
        if only_overdue:
            enriched = [e for e in enriched if e["overdue_count"] > 0]

        total = len(enriched)

        # Compute summary totals BEFORE pagination (across ALL matching records)
        summary_total_overdue = sum(e["total_overdue"] for e in enriched)
        summary_overdue_customers = sum(1 for e in enriched if e["overdue_count"] > 0)

        # Step 5: Sort
        if sort_by == "total_overdue":
            enriched.sort(key=lambda e: e["total_overdue"], reverse=(sort_order == "desc"))
        elif sort_by == "overdue_count":
            enriched.sort(key=lambda e: e["overdue_count"], reverse=(sort_order == "desc"))
        elif sort_by == "earliest_due_date":
            from datetime import date as date_type
            far_future = date_type(9999, 12, 31)
            enriched.sort(
                key=lambda e: e.get("earliest_due_date") or far_future,
                reverse=(sort_order == "desc"),
            )
        else:
            enriched.sort(key=lambda e: (e["customer"].ragione_sociale or "").lower())

        # Step 6: Paginate
        page = enriched[skip:skip + limit]

        items = []
        for entry in page:
            cust = entry["customer"]
            items.append({
                "id": cust.id,
                "ragione_sociale": cust.ragione_sociale,
                "partita_iva": cust.partita_iva,
                "phone": cust.phone,
                "email": cust.email,
                "excluded": cust.excluded,
                "source": cust.source,
                "phone_validated": cust.phone_validated,
                "recovery_status": cust.recovery_status,
                "next_action_date": cust.next_action_date.isoformat() if cust.next_action_date else None,
                "next_action_type": cust.next_action_type,
                "invoice_count": entry["invoice_count"],
                "total_due": entry["total_due"],
                "overdue_count": entry["overdue_count"],
                "total_overdue": entry["total_overdue"],
                "earliest_due_date": entry["earliest_due_date"].isoformat() if entry.get("earliest_due_date") else None,
                "created_at": cust.created_at.isoformat(),
            })

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "summary_total_overdue": summary_total_overdue,
            "summary_overdue_customers": summary_overdue_customers,
            "items": items,
        }

    except Exception as e:
        logger.error(f"Error listing customers: {e}", exc_info=True)
        raise


@router.get("/suggest")
async def suggest_customers(
    q: str = Query(..., min_length=2, description="Testo di ricerca approssimato"),
    limit: int = Query(6, ge=1, le=20),
    session: Session = Depends(get_session),
):
    """"Forse intendevi": clienti la cui ragione sociale è APPROSSIMABILE al
    testo cercato — accenti, forme legali (S.r.l./Srl) e punteggiatura
    ignorati, refusi tollerati. Aiuta a ritrovare un cliente di cui non si
    ricorda la grafia esatta (es. "Domo Milano" → "Domò Milano", "Sakeya
    Srl" → "Sakeya S.r.l.").

    Cerca su TUTTI i clienti, indipendentemente dal filtro only_overdue.
    Definito PRIMA di /{customer_id} così 'suggest' non è letto come id.
    """
    from backend.engine.normalizer import rank_similar
    from sqlalchemy import case

    rows = session.query(
        Customer.id, Customer.ragione_sociale,
        Customer.partita_iva, Customer.excluded,
    ).all()
    if not rows:
        return {"query": q, "items": []}

    ranked = rank_similar(q, [r[1] or "" for r in rows], limit=limit)
    if not ranked:
        return {"query": q, "items": []}

    ids = [rows[idx][0] for idx, _ in ranked]
    stats = {}
    stat_rows = (
        session.query(
            Invoice.customer_id,
            func.sum(case((Invoice.days_overdue > 0, 1), else_=0)).label("overdue_count"),
            func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).label("total_overdue"),
        )
        .filter(Invoice.status != "paid", Invoice.customer_id.in_(ids))
        .group_by(Invoice.customer_id)
        .all()
    )
    for sr in stat_rows:
        stats[sr[0]] = {"overdue_count": int(sr[1] or 0), "total_overdue": float(sr[2] or 0)}

    items = []
    for idx, score in ranked:
        r = rows[idx]
        s = stats.get(r[0], {"overdue_count": 0, "total_overdue": 0.0})
        items.append({
            "id": r[0],
            "ragione_sociale": r[1],
            "partita_iva": r[2],
            "excluded": bool(r[3]),
            "score": score,
            "overdue_count": s["overdue_count"],
            "total_overdue": s["total_overdue"],
        })
    return {"query": q, "items": items}


@router.get("/{customer_id}")
async def get_customer_detail(
    customer_id: int,
    session: Session = Depends(get_session),
):
    """Get detailed information for a customer including their invoices."""
    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        # Get all invoices for this customer
        invoices = session.query(Invoice).filter(
            Invoice.customer_id == customer_id
        ).order_by(Invoice.due_date.desc()).all()

        # Calculate totals excluding paid invoices
        total_amount = sum(inv.amount for inv in invoices if inv.status != "paid")
        total_due = sum(inv.amount_due for inv in invoices if inv.status != "paid")

        invoice_list = [
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "amount": float(inv.amount),
                "amount_due": float(inv.amount_due),
                "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
                "due_date": inv.due_date.isoformat() if inv.due_date else None,
                "due_date_source": inv.due_date_source or ("assumed" if inv.due_date else None),
                "days_overdue": inv.days_overdue,
                "status": inv.status,
                "source_platform": inv.source_platform,
                "shopify_order_number": inv.shopify_order_number,
                # Controllo puntuale P.IVA + ragione sociale (istantaneo:
                # confronta dati già in DB, nessun sync). Semaforo per-riga.
                "verification": verify_invoice_customer(inv, customer),
            }
            for inv in invoices
        ]

        # Fatture in QUARANTENA suggerite a QUESTO cliente (customer_id NULL +
        # suggested_customer_id → lui): senza questa lista non compaiono MAI
        # sul profilo e l'operatore che le cerca qui conclude che il sistema
        # non le conosce (caso Belfiore 655/2026). Le 'paid' sono INCLUSE:
        # una quarantenata marcata pagata sparirebbe altrimenti ovunque
        # (/positions/suggestions filtra status != 'paid').
        pending_invoices = (
            session.query(Invoice)
            .filter(
                Invoice.customer_id.is_(None),
                Invoice.suggested_customer_id == customer_id,
            )
            .order_by(Invoice.due_date.desc())
            .all()
        )

        pending_suggestions = [
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "amount": float(inv.amount),
                "amount_due": float(inv.amount_due),
                "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
                "due_date": inv.due_date.isoformat() if inv.due_date else None,
                "days_overdue": inv.days_overdue,
                "status": inv.status,
                "suggested_method": inv.suggested_method,
                "suggested_score": inv.suggested_score,
                "customer_name_raw": inv.customer_name_raw,
                "source_platform": inv.source_platform,
                # Verifica anche il suggerimento contro QUESTO cliente:
                # aiuta a decidere Conferma/Rifiuta con lo stesso semaforo.
                "verification": verify_invoice_customer(inv, customer),
            }
            for inv in pending_invoices
        ]

        # Get recovery actions
        actions = (
            session.query(RecoveryAction)
            .filter(RecoveryAction.customer_id == customer_id)
            .order_by(RecoveryAction.created_at.desc())
            .limit(20)
            .all()
        )

        action_list = [
            {
                "id": a.id,
                "action_type": a.action_type,
                "scheduled_date": a.scheduled_date.isoformat() if a.scheduled_date else None,
                "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                "outcome": a.outcome,
                "notes": a.notes,
                "channel": a.channel,
                "invoice_ids": a.invoice_ids or [],
                "cancelled": bool(a.cancelled),
                "cancelled_reason": a.cancelled_reason,
                "case_id": a.case_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in actions
        ]

        # Pratica aperta: numerazione e tono contano SOLO le azioni di
        # questo ciclo di debito (più gli eventuali contatti ereditati da
        # una pratica archiviata), mai tutta la storia del cliente.
        open_case = get_open_case(session, customer_id)
        case_block = None
        contact_action_count = 0
        if open_case:
            contact_action_count = contact_count(session, open_case)
            today_start = business_day_start()
            sollecito_today = (
                session.query(func.count(RecoveryAction.id))
                .filter(
                    RecoveryAction.case_id == open_case.id,
                    RecoveryAction.channel.in_(["whatsapp_copy", "whatsapp_link"]),
                    RecoveryAction.completed_at >= today_start,
                    RecoveryAction.cancelled.isnot(True),
                )
                .scalar() or 0
            ) > 0
            case_block = {
                "id": open_case.id,
                "opened_at": open_case.opened_at.isoformat() if open_case.opened_at else None,
                "contact_count": contact_action_count,
                "inherited_contacts": open_case.inherited_contacts or 0,
                "reopened_after_archive": bool(open_case.reopened_after_archive),
                "sollecito_registered_today": sollecito_today,
            }

        return {
            "id": customer.id,
            "ragione_sociale": customer.ragione_sociale,
            "partita_iva": customer.partita_iva,
            "codice_fiscale": customer.codice_fiscale,
            "phone": customer.phone,
            "phones": customer.phones_json or [],
            "email": customer.email,
            "excluded": customer.excluded,
            "source": customer.source,
            "phone_validated": customer.phone_validated,
            "shopify_id": customer.shopify_id,
            "tags": customer.tags,
            "recovery_status": customer.recovery_status,
            "next_action_date": customer.next_action_date.isoformat() if customer.next_action_date else None,
            "next_action_type": customer.next_action_type,
            "created_at": customer.created_at.isoformat(),
            "updated_at": customer.updated_at.isoformat(),
            "invoices": {
                "total_amount": float(total_amount),
                "total_due": float(total_due),
                "count": len(invoices),
                "items": invoice_list,
            },
            "pending_suggestions": pending_suggestions,
            "recovery_actions": action_list,
            "contact_action_count": contact_action_count,
            "case": case_block,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer detail: {e}", exc_info=True)
        raise


@router.get("/{customer_id}/neighbors")
async def get_customer_neighbors(
    customer_id: int,
    session: Session = Depends(get_session),
):
    """
    Get previous and next customer IDs for navigation.
    Based on overdue customers sorted by total_overdue desc.
    """
    from sqlalchemy import case
    try:
        # Get ordered list of customer IDs with overdue invoices (same as default sort)
        overdue_customers = (
            session.query(
                Customer.id,
                func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).label("total_overdue"),
            )
            .join(Invoice, Invoice.customer_id == Customer.id)
            .filter(
                Invoice.status != "paid",
                Customer.excluded.is_(False),
            )
            .group_by(Customer.id)
            .having(func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)) > 0)
            .order_by(func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).desc())
            .all()
        )

        ids = [row[0] for row in overdue_customers]
        prev_id = None
        next_id = None

        if customer_id in ids:
            idx = ids.index(customer_id)
            if idx > 0:
                prev_id = ids[idx - 1]
            if idx < len(ids) - 1:
                next_id = ids[idx + 1]

        return {
            "prev_id": prev_id,
            "next_id": next_id,
            "position": ids.index(customer_id) + 1 if customer_id in ids else None,
            "total": len(ids),
        }

    except Exception as e:
        logger.error(f"Error fetching customer neighbors: {e}", exc_info=True)
        raise


@router.put("/{customer_id}/exclude")
async def toggle_customer_exclusion(
    customer_id: int,
    exclude: bool,
    session: Session = Depends(get_session),
):
    """Toggle the excluded flag for a customer."""
    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        customer.excluded = exclude
        customer.updated_at = datetime.utcnow()
        session.commit()

        # Log activity
        activity = ActivityLog(
            action="customer_excluded" if exclude else "customer_included",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "excluded": exclude,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Customer {customer_id} exclusion status changed to {exclude}")

        return {
            "id": customer.id,
            "excluded": customer.excluded,
            "updated_at": customer.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating customer exclusion: {e}", exc_info=True)
        session.rollback()
        raise


@router.put("/{customer_id}/phone")
async def update_customer_phone(
    customer_id: int,
    phone: str,
    session: Session = Depends(get_session),
):
    """Update the phone number for a customer and sync to Shopify."""
    try:
        customer = session.query(Customer).filter(
            Customer.id == customer_id
        ).first()
        if not customer:
            raise HTTPException(
                status_code=404, detail="Customer not found"
            )

        old_phone = customer.phone
        customer.phone = phone
        customer.phone_validated = False
        customer.updated_at = datetime.utcnow()

        # Add/update in phones_json
        phones = customer.phones_json or []
        # Check if there's already a "Manuale" entry
        manual_found = False
        for p in phones:
            if p.get("source") == "manual":
                p["number"] = phone
                manual_found = True
                break
        if not manual_found:
            phones.insert(0, {
                "number": phone,
                "source": "manual",
                "label": "Manuale",
            })
        customer.phones_json = phones

        session.commit()

        # Sync to Shopify if customer has shopify_id
        shopify_synced = False
        if customer.shopify_id:
            try:
                from backend.connectors.shopify import (
                    ShopifyConnector,
                )
                shopify = ShopifyConnector()
                shopify_synced = shopify.update_customer_phone(
                    customer.shopify_id, phone
                )
            except Exception as e:
                logger.warning(
                    f"Could not sync phone to Shopify: {e}"
                )

        # Log activity
        activity = ActivityLog(
            action="phone_updated",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "old_phone": old_phone,
                "new_phone": phone,
                "shopify_synced": shopify_synced,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(
            f"Customer {customer_id} phone updated: "
            f"{old_phone} -> {phone} "
            f"(shopify={'ok' if shopify_synced else 'skip'})"
        )

        return {
            "id": customer.id,
            "phone": customer.phone,
            "phones": customer.phones_json or [],
            "phone_validated": customer.phone_validated,
            "shopify_synced": shopify_synced,
            "updated_at": customer.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating customer phone: {e}", exc_info=True)
        session.rollback()
        raise


class CreateCustomerRequest(BaseModel):
    ragione_sociale: str
    partita_iva: Optional[str] = None
    codice_fiscale: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


@router.post("")
async def create_customer(
    body: CreateCustomerRequest,
    session: Session = Depends(get_session),
):
    """Create a new customer manually (for data corrections)."""
    try:
        ragione_sociale = body.ragione_sociale
        partita_iva = body.partita_iva
        codice_fiscale = body.codice_fiscale
        phone = body.phone
        email = body.email

        existing = session.query(Customer).filter(
            Customer.ragione_sociale == ragione_sociale
        ).first()
        if existing:
            return {
                "id": existing.id,
                "ragione_sociale": existing.ragione_sociale,
                "partita_iva": existing.partita_iva,
                "already_existed": True,
            }

        import re
        normalized = re.sub(r'[^a-z0-9]', '', ragione_sociale.lower())

        customer = Customer(
            ragione_sociale=ragione_sociale,
            ragione_sociale_normalized=normalized,
            partita_iva=partita_iva,
            codice_fiscale=codice_fiscale,
            phone=phone,
            email=email,
            source="manual",
        )
        session.add(customer)
        session.commit()

        activity = ActivityLog(
            action="customer_created",
            entity_type="customer",
            entity_id=customer.id,
            details={
                "ragione_sociale": ragione_sociale,
                "source": "manual",
                "reason": "data_correction",
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Customer created manually: {ragione_sociale} (ID: {customer.id})")

        return {
            "id": customer.id,
            "ragione_sociale": customer.ragione_sociale,
            "partita_iva": customer.partita_iva,
            "already_existed": False,
        }

    except Exception as e:
        logger.error(f"Error creating customer: {e}", exc_info=True)
        session.rollback()
        raise
