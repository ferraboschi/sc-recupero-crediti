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

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from backend.database import Invoice, Customer
from backend.engine.normalizer import (
    normalize_ragione_sociale, name_similarity_score,
    light_similarity_score, person_part_of,
    legal_forms_of, is_ditta_individuale,
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

# Le persone devono concordare quasi alla lettera: tolleriamo l'ordine
# (Rossi Mario / Mario Rossi) e i refusi, non un titolare diverso.
PERSON_CONCORDANT_THRESHOLD = 90


def _distinct_legal_entities(name_a: str, name_b: str) -> bool:
    """True se i due nomi NON possono essere la stessa entità giuridica:
    uno è una ditta individuale (titolare esplicito + nessuna forma legale),
    l'altro dichiara una forma societaria.

    Perché è deterministico e non una soglia: una ditta individuale non è un
    soggetto distinto dal suo titolare (la P.IVA è attribuita alla persona
    fisica), una società di capitali sì. Sono due soggetti diversi, con due
    P.IVA diverse, SEMPRE — anche quando l'azienda è la stessa nei fatti e
    si è "trasformata": il passaggio ditta individuale → Srl è un
    CONFERIMENTO d'azienda in una società nuova (non una trasformazione ex
    art. 2498 c.c., che riguarda solo società fra loro), quindi apre una
    P.IVA nuova e chiude quella del titolare. Le fatture dei due soggetti
    non appartengono allo stesso profilo, e i solleciti nemmeno.

    Serve informazione POSITIVA su entrambi i lati, e per questo la guardia
    è stretta:
    - 'Gaijin di Fois Stefano' (ditta indiv.) vs 'Gaijin Srl' (società) → True
    - 'SHU&SHU DI SHU KEI S.A.S.' vs 'SHU&SHU S.A.S.' → False: il lato col
      titolare dichiara la S.A.S., è una società di persone (che il socio
      nella ragione sociale ce l'ha per obbligo), non una ditta individuale.
    - 'Fronte Mare' vs 'Fronte Mare Srl' → False: il lato nudo non afferma
      di essere una ditta individuale, ha solo la forma omessa — è assenza
      d'informazione, il caso normalissimo dei record abbreviati.
    - 'Trattoria Da Gino SNC' vs 'Trattoria Da Gino Srl' → False: due
      società; la trasformazione SNC→Srl CONSERVA la P.IVA (stesso
      soggetto, art. 2498 c.c.). Confrontare le forme fra società sarebbe
      sbagliato, ed è il motivo per cui questa guardia NON lo fa.
    """
    if is_ditta_individuale(name_a) and legal_forms_of(name_b):
        return True
    if is_ditta_individuale(name_b) and legal_forms_of(name_a):
        return True
    return False


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
    # fuzzy / piva_ambiguous / piva_name_mismatch / name_ambiguous /
    # name_exact_piva_unverified / legal_form_conflict
    suggested_method: Optional[str] = None
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
        legal_conflicts = []
        for c in customers:
            cust_piva = validate_piva(c.partita_iva)
            # P.IVA in conflitto = entità diverse, mai un match
            if inv_piva and cust_piva and inv_piva != cust_piva:
                continue
            if normalize_ragione_sociale(c.ragione_sociale or "") == inv_name_norm:
                # La chiave normalizzata butta via la forma legale: 'Gaijin
                # di Fois Stefano' e 'Gaijin Srl' collassano entrambi su
                # 'gaijin'. Una ditta individuale e una società però non
                # sono lo stesso soggetto: candidato scartato qui, così se
                # esiste ANCHE il profilo giusto ('Gaijin Srl') la fattura
                # ci finisce da sola invece di finire in quarantena.
                if _distinct_legal_entities(inv_name, c.ragione_sociale or ""):
                    legal_conflicts.append(c)
                else:
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
            # Quando ENTRAMBI i lati portano una persona, è LA PERSONA a
            # dover concordare: il confronto sul nome intero non serve,
            # perché l'insegna condivisa diluisce la differenza fra i due
            # titolari (stesse persone diverse: 'Osteria' → 65, 'Antica
            # Osteria del Borgo' → 81) e il subset-bonus la azzera del tutto
            # quando un nome è annidato nell'altro ('Wang Li' / 'Wang Li
            # Hua', normale nella traslitterazione cinese → 100).
            # token_sort, non token_set: niente subset-bonus. Invariante
            # alla lunghezza dell'insegna, perché confronta solo le persone.
            p_inv = person_part_of(invoice.customer_name_raw)
            p_cand = person_part_of(candidate.ragione_sociale)
            if p_inv and p_cand:
                person_score = int(fuzz.token_sort_ratio(p_inv, p_cand))
                if person_score < PERSON_CONCORDANT_THRESHOLD:
                    result.suggested_customer = candidate
                    result.suggested_method = "name_ambiguous"
                    result.suggested_score = person_score
                    log_warn(
                        f"Invoice {invoice.invoice_number}: normalized name "
                        f"matches '{candidate.ragione_sociale}' but the "
                        f"owners differ ('{p_inv}' vs '{p_cand}', "
                        f"score={person_score}) — quarantined"
                    )
                    return result

            # Rete di sicurezza, per i casi che la guardia sopra non copre
            # (persona assente su almeno un lato). NON è questa a fermare
            # due titolari diversi: il suo scorer è token_set e la sua
            # soglia è tarata sul nome intero, quindi si lascia diluire
            # dall'insegna — è esattamente il motivo per cui esiste la
            # guardia sulle persone.
            #
            # Scorer NON-strict (token_set), al contrario di repair.py:271 —
            # e non è un'incoerenza. La guardia scatta solo quando le chiavi
            # normalizzate sono GIÀ uguali: sotto quella precondizione le
            # uniche differenze possibili sono la forma legale e 'di Nome
            # Cognome'. Con la persona su UN lato solo ('SHU&SHU DI SHU KEI'
            # vs 'SHU&SHU') si ha ASSENZA d'informazione, non
            # contraddizione: il subset-bonus la riconosce → 100 → ok.
            # Con lo strict quel caso legittimo varrebbe 56 e l'83% delle
            # ditte individuali degraderebbe a quarantena; repair.py usa
            # strict a ragione, perché lì si decide se spostare VIA una
            # fattura già abbinata e il conservatorismo non costa nulla.
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
        if legal_conflicts:
            # Nessun candidato compatibile, ma un omonimo di forma giuridica
            # incompatibile c'è: NON è un match, però lasciare la fattura
            # orfana la rende invisibile (è il difetto della segnalazione #5).
            # Suggerimento esplicito: l'operatore conferma, o — quasi sempre
            # la mossa giusta — usa "Crea nuovo cliente" dalla quarantena.
            result.suggested_customer = legal_conflicts[0]
            result.suggested_method = "legal_form_conflict"
            result.suggested_score = 100
            log_warn(
                f"Invoice {invoice.invoice_number}: normalized name matches "
                f"'{legal_conflicts[0].ragione_sociale}' but one side is a "
                f"ditta individuale and the other a company — quarantined"
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
