"""Pratiche di recupero (RecoveryCase): lifecycle e backfill.

Una pratica è UN ciclo di debito di un cliente: si apre quando compaiono
fatture scadute non pagate, raccoglie i solleciti di quel ciclo, si chiude
a saldo. Numerazione ("PRIMA/SECONDA azione") e tono del messaggio contano
le azioni della pratica — mai tutta la storia del cliente.

Regole di chiusura (closed_reason):
- paid:       tutte le fatture del ciclo risultano pagate
- no_overdue: la pratica si è svuotata SENZA saldo (es. scadenze corrette
              nel futuro) → riapribile in qualsiasi momento
- resolved:   restano solo fatture contestate (disputed)
- archived:   archiviata dall'operatore (inesigibile / passata al legale)
- excluded:   cliente escluso dal recupero

Riapertura della STESSA pratica (contatore preservato, todo ripristinati):
- sempre, se era chiusa 'no_overdue'
- entro REOPEN_PAID_WINDOW_DAYS se era chiusa 'paid' e una SUA fattura
  torna scaduta-non-pagata (guardia anti-flapping dello scraper)
- MAI scavalcando un'archiviazione: si riprende l'ULTIMA decisione presa sul
  debito, non una precedente. Una pratica chiusa PRIMA dell'archiviazione ha
  il contatore di allora e riaprirla farebbe ripartire il tono da zero.

Il debito archiviato è dichiarato INESIGIBILE: le sue fatture restano nella
pratica archiviata (scadute e non pagate: è il motivo dell'archiviazione) e
non riaprono nulla al sync successivo. Solo un debito NUOVO — una fattura
scaduta che non era nella pratica archiviata — apre una pratica nuova.

Dopo una pratica 'archived' (o con azioni legali) la nuova pratica eredita
il conteggio contatti: il tono non riparte mai dal sollecito cordiale.
"""

import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, joinedload

from backend.config import config
from backend.database import (
    Customer, Invoice, RecoveryCase, RecoveryAction, ActivityLog, SyncState,
    RecoveryActionInvoice,
)
# Definizione unica di "scaduto" — vive in engine/overdue.py perché la
# condividono motore e KPI. Ri-esportata qui: è da qui che la importano
# gli 11 punti del motore.
from backend.engine.overdue import is_overdue_unpaid, is_in_incasso  # noqa: F401

logger = logging.getLogger(__name__)


def business_day_start(now_utc: Optional[datetime] = None) -> datetime:
    """Inizio della giornata lavorativa ITALIANA corrente, in UTC naive.

    Il server (Render) gira in UTC: usare date.today() per il dedup dei
    solleciti sposterebbe il confine di giornata all'1-2 di notte italiane.
    Tutti i confronti "oggi" sui solleciti passano da qui.
    """
    tz = ZoneInfo(config.TIMEZONE)
    now = (now_utc or datetime.utcnow()).replace(tzinfo=timezone.utc)
    local_midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


# Tipi di azione che rappresentano un CONTATTO verso il cliente
# (contano per numerazione e tono). lawyer/wait/archive/note non lo sono.
CONTACT_TYPES = ("first_contact", "second_contact")

# Giorni entro cui una pratica chiusa 'paid' viene riaperta se una sua
# fattura torna scaduta (protegge dal flapping paid→open dello scraper).
REOPEN_PAID_WINDOW_DAYS = 30

# Progressione dopo l'N-esimo contatto: (tipo prossima azione, giorni).
PROGRESSION = {
    1: ("second_contact", 7),
    2: ("lawyer", 14),
    # dal 3° contatto in poi: follow-up legale
    "default": ("lawyer", 30),
}

BACKFILL_HISTORY_CAP_DAYS = 180


def get_open_case(session: Session, customer_id: int) -> Optional[RecoveryCase]:
    return session.query(RecoveryCase).filter(
        RecoveryCase.customer_id == customer_id,
        RecoveryCase.status == "open",
    ).first()


def contact_count(session: Session, case: RecoveryCase) -> int:
    """Contatti effettuati nella pratica (incl. ereditati da pratica archiviata)."""
    n = session.query(RecoveryAction).filter(
        RecoveryAction.case_id == case.id,
        RecoveryAction.action_type.in_(CONTACT_TYPES),
        RecoveryAction.completed_at.isnot(None),
        RecoveryAction.cancelled.isnot(True),
    ).count()
    return n + (case.inherited_contacts or 0)


