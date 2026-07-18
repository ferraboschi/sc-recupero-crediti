"""Positions (invoices + customers) API endpoints."""

import logging
import csv
from datetime import datetime
from io import StringIO
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.database import get_session, Invoice, Customer, ActivityLog

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_positions(
    session: Session = Depends(get_session),
    status: str = Query(None),
    min_amount: float = Query(None),
    search: str = Query(None),
    source: str = Query(None, description="Filter by source: fatturapro or fatture24"),
    issue_date_from: str = Query(None, description="Issue date from (YYYY-MM-DD)"),
    issue_date_to: str = Query(None, description="Issue date to (YYYY-MM-DD)"),
    due_date_from: str = Query(None, description="Due date from (YYYY-MM-DD)"),
    due_date_to: str = Query(None, description="Due date to (YYYY-MM-DD)"),
    overdue: str = Query(None, description="Filter by overdue status: 'yes' for overdue, 'no' for not overdue"),
    has_customer: str = Query(None, description="Filter by customer assignment: 'yes' for matched invoices only"),
    exclude_status: str = Query(None, description="Exclude invoices with this status (e.g. 'paid')"),
    sort_by: str = Query(None, description="Sort field: amount_due, issue_date, due_date, days_overdue"),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """
    List all positions (invoices joined with customers).

    Supports filtering by status, escalation level, minimum amount, search,
    source platform, overdue status, customer assignment, issue date range,
    and due date range.
    """
    from datetime import date as date_type

    try:
        query = session.query(Invoice).outerjoin(Customer)

        # Apply filters
        if status:
            query = query.filter(Invoice.status == status)

        if exclude_status:
            query = query.filter(Invoice.status != exclude_status)

        if min_amount is not None:
            query = query.filter(Invoice.amount_due >= min_amount)

        if source:
            query = query.filter(Invoice.source_platform == source)

        if overdue == "yes":
            query = query.filter(Invoice.days_overdue > 0)
        elif overdue == "no":
            query = query.filter(Invoice.days_overdue <= 0)

        if has_customer == "yes":
            query = query.filter(Invoice.customer_id.isnot(None))
        elif has_customer == "no":
            query = query.filter(Invoice.customer_id.is_(None))

        if issue_date_from:
            try:
                d = date_type.fromisoformat(issue_date_from)
                query = query.filter(Invoice.issue_date >= d)
            except ValueError:
                pass

        if issue_date_to:
            try:
                d = date_type.fromisoformat(issue_date_to)
                query = query.filter(Invoice.issue_date <= d)
            except ValueError:
                pass

        if due_date_from:
            try:
                d = date_type.fromisoformat(due_date_from)
                query = query.filter(Invoice.due_date >= d)
            except ValueError:
                pass

        if due_date_to:
            try:
                d = date_type.fromisoformat(due_date_to)
                query = query.filter(Invoice.due_date <= d)
            except ValueError:
                pass

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Customer.ragione_sociale.ilike(search_pattern),
                    Customer.partita_iva.ilike(search_pattern),
                    Invoice.invoice_number.ilike(search_pattern),
                    Invoice.customer_name_raw.ilike(search_pattern),
                )
            )

        # Sorting
        sort_map = {
            "amount_due": Invoice.amount_due,
            "issue_date": Invoice.issue_date,
            "due_date": Invoice.due_date,
            "days_overdue": Invoice.days_overdue,
        }
        if sort_by and sort_by in sort_map:
            col = sort_map[sort_by]
            query = query.order_by(col.desc() if sort_order == "desc" else col.asc())

        # Total count and sum before pagination (with_entities returns a new query, doesn't mutate)
        from sqlalchemy import func as sqlfunc
        agg = query.with_entities(
            sqlfunc.count(Invoice.id),
            sqlfunc.sum(Invoice.amount_due),
        ).first()
        total = agg[0] or 0
        summary_total_amount_due = float(agg[1] or 0)

        # Get paginated results (original query still intact)
        positions = query.offset(skip).limit(limit).all()

        return {
            "total": total,
            "summary_total_amount_due": float(summary_total_amount_due),
            "skip": skip,
            "limit": limit,
            "items": [
                {
                    "id": pos.id,
                    "invoice_number": pos.invoice_number,
                    "amount": float(pos.amount),
                    "amount_due": float(pos.amount_due),
                    "issue_date": pos.issue_date.isoformat() if pos.issue_date else None,
                    "due_date": pos.due_date.isoformat() if pos.due_date else None,
                    "due_date_source": pos.due_date_source or ("assumed" if pos.due_date else None),
                    "days_overdue": pos.days_overdue,
                    "status": pos.status,
                    "source_platform": pos.source_platform,
                    "customer_name_raw": pos.customer_name_raw,
                    "match_method": pos.match_method,
                    "has_suggestion": pos.suggested_customer_id is not None,
                    "customer": {
                        "id": pos.customer.id if pos.customer else None,
                        "ragione_sociale": pos.customer.ragione_sociale if pos.customer else pos.customer_name_raw,
                        "partita_iva": pos.customer.partita_iva if pos.customer else pos.customer_piva_raw,
                        "phone": pos.customer.phone if pos.customer else None,
                    } if pos.customer else None,
                }
                for pos in positions
            ],
        }

    except Exception as e:
        logger.error(f"Error listing positions: {e}", exc_info=True)
        raise


