"""Stadio di avanzamento PER FATTURA e gruppi per stadio (Fase 4).

Il soggetto del recupero è la fattura: ogni fattura ha il SUO stadio, derivato
(mai scritto a mano) da ciò che le è successo — solleciti ricevuti (tabella di
join), consegna al legale, assegno in mano / insoluto. Le fatture allo stesso
stadio formano un GRUPPO: la scheda cliente racconta "questo gruppo ha seguito
questo flusso e si trova qui", e le azioni si fanno sul gruppo.

Definizione UNICA: la usano il dettaglio cliente (colonna "Avanzamento" e
sezione "Azioni di recupero") e chiunque debba dire "a che punto è questa
fattura". Niente qui tocca cifre in euro.
"""
from typing import Dict, Any, List, Optional

from sqlalchemy.orm import Session

from backend.database import Customer, RecoveryAction, RecoveryCase
from backend.engine.cases import CONTACT_TYPES, _has_unlinked_contacts
from backend.engine.action_invoices import (
    per_invoice_sollecito_stats, delivered_invoice_ids,
)
from backend.engine.overdue import is_in_incasso, is_suspect_bounce

# Ordine di presentazione: prima ciò che chiede attenzione.
STAGE_ORDER = ("insoluto", "sospetto", "second", "first", "none", "lawyer", "in_incasso")

STAGE_LABELS = {
    "none": "Nessun sollecito",
    "first": "1° sollecito fatto",
    "second": "2° sollecito fatto",
    "lawyer": "Consegnata all'avvocato",
    "in_incasso": "In incasso (assegno)",
    "insoluto": "Assegno insoluto",
    "sospetto": "Riaperta dopo assegno: verificare",
}

# Tono del prossimo messaggio per il gruppo: 'first' = cordiale (mai
# sollecitata), 'second' = perentorio (già sollecitata). None = non si
# sollecita (dal legale / in incasso).
STAGE_TONE = {
    "none": "first", "first": "second", "second": "second",
    "insoluto": "second", "sospetto": "second",
    "lawyer": None, "in_incasso": None,
}

ACTION_LABELS = {
    "first_contact": "1° sollecito",
    "second_contact": "2° sollecito",
    "lawyer": "Passaggio all'avvocato",
    "wait": "Attesa",
    "note": "Nota",
    "archive": "Archiviazione",
}


def invoice_stage(inv, count: int, inherited: int, delivered_ids, force_second: bool) -> str:
    """Stadio di UNA fattura (scaduta e non pagata) — definizione unica."""
    if inv.status != "paid" and getattr(inv, "bounced_at", None) is not None:
        return "insoluto"
    if is_suspect_bounce(inv):
        return "sospetto"
    if is_in_incasso(inv):
        return "in_incasso"
    if inv.id in delivered_ids:
        return "lawyer"
    eff = int(count or 0) + int(inherited or 0) + (1 if force_second else 0)
    if eff >= 2:
        return "second"
    if eff == 1:
        return "first"
    return "none"


def _when(a: RecoveryAction):
    return a.completed_at or a.created_at


def _action_row(a: RecoveryAction, cited_in_group: Optional[int], cited_total: Optional[int], legacy_all: bool) -> Dict[str, Any]:
    return {
        "id": a.id,
        "action_type": a.action_type,
        "label": ACTION_LABELS.get(a.action_type, a.action_type),
        "completed_at": a.completed_at.isoformat() if a.completed_at else None,
        "scheduled_date": a.scheduled_date.isoformat() if a.scheduled_date else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "channel": a.channel,
        "outcome": a.outcome,
        "notes": a.notes,
        "cancelled": bool(a.cancelled),
        "cited_in_group": cited_in_group,
        "cited_total": cited_total,
        "legacy_all": legacy_all,
    }


def build_stage_groups(session: Session, customer: Customer, case: Optional[RecoveryCase]) -> Dict[str, Any]:
    """{"invoices": {inv_id: stage}, "groups": [...], "client_actions": [...],
    "pending": [...]}.

    - groups: uno per stadio presente, in STAGE_ORDER, con le fatture, il totale,
      il tono e la STORIA del gruppo (le azioni che citano almeno una sua
      fattura, in ordine cronologico, con "k di N fatture citate" se l'azione
      copriva anche fatture oggi in altri stadi; un'azione legacy senza fatture
      citate valeva "tutte le scadute all'epoca" e compare in ogni gruppo).
    - client_actions: azioni sul CLIENTE non legate a fatture (attese,
      archiviazioni, note senza fatture).
    - pending: todo pendenti della pratica (prossime azioni).
    """
    overdue = [
        inv for inv in customer.invoices
        if inv.status != "paid" and (inv.days_overdue or 0) > 0
    ]
    ids = [i.id for i in overdue]
    stats = per_invoice_sollecito_stats(session, ids) if ids else {}
    delivered = delivered_invoice_ids(session, case, overdue) if (case and overdue) else set()
    inherited = (case.inherited_contacts or 0) if case else 0
    force_second = _has_unlinked_contacts(session, case) if case else False

    stage_of: Dict[int, str] = {}
    for inv in overdue:
        stage_of[inv.id] = invoice_stage(
            inv, stats.get(inv.id, {}).get("count", 0), inherited, delivered, force_second
        )

    actions = (
        session.query(RecoveryAction)
        .filter(RecoveryAction.customer_id == customer.id)
        .order_by(RecoveryAction.created_at.asc())
        .all()
    )
    done = [a for a in actions if not a.cancelled and a.completed_at is not None or (a.action_type == "note" and not a.cancelled)]
    pending = [a for a in actions if not a.cancelled and a.completed_at is None and a.action_type != "note"]

    groups: List[Dict[str, Any]] = []
    for stage in STAGE_ORDER:
        members = [inv for inv in overdue if stage_of[inv.id] == stage]
        if not members:
            continue
        member_ids = {i.id for i in members}
        rows = []
        for a in done:
            cited = set(a.invoice_ids or [])
            if a.invoice_ids is None and a.action_type in CONTACT_TYPES + ("lawyer",):
                rows.append(_action_row(a, len(member_ids), None, True))
            elif cited & member_ids:
                rows.append(_action_row(a, len(cited & member_ids), len(cited), False))
        rows.sort(key=lambda r: r["completed_at"] or r["created_at"] or "")
        groups.append({
            "stage": stage,
            "label": STAGE_LABELS[stage],
            "tone": STAGE_TONE[stage],
            "invoice_ids": sorted(member_ids),
            "invoices": [{
                "id": i.id, "invoice_number": i.invoice_number,
                "amount_due": float(i.amount_due or 0),
                "due_date": i.due_date.isoformat() if i.due_date else None,
                "days_overdue": int(i.days_overdue or 0),
                "sollecito_count": stats.get(i.id, {}).get("count", 0),
            } for i in sorted(members, key=lambda x: (x.due_date or x.issue_date or x.created_at, x.id))],
            "total": round(float(sum(i.amount_due or 0 for i in members)), 2),
            "actions": rows,
        })

    client_actions = [
        _action_row(a, None, None, False) for a in done
        if not (a.invoice_ids or []) and not (a.invoice_ids is None and a.action_type in CONTACT_TYPES + ("lawyer",))
    ]
    pending_rows = [_action_row(a, None, None, False) for a in pending]
    return {
        "invoices": stage_of,
        "groups": groups,
        "client_actions": client_actions,
        "pending": pending_rows,
    }
