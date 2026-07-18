"""Normalizer module - normalizes Italian company names (ragione sociali) for matching."""

import re
import functools
import logging
import unicodedata
from typing import FrozenSet, List, Optional, Tuple

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


# Italian legal form abbreviations to remove (safe anywhere in the name:
# multi-letter, non ambigue con parole vere)
LEGAL_FORMS = [
    # Società a Responsabilità Limitata (e varianti)
    "S.R.L.S.", "SRLS",  # Semplificata — molto comune per società nuove
    "S.R.L.", "SRL",
    "S.P.A.",
    "S.A.S.", "SAS",
    "S.N.C.", "SNC",
    "S.C.A.R.L.", "SCARL",
    "S.C.P.A.", "SCPA",
    "S.C.R.L.", "SCRL",  # Cooperativa a Resp. Limitata
    "S.R.C.", "SRC",
    "S.R.S.", "SRS",
    "S.A.P.A.",
    "S.T.P.", "STP",  # Società tra Professionisti
    "A.S.D.", "ASD",
    "A.P.S.", "APS",
    "O.N.G.",
    "E.T.S.", "ETS",
    "ONLUS",
    "UNIPERSONALE",
    # Forme legali estere (per clienti stranieri fatturati in Italia)
    "LLC", "LTD", "GMBH", "SARL",
]

# Sigle la cui variante SENZA punti è una parola vera o un cognome: la
# forma PUNTATA è inequivocabile e si rimuove ovunque, quella NUDA non si
# rimuove MAI — dalla sola stringa non è distinguibile se sia la forma
# legale o parte del nome, e un match sbagliato manda il sollecito
# all'azienda sbagliata (meglio un suggerimento da confermare).
#   spa  = centro benessere (alberghi/ristoranti, il nostro target)
#   ong  = cognome cinese/vietnamita (i ristoranti asiatici comprano sake)
#   sapa = Sa Pa, città del Vietnam; e il mosto cotto (enoteche)
NODOTS_ANYWHERE_UNSAFE = {"S.P.A.", "O.N.G.", "S.A.P.A."}

# Sigle CORTE e ambigue (2 lettere, o parole reali come "sa"): rimosse SOLO
# a fine nome, altrimenti mangiano pezzi veri della ragione sociale
# (es. "Sa Duchessa", "Pa' Sushi").
TRAILING_LEGAL_FORMS = [
    "S.S.", "S.C.", "S.A.A.", "P.A.", "A.S.",
    "AG", "SA",
]

# Common prefixes to remove
COMMON_PREFIXES = [
    "ditta",
    "impresa",
    "società",
    "azienda",
    "cooperativa",
]


def remove_accents(text: str) -> str:
    """Remove accents and diacritics from text."""
    nfkd_form = unicodedata.normalize("NFKD", text)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])


# Il suffisso "di Nome Cognome". SEDE UNICA: la usano sia la
# normalizzazione (che lo TOGLIE dalla chiave) sia person_part_of (che lo
# RIESTRAE). Finché è la stessa costante, le due operazioni non possono
# divergere e la guardia sui titolari resta totale — il buco silenzioso
# che nasceva dalla copia in matching.py (piano 17/07, voce 13).
# 2+ parole dopo "di": una sola è di solito parte del nome vero
# ("Osteria di Mare") e toglierla collasserebbe aziende diverse.
_DI_PERSON = re.compile(r"\s+di\s+(\w+(?:\s+\w+)+)\s*$")


def _canonical_form(form: str) -> str:
    """'S.R.L.' e 'SRL' sono la STESSA forma legale: una sola etichetta."""
    return form.replace(".", "").upper()


