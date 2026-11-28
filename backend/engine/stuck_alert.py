"""Avviso interno quando un'email automatica resta FERMA.

Decisione owner (28/11/2026): niente infrastruttura per verificare da soli
su Resend — non è mai capitato — ma, se capita, deve arrivare un avviso: a
Lorenzo e a fatture@. Dopo il giro del mattino (promemoria + secondo avviso)
si guardano i due registri: per ogni email reale rimasta ferma (fallita o
interrotta a metà invio) di una fattura ancora da saldare parte UNA email
interna, una volta sola per quel tentativo.

Non invia nulla ai clienti, non tocca i registri delle email: ricorda solo,
in `sync_state` (chiave `email_stuck_alert`), di quali situazioni ferme ha
già avvisato. Un errore qui non deve mai far cadere il giro.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from backend.config import config
from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail, SyncState
from backend.engine import followup_email as followup
from backend.engine.format import fmt_date, fmt_eur
from backend.engine.reminder_email import (
    ERROR_CONFLICT, MODE_REALE, NOT_REMINDABLE_STATUSES, STATUS_FAILED, STATUS_SENDING, invoice_label,
    is_uncertain, normalize_email, resolve_mode,
)

logger = logging.getLogger(__name__)

STATE_KEY = "email_stuck_alert"
SENDING_GRACE = timedelta(minutes=2)   # una riga «in invio» più giovane è un invio in corso, non una riga ferma
KINDS = (("promemoria", "Promemoria", InvoiceReminderEmail), ("followup", "Secondo avviso", InvoiceFollowupEmail))


def recipients(cfg=None) -> List[str]:
    """Gli indirizzi INTERNI dell'avviso (validi, senza doppioni)."""
    cfg = cfg or config
    raw = getattr(cfg, "REMINDER_ALERT_TO", "") or ""
    out: List[str] = []
    for part in raw.replace(";", ",").split(","):
        email = normalize_email(part)
        if email and email not in out:
            out.append(email)
    return out


def _mark(kind: str, row) -> str:
    """L'identità di UNA situazione ferma: la riga e COME è ferma. Non l'ora
    del tentativo: un giro che ritenta da solo e fallisce allo stesso modo
    (indirizzo rifiutato, PDF mancante) non deve riavvisare ogni volta."""
    return f"{kind}:{row.id}:{row.status}:{row.error_kind or '-'}"


def _why(kind: str, row) -> str:
    uncertain = followup._is_uncertain(row) if kind == "followup" else is_uncertain(row)
    if row.error_kind == ERROR_CONFLICT:
        return "Resend l'aveva già ricevuta: con ogni probabilità è ARRIVATA. Non rimandare senza verificare."
    if row.status == STATUS_SENDING:
        return "Invio interrotto a metà: potrebbe essere arrivata. Da verificare su Resend."
    if uncertain:
        return "Esito incerto: potrebbe essere arrivata. Da verificare su Resend prima di rimandarla."
    return "Certamente NON partita: un giro la riprende da solo nel suo giorno, altrimenti «Riprova» in Sistema."


def stuck_emails(session: Session, now_utc: Optional[datetime] = None) -> List[dict]:
    """Le email reali ferme di fatture ancora da saldare, una voce per email
    (le fatture della stessa email insieme), dalla più recente."""
    now = (now_utc or datetime.now(timezone.utc)).replace(tzinfo=None)
    groups: Dict[str, dict] = {}
    for kind, label, model in KINDS:
        found = (
            session.query(model, Invoice, Customer)
            .join(Invoice, Invoice.id == model.invoice_id)
            .outerjoin(Customer, Customer.id == model.customer_id)
            .filter(
                model.status.in_((STATUS_FAILED, STATUS_SENDING)),
                model.mode == MODE_REALE,
                Invoice.status.notin_(NOT_REMINDABLE_STATUSES),
                Invoice.amount_due > 0,
            )
            .order_by(model.id.asc())
            .all()
        )
        for row, inv, customer in found:
            started = row.attempted_at or row.created_at
            if row.status == STATUS_SENDING and started and now - started < SENDING_GRACE:
                continue   # un giro manuale la sta mandando adesso
            key = f"{kind}:{row.group_key or 'riga-' + str(row.id)}:{row.customer_id}"
            entry = groups.setdefault(key, {
                "kind": kind, "kind_label": label, "customer": (customer.ragione_sociale if customer else None)
                or inv.customer_name_raw or "cliente sconosciuto", "to_email": row.to_email, "invoices": [],
                "marks": [], "why": _why(kind, row), "error": (row.error or "")[:300], "when": None,
            })
            entry["invoices"].append((invoice_label(inv.invoice_number), inv.due_date, float(row.amount_due or 0)))
            entry["marks"].append(_mark(kind, row))
            moment = row.attempted_at or row.created_at
            if moment and (entry["when"] is None or moment > entry["when"]):
                entry["when"] = moment
    return sorted(groups.values(), key=lambda e: e["when"] or datetime.min, reverse=True)


