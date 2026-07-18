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

    # `guaranteed` = garanzia da CHECKSUM (verde per costruzione). Va fissato
    # PRIMA dell'eventuale upgrade manuale: la conferma d'intestazione è verde
    # ma NON è una garanzia — resta guaranteed=False.
    guaranteed = level == "verified"

    # ── Conferma d'identità DUREVOLE: intestazione accettata ────────────
    # Se l'operatore ha dichiarato che questa intestazione appartiene al
    # cliente (tratto CustomerAcceptedName, letto DAL VIVO dalla relationship),
    # la fattura esce dai problemi dell'audit (verdict→ok) e il semaforo
    # diventa verde (level→verified), MA con guaranteed=False: è una conferma
    # umana, non una garanzia checksum. Vale per costruzione anche per le
    # fatture FUTURE con la stessa intestazione (nessuna scrittura per-fattura).
    #
    # VALVOLA DI SICUREZZA OBBLIGATORIA #1: l'upgrade NON avviene se
    # piva_conflict è True (P.IVA fattura e cliente entrambe valide e diverse).
    # Una conferma d'intestazione non deve MAI zittire una P.IVA in
    # contraddizione: resta critical.
    #
    # VALVOLA DI SICUREZZA OBBLIGATORIA #2 (piva_assignable): l'upgrade NON
    # avviene se la fattura porta una P.IVA valida CHE IL CLIENTE NON HA
    # (inv_piva valido e cust_piva assente). Lì la strada giusta è ASSEGNARE
    # la P.IVA al cliente (azione più forte, verde da checksum e cascade sulle
    # future), non smarcare la riga: accettare il nome spegnerebbe l'offerta
    # "Assegna P.IVA" e — poiché l'audit passa da verify — nasconderebbe il
    # cliente dagli insiemi "da sanificare" mentre bonifica-suggestions lo
    # tiene (divergenza lista/audit). La riga resta quindi al suo esito
    # naturale (warning) e l'audit continua a offrire bonifica_piva. È anche
    # ciò che rende VERA l'invariante verify-free di _single_shared_piva. Fuori
    # da questo caso l'accepted-name smarca regolarmente (fattura senza P.IVA
    # valida, o cliente che la P.IVA ce l'ha già).
    #
    # `getattr` difensivo: i chiamanti che passano un customer "simulato"
    # (SimpleNamespace, niente relationship) non devono esplodere.
    piva_assignable = inv_piva is not None and cust_piva is None
    manual_confirmed = False
    if (
        customer is not None
        and not piva_conflict
        and not piva_assignable
        and level != "verified"
        and inv_name_src
    ):
        inv_norm = normalize_ragione_sociale(inv_name_src)
        if inv_norm:
            for an in (getattr(customer, "accepted_names", None) or []):
                if getattr(an, "name_normalized", None) == inv_norm:
                    manual_confirmed = True
                    break
    if manual_confirmed:
        level = "verified"
        verdict = "ok"

    # ── Messaggio per l'operatore (sul livello del semaforo) ───────────
    if manual_confirmed:
        message = (
            "✔︎ Confermato a mano: hai indicato che questa intestazione "
            "appartiene a questo cliente. Non è una garanzia da partita IVA, è "
            "una tua conferma — e vale anche per le fatture future con questa "
            "stessa intestazione."
        )
    elif level == "verified":
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
        "guaranteed": guaranteed,     # verde da CHECKSUM (non la conferma manuale)
        # Verde da conferma UMANA (intestazione accettata), non checksum: la
        # UI può distinguerlo dal verde-garanzia.
        "manual_confirmed": manual_confirmed,
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
