"""Solleciti PER-FATTURA — la tabella di join `recovery_action_invoices`.

Il soggetto del recupero è la FATTURA: ogni fattura ha una vita propria
(emissione, scadenza, valore) e un numero di attività fatte SU DI SÉ. Fatture
diverse dello stesso cliente possono stare in stadi diversi (una al 2°
sollecito, una nuova appena scaduta al 1°). Questa normalizzazione rende
autorevole `RecoveryAction.invoice_ids`: quante volte una singola fattura è
stata sollecitata = COUNT ... GROUP BY invoice_id (deriva-on-read, niente
cache, niente drift).

Funzioni PURE di lettura + il dual-write idempotente. Nessuna qui cambia i
TOTALI dello scaduto né il tono dei messaggi: è la fondazione additiva del
modello fattura-centrico. La numerazione/tono live restano per-pratica finché
non si fa la cutover (fase successiva, con verifica dal vivo).
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any

from sqlalchemy import func, or_, and_
from sqlalchemy.orm import Session

from backend.database import (
    RecoveryCase,
    RecoveryAction, RecoveryActionInvoice, Invoice, SyncState,
)
from backend.engine.cases import CONTACT_TYPES

logger = logging.getLogger(__name__)


def set_action_invoices(
    session: Session, action_id: int, invoice_ids: List[int]
) -> None:
    """Scrive le righe di join mancanti per (action_id, invoice_id).

    Idempotente: non duplica le righe già presenti e NON rimuove nulla (un
    sollecito non 'toglie' una fattura già citata). Chiamata in parallelo alla
    scrittura di `RecoveryAction.invoice_ids` (dual-write). Assume che l'azione
    sia già stata flush-ata (id valorizzato).
    """
    if not invoice_ids:
        return
    existing = {
        r[0]
        for r in session.query(RecoveryActionInvoice.invoice_id)
        .filter(RecoveryActionInvoice.action_id == action_id)
        .all()
    }
    for inv_id in sorted(set(invoice_ids)):
        if inv_id in existing:
            continue
        session.add(
            RecoveryActionInvoice(action_id=action_id, invoice_id=inv_id)
        )


def per_invoice_sollecito_stats(
    session: Session, invoice_ids: Optional[List[int]] = None
) -> Dict[int, Dict[str, Any]]:
    """{invoice_id: {"count": n, "last_at": datetime|None}}.

    Conta i SOLLECITI (contatti COMPLETATI, non annullati) che citano ogni
    fattura, via la tabella di join. `count` = quanti solleciti ha ricevuto
    QUELLA fattura; `last_at` = data dell'ultimo. Passare `invoice_ids` per
    restringere (una sola query, niente N+1). Fatture senza solleciti non
    compaiono nel dict (il chiamante usa .get(id, {}) → 0).
    """
    if invoice_ids is not None and not invoice_ids:
        return {}
    q = (
        session.query(
            RecoveryActionInvoice.invoice_id,
            func.count(RecoveryActionInvoice.action_id),
            func.max(
                func.coalesce(
                    RecoveryAction.completed_at, RecoveryAction.created_at
                )
            ),
        )
        .join(RecoveryAction, RecoveryAction.id == RecoveryActionInvoice.action_id)
        .filter(
            RecoveryAction.action_type.in_(CONTACT_TYPES),
            RecoveryAction.completed_at.isnot(None),
            RecoveryAction.cancelled.isnot(True),
        )
        .group_by(RecoveryActionInvoice.invoice_id)
    )
    if invoice_ids is not None:
        q = q.filter(RecoveryActionInvoice.invoice_id.in_(invoice_ids))
    return {
        row[0]: {"count": int(row[1] or 0), "last_at": row[2]}
        for row in q.all()
    }


def per_invoice_actions(
    session: Session, invoice_ids: List[int]
) -> Dict[int, List[RecoveryAction]]:
    """{invoice_id: [RecoveryAction,...]} in ordine cronologico.

    I SOLLECITI (contatti completati, non annullati) che citano ciascuna
    fattura, via join. Stesso identico predicato di
    `per_invoice_sollecito_stats`: così nel dossier il conteggio 'N solleciti'
    e le righe elencate sotto NON possono divergere. Serve al dossier avvocato
    per riportare, PER SINGOLA fattura, lo stato di sollecito e le note
    dell'attività — la nota 'viaggia' con la fattura fino al legale invece di
    restare appiccicata al cliente.
    """
    if not invoice_ids:
        return {}
    rows = (
        session.query(RecoveryActionInvoice.invoice_id, RecoveryAction)
        .join(RecoveryAction, RecoveryAction.id == RecoveryActionInvoice.action_id)
        .filter(
            RecoveryActionInvoice.invoice_id.in_(invoice_ids),
            RecoveryAction.cancelled.isnot(True),
            or_(
                and_(RecoveryAction.action_type.in_(CONTACT_TYPES),
                     RecoveryAction.completed_at.isnot(None)),
                RecoveryAction.action_type == "note",  # le note di gruppo viaggiano col debito
            ),
        )
        .order_by(RecoveryAction.created_at.asc())
        .all()
    )
    out: Dict[int, List[RecoveryAction]] = {}
    for inv_id, action in rows:
        out.setdefault(inv_id, []).append(action)
    return out


def delivered_invoice_ids(session: Session, case, overdue_invoices) -> set:
    """Fatture del ciclo APERTO già CONSEGNATE all'avvocato — PER FATTURA.

    = fatture citate dalle azioni 'lawyer' COMPLETATE non annullate della
    pratica (tabella di join). Un'azione LEGACY (invoice_ids NULL e nessuna
    riga di join: handover pre-tabella o todo legale completato) valeva
    "tutto il cliente": la si attribuisce alle fatture GIÀ SCADUTE alla data
    della consegna (due_date < data) — stesso proxy del backfill — così una
    fattura scaduta DOPO quella consegna NON risulta consegnata (caso Ferro).
    invoice_ids == [] esplicito = nulla consegnato. Unica definizione: la usano
    la sezione Avvocato e il rollup dello stato cliente.
    `overdue_invoices` = oggetti Invoice (serve due_date).
    """
    acts = (
        session.query(RecoveryAction)
        .filter(
            RecoveryAction.case_id == case.id,
            RecoveryAction.action_type == "lawyer",
            RecoveryAction.completed_at.isnot(None),
            RecoveryAction.cancelled.isnot(True),
        )
        .all()
    )
    if not acts:
        return set()
    rows = (
        session.query(RecoveryActionInvoice.action_id, RecoveryActionInvoice.invoice_id)
        .filter(RecoveryActionInvoice.action_id.in_([a.id for a in acts]))
        .all()
    )
    linked = {r[0] for r in rows}
    delivered = {r[1] for r in rows}
    for a in acts:
        if a.id in linked or a.invoice_ids is not None:
            continue  # esplicita (anche se vuota)
        when = (a.completed_at or a.created_at)
        when_d = when.date() if when else None
        for inv in overdue_invoices:
            if when_d is None or (inv.due_date and inv.due_date < when_d):
                delivered.add(inv.id)
    return delivered


# ── Backfill una-tantum dello storico ───────────────────────────────

def backfill_action_invoices(session: Session) -> Dict[str, Any]:
    """Popola la tabella di join dallo storico. Idempotente: marker in
    sync_state (stesso commit) + skip delle coppie già presenti (riavviabile
    anche se il marker manca).

    Due sorgenti:
      1. Azioni CON `invoice_ids`: una riga per id citato (fedele al dato).
      2. Azioni CONTATTO completate SENZA `invoice_ids` (legacy, pre-colonna):
         'coprivano tutte le fatture scadute del cliente a quel momento'. Proxy
         difendibile e non gonfiante: le fatture del cliente già scadute
         (due_date < data azione) all'epoca del sollecito. Le emesse dopo NON
         ereditano il sollecito (una fattura di settembre non nasce già
         sollecitata perché a giugno se ne sollecitò un'altra).

    CAVEAT (solo storico legacy, da decidere con l'owner): per un cliente che
    ha subìto una FUSIONE anagrafica, un'azione legacy senza invoice_ids può
    collegarsi anche alle fatture proprie del sopravvissuto (stesso
    customer_id dopo il merge), sovrastimando i solleciti su quelle nel dossier
    avvocato. Riguarda solo i solleciti pre-colonna su clienti fusi; la
    timeline autorevole completa resta comunque nel dossier. Sorgente 1 (azioni
    con invoice_ids reali, cioè tutti i solleciti moderni) è invece esatta.
    """
    marker = (
        session.query(SyncState)
        .filter_by(key="action_invoices_backfill")
        .first()
    )
    if marker and (marker.result or {}).get("done"):
        return {"skipped": True}

    stats = {"from_invoice_ids": 0, "from_legacy_overdue": 0, "actions_seen": 0}
    existing_pairs = set(
        session.query(
            RecoveryActionInvoice.action_id, RecoveryActionInvoice.invoice_id
        ).all()
    )
    actions = session.query(RecoveryAction).all()

    # 1. Azioni con invoice_ids espliciti.
    legacy: List[RecoveryAction] = []
    for a in actions:
        stats["actions_seen"] += 1
        inv_ids = list(a.invoice_ids or [])
        if not inv_ids:
            if (
                a.action_type in CONTACT_TYPES
                and a.completed_at is not None
                and not a.cancelled
            ):
                legacy.append(a)
            continue
        for inv_id in set(inv_ids):
            if (a.id, inv_id) in existing_pairs:
                continue
            session.add(
                RecoveryActionInvoice(action_id=a.id, invoice_id=inv_id)
            )
            existing_pairs.add((a.id, inv_id))
            stats["from_invoice_ids"] += 1

    # 2. Azioni contatto legacy senza invoice_ids → regola due_date < data.
    if legacy:
        cust_ids = {a.customer_id for a in legacy if a.customer_id}
        inv_by_cust: Dict[int, List[Invoice]] = {}
        if cust_ids:
            for inv in (
                session.query(Invoice)
                .filter(Invoice.customer_id.in_(cust_ids))
                .all()
            ):
                inv_by_cust.setdefault(inv.customer_id, []).append(inv)
        for a in legacy:
            when = a.completed_at or a.created_at
            if when is None:
                continue
            when_d = when.date()
            for inv in inv_by_cust.get(a.customer_id, []):
                if (
                    inv.due_date
                    and inv.due_date < when_d
                    and (a.id, inv.id) not in existing_pairs
                ):
                    session.add(
                        RecoveryActionInvoice(action_id=a.id, invoice_id=inv.id)
                    )
                    existing_pairs.add((a.id, inv.id))
                    stats["from_legacy_overdue"] += 1

    now = datetime.utcnow()
    if not marker:
        marker = SyncState(key="action_invoices_backfill")
        session.add(marker)
    marker.last_sync = now
    marker.result = {"done": True, **stats}
    marker.updated_at = now
    session.commit()
    logger.info(f"Action-invoices backfill done: {stats}")
    return stats


def run_backfill_action_invoices_if_needed() -> Optional[Dict[str, Any]]:
    """Entry point per lo startup: backfill se mai completato.

    Ritenta su IntegrityError: se un sollecito live si registra proprio nella
    finestra del backfill (nuova azione + riga di join concorrente), il commit
    finale può collidere sulla PK. Il ritentativo ri-snapshotta le coppie già
    presenti (ora include quella concorrente) e converge, invece di lasciare lo
    storico non-collegato fino al prossimo riavvio.
    """
    from backend.database import get_session_direct
    from sqlalchemy.exc import IntegrityError

    session = get_session_direct()
    try:
        for attempt in range(3):
            try:
                return backfill_action_invoices(session)
            except IntegrityError as e:
                session.rollback()
                logger.warning(
                    f"Action-invoices backfill conflitto PK "
                    f"(tentativo {attempt + 1}), ritento: {e}"
                )
        logger.error(
            "Action-invoices backfill non converge dopo i ritentativi: "
            "riprova al prossimo avvio"
        )
        return None
    except Exception as e:
        session.rollback()
        logger.error(
            f"Action-invoices backfill FAILED (retry al prossimo avvio): {e}",
            exc_info=True,
        )
        return None
    finally:
        session.close()


def per_invoice_history(session: Session, invoices) -> Dict[int, List[Dict[str, Any]]]:
    """{invoice_id: [{"n", "action_type", "date", "channel", "notes", "legacy"}, ...]}
    in ordine cronologico — il REGISTRO della singola fattura (Fase 5).

    Voci: solleciti (contatti completati) con l'ordinale n. 1, 2, … PER QUELLA
    fattura, consegne al legale, note che la citano. `invoices` = oggetti
    Invoice (servono case_id e due_date). Regole, le STESSE di register_sollecito
    e del rollup, così il numero in riga non contraddice il toast:
    - un'azione LEGACY (invoice_ids NULL e nessuna riga di join: storico
      pre-tabella) valeva "tutto il cliente" → attribuita alle fatture della
      sua pratica GIÀ SCADUTE a quella data (stesso proxy di
      `delivered_invoice_ids`), marcata legacy=True;
    - i contatti EREDITATI dalla pratica precedente (case.inherited_contacts)
      spostano in avanti l'ordinale (il tono non riparte cordiale).
    Ordine per data effettiva (completed_at, altrimenti created_at: le note non
    hanno completed_at e NON devono finire in coda). Due query in tutto.
    """
    invoices = list(invoices or [])
    if not invoices:
        return {}
    invoice_ids = [inv.id for inv in invoices]
    rows = (
        session.query(RecoveryActionInvoice.invoice_id, RecoveryAction)
        .join(RecoveryAction, RecoveryAction.id == RecoveryActionInvoice.action_id)
        .filter(
            RecoveryActionInvoice.invoice_id.in_(invoice_ids),
            RecoveryAction.cancelled.isnot(True),
            or_(
                and_(RecoveryAction.action_type.in_(CONTACT_TYPES + ("lawyer",)),
                     RecoveryAction.completed_at.isnot(None)),
                RecoveryAction.action_type == "note",
            ),
        )
        .all()
    )
    entries: Dict[int, List[tuple]] = {}  # inv_id -> [(when, action, legacy)]
    for inv_id, a in rows:
        entries.setdefault(inv_id, []).append((a.completed_at or a.created_at, a, False))

    # Rete legacy per pratica: contatti/consegne completati senza fatture
    # collegate → valgono per le fatture della pratica scadute a quella data.
    case_ids = sorted({inv.case_id for inv in invoices if inv.case_id})
    inherited_of: Dict[int, int] = {}
    if case_ids:
        for cid, inh in session.query(RecoveryCase.id, RecoveryCase.inherited_contacts).filter(
            RecoveryCase.id.in_(case_ids)
        ).all():
            inherited_of[cid] = int(inh or 0)
        legacy = (
            session.query(RecoveryAction)
            .filter(
                RecoveryAction.case_id.in_(case_ids),
                RecoveryAction.action_type.in_(CONTACT_TYPES + ("lawyer",)),
                RecoveryAction.completed_at.isnot(None),
                RecoveryAction.cancelled.isnot(True),
            )
            .all()
        )
        # invoice_ids è JSON: il None Python può essere salvato come 'null'
        # JSON, non come NULL SQL → il filtro si fa in Python (come
        # _has_unlinked_contacts). [] esplicito NON è legacy.
        legacy = [a for a in legacy if a.invoice_ids is None]
        if legacy:
            linked = {r[0] for r in session.query(RecoveryActionInvoice.action_id)
                      .filter(RecoveryActionInvoice.action_id.in_([a.id for a in legacy])).all()}
            legacy = [a for a in legacy if a.id not in linked]
        for a in legacy:
            when = a.completed_at
            for inv in invoices:
                if inv.case_id == a.case_id and inv.due_date and inv.due_date < when.date():
                    entries.setdefault(inv.id, []).append((when, a, True))

    out: Dict[int, List[Dict[str, Any]]] = {}
    case_of = {inv.id: inv.case_id for inv in invoices}
    for inv_id, items in entries.items():
        items.sort(key=lambda t: (t[0] or datetime.min, t[1].id))
        n = inherited_of.get(case_of.get(inv_id), 0)
        for when, a, is_legacy in items:
            ordinal = None
            if a.action_type in CONTACT_TYPES:
                n += 1
                ordinal = n
            out.setdefault(inv_id, []).append({
                "action_id": a.id,
                "n": ordinal,
                "action_type": a.action_type,
                "date": when.isoformat() if when else None,
                "channel": a.channel,
                "outcome": a.outcome,
                "notes": a.notes,
                "legacy": is_legacy,
            })
    return out
