"""Storico dello scaduto: uno snapshot al giorno per il grafico di evoluzione.

La dashboard fotografa solo l'istante presente. Qui si persiste la cascata
giorno per giorno, così l'evoluzione dello scaduto — totale, lavorabile,
recuperato — diventa una serie storica.

Due scritture, due nature:
- `record_overdue_snapshot`: lo snapshot VERO del giorno corrente (dal sync).
- `backfill_overdue_history`: la ricostruzione STIMATA del passato dalle
  date fattura (estimated=True), one-shot allo startup, perché il grafico
  non parta vuoto. I punti veri la rimpiazzano man mano.

Modulo DEDICATO apposta: `record_overdue_snapshot` ha bisogno sia degli
helper di engine/overdue.py sia di business_day_start (engine/cases.py), e
cases.py importa già overdue.py. Mettere qui la scrittura evita l'import
circolare che nascerebbe importando cases dentro overdue.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from backend.database import Customer, Invoice, OverdueSnapshot, SyncState
from backend.engine.overdue import (
    OVERDUE_BUCKETS, bucket_expr,
    compute_overdue_buckets, compute_recuperato_certo,
    first_recovery_action_subquery, recovered_invoice_clause,
)
from backend.engine.cases import business_day_start

logger = logging.getLogger(__name__)

# Marker one-shot in sync_state (stile case_backfill): scritto SOLO a
# successo, nello stesso commit del backfill — un fallimento a metà non
# lascia il marker e si riprova al prossimo avvio.
HISTORY_BACKFILL_KEY = "overdue_history_backfill"

# Quanti giorni di passato ricostruire (la finestra di default del grafico).
HISTORY_BACKFILL_DAYS = 90


def record_overdue_snapshot(session):
    """UPSERT dello snapshot dello scaduto per il giorno lavorativo corrente.

    Un solo snapshot per giorno: se ne esiste già uno per oggi lo aggiorna
    (idempotente — due sync nello stesso giorno non creano due righe),
    altrimenti lo crea. La "data di oggi" è il giorno lavorativo ITALIANO
    (business_day_start), non date.today() UTC: sul server UTC il confine di
    giornata cadrebbe all'1-2 di notte italiane.

    Gli importi vengono dalle stesse funzioni che alimentano
    /riconciliazione (compute_overdue_buckets, compute_recuperato_certo):
    la serie storica e il numero live condividono la definizione e non
    possono divergere.

    Se la riga del giorno era una STIMA del backfill, qui viene promossa a
    snapshot vero (estimated=False, valori ricalcolati): il punto reale
    sostituisce la ricostruzione, mai il contrario.

    Fa il commit e ritorna la riga. Il chiamante possiede la sessione (non
    viene chiusa qui).
    """
    buckets = compute_overdue_buckets(session)
    rec_fatture, rec_importo = compute_recuperato_certo(session)
    today = business_day_start().date()

    snap = (
        session.query(OverdueSnapshot)
        .filter(OverdueSnapshot.date == today)
        .first()
    )
    if snap is None:
        snap = OverdueSnapshot(date=today)
        session.add(snap)

    # Uno snapshot scritto dal sync è SEMPRE vero: se la riga era una stima
    # del backfill, da qui in poi non lo è più.
    snap.estimated = False

    snap.scaduto_totale = buckets["scaduto_totale"]["importo"]
    snap.non_abbinati = buckets["non_abbinati"]["importo"]
    snap.esclusi = buckets["esclusi"]["importo"]
    snap.contestati = buckets["contestati"]["importo"]
    snap.lavorabile = buckets["lavorabile"]["importo"]
    snap.recuperato_certo = round(rec_importo, 2)

    snap.scaduto_totale_fatture = buckets["scaduto_totale"]["fatture"]
    snap.non_abbinati_fatture = buckets["non_abbinati"]["fatture"]
    snap.esclusi_fatture = buckets["esclusi"]["fatture"]
    snap.contestati_fatture = buckets["contestati"]["fatture"]
    snap.lavorabile_fatture = buckets["lavorabile"]["fatture"]
    snap.recuperato_certo_fatture = rec_fatture

    session.commit()
    logger.info(
        "Overdue snapshot %s: scaduto=%.2f lavorabile=%.2f recuperato=%.2f",
        today, snap.scaduto_totale, snap.lavorabile, snap.recuperato_certo,
    )
    return snap


def backfill_overdue_history(session, days: int = HISTORY_BACKFILL_DAYS) -> dict:
    """Ricostruisce ~`days` giorni di storico STIMATO dalle date fattura.

    Per ogni giorno D da (oggi−days) a IERI — giorno lavorativo italiano,
    stessa àncora di record_overdue_snapshot — stima lo stato che la cascata
    aveva al giorno D usando i dati fattura di OGGI:

    - una fattura era "aperta e scaduta" al giorno D se `due_date <= D` e
      non era ancora pagata a quella data. "Pagata al giorno D": `paid_at <=
      D` se paid_at esiste; per le `status='paid'` SENZA paid_at (storico
      pre-migrazione) si usa `updated_at` come data-pagamento stimata — la
      stessa convenzione dello "storico stimato" di /riconciliazione.
    - LIMITE ONESTO sugli importi: per le pagate il residuo storico non
      esiste più (amount_due è stato azzerato al pagamento), quindi si usa
      l'importo PIENO (`amount`); per le ancora aperte si usa `amount_due`
      di oggi proiettato indietro. È una stima, ed è marcata come tale.
    - LIMITE ONESTO sulla composizione: il bucket è quello di `bucket_expr()`
      (engine/overdue.py) — la classificazione di OGGI (escluso/contestato/
      orfana) proiettata indietro. Un cliente escluso il mese scorso risulta
      escluso anche nei giorni in cui non lo era ancora: la storia delle
      decisioni non è registrata e non viene inventata.
    - `recuperato_certo(D)`: cumulato delle fatture con `paid_at <= D` che
      rispettano le stesse clausole del live (first_recovery_action_subquery
      + recovered_invoice_clause): prima della migrazione paid_at non esiste,
      quindi la serie stimata parte da 0 — coerente col "certo" della
      cascata, che non mescola mele e pere.

    Scrive righe `OverdueSnapshot(estimated=True)` SOLO per le date senza
    alcuno snapshot: le righe VERE non si toccano mai, e nemmeno le stime
    già scritte (idempotente per costruzione). Non fa commit: flush soltanto
    — il chiamante decide il confine transazionale (il marker one-shot viene
    scritto nello STESSO commit).
    """
    today = business_day_start().date()
    start = today - timedelta(days=days)

    existing = {
        d for (d,) in session.query(OverdueSnapshot.date)
        .filter(OverdueSnapshot.date >= start)
        .all()
    }

    # L'universo storico in UNA query: fatture con una scadenza già passata.
    # Il bucket lo assegna bucket_expr() — la stessa gerarchia della cascata
    # live, riusata e non ricopiata (se divergesse, sarebbe il bug che
    # questa architettura è nata per curare).
    invoice_rows = (
        session.query(
            bucket_expr().label("bucket"),
            Invoice.due_date,
            Invoice.status,
            Invoice.paid_at,
            Invoice.updated_at,
            Invoice.amount,
            Invoice.amount_due,
        )
        .outerjoin(Customer, Invoice.customer_id == Customer.id)
        .filter(Invoice.due_date.isnot(None), Invoice.due_date < today)
        .all()
    )

    # Pre-digest: (bucket, due_date, data-pagamento stimata | None, importo).
    universe = []
    for bucket, due, status, paid_at, updated_at, amount, amount_due in invoice_rows:
        if paid_at is not None:
            paid_on = paid_at.date()
        elif status == "paid":
            # Storico pre-migrazione: updated_at come data-pagamento stimata.
            # Senza nemmeno updated_at non c'è NULLA che dati il pagamento:
            # la riga si tratta come pagata da sempre (mai nella serie) —
            # meglio ometterla che inventarle una permanenza nello scaduto.
            if updated_at is None:
                continue
            paid_on = updated_at.date()
        else:
            paid_on = None  # mai pagata: aperta ancora oggi
        importo = float(amount or 0) if status == "paid" else float(amount_due or 0)
        universe.append((bucket, due, paid_on, importo))

    # Recuperato certo storico: le stesse clausole del live (riusate), ma
    # riga per riga così il cumulato si può tagliare a ogni giorno D.
    first_action = first_recovery_action_subquery(session)
    recovered = [
        (paid_at.date(), float(residuo or 0.0))
        for paid_at, residuo in (
            session.query(
                Invoice.paid_at,
                func.coalesce(Invoice.amount_due_at_paid, 0.0),
            )
            .join(first_action, Invoice.customer_id == first_action.c.customer_id)
            .filter(
                Invoice.status == "paid",
                Invoice.paid_at.isnot(None),
                Invoice.paid_at >= first_action.c.first_action,
                recovered_invoice_clause(first_action),
            )
            .order_by(Invoice.paid_at.asc())
            .all()
        )
    ]

    created = 0
    skipped = 0
    rec_idx = 0
    rec_importo = 0.0
    rec_fatture = 0
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)

        # Il cumulato del recuperato avanza SEMPRE, anche sui giorni saltati.
        while rec_idx < len(recovered) and recovered[rec_idx][0] <= d:
            rec_importo += recovered[rec_idx][1]
            rec_fatture += 1
            rec_idx += 1

        if d in existing:
            skipped += 1
            continue

        per_bucket = {b: [0, 0.0] for b in OVERDUE_BUCKETS}
        for bucket, due, paid_on, importo in universe:
            if due > d:
                continue  # non ancora scaduta al giorno D
            if paid_on is not None and paid_on <= d:
                continue  # già pagata al giorno D
            per_bucket[bucket][0] += 1
            per_bucket[bucket][1] += importo

        # Arrotondamento per bucket PRIMA del totale, come
        # compute_overdue_buckets: l'identità cascata regge al centesimo.
        importi = {b: round(v[1], 2) for b, v in per_bucket.items()}
        fatture = {b: v[0] for b, v in per_bucket.items()}

        session.add(OverdueSnapshot(
            date=d,
            estimated=True,
            scaduto_totale=round(sum(importi.values()), 2),
            non_abbinati=importi["non_abbinati"],
            esclusi=importi["esclusi"],
            contestati=importi["contestati"],
            lavorabile=importi["lavorabile"],
            recuperato_certo=round(rec_importo, 2),
            scaduto_totale_fatture=sum(fatture.values()),
            non_abbinati_fatture=fatture["non_abbinati"],
            esclusi_fatture=fatture["esclusi"],
            contestati_fatture=fatture["contestati"],
            lavorabile_fatture=fatture["lavorabile"],
            recuperato_certo_fatture=rec_fatture,
        ))
        created += 1

    session.flush()
    return {
        "created": created,
        "skipped_existing": skipped,
        "from": start.isoformat(),
        "to": (today - timedelta(days=1)).isoformat(),
    }


def backfill_overdue_history_if_needed(
    session, days: int = HISTORY_BACKFILL_DAYS
) -> dict:
    """Backfill one-shot: marker in sync_state scritto nello STESSO commit.

    Tutto-o-niente come il case_backfill (cases.backfill_cases): se il
    backfill esplode a metà, niente marker e niente righe — al prossimo
    avvio si riprova da capo. Se il marker c'è già, non si fa nulla.
    """
    marker = session.query(SyncState).filter_by(key=HISTORY_BACKFILL_KEY).first()
    if marker and (marker.result or {}).get("done"):
        return {"skipped": True}

    stats = backfill_overdue_history(session, days=days)

    now = datetime.utcnow()
    if not marker:
        marker = SyncState(key=HISTORY_BACKFILL_KEY)
        session.add(marker)
    marker.last_sync = now
    marker.result = {"done": True, **stats}
    marker.updated_at = now

    session.commit()
    logger.info(f"Overdue history backfill done: {stats}")
    return stats


def run_history_backfill_if_needed(days: int = HISTORY_BACKFILL_DAYS):
    """Entry point per lo startup (stile cases.run_backfill_if_needed):
    fallisce in silenzio operativo — log e retry al prossimo avvio."""
    from backend.database import get_session_direct
    session = get_session_direct()
    try:
        return backfill_overdue_history_if_needed(session, days=days)
    except Exception as e:
        session.rollback()
        logger.error(
            f"Overdue history backfill FAILED (will retry at next startup): {e}",
            exc_info=True,
        )
        return None
    finally:
        session.close()