def _has_unlinked_contacts(session: Session, case: RecoveryCase) -> bool:
    """La pratica ha contatti completati NON collegati a fatture (righe di
    join assenti = storico pre-tabella)? Solo allora vale la rete legacy."""
    acts = session.query(RecoveryAction.id, RecoveryAction.invoice_ids).filter(
        RecoveryAction.case_id == case.id,
        RecoveryAction.action_type.in_(CONTACT_TYPES),
        RecoveryAction.completed_at.isnot(None),
        RecoveryAction.cancelled.isnot(True),
    ).all()
    if not acts:
        return False
    ids = [a[0] for a in acts]
    linked = {r[0] for r in session.query(RecoveryActionInvoice.action_id)
              .filter(RecoveryActionInvoice.action_id.in_(ids)).all()}
    # Legacy = invoice_ids NULL (colonna assente all'epoca) E nessuna riga di
    # join. Un [] ESPLICITO ("non ha citato nulla", es. contatto completato
    # senza scadute) NON è legacy e non deve avvelenare la pratica.
    return any(inv_ids is None and aid not in linked for aid, inv_ids in acts)


def _case_has_lawyer_actions(session: Session, case: RecoveryCase) -> bool:
    return session.query(RecoveryAction).filter(
        RecoveryAction.case_id == case.id,
        RecoveryAction.action_type == "lawyer",
        RecoveryAction.cancelled.isnot(True),
    ).count() > 0


def open_new_case(session: Session, customer: Customer) -> RecoveryCase:
    """Apre una nuova pratica, ereditando i contatti se l'ultima pratica
    chiusa era archiviata o passata al legale."""
    inherited = 0
    reopened_after_archive = False
    last_closed = session.query(RecoveryCase).filter(
        RecoveryCase.customer_id == customer.id,
        RecoveryCase.status == "closed",
    ).order_by(RecoveryCase.closed_at.desc().nullslast()).first()

    if last_closed and (
        last_closed.closed_reason == "archived"
        or _case_has_lawyer_actions(session, last_closed)
    ):
        inherited = contact_count(session, last_closed)
        reopened_after_archive = True

    case = RecoveryCase(
        customer_id=customer.id,
        status="open",
        opened_at=datetime.utcnow(),
        inherited_contacts=inherited,
        reopened_after_archive=reopened_after_archive,
    )
    try:
        # L'indice UNIQUE parziale (customer_id WHERE status='open') protegge
        # dalle corse tra scheduler ed endpoint. Il SAVEPOINT confina il
        # conflitto a QUESTO insert: un rollback di sessione butterebbe via
        # anche il lavoro non ancora committato del pass di lifecycle in
        # corso (aperture/chiusure degli altri clienti).
        with session.begin_nested():
            session.add(case)
    except Exception:
        existing = get_open_case(session, customer.id)
        if existing:
            logger.info(
                f"Open case for customer {customer.id} created concurrently — reusing {existing.id}"
            )
            return existing
        raise

    # Il cliente torna in gestione (anche se era 'archived': nuova pratica)
    if customer.recovery_status == "archived":
        customer.recovery_status = "idle"

    session.add(ActivityLog(
        action="case_opened",
        entity_type="case",
        entity_id=case.id,
        details={
            "customer_id": customer.id,
            "customer": customer.ragione_sociale,
            "inherited_contacts": inherited,
            "reopened_after_archive": reopened_after_archive,
        },
    ))
    return case


def reopen_case(session: Session, case: RecoveryCase) -> RecoveryCase:
    """Riapre una pratica chiusa: contatore preservato, todo ripristinati."""
    case.status = "open"
    reason_was = case.closed_reason
    case.closed_at = None
    case.closed_reason = None
    case.updated_at = datetime.utcnow()

    # Ripristina le azioni pendenti annullate dalla chiusura
    restored = session.query(RecoveryAction).filter(
        RecoveryAction.case_id == case.id,
        RecoveryAction.cancelled.is_(True),
        RecoveryAction.cancelled_reason == "case_closed",
    ).all()
    for action in restored:
        action.cancelled = False
        action.cancelled_reason = None

    customer = case.customer
    if customer:
        _refresh_customer_status(session, customer, case)

    session.add(ActivityLog(
        action="case_reopened",
        entity_type="case",
        entity_id=case.id,
        details={
            "customer_id": case.customer_id,
            "was_closed_as": reason_was,
            "restored_pending_actions": len(restored),
        },
    ))
    logger.info(f"Case {case.id} reopened (was '{reason_was}', restored {len(restored)} pending)")
    return case


