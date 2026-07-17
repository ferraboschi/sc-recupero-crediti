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

from sqlalchemy import case

from backend.database import Invoice, Customer

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
