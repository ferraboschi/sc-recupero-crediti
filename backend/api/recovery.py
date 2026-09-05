"""Recovery workflow API endpoints.

Tutte le azioni sono agganciate alla PRATICA aperta del cliente
(RecoveryCase): numerazione e tono dei solleciti contano le azioni della
pratica, e alla chiusura (saldo) il ciclo riparte pulito.
"""

import os
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from io import BytesIO

from backend.config import config
from backend.database import get_session, Customer, Invoice, RecoveryAction, ActivityLog
from backend.engine.cases import (
    CONTACT_TYPES, business_day_start, contact_count, ensure_open_case,
    get_open_case, is_overdue_unpaid, schedule_next_action, close_case,
    _refresh_customer_status, _has_unlinked_contacts,
)
from backend.engine.action_invoices import (
    set_action_invoices, delivered_invoice_ids, per_invoice_sollecito_stats,
)

logger = logging.getLogger(__name__)
router = APIRouter()

WHATSAPP_CHANNELS = ("whatsapp_copy", "whatsapp_link")


# --- Pydantic models ---

class ActionCreate(BaseModel):
    action_type: str  # first_contact / second_contact / lawyer / archive / wait / note
    scheduled_date: Optional[str] = None  # YYYY-MM-DD
    notes: Optional[str] = None


class SollecitoCreate(BaseModel):
    invoice_ids: List[int] = []
    channel: str = "whatsapp_copy"  # whatsapp_copy / whatsapp_link


def _serialize_action(a: RecoveryAction) -> dict:
    return {
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


# --- Utilizzo: registro giorno-per-giorno del lavoro reale (sola lettura) ---

def _italian_day(dt: datetime) -> date:
    """Giorno di calendario ITALIANO di un timestamp salvato in UTC naive.

    created_at e' scritto con datetime.utcnow() (UTC, senza tzinfo): il server
    gira in UTC, quindi date() nudo sposterebbe il confine di giornata a
    mezzanotte UTC (l'1-2 di notte italiane). Coerente con business_day_start.
    """
    tz = ZoneInfo(config.TIMEZONE)
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).date()


@router.get("/utilizzo")
def get_utilizzo(session: Session = Depends(get_session)):
    """Registro d'utilizzo: quante AZIONI di recupero, su quanti ACCOUNT, al giorno.

    Sola lettura, nessuna mutazione. E' il monitor per vedere quanto la persona
    ha davvero usato il sistema: un'azione = una RecoveryAction con un `channel`
    valorizzato (il flusso Copia Messaggio / WhatsApp e' l'unico che scrive un
    channel: e' il segnale NON falsificabile del lavoro reale) e non annullata.
    La data e' il giorno di calendario italiano di `created_at`.

    - `per_giorno`: il conteggio giornaliero {data, azioni (n. solleciti),
      account (clienti DISTINTI)}, ordinato per data desc. E' la vista audit
      principale (la timeline dell'uso).
    - `eventi`: una riga per sollecito {data, customer_id, cliente, action_type,
      channel}, ordinata per data desc, poi cliente. E' il dettaglio per giorno.

    Dall'inizio dei dati: nessun floor sulla data. Una sola query aggregata
    (join a Customer per il nome), il resto e' una fold in memoria — il conteggio
    query e' costante indipendentemente dal volume dei dati (niente N+1).
    """
    rows = (
        session.query(
            RecoveryAction.customer_id,
            RecoveryAction.action_type,
            RecoveryAction.channel,
            RecoveryAction.created_at,
            Customer.ragione_sociale,
        )
        .join(Customer, Customer.id == RecoveryAction.customer_id)
        .filter(RecoveryAction.channel.isnot(None))
        .filter(RecoveryAction.cancelled.isnot(True))
        .all()
    )

    eventi = []
    per_giorno: dict = {}  # data (str) -> {"customers": set, "azioni": int}
    for customer_id, action_type, channel, created_at, ragione_sociale in rows:
        if created_at is None:
            continue
        data_str = _italian_day(created_at).isoformat()
        eventi.append({
            "data": data_str,
            "customer_id": customer_id,
            "cliente": ragione_sociale,
            "action_type": action_type,
            "channel": channel,
        })
        bucket = per_giorno.setdefault(data_str, {"customers": set(), "azioni": 0})
        bucket["customers"].add(customer_id)
        bucket["azioni"] += 1

    # Ordina eventi per data desc, poi cliente asc (sort stabile: prima la
    # chiave secondaria, poi la primaria).
    eventi.sort(key=lambda e: (e["cliente"] or "").casefold())
    eventi.sort(key=lambda e: e["data"], reverse=True)

    per_giorno_list = [
        {
            "data": d,
            "azioni": v["azioni"],
            "account": len(v["customers"]),
        }
        for d, v in per_giorno.items()
    ]
    per_giorno_list.sort(key=lambda x: x["data"], reverse=True)

    return {"eventi": eventi, "per_giorno": per_giorno_list}