def _strip_legal_forms(text: str) -> Tuple[str, FrozenSet[str]]:
    """Toglie le forme legali e dice QUALI ha tolto.

    Sede UNICA della rimozione: `legal_forms_of` non ha una regex propria,
    passa di qui. Il rispecchiamento è quindi PER COSTRUZIONE, non per
    disciplina: aggiungere una sigla a LEGAL_FORMS aggiorna insieme la
    chiave normalizzata e le forme rilevate, e non può aprire il divario
    che ha reso fragile _DI_PERSON.

    `text` è già senza accenti e minuscolo (vedi _normalize_impl).
    """
    found = set()

    # Sigle sicure ovunque nel nome (multi-lettera, non ambigue).
    for form in LEGAL_FORMS:
        escaped = re.escape(form.lower())
        # Dot-flexible: "s.r.l." matcha "s.r.l." e "s.r.l"
        text, n = re.subn(rf"(?<!\w){escaped}\.?(?!\w)", "", text)
        if n:
            found.add(_canonical_form(form))
        # Variante senza punti ("srl"), salvo quelle ambigue con parole vere
        nodots = form.replace(".", "").lower()
        if nodots != form.lower() and form not in NODOTS_ANYWHERE_UNSAFE:
            text, n = re.subn(rf"(?<!\w){re.escape(nodots)}(?!\w)", "", text)
            if n:
                found.add(_canonical_form(form))

    # Sigle corte/ambigue: solo a fine nome.
    for form in TRAILING_LEGAL_FORMS:
        escaped = re.escape(form.lower())
        text, n = re.subn(rf"(?<!\w){escaped}\.?\s*$", "", text)
        if n:
            found.add(_canonical_form(form))
        nodots = form.replace(".", "").lower()
        if nodots != form.lower():
            text, n = re.subn(rf"(?<!\w){re.escape(nodots)}\s*$", "", text)
            if n:
                found.add(_canonical_form(form))

    return text, frozenset(found)


@functools.lru_cache(maxsize=8192)
def legal_forms_of(name: str) -> FrozenSet[str]:
    """Le forme legali presenti nel nome, canonicalizzate ('SRL', 'SAS'…).

    Operazione INVERSA di quella che normalize_ragione_sociale compie in
    silenzio: la chiave butta via la forma legale, qui la si recupera per
    decidere se due nomi che collassano sulla stessa chiave siano davvero
    la stessa entità giuridica.

    Insieme VUOTO = "nessuna forma riconosciuta", che NON significa "ditta
    individuale": un record può semplicemente ometterla ('Fronte Mare' per
    'Fronte Mare Srl'). Vedi is_ditta_individuale.

    >>> sorted(legal_forms_of("SHU&SHU DI SHU KEI S.A.S."))
    ['SAS']
    >>> sorted(legal_forms_of("Gaijin Srl"))
    ['SRL']
    >>> sorted(legal_forms_of("Gaijin di Fois Stefano"))
    []
    """
    if not name:
        return frozenset()
    return _strip_legal_forms(remove_accents(name).lower())[1]


@functools.lru_cache(maxsize=8192)
def person_part_of(name: str) -> Optional[str]:
    """Il titolare 'di Nome Cognome' di una ragione sociale, se c'è.

    >>> person_part_of("Gaijin di Fois Stefano")
    'fois stefano'
    >>> person_part_of("Gaijin Srl") is None
    True
    """
    m = _DI_PERSON.search(normalize_ragione_sociale_light(name or ""))
    return m.group(1) if m else None


def is_ditta_individuale(name: str) -> bool:
    """True se il nome porta la FIRMA della ditta individuale: titolare
    esplicito ('X di Mario Rossi') E nessuna forma legale.

    Non è l'assenza di forma a qualificare — un record la omette di
    continuo ('Fronte Mare' sta per 'Fronte Mare Srl') — ed è per questo
    che serve la COMPRESENZA delle due condizioni: una società di persone
    che porta il socio nella ragione sociale DEVE dichiarare la forma
    ('SHU&SHU DI SHU KEI S.A.S.'). Titolare + nessuna forma resta quindi
    la ditta individuale, e questa è INFORMAZIONE POSITIVA, non assenza.

    >>> is_ditta_individuale("Gaijin di Fois Stefano")
    True
    >>> is_ditta_individuale("SHU&SHU DI SHU KEI S.A.S.")
    False
    >>> is_ditta_individuale("Fronte Mare")
    False
    """
    return bool(person_part_of(name) and not legal_forms_of(name))