def ensure_open_case(session: Session, customer: Customer) -> RecoveryCase:
    """Pratica aperta del cliente, creandola (o riaprendo quella giusta) se
    serve. Le fatture scadute del cliente vengono agganciate subito: la
    chiusura a saldo deve vedere le SUE fatture, non aspettare il sync."""
    case = get_open_case(session, customer.id)
    if not case:
        # C'è una pratica chiusa riapribile tra quelle delle sue fatture?
        reopenable = _find_reopenable_case(session, customer)
        case = reopen_case(session, reopenable) if reopenable else open_new_case(session, customer)

    attach_overdue_invoices(session, customer, case)
    return case


def attach_overdue_invoices(session: Session, customer: Customer, case: RecoveryCase) -> int:
    """Aggancia alla pratica le fatture scadute non pagate del cliente che
    non appartengono già a una pratica aperta."""
    attached = 0
    for inv in customer.invoices:
        if not is_overdue_unpaid(inv):
            continue
        if inv.case_id == case.id:
            continue
        if inv.case_id is None or (inv.case is not None and inv.case.status == "closed"):
            inv.case_id = case.id
            attached += 1
    return attached


def _archived_case_ids(session: Session) -> set:
    """Id di TUTTE le pratiche archiviate: il loro debito è stato dichiarato
    inesigibile dall'operatore.

    Una fattura agganciata a una di queste pratiche è debito già archiviato:
    resta scaduta per definizione (è il motivo stesso dell'archiviazione) e
    non deve far ripartire nulla al sync successivo.
    """
    return {
        row[0] for row in session.query(RecoveryCase.id).filter(
            RecoveryCase.status == "closed",
            RecoveryCase.closed_reason == "archived",
        ).all()
    }


def _find_reopenable_case(session: Session, customer: Customer) -> Optional[RecoveryCase]:
    """Pratica chiusa che il debito scaduto del cliente può far riaprire, o None.

    I candidati si scorrono dalla chiusura più recente alla più vecchia:
    riaprire significa riprendere l'ULTIMA decisione presa su questo debito.
    """
    overdue = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]
    case_ids = {inv.case_id for inv in overdue if inv.case_id}
    if not case_ids:
        return None
    candidates = session.query(RecoveryCase).filter(
        RecoveryCase.id.in_(case_ids),
        RecoveryCase.customer_id == customer.id,
        RecoveryCase.status == "closed",
    ).order_by(RecoveryCase.closed_at.desc().nullslast()).all()
    cutoff = datetime.utcnow() - timedelta(days=REOPEN_PAID_WINDOW_DAYS)
    for case in candidates:
        if case.closed_reason in ("no_overdue", "in_incasso"):
            return case
        if case.closed_reason == "paid" and case.closed_at and case.closed_at >= cutoff:
            return case
        if case.closed_reason == "archived":
            # Un'archiviazione non si scavalca: è la decisione più recente
            # dell'operatore su questo cliente. Riaprire una pratica chiusa
            # PRIMA di essa significa tornare indietro nel tempo e riprenderne
            # il contatore a zero — il cliente appena passato al legale
            # riceverebbe un primo sollecito cordiale. Fermarsi qui fa cadere
            # il caso su open_new_case, che eredita il tono dall'archiviata.
            return None
    return None