@router.get("/export")
async def export_positions(session: Session = Depends(get_session)):
    """Export all positions as CSV."""
    try:
        positions = session.query(Invoice).outerjoin(Customer).all()

        # Create CSV in memory
        output = StringIO()
        writer = csv.writer(output)

        # Write header
        writer.writerow([
            "Invoice Number",
            "Customer",
            "P.IVA",
            "Amount",
            "Amount Due",
            "Issue Date",
            "Due Date",
            "Days Overdue",
            "Status",
            "Phone",
            "Email",
        ])

        # Write data rows
        for pos in positions:
            writer.writerow([
                pos.invoice_number,
                pos.customer.ragione_sociale if pos.customer else pos.customer_name_raw,
                pos.customer.partita_iva if pos.customer else pos.customer_piva_raw,
                float(pos.amount),
                float(pos.amount_due),
                pos.issue_date.isoformat() if pos.issue_date else "",
                pos.due_date.isoformat() if pos.due_date else "",
                pos.days_overdue,
                pos.status,
                pos.customer.phone if pos.customer else "",
                pos.customer.email if pos.customer else "",
            ])

        # Return as streaming response
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=positions_export.csv"}
        )

    except Exception as e:
        logger.error(f"Error exporting positions: {e}", exc_info=True)
        raise


@router.get("/suggestions")
async def list_suggestions(session: Session = Depends(get_session)):
    """Fatture in quarantena: abbinamento suggerito da confermare o rifiutare.

    Il matching automatico assegna solo i casi sicuri (P.IVA univoca, nome
    esatto univoco); fuzzy e ambiguità finiscono qui e decide l'operatore.
    """
    try:
        invoices = (
            session.query(Invoice)
            .filter(
                Invoice.customer_id.is_(None),
                Invoice.suggested_customer_id.isnot(None),
                Invoice.status != "paid",
            )
            .order_by(Invoice.suggested_score.desc().nullslast())
            .all()
        )

        # Batch-load suggested customers (avoid N+1)
        cust_ids = {inv.suggested_customer_id for inv in invoices}
        customers = {}
        if cust_ids:
            for c in session.query(Customer).filter(Customer.id.in_(cust_ids)).all():
                customers[c.id] = c

        items = []
        for inv in invoices:
            cust = customers.get(inv.suggested_customer_id)
            items.append({
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "amount_due": float(inv.amount_due),
                "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
                "customer_name_raw": inv.customer_name_raw,
                "customer_piva_raw": inv.customer_piva_raw,
                "source_platform": inv.source_platform,
                "suggested_method": inv.suggested_method,
                "suggested_score": inv.suggested_score,
                # Bassa confidenza: fuzzy sotto 85, oppure score bassissimo
                # QUALUNQUE sia il metodo (un 'piva_name_mismatch' a 20 era
                # presentato come un suggerimento qualsiasi).
                "low_confidence": (
                    ((inv.suggested_score or 0) < 85 and inv.suggested_method == "fuzzy")
                    or (inv.suggested_score or 0) < 40
                ),
                "suggested_customer": {
                    "id": cust.id,
                    "ragione_sociale": cust.ragione_sociale,
                    "partita_iva": cust.partita_iva,
                } if cust else None,
            })

        return {"items": items, "total": len(items)}

    except Exception as e:
        logger.error(f"Error listing suggestions: {e}", exc_info=True)
        raise


