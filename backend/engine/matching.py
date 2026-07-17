"""Matching module — abbina le fatture ai clienti, con provenance.

Principi (nati dagli errori di abbinamento visti in produzione):
1. La P.IVA valida è l'unico identificatore affidabile — ma solo se UN SOLO
   cliente la possiede e il nome non è palesemente di un'altra azienda.
2. Il nome normalizzato abbina automaticamente solo se distintivo e UNIVOCO.
3. Il fuzzy NON abbina mai automaticamente: produce un SUGGERIMENTO in
   quarantena (suggested_*) che l'operatore conferma o rifiuta dalla UI.
4. Ogni abbinamento registra come è avvenuto (match_method, match_score).
5. Una fattura scollegata a mano (match_method='unlinked') non viene mai
   più abbinata automaticamente: solo suggerimenti.
"""

import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

from sqlalchemy.orm import Session

from backend.database import Invoice, Customer
from backend.engine.normalizer import (
    normalize_ragione_sociale, name_similarity_score,
    light_similarity_score,
)
from backend.engine.piva import validate_piva
from backend.config import config

logger = logging.getLogger(__name__)

# Sotto questa somiglianza nome-fattura vs nome-cliente, un match per P.IVA
# è sospetto (P.IVA probabilmente scrappata male) → quarantena.
PIVA_NAME_MISMATCH_THRESHOLD = 40

# Un nome normalizzato più corto di così non è abbastanza distintivo per un
# abbinamento automatico.
MIN_DISTINCTIVE_NAME_LEN = 4

# Sopra questa somiglianza il nome CONFERMA il candidato. Sotto, due insegne
# diverse collassate sulla stessa chiave normalizzata ('Osteria di Mario
# Rossi' / 'Osteria di Luigi Bianchi') → quarantena, decide l'operatore.
NAME_CONCORDANT_THRESHOLD = 75


def piva_contradiction(invoice: Invoice, customer: Optional[Customer]) -> bool:
    """True se la P.IVA della fattura e quella del cliente abbinato sono
    ENTRAMBE checksum-valide e DIVERSE: contraddizione deterministica,
    l'abbinamento è certamente sbagliato.

    Predicato unico condiviso da audit abbinamenti e repair pass: nessuna
    soglia di somiglianza coinvolta.
    """
    if customer is None:
        return False
    inv_piva = validate_piva(invoice.customer_piva_raw)
    cust_piva = validate_piva(customer.partita_iva)
    return bool(inv_piva and cust_piva and inv_piva != cust_piva)


@dataclass
class MatchResult:
    """Esito del matching di una fattura."""
    customer: Optional[Customer] = None  # abbinamento automatico sicuro
    method: Optional[str] = None         # piva / name_exact
    score: Optional[int] = None
    # Suggerimento in quarantena (mai auto-assegnato)
    suggested_customer: Optional[Customer] = None
    suggested_method: Optional[str] = None  # fuzzy / piva_ambiguous / piva_name_mismatch / name_ambiguous
    suggested_score: Optional[int] = None


