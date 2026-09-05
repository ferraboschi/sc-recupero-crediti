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
# 3b. in_incasso — pagata con ASSEGNO registrato dall'operatore, in attesa di
#    incasso e NON insoluta: il credito esiste ancora (FatturaPro la vede
#    aperta) ma non si insegue. Resta nell'universo, esce dal lavorabile.
OVERDUE_BUCKETS = ("non_abbinati", "esclusi", "contestati", "in_incasso", "lavorabile")

# Stati pratica di un cliente lavorabile (la cache su Customer.recovery_status).
# `sconosciuto` raccoglie NULL e valori imprevisti: senza di lui la somma
# degli stati non chiuderebbe sul lavorabile e avremmo ricostruito il bug.
CASE_STAGES = (
    "idle", "first_contact", "second_contact",
    "lawyer", "waiting", "archived", "sconosciuto",
)

# I tipi di azione che valgono come "sollecito" (contano nel recuperato).
# 'note'/'archive'/'wait' non sono contatti col debitore.
RECOVERY_ACTION_TYPES = ("first_contact", "second_contact", "lawyer")


def is_in_incasso(inv: Invoice) -> bool:
    """Assegno in mano NON insoluto su fattura NON pagata: fuori dal lavorabile,
    dentro l'universo. `status != paid` sta QUI (definizione unica): il sync
    che marca pagata per assenza non deve essere ricordato da ogni consumer."""
    return (
        inv.status != "paid"
        and bool(getattr(inv, "payment_pending", None))
        and getattr(inv, "bounced_at", None) is None
    )


def in_incasso_clause():
    """Gemello SQL di is_in_incasso."""
    return (
        (Invoice.status != "paid")
        & Invoice.payment_pending.isnot(None)
        & Invoice.bounced_at.is_(None)
    )


def is_overdue_unpaid(inv: Invoice) -> bool:
    """Fattura che tiene viva una pratica: scaduta, non pagata, non contestata,
    non in incasso (assegno in mano)."""
    return (
        inv.status not in ("paid", "disputed")
        and (inv.days_overdue or 0) > 0
        and not is_in_incasso(inv)
    )


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
        & ~in_incasso_clause()
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
        (in_incasso_clause(), "in_incasso"),
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
    ramo): scaduto_totale == non_abbinati + esclusi + contestati + in_incasso + lavorabile.
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
    # Importo arrotondato al centesimo già QUI, non solo sul totale: così
    # l'identità scaduto_totale == somma(bucket) regge anche col confronto
    # grezzo (===) di un frontend, senza rumore IEEE sub-centesimo che
    # farebbe lampeggiare "i conti non tornano" su una differenza di 1e-14.
    per_bucket = {b: {"fatture": 0, "importo": 0.0} for b in OVERDUE_BUCKETS}
    for bucket, fatture, importo in rows:
        per_bucket[bucket] = {
            "fatture": int(fatture or 0),
            "importo": round(float(importo or 0), 2),
        }
    totale = {
        "fatture": sum(b["fatture"] for b in per_bucket.values()),
        "importo": round(sum(b["importo"] for b in per_bucket.values()), 2),
    }
    return {"scaduto_totale": totale, **per_bucket}


def first_recovery_action_subquery(session):
    """Prima azione di recupero NON annullata, per cliente.

    Ritorna una subquery `(customer_id, first_action)` dove `first_action` è
    il MIN(created_at) fra i solleciti (RECOVERY_ACTION_TYPES) che il cliente
    ha davvero ricevuto.

    UNA definizione sola, condivisa da tutti i consumatori del "recuperato"
    (/riconciliazione, snapshot, /pipeline, /attivita): se ognuno se la
    riscrivesse a mano, prima o poi divergerebbero e un endpoint direbbe un
    numero e lo snapshot un altro.

    Filtra `cancelled IS NOT TRUE` (BUG 5a): un sollecito annullato è
    "registrato per errore" (lo dice /undo) — non è un primo sollecito, e il
    pagamento successivo non lo abbiamo recuperato noi. `isnot(True)` copre
    anche i NULL (colonna aggiunta via ALTER), come i 30+ punti che già
    filtrano così nel resto del codebase.
    """
    return (
        session.query(
            RecoveryAction.customer_id,
            func.min(RecoveryAction.created_at).label("first_action"),
        )
        .filter(
            RecoveryAction.action_type.in_(RECOVERY_ACTION_TYPES),
            RecoveryAction.cancelled.isnot(True),
        )
        .group_by(RecoveryAction.customer_id)
        .subquery()
    )


