"""Verifica puntuale fattura ↔ cliente (P.IVA + ragione sociale).

Controllo VELOCISSIMO e SENZA sync: confronta valori già presenti nel DB
(fattura: customer_piva_raw / customer_name_raw; cliente: partita_iva /
ragione_sociale). Nessuna chiamata esterna → millisecondi.

Fonte di verità unica per:
- il semaforo per-riga sul dettaglio cliente (frontend);
- l'audit abbinamenti (pagina Sistema).

Tre livelli, onesti per costruzione (il verde compare SOLO quando la
corrispondenza è davvero garantita):
- verified  : P.IVA presente su entrambi, checksum-valida, UGUALE, e
              ragione sociale coincidente. → la garanzia esplicita.
- warning   : manca la P.IVA da un lato, o i nomi sono simili ma non
              coincidenti (verifica manuale consigliata).
- critical  : P.IVA in contraddizione (entrambe valide e diverse), nomi
              dissimili, o P.IVA coincidente su nomi dissimili (possibile
              P.IVA avvelenata).
"""

from typing import Any, Dict, Optional

from backend.engine.normalizer import (
    name_similarity_score, normalize_ragione_sociale,
)
from backend.engine.piva import validate_piva, is_checksum_backed

# Sotto questa somiglianza i nomi sono "dissimili" → critico.
NAME_DISSIMILAR_THRESHOLD = 40
# Sotto questa somiglianza i nomi sono "poco simili" → avviso.
NAME_WEAK_THRESHOLD = 75
# A questa (o sopra) i nomi coincidono: uguaglianza normalizzata o
# token_set_ratio pieno (permutazioni/insegna+persona della stessa entità).
NAME_EQUIVALENT_SCORE = 100