def match_invoice_to_customer(
    invoice: Invoice,
    customers: List[Customer],
    session: Session,
    advisory: bool = False,
) -> MatchResult:
    """Match di una fattura contro la lista clienti.

    Ritorna sempre un MatchResult: o un abbinamento automatico sicuro
    (customer valorizzato), o un suggerimento in quarantena, o niente.

    advisory=True declassa i log a DEBUG: il repair ricorrente ri-esamina
    ogni fattura abbinata a OGNI sync e le stesse righe INFO/WARNING
    ripetute per centinaia di fatture sane intaserebbero i log Render.
    """
    result = MatchResult()
    log_info = logger.debug if advisory else logger.info
    log_warn = logger.debug if advisory else logger.warning

    inv_piva = validate_piva(invoice.customer_piva_raw)
    inv_name = (invoice.customer_name_raw or "").strip()
    inv_name_norm = normalize_ragione_sociale(inv_name) if inv_name else ""

    if not inv_piva and not inv_name:
        log_warn(f"Invoice {invoice.invoice_number} has no customer data")
        return result

    # ── Strategia 1: P.IVA esatta ───────────────────────────────────
    if inv_piva:
        piva_matches = [
            c for c in customers
            if validate_piva(c.partita_iva) == inv_piva
        ]
        if len(piva_matches) == 1:
            candidate = piva_matches[0]
            # Guardia anti-poisoning: P.IVA uguale ma nome completamente
            # diverso = P.IVA probabilmente corrotta → quarantena.
            # Lo score è robusto ai nomi-persona: "MERCURI CHRISTIAN" è
            # concorde con "Dr. Gahe di Mercuri Christian".
            if inv_name and candidate.ragione_sociale:
                name_score = name_similarity_score(inv_name, candidate.ragione_sociale)
                if name_score < PIVA_NAME_MISMATCH_THRESHOLD:
                    log_warn(
                        f"Invoice {invoice.invoice_number}: P.IVA {inv_piva} matches "
                        f"'{candidate.ragione_sociale}' but names are dissimilar "
                        f"(score={name_score}) — quarantined"
                    )
                    result.suggested_customer = candidate
                    result.suggested_method = "piva_name_mismatch"
                    result.suggested_score = int(name_score)
                    return result
            result.customer = candidate
            result.method = "piva"
            result.score = 100
            log_info(
                f"Invoice {invoice.invoice_number} matched to "
                f"{candidate.ragione_sociale} by P.IVA {inv_piva}"
            )
            return result
        if len(piva_matches) > 1:
            # Più clienti con la stessa P.IVA (duplicati): decide l'operatore.
            best = piva_matches[0]
            if inv_name:
                best = max(
                    piva_matches,
                    key=lambda c: name_similarity_score(inv_name, c.ragione_sociale or ""),
                )
            result.suggested_customer = best
            result.suggested_method = "piva_ambiguous"
            result.suggested_score = 100
            log_warn(
                f"Invoice {invoice.invoice_number}: P.IVA {inv_piva} shared by "
                f"{len(piva_matches)} customers — quarantined"
            )
            return result

    # ── Strategia 2: nome normalizzato esatto ───────────────────────
    if inv_name_norm and len(inv_name_norm) >= MIN_DISTINCTIVE_NAME_LEN:
        name_matches = []
        for c in customers:
            cust_piva = validate_piva(c.partita_iva)
            # P.IVA in conflitto = entità diverse, mai un match
            if inv_piva and cust_piva and inv_piva != cust_piva:
                continue
            if normalize_ragione_sociale(c.ragione_sociale or "") == inv_name_norm:
                name_matches.append(c)
        if len(name_matches) == 1:
            candidate = name_matches[0]
            if inv_piva and not validate_piva(candidate.partita_iva):
                # La fattura HA una P.IVA valida ma il cliente candidato no:
                # il nome coincide ma l'identità non è verificabile (e la
                # Strategia 1 non ha trovato quella P.IVA su nessun cliente).
                # Un omonimo qui creerebbe un nuovo abbinamento sbagliato →
                # quarantena, decide l'operatore.
                result.suggested_customer = candidate
                result.suggested_method = "name_exact_piva_unverified"
                result.suggested_score = 100
                log_warn(
                    f"Invoice {invoice.invoice_number}: exact name match to "
                    f"'{candidate.ragione_sociale}' but invoice P.IVA {inv_piva} "
                    f"is not on the customer — quarantined"
                )
                return result
            # Il nome normalizzato coincide, ma la normalizzazione è
            # aggressiva (taglia forme legali e 'di Nome Cognome'): due
            # insegne diverse possono collassare sulla stessa chiave
            # ('Osteria di Mario Rossi' / 'Osteria di Luigi Bianchi').
            #
            # Qui serve lo scorer NON-strict (token_set), al contrario di
            # repair.py:271 — e non è un'incoerenza. La guardia scatta solo
            # quando le chiavi normalizzate sono GIÀ uguali: sotto quella
            # precondizione le uniche differenze possibili sono la forma
            # legale e 'di Nome Cognome', quindi
            #   - persona su UN lato solo (fattura 'SHU&SHU DI SHU KEI' vs
            #     cliente 'SHU&SHU') = ASSENZA d'informazione, non
            #     contraddizione → il subset-bonus la riconosce → 100 → ok;
            #   - persone su ENTRAMBI i lati e DIVERSE = contraddizione →
            #     niente subset → 65-71 → quarantena.
            # Misurato: con strict le due popolazioni si sovrappongono
            # (legit 56-73, collisioni 50-71: nessuna soglia le separa) e
            # l'83% delle ditte individuali legittime degraderebbe a
            # quarantena. Con token_set: legit 100, collisioni <=71.
            # repair.py usa strict a ragione: lì si decide se spostare VIA
            # una fattura già abbinata, e il conservatorismo non costa nulla.
            light = light_similarity_score(
                invoice.customer_name_raw or "",
                candidate.ragione_sociale or "",
            )
            if light < NAME_CONCORDANT_THRESHOLD:
                result.suggested_customer = candidate
                result.suggested_method = "name_ambiguous"
                result.suggested_score = light
                log_warn(
                    f"Invoice {invoice.invoice_number}: normalized name "
                    f"matches '{candidate.ragione_sociale}' but light score "
                    f"is {light} — quarantined"
                )
                return result
            result.customer = candidate
            result.method = "name_exact"
            result.score = 100
            log_info(
                f"Invoice {invoice.invoice_number} matched to "
                f"{candidate.ragione_sociale} by normalized name"
            )
            return result
        if len(name_matches) > 1:
            result.suggested_customer = name_matches[0]
            result.suggested_method = "name_ambiguous"
            result.suggested_score = 100
            log_warn(
                f"Invoice {invoice.invoice_number}: normalized name "
                f"'{inv_name_norm}' shared by {len(name_matches)} customers — quarantined"
            )
            return result

    # ── Strategia 3: fuzzy → SOLO suggerimento ──────────────────────
    # Lo score include il confronto 'light' (pattern 'di Nome Cognome'
    # conservato): la fattura intestata alla sola persona suggerisce
    # l'insegna completa invece di restare orfana.
    if inv_name:
        best_customer = None
        best_score = 0
        for c in customers:
            cust_piva = validate_piva(c.partita_iva)
            if inv_piva and cust_piva and inv_piva != cust_piva:
                continue
            score = name_similarity_score(inv_name, c.ragione_sociale or "")
            if score >= config.FUZZY_MATCH_THRESHOLD and score > best_score:
                best_customer = c
                best_score = score
        if best_customer:
            result.suggested_customer = best_customer
            result.suggested_method = "fuzzy"
            result.suggested_score = int(best_score)
            log_info(
                f"Invoice {invoice.invoice_number}: fuzzy suggestion "
                f"{best_customer.ragione_sociale} (score={best_score}) — needs confirmation"
            )
            return result

    logger.debug(
        f"Invoice {invoice.invoice_number} ({invoice.customer_name_raw}) could not be matched"
    )
    return result