def recovered_invoice_clause(first_action_sq):
    """La fattura era già in circolo quando è partito il sollecito.

    BUG 5c: l'attribuzione per-CLIENTE (join sul solo customer_id) contava
    come "recuperata" una fattura NUOVA — emessa e pagata nei termini mesi
    dopo un vecchio sollecito. Ma una fattura emessa DOPO il primo sollecito
    non può essere ciò che stavamo recuperando.

    Discriminante scelto: `issue_date < first_action` (la data di emissione,
    non `invoice_ids`/`case_id`). Motivo:
    - `invoice_ids` è NULL su tutte le azioni ante-migrazione (colonna
      aggiunta via ALTER), e la PRIMA azione — quella che qui conta — è
      proprio la più vecchia, quindi la più spesso priva di invoice_ids:
      legarci il recupero farebbe evaporare i recuperi storici legittimi.
      In più un sollecito cita spesso un SOTTOINSIEME delle fatture scadute,
      quindi il legame sarebbe anche troppo stretto.
    - `Invoice.case_id` è volatile (azzerato a ogni scollega/repair, chiuso
      col ciclo): inaffidabile per le pagate storiche.
    - `issue_date` è scritta al sync dal documento ed è già la data con cui
      il motore data il ciclo di recupero (cases.py) — proxy robusto.

    issue_date NULL: la fattura NON evapora (`OR issue_date IS NULL`). Una
    riga legacy senza data di emissione è quasi sempre un recupero storico
    legittimo; una fattura NUOVA, invece, arriva dal sync con la sua data.
    Tenere i NULL protegge i recuperi legittimi (la regola: non evaporarli)
    e riapre il 5c solo per la rara coincidenza NULL-e-nuova-e-pagata.
    """
    return (
        (Invoice.issue_date < first_action_sq.c.first_action)
        | Invoice.issue_date.is_(None)
    )


def compute_recuperato_certo(session):
    """Recuperato CERTO, cumulato: fatture pagate DOPO il primo sollecito, a
    residuo (amount_due_at_paid), con paid_at valorizzata, ed emesse PRIMA
    del sollecito.

    Ritorna la coppia (fatture, importo). È la stessa definizione che
    /riconciliazione espone come `recuperato.certo`: condividerla tiene lo
    snapshot storico allineato al numero live, senza una seconda copia della
    query che poi diverge.

    NB: solo il "certo" (paid_at valorizzata). Lo "storico stimato"
    ante-migrazione resta un affare di /riconciliazione — non entra nella
    serie, dove sommeremmo mele e pere.
    """
    first_action = first_recovery_action_subquery(session)
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
            recovered_invoice_clause(first_action),
        )
        .one()
    )
    return int(certo[0] or 0), float(certo[1] or 0.0)


def compute_in_incasso_assegni(session):
    """Recuperato "in incasso da assegni": assegni in mano (non insoluti, non
    ancora pagati su FatturaPro) registrati DOPO il primo sollecito, su fatture
    emesse PRIMA (stessa attribuzione del certo). Somma del residuo alla
    registrazione. È la sotto-voce del recuperato decisa dall'owner (Q2):
    conta dalla registrazione, ma NON si mescola alla cassa vera; lo storno
    all'insoluto è automatico (bounced_at → esce dalla clausola).
    Ritorna (fatture, importo).
    """
    first_action = first_recovery_action_subquery(session)
    # Importo = residuo VIVO (amount_due, riscritto dal sync): lo stesso che
    # la cascata mette nel bucket e che il "certo" contabilizzerà all'incasso
    # (amount_due_at_paid). payment_pending_amount resta come audit. Stessa
    # gerarchia della cascata: solo scadute, non contestate, cliente attivo.
    row = (
        session.query(
            func.count(Invoice.id),
            func.sum(func.coalesce(Invoice.amount_due, 0.0)),
        )
        .join(first_action, Invoice.customer_id == first_action.c.customer_id)
        .join(Customer, Invoice.customer_id == Customer.id)
        .filter(
            in_incasso_clause(),
            overdue_clause(),
            Invoice.status != "disputed",
            Customer.excluded.isnot(True),
            Invoice.payment_pending_at >= first_action.c.first_action,
            recovered_invoice_clause(first_action),
        )
        .one()
    )
    return int(row[0] or 0), float(row[1] or 0.0)