# --- Registrazione solleciti (Copia Messaggio / WhatsApp) ---

@router.post("/customers/{customer_id}/solleciti")
def register_sollecito(
    customer_id: int,
    body: SollecitoCreate,
    session: Session = Depends(get_session),
):
    """Registra un sollecito inviato via WhatsApp (Copia Messaggio).

    - Crea un'azione di contatto COMPLETATA ora, agganciata alla pratica
      aperta (creata/riaperta se serve), con canale e fatture citate.
    - Un eventuale todo di contatto pendente viene soppiantato (il todo
      legale invece resta: un sollecito WhatsApp non sostituisce l'avvocato).
    - Dedup per giornata solare: un secondo copy nello stesso giorno
      aggiorna le fatture citate e ritorna lo stesso numero di sollecito.
    - Cliente senza fatture scadute → 200 {registered: false}: il copy è
      legittimo (promemoria di cortesia), semplicemente non è un sollecito.
    """
    if body.channel not in WHATSAPP_CHANNELS:
        raise HTTPException(status_code=400, detail=f"Canale non valido: {body.channel}")

    try:
        # Lock del cliente per rendere atomico dedup-check + insert
        # (su PostgreSQL; SQLite serializza comunque le scritture).
        customer = (
            session.query(Customer)
            .filter(Customer.id == customer_id)
            .with_for_update()
            .first()
        )
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")
        if customer.excluded:
            raise HTTPException(status_code=409, detail="Cliente escluso dal recupero crediti")

        overdue = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]
        if not overdue:
            return {
                "registered": False,
                "reason": "no_overdue",
                "message": "Nessuna fattura scaduta: messaggio copiato ma sollecito non registrato",
            }

        # Le fatture citate devono essere DI questo cliente: un id estraneo
        # significa che il chiamante sta guardando un altro cliente (race del
        # frontend fra due fetch) — registrarlo inquinerebbe la pratica con
        # fatture altrui e falserebbe il tono del prossimo sollecito.
        # Il controllo sta PRIMA del dedup: anche il merge giornaliero
        # (existing_today.invoice_ids) è una scrittura da proteggere.
        own_invoice_ids = {inv.id for inv in customer.invoices}
        unknown = [i for i in body.invoice_ids if i not in own_invoice_ids]
        not_workable = [
            inv.id for inv in customer.invoices
            if inv.id in set(body.invoice_ids) and not is_overdue_unpaid(inv)
        ]
        if not_workable:
            raise HTTPException(
                status_code=400,
                detail=f"Fatture non sollecitabili (pagate, contestate o in incasso): {not_workable}",
            )
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Fatture non appartenenti al cliente {customer.id}: {unknown}",
            )

        # Set di fatture CITATE dal sollecito. Un copy senza selezione esplicita
        # (invoice_ids vuoto) "sollecita l'intero debito": lo si attribuisce a
        # TUTTE le fatture scadute correnti — stessa semantica del backfill
        # legacy — così il conteggio per-fattura e il dossier non mostrano "0
        # solleciti" per una fattura davvero sollecitata. Non tocca numerazione
        # né tono (contact_count resta invariato).
        cited_ids = (
            sorted(set(body.invoice_ids))
            if body.invoice_ids
            else sorted(inv.id for inv in overdue)
        )

        case = ensure_open_case(session, customer)
        today = date.today()
        now = datetime.utcnow()

        # Numerazione PER-FATTURA: il numero del sollecito è quello delle
        # fatture citate — stadio più basso fra loro + 1, così la tonalità più
        # gentile vale per il gruppo (una fattura nuova riceve il SUO 1°
        # sollecito anche se il cliente è già al 2° su altre). Il frontend
        # raggruppa la selezione per stadio: un messaggio per stadio.
        # Contatti EREDITATI da una pratica archiviata/legale precedente: il
        # tono non riparte mai cordiale con quel cliente (regola owner) → si
        # sommano allo stadio di ogni fattura.
        inherited = case.inherited_contacts or 0

        # "Oggi" = giornata lavorativa italiana (il server gira in UTC).
        start_of_day = business_day_start()
        todays = session.query(RecoveryAction).filter(
            RecoveryAction.case_id == case.id,
            RecoveryAction.channel.in_(WHATSAPP_CHANNELS),
            RecoveryAction.completed_at >= start_of_day,
            RecoveryAction.cancelled.isnot(True),
        ).order_by(RecoveryAction.completed_at.desc()).all()
        today_ids = set()
        for a in todays:
            today_ids |= set(a.invoice_ids or [])

        # 1) STESSA FATTURA, stesso giorno = stesso sollecito: un ri-copy (refuso)
        #    non è un nuovo sollecito e non fa salire la fattura di stadio. Le
        #    fatture già citate oggi si SEPARANO dal resto (mai unite a un'azione
        #    di un altro stadio).
        already = [i for i in cited_ids if i in today_ids]
        rest = [i for i in cited_ids if i not in today_ids]
        if not rest:
            existing = next(a for a in todays if set(a.invoice_ids or []) & set(already))
            prev_all = per_invoice_sollecito_stats(session, existing.invoice_ids or [])
            n_existing = min((prev_all.get(i, {}).get("count", 1) for i in (existing.invoice_ids or [])), default=1) + inherited
            if _has_unlinked_contacts(session, case):
                n_existing = max(n_existing, contact_count(session, case))
            return {
                "registered": True,
                "already_registered_today": True,
                "action_id": existing.id,
                "sollecito_n": n_existing,
                "next_action": {
                    "action_type": customer.next_action_type,
                    "scheduled_date": customer.next_action_date.isoformat() if customer.next_action_date else None,
                },
            }
        cited_ids = rest

        # Numerazione PER-FATTURA: il numero del sollecito è quello delle
        # fatture citate — stadio più basso fra loro + ereditati + 1, così la
        # tonalità più gentile vale per il gruppo (una fattura nuova riceve il
        # SUO 1° sollecito anche se il cliente è già al 2° su altre). Il
        # frontend raggruppa la selezione per stadio: un messaggio per stadio.
        prev = per_invoice_sollecito_stats(session, cited_ids)
        min_prev = min((prev.get(i, {}).get("count", 0) for i in cited_ids), default=0)
        n = min_prev + inherited + 1
        # Rete legacy: contatti completati SENZA fatture collegate (storico
        # pre-tabella, o registrati a mano nella finestra pre-backfill) → non
        # si ricomincia cordiale: vale il contatore di pratica (vecchia regola).
        if _has_unlinked_contacts(session, case):
            n = max(n, contact_count(session, case) + 1)
        action_type = "first_contact" if n == 1 else "second_contact"

        # 2) stesso STADIO oggi → si unisce a quel sollecito. Due stadi diversi
        #    nello stesso giorno (1° per la nuova, 2° per le vecchie) sono DUE
        #    solleciti distinti.
        existing_today = next((a for a in todays if a.action_type == action_type), None)

        if existing_today:
            merged = sorted(set((existing_today.invoice_ids or []) + cited_ids))
            existing_today.invoice_ids = merged
            # Dual-write della tabella di join: le fatture appena aggiunte
            # dal secondo copy odierno ereditano lo stesso sollecito.
            set_action_invoices(session, existing_today.id, merged)
            session.flush()
            _refresh_customer_status(session, customer, case)
            session.add(ActivityLog(
                action="sollecito_merge", entity_type="recovery_action", entity_id=existing_today.id,
                details={"customer": customer.ragione_sociale, "case_id": case.id,
                         "added_invoice_ids": cited_ids, "sollecito_n": n},
            ))
            session.commit()
            return {
                "registered": True,
                "already_registered_today": True,
                "action_id": existing_today.id,
                "sollecito_n": n,
                "next_action": {
                    "action_type": customer.next_action_type,
                    "scheduled_date": customer.next_action_date.isoformat() if customer.next_action_date else None,
                },
            }

        # Soppianta i todo di contatto pendenti (NON quelli legali)
        pending_contacts = session.query(RecoveryAction).filter(
            RecoveryAction.case_id == case.id,
            RecoveryAction.action_type.in_(CONTACT_TYPES),
            RecoveryAction.completed_at.is_(None),
            RecoveryAction.cancelled.isnot(True),
        ).all()

        channel_label = "Copia Messaggio" if body.channel == "whatsapp_copy" else "link WhatsApp"

        action = RecoveryAction(
            customer_id=customer.id,
            case_id=case.id,
            action_type=action_type,
            scheduled_date=today,
            completed_at=now,
            outcome="contacted",
            channel=body.channel,
            invoice_ids=cited_ids,
            notes=f"Sollecito n. {n} via {channel_label} ({len(cited_ids)} fatture)",
        )
        session.add(action)
        session.flush()

        # Dual-write della tabella di join (numerazione per-fattura autorevole).
        set_action_invoices(session, action.id, cited_ids)
        # flush esplicito: le righe di join devono essere visibili alle query
        # che seguono (schedule_next_action, rollup) anche senza autoflush.
        session.flush()

        for p in pending_contacts:
            p.cancelled = True
            p.cancelled_reason = f"superseded_by_sollecito:{action.id}"

        next_action = schedule_next_action(session, customer, case, n, superseded_by=action.id)
        # Stato cliente = rollup PER-FATTURA (il peggiore fra le scadute): un
        # 1° sollecito sulla fattura nuova NON retrocede un cliente che è già
        # al 2° sulle vecchie. flush prima: il refresh legge i todo dal DB.
        session.flush()
        _refresh_customer_status(session, customer, case)

        session.add(ActivityLog(
            action="sollecito",
            entity_type="recovery_action",
            entity_id=action.id,
            details={
                "customer": customer.ragione_sociale,
                "case_id": case.id,
                "sollecito_n": n,
                "channel": body.channel,
                "invoice_ids": cited_ids,
            },
        ))
        session.commit()

        return {
            "registered": True,
            "action_id": action.id,
            "sollecito_n": n,
            "superseded_pending": len(pending_contacts),
            "next_action": {
                "action_type": next_action.action_type,
                "scheduled_date": next_action.scheduled_date.isoformat(),
            } if next_action else {
                "action_type": customer.next_action_type,
                "scheduled_date": customer.next_action_date.isoformat() if customer.next_action_date else None,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering sollecito: {e}", exc_info=True)
        session.rollback()
        raise


@router.delete("/customers/{customer_id}/solleciti/{action_id}")
def undo_sollecito(
    customer_id: int,
    action_id: int,
    session: Session = Depends(get_session),
):
    """Annulla un sollecito registrato per errore (solo lo stesso giorno).

    Ripristina i todo soppiantati e rimuove la prossima azione
    auto-pianificata; la numerazione torna com'era.
    """
    try:
        action = session.query(RecoveryAction).filter(
            RecoveryAction.id == action_id,
            RecoveryAction.customer_id == customer_id,
        ).first()
        if not action:
            raise HTTPException(status_code=404, detail="Sollecito non trovato")
        if action.channel not in WHATSAPP_CHANNELS or action.cancelled:
            raise HTTPException(status_code=400, detail="Azione non annullabile")
        if not action.completed_at or action.completed_at < business_day_start():
            raise HTTPException(status_code=400, detail="Annullabile solo lo stesso giorno")

        action.cancelled = True
        action.cancelled_reason = "undo"

        # Ripristina i todo soppiantati da QUESTO sollecito
        superseded = session.query(RecoveryAction).filter(
            RecoveryAction.case_id == action.case_id,
            RecoveryAction.cancelled_reason == f"superseded_by_sollecito:{action.id}",
        ).all()
        for p in superseded:
            p.cancelled = False
            p.cancelled_reason = None

        # Rimuove la prossima azione auto-pianificata da questo sollecito
        auto_next = session.query(RecoveryAction).filter(
            RecoveryAction.case_id == action.case_id,
            RecoveryAction.completed_at.is_(None),
            RecoveryAction.cancelled.isnot(True),
            RecoveryAction.created_at >= action.created_at,
            RecoveryAction.notes.like("Auto-pianificata dopo il%"),
        ).all()
        for nxt in auto_next:
            session.delete(nxt)

        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        case = get_open_case(session, customer_id)
        if customer and case:
            session.flush()  # l'annullamento e la delete devono essere visibili al refresh
            _refresh_customer_status(session, customer, case)

        session.add(ActivityLog(
            action="sollecito_undo",
            entity_type="recovery_action",
            entity_id=action.id,
            details={"customer_id": customer_id, "restored_pending": len(superseded)},
        ))
        session.commit()

        return {
            "undone": True,
            "restored_pending": len(superseded),
            "removed_next_actions": len(auto_next),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error undoing sollecito: {e}", exc_info=True)
        session.rollback()
        raise


# --- Customer recovery actions ---

@router.get("/customers/{customer_id}/actions")
def get_customer_actions(
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
            "actions": [_serialize_action(a) for a in actions],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer actions: {e}", exc_info=True)
        raise


@router.post("/customers/{customer_id}/actions")
def create_action(
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

        # Aggancio alla pratica aperta (creata se il cliente ha scadute).
        # Le note non c'entrano col ciclo di recupero e restano senza pratica.
        case = None
        if action.action_type != "note":
            overdue = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]
            if overdue and not customer.excluded:
                case = ensure_open_case(session, customer)

        # Guardia anti-doppione: un contatto pianificato quando la pratica ha
        # già un todo di contatto (o un sollecito WhatsApp registrato oggi)
        # duplicherebbe la progressione.
        if case and action.action_type in CONTACT_TYPES:
            pending_contact = session.query(RecoveryAction).filter(
                RecoveryAction.case_id == case.id,
                RecoveryAction.action_type.in_(CONTACT_TYPES),
                RecoveryAction.completed_at.is_(None),
                RecoveryAction.cancelled.isnot(True),
            ).first()
            if pending_contact:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Esiste già un contatto pianificato per il "
                        f"{pending_contact.scheduled_date.isoformat() if pending_contact.scheduled_date else 'N/D'}: "
                        f"modificane la data invece di crearne un altro"
                    ),
                )
            sollecito_today = session.query(RecoveryAction).filter(
                RecoveryAction.case_id == case.id,
                RecoveryAction.channel.in_(WHATSAPP_CHANNELS),
                RecoveryAction.completed_at >= business_day_start(),
                RecoveryAction.cancelled.isnot(True),
            ).first()
            if sollecito_today:
                raise HTTPException(
                    status_code=409,
                    detail="Sollecito già registrato oggi via WhatsApp (Copia Messaggio)",
                )

        # Create the action record
        new_action = RecoveryAction(
            customer_id=customer_id,
            case_id=case.id if case else None,
            action_type=action.action_type,
            scheduled_date=scheduled or today,
            notes=action.notes,
        )
        session.add(new_action)

        # Update customer recovery status and next action
        # Use user-provided scheduled_date if available, otherwise use defaults
        if action.action_type == "first_contact":
            customer.recovery_status = "first_contact"
            customer.next_action_date = scheduled or (today + timedelta(days=7))
            customer.next_action_type = "second_contact"
        elif action.action_type == "second_contact":
            customer.recovery_status = "second_contact"
            customer.next_action_date = scheduled or (today + timedelta(days=14))
            customer.next_action_type = "lawyer"
        elif action.action_type == "lawyer":
            customer.recovery_status = "lawyer"
            customer.next_action_date = scheduled or (today + timedelta(days=30))
            customer.next_action_type = "lawyer"  # Follow-up with lawyer
        elif action.action_type == "archive":
            customer.recovery_status = "archived"
            customer.next_action_date = None
            customer.next_action_type = None
            # Archiviare è un atto immediato (non un todo) e chiude la pratica
            new_action.completed_at = datetime.utcnow()
            if case:
                session.flush()
                close_case(session, case, "archived")
        elif action.action_type == "wait":
            customer.recovery_status = "waiting"
            customer.next_action_date = scheduled or (today + timedelta(days=30))
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
def complete_action(
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
        if action.cancelled:
            raise HTTPException(
                status_code=400,
                detail="Azione annullata: non può essere completata "
                       "(completarla riattiverebbe una progressione chiusa)",
            )
        if action.completed_at:
            raise HTTPException(status_code=400, detail="Azione già completata")

        action.completed_at = datetime.utcnow()
        if outcome:
            action.outcome = outcome
        if notes:
            action.notes = (action.notes or '') + (' | Esito: ' + notes if action.notes else notes)

        customer = session.query(Customer).filter(Customer.id == customer_id).first()

        # --- Auto-progression (unificata con l'endpoint solleciti) ---
        next_action = None
        if customer and action.action_type in CONTACT_TYPES + ("lawyer",):
            case = None
            if action.case_id and action.case and action.case.status == "open":
                case = action.case
            elif not customer.excluded:
                overdue = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]
                if overdue:
                    case = ensure_open_case(session, customer)
                    if not action.case_id:
                        action.case_id = case.id

            if case:
                if action.action_type in CONTACT_TYPES:
                    # Contatto registrato A MANO (telefonata/email) senza
                    # fatture citate: riguardava l'intero debito → cita le
                    # scadute del momento (esplicito, come il backfill), così
                    # il conteggio PER-FATTURA lo vede.
                    if action.invoice_ids is None:
                        cited = [inv.id for inv in customer.invoices if is_overdue_unpaid(inv)]
                        action.invoice_ids = cited
                        session.flush()
                        set_action_invoices(session, action.id, cited)
                    session.flush()  # righe di join visibili anche senza autoflush
                    # n PER-FATTURA, esattamente come register_sollecito (le
                    # fatture citate hanno già la riga di questo contatto):
                    # stadio più basso citato + ereditati; rete legacy.
                    ids = list(action.invoice_ids or [])
                    inherited = case.inherited_contacts or 0
                    if ids:
                        prev = per_invoice_sollecito_stats(session, ids)
                        n = min((prev.get(i, {}).get("count", 1) for i in ids), default=1) + inherited
                    else:
                        n = contact_count(session, case)
                    if _has_unlinked_contacts(session, case):
                        n = max(n, contact_count(session, case))
                    next_action = schedule_next_action(session, customer, case, n, superseded_by=action.id)
                    # Stato cliente = rollup PER-FATTURA (non il contatore di
                    # pratica): altrimenti il prossimo refresh lo cambierebbe.
                    session.flush()
                    _refresh_customer_status(session, customer, case)
                else:  # lawyer completato → consegna ESPLICITA + follow-up +30gg
                    # Le fatture consegnate si scrivono SEMPRE (invoice_ids +
                    # join): quelle scadute non ancora consegnate. Mai più
                    # dedotte a posteriori.
                    overdue_objs = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]
                    already = delivered_invoice_ids(session, case, overdue_objs)
                    # PRIMA chiusura legale della pratica = consegna di tutte
                    # le scadute. Se esiste già una consegna, questo è un
                    # FOLLOW-UP: non consegna nulla di nuovo ([] esplicito).
                    to_deliver = [] if already else [inv.id for inv in overdue_objs]
                    action.invoice_ids = to_deliver
                    session.flush()
                    set_action_invoices(session, action.id, to_deliver)
                    if all(inv.id in (already | set(to_deliver)) for inv in overdue_objs):
                        customer.recovery_status = "lawyer"
                    next_date = date.today() + timedelta(days=30)
                    next_action = RecoveryAction(
                        customer_id=customer_id,
                        case_id=case.id,
                        action_type="lawyer",
                        scheduled_date=next_date,
                        notes="Auto-pianificata: follow-up avvocato",
                    )
                    session.add(next_action)
                    customer.next_action_date = next_date
                    customer.next_action_type = "lawyer"
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


@router.patch("/customers/{customer_id}/actions/{action_id}/reschedule")
def reschedule_action(
    customer_id: int,
    action_id: int,
    new_date: str = Query(..., description="New scheduled date in YYYY-MM-DD format"),
    session: Session = Depends(get_session),
):
    """Update the scheduled_date of a recovery action and propagate to customer.

    Only allowed for contact-type actions (first_contact, second_contact, lawyer, wait).
    Updates both the action's scheduled_date AND the customer's next_action_date
    so that Dashboard and Attività reflect the change.
    """
    try:
        action = session.query(RecoveryAction).filter(
            RecoveryAction.id == action_id,
            RecoveryAction.customer_id == customer_id,
        ).first()

        if not action:
            raise HTTPException(status_code=404, detail="Azione non trovata")

        # Only allow rescheduling contact/lawyer/wait actions, not notes
        if action.action_type == "note":
            raise HTTPException(status_code=400, detail="Le note non hanno data pianificata modificabile")

        customer = session.query(Customer).get(customer_id)
        if not customer:
            raise HTTPException(status_code=404, detail="Cliente non trovato")

        from datetime import date as date_type
        parsed_date = date_type.fromisoformat(new_date)
        old_date = action.scheduled_date

        # Update the action's scheduled date
        action.scheduled_date = parsed_date

        # Propagate to customer's next_action_date
        # (only if this action is the one driving the current schedule)
        customer.next_action_date = parsed_date

        session.commit()

        logger.info(
            f"Rescheduled action {action_id} ({action.action_type}) for customer "
            f"{customer.ragione_sociale}: {old_date} → {parsed_date}"
        )

        return {
            "status": "ok",
            "action_id": action_id,
            "old_date": old_date.isoformat() if old_date else None,
            "new_date": parsed_date.isoformat(),
            "customer_next_action_date": customer.next_action_date.isoformat(),
        }

    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Data non valida: {new_date}. Formato: YYYY-MM-DD")
    except Exception as e:
        logger.error(f"Error rescheduling action: {e}", exc_info=True)
        session.rollback()
        raise


# --- PDF Riepilogativo ---

@router.get("/customers/{customer_id}/pdf-riepilogativo")
def generate_pdf_riepilogativo(
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
def generate_single_invoice_pdf(
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
def generate_selected_invoices_pdf(
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
    pdf.cell(0, 7, f"IBAN: {os.getenv('COMPANY_IBAN', 'IT44N0200801671000105175151')}", new_x="LMARGIN", new_y="NEXT")
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
    pdf.cell(0, 6, f"IBAN: {os.getenv('COMPANY_IBAN', 'IT44N0200801671000105175151')}",
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
def download_invoices_zip(
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