@router.post("/{position_id}/confirm-suggestion")
async def confirm_suggestion(position_id: int, session: Session = Depends(get_session)):
    """Conferma il suggerimento: la fattura viene abbinata al cliente proposto."""
    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")
        if not position.suggested_customer_id:
            raise HTTPException(status_code=400, detail="Nessun suggerimento da confermare")

        customer = session.query(Customer).filter(
            Customer.id == position.suggested_customer_id
        ).first()
        if not customer:
            # Cliente sparito nel frattempo: pulisce il suggerimento
            position.suggested_customer_id = None
            position.suggested_method = None
            position.suggested_score = None
            session.commit()
            raise HTTPException(status_code=404, detail="Il cliente suggerito non esiste più")

        position.customer_id = customer.id
        position.case_id = None  # la pratica giusta viene agganciata dal lifecycle
        position.match_method = "fuzzy_confirmed"
        position.match_score = position.suggested_score
        method_was = position.suggested_method
        position.suggested_customer_id = None
        position.suggested_method = None
        position.suggested_score = None
        session.commit()

        session.add(ActivityLog(
            action="suggestion_confirmed",
            entity_type="invoice",
            entity_id=position_id,
            details={
                "invoice_number": position.invoice_number,
                "customer_id": customer.id,
                "customer_name": customer.ragione_sociale,
                "method": method_was,
                "score": position.match_score,
            },
        ))
        session.commit()

        return {
            "id": position.id,
            "customer_id": customer.id,
            "customer_name": customer.ragione_sociale,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error confirming suggestion: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{position_id}/reject-suggestion")
async def reject_suggestion(position_id: int, session: Session = Depends(get_session)):
    """Rifiuta il suggerimento: la fattura resta senza cliente e non verrà
    più riproposta in automatico (match_method='unlinked')."""
    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")
        if not position.suggested_customer_id:
            raise HTTPException(status_code=400, detail="Nessun suggerimento da rifiutare")

        rejected_id = position.suggested_customer_id
        position.suggested_customer_id = None
        position.suggested_method = None
        position.suggested_score = None
        position.match_method = "unlinked"
        session.commit()

        session.add(ActivityLog(
            action="suggestion_rejected",
            entity_type="invoice",
            entity_id=position_id,
            details={
                "invoice_number": position.invoice_number,
                "rejected_customer_id": rejected_id,
            },
        ))
        session.commit()

        return {"id": position.id, "rejected": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rejecting suggestion: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{position_id}/create-customer")
async def create_customer_from_invoice(position_id: int, session: Session = Depends(get_session)):
    """Crea un NUOVO cliente dai dati grezzi della fattura e la abbina.

    Serve quando il suggerimento in quarantena è sbagliato (es. fattura
    YOHO MILANO suggerita a Domò Milano): prima l'operatore poteva solo
    confermare l'errore o rifiutare (fattura 'unlinked' per sempre), e
    l'auto-create del sync salta le fatture con suggerimento pendente —
    il cliente giusto non nasceva mai da solo.
    """
    from backend.engine.normalizer import normalize_ragione_sociale
    from backend.engine.piva import validate_piva

    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")
        if position.customer_id:
            raise HTTPException(
                status_code=400,
                detail="La fattura è già abbinata a un cliente: usa Riassegna per correggerla",
            )

        name = (position.customer_name_raw or "").strip()
        if not name:
            raise HTTPException(
                status_code=400,
                detail="La fattura non ha un nome destinatario: impossibile creare il cliente",
            )

        # P.IVA solo se VALIDA (checksum/formato): una P.IVA sporca sul nuovo
        # cliente produrrebbe abbinamenti sbagliati a cascata nei sync futuri.
        piva = validate_piva(position.customer_piva_raw)
        name_norm = normalize_ragione_sociale(name)

        # Duplicati: se l'entità esiste già l'operatore deve usare Riassegna,
        # non creare un doppione che spacca lo storico del credito.
        if piva:
            # validate_piva toglie il prefisso 'IT'; in anagrafica può esserci
            # ancora la variante prefissata (es. clienti importati da Shopify).
            existing = session.query(Customer).filter(
                Customer.partita_iva.in_([piva, f"IT{piva}"])
            ).first()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Esiste già un cliente con questa P.IVA: "
                        f"'{existing.ragione_sociale}' (ID {existing.id}). "
                        f"Usa Riassegna per abbinare la fattura a quel cliente."
                    ),
                )
        if name_norm:
            # La colonna ragione_sociale_normalized contiene chiavi scritte
            # da versioni diverse del normalizzatore (mai backfillate): il
            # confronto affidabile è sul ricalcolo fresh, come fa il
            # matching (Strategia 2). Un omonimo con P.IVA valida DIVERSA
            # da quella della fattura è un'entità diversa (stessa regola di
            # matching/auto-create): non blocca la creazione.
            existing = None
            for c in session.query(Customer).all():
                if normalize_ragione_sociale(c.ragione_sociale or "") != name_norm:
                    continue
                cust_piva = validate_piva(c.partita_iva)
                if piva and cust_piva and piva != cust_piva:
                    continue
                existing = c
                break
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Esiste già un cliente con lo stesso nome: "
                        f"'{existing.ragione_sociale}' (ID {existing.id}). "
                        f"Usa Riassegna per abbinare la fattura a quel cliente."
                    ),
                )

        customer = Customer(
            ragione_sociale=name,
            ragione_sociale_normalized=name_norm,
            partita_iva=piva,
            source="manual",
        )
        session.add(customer)
        session.flush()  # serve l'ID per abbinare la fattura

        position.customer_id = customer.id
        position.case_id = None  # la pratica giusta viene agganciata dal lifecycle
        position.match_method = "manual"
        position.match_score = 100
        position.suggested_customer_id = None
        position.suggested_method = None
        position.suggested_score = None
        session.commit()

        session.add(ActivityLog(
            action="customer_created_from_invoice",
            entity_type="customer",
            entity_id=customer.id,
            details={
                "invoice_id": position.id,
                "invoice_number": position.invoice_number,
                "ragione_sociale": customer.ragione_sociale,
                "partita_iva": customer.partita_iva,
                "source_platform": position.source_platform,
            },
        ))
        session.commit()

        logger.info(
            f"Customer '{customer.ragione_sociale}' (ID {customer.id}) created "
            f"from invoice {position.invoice_number} and linked"
        )

        return {
            "id": position.id,
            "customer_id": customer.id,
            "customer_name": customer.ragione_sociale,
            "partita_iva": customer.partita_iva,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating customer from invoice: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{position_id}/unlink")
async def unlink_position(position_id: int, session: Session = Depends(get_session)):
    """Scollega una fattura dal cliente (abbinamento errato).

    La fattura viene marcata 'unlinked': non verrà mai più abbinata in
    automatico — solo suggerimenti con conferma esplicita o riassegnazione
    manuale (altrimenti il sync notturno rifarebbe lo stesso errore).
    """
    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")
        if not position.customer_id:
            raise HTTPException(status_code=400, detail="La fattura non è abbinata a nessun cliente")

        old_customer_id = position.customer_id
        old_customer = position.customer
        old_customer_name = old_customer.ragione_sociale if old_customer else None
        position.customer_id = None
        position.case_id = None
        position.match_method = "unlinked"
        position.match_score = None
        position.suggested_customer_id = None
        position.suggested_method = None
        position.suggested_score = None

        # Riallinea subito pratica e stato-cache del vecchio cliente
        # (prima restavano stantii fino al sync notturno).
        if old_customer is not None:
            from backend.engine.repair import reconcile_customer_after_detach
            try:
                reconcile_customer_after_detach(session, old_customer)
            except Exception as e:
                logger.warning(
                    f"Post-unlink reconcile failed for customer {old_customer_id}: {e}"
                )
        session.commit()

        session.add(ActivityLog(
            action="unlink",
            entity_type="invoice",
            entity_id=position_id,
            details={
                "invoice_number": position.invoice_number,
                "old_customer_id": old_customer_id,
                "old_customer_name": old_customer_name,
            },
        ))
        session.commit()

        logger.info(
            f"Position {position_id} unlinked from customer {old_customer_id} "
            f"('{old_customer_name}')"
        )
        return {"id": position.id, "unlinked": True, "old_customer_name": old_customer_name}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unlinking position: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{position_id}/assign-piva-to-customer")
async def assign_piva_to_customer(position_id: int, session: Session = Depends(get_session)):
    """Copia la P.IVA della FATTURA sul CLIENTE abbinato.

    Caso incoerente segnalato dall'audit come 'Dubbio': la fattura riporta
    una P.IVA valida ma il cliente in anagrafica non ne ha una. Con un click
    l'operatore allinea l'anagrafica alla fattura, così i match futuri
    diventano garantiti per P.IVA invece che per solo nome.

    Guardie:
    - 404 se la fattura o il cliente non esistono;
    - 400 se la fattura non è abbinata o non ha una P.IVA valida;
    - 409 se il cliente ha GIÀ una P.IVA valida DIVERSA (non la sovrascrivo
      in silenzio: il messaggio riporta entrambe);
    - no-op 200 se il cliente ha già la stessa P.IVA (validata).
    """
    from backend.engine.piva import validate_piva

    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")
        if not position.customer_id:
            raise HTTPException(
                status_code=400,
                detail="La fattura non è abbinata a nessun cliente",
            )

        customer = session.query(Customer).filter(
            Customer.id == position.customer_id
        ).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Cliente non trovato")

        # P.IVA solo se VALIDA (checksum/formato): copiare una P.IVA sporca
        # sull'anagrafica produrrebbe abbinamenti sbagliati a cascata.
        inv_piva = validate_piva(position.customer_piva_raw)
        if not inv_piva:
            raise HTTPException(
                status_code=400,
                detail="La fattura non riporta una P.IVA valida da assegnare",
            )

        cust_piva = validate_piva(customer.partita_iva)
        if cust_piva:
            if cust_piva == inv_piva:
                # Già allineati: niente da fare.
                return {
                    "ok": True,
                    "customer_id": customer.id,
                    "partita_iva": customer.partita_iva,
                }
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Il cliente '{customer.ragione_sociale}' ha già una P.IVA "
                    f"valida diversa ('{cust_piva}') da quella della fattura "
                    f"('{inv_piva}'): non la sovrascrivo. Verifica a mano quale "
                    f"sia corretta."
                ),
            )

        # ragione_sociale_normalized resta invariato: tocchiamo solo la P.IVA.
        customer.partita_iva = inv_piva
        session.commit()

        session.add(ActivityLog(
            action="audit_assign_piva",
            entity_type="customer",
            entity_id=customer.id,
            details={
                "invoice_number": position.invoice_number,
                "customer_id": customer.id,
                "piva": inv_piva,
            },
        ))
        session.commit()

        logger.info(
            f"P.IVA '{inv_piva}' assegnata al cliente {customer.id} "
            f"dalla fattura {position.invoice_number}"
        )
        return {"ok": True, "customer_id": customer.id, "partita_iva": inv_piva}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error assigning P.IVA to customer: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{position_id}/assign-name-to-customer")