def close_case(session: Session, case: RecoveryCase, reason: str) -> None:
    """Chiude la pratica: annulla i todo del ciclo e resetta lo stato cliente."""
    case.status = "closed"
    case.closed_at = datetime.utcnow()
    case.closed_reason = reason
    case.updated_at = datetime.utcnow()

    # Annulla le azioni pendenti della pratica E quelle orfane del cliente
    # (azioni registrate prima dell'introduzione delle pratiche).
    # Le NOTE sono annotazioni, non todo: restano fuori.
    pending = session.query(RecoveryAction).filter(
        RecoveryAction.customer_id == case.customer_id,
        RecoveryAction.completed_at.is_(None),
        RecoveryAction.cancelled.isnot(True),
        RecoveryAction.action_type != "note",
        (RecoveryAction.case_id == case.id) | (RecoveryAction.case_id.is_(None)),
    ).all()
    note_by_reason = {
        "paid": "annullata: pratica chiusa a saldo",
        "no_overdue": "sospesa: nessuna fattura scaduta residua",
        "in_incasso": "sospesa: fatture coperte da assegno in attesa di incasso",
        "resolved": "annullata: restano solo fatture contestate",
        "archived": "annullata: pratica archiviata",
        "excluded": "annullata: cliente escluso",
    }
    for action in pending:
        action.cancelled = True
        action.cancelled_reason = "case_closed"
        extra = note_by_reason.get(reason, "annullata: pratica chiusa")
        action.notes = f"{action.notes} | {extra}" if action.notes else extra

    customer = case.customer
    if customer:
        if reason == "archived":
            customer.recovery_status = "archived"
        elif reason != "excluded":
            customer.recovery_status = "idle"
        customer.next_action_date = None
        customer.next_action_type = None
        customer.updated_at = datetime.utcnow()

    session.add(ActivityLog(
        action="case_closed",
        entity_type="case",
        entity_id=case.id,
        details={
            "customer_id": case.customer_id,
            "reason": reason,
            "cancelled_pending_actions": len(pending),
        },
    ))
    logger.info(f"Case {case.id} closed ({reason}), {len(pending)} pending cancelled")


def _refresh_customer_status(session: Session, customer: Customer, case: RecoveryCase) -> None:
    """Ricava lo stato-cache del cliente dal contenuto della pratica.

    'lawyer' SOLO se TUTTE le scadute sono state consegnate al legale (azioni
    'lawyer' completate che le citano — vedi engine.action_invoices); un todo
    legale PENDENTE o una consegna PARZIALE non sono "dal legale". NB: i
    bucket per stadio della riconciliazione sono keyed su questo campo
    (engine/overdue.py): il totale non cambia, cambia la ripartizione.
    """
    from backend.engine.action_invoices import (
        delivered_invoice_ids, per_invoice_sollecito_stats,
    )
    overdue = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]
    all_delivered = bool(overdue) and (
        {i.id for i in overdue} <= delivered_invoice_ids(session, case, overdue)
    )
    # Stadio = il PEGGIORE fra le scadute (max solleciti ricevuti da UNA
    # fattura, tabella di join) + i contatti EREDITATI da una pratica
    # archiviata/legale (il tono non riparte mai cordiale: regola owner).
    # Rete di sicurezza legacy SOLO se la pratica ha contatti completati SENZA
    # righe di join (storico pre-tabella non collegato): se i contatti sono
    # collegati ma le loro fatture sono state pagate, la scaduta residua mai
    # sollecitata parte davvero da zero.
    inherited = case.inherited_contacts or 0
    stats = per_invoice_sollecito_stats(session, [i.id for i in overdue]) if overdue else {}
    if stats:
        n = max(v["count"] for v in stats.values()) + inherited
    elif _has_unlinked_contacts(session, case):
        n = contact_count(session, case)
    else:
        n = inherited
    if all_delivered:
        customer.recovery_status = "lawyer"
    elif n >= 2:
        customer.recovery_status = "second_contact"
    elif n == 1:
        customer.recovery_status = "first_contact"
    else:
        customer.recovery_status = "idle"

    next_pending = session.query(RecoveryAction).filter(
        RecoveryAction.case_id == case.id,
        RecoveryAction.completed_at.is_(None),
        RecoveryAction.cancelled.isnot(True),
        RecoveryAction.scheduled_date.isnot(None),
    ).order_by(RecoveryAction.scheduled_date.asc()).first()
    customer.next_action_date = next_pending.scheduled_date if next_pending else None
    customer.next_action_type = next_pending.action_type if next_pending else None
    customer.updated_at = datetime.utcnow()