@functools.lru_cache(maxsize=8192)
def _normalize_impl(name: str, strip_person: bool) -> str:
    """Implementazione condivisa della normalizzazione.

    strip_person controlla il taglio del pattern finale "di Nome Cognome":
    la chiave di matching lo rimuove (le fatture spesso omettono la parte
    persona), la chiave 'light' lo conserva (le fatture delle ditte
    individuali spesso riportano SOLO la persona).

    Memoizzata: è una funzione pura e i nomi cliente si ripetono identici
    per ogni fattura del run (repair ricorrente: O(fatture × clienti)
    normalizzazioni regex senza cache).
    """
    if not name:
        return ""

    # Remove accents first
    normalized = remove_accents(name)

    # Convert to lowercase
    normalized = normalized.lower()

    # Remove legal forms (must handle both with and without dots).
    # Sede unica: la stessa funzione che dice QUALI forme c'erano
    # (legal_forms_of) — così le due non possono divergere.
    normalized, _forms = _strip_legal_forms(normalized)

    # Clean up before di pattern matching
    normalized = normalized.strip()

    if strip_person:
        # Handle "di" + personal name pattern
        # E.g., "SHU&SHU DI SHU KEI" -> "SHU&SHU"
        # Stessa costante che person_part_of usa per RIESTRARRE il titolare.
        normalized = _DI_PERSON.sub("", normalized)

    # Remove common prefixes
    prefix_pattern = "|".join(re.escape(prefix.lower()) for prefix in COMMON_PREFIXES)
    prefix_regex = re.compile(rf"^\b({prefix_pattern})\b\s+")
    normalized = prefix_regex.sub("", normalized)

    # Remove punctuation except for & and -
    # Keep & and - as they can be part of company names
    normalized = re.sub(r"[^\w&\-\s]", "", normalized)

    # La spaziatura attorno a '&' non è distintiva: 'M & M' e 'M&M' sono la
    # stessa insegna e devono produrre la stessa chiave.
    normalized = re.sub(r"\s*&\s*", "&", normalized)

    # Remove extra whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized


def normalize_ragione_sociale(name: str) -> str:
    """
    Normalize Italian company name (ragione sociale) for matching.

    Removes:
    - Legal forms (S.R.L., SPA, etc.)
    - "di" + personal name patterns
    - Punctuation and extra whitespace
    - Common prefixes
    - Accents and diacritics

    Converts to lowercase.

    Args:
        name: The raw company name to normalize

    Returns:
        Normalized company name suitable for matching

    Examples:
        >>> normalize_ragione_sociale("SHU&SHU DI SHU KEI S.A.S.")
        "shu&shu"
        >>> normalize_ragione_sociale("ACME S.R.L.")
        "acme"
        >>> normalize_ragione_sociale("Società ROSSI s.p.a.")
        "rossi"
    """
    return _normalize_impl(name, strip_person=True)


def normalize_ragione_sociale_light(name: str) -> str:
    """Normalizzazione 'light': come normalize_ragione_sociale ma CONSERVA
    il pattern finale "di Nome Cognome".

    Serve ai confronti di somiglianza: la fattura di una ditta individuale
    è spesso intestata alla sola persona ("MERCURI CHRISTIAN"), che la
    chiave di matching ("dr gahe") butta via per costruzione.

    >>> normalize_ragione_sociale_light("Dr. Gahe di Mercuri Christian")
    "dr gahe di mercuri christian"
    """
    return _normalize_impl(name, strip_person=False)


def are_similar(
    name1: str,
    name2: str,
    threshold: int = 85
) -> Tuple[bool, int]:
    """
    Check if two company names are similar using fuzzy matching.

    Uses rapidfuzz token_set_ratio for robust comparison that handles
    different word order and variations.

    Args:
        name1: First company name (will be normalized)
        name2: Second company name (will be normalized)
        threshold: Minimum similarity score (0-100) to consider a match

    Returns:
        Tuple of (is_similar: bool, score: int where 0-100 is the similarity score)

    Examples:
        >>> are_similar("ACME S.R.L.", "Acme srl")
        (True, 100)
        >>> are_similar("Mario Rossi S.A.S.", "Rossi Mario SAS")
        (True, 90)  # Approximate
        >>> are_similar("Company A", "Company B")
        (False, 15)  # Approximate
    """
    norm1 = normalize_ragione_sociale(name1)
    norm2 = normalize_ragione_sociale(name2)

    # Use token_set_ratio which is better for matching names with different word order
    # and partial matches
    score = fuzz.token_set_ratio(norm1, norm2)

    is_similar = score >= threshold

    logger.debug(
        f"Similarity check: '{name1}' vs '{name2}' -> score={score}, similar={is_similar}"
    )

    return is_similar, score