async def assign_name_to_customer(
    position_id: int,
    confirm: bool = Query(False, description="Senza confirm: solo anteprima d'impatto, nessuna modifica"),
    session: Session = Depends(get_session),
):
    """Copia la ragione sociale della FATTURA sul CLIENTE abbinato.

    Via ② del menu "Risolvi" sulla riga discordante: stessa azienda, ma il
    profilo ha il nome vecchio (es. cambio ragione sociale mai recepito in
    anagrafica). Si aggiorna il CLIENTE dal documento — MAI il contrario:
    customer_name_raw è la prova documentale, toccarlo renderebbe la
    verifica circolare.

    Guardie (simmetriche ad assign-piva-to-customer):
    - 404 se la fattura o il cliente non esistono;
    - 400 se la fattura non è abbinata o non ha un nome destinatario;
    - 409 se le P.IVA confliggono (entrambe checksum-valide e diverse =
      entità diverse: il rinomino nasconderebbe un mis-abbinamento — la via
      giusta è Riassegna);
    - no-op 200 se il nome è già identico (nessun lock inutile).

    Flusso preview→confirm: la prima chiamata (confirm=false) NON applica e
    ritorna l'impatto — quante ALTRE fatture di questo cliente diventerebbero
    discordanti col nuovo nome (verify ricalcolato su un cliente simulato).
    Con confirm=true applica: nome + normalized ricalcolata col normalizzatore
    canonico + ragione_sociale_locked=True (il sync Shopify non deve poter
    annullare la bonifica al giro successivo).
    """
    from types import SimpleNamespace
    from backend.engine.matching import piva_contradiction
    from backend.engine.normalizer import normalize_ragione_sociale
    from backend.engine.verify import verify_invoice_customer

    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")
        if not position.customer_id:
            raise HTTPException(
                status_code=400,
                detail="La fattura non è abbinata a nessun cliente",
            )

        customer = session.query(Customer).filter(
            Customer.id == position.customer_id
        ).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Cliente non trovato")

        new_name = (position.customer_name_raw or "").strip()
        if not new_name:
            raise HTTPException(
                status_code=400,
                detail="La fattura non riporta un nome destinatario da assegnare",
            )

        # P.IVA in contraddizione (entrambe checksum-valide e diverse) =
        # entità diverse: rinominare il cliente nasconderebbe il vero
        # problema (fattura abbinata al cliente sbagliato).
        if piva_contradiction(position, customer):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"La P.IVA della fattura ('{position.customer_piva_raw}') e "
                    f"quella del cliente '{customer.ragione_sociale}' "
                    f"('{customer.partita_iva}') sono entrambe valide e DIVERSE: "
                    f"sono entità diverse, rinominare il profilo nasconderebbe un "
                    f"abbinamento sbagliato. Usa 'È di un altro cliente' per "
                    f"riassegnare la fattura."
                ),
            )

        old_name = (customer.ragione_sociale or "").strip()
        if new_name == old_name:
            # Già allineati: niente da fare, e nessun lock inutile.
            return {
                "ok": True,
                "applied": False,
                "already_aligned": True,
                "customer_id": customer.id,
                "old_name": old_name,
                "new_name": new_name,
                "impact": {"would_become_discordant": 0, "invoices": []},
            }

        # ── Impatto: quante ALTRE fatture (non pagate) di questo cliente
        # DIVENTEREBBERO discordanti col nuovo nome. Si ricalcola verify su
        # un cliente "simulato" col nuovo nome (stessa P.IVA): contano solo
        # i passaggi a 'bad' — le già discordanti non sono un peggioramento.
        simulated = SimpleNamespace(
            ragione_sociale=new_name,
            partita_iva=customer.partita_iva,
        )
        siblings = (
            session.query(Invoice)
            .filter(
                Invoice.customer_id == customer.id,
                Invoice.id != position.id,
                Invoice.status != "paid",
            )
            .all()
        )
        impacted = []
        for sib in siblings:
            before = verify_invoice_customer(sib, customer)["verdict"]
            after = verify_invoice_customer(sib, simulated)["verdict"]
            if after == "bad" and before != "bad":
                impacted.append({
                    "invoice_id": sib.id,
                    "invoice_number": sib.invoice_number,
                    "amount_due": float(sib.amount_due),
                })

        impact = {
            "would_become_discordant": len(impacted),
            "invoices": impacted,
        }

        if not confirm:
            # Anteprima: NESSUNA modifica.
            return {
                "ok": True,
                "applied": False,
                "customer_id": customer.id,
                "old_name": old_name,
                "new_name": new_name,
                "impact": impact,
            }

        customer.ragione_sociale = new_name
        customer.ragione_sociale_normalized = normalize_ragione_sociale(new_name)
        customer.ragione_sociale_locked = True
        session.commit()

        session.add(ActivityLog(
            action="audit_assign_name",
            entity_type="customer",
            entity_id=customer.id,
            details={
                "invoice_id": position.id,
                "invoice_number": position.invoice_number,
                "customer_id": customer.id,
                "old_name": old_name,
                "new_name": new_name,
                "would_become_discordant": len(impacted),
            },
        ))
        session.commit()

        logger.info(
            f"Ragione sociale del cliente {customer.id} aggiornata da "
            f"'{old_name}' a '{new_name}' dalla fattura "
            f"{position.invoice_number} (lock anti-sync attivo)"
        )
        return {
            "ok": True,
            "applied": True,
            "customer_id": customer.id,
            "old_name": old_name,
            "new_name": new_name,
            "locked": True,
            "impact": impact,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error assigning name to customer: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{position_id}/mark-reviewed")
async def mark_reviewed(position_id: int, session: Session = Depends(get_session)):
    """Segna la fattura come 'verificata a mano' nell'audit abbinamenti.

    L'operatore ha controllato di persona questo abbinamento dubbio/critico
    e lo considera corretto: da qui in poi non compare più tra i problemi
    dell'audit (salvo include_reviewed=true).
    """
    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")

        position.audit_reviewed_at = datetime.utcnow()
        session.commit()

        session.add(ActivityLog(
            action="audit_marked_reviewed",
            entity_type="invoice",
            entity_id=position_id,
            details={"invoice_number": position.invoice_number},
        ))
        session.commit()

        return {"ok": True, "invoice_id": position.id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error marking invoice reviewed: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{position_id}/unmark-reviewed")
async def unmark_reviewed(position_id: int, session: Session = Depends(get_session)):
    """Annulla la verifica manuale: la fattura torna tra i problemi dell'audit."""
    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")

        position.audit_reviewed_at = None
        session.commit()

        session.add(ActivityLog(
            action="audit_unmarked_reviewed",
            entity_type="invoice",
            entity_id=position_id,
            details={"invoice_number": position.invoice_number},
        ))
        session.commit()

        return {"ok": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unmarking invoice reviewed: {e}", exc_info=True)
        session.rollback()
        raise


@router.get("/{position_id}")
async def get_position_detail(position_id: int, session: Session = Depends(get_session)):
    """Get detailed information for a single position."""
    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")

        return {
            "id": position.id,
            "invoice_number": position.invoice_number,
            "amount": float(position.amount),
            "amount_due": float(position.amount_due),
            "issue_date": position.issue_date.isoformat() if position.issue_date else None,
            "due_date": position.due_date.isoformat() if position.due_date else None,
            "due_date_source": position.due_date_source or ("assumed" if position.due_date else None),
            "days_overdue": position.days_overdue,
            "status": position.status,
            "source_platform": position.source_platform,
            "match_method": position.match_method,
            "match_score": position.match_score,
            "customer": {
                "id": position.customer.id if position.customer else None,
                "ragione_sociale": position.customer.ragione_sociale if position.customer else position.customer_name_raw,
                "partita_iva": position.customer.partita_iva if position.customer else position.customer_piva_raw,
                "phone": position.customer.phone if position.customer else None,
                "email": position.customer.email if position.customer else None,
                "excluded": position.customer.excluded if position.customer else None,
            } if position.customer else None,
            "created_at": position.created_at.isoformat(),
            "updated_at": position.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching position detail: {e}", exc_info=True)
        raise


@router.put("/{position_id}/status")
async def update_position_status(
    position_id: int,
    new_status: str,
    session: Session = Depends(get_session),
):
    """Update the status of a position."""
    valid_statuses = ["open", "contacted", "promised", "disputed", "escalated"]
    # "paid" rimosso: lo stato pagamento arriva solo dal sync/refresh automatico

    if new_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
        )

    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")

        old_status = position.status
        position.status = new_status
        session.commit()

        # Log activity
        activity = ActivityLog(
            action="status_change",
            entity_type="invoice",
            entity_id=position_id,
            details={
                "old_status": old_status,
                "new_status": new_status,
                "invoice_number": position.invoice_number,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Position {position_id} status changed from {old_status} to {new_status}")

        return {
            "id": position.id,
            "status": position.status,
            "updated_at": position.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating position status: {e}", exc_info=True)
        session.rollback()
        raise


@router.put("/{position_id}/reassign")
async def reassign_position(
    position_id: int,
    new_customer_id: int = Query(..., description="ID of the customer to reassign this invoice to"),
    force: bool = Query(False, description="Force reassignment even if P.IVA conflict detected"),
    session: Session = Depends(get_session),
):
    """Reassign an invoice to a different customer.

    REGOLA P.IVA: Se la fattura ha una P.IVA (customer_piva_raw) e il cliente
    di destinazione ha una P.IVA diversa, la riassegnazione viene bloccata.
    Clienti diversi per P.IVA = entità diverse. Usare force=true solo se si è
    certi che la P.IVA sulla fattura sia errata.
    """
    try:
        position = session.query(Invoice).filter(Invoice.id == position_id).first()
        if not position:
            raise HTTPException(status_code=404, detail="Position not found")

        new_customer = session.query(Customer).filter(Customer.id == new_customer_id).first()
        if not new_customer:
            raise HTTPException(status_code=404, detail="Target customer not found")

        # ── REGOLA P.IVA IMPRESCINDIBILE ──
        # Se la fattura ha P.IVA e il cliente destinazione ha una P.IVA DIVERSA,
        # bloccare la riassegnazione: P.IVA diverse = entità diverse.
        # Confronto su P.IVA NORMALIZZATE ('IT12345678901' e '12345678901'
        # sono la stessa entità, non un conflitto — prima era un falso 409)
        # ma NON validate: una P.IVA malformata deve continuare a bloccare,
        # non a bypassare il 409 (questo è un blocco di sicurezza manuale,
        # non un match automatico).
        from backend.engine.piva import normalize_piva
        inv_piva = normalize_piva(position.customer_piva_raw)
        cust_piva = normalize_piva(new_customer.partita_iva)

        if inv_piva and cust_piva and inv_piva != cust_piva:
            if not force:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"CONFLITTO P.IVA: La fattura {position.invoice_number} ha P.IVA "
                        f"'{inv_piva}' ma il cliente '{new_customer.ragione_sociale}' ha P.IVA "
                        f"'{cust_piva}'. P.IVA diverse = entità diverse. "
                        f"Riassegnazione bloccata. Usa force=true solo se la P.IVA "
                        f"sulla fattura è errata."
                    )
                )
            logger.warning(
                f"FORCED reassign with P.IVA conflict: invoice {position.invoice_number} "
                f"P.IVA '{inv_piva}' → customer '{new_customer.ragione_sociale}' "
                f"P.IVA '{cust_piva}'"
            )

        # Se il cliente destinazione ha P.IVA e la fattura no, logga un avviso
        if cust_piva and not inv_piva:
            logger.info(
                f"Reassign: invoice {position.invoice_number} has no P.IVA, "
                f"assigning to customer '{new_customer.ragione_sociale}' "
                f"with P.IVA '{cust_piva}'"
            )

        old_customer_id = position.customer_id
        old_customer_name = position.customer.ragione_sociale if position.customer else None
        position.customer_id = new_customer_id
        # La pratica del vecchio cliente non può tenersi la fattura: il
        # lifecycle la aggancerà alla pratica del cliente nuovo.
        position.case_id = None
        position.match_method = "manual"
        position.match_score = None
        position.suggested_customer_id = None
        position.suggested_method = None
        position.suggested_score = None
        session.commit()

        activity = ActivityLog(
            action="reassign",
            entity_type="invoice",
            entity_id=position_id,
            details={
                "invoice_number": position.invoice_number,
                "old_customer_id": old_customer_id,
                "old_customer_name": old_customer_name,
                "new_customer_id": new_customer_id,
                "new_customer_name": new_customer.ragione_sociale,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Position {position_id} reassigned from customer {old_customer_id} to {new_customer_id}")

        return {
            "id": position.id,
            "invoice_number": position.invoice_number,
            "old_customer_id": old_customer_id,
            "old_customer_name": old_customer_name,
            "new_customer_id": new_customer_id,
            "new_customer_name": new_customer.ragione_sociale,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reassigning position: {e}", exc_info=True)
        session.rollback()
        raise
