"""Secondo avviso email: fattura scaduta da N giorni e ancora non saldata.

Richiesta owner (24/11/2026): dopo il promemoria del giorno prima della
scadenza, se a una settimana dalla scadenza la fattura risulta ancora non
pagata il cliente riceve «un'ulteriore email», più diretta: abbiamo rilevato
che la fattura è scaduta da 7 giorni e chiediamo il saldo.

Regole (ognuna ha il suo test in tests/test_followup_email.py):

- QUANDO. Il giro quotidiano (subito dopo quello dei promemoria, quindi dopo
  il sync delle fatture) guarda le scadenze tra oggi−(N+recupero) e oggi−N
  (default N = 7, recupero = 6): ogni fattura riceve l'avviso il PRIMO giorno
  lavorativo in cui è scaduta da almeno N giorni. Mai nel weekend: una
  scadenza di sabato o domenica parte il lunedì. Il testo dice sempre i
  giorni VERI di ritardo al momento dell'invio. I giorni di recupero servono
  a non perdere una fattura per un giro saltato (riavvio, sync fermo).
- A CHI. Solo alle fatture il cui promemoria pre-scadenza è stato inviato
  davvero al cliente (riga `sent` in modalità reale per la stessa scadenza):
  è un secondo avviso, mai il primo. Le altre restano registrate come
  saltate («promemoria non inviato»).
- IL CONTROLLO DEL PAGAMENTO. L'email afferma «non risulta saldata»: parte
  solo se l'ultimo sync delle fatture è RIUSCITO, completo e recente
  (REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS). Altrimenti il giro si ferma senza
  scrivere nulla e la finestra di recupero lo riprende il giorno dopo.
  Restano valide tutte le esclusioni del promemoria (cliente escluso/unito,
  avvocato, assegno in incasso, scadenza stimata, forse già pagata,
  abbinamento da verificare, email assente o non valida) più: pratica in
  attesa decisa dall'operatore.
- UNA SOLA VOLTA. Una riga per (fattura, scadenza) in
  `invoice_followup_emails`, riservata prima dell'invio: stesso motore del
  promemoria (reminder_email.py), nessun doppione per costruzione.
- SPENTO DI DEFAULT. Senza REMINDER_FOLLOWUP_ENABLED=true la modalità è
  «off»: nessuna riga scritta, nessuna email; il pannello mostra l'anteprima
  di ciò che partirebbe. Da acceso segue la modalità del promemoria
  (prova / test / reale).

Non tocca il recupero manuale: non crea azioni, non cambia lo stadio delle
fatture né lo stato del cliente (i solleciti di Luigi restano i suoi).
"""
import html
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from backend.config import config
from backend.database import Customer, Invoice, InvoiceFollowupEmail, InvoiceReminderEmail, SyncState
from backend.engine import reminder_email as base
from backend.engine.format import fmt_date, fmt_eur, italian_today, safe_filename
from backend.engine.reminder_email import (
    MODE_LABELS, MODE_REALE, NOT_REMINDABLE_STATUSES, SKIP_LABELS, STATUS_FAILED, STATUS_SENDING, STATUS_SENT,
    EmailKind, ModeMismatch, NotFound, ReminderBusy, carbon_copies, invoice_label, kind_skip_reason,
    needs_processing, overdue_invoices, resolve_mode,
)

logger = logging.getLogger(__name__)

MODE_OFF = "off"
FOLLOWUP_MODE_LABELS = dict(MODE_LABELS, **{MODE_OFF: "Spento (solo anteprima)"})

BLOCK_LABELS = {
    "off": "Secondo avviso spento (REMINDER_FOLLOWUP_ENABLED)",
    "weekend": "Weekend: nessun invio",
    "sync": "Sync fatture non aggiornato: invio sospeso",
}


# ── Configurazione e calendario ───────────────────────────────────────