def schedule_next_action(
    session: Session,
    customer: Customer,
    case: RecoveryCase,
    contacts_done: int,
    scheduled_date: Optional[date] = None,
    superseded_by: Optional[int] = None,
) -> Optional[RecoveryAction]:
    """Pianifica la prossima azione dopo un contatto — UNICA fonte della
    progressione (+7 → second, +14 → lawyer, +30 → follow-up lawyer).

    Se la pratica ha già un todo legale pendente, non pianifica nulla:
    il passaggio all'avvocato resta l'unico next step.
    """
    pending_lawyer = session.query(RecoveryAction).filter(
        RecoveryAction.case_id == case.id,
        RecoveryAction.action_type == "lawyer",
        RecoveryAction.completed_at.is_(None),
        RecoveryAction.cancelled.isnot(True),
    ).first()
    if pending_lawyer:
        # Il todo legale vale solo se ALMENO una scaduta non consegnata ha
        # ricevuto 2 solleciti propri: se le fatture per cui era stato
        # pianificato sono state pagate e resta solo debito nuovo, è stantio
        # → si annulla e si segue la progressione normale.
        from backend.engine.action_invoices import (
            per_invoice_sollecito_stats, delivered_invoice_ids,
        )
        overdue = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]
        delivered = delivered_invoice_ids(session, case, overdue) if overdue else set()
        stats = per_invoice_sollecito_stats(session, [i.id for i in overdue if i.id not in delivered])
        mature = any(v["count"] + (case.inherited_contacts or 0) >= 2 for v in stats.values())
        # È stantio SOLO il todo "Auto-pianificata dopo il N° contatto" quando
        # NESSUNA fattura lo giustifica più: niente scadute mature non
        # consegnate E niente fatture CONSEGNATE ancora impagate (il follow-up
        # avvocato esiste proprio per quelle). Un todo creato a mano è una
        # decisione dell'operatore e blocca sempre.
        auto_planned = (pending_lawyer.notes or "").startswith("Auto-pianificata dopo il")
        if (not auto_planned or mature or delivered
                or _has_unlinked_contacts(session, case)):
            customer.next_action_date = pending_lawyer.scheduled_date
            customer.next_action_type = "lawyer"
            return None
        pending_lawyer.cancelled = True
        # Stesso formato del supersede dei contatti: l'undo del sollecito che
        # lo ha soppiantato lo ripristina.
        pending_lawyer.cancelled_reason = (
            f"superseded_by_sollecito:{superseded_by}" if superseded_by else "superseded_by_sollecito:stale"
        )
        pending_lawyer.notes = f"{pending_lawyer.notes} | annullato: le fatture del passaggio legale sono state pagate" if pending_lawyer.notes else "annullato: le fatture del passaggio legale sono state pagate"

    next_type, delta_days = PROGRESSION.get(contacts_done, PROGRESSION["default"])
    next_date = scheduled_date or (date.today() + timedelta(days=delta_days))

    next_action = RecoveryAction(
        customer_id=customer.id,
        case_id=case.id,
        action_type=next_type,
        scheduled_date=next_date,
        notes=f"Auto-pianificata dopo il {contacts_done}° contatto",
    )
    session.add(next_action)

    customer.next_action_date = next_date
    customer.next_action_type = next_type
    customer.updated_at = datetime.utcnow()
    return next_action


def update_case_lifecycle(session: Session, allow_close: bool = True) -> Dict[str, Any]:
    """Passata completa: apre/aggancia/chiude le pratiche in base alle fatture.

    allow_close=False quando il sync fatture è stato PARZIALE: in quel caso
    la payment detection non è affidabile e chiudere pratiche (annullando i
    todo) sarebbe distruttivo. Aprire e agganciare resta sempre sicuro.
    """
    stats = {"opened": 0, "reopened": 0, "closed": 0, "attached": 0, "detached": 0}

    # I clienti fusi (merged_into) non hanno più fatture proprie e non devono
    # generare/toccare pratiche: fuori dal ciclo.
    customers = session.query(Customer).filter(
        Customer.merged_into.is_(None)
    ).options(
        joinedload(Customer.invoices),
    ).all()
    open_cases = {
        c.customer_id: c
        for c in session.query(RecoveryCase).filter(RecoveryCase.status == "open").all()
    }
    archived_ids = _archived_case_ids(session)

    for customer in customers:
        try:
            open_case = open_cases.get(customer.id)
            overdue = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]

            # Regola difensiva: una fattura riassegnata a un altro cliente non
            # può restare agganciata alla pratica del cliente precedente.
            for inv in customer.invoices:
                if inv.case_id and inv.case is not None and inv.case.customer_id != customer.id:
                    inv.case_id = None
                    stats["detached"] += 1

            # Il debito già archiviato non riapre nulla: le sue fatture restano
            # scadute per definizione (è il motivo dell'archiviazione) e restano
            # nella pratica archiviata. Solo un debito NUOVO merita una pratica
            # nuova. Va dopo lo sgancio difensivo: una fattura riassegnata ha
            # ormai case_id=None ed è debito nuovo per il cliente che la eredita.
            overdue = [inv for inv in overdue if inv.case_id not in archived_ids]

            # Cliente escluso: la pratica aperta si chiude.
            if customer.excluded:
                if open_case and allow_close:
                    close_case(session, open_case, "excluded")
                    stats["closed"] += 1
                continue

            if overdue:
                if not open_case:
                    reopenable = _find_reopenable_case(session, customer)
                    if reopenable:
                        open_case = reopen_case(session, reopenable)
                        stats["reopened"] += 1
                    else:
                        open_case = open_new_case(session, customer)
                        stats["opened"] += 1
                    open_cases[customer.id] = open_case

                for inv in overdue:
                    if inv.case_id == open_case.id:
                        continue
                    if inv.case_id is None or (inv.case is not None and inv.case.status == "closed"):
                        inv.case_id = open_case.id
                        stats["attached"] += 1

            elif open_case and allow_close:
                attached = session.query(Invoice).filter(
                    Invoice.case_id == open_case.id
                ).all()
                non_paid = [inv for inv in attached if inv.status != "paid"]
                if attached and not non_paid:
                    reason = "paid"
                elif non_paid and all(inv.status == "disputed" for inv in non_paid):
                    reason = "resolved"
                elif non_paid and any(is_in_incasso(inv) for inv in non_paid) and all(
                        is_in_incasso(inv) or inv.status == "disputed" for inv in non_paid):
                    reason = "in_incasso"
                else:
                    reason = "no_overdue"
                close_case(session, open_case, reason)
                stats["closed"] += 1
                open_cases.pop(customer.id, None)
        except Exception as e:
            logger.error(f"Case lifecycle error for customer {customer.id}: {e}", exc_info=True)
            continue

    session.commit()
    logger.info(f"Case lifecycle: {stats}")
    return stats