def light_similarity_score(name1: str, name2: str) -> int:
    """Somiglianza sulle chiavi 'light' (pattern 'di Nome Cognome' conservato)."""
    norm1 = normalize_ragione_sociale_light(name1)
    norm2 = normalize_ragione_sociale_light(name2)
    if not norm1 or not norm2:
        return 0
    return int(fuzz.token_set_ratio(norm1, norm2))


def light_similarity_score_strict(name1: str, name2: str) -> int:
    """Come light_similarity_score ma SENZA il bonus-subset di
    token_set_ratio: serve alle guardie che devono CONFERMARE un
    candidato (non solo non-smentirlo). Con token_set_ratio un lato che
    porta il pattern 'di Nome Cognome' e uno che non lo porta valgono
    100 per puro contenimento ('Osteria di Mario Rossi' vs 'Osteria
    SRL') — che è esattamente il collasso da bloccare, non una conferma.
    """
    norm1 = normalize_ragione_sociale_light(name1)
    norm2 = normalize_ragione_sociale_light(name2)
    if not norm1 or not norm2:
        return 0
    return int(fuzz.token_sort_ratio(norm1, norm2))


def name_similarity_score(name1: str, name2: str) -> int:
    """Somiglianza robusta ai nomi-persona: max tra lo score sulle chiavi
    de-personalizzate (are_similar) e quello sulle chiavi 'light'.

    "MERCURI CHRISTIAN" vs "Dr. Gahe di Mercuri Christian" vale 25 sulle
    chiavi di matching ('mercuri christian' vs 'dr gahe') ma 100 sulle
    chiavi light: per le GUARDIE (anti-poisoning P.IVA, repair, audit) la
    persona contenuta per intero nell'insegna è concordanza, non
    dissomiglianza.
    """
    _, full_score = are_similar(name1, name2, threshold=100)
    return int(max(full_score, light_similarity_score(name1, name2)))


# Sotto questa somiglianza (chiavi normalizzate) due nomi NON sono
# "approssimabili": è rumore, non un refuso/variante dello stesso nome.
SUGGEST_CUTOFF = 78


def rank_similar(
    query: str,
    names: List[str],
    limit: int = 6,
    cutoff: int = SUGGEST_CUTOFF,
) -> List[Tuple[int, int]]:
    """Ranking 'forse intendevi': indici dei nomi più vicini alla query.

    Confronta le chiavi NORMALIZZATE (accenti, forme legali S.r.l./Srl,
    punteggiatura ignorati), così "Domo Milano" trova "Domò Milano" e
    "Sakeya S.r.l." trova "Sakeya Srl"/"Sakeya", e i refusi sono tollerati
    ("Sakya" → "Sakeya", "Ostria del Borgo" → "Osteria del Borgo").

    Usa token_SORT_ratio (non token_set): è sensibile alla LUNGHEZZA, così
    NON premia con 100 un'azienda diversa che condivide solo un token
    generico. Es. "Domo Milano" NON suggerisce "Yoho Milano" (token 'domo'
    vs 'yoho' diversi), e la query "milano" da sola non tira su mezza
    città — proprio i falsi positivi da evitare. In cambio, una singola
    parola parziale ("Domo" da sola) non basta: serve scrivere abbastanza
    del nome ("domo milan" → "Domò Milano", 95).

    Ritorna [(indice_in_names, score)] ordinati per score decrescente,
    solo sopra `cutoff`, al massimo `limit` risultati.
    """
    qn = normalize_ragione_sociale(query)
    if not qn:
        return []
    norms = [normalize_ragione_sociale(n or "") for n in names]
    results = process.extract(
        qn, norms,
        scorer=fuzz.token_sort_ratio,
        limit=limit,
        score_cutoff=cutoff,
    )
    # process.extract su una lista ritorna (match, score, indice).
    return [(idx, int(score)) for (_match, score, idx) in results]