def _int_setting(cfg, name: str, default: int, minimum: int) -> int:
    """Intero di configurazione; un valore assente o non numerico vale il
    default (una variabile scritta male su Render non deve far cadere il
    giro né, peggio, anticipare gli invii)."""
    try:
        value = int(str(getattr(cfg, name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def followup_days(cfg=None) -> int:
    """Giorni dalla scadenza dopo i quali parte il secondo avviso (min 1)."""
    return _int_setting(cfg or config, "REMINDER_FOLLOWUP_DAYS", 7, 1)


def followup_lookback(cfg=None) -> int:
    """Giorni di recupero oltre il primo giorno utile (min 2: il weekend)."""
    return _int_setting(cfg or config, "REMINDER_FOLLOWUP_LOOKBACK_DAYS", 6, 2)


def max_sync_age_hours(cfg=None) -> int:
    return _int_setting(cfg or config, "REMINDER_FOLLOWUP_MAX_SYNC_AGE_HOURS", 3, 1)


def resolve_followup_mode(cfg=None) -> Tuple[str, List[str]]:
    """Modalità effettiva del secondo avviso + variabili mancanti. Spento
    finché REMINDER_FOLLOWUP_ENABLED non è vero; da acceso è la modalità del
    promemoria (stessa configurazione Resend, stesso indirizzo di prova)."""
    cfg = cfg or config
    mode, missing = resolve_mode(cfg)
    if not getattr(cfg, "REMINDER_FOLLOWUP_ENABLED", False):
        return MODE_OFF, ["REMINDER_FOLLOWUP_ENABLED"] + list(missing)
    return mode, list(missing)


def followup_window(now_utc: Optional[datetime] = None, cfg=None) -> Optional[Tuple[date, date]]:
    """Le scadenze (prima, ultima) che il giro di oggi guarda; None nel
    weekend. L'ultima è oggi−N: una fattura entra il giorno in cui è scaduta
    da N giorni e resta candidata per i giorni di recupero."""
    today = italian_today(now_utc)
    if today.weekday() >= 5:
        return None
    days = followup_days(cfg)
    return today - timedelta(days=days + followup_lookback(cfg)), today - timedelta(days=days)


def sync_gate(session: Session, now_utc: Optional[datetime] = None, cfg=None) -> dict:
    """Il dato «non pagata» è affidabile? Sì solo se l'ULTIMO sync delle
    fatture è riuscito, completo e recente. Nessun dettaglio dell'errore del
    sync viene riportato qui (lo mostra già «Salute sistema»)."""
    max_hours = max_sync_age_hours(cfg)
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    out = {"ok": False, "max_age_hours": max_hours, "last_sync": None, "age_minutes": None, "detail": None}
    row = session.query(SyncState).filter(SyncState.key == "invoices").first()
    if row is None or row.last_sync is None:
        out["detail"] = "nessun sync delle fatture registrato"
        return out
    age_minutes = int((now - row.last_sync).total_seconds() // 60)
    out["last_sync"] = row.last_sync.isoformat()
    out["age_minutes"] = age_minutes
    result = row.result if isinstance(row.result, dict) else {}
    fatturapro = result.get("fatturapro") if isinstance(result.get("fatturapro"), dict) else {}
    if not fatturapro.get("success"):
        out["detail"] = "l'ultimo sync delle fatture non è riuscito"
    elif fatturapro.get("partial"):
        out["detail"] = "l'ultimo sync delle fatture è parziale"
    elif age_minutes > max_hours * 60:
        out["detail"] = (
            f"l'ultimo sync delle fatture riuscito è di {age_minutes // 60} ore fa "
            f"(massimo {max_hours})"
        )
    else:
        out["ok"] = True
    return out


# ── Selezione ─────────────────────────────────────────────────────────

def candidate_invoices(session: Session, window: Tuple[date, date]) -> List[Invoice]:
    """Fatture non pagate/annullate/contestate con scadenza nella finestra."""
    first, last = window
    return (
        session.query(Invoice)
        .filter(
            Invoice.due_date >= first,
            Invoice.due_date <= last,
            Invoice.status.notin_(NOT_REMINDABLE_STATUSES),
            Invoice.amount_due > 0,
            Invoice.customer_id.isnot(None),
        )
        .order_by(Invoice.due_date.asc(), Invoice.customer_id.asc(), Invoice.invoice_number.asc())
        .all()
    )


def reminder_sent(session: Session, invoice: Invoice) -> Optional[InvoiceReminderEmail]:
    """Il promemoria pre-scadenza inviato DAVVERO al cliente per questa
    fattura e questa scadenza (None se non c'è: prova, test, saltato,
    fallito, o scadenza cambiata dopo l'invio)."""
    if invoice.due_date is None:
        return None
    return (
        session.query(InvoiceReminderEmail)
        .filter(
            InvoiceReminderEmail.invoice_id == invoice.id,
            InvoiceReminderEmail.due_date == invoice.due_date,
            InvoiceReminderEmail.status == STATUS_SENT,
            InvoiceReminderEmail.mode == MODE_REALE,
        )
        .first()
    )


def followup_extra_skip(session: Session, invoice: Invoice, customer: Optional[Customer]) -> Optional[str]:
    """Esclusioni proprie del secondo avviso (dopo quelle comuni)."""
    if reminder_sent(session, invoice) is None:
        return "senza_promemoria"
    # Attesa decisa dall'operatore (todo «Attesa» pendente sulla pratica): un
    # accordo preso a voce non va scavalcato da un'email automatica.
    if customer is not None and (customer.recovery_status or "") == "waiting":
        return "in_attesa"
    return None


# ── Composizione ──────────────────────────────────────────────────────

def days_overdue_on(invoice: Invoice, today: date) -> int:
    """Giorni di ritardo VERI alla data data (dalla scadenza, non dal campo
    `days_overdue` che il sync ricalcola a orari suoi)."""
    if invoice.due_date is None:
        return int(invoice.days_overdue or 0)
    return max((today - invoice.due_date).days, 0)


def compose_followup(customer: Customer, invoice: Invoice, overdue: List[Invoice],
                     today: Optional[date] = None) -> dict:
    """Oggetto e testo del secondo avviso: diretto, corto, con tutti i
    dettagli — fattura, importo, scadenza, giorni di ritardo, eventuali altre
    scadute col totale, coordinate di pagamento, cosa fare se ha già pagato."""
    today = today or italian_today()
    name = (customer.ragione_sociale or "").strip() or "Cliente"
    importo = fmt_eur(invoice.amount_due)
    scadenza = fmt_date(invoice.due_date)
    numero = invoice_label(invoice.invoice_number)
    days = days_overdue_on(invoice, today)
    giorni = "1 giorno" if days == 1 else f"{days} giorni"
    subject = f"Sollecito di pagamento: fattura n. {numero} scaduta il {scadenza} ({importo})"

    total = round(sum(float(i.amount_due or 0) for i in overdue), 2)
    grand_total = round(float(invoice.amount_due or 0) + total, 2)
    paragraphs = [
        f"Spett.le {name},",
        (
            f"abbiamo rilevato che la fattura n. {numero} di {importo} è scaduta da {giorni} "
            f"(scadenza {scadenza}) e non risulta ancora saldata. Copia della fattura in allegato."
        ),
    ]
    if overdue:
        n = len(overdue)
        if n == 1:
            altre = f"Risulta inoltre scaduta e non saldata 1 altra fattura per {fmt_eur(total)}."
        else:
            altre = f"Risultano inoltre scadute e non saldate altre {n} fatture per un totale di {fmt_eur(total)}."
        paragraphs.append(f"{altre} Totale da saldare: {fmt_eur(grand_total)}. Riepilogo in allegato.")
    saldo = f"Le chiediamo di provvedere al saldo: bonifico a {config.COMPANY_NAME}"
    if config.COMPANY_IBAN:
        saldo += f", IBAN {config.COMPANY_IBAN}"
    saldo += f", causale \"Fattura {numero}\"."
    paragraphs.append(saldo)
    paragraphs.append(
        "Se ha già effettuato il pagamento, Le chiediamo di inviarci la contabile "
        "rispondendo a questa email."
    )
    paragraphs.append(f"{config.COMPANY_NAME} - Amministrazione")

    text = "\n\n".join(paragraphs)
    html_body = "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)
    return {
        "subject": subject,
        "text": text,
        "html": html_body,
        "overdue_count": len(overdue),
        "overdue_total": total,
        "grand_total": grand_total,
        "days_overdue": days,
    }


def _pdf_view(invoice: Invoice, today: date) -> SimpleNamespace:
    """La fattura come la leggono i generatori di PDF, con i giorni di
    ritardo calcolati ad OGGI: sul documento che va al cliente non deve
    comparire un ritardo vecchio (o «in scadenza» su una fattura scaduta)."""
    return SimpleNamespace(
        invoice_number=invoice.invoice_number, amount=invoice.amount, amount_due=invoice.amount_due,
        issue_date=invoice.issue_date, due_date=invoice.due_date, status=invoice.status,
        source_platform=invoice.source_platform, days_overdue=days_overdue_on(invoice, today),
    )


def build_followup_attachments(customer: Customer, invoice: Invoice,
                               overdue: List[Invoice]) -> List[Tuple[str, bytes]]:
    """Copia di cortesia della fattura scaduta + (se ce ne sono altre) il
    riepilogo di tutte le scadute, lei compresa, coi giorni di ritardo e il
    totale. Import locale: i generatori vivono in backend.api.recovery."""
    from backend.api.recovery import _build_invoice_pdf, _build_riepilogativo_pdf

    today = italian_today()
    out = [(
        f"copia_cortesia_fattura_{safe_filename(invoice_label(invoice.invoice_number))}.pdf",
        bytes(_build_invoice_pdf(customer, _pdf_view(invoice, today), courtesy=True)),
    )]
    if overdue:
        views = [_pdf_view(i, today) for i in [invoice] + list(overdue)]
        out.append((
            f"riepilogo_scadute_{safe_filename(customer.ragione_sociale or 'cliente')}.pdf",
            bytes(_build_riepilogativo_pdf(customer, views, title="Riepilogo Fatture Scadute")),
        ))
    return out


FOLLOWUP = EmailKind(
    key="followup", label="Secondo avviso", model=InvoiceFollowupEmail,
    compose=lambda *a, **kw: compose_followup(*a, **kw),
    attachments=lambda *a, **kw: build_followup_attachments(*a, **kw),
    idempotency_prefix="followup",
    extra_skip=lambda *a, **kw: followup_extra_skip(*a, **kw),
)


# ── Anteprima ─────────────────────────────────────────────────────────

def _existing_row(session: Session, invoice: Invoice) -> Optional[InvoiceFollowupEmail]:
    return (
        session.query(InvoiceFollowupEmail)
        .filter_by(invoice_id=invoice.id, due_date=invoice.due_date)
        .first()
    )


def preview_followups(session: Session, *, now_utc: Optional[datetime] = None, cfg=None,
                      mode: Optional[str] = None) -> List[dict]:
    """Cosa farebbe il giro di oggi: NESSUNA scrittura. Da spento mostra ciò
    che partirebbe accendendolo (valutato con la modalità del promemoria)."""
    cfg = cfg or config
    window = followup_window(now_utc, cfg)
    if window is None:
        return []
    mode = mode or resolve_followup_mode(cfg)[0]
    eval_mode = resolve_mode(cfg)[0] if mode == MODE_OFF else mode
    today = italian_today(now_utc)
    out = []
    for inv in candidate_invoices(session, window):
        customer = inv.customer
        existing = _existing_row(session, inv)
        reason = kind_skip_reason(session, inv, customer, FOLLOWUP)
        will, why = needs_processing(existing, eval_mode)
        reminder = reminder_sent(session, inv)
        entry = {
            "invoice_id": inv.id,
            "invoice_number": inv.invoice_number,
            "customer_id": customer.id if customer else None,
            "ragione_sociale": customer.ragione_sociale if customer else inv.customer_name_raw,
            "to_email": (customer.email or "").strip() if customer else None,
            "cc": carbon_copies(customer, cfg) if (customer and eval_mode == MODE_REALE) else [],
            "would_cc": carbon_copies(customer, cfg) if customer else [],
            "amount_due": float(inv.amount_due or 0),
            "due_date": inv.due_date.isoformat(),
            "days_overdue": days_overdue_on(inv, today),
            "reminder_sent_at": reminder.sent_at.isoformat() if reminder is not None and reminder.sent_at else None,
            "skip_reason": reason,
            "skip_label": SKIP_LABELS.get(reason) if reason else None,
            "already": existing.status if existing else None,
            "already_mode": existing.mode if existing else None,
            "will_process": bool(will and not reason),
            "why": why if not reason else SKIP_LABELS.get(reason, reason),
            "overdue_count": 0,
            "overdue_total": 0.0,
            "grand_total": float(inv.amount_due or 0),
            "subject": None,
            "text": None,
        }
        if not reason:
            overdue = overdue_invoices(session, customer.id, exclude_id=inv.id)
            msg = compose_followup(customer, inv, overdue, today)
            entry.update({
                "overdue_count": msg["overdue_count"],
                "overdue_total": msg["overdue_total"],
                "grand_total": msg["grand_total"],
                "subject": msg["subject"],
                "text": msg["text"],
            })
        out.append(entry)
    return out


# ── Esecuzione ────────────────────────────────────────────────────────

def _summary(mode: str, missing: List[str], window: Optional[Tuple[date, date]]) -> dict:
    summary = base._empty_summary(None, mode, missing)
    summary.update({
        "kind": FOLLOWUP.key,
        "window": [d.isoformat() for d in window] if window else None,
        "blocked": None,
        "blocked_label": None,
        "sync": None,
    })
    return summary


def _blocked(summary: dict, why: str) -> dict:
    summary["blocked"] = why
    summary["blocked_label"] = BLOCK_LABELS.get(why, why)
    return summary


def run_followups(
    session: Session,
    *,
    now_utc: Optional[datetime] = None,
    mailer=None,
    cfg=None,
    mode: Optional[str] = None,
    expected_mode: Optional[str] = None,
) -> dict:
    """Il giro quotidiano del secondo avviso. Non parte (e non scrive nulla)
    se la funzione è spenta, nel weekend o se il sync delle fatture non è
    aggiornato: il motivo è in `blocked`.

    `expected_mode`: la modalità che l'operatore ha confermato a video; se il
    server ne risolve un'altra (env cambiate nel frattempo) non si parte.
    """
    cfg = cfg or config
    resolved, missing = resolve_followup_mode(cfg)
    mode = mode or resolved
    if expected_mode and expected_mode != mode:
        raise ModeMismatch(mode, expected_mode)
    window = followup_window(now_utc, cfg)
    summary = _summary(mode, missing, window)
    if mode == MODE_OFF:
        return _blocked(summary, "off")
    if window is None:
        return _blocked(summary, "weekend")
    gate = sync_gate(session, now_utc, cfg)
    summary["sync"] = gate
    if not gate["ok"]:
        logger.warning("Secondo avviso email NON eseguito: %s", gate["detail"])
        return _blocked(summary, "sync")

    if not base._reminder_lock.acquire(blocking=False):
        raise ReminderBusy()
    try:
        mode, _missing, mailer, own_mailer = base._resolve_runtime(cfg, mailer, mode)
        today = italian_today(now_utc)
        try:
            for inv in candidate_invoices(session, window):
                summary["candidates"] += 1
                existing = _existing_row(session, inv)
                will, _why = needs_processing(existing, mode)
                if not will:
                    summary["already"] += 1
                    continue
                base._process_safely(session, inv, inv.due_date, mode=mode, mailer=mailer, cfg=cfg,
                                     summary=summary, existing=existing, today=today, kind=FOLLOWUP)
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        base._reminder_lock.release()

    logger.info(
        "Secondo avviso email (scadenze %s → %s, modalità %s): candidate=%d inviate=%d preparate=%d "
        "saltate=%d fallite=%d non_lavorate=%d",
        summary["window"][0], summary["window"][1], mode, summary["candidates"], summary["sent"],
        summary["prepared"], summary["skipped"], summary["failed"], summary["already"],
    )
    return summary


def retry_followup_row(session: Session, row: InvoiceFollowupEmail, *, mailer=None, cfg=None,
                       mode: Optional[str] = None, expected_mode: Optional[str] = None,
                       now_utc: Optional[datetime] = None) -> dict:
    """Ritenta UN secondo avviso fermo (`failed` o `sending` orfana). La
    fattura deve essere ancora scaduta e non saldata, con la stessa scadenza;
    valgono lo spento e il controllo del sync del giro."""
    cfg = cfg or config
    if row.status not in (STATUS_FAILED, STATUS_SENDING):
        raise ValueError("si ritentano solo gli avvisi falliti o interrotti")
    inv = session.query(Invoice).filter(Invoice.id == row.invoice_id).first()
    if inv is None:
        raise ValueError("fattura non più presente")
    if inv.status in NOT_REMINDABLE_STATUSES or float(inv.amount_due or 0) <= 0:
        raise ValueError("fattura non più da sollecitare (pagata, annullata o contestata)")
    today = italian_today(now_utc)
    if inv.due_date is None or inv.due_date != row.due_date:
        raise ValueError("la scadenza della fattura è cambiata: questo avviso non è più valido")
    if inv.due_date >= today:
        raise ValueError("la fattura non è scaduta")
    resolved, missing = resolve_followup_mode(cfg)
    mode = mode or resolved
    if expected_mode and expected_mode != mode:
        raise ModeMismatch(mode, expected_mode)
    if mode == MODE_OFF:
        raise ValueError("secondo avviso spento (REMINDER_FOLLOWUP_ENABLED)")
    gate = sync_gate(session, now_utc, cfg)
    if not gate["ok"]:
        raise ValueError(f"sync fatture non aggiornato ({gate['detail']}): riprova dopo un sync riuscito")
    if not base._reminder_lock.acquire(blocking=False):
        raise ReminderBusy()
    try:
        mode, _missing, mailer, own_mailer = base._resolve_runtime(cfg, mailer, mode)
        summary = _summary(mode, missing, None)
        summary["sync"] = gate
        summary["candidates"] = 1
        try:
            base._process_safely(session, inv, row.due_date, mode=mode, mailer=mailer, cfg=cfg,
                                 summary=summary, existing=row, today=today, kind=FOLLOWUP)
        finally:
            if own_mailer and mailer is not None:
                mailer.close()
    finally:
        base._reminder_lock.release()
    return summary


def send_followup_test(session: Session, invoice_id: int, to_address: str, *, mailer=None, cfg=None,
                       now_utc: Optional[datetime] = None) -> dict:
    """Invia a UN indirizzo interno il secondo avviso che il cliente
    riceverebbe per la fattura data ([PROVA], nessuna copia, registro
    intoccato). Funziona anche da spento: serve a leggere testo e allegati
    veri prima di accenderlo."""
    inv = session.query(Invoice).filter(Invoice.id == invoice_id).first()
    if inv is None:
        raise NotFound("fattura non trovata")
    today = italian_today(now_utc)
    if inv.due_date is None or inv.due_date >= today:
        raise ValueError("fattura non ancora scaduta: il secondo avviso riguarda solo fatture scadute")
    return base.send_test(session, invoice_id, to_address, mailer=mailer, cfg=cfg, kind=FOLLOWUP, today=today)
