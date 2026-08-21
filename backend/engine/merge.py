"""Deduplica anagrafica: merge di clienti che sono la STESSA azienda.

Problema: la stessa azienda può esistere come DUE (o più) schede cliente —
stessa P.IVA, telefono, e ragione sociale identica o quasi (es. "Basara" e
"Basara a socio unico"). Nasce da fonti diverse (due clienti Shopify, o un
cliente Shopify + un orfano FatturaPro). Le fatture si spargono tra le schede:
un solo "Copia Messaggio" non le recupera tutte.

Regola (decisa dall'owner): **la P.IVA comanda.** Stessa P.IVA italiana
checksum-valida = stessa azienda → si fondono, anche con ragione sociale
leggermente diversa. Il nome serve solo come RETE DI SICUREZZA: se la P.IVA
coincide ma i nomi sono del tutto diversi (una scheda "ristorante", o una
P.IVA digitata male finita su un'azienda estranea) NON si fonde in automatico —
quel cluster resta per l'approvazione manuale.

Perché `are_similar` (token_set_ratio) e non lo scorer name-only del matching:
lì manca la rete della P.IVA, quindi serve token_sort (length-aware) contro i
falsi positivi. QUI la P.IVA checksum-valida È già la chiave forte, quindi il
nome può essere lasco: token_set dà 100 ai sottoinsiemi ("Basara" ⊆ "Basara a
socio unico") ed è esattamente ciò che l'owner vuole; scarta solo i nomi
davvero estranei.

Nessun hard-delete: la scheda fusa resta nel DB con `merged_into` valorizzato
(audit), e va esclusa da liste/conteggi/ricerca. Le sue fatture/azioni/pratiche
sono ripuntate alla sopravvissuta qui, al momento del merge.
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import (
    Customer,
    CustomerAcceptedName,
    Invoice,
    RecoveryAction,
    RecoveryCase,
)
from backend.engine.normalizer import are_similar, normalize_ragione_sociale
from backend.engine.piva import is_checksum_backed, normalize_piva

logger = logging.getLogger(__name__)

# Soglia base di somiglianza nome per l'AUTO-merge (token_set_ratio, subset-
# friendly). La P.IVA checksum-valida è la chiave forte; il nome è la rete di
# sicurezza. 85 lascia passare i sottoinsiemi (100) e le sole-sigle diverse.
AUTO_MERGE_NAME_MIN = 85

# Token GENERICI (categoria/geografia/forma d'impresa): da soli NON
# identificano un'azienda. Servono a chiudere il buco del token_set_ratio, che
# dà 100 ai sottoinsiemi: senza questo, "ristorante" ⊆ "Ristorante Da Gino"
# passerebbe (100) e si auto-fonderebbe — proprio il caso che l'owner vuole
# FERMARE. La regola: per l'auto serve un token DISTINTIVO (brand) condiviso,
# non una sola parola di categoria.
GENERIC_TOKENS = frozenset({
    # ristorazione / commercio
    "ristorante", "ristoranti", "trattoria", "osteria", "hosteria", "pizzeria",
    "bar", "caffe", "cafe", "caffetteria", "sushi", "ramen", "poke", "kebab",
    "food", "cucina", "gastronomia", "gelateria", "pasticceria", "enoteca",
    "birreria", "braceria", "hamburgeria", "paninoteca", "bistrot", "pub",
    "lounge", "market", "supermarket", "minimarket", "shop", "store", "bottega",
    "macelleria", "panetteria", "forno", "hotel", "albergo", "resort", "pizza",
    # impresa generica
    "group", "gruppo", "company", "azienda", "ditta", "societa", "impresa",
    "holding", "service", "services", "servizi", "commerciale", "trading",
    "international", "italia", "italy", "italiana", "italiano", "sushiya",
    # città comuni (non sono il brand)
    "milano", "roma", "torino", "napoli", "firenze", "bologna", "venezia",
    "genova", "palermo", "verona", "padova", "brescia", "bergamo", "monza",
})

# Priorità dello stato pratica (cache): la fusa prende il più avanzato dei due
# (verrà comunque ricalcolato dallo step 'cases' del sync dopo l'auto-merge).
_STATUS_RANK = {
    "idle": 0,
    "waiting": 1,
    "archived": 1,
    "first_contact": 2,
    "second_contact": 3,
    "lawyer": 4,
}


def _distinctive_tokens(name: Optional[str]) -> set:
    """I token 'brand' di un nome: né generici né troppo corti."""
    norm = normalize_ragione_sociale(name or "")
    return {t for t in norm.split() if len(t) >= 3 and t not in GENERIC_TOKENS}


def names_correspond(a: Optional[str], b: Optional[str]) -> tuple[bool, int]:
    """True se i due nomi corrispondono abbastanza per l'AUTO-merge.

    Due gate (la P.IVA checksum-valida è già la chiave forte):
    1. somiglianza token_set ≥ AUTO_MERGE_NAME_MIN (subset-friendly: "Basara"
       ⊆ "Basara a socio unico" = 100);
    2. condividono almeno un token DISTINTIVO (brand), oppure i nomi
       normalizzati sono identici.

    Il gate 2 chiude il buco del subset generico: "ristorante" ⊆ "Ristorante
    Da Gino" passa il gate 1 (100) ma NON il 2 (nessun brand condiviso:
    'ristorante' è generico) → resta per la revisione manuale, come vuole
    l'owner. "Basara"/"Basara a socio unico" condividono il brand 'basara' → ok.
    """
    ok, score = are_similar(a or "", b or "", threshold=AUTO_MERGE_NAME_MIN)
    if not ok:
        return False, score
    da, db = _distinctive_tokens(a), _distinctive_tokens(b)
    if da & db:
        return True, score
    # Nessun brand condiviso: auto solo se i nomi normalizzati sono identici
    # (es. due schede entrambe "Ristorante" con la stessa P.IVA = duplicato).
    na = normalize_ragione_sociale(a or "")
    nb = normalize_ragione_sociale(b or "")
    if na and na == nb:
        return True, score
    return False, score


def _more_advanced_status(a: Optional[str], b: Optional[str]) -> str:
    ra = _STATUS_RANK.get(a or "idle", 0)
    rb = _STATUS_RANK.get(b or "idle", 0)
    return (a or "idle") if ra >= rb else (b or "idle")


def _merge_phones(survivor: Customer, dup: Customer) -> None:
    """Unione di phones_json (per numero), senza duplicati."""
    sp = list(survivor.phones_json or [])
    seen = {p.get("number") for p in sp if isinstance(p, dict)}
    for p in (dup.phones_json or []):
        if isinstance(p, dict) and p.get("number") and p["number"] not in seen:
            sp.append(p)
            seen.add(p["number"])
    if sp:
        survivor.phones_json = sp


def _pick_survivor(members: list[Customer], counts: dict[int, int]) -> Customer:
    """La scheda sopravvissuta del cluster: preferisci l'identità Shopify
    canonica (ha shopify_id), poi più fatture, poi id più basso (stabile)."""
    def key(c: Customer):
        return (
            1 if c.excluded else 0,       # ATTIVO prima dell'escluso: la
                                          # sopravvissuta non deve ereditare
                                          # un'esclusione che nasconde credito.
            0 if c.shopify_id else 1,     # identità Shopify canonica
            -counts.get(c.id, 0),         # più fatture
            c.id,                         # deterministico
        )
    return sorted(members, key=key)[0]


def merge_customers(session: Session, survivor: Customer, dup: Customer) -> None:
    """Fonde `dup` in `survivor`. NON committa (lo fa il chiamante).

    Ripunta fatture, suggerimenti, azioni e pratiche; rispetta l'invariante
    'una sola pratica aperta per cliente'; arricchisce i buchi del survivor;
    marca il dup come fuso (mai hard-delete).
    """
    if dup.id == survivor.id:
        return
    now = datetime.datetime.utcnow()

    # 1. Fatture del dup → survivor (il case_id resta, gestito allo step 4).
    session.query(Invoice).filter(Invoice.customer_id == dup.id).update(
        {Invoice.customer_id: survivor.id}, synchronize_session=False
    )
    # 2. Suggerimenti in quarantena che puntano al dup → survivor.
    session.query(Invoice).filter(Invoice.suggested_customer_id == dup.id).update(
        {Invoice.suggested_customer_id: survivor.id}, synchronize_session=False
    )
    # 3. Azioni di recupero del dup → survivor (il case_id resta, step 4).
    session.query(RecoveryAction).filter(
        RecoveryAction.customer_id == dup.id
    ).update({RecoveryAction.customer_id: survivor.id}, synchronize_session=False)

    # 4. Pratiche. Vincolo DB: una sola pratica APERTA per cliente.
    survivor_open = (
        session.query(RecoveryCase)
        .filter(RecoveryCase.customer_id == survivor.id, RecoveryCase.status == "open")
        .first()
    )
    dup_cases = (
        session.query(RecoveryCase)
        .filter(RecoveryCase.customer_id == dup.id)
        .all()
    )
    for case in dup_cases:
        if case.status == "open" and survivor_open is not None:
            # Entrambi hanno una pratica aperta: fondi quella del dup nella
            # sopravvissuta (sposta azioni+fatture), poi chiudila.
            session.query(RecoveryAction).filter(
                RecoveryAction.case_id == case.id
            ).update({RecoveryAction.case_id: survivor_open.id},
                     synchronize_session=False)
            session.query(Invoice).filter(Invoice.case_id == case.id).update(
                {Invoice.case_id: survivor_open.id}, synchronize_session=False
            )
            # Continuità del tono: non ripartire dal sollecito cordiale.
            survivor_open.inherited_contacts = max(
                survivor_open.inherited_contacts or 0,
                case.inherited_contacts or 0,
            )
            # La pratica chiusa resta filata sotto la sopravvissuta (audit),
            # non sotto il duplicato nascosto.
            case.customer_id = survivor.id
            case.status = "closed"
            case.closed_at = now
            case.closed_reason = "merged"
        else:
            # Pratica chiusa, oppure il survivor non ne ha una aperta: basta
            # ripuntarla al survivor. Se è aperta, ora il survivor ne ha una.
            case.customer_id = survivor.id
            if case.status == "open":
                survivor_open = case

    # 5. Intestazioni accettate: sposta, saltando i duplicati sull'UNIQUE.
    existing_names = {
        n.name_normalized
        for n in session.query(CustomerAcceptedName)
        .filter(CustomerAcceptedName.customer_id == survivor.id)
        .all()
    }
    for an in (
        session.query(CustomerAcceptedName)
        .filter(CustomerAcceptedName.customer_id == dup.id)
        .all()
    ):
        if an.name_normalized in existing_names:
            session.delete(an)
        else:
            an.customer_id = survivor.id
            existing_names.add(an.name_normalized)
    # Conserva la grafia del dup come intestazione accettata del survivor (se
    # diversa): le fatture con quel nome restano verdi nell'audit.
    dup_norm = normalize_ragione_sociale(dup.ragione_sociale or "")
    if dup_norm and dup_norm not in existing_names:
        session.add(
            CustomerAcceptedName(
                customer_id=survivor.id,
                name_normalized=dup_norm,
                note=dup.ragione_sociale,
            )
        )

    # 6. Arricchisci i BUCHI del survivor col dup (non sovrascrive mai).
    for fld in ("phone", "email", "codice_fiscale", "codice_sdi", "partita_iva"):
        if not getattr(survivor, fld) and getattr(dup, fld):
            setattr(survivor, fld, getattr(dup, fld))
    _merge_phones(survivor, dup)
    # NON si eredita 'excluded': un OR silenzioso sposterebbe credito dentro/
    # fuori dal 'lavorabile' (regola finanziaria = decisione owner). La
    # sopravvissuta tiene il PROPRIO stato; l'auto-merge salta i cluster con
    # esclusione mista e li manda in revisione (vedi auto_merge_exact_piva).
    # Adotta il nome più RICCO: se quello del survivor è un sottoinsieme
    # stretto di quello del dup (es. survivor "Basara", dup "Basara Milano
    # Italia"), tieni il più informativo — a meno di lock manuale sul nome.
    if not survivor.ragione_sociale_locked:
        s_tok = set(normalize_ragione_sociale(survivor.ragione_sociale or "").split())
        d_tok = set(normalize_ragione_sociale(dup.ragione_sociale or "").split())
        if s_tok and d_tok and s_tok < d_tok:
            survivor.ragione_sociale = dup.ragione_sociale
            survivor.ragione_sociale_normalized = normalize_ragione_sociale(
                dup.ragione_sociale or ""
            )
    survivor.recovery_status = _more_advanced_status(
        survivor.recovery_status, dup.recovery_status
    )

    # 7. Marca il dup come fuso (mai hard-delete) e NEUTRALIZZA la sua cache di
    # stato: il vero stato ora vive sul survivor, così il dup non compare in
    # nessuna query per stato/prossima-azione anche se un lettore dimenticasse
    # il filtro merged_into (difesa in profondità).
    dup.merged_into = survivor.id
    dup.recovery_status = "idle"
    dup.next_action_date = None
    dup.next_action_type = None
    survivor.updated_at = now

    # Flush deterministico: rende visibili queste mutazioni (in particolare le
    # pratiche ripuntate/adottate) alla PROSSIMA chiamata di merge_customers
    # nello stesso commit — es. il merge manuale di più duplicati in un colpo.
    # Senza, con autoflush=False (i test) due dup con pratica aperta possono
    # violare uq_open_case_per_customer al commit.
    session.flush()


def find_piva_clusters(
    session: Session, active_only: bool = True
) -> dict[str, list[Customer]]:
    """Raggruppa i clienti per P.IVA normalizzata, tenendo solo i gruppi con
    più di una scheda. Salta le P.IVA vuote."""
    q = session.query(Customer)
    if active_only:
        q = q.filter(Customer.merged_into.is_(None))
    by_piva: dict[str, list[Customer]] = {}
    for c in q.all():
        norm = normalize_piva(c.partita_iva)
        if not norm:
            continue
        by_piva.setdefault(norm, []).append(c)
    return {p: cs for p, cs in by_piva.items() if len(cs) > 1}


def auto_merge_exact_piva(session: Session) -> dict:
    """Fonde in AUTOMATICO le schede con stessa P.IVA italiana checksum-valida
    e nome corrispondente. Le altre (P.IVA estera, o nome non corrispondente)
    restano per l'approvazione manuale.

    Committa per cluster (resiliente: un merge fallito non perde gli altri).
    Ritorna {merged, clusters_touched, left_for_review}.
    """
    clusters = find_piva_clusters(session)
    counts = dict(
        session.query(Invoice.customer_id, func.count(Invoice.id))
        .filter(Invoice.customer_id.isnot(None))
        .group_by(Invoice.customer_id)
        .all()
    )
    merged = 0
    touched = 0
    left = 0
    for piva, members in clusters.items():
        # P.IVA estera: solo formato, due entità diverse potrebbero condividere
        # una stringa inventata → mai auto-merge, solo lista.
        if not is_checksum_backed(piva):
            left += len(members) - 1
            continue
        # Esclusione mista nel cluster: unire un'azienda esclusa con una attiva
        # sposterebbe credito dentro/fuori dal 'lavorabile' — è policy (regola
        # 7), mai in automatico. Si lascia alla revisione manuale.
        if len({bool(c.excluded) for c in members}) > 1:
            left += len(members) - 1
            continue
        survivor = _pick_survivor(members, counts)
        cluster_merged = 0
        cluster_left = 0
        try:
            for c in members:
                if c.id == survivor.id:
                    continue
                ok, _score = names_correspond(
                    survivor.ragione_sociale, c.ragione_sociale
                )
                if ok:
                    merge_customers(session, survivor, c)
                    cluster_merged += 1
                else:
                    cluster_left += 1
            if cluster_merged:
                session.commit()
                merged += cluster_merged
                touched += 1
            # I contatori si aggiornano SOLO dopo il commit riuscito: niente
            # sovrastima se il cluster fa rollback.
            left += cluster_left
        except Exception:  # pragma: no cover - resilienza per-cluster
            session.rollback()
            logger.exception("auto_merge: cluster P.IVA %s fallito", piva)
    return {"merged": merged, "clusters_touched": touched, "left_for_review": left}