def _local(moment: datetime, tz_name: str) -> str:
    try:
        return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return moment.strftime("%d/%m/%Y %H:%M") + " UTC"


def compose(entries: List[dict], tz_name: str = "Europe/Rome") -> dict:
    n = len(entries)
    subject = ("Recupero crediti: 1 email automatica ferma da verificare" if n == 1
               else f"Recupero crediti: {n} email automatiche ferme da verificare")
    blocks = []
    for e in entries:
        lines = [f"{e['kind_label']} — {e['customer']}" + (f" ({e['to_email']})" if e["to_email"] else "")]
        for number, due, amount in e["invoices"]:
            lines.append(f"  - fattura n. {number}, scadenza {fmt_date(due) if due else '—'}, {fmt_eur(amount)}")
        if e["when"]:
            lines.append(f"  Ultimo tentativo: {_local(e['when'], tz_name)}")
        lines.append(f"  {e['why']}")
        if e["error"]:
            lines.append(f"  Dettaglio: {e['error']}")
        blocks.append("\n".join(lines))
    text = "\n\n".join(
        ["Dopo il giro delle email automatiche queste sono rimaste FERME. Ai clienti non è stato mandato "
         "altro: restano così finché qualcuno non le gestisce (Sistema → «Promemoria email» / «Secondo avviso email»)."]
        + blocks
        + ["Avviso automatico della piattaforma Recupero crediti: arriva una volta sola per ogni tentativo fermo."]
    )
    return {"subject": subject, "text": text}


def _load_marks(session: Session) -> List[str]:
    row = session.query(SyncState).filter(SyncState.key == STATE_KEY).first()
    marks = (row.result or {}).get("marks") if row is not None and isinstance(row.result, dict) else None
    return list(marks) if isinstance(marks, list) else []


def _save_marks(session: Session, marks: List[str], now: datetime) -> None:
    row = session.query(SyncState).filter(SyncState.key == STATE_KEY).first() or SyncState(key=STATE_KEY)
    row.last_sync = now
    row.result = {"marks": marks}
    session.add(row)
    session.commit()


def send_stuck_alert(session: Session, *, mailer=None, cfg=None, now_utc: Optional[datetime] = None) -> dict:
    """Manda l'avviso per le email ferme di cui non si è ancora avvisato.
    Solo in modalità reale e con almeno un destinatario. Mai un'eccezione."""
    cfg = cfg or config
    out = {"stuck": 0, "new": 0, "sent": False, "to": [], "skipped": None}
    try:
        if resolve_mode(cfg)[0] != MODE_REALE:
            out["skipped"] = "non in modalità reale"
            return out
        to = recipients(cfg)
        out["to"] = to
        entries = stuck_emails(session, now_utc)
        out["stuck"] = len(entries)
        seen = _load_marks(session)
        fresh = [e for e in entries if any(mark not in seen for mark in e["marks"])]
        out["new"] = len(fresh)
        if not fresh:
            return out
        if not to:
            out["skipped"] = "nessun destinatario (REMINDER_ALERT_TO)"
            logger.warning("Email automatiche ferme: %d, ma nessun destinatario per l'avviso", len(fresh))
            return out
        msg = compose(fresh, getattr(cfg, "TIMEZONE", None) or "Europe/Rome")
        marks = sorted(mark for e in fresh for mark in e["marks"])
        # Si ricordano solo le situazioni ANCORA ferme: l'elenco non cresce
        # all'infinito e una riga che torna ferma dopo essere stata risolta
        # avvisa di nuovo.
        current = {mark for e in entries for mark in e["marks"]}
        own = False
        if mailer is None:
            from backend.connectors.resend_mail import ResendMailer
            mailer = ResendMailer(cfg.RESEND_API_KEY, getattr(cfg, "RESEND_API_URL", None))
            own = True
        try:
            mailer.send(
                sender=cfg.REMINDER_EMAIL_FROM, to=to, subject=msg["subject"], text=msg["text"],
                idempotency_key="stuck-alert-" + hashlib.sha256("|".join(marks).encode("utf-8")).hexdigest()[:24],
            )
        finally:
            if own:
                mailer.close()
        now = now_utc or datetime.now(timezone.utc)
        _save_marks(session, sorted(current & (set(seen) | set(marks))), now.replace(tzinfo=None) if now.tzinfo else now)
        out["sent"] = True
        logger.info("Avviso email ferme inviato a %d destinatari per %d email", len(to), len(fresh))
    except Exception as e:  # l'avviso non deve mai far cadere il giro
        logger.error(f"Avviso email ferme non inviato: {e}", exc_info=True)
        out["skipped"] = "errore"
        try:
            session.rollback()
        except Exception:
            pass
    return out