# ── Backfill una-tantum (migrazione dati live) ──────────────────────

def backfill_cases(session: Session) -> Dict[str, Any]:
    """Costruisce le pratiche iniziali dai dati esistenti. Idempotente:
    protetto da un marker in sync_state, scritto nello STESSO commit del
    backfill (tutto-o-niente: un fallimento a metà non lascia stato parziale
    e riprova al prossimo avvio).

    Per ogni cliente con fatture scadute non pagate:
    - pratica aperta + aggancio fatture;
    - aggancio delle azioni del ciclo corrente (created_at >= inizio ciclo,
      cap 180gg); le azioni CONTATTO pendenti legacy vengono convertite in
      completate (nel flusso storico l'azione veniva registrata al momento
      del contatto) e il loro todo futuro viene ricreato come pending.

    Per ogni cliente SENZA scadute ma con recovery_status attivo:
    - reset a idle + annullo dei todo orfani (fix immediato degli stati
      stantii in produzione).
    """
    marker = session.query(SyncState).filter_by(key="case_backfill").first()
    if marker and (marker.result or {}).get("done"):
        return {"skipped": True}

    stats = {
        "cases_created": 0, "invoices_attached": 0, "actions_attached": 0,
        "pending_converted": 0, "stale_customers_reset": 0, "orphan_pending_cancelled": 0,
    }
    now = datetime.utcnow()
    history_cutoff = now - timedelta(days=BACKFILL_HISTORY_CAP_DAYS)

    # I clienti fusi (merged_into) non hanno più fatture proprie e non devono
    # generare/toccare pratiche: fuori dal ciclo.
    customers = session.query(Customer).filter(
        Customer.merged_into.is_(None)
    ).options(
        joinedload(Customer.invoices),
    ).all()

    for customer in customers:
        overdue = [inv for inv in customer.invoices if is_overdue_unpaid(inv)]

        if overdue and not customer.excluded:
            # Convergenza sul retry: se una pratica aperta esiste già (un
            # backfill precedente fallito a metà, il lifecycle dello
            # startup-sync, un sollecito registrato nel frattempo) la si
            # RIUSA — crearne un'altra alla cieca violerebbe l'indice
            # UNIQUE e renderebbe il backfill non riavviabile per sempre.
            case = get_open_case(session, customer.id)
            if case is None:
                case = RecoveryCase(
                    customer_id=customer.id,
                    status="open",
                    opened_at=now,
                )
                try:
                    with session.begin_nested():
                        session.add(case)
                except Exception:
                    case = get_open_case(session, customer.id)
                    if case is None:
                        raise
                stats["cases_created"] += 1

            for inv in overdue:
                inv.case_id = case.id
                stats["invoices_attached"] += 1

            # Inizio del ciclo corrente: la scadenza più vecchia ancora aperta.
            due_dates = [inv.due_date for inv in overdue if inv.due_date]
            issue_dates = [inv.issue_date for inv in overdue if inv.issue_date]
            if due_dates:
                cycle_start_date = min(due_dates)
            elif issue_dates:
                cycle_start_date = min(issue_dates)
            else:
                cycle_start_date = None

            cycle_start = None
            if cycle_start_date:
                cycle_start = max(
                    datetime.combine(cycle_start_date, datetime.min.time()),
                    history_cutoff,
                )

            actions = session.query(RecoveryAction).filter(
                RecoveryAction.customer_id == customer.id,
                RecoveryAction.case_id.is_(None),
            ).all()
            attached_contacts = 0
            for action in actions:
                is_pending = action.completed_at is None and not action.cancelled
                in_window = cycle_start is not None and action.created_at and action.created_at >= cycle_start
                if not (is_pending or in_window):
                    continue
                action.case_id = case.id
                stats["actions_attached"] += 1

                # Contatto legacy pendente = contatto già avvenuto (registrato
                # alla creazione) + promemoria futuro. Si scinde nei due pezzi.
                if is_pending and action.action_type in CONTACT_TYPES and in_window:
                    future_date = action.scheduled_date
                    action.completed_at = action.created_at
                    action.scheduled_date = action.created_at.date()
                    action.outcome = action.outcome or "contacted"
                    stats["pending_converted"] += 1
                    attached_contacts += 1
                    if future_date and future_date >= date.today():
                        next_type = "second_contact" if action.action_type == "first_contact" else "lawyer"
                        session.add(RecoveryAction(
                            customer_id=customer.id,
                            case_id=case.id,
                            action_type=next_type,
                            scheduled_date=future_date,
                            notes="Ricreata dal backfill pratiche (todo del ciclo in corso)",
                        ))
                elif action.completed_at and action.action_type in CONTACT_TYPES:
                    attached_contacts += 1

            if attached_contacts >= 3:
                session.add(ActivityLog(
                    action="case_backfill_review",
                    entity_type="case",
                    entity_id=case.id,
                    details={
                        "customer": customer.ragione_sociale,
                        "attached_contacts": attached_contacts,
                        "note": "Numerazione alta ereditata dal backfill: verificare",
                    },
                ))

        elif not overdue and customer.recovery_status in (
            "first_contact", "second_contact", "lawyer", "waiting"
        ):
            customer.recovery_status = "idle"
            customer.next_action_date = None
            customer.next_action_type = None
            customer.updated_at = now
            stats["stale_customers_reset"] += 1

            orphans = session.query(RecoveryAction).filter(
                RecoveryAction.customer_id == customer.id,
                RecoveryAction.completed_at.is_(None),
                RecoveryAction.cancelled.isnot(True),
            ).all()
            for action in orphans:
                action.cancelled = True
                action.cancelled_reason = "case_closed"
                note = "annullata dal backfill: nessuna fattura scaduta residua"
                action.notes = f"{action.notes} | {note}" if action.notes else note
                stats["orphan_pending_cancelled"] += 1

            session.add(ActivityLog(
                action="case_backfill_reset",
                entity_type="customer",
                entity_id=customer.id,
                details={"customer": customer.ragione_sociale},
            ))

    # Marker nello stesso commit: o tutto o niente.
    if not marker:
        marker = SyncState(key="case_backfill")
        session.add(marker)
    marker.last_sync = now
    marker.result = {"done": True, **stats}
    marker.updated_at = now

    session.add(ActivityLog(action="case_backfill_done", details=stats))
    session.commit()
    logger.info(f"Case backfill done: {stats}")
    return stats


