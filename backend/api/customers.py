"""Customers API endpoints."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, Integer
from sqlalchemy.orm import Session
from datetime import datetime

from sqlalchemy import func
from backend.database import get_session, Customer, Invoice, ActivityLog, RecoveryAction

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
    from sqlalchemy import case as sa_case

    try:
        # Build stats subquery
        stats_sub = (
            session.query(
                Invoice.customer_id.label("cid"),
                func.count(Invoice.id).label("inv_count"),
                func.coalesce(func.sum(Invoice.amount_due), 0).label("t_due"),
                func.coalesce(func.sum(
                    func.cast(Invoice.days_overdue > 0, Integer)
                ), 0).label("o_count"),
                func.coalesce(func.sum(
                    sa_case(
                        (Invoice.days_overdue > 0, Invoice.amount_due),
                        else_=0,
                    )
                ), 0).label("t_overdue"),
            )
            .filter(Invoice.status != "paid")
            .group_by(Invoice.customer_id)
            .subquery("inv_stats")
        )

        query = (
            session.query(
                Customer,
                func.coalesce(stats_sub.c.inv_count, 0).label("invoice_count"),
                func.coalesce(stats_sub.c.t_due, 0).label("total_due"),
                func.coalesce(stats_sub.c.o_count, 0).label("overdue_count"),
                func.coalesce(stats_sub.c.t_overdue, 0).label("total_overdue"),
            )
            .outerjoin(stats_sub, Customer.id == stats_sub.c.cid)
        )

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

        if only_overdue:
            query = query.filter(stats_sub.c.o_count > 0)

        total = query.count()

        # Sorting
        if sort_by == "total_overdue":
            col = stats_sub.c.t_overdue
            query = query.order_by(col.desc() if sort_order == "desc" else col.asc())
        elif sort_by == "overdue_count":
            col = stats_sub.c.o_count
            query = query.order_by(col.desc() if sort_order == "desc" else col.asc())
        else:
            query = query.order_by(Customer.ragione_sociale.asc())

        results = query.offset(skip).limit(limit).all()

        items = []
        for row in results:
            cust = row[0]
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
                "invoice_count": row[1] or 0,
                "total_due": float(row[2] or 0),
                "overdue_count": row[3] or 0,
                "total_overdue": float(row[4] or 0),
                "created_at": cust.created_at.isoformat(),
            })

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": items,
        }

    except Exception as e:
        logger.error(f"Error listing customers: {e}", exc_info=True)
        raise


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
                "days_overdue": inv.days_overdue,
                "status": inv.status,
                "source_platform": inv.source_platform,
            }
            for inv in invoices
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
                "notes": a.notes,
                "created_at": a.created_at.isoformat(),
            }
            for a in actions
        ]

        return {
            "id": customer.id,
            "ragione_sociale": customer.ragione_sociale,
            "partita_iva": customer.partita_iva,
            "codice_fiscale": customer.codice_fiscale,
            "phone": customer.phone,
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
            "recovery_actions": action_list,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer detail: {e}", exc_info=True)
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
    """Update the phone number for a customer."""
    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        old_phone = customer.phone
        customer.phone = phone
        customer.phone_validated = False  # Reset validation status when phone is updated
        customer.updated_at = datetime.utcnow()
        session.commit()

        # Log activity
        activity = ActivityLog(
            action="phone_updated",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "old_phone": old_phone,
                "new_phone": phone,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Customer {customer_id} phone updated from {old_phone} to {phone}")

        return {
            "id": customer.id,
            "phone": customer.phone,
            "phone_validated": customer.phone_validated,
            "updated_at": customer.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating customer phone: {e}", exc_info=True)
        session.rollback()
        raise
