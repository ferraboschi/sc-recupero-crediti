"""Storico dello scaduto: uno snapshot al giorno per il grafico di evoluzione.

La dashboard fotografa solo l'istante presente. Qui si persiste la cascata
giorno per giorno, così l'evoluzione dello scaduto — totale, lavorabile,
recuperato — diventa una serie storica.

Modulo DEDICATO apposta: `record_overdue_snapshot` ha bisogno sia degli
helper di engine/overdue.py sia di business_day_start (engine/cases.py), e
cases.py importa già overdue.py. Mettere qui la scrittura evita l'import
circolare che nascerebbe importando cases dentro overdue.
"""

import logging

from backend.database import OverdueSnapshot
from backend.engine.overdue import (
    compute_overdue_buckets, compute_recuperato_certo,
)
from backend.engine.cases import business_day_start

logger = logging.getLogger(__name__)


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
