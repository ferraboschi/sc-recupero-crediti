"""Repair pass RICORRENTE degli abbinamenti fattura→cliente.

Ripara i danni lasciati dal vecchio motore di matching (i casi
"fattura di QOQA sul profilo Rooftop"), che il rework ha smesso di
produrre ma non ha mai corretto retroattivamente: run_matching processa
solo fatture con customer_id NULL, quindi gli errori pre-rework restavano
congelati per sempre.

Passi:
1. DETACH deterministico — fatture il cui abbinamento è CONTRADDETTO dalla
   P.IVA (entrambe checksum-valide e diverse, predicato piva_contradiction):
   scollegate con match_method=None (NON 'unlinked': quello è il freno delle
   decisioni umane e bloccherebbe per sempre il riabbinamento automatico).
   Escluse le decisioni esplicite dell'operatore (manual/fuzzy_confirmed/
   unlinked). Include le PAGATE: una pagata mal attribuita inquina i totali
   storici del profilo e scollegarla è innocuo per i solleciti.
2. RE-MATCH advisory degli abbinamenti MACCHINA rimasti (legacy, piva,
   name_exact, auto_created, …): se il motore NUOVO concorda → provenance
   'legacy' promossa all'esito reale; se troverebbe con certezza (P.IVA
   univoca, o name_exact univoco confermato dal nome 'light') un cliente
   DIVERSO → detach (il riabbinamento passa comunque dal matching sicuro);
   ogni altro disaccordo → solo ActivityLog di review, l'abbinamento non
   si tocca.
3. run_matching — riabbina subito le fatture scollegate (P.IVA univoca o
   quarantena: mai un riabbinamento cieco). Loop-safe by-design: la
   Strategia 1 abbina per la P.IVA DELLA FATTURA, che per costruzione del
   predicato contraddice quella del vecchio cliente.
4. Riconciliazione dei clienti che hanno perso fatture: pratiche rimaste
   senza scadute chiuse ('no_overdue', riapribile), stato-cache refreshato.

Il repair gira a OGNI full sync con enrichment completo: i passi sono
idempotenti (detach solo su evidenza deterministica, review deduplicate
via ActivityLog). Il marker SyncState registra solo l'ultima esecuzione
per l'osservabilità — non è più un gate one-shot: le contraddizioni che
emergono nei cicli successivi (P.IVA arricchite dopo, clienti creati
dopo) vengono riparate al giro successivo invece di restare congelate.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from backend.database import (
    Invoice, Customer, SyncState, ActivityLog,
)
from backend.engine.matching import (
    match_invoice_to_customer, piva_contradiction, run_matching,
    PIVA_NAME_MISMATCH_THRESHOLD, NAME_CONCORDANT_THRESHOLD,
)
from backend.engine.cases import (
    close_case, get_open_case, is_overdue_unpaid, _refresh_customer_status,
)
from backend.engine.normalizer import (
    name_similarity_score, light_similarity_score_strict,
)
from backend.engine.piva import validate_piva

logger = logging.getLogger(__name__)

# Abbinamenti decisi esplicitamente da un operatore: mai toccati dal repair.
HUMAN_DECIDED_METHODS = ("manual", "fuzzy_confirmed", "unlinked")

# Marker di osservabilità (ultima esecuzione + stats). La key resta 'v2'
# per continuità con lo storico in produzione.
REPAIR_MARKER_KEY = "match_repair_v2"


def _name_score_vs_customer(invoice: Invoice, customer: Customer):
    """Somiglianza nome-fattura vs cliente attuale, o None se non calcolabile.

    Usa lo score robusto ai nomi-persona: la fattura di una ditta
    individuale intestata alla sola persona è CONCORDE con l'insegna
    completa 'X di Nome Cognome' (mai un detach su quella base).
    """
    inv_name = (invoice.customer_name_raw or "").strip()
    if not inv_name or not customer.ragione_sociale:
        return None
    return name_similarity_score(inv_name, customer.ragione_sociale)


# Chiavi dei details che identificano una situazione di review: se un log
# della stessa azione con gli stessi valori esiste già, la review è nota.
_REVIEW_IDENTITY_KEYS = (
    "old_customer_id", "new_customer_id", "suggested_customer_id",
    "disagree_customer_id", "customer_id", "invoice_piva", "customer_piva",
)


def _log_review(session, action: str, invoice: Invoice, details: dict,
                stats: Dict[str, Any], stat_key: str) -> None:
    """Registra una review contando SEMPRE nelle stats ma scrivendo
    l'ActivityLog una sola volta: il repair è ricorrente e la stessa
    situazione non deve rilogggarsi a ogni sync."""
    stats[stat_key] += 1
    existing = session.query(ActivityLog).filter_by(
        action=action, entity_type="invoice", entity_id=invoice.id,
    ).all()
    for entry in existing:
        prev = entry.details or {}
        if all(
            prev.get(k) == details.get(k)
            for k in _REVIEW_IDENTITY_KEYS
            if k in prev or k in details
        ):
            return
    session.add(ActivityLog(
        action=action, entity_type="invoice", entity_id=invoice.id,
        details=details,
    ))


def _detach(invoice: Invoice) -> None:
    """Scollega la fattura rimandandola al matching sicuro."""
    invoice.customer_id = None
    invoice.case_id = None
    invoice.match_method = None
    invoice.match_score = None
    invoice.suggested_customer_id = None
    invoice.suggested_method = None
    invoice.suggested_score = None
    invoice.updated_at = datetime.utcnow()


def repair_matches(session: Session) -> Dict[str, Any]:
    """Esegue il repair (ricorrente: il marker registra solo l'ultima run)."""
    marker = session.query(SyncState).filter_by(key=REPAIR_MARKER_KEY).first()

    now = datetime.utcnow()
    stats = {
        "piva_conflict_detached": 0,
        "piva_conflict_review": 0,
        "legacy_promoted": 0,
        "legacy_piva_relink_detached": 0,
        "name_exact_relink_detached": 0,
        "legacy_review_logged": 0,
        "no_candidate_review": 0,
        "cases_closed": 0,
        "customers_reconciled": 0,
    }
    touched_customer_ids = set()

    # Include le pagate: il passo 1 le può scollegare (ripulisce i totali
    # storici del profilo sbagliato); il passo 2 le salta.
    attached = (
        session.query(Invoice)
        .filter(Invoice.customer_id.isnot(None))
        .all()
    )
    cust_ids = {inv.customer_id for inv in attached}
    customers_by_id = {}
    if cust_ids:
        for c in session.query(Customer).filter(Customer.id.in_(cust_ids)).all():
            customers_by_id[c.id] = c
    all_customers = session.query(Customer).all()

    # ── Passo 1: detach su contraddizione P.IVA confermata dal nome ──
    # La contraddizione P.IVA da sola NON basta: il valore avvelenato può
    # stare sulla FATTURA (customer_piva_raw scrappato dal full-text prima
    # delle guardie: es. la P.IVA del venditore), nel qual caso
    # l'abbinamento per nome è quello giusto e scollegarlo lo
    # distruggerebbe. Si scollega solo quando anche il NOME dice che la
    # fattura è di qualcun altro (score < soglia anti-poisoning); ogni
    # altra contraddizione va in review manuale.
    for inv in attached:
        if inv.match_method in HUMAN_DECIDED_METHODS:
            continue
        cust = customers_by_id.get(inv.customer_id)
        if cust is None or not piva_contradiction(inv, cust):
            continue
        name_score = _name_score_vs_customer(inv, cust)
        details = {
            "invoice_number": inv.invoice_number,
            "old_customer_id": cust.id,
            "old_customer_name": cust.ragione_sociale,
            "invoice_piva": validate_piva(inv.customer_piva_raw),
            "customer_piva": validate_piva(cust.partita_iva),
            "old_match_method": inv.match_method,
            "name_score": name_score,
        }
        if name_score is not None and name_score < PIVA_NAME_MISMATCH_THRESHOLD:
            session.add(ActivityLog(
                action="repair_piva_conflict",
                entity_type="invoice",
                entity_id=inv.id,
                details=details,
            ))
            _detach(inv)
            touched_customer_ids.add(cust.id)
            stats["piva_conflict_detached"] += 1
        else:
            # Nome concordante, ambiguo o assente: nessuna azione
            # automatica, emerge dall'audit (verdetto 'bad'/'warn') e
            # decide l'operatore.
            _log_review(
                session, "repair_piva_conflict_review", inv, details,
                stats, "piva_conflict_review",
            )

    # ── Passo 2: re-match advisory degli abbinamenti macchina ────────
    # Non solo 'legacy': anche piva/name_exact/auto_created sbagliati dal
    # vecchio codice restavano congelati per sempre (run_matching processa
    # solo customer_id NULL). Interviene solo su esiti deterministici;
    # tutto il resto è review deduplicata.
    for inv in attached:
        if inv.customer_id is None or inv.status == "paid":
            continue
        if inv.match_method in HUMAN_DECIDED_METHODS:
            continue
        cust = customers_by_id.get(inv.customer_id)
        if cust is None:
            continue
        if piva_contradiction(inv, cust):
            # Già gestita (o mandata in review) dal passo 1: qui un relink
            # aggirerebbe la guardia nome appena applicata.
            continue
        result = match_invoice_to_customer(inv, all_customers, session, advisory=True)
        if result.customer is not None and result.customer.id == inv.customer_id:
            # Il motore nuovo concorda: promozione della provenance (solo
            # per le 'legacy' — gli altri metodi sono già provenance vera).
            if inv.match_method == "legacy":
                inv.match_method = result.method
                inv.match_score = result.score
                stats["legacy_promoted"] += 1
        elif result.customer is not None and result.method == "piva":
            # Certezza P.IVA su un cliente DIVERSO (il vecchio cliente non
            # ha P.IVA, altrimenti sarebbe già scattato il passo 1).
            # Stessa cautela del passo 1: se il nome della fattura CONFERMA
            # il cliente attuale, la P.IVA della fattura è sospetta →
            # review, non relink.
            name_score = _name_score_vs_customer(inv, cust)
            log_details = {
                "invoice_number": inv.invoice_number,
                "old_customer_id": cust.id,
                "old_customer_name": cust.ragione_sociale,
                "new_customer_id": result.customer.id,
                "new_customer_name": result.customer.ragione_sociale,
                "name_score_vs_old": name_score,
            }
            if name_score is not None and name_score >= NAME_CONCORDANT_THRESHOLD:
                _log_review(
                    session, "repair_legacy_review", inv, log_details,
                    stats, "legacy_review_logged",
                )
            else:
                session.add(ActivityLog(
                    action="repair_legacy_piva_relink",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details=log_details,
                ))
                _detach(inv)
                touched_customer_ids.add(cust.id)
                stats["legacy_piva_relink_detached"] += 1
        elif result.customer is not None and result.method == "name_exact":
            # Il nome normalizzato della fattura coincide ESATTAMENTE e
            # UNIVOCAMENTE con un ALTRO cliente (e per costruzione non con
            # quello attuale): è il caso 'fattura YOHO sul profilo Domò'.
            # Guardia anti-collasso del normalizzatore: il nome 'light'
            # (pattern 'di Nome Cognome' conservato) deve confermare il
            # candidato, altrimenti due insegne diverse collassate sulla
            # stessa chiave ('Osteria di Mario Rossi' / 'Osteria di Luigi
            # Bianchi') produrrebbero un relink sbagliato. Serve lo scorer
            # STRICT (token_sort, non token_set): col subset-bonus il
            # collasso monolaterale ('Osteria di Mario Rossi' vs 'Osteria
            # SRL') varrebbe 100 e passerebbe la guardia.
            light = light_similarity_score_strict(
                inv.customer_name_raw or "",
                result.customer.ragione_sociale or "",
            )
            log_details = {
                "invoice_number": inv.invoice_number,
                "old_customer_id": cust.id,
                "old_customer_name": cust.ragione_sociale,
                "new_customer_id": result.customer.id,
                "new_customer_name": result.customer.ragione_sociale,
                "light_score_vs_new": light,
            }
            if light >= NAME_CONCORDANT_THRESHOLD:
                session.add(ActivityLog(
                    action="repair_name_exact_relink",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details=log_details,
                ))
                _detach(inv)
                touched_customer_ids.add(cust.id)
                stats["name_exact_relink_detached"] += 1
            else:
                _log_review(
                    session, "repair_legacy_review", inv, log_details,
                    stats, "legacy_review_logged",
                )
        elif result.customer is not None and result.customer.id != inv.customer_id:
            # Disaccordo AUTOMATICO di un metodo futuro non gestito sopra:
            # non si tocca nulla ma va tracciato per la review.
            _log_review(
                session, "repair_legacy_review", inv,
                {
                    "invoice_number": inv.invoice_number,
                    "customer_id": cust.id,
                    "customer_name": cust.ragione_sociale,
                    "disagree_customer_id": result.customer.id,
                    "disagree_method": result.method,
                },
                stats, "legacy_review_logged",
            )
        elif result.suggested_customer is not None and result.suggested_customer.id != inv.customer_id:
            # Il motore nuovo la vedrebbe diversamente ma senza certezza:
            # non si tocca nulla, si lascia traccia per la review manuale
            # (visibile anche nell'audit abbinamenti).
            _log_review(
                session, "repair_legacy_review", inv,
                {
                    "invoice_number": inv.invoice_number,
                    "customer_id": cust.id,
                    "customer_name": cust.ragione_sociale,
                    "suggested_customer_id": result.suggested_customer.id,
                    "suggested_method": result.suggested_method,
                    "suggested_score": result.suggested_score,
                },
                stats, "legacy_review_logged",
            )
        elif result.customer is None and result.suggested_customer is None:
            # Il motore nuovo non trova NULLA per questa fattura: se il
            # nome è anche dissimile dal cliente attuale l'abbinamento è
            # sospetto — prima era un buco silenzioso (nessuna azione,
            # nessun log), ora almeno emerge in review.
            name_score = _name_score_vs_customer(inv, cust)
            if name_score is not None and name_score < PIVA_NAME_MISMATCH_THRESHOLD:
                _log_review(
                    session, "repair_no_candidate_review", inv,
                    {
                        "invoice_number": inv.invoice_number,
                        "customer_id": cust.id,
                        "customer_name": cust.ragione_sociale,
                        "name_score": name_score,
                    },
                    stats, "no_candidate_review",
                )

    # Marker nello stesso commit del detach: o tutto o niente.
    if not marker:
        marker = SyncState(key=REPAIR_MARKER_KEY)
        session.add(marker)
    marker.last_sync = now
    marker.result = {"done": True, **stats}
    marker.updated_at = now
    # match_repair_done si logga solo quando la run ha MODIFICATO qualcosa
    # (detach/promozioni): il repair è ricorrente e le run passive — anche
    # con review pendenti già note — non devono intasare il log.
    mutated = (
        stats["piva_conflict_detached"]
        or stats["legacy_piva_relink_detached"]
        or stats["name_exact_relink_detached"]
        or stats["legacy_promoted"]
    )
    if mutated:
        session.add(ActivityLog(action="match_repair_done", details=dict(stats)))
    session.commit()

    # ── Passo 3: riabbinamento sicuro delle fatture scollegate ──────
    # (run_matching processa tutte le fatture con customer_id NULL e fa
    # commit da sé; se qui fallisce, il prossimo sync lo ripete comunque.)
    if (
        stats["piva_conflict_detached"]
        or stats["legacy_piva_relink_detached"]
        or stats["name_exact_relink_detached"]
    ):
        try:
            run_matching(session)
        except Exception as e:
            # Rollback esplicito: senza, la sessione resta invalidata e il
            # passo 4 (riconciliazione) salterebbe in blocco.
            logger.error(f"Post-repair matching failed (next sync will retry): {e}")
            try:
                session.rollback()
            except Exception:
                pass

    # ── Passo 4: riconciliazione dei clienti che hanno perso fatture ─
    for cust_id in touched_customer_ids:
        customer = session.query(Customer).filter_by(id=cust_id).first()
        if customer is None:
            continue
        reconcile_customer_after_detach(session, customer, stats)
        stats["customers_reconciled"] += 1
    session.commit()

    # Aggiorna il marker con le stats definitive (best-effort).
    marker.result = {"done": True, **stats}
    session.commit()

    logger.info(f"Match repair done: {stats}")
    return stats


def reconcile_customer_after_detach(
    session: Session,
    customer: Customer,
    stats: Optional[Dict[str, Any]] = None,
) -> None:
    """Riallinea pratica e stato-cache di un cliente che ha perso fatture.

    Usato dal repair pass e dallo Scollega manuale (prima, lo stato del
    vecchio cliente restava stantio fino al sync notturno).
    """
    case = get_open_case(session, customer.id)
    overdue_left = [
        inv for inv in session.query(Invoice).filter_by(customer_id=customer.id).all()
        if is_overdue_unpaid(inv)
    ]
    if case is not None:
        if not overdue_left:
            close_case(session, case, "no_overdue")
            if stats is not None:
                stats["cases_closed"] += 1
        else:
            _refresh_customer_status(session, customer, case)
    elif not overdue_left and customer.recovery_status in (
        "first_contact", "second_contact", "lawyer", "waiting"
    ):
        customer.recovery_status = "idle"
        customer.next_action_date = None
        customer.next_action_type = None
        customer.updated_at = datetime.utcnow()