def run_backfill_if_needed() -> Optional[Dict[str, Any]]:
    """Entry point per lo startup: esegue il backfill se mai completato."""
    from backend.database import get_session_direct
    session = get_session_direct()
    try:
        return backfill_cases(session)
    except Exception as e:
        session.rollback()
        logger.error(f"Case backfill FAILED (will retry at next startup): {e}", exc_info=True)
        return None
    finally:
        session.close()


def resplit_status_if_needed() -> Optional[Dict[str, Any]]:
    """Una-tantum al primo avvio dopo il deploy del rollup per-fattura: ricalcola
    lo stato di TUTTE le pratiche aperte e registra ogni spostamento in
    ActivityLog ('status_resplit', vecchio → nuovo). I bucket per stadio della
    riconciliazione sono keyed sullo stato: così il salto nei tile è UN evento
    spiegabile, non una deriva cliente per cliente nelle settimane. Il totale
    non cambia di un euro; cambia solo la ripartizione I/II contatto/Avvocato.
    """
    from backend.database import get_session_direct
    session = get_session_direct()
    try:
        marker = session.query(SyncState).filter_by(key="status_resplit_v1").first()
        if marker and (marker.result or {}).get("done"):
            return {"skipped": True}
        # Vincolo: il rollup per-fattura ha senso solo DOPO il backfill della
        # tabella di join; se non è (ancora) completato si rimanda al prossimo
        # avvio, altrimenti si demolirebbero stati corretti (es. 'lawyer' con
        # handover moderno) sulla base di righe di join assenti.
        jb = session.query(SyncState).filter_by(key="action_invoices_backfill").first()
        if not (jb and (jb.result or {}).get("done")):
            logger.info("Status resplit rimandato: backfill azione↔fattura non completato")
            return {"skipped": "waiting_join_backfill"}
        moved = []
        cases = session.query(RecoveryCase).filter(RecoveryCase.status == "open").all()
        for case in cases:
            customer = case.customer
            if customer is None or customer.excluded:
                continue
            old = customer.recovery_status
            _refresh_customer_status(session, customer, case)
            if customer.recovery_status != old:
                moved.append({"customer_id": customer.id, "customer": customer.ragione_sociale,
                              "from": old, "to": customer.recovery_status})
                session.add(ActivityLog(
                    action="status_resplit", entity_type="customer", entity_id=customer.id,
                    details={"from": old, "to": customer.recovery_status,
                             "note": "rollup per-fattura: lo stadio segue la fattura più sollecitata; "
                                     "'lawyer' solo con tutte le scadute consegnate"},
                ))
        now = datetime.utcnow()
        if not marker:
            marker = SyncState(key="status_resplit_v1")
            session.add(marker)
        marker.last_sync = now
        marker.result = {"done": True, "cases": len(cases), "moved": len(moved)}
        marker.updated_at = now
        session.add(ActivityLog(action="status_resplit_done",
                                details={"cases": len(cases), "moved": len(moved), "items": moved[:200]}))
        session.commit()
        logger.info(f"Status resplit done: {len(moved)}/{len(cases)} clienti spostati")
        return {"cases": len(cases), "moved": len(moved)}
    except Exception as e:
        session.rollback()
        logger.error(f"Status resplit FAILED (retry al prossimo avvio): {e}", exc_info=True)
        return None
    finally:
        session.close()