def run_matching(session: Session) -> Dict[str, Any]:
    """Batch match delle fatture senza cliente.

    Abbina automaticamente solo i match sicuri (P.IVA univoca, nome esatto
    univoco); tutto il resto finisce in quarantena come suggerimento.
    """
    stats = {
        'matched_piva': 0,
        'matched_exact': 0,
        'suggested': 0,
        'unmatched': 0,
        'total': 0,
    }

    unmatched_invoices = session.query(Invoice).filter(
        Invoice.customer_id.is_(None)
    ).all()
    stats['total'] = len(unmatched_invoices)

    customers = session.query(Customer).all()
    if not customers:
        logger.warning("No customers found in database for matching")
        stats['unmatched'] = len(unmatched_invoices)
        return stats

    logger.info(
        f"Starting matching for {stats['total']} invoices against {len(customers)} customers"
    )

    for invoice in unmatched_invoices:
        result = match_invoice_to_customer(invoice, customers, session)

        # Una fattura scollegata a mano non viene mai più abbinata in
        # automatico: qualsiasi esito diventa al massimo un suggerimento.
        if invoice.match_method == "unlinked" and result.customer is not None:
            result.suggested_customer = result.customer
            result.suggested_method = result.method
            result.suggested_score = result.score
            result.customer = None
            result.method = None

        # Un suggerimento SOLO-fuzzy su una fattura scollegata/rifiutata a
        # mano è già stato respinto una volta: non riproporlo a ogni sync
        # ("non verrà più riproposta in automatico" deve essere vero).
        if invoice.match_method == "unlinked" and result.suggested_method == "fuzzy":
            result.suggested_customer = None
            result.suggested_method = None
            result.suggested_score = None

        if result.customer is not None:
            invoice.customer_id = result.customer.id
            invoice.match_method = result.method
            invoice.match_score = result.score
            invoice.suggested_customer_id = None
            invoice.suggested_method = None
            invoice.suggested_score = None
            if result.method == "piva":
                stats['matched_piva'] += 1
            else:
                stats['matched_exact'] += 1
        elif result.suggested_customer is not None:
            invoice.suggested_customer_id = result.suggested_customer.id
            invoice.suggested_method = result.suggested_method
            invoice.suggested_score = result.suggested_score
            stats['suggested'] += 1
        else:
            # Pulisce eventuali suggerimenti stali di run precedenti
            # (es. cliente suggerito poi rimosso/mergiato).
            invoice.suggested_customer_id = None
            invoice.suggested_method = None
            invoice.suggested_score = None
            stats['unmatched'] += 1

    session.commit()

    logger.info(
        f"Matching complete: {stats['matched_piva']} P.IVA, "
        f"{stats['matched_exact']} exact, {stats['suggested']} suggested (quarantine), "
        f"{stats['unmatched']} unmatched"
    )
    return stats
