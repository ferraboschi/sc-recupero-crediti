"""Recovery workflow API endpoints."""

import logging
from datetime import datetime, date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from pydantic import BaseModel
from io import BytesIO

from backend.database import get_session, Customer, Invoice, RecoveryAction, ActivityLog

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Pydantic models ---

class ActionCreate(BaseModel):
    action_type: str  # first_contact / second_contact / lawyer / archive / wait / note
    scheduled_date: Optional[str] = None  # YYYY-MM-DD
    notes: Optional[str] = None


class ActionUpdate(BaseModel):
    notes: Optional[str] = None
    scheduled_date: Optional[str] = None


# --- Calendar endpoint ---

@router.get("/calendar")
async def get_calendar(
    session: Session = Depends(get_session),
    date_from: str = Query(None, description="Start date (YYYY-MM-DD), defaults to today"),
    date_to: str = Query(None, description="End date (YYYY-MM-DD), defaults to +30 days"),
):
    """
    Get all scheduled recovery actions for the calendar view.
    Returns actions grouped by date.
    """
    try:
        today = date.today()
        start = date.fromisoformat(date_from) if date_from else today - timedelta(days=7)
        end = date.fromisoformat(date_to) if date_to else today + timedelta(days=60)

        actions = (
            session.query(RecoveryAction)
            .join(Customer)
            .filter(
                RecoveryAction.scheduled_date.isnot(None),
                RecoveryAction.scheduled_date >= start,
                RecoveryAction.scheduled_date <= end,
                RecoveryAction.completed_at.is_(None),
            )
            .order_by(RecoveryAction.scheduled_date.asc())
            .all()
        )

        # Also get customers with next_action_date that don't have a pending action
        customers_with_actions = (
            session.query(Customer)
            .filter(
                Customer.next_action_date.isnot(None),
                Customer.next_action_date >= start,
                Customer.next_action_date <= end,
                Customer.recovery_status != "archived",
            )
            .all()
        )

        items = []
        seen_customer_ids = set()

        for action in actions:
            seen_customer_ids.add(action.customer_id)
            # Count overdue invoices for this customer
            overdue_count = session.query(Invoice).filter(
                Invoice.customer_id == action.customer_id,
                Invoice.status != "paid",
                Invoice.days_overdue > 0,
            ).count()
            total_due = session.query(Invoice).filter(
                Invoice.customer_id == action.customer_id,
                Invoice.status != "paid",
            ).with_entities(Invoice.amount_due).all()
            total_amount = sum(r[0] for r in total_due) if total_due else 0

            items.append({
                "id": action.id,
                "customer_id": action.customer_id,
                "customer_name": action.customer.ragione_sociale,
                "action_type": action.action_type,
                "scheduled_date": action.scheduled_date.isoformat(),
                "notes": action.notes,
                "overdue_invoices": overdue_count,
                "total_due": float(total_amount),
                "source": "action",
            })

        # Add customers that have next_action_date but no pending action record
        for cust in customers_with_actions:
            if cust.id not in seen_customer_ids:
                overdue_count = session.query(Invoice).filter(
                    Invoice.customer_id == cust.id,
                    Invoice.status != "paid",
                    Invoice.days_overdue > 0,
                ).count()
                total_due = session.query(Invoice).filter(
                    Invoice.customer_id == cust.id,
                    Invoice.status != "paid",
                ).with_entities(Invoice.amount_due).all()
                total_amount = sum(r[0] for r in total_due) if total_due else 0

                items.append({
                    "id": None,
                    "customer_id": cust.id,
                    "customer_name": cust.ragione_sociale,
                    "action_type": cust.next_action_type or cust.recovery_status,
                    "scheduled_date": cust.next_action_date.isoformat(),
                    "notes": None,
                    "overdue_invoices": overdue_count,
                    "total_due": float(total_amount),
                    "source": "customer",
                })

        # Sort all items by date
        items.sort(key=lambda x: x["scheduled_date"])

        return {"items": items, "total": len(items)}

    except Exception as e:
        logger.error(f"Error fetching calendar: {e}", exc_info=True)
        raise


# --- Customer recovery actions ---