def verify_invoice_customer(
    invoice: Any,
    customer: Optional[Any],
) -> Dict[str, Any]:
    """Confronta la P.IVA e la ragione sociale della fattura con quelle del
    cliente. Ritorna un dizionario pronto per l'API/UI.

    `customer` può essere None (fattura senza cliente): in tal caso il
    confronto non è possibile e il livello è 'warning'.
    """
    # Valori GREZZI (non strip-pati): la presenza del nome si valuta sul
    # grezzo, come faceva l'audit precedente — un nome di soli spazi era
    # "presente" (score 0), non "assente"; strip-parlo prima cambierebbe il
    # verdetto. name_similarity_score normalizza internamente.
    inv_name_src = getattr(invoice, "customer_name_raw", None) or ""
    cust_name_src = (getattr(customer, "ragione_sociale", None) or "") if customer else ""
    inv_piva_raw = (getattr(invoice, "customer_piva_raw", None) or "").strip()
    cust_piva_raw = (getattr(customer, "partita_iva", None) or "").strip() if customer else ""
    # Versioni pulite solo per il DISPLAY affiancato.
    inv_name_raw = inv_name_src.strip()
    cust_name_raw = cust_name_src.strip()

    inv_piva = validate_piva(inv_piva_raw)
    cust_piva = validate_piva(cust_piva_raw)

    piva_match = bool(inv_piva and cust_piva and inv_piva == cust_piva)
    piva_conflict = bool(inv_piva and cust_piva and inv_piva != cust_piva)
    # Garanzia forte (verde) solo con checksum reale: le P.IVA estere
    # passano il solo formato, non basta a "garantire" l'identità.
    piva_match_guaranteed = piva_match and is_checksum_backed(inv_piva)

    name_score: Optional[int] = None
    name_equivalent = False
    if inv_name_src and cust_name_src:
        name_score = name_similarity_score(inv_name_src, cust_name_src)
        name_equivalent = (
            name_score >= NAME_EQUIVALENT_SCORE
            or normalize_ragione_sociale(inv_name_src)
            == normalize_ragione_sociale(cust_name_src)
        )

    _score = name_score if name_score is not None else 0

    # ── Verdetto AUDIT (rileva i problemi; P.IVA uguale = ok anche con
    # nome non identico, perché la P.IVA è l'identificatore forte) ──────
    if customer is None:
        verdict = "warn"
    elif piva_conflict:
        verdict = "bad"
    elif piva_match and name_score is not None and name_score < NAME_DISSIMILAR_THRESHOLD:
        verdict = "bad"   # P.IVA coincidente ma nomi dissimili: possibile avvelenata
    elif name_score is not None and name_score < NAME_DISSIMILAR_THRESHOLD and not piva_match:
        verdict = "bad"
    elif name_score is not None and name_score < NAME_WEAK_THRESHOLD and not piva_match:
        verdict = "warn"
    elif inv_piva and not cust_piva:
        verdict = "warn"
    elif name_score is None and not piva_match:
        verdict = "warn"
    else:
        verdict = "ok"

    # ── Livello SEMAFORO (severo: il verde è una GARANZIA, non basta
    # l'assenza di problemi) — richiede P.IVA con CHECKSUM reale ────────
    if verdict == "bad":
        level = "critical"
    elif piva_match_guaranteed and name_equivalent:
        level = "verified"
    else:
        level = "warning"

    # ── Messaggio per l'operatore (sul livello del semaforo) ───────────
    if level == "verified":
        message = (
            "✔︎ Verificato: ti garantisco di aver ricontrollato che la partita "
            "IVA di questa fattura corrisponde a quella del cliente e che la "
            "ragione sociale coincide."
        )
    elif level == "critical":
        if piva_conflict:
            message = (
                f"⛔ La P.IVA della fattura ({inv_piva}) è DIVERSA da quella del "
                f"cliente ({cust_piva}): con ogni probabilità questa fattura non "
                f"è di questo cliente."
            )
        elif piva_match:
            message = (
                f"⛔ La P.IVA coincide ma le ragioni sociali sono molto diverse "
                f"(somiglianza {_score}%): possibile P.IVA errata/avvelenata su "
                f"uno dei due. Da verificare a mano."
            )
        else:
            message = (
                f"⛔ La ragione sociale della fattura non corrisponde a quella "
                f"del cliente (somiglianza {_score}%). Da verificare."
            )
    else:  # warning
        if customer is None:
            message = "Fattura non ancora abbinata a un cliente: verifica manuale."
        elif inv_piva and not cust_piva:
            message = (
                "⚠️ La fattura riporta una P.IVA valida ma il cliente non ne ha "
                "una registrata: impossibile garantire la corrispondenza. "
                "Verifica manuale."
            )
        elif cust_piva and not inv_piva:
            message = (
                "⚠️ Il cliente ha una P.IVA ma la fattura non ne riporta una "
                "valida: impossibile garantire la corrispondenza. Verifica manuale."
            )
        elif piva_match and not piva_match_guaranteed:
            # P.IVA estera coincidente: match per sola uguaglianza di
            # stringa (nessun checksum) → non è una garanzia piena.
            message = (
                "⚠️ La P.IVA (estera) coincide su entrambi, ma è validata solo "
                "nel formato (nessun checksum): corrispondenza probabile, non "
                "garantibile con certezza."
            )
        elif piva_match and not name_equivalent:
            message = (
                f"⚠️ La P.IVA corrisponde, ma le ragioni sociali non sono "
                f"identiche (somiglianza {_score}%): controlla che sia lo stesso "
                f"soggetto."
            )
        elif name_equivalent:
            message = (
                "⚠️ Le ragioni sociali coincidono, ma né la fattura né il cliente "
                "hanno una P.IVA: corrispondenza non garantibile con certezza."
            )
        else:
            message = (
                f"⚠️ Corrispondenza non garantita (somiglianza nome {_score}%): "
                f"verifica manuale."
            )

    return {
        "level": level,               # verified | warning | critical
        "verdict": verdict,           # bad | warn | ok (compat audit)
        "guaranteed": level == "verified",
        "message": message,
        "piva_match": piva_match,
        "piva_conflict": piva_conflict,
        "name_score": name_score,
        "name_equivalent": name_equivalent,
        # Valori grezzi affiancati per il controllo VISIVO ridondante.
        "invoice_piva": inv_piva_raw or None,
        "customer_piva": cust_piva_raw or None,
        "invoice_name": inv_name_raw or None,
        "customer_name": cust_name_raw or None,
    }