def refresh_customer_lifecycle(session: Session, customer: Customer) -> Optional[RecoveryCase]:
    """Lifecycle di UN cliente, stesse regole della passata completa: apre /
    riapre / aggancia se ci sono scadute lavorabili, chiude se non ne restano.
    Usato dagli endpoint che cambiano lo stato di UNA fattura (assegno in mano,
    insoluto) per non aspettare il prossimo sync. Ritorna la pratica aperta.
    """
    open_case = get_open_case(session, customer.id)
    if customer.excluded:
        if open_case:
            close_case(session, open_case, "excluded")
        return None
    archived_ids = _archived_case_ids(session)
    overdue = [
        inv for inv in customer.invoices
        if is_overdue_unpaid(inv) and inv.case_id not in archived_ids
        and not (inv.case_id and inv.case is not None and inv.case.customer_id != customer.id)
    ]
    if overdue:
        if not open_case:
            reopenable = _find_reopenable_case(session, customer)
            open_case = reopen_case(session, reopenable) if reopenable else open_new_case(session, customer)
        for inv in overdue:
            if inv.case_id != open_case.id and (
                inv.case_id is None or (inv.case is not None and inv.case.status == "closed")
            ):
                inv.case_id = open_case.id
        session.flush()
        _refresh_customer_status(session, customer, open_case)
        return open_case
    if open_case:
        attached = session.query(Invoice).filter(Invoice.case_id == open_case.id).all()
        non_paid = [inv for inv in attached if inv.status != "paid"]
        if attached and not non_paid:
            reason = "paid"
        elif non_paid and all(inv.status == "disputed" for inv in non_paid):
            reason = "resolved"
        elif non_paid and any(is_in_incasso(inv) for inv in non_paid) and all(
                is_in_incasso(inv) or inv.status == "disputed" for inv in non_paid):
            reason = "in_incasso"
        else:
            reason = "no_overdue"
        close_case(session, open_case, reason)
    return None