@router.get("/customers/{customer_id}/actions")
async def get_customer_actions(
    customer_id: int,
    session: Session = Depends(get_session),
):
    """Get recovery action history for a customer."""
    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        actions = (
            session.query(RecoveryAction)
            .filter(RecoveryAction.customer_id == customer_id)
            .order_by(RecoveryAction.created_at.desc())
            .all()
        )

        return {
            "customer_id": customer_id,
            "recovery_status": customer.recovery_status,
            "next_action_date": customer.next_action_date.isoformat() if customer.next_action_date else None,
            "next_action_type": customer.next_action_type,
            "actions": [
                {
                    "id": a.id,
                    "action_type": a.action_type,
                    "scheduled_date": a.scheduled_date.isoformat() if a.scheduled_date else None,
                    "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                    "outcome": a.outcome,
                    "notes": a.notes,
                    "created_at": a.created_at.isoformat(),
                }
                for a in actions
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer actions: {e}", exc_info=True)
        raise


@router.post("/customers/{customer_id}/actions")
async def create_action(
    customer_id: int,
    action: ActionCreate,
    session: Session = Depends(get_session),
):
    """
    Create a new recovery action for a customer.

    Action types and their behavior:
    - first_contact: Schedule first contact, sets next_action +7 days
    - second_contact: Schedule second contact, sets next_action +14 days
    - lawyer: Pass to lawyer, auto-schedules follow-up in 30 days
    - archive: Mark as unrecoverable, no next action
    - wait: Postpone next action by 30 days
    - note: Just add a note, no status change
    """
    valid_types = ["first_contact", "second_contact", "lawyer", "archive", "wait", "note"]
    if action.action_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action type. Must be one of: {', '.join(valid_types)}"
        )

    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        today = date.today()
        scheduled = date.fromisoformat(action.scheduled_date) if action.scheduled_date else None

        # Create the action record
        new_action = RecoveryAction(
            customer_id=customer_id,
            action_type=action.action_type,
            scheduled_date=scheduled or today,
            notes=action.notes,
        )
        session.add(new_action)

        # Update customer recovery status and next action
        if action.action_type == "first_contact":
            customer.recovery_status = "first_contact"
            customer.next_action_date = today + timedelta(days=7)
            customer.next_action_type = "second_contact"
        elif action.action_type == "second_contact":
            customer.recovery_status = "second_contact"
            customer.next_action_date = today + timedelta(days=14)
            customer.next_action_type = "lawyer"
        elif action.action_type == "lawyer":
            customer.recovery_status = "lawyer"
            customer.next_action_date = today + timedelta(days=30)
            customer.next_action_type = "lawyer"  # Follow-up with lawyer
        elif action.action_type == "archive":
            customer.recovery_status = "archived"
            customer.next_action_date = None
            customer.next_action_type = None
        elif action.action_type == "wait":
            customer.recovery_status = "waiting"
            customer.next_action_date = today + timedelta(days=30)
            # Keep same next_action_type
        # "note" doesn't change status

        customer.updated_at = datetime.utcnow()
        session.commit()

        # Log activity
        activity = ActivityLog(
            action=f"recovery_{action.action_type}",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "action_type": action.action_type,
                "notes": action.notes,
                "next_action_date": customer.next_action_date.isoformat() if customer.next_action_date else None,
            }
        )
        session.add(activity)
        session.commit()

        return {
            "id": new_action.id,
            "action_type": new_action.action_type,
            "scheduled_date": new_action.scheduled_date.isoformat() if new_action.scheduled_date else None,
            "recovery_status": customer.recovery_status,
            "next_action_date": customer.next_action_date.isoformat() if customer.next_action_date else None,
            "next_action_type": customer.next_action_type,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating action: {e}", exc_info=True)
        session.rollback()
        raise


@router.put("/customers/{customer_id}/actions/{action_id}/complete")
async def complete_action(
    customer_id: int,
    action_id: int,
    outcome: Optional[str] = Query(None, description="Action outcome: contacted/promised/partial_payment/paid/unreachable/disputed/no_answer"),
    notes: Optional[str] = Query(None, description="Additional notes"),
    session: Session = Depends(get_session),
):
    """Mark a recovery action as completed and auto-create the next action in the progression.

    Progression: first_contact → second_contact → lawyer
    - first_contact completato → crea second_contact schedulato +14gg
    - second_contact completato → crea lawyer schedulato +30gg
    - lawyer completato → follow-up lawyer +30gg
    """
    try:
        action = session.query(RecoveryAction).filter(
            RecoveryAction.id == action_id,
            RecoveryAction.customer_id == customer_id,
        ).first()
        if not action:
            raise HTTPException(status_code=404, detail="Action not found")

        action.completed_at = datetime.utcnow()
        if outcome:
            action.outcome = outcome
        if notes:
            action.notes = (action.notes or '') + (' | Esito: ' + notes if action.notes else notes)

        customer = session.query(Customer).filter(Customer.id == customer_id).first()

        # --- Auto-progression: crea la prossima azione automaticamente ---
        today = date.today()
        next_action = None
        PROGRESSION = {
            "first_contact": ("second_contact", 14),
            "second_contact": ("lawyer", 30),
            "lawyer": ("lawyer", 30),  # follow-up avvocato
        }

        if action.action_type in PROGRESSION and customer:
            next_type, next_days = PROGRESSION[action.action_type]
            next_date = today + timedelta(days=next_days)

            next_action = RecoveryAction(
                customer_id=customer_id,
                action_type=next_type,
                scheduled_date=next_date,
                notes=f"Auto-generato dopo completamento {action.action_type}",
            )
            session.add(next_action)

            # Aggiorna stato cliente
            customer.recovery_status = next_type
            customer.next_action_date = next_date
            customer.next_action_type = next_type
            customer.updated_at = datetime.utcnow()

        session.commit()

        # Log activity
        activity = ActivityLog(
            action="recovery_completed",
            entity_type="recovery_action",
            entity_id=action_id,
            details={
                "customer_id": customer_id,
                "outcome": outcome,
                "action_type": action.action_type,
                "next_action_type": next_action.action_type if next_action else None,
                "next_action_date": next_action.scheduled_date.isoformat() if next_action else None,
            }
        )
        session.add(activity)
        session.commit()

        result = {
            "id": action.id,
            "completed_at": action.completed_at.isoformat(),
            "outcome": action.outcome,
        }
        if next_action:
            result["next_action"] = {
                "id": next_action.id,
                "action_type": next_action.action_type,
                "scheduled_date": next_action.scheduled_date.isoformat(),
            }
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error completing action: {e}", exc_info=True)
        session.rollback()
        raise


# --- PDF Riepilogativo ---

@router.get("/customers/{customer_id}/pdf-riepilogativo")
async def generate_pdf_riepilogativo(
    customer_id: int,
    session: Session = Depends(get_session),
    overdue_only: bool = Query(True, description="Include only overdue invoices"),
):
    """
    Generate a PDF summary of overdue invoices for a customer.
    Includes: invoice number, amount, due date, total, IBAN.
    """
    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        # Get invoices
        query = session.query(Invoice).filter(
            Invoice.customer_id == customer_id,
            Invoice.status != "paid",
        )
        if overdue_only:
            query = query.filter(Invoice.days_overdue > 0)

        invoices = query.order_by(Invoice.due_date.asc()).all()

        if not invoices:
            raise HTTPException(status_code=404, detail="No invoices found for this customer")

        # Generate PDF
        pdf_bytes = _build_riepilogativo_pdf(customer, invoices)

        filename = f"riepilogativo_{customer.ragione_sociale.replace(' ', '_')}.pdf"

        return StreamingResponse(
            BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating PDF: {e}", exc_info=True)
        raise


@router.get("/invoices/{invoice_id}/pdf")
async def generate_single_invoice_pdf(
    invoice_id: int,
    session: Session = Depends(get_session),
):
    """
    Generate a PDF for a single invoice with payment details.
    """
    try:
        invoice = session.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")

        customer = session.query(Customer).filter(Customer.id == invoice.customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        pdf_bytes = _build_invoice_pdf(customer, invoice)
        safe_num = invoice.invoice_number.replace('/', '_')
        safe_name = customer.ragione_sociale.replace(' ', '_')
        filename = f"fattura_{safe_num}_{safe_name}.pdf"

        return StreamingResponse(
            BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating single invoice PDF: {e}", exc_info=True)
        raise


@router.get("/customers/{customer_id}/pdf-selected")
async def generate_selected_invoices_pdf(
    customer_id: int,
    invoice_ids: str = Query(..., description="Comma-separated invoice IDs"),
    session: Session = Depends(get_session),
):
    """
    Generate a PDF riepilogativo for selected invoices only.
    """
    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        ids = [int(x.strip()) for x in invoice_ids.split(",") if x.strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="No invoice IDs provided")

        invoices = (
            session.query(Invoice)
            .filter(Invoice.id.in_(ids), Invoice.customer_id == customer_id)
            .order_by(Invoice.due_date.asc())
            .all()
        )

        if not invoices:
            raise HTTPException(status_code=404, detail="No invoices found")

        pdf_bytes = _build_riepilogativo_pdf(customer, invoices)
        filename = f"riepilogativo_{customer.ragione_sociale.replace(' ', '_')}.pdf"

        return StreamingResponse(
            BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating selected invoices PDF: {e}", exc_info=True)
        raise


def _build_riepilogativo_pdf(customer, invoices):
    """Build the PDF riepilogativo using fpdf2."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Header
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "Sake Company", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "Sake Company srl", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(8)

    # Title
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Riepilogo Fatture Scadute", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(6)

    # Customer info
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, f"Cliente: {customer.ragione_sociale}", new_x="LMARGIN", new_y="NEXT")
    if customer.partita_iva:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, f"P.IVA: {customer.partita_iva}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Data: {date.today().strftime('%d/%m/%Y')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # Table header
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    col_widths = [50, 45, 40, 55]  # Fattura, Importo, Scadenza, GG Ritardo
    headers = ["N. Fattura", "Importo", "Scadenza", "GG Ritardo"]
    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], 8, h, border=1, fill=True, align="C")
    pdf.ln()

    # Table rows
    pdf.set_font("Helvetica", "", 10)
    total_due = 0.0
    for inv in invoices:
        total_due += float(inv.amount_due)
        pdf.cell(col_widths[0], 7, str(inv.invoice_number)[:25], border=1, align="L")
        pdf.cell(col_widths[1], 7, f"{float(inv.amount_due):,.2f} EUR".replace(",", "."), border=1, align="R")
        pdf.cell(col_widths[2], 7, inv.due_date.strftime("%d/%m/%Y") if inv.due_date else "-", border=1, align="C")
        pdf.cell(col_widths[3], 7, str(inv.days_overdue or 0), border=1, align="C")
        pdf.ln()

    # Total row
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(col_widths[0], 9, "TOTALE", border=1, fill=True, align="R")
    pdf.cell(col_widths[1], 9, f"{total_due:,.2f} EUR".replace(",", "."), border=1, fill=True, align="R")
    pdf.cell(col_widths[2] + col_widths[3], 9, "", border=1, fill=True)
    pdf.ln(14)

    # Payment info
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Coordinate per il pagamento:", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, "Pagamento: a vista", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, "Intestatario: Sake Company srl", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, "IBAN: IT44N0200801671000105175151", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, "Banca: UniCredit", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Causale: Saldo fatture {customer.ragione_sociale}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # Footer note
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(
        0, 5,
        "Vi preghiamo di provvedere al pagamento a vista. "
        "Per qualsiasi chiarimento, non esitate a contattarci."
    )

    return pdf.output()


def _build_invoice_pdf(customer, invoice):
    """Build a courtesy copy PDF for a single invoice."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # --- Company header ---
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Sake Company srl", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, "P.IVA: 04aborita6 | Milano, Italia",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Line separator
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # --- FATTURA title + number ---
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 12, "FATTURA", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7,
             f"N. {invoice.invoice_number}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # --- Two-column: dates left, customer right ---
    y_start = pdf.get_y()

    # Left: dates
    pdf.set_font("Helvetica", "", 10)
    issue_str = (invoice.issue_date.strftime("%d/%m/%Y")
                 if invoice.issue_date else "-")
    due_str = (invoice.due_date.strftime("%d/%m/%Y")
               if invoice.due_date else "-")
    pdf.cell(95, 6, f"Data emissione: {issue_str}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(95, 6, f"Data scadenza: {due_str}",
             new_x="LMARGIN", new_y="NEXT")
    if invoice.days_overdue and invoice.days_overdue > 0:
        pdf.set_text_color(200, 0, 0)
        pdf.cell(95, 6,
                 f"Giorni di ritardo: {invoice.days_overdue}",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    pdf.cell(95, 6,
             f"Fonte: {invoice.source_platform or '-'}",
             new_x="LMARGIN", new_y="NEXT")
    y_after_left = pdf.get_y()

    # Right: customer box
    pdf.set_y(y_start)
    pdf.set_x(110)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(90, 6, "Destinatario:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(110)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(90, 6, customer.ragione_sociale or "-",
             new_x="LMARGIN", new_y="NEXT")
    if customer.partita_iva:
        pdf.set_x(110)
        pdf.cell(90, 6, f"P.IVA: {customer.partita_iva}",
                 new_x="LMARGIN", new_y="NEXT")
    if customer.codice_fiscale:
        pdf.set_x(110)
        pdf.cell(90, 6, f"C.F.: {customer.codice_fiscale}",
                 new_x="LMARGIN", new_y="NEXT")
    if customer.email:
        pdf.set_x(110)
        pdf.cell(90, 6, f"Email: {customer.email}",
                 new_x="LMARGIN", new_y="NEXT")

    pdf.set_y(max(y_after_left, pdf.get_y()) + 8)

    # --- Invoice line table ---
    pdf.set_draw_color(180, 180, 180)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(90, 8, "Descrizione", border=1, fill=True, align="L")
    pdf.cell(35, 8, "Importo", border=1, fill=True, align="R")
    pdf.cell(35, 8, "Dovuto", border=1, fill=True, align="R")
    pdf.cell(30, 8, "Stato", border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("Helvetica", "", 10)
    desc = f"Fattura n. {invoice.invoice_number}"
    status_label = {
        "open": "Aperto", "paid": "Pagato",
        "contacted": "Contattato", "promised": "Promesso",
        "disputed": "Contestato", "escalated": "Escalation",
    }.get(invoice.status, invoice.status or "-")

    pdf.cell(90, 7, desc[:45], border=1, align="L")
    pdf.cell(35, 7,
             f"{float(invoice.amount):,.2f}".replace(",", "."),
             border=1, align="R")
    pdf.cell(35, 7,
             f"{float(invoice.amount_due):,.2f}".replace(",", "."),
             border=1, align="R")
    pdf.cell(30, 7, status_label, border=1, align="C")
    pdf.ln()

    # Totals
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(90, 9, "", border=0)
    pdf.cell(35, 9, "TOTALE:", border=1, fill=True, align="R")
    pdf.cell(35, 9,
             f"{float(invoice.amount_due):,.2f} EUR".replace(",", "."),
             border=1, fill=True, align="R")
    pdf.cell(30, 9, "", border=1, fill=True)
    pdf.ln(14)

    # --- Payment info ---
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Coordinate per il pagamento:",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "Intestatario: Sake Company srl",
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "IBAN: IT44N0200801671000105175151",
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "Banca: UniCredit",
             new_x="LMARGIN", new_y="NEXT")
    causale = f"Saldo fattura {invoice.invoice_number}"
    pdf.cell(0, 6, f"Causale: {causale}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    # Footer
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5,
             "Copia di cortesia generata da SC Recupero Crediti",
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 5,
             f"Generata il {date.today().strftime('%d/%m/%Y')}",
             new_x="LMARGIN", new_y="NEXT", align="C")

    return pdf.output()


@router.get("/customers/{customer_id}/invoices-zip")
async def download_invoices_zip(
    customer_id: int,
    invoice_ids: str = Query(
        ..., description="Comma-separated invoice IDs"
    ),
    session: Session = Depends(get_session),
):
    """
    Download selected invoices as individual PDFs in a ZIP file.
    Each PDF is a courtesy copy of the original invoice.
    """
    import zipfile

    try:
        customer = session.query(Customer).filter(
            Customer.id == customer_id
        ).first()
        if not customer:
            raise HTTPException(
                status_code=404, detail="Customer not found"
            )

        ids = [
            int(x.strip())
            for x in invoice_ids.split(",") if x.strip()
        ]
        if not ids:
            raise HTTPException(
                status_code=400, detail="No invoice IDs provided"
            )

        invoices = (
            session.query(Invoice)
            .filter(
                Invoice.id.in_(ids),
                Invoice.customer_id == customer_id,
            )
            .order_by(Invoice.due_date.asc())
            .all()
        )

        if not invoices:
            raise HTTPException(
                status_code=404, detail="No invoices found"
            )

        # Build ZIP with individual PDFs
        zip_buffer = BytesIO()
        with zipfile.ZipFile(
            zip_buffer, "w", zipfile.ZIP_DEFLATED
        ) as zf:
            for inv in invoices:
                pdf_bytes = _build_invoice_pdf(customer, inv)
                safe_num = (
                    inv.invoice_number.replace("/", "_")
                    .replace("\\", "_")
                )
                fname = f"fattura_{safe_num}.pdf"
                zf.writestr(fname, pdf_bytes)

        zip_buffer.seek(0)
        safe_name = customer.ragione_sociale.replace(" ", "_")
        filename = f"fatture_{safe_name}.zip"

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition":
                f'attachment; filename="{filename}"'
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error generating invoices ZIP: {e}",
            exc_info=True,
        )
        raise


# --- Recovery Report / Attività ---

@router.get("/report")
async def get_recovery_report(
    session: Session = Depends(get_session),
):
    """
    Get recovery report with stats by status, upcoming deadlines, and paid summary.

    Returns data for the Attività report page:
    - recuperi_attivi: customers in active recovery (first_contact, second_contact)
    - saldato: customers/invoices that have been paid
    - in_attesa: customers in waiting status
    - avvocato: customers passed to lawyer
    - prossime_scadenze: upcoming scheduled actions (next 30 days)
    - summary stats
    """
    from sqlalchemy import func

    try:
        today = date.today()

        # --- Summary stats ---
        # Active recovery customers
        active_customers = session.query(Customer).filter(
            Customer.recovery_status.in_(["first_contact", "second_contact"]),
            Customer.excluded.is_(False),
        ).all()

        # Waiting customers
        waiting_customers = session.query(Customer).filter(
            Customer.recovery_status == "waiting",
            Customer.excluded.is_(False),
        ).all()

        # Lawyer customers
        lawyer_customers = session.query(Customer).filter(
            Customer.recovery_status == "lawyer",
            Customer.excluded.is_(False),
        ).all()

        # Paid invoices
        paid_invoices = session.query(Invoice).filter(
            Invoice.status == "paid",
        ).all()

        # Upcoming actions (next 30 days)
        upcoming_actions = (
            session.query(RecoveryAction)
            .join(Customer)
            .filter(
                RecoveryAction.scheduled_date.isnot(None),
                RecoveryAction.scheduled_date >= today,
                RecoveryAction.scheduled_date <= today + timedelta(days=30),
                RecoveryAction.completed_at.is_(None),
            )
            .order_by(RecoveryAction.scheduled_date.asc())
            .all()
        )

        # Also include customers with next_action_date
        customers_upcoming = session.query(Customer).filter(
            Customer.next_action_date.isnot(None),
            Customer.next_action_date >= today,
            Customer.next_action_date <= today + timedelta(days=30),
            Customer.recovery_status != "archived",
            Customer.excluded.is_(False),
        ).all()

        # Helper to get customer invoice stats
        def get_customer_stats(cust):
            inv_query = session.query(Invoice).filter(
                Invoice.customer_id == cust.id,
                Invoice.status != "paid",
            )
            total_due = sum(i.amount_due for i in inv_query.all())
            count = inv_query.count()
            return {
                "id": cust.id,
                "ragione_sociale": cust.ragione_sociale,
                "partita_iva": cust.partita_iva,
                "recovery_status": cust.recovery_status,
                "next_action_date": cust.next_action_date.isoformat() if cust.next_action_date else None,
                "next_action_type": cust.next_action_type,
                "total_due": float(total_due),
                "invoice_count": count,
            }

        # Build response
        return {
            "summary": {
                "active_count": len(active_customers),
                "active_total_due": float(sum(
                    sum(i.amount_due for i in session.query(Invoice).filter(
                        Invoice.customer_id == c.id, Invoice.status != "paid"
                    ).all())
                    for c in active_customers
                )),
                "waiting_count": len(waiting_customers),
                "lawyer_count": len(lawyer_customers),
                "paid_count": len(paid_invoices),
                "paid_total": float(sum(i.amount for i in paid_invoices)),
                "upcoming_actions_count": len(upcoming_actions) + len([
                    c for c in customers_upcoming
                    if c.id not in {a.customer_id for a in upcoming_actions}
                ]),
            },
            "recuperi_attivi": [get_customer_stats(c) for c in active_customers],
            "in_attesa": [get_customer_stats(c) for c in waiting_customers],
            "avvocato": [get_customer_stats(c) for c in lawyer_customers],
            "saldato": [
                {
                    "id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "amount": float(inv.amount),
                    "customer_name": inv.customer.ragione_sociale if inv.customer else inv.customer_name_raw,
                    "customer_id": inv.customer_id,
                    "source_platform": inv.source_platform,
                }
                for inv in paid_invoices[:50]  # Limit to recent 50
            ],
            "prossime_scadenze": [
                {
                    "id": a.id,
                    "customer_id": a.customer_id,
                    "customer_name": a.customer.ragione_sociale,
                    "action_type": a.action_type,
                    "scheduled_date": a.scheduled_date.isoformat(),
                    "notes": a.notes,
                }
                for a in upcoming_actions
            ],
        }

    except Exception as e:
        logger.error(f"Error fetching recovery report: {e}", exc_info=True)
        raise
