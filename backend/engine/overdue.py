"""Cosa vuol dire "scaduto". Una definizione sola, per tutti.

Il sistema ne aveva due, incompatibili: `is_overdue_unpaid` (che esclude i
contestati, usata dal motore in 11 punti) e `days_overdue > 0` ricopiata a
mano nelle query aggregate (che li include, 29 punti). Da lì i numeri della
home che non tornavano e i todo che non si potevano chiudere — il KPI
proponeva un cliente che il motore poi rifiutava.

Qui ci sono DUE concetti, e sono diversi apposta:

- **universo** (`overdue_clause`): tutto ciò che è scaduto e non pagato,
  contestati/esclusi/orfane comprese. È la CIMA della cascata: il credito
  esiste comunque, va solo classificato.
- **lavorabile** (`workable_clause`, `is_overdue_unpaid`): lo scaduto che il
  motore insegue davvero. Se un KPI propone un'azione all'operatore, deve
  parlare questa lingua, o promette lavoro che il motore rifiuterà.

La riconciliazione è il ponte fra i due: `bucket_expr` assegna ogni riga
dell'universo a UNA categoria, e la somma delle categorie ridà l'universo.
"""

from sqlalchemy import case, func

from backend.database import Invoice, Customer, RecoveryAction

# Ordine di PRECEDENZA delle categorie della cascata. Le condizioni si
# sovrappongono nella realtà (una fattura può essere orfana E contestata,
# un cliente escluso può avere fatture contestate): senza una precedenza
# le categorie si conterebbero due volte e la cascata non chiuderebbe.
#
# L'ordine non è arbitrario — è quello che il motore già applica in
# `cases.update_case_lifecycle`:
# 1. non_abbinati — senza cliente la domanda "è escluso?" non ha risposta:
#    è l'unica categoria definibile senza un cliente, quindi viene prima.
# 2. esclusi — decisione sul RAPPORTO col cliente (non lo inseguiamo, punto).
#    Il lifecycle guarda `customer.excluded` PRIMA di ogni stato fattura.
# 3. contestati — fatto su una SINGOLA fattura: se il cliente è già escluso,
#    che una sua fattura sia contestata non cambia nulla.
# 4. lavorabile — quel che resta: esattamente ciò che il motore lavora.
OVERDUE_BUCKETS = ("non_abbinati", "esclusi", "contestati", "lavorabile")

# Stati pratica di un cliente lavorabile (la cache su Customer.recovery_status).
# `sconosciuto` raccoglie NULL e valori imprevisti: senza di lui la somma
# degli stati non chiuderebbe sul lavorabile e avremmo ricostruito il bug.
CASE_STAGES = (
    "idle", "first_contact", "second_contact",
    "lawyer", "waiting", "archived", "sconosciuto",
)


def is_overdue_unpaid(inv: Invoice) -> bool:
    """Fattura che tiene viva una pratica: scaduta, non pagata, non contestata."""
    return inv.status not in ("paid", "disputed") and (inv.days_overdue or 0) > 0


def overdue_clause():
    """UNIVERSO: scaduta e non pagata. La cima della cascata."""
    return (Invoice.status != "paid") & (Invoice.days_overdue > 0)


def workable_clause():
    """LAVORABILE in SQL: l'equivalente aggregato di `is_overdue_unpaid`
    più le condizioni sul cliente.

    Richiede un join (anche outer) su Customer.
    """
    return (
        overdue_clause()
        & Invoice.customer_id.isnot(None)
        & (Invoice.status != "disputed")
        & Customer.excluded.isnot(True)
    )


def bucket_expr():
    """Categoria della cascata per una riga dell'universo.

    Un CASE SQL sceglie UN solo ramo per riga: la mutua esclusività è
    strutturale, non una promessa aritmetica. È ciò che rende l'identità
    `universo == somma(categorie)` vera per costruzione.

    Richiede un outerjoin su Customer (le orfane non hanno cliente).
    """
    return case(
        (Invoice.customer_id.is_(None), "non_abbinati"),
        (Customer.excluded.is_(True), "esclusi"),
        (Invoice.status == "disputed", "contestati"),
        else_="lavorabile",
    )


def stage_expr():
    """Stato pratica di un cliente lavorabile, con i NULL raccolti.

    Richiede un outerjoin su Customer.
    """
    return case(
        (Customer.recovery_status.in_(CASE_STAGES[:-1]), Customer.recovery_status),
        else_="sconosciuto",
    )


def compute_overdue_buckets(session) -> dict:
    """I bucket della cascata come DICT (non come query): importo e conteggio
    fatture per categoria, più lo `scaduto_totale` (la cima).

    È la STESSA, identica query di /riconciliazione. Condividere questa
    funzione è ciò che impedisce alla serie storica (snapshot) e alla cascata
    live di divergere: c'è una definizione sola, e le legge tutte e due.

    Ritorna:
        {"scaduto_totale": {"fatture": int, "importo": float},
         "non_abbinati":   {...}, "esclusi": {...},
         "contestati":     {...}, "lavorabile": {...}}

    L'identità vale per costruzione (bucket_expr assegna ogni riga a un solo
    ramo): scaduto_totale == non_abbinati + esclusi + contestati + lavorabile.
    """
    rows = (
        session.query(
            bucket_expr().label("bucket"),
            func.count(Invoice.id).label("fatture"),
            func.sum(Invoice.amount_due).label("importo"),
        )
        .outerjoin(Customer, Invoice.customer_id == Customer.id)
        .filter(overdue_clause())
        .group_by(bucket_expr())
        .all()
    )
    per_bucket = {b: {"fatture": 0, "importo": 0.0} for b in OVERDUE_BUCKETS}
    for bucket, fatture, importo in rows:
        per_bucket[bucket] = {
            "fatture": int(fatture or 0),
            "importo": float(importo or 0),
        }
    totale = {
        "fatture": sum(b["fatture"] for b in per_bucket.values()),
        "importo": round(sum(b["importo"] for b in per_bucket.values()), 2),
    }
    return {"scaduto_totale": totale, **per_bucket}


def compute_recuperato_certo(session):
    """Recuperato CERTO, cumulato: fatture pagate DOPO il primo sollecito, a
    residuo (amount_due_at_paid), con paid_at valorizzata.

    Ritorna la coppia (fatture, importo). È la stessa definizione che
    /riconciliazione espone come `recuperato.certo`: condividerla tiene lo
    snapshot storico allineato al numero live, senza una seconda copia della
    query che poi diverge.

    NB: solo il "certo" (paid_at valorizzata). Lo "storico stimato"
    ante-migrazione resta un affare di /riconciliazione — non entra nella
    serie, dove sommeremmo mele e pere.
    """
    first_action = (
        session.query(
            RecoveryAction.customer_id,
            func.min(RecoveryAction.created_at).label("first_action"),
        )
        .filter(
            RecoveryAction.action_type.in_(
                ["first_contact", "second_contact", "lawyer"]
            )
        )
        .group_by(RecoveryAction.customer_id)
        .subquery()
    )
    certo = (
        session.query(
            func.count(Invoice.id),
            func.sum(func.coalesce(Invoice.amount_due_at_paid, 0.0)),
        )
        .join(first_action, Invoice.customer_id == first_action.c.customer_id)
        .filter(
            Invoice.status == "paid",
            Invoice.paid_at.isnot(None),
            Invoice.paid_at >= first_action.c.first_action,
        )
        .one()
    )
    return int(certo[0] or 0), float(certo[1] or 0.0)
