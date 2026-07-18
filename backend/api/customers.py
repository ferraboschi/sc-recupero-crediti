"""Customers API endpoints."""

import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from datetime import datetime

from backend.database import (
    get_session, Customer, Invoice, ActivityLog, RecoveryAction,
    CustomerAcceptedName,
)
from backend.engine.cases import get_open_case, contact_count, business_day_start
from backend.engine.verify import verify_invoice_customer
from backend.engine.normalizer import normalize_ragione_sociale, name_similarity_score
from backend.engine.overdue import overdue_clause, RECOVERY_ACTION_TYPES
from backend.engine.piva import validate_piva

logger = logging.getLogger(__name__)
router = APIRouter()


def _accepted_name_dict(an: CustomerAcceptedName) -> dict:
    """Serializzazione di un'intestazione accettata per l'API/UI."""
    return {
        "id": an.id,
        "name_normalized": an.name_normalized,
        "note": an.note,
        "created_at": an.created_at.isoformat() if an.created_at else None,
    }


def _audit_customer_ids(session, include_paid: bool = False) -> set:
    """Insieme dei clienti "da sanificare": quelli con almeno una fattura
    dall'esito warn/bad (verify_invoice_customer) NON già verificata a mano,
    oppure con almeno un suggerimento pendente in quarantena.

    Efficienza: UNA sola scansione batch con join su Customer (i clienti
    arrivano nella stessa query, niente SELECT-per-fattura → niente N+1),
    più una query DISTINCT per i suggerimenti pendenti. Il verify è
    Python-level (fuzzy sui nomi, non esprimibile in SQL): si itera, ma su
    una scansione sola e joinata, non ricaricando l'intero DB per cliente.
    """
    ids = set()
    q = (
        session.query(Invoice, Customer)
        .join(Customer, Invoice.customer_id == Customer.id)
        .filter(Invoice.audit_reviewed_at.is_(None))
    )
    if not include_paid:
        q = q.filter(Invoice.status != "paid")
    for inv, cust in q.all():
        if verify_invoice_customer(inv, cust)["verdict"] in ("warn", "bad"):
            ids.add(cust.id)

    # Suggerimenti pendenti verso un cliente ESISTENTE: puro SQL, nessun
    # verify. Le PAGATE sono INCLUSE, in parità con la scheda cliente e con
    # /{id}/audit (che non filtrano per status): una quarantenata pagata
    # resta visibile sul profilo finché non viene abbinata (caso Belfiore,
    # docs/verifica-segnalazioni-20260716.md — inquina i totali storici),
    # quindi badge e filtro "da sanificare" devono contarla, o la lista
    # nasconde ciò che la scheda segnala. Il join su Customer scarta i
    # suggerimenti ORFANI (cliente cancellato da un merge): senza, il badge
    # conterebbe un id fantasma che la lista non sa mostrare.
    pend = (
        session.query(Invoice.suggested_customer_id)
        .join(Customer, Customer.id == Invoice.suggested_customer_id)
        .filter(Invoice.customer_id.is_(None))
        .distinct()
    )
    for (cid,) in pend:
        ids.add(cid)
    return ids


def _single_shared_piva(customer, invoices):
    """Criterio della bonifica_piva a livello cliente, in UNA definizione sola
    condivisa da lista e bulk (così non possono divergere dall'audit).

    Dato un cliente e le sue fatture NON pagate, ritorna:
    - ("single", piva, [carrier_ids], confidence) se il cliente NON ha una
      P.IVA valida e le sue fatture non-verificate che portano una P.IVA valida
      ne condividono UNA SOLA;
    - ("conflict", [piva distinte]) se ne portano di DIVERSE (forse due clienti);
    - None se il cliente ha già una P.IVA valida, o nessuna fattura porta una
      P.IVA valida da assegnare.

    confidence = somiglianza nome MINIMA (caso peggiore) fra la ragione sociale
    del cliente e le intestazioni grezze delle fatture che portano quella P.IVA
    — lo STESSO scorer di verify (name_similarity_score, il "Somiglianza nomi").

    INVARIANTE (e perché regge): una fattura con P.IVA valida verso un cliente
    SENZA P.IVA valida non è MAI 'ok' nel verify — è sempre warn/bad. Regge
    perché verify GATE-a l'upgrade via accepted_names esattamente su questo
    caso (piva_assignable): un'intestazione accettata NON smarca una fattura la
    cui P.IVA è ancora assegnabile al cliente (la strada giusta è assegnarla).
    Senza quel gate l'invariante sarebbe FALSA e questo shortcut divergerebbe
    dall'audit. Data l'invariante, raccogliere i carrier con P.IVA valida
    equivale a raccogliere i "problemi con P.IVA" di bonifica_piva SENZA
    chiamare verify — che leggerebbe accepted_names dal vivo (una lazy-load per
    cliente = N+1 sulla lista da 115+).
    """
    if validate_piva(customer.partita_iva) is not None:
        return None
    carriers = []  # (invoice_id, piva, confidence)
    for inv in invoices:
        # Le già "Segnate verificate" escono dai problemi (come in bonifica_piva).
        if inv.audit_reviewed_at is not None:
            continue
        ip = validate_piva(inv.customer_piva_raw)
        if ip is None:
            continue
        conf = name_similarity_score(
            inv.customer_name_raw or "", customer.ragione_sociale or ""
        )
        carriers.append((inv.id, ip, conf))
    if not carriers:
        return None
    distinct = sorted({p for _, p, _ in carriers})
    if len(distinct) > 1:
        return ("conflict", distinct)
    the_piva = distinct[0]
    group = [(iid, conf) for iid, p, conf in carriers if p == the_piva]
    return ("single", the_piva, [iid for iid, _ in group], min(conf for _, conf in group))


@router.get("")
async def list_customers(
    session: Session = Depends(get_session),
    search: str = Query(None),
    excluded: bool = Query(None, description="Filtra per stato escluso (true/false). Omesso = tutti."),
    only_overdue: bool = Query(False, description="Show only customers with overdue invoices"),
    to_sanitize: bool = Query(False, description="Solo clienti 'da sanificare' (audit warn/bad o suggerimento pendente)"),
    no_phone: bool = Query(False, description="Solo clienti senza telefono (non sollecitabili)"),
    recovery_status: str = Query(None, description="Filtra per stato pratica (idle/first_contact/…)"),
    sort_by: str = Query(None, description="Sort field: total_overdue, overdue_count, days_overdue, last_action, earliest_due_date, ragione_sociale"),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """
    List customers with optional search and filter by excluded status.

    Filtri: only_overdue, to_sanitize (audit), no_phone (non sollecitabili),
    recovery_status (stato pratica), excluded.
    Ordinamenti: total_overdue (scaduto), overdue_count (n. fatture scadute),
    days_overdue (max giorni scaduto), last_action (ultimo sollecito),
    earliest_due_date (scadenza più vicina), ragione_sociale.

    La definizione di "scaduto" NON è ricopiata: usa overdue_clause() (la
    stessa della cascata di riconciliazione).
    """
    try:
        # Step 1: Get invoice stats using SQL aggregation (much faster than loading all rows)
        from sqlalchemy import case
        raw_stats = (
            session.query(
                Invoice.customer_id,
                func.count(Invoice.id).label("invoice_count"),
                func.sum(Invoice.amount_due).label("total_due"),
                func.sum(case((overdue_clause(), 1), else_=0)).label("overdue_count"),
                func.sum(case((overdue_clause(), Invoice.amount_due), else_=0)).label("total_overdue"),
                func.min(case((overdue_clause(), Invoice.due_date), else_=None)).label("earliest_due_date"),
                func.max(case((overdue_clause(), Invoice.days_overdue), else_=None)).label("max_days_overdue"),
            )
            .filter(Invoice.status != "paid", Invoice.customer_id.isnot(None))
            .group_by(Invoice.customer_id)
            .all()
        )

        invoice_stats = {}
        for row in raw_stats:
            invoice_stats[row[0]] = {
                "invoice_count": row[1] or 0,
                "total_due": float(row[2] or 0),
                "overdue_count": row[3] or 0,
                "total_overdue": float(row[4] or 0),
                "earliest_due_date": row[5],
                "max_days_overdue": row[6] or 0,
            }

        # Ultimo sollecito per cliente: MAX(created_at) fra i contatti reali
        # (RECOVERY_ACTION_TYPES, non annullati). Una query aggregata sola —
        # niente N+1. Serve all'ordinamento e alla colonna "ultimo sollecito".
        last_action_rows = (
            session.query(
                RecoveryAction.customer_id,
                func.max(RecoveryAction.created_at).label("last_action"),
            )
            .filter(
                RecoveryAction.action_type.in_(RECOVERY_ACTION_TYPES),
                RecoveryAction.cancelled.isnot(True),
            )
            .group_by(RecoveryAction.customer_id)
            .all()
        )
        last_action_by_customer = {r[0]: r[1] for r in last_action_rows}

        # Insieme "da sanificare" (audit): calcolato UNA sola volta e solo se
        # il filtro è attivo — è la parte costosa (verify Python-level).
        sanitize_ids = _audit_customer_ids(session) if to_sanitize else None

        # Step 2: Query customers with basic filters.
        # ORDER BY id: senza, l'ordine di ritorno è garantito solo per caso
        # (SQLite = rowid) ma NON su Postgres/Supabase — i pari-merito dei
        # sort seguenti si sposterebbero fra richieste e la paginazione
        # salterebbe/ripeterebbe righe (stessa classe di bug già corretta su
        # fatture e positions).
        query = session.query(Customer).order_by(Customer.id)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                or_(
                    Customer.ragione_sociale.ilike(search_pattern),
                    Customer.partita_iva.ilike(search_pattern),
                    Customer.email.ilike(search_pattern),
                )
            )

        if excluded is not None:
            query = query.filter(Customer.excluded == excluded)

        if recovery_status:
            query = query.filter(Customer.recovery_status == recovery_status)

        if no_phone:
            # Non sollecitabile: telefono NULL o stringa vuota/di soli spazi.
            query = query.filter(
                or_(Customer.phone.is_(None), func.trim(Customer.phone) == "")
            )

        all_customers = query.all()

        # Step 3: Build enriched list
        enriched = []
        for cust in all_customers:
            stats = invoice_stats.get(cust.id, {"invoice_count": 0, "total_due": 0.0, "overdue_count": 0, "total_overdue": 0.0, "earliest_due_date": None, "max_days_overdue": 0})
            enriched.append({
                "customer": cust,
                "last_action": last_action_by_customer.get(cust.id),
                **stats,
            })

        # Step 4: Filtri in-Python (dipendono dagli aggregati / dall'audit)
        if only_overdue:
            enriched = [e for e in enriched if e["overdue_count"] > 0]
        if to_sanitize:
            enriched = [e for e in enriched if e["customer"].id in sanitize_ids]

        total = len(enriched)

        # Compute summary totals BEFORE pagination (across ALL matching records)
        summary_total_overdue = sum(e["total_overdue"] for e in enriched)
        summary_overdue_customers = sum(1 for e in enriched if e["overdue_count"] > 0)

        # Step 5: Sort. Ogni chiave ha l'id come tiebreaker FINALE, sempre
        # CRESCENTE (in desc si nega, così reverse=True lo riporta
        # crescente): i pari-merito hanno un ordine totale, identico fra
        # richieste e coerente con /neighbors (ORDER BY … DESC, id ASC).
        desc = sort_order == "desc"

        def _id_tie(e):
            cid = e["customer"].id
            return -cid if desc else cid

        if sort_by == "total_overdue":
            enriched.sort(key=lambda e: (e["total_overdue"], _id_tie(e)), reverse=desc)
        elif sort_by == "overdue_count":
            enriched.sort(key=lambda e: (e["overdue_count"], _id_tie(e)), reverse=desc)
        elif sort_by == "days_overdue":
            enriched.sort(key=lambda e: (e["max_days_overdue"], _id_tie(e)), reverse=desc)
        elif sort_by == "last_action":
            from datetime import datetime as _dt
            far_past = _dt.min
            enriched.sort(
                key=lambda e: (e.get("last_action") or far_past, _id_tie(e)),
                reverse=desc,
            )
        elif sort_by == "earliest_due_date":
            from datetime import date as date_type
            far_future = date_type(9999, 12, 31)
            enriched.sort(
                key=lambda e: (e.get("earliest_due_date") or far_future, _id_tie(e)),
                reverse=desc,
            )
        else:
            enriched.sort(
                key=lambda e: ((e["customer"].ragione_sociale or "").lower(), e["customer"].id)
            )

        # Step 6: Paginate
        page = enriched[skip:skip + limit]

        items = []
        for entry in page:
            cust = entry["customer"]
            items.append({
                "id": cust.id,
                "ragione_sociale": cust.ragione_sociale,
                "partita_iva": cust.partita_iva,
                "phone": cust.phone,
                "email": cust.email,
                "excluded": cust.excluded,
                "source": cust.source,
                "phone_validated": cust.phone_validated,
                "recovery_status": cust.recovery_status,
                "next_action_date": cust.next_action_date.isoformat() if cust.next_action_date else None,
                "next_action_type": cust.next_action_type,
                "invoice_count": entry["invoice_count"],
                "total_due": entry["total_due"],
                "overdue_count": entry["overdue_count"],
                "total_overdue": entry["total_overdue"],
                "max_days_overdue": entry["max_days_overdue"],
                "earliest_due_date": entry["earliest_due_date"].isoformat() if entry.get("earliest_due_date") else None,
                "last_action": entry["last_action"].isoformat() if entry.get("last_action") else None,
                "created_at": cust.created_at.isoformat(),
            })

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "summary_total_overdue": summary_total_overdue,
            "summary_overdue_customers": summary_overdue_customers,
            "items": items,
        }

    except Exception as e:
        logger.error(f"Error listing customers: {e}", exc_info=True)
        raise


@router.get("/suggest")
async def suggest_customers(
    q: str = Query(..., min_length=2, description="Testo di ricerca approssimato"),
    limit: int = Query(6, ge=1, le=20),
    session: Session = Depends(get_session),
):
    """"Forse intendevi": clienti la cui ragione sociale è APPROSSIMABILE al
    testo cercato — accenti, forme legali (S.r.l./Srl) e punteggiatura
    ignorati, refusi tollerati. Aiuta a ritrovare un cliente di cui non si
    ricorda la grafia esatta (es. "Domo Milano" → "Domò Milano", "Sakeya
    Srl" → "Sakeya S.r.l.").

    Cerca su TUTTI i clienti, indipendentemente dal filtro only_overdue.
    Definito PRIMA di /{customer_id} così 'suggest' non è letto come id.
    """
    from backend.engine.normalizer import rank_similar
    from sqlalchemy import case

    rows = session.query(
        Customer.id, Customer.ragione_sociale,
        Customer.partita_iva, Customer.excluded,
    ).all()
    if not rows:
        return {"query": q, "items": []}

    ranked = rank_similar(q, [r[1] or "" for r in rows], limit=limit)
    if not ranked:
        return {"query": q, "items": []}

    ids = [rows[idx][0] for idx, _ in ranked]
    stats = {}
    stat_rows = (
        session.query(
            Invoice.customer_id,
            func.sum(case((Invoice.days_overdue > 0, 1), else_=0)).label("overdue_count"),
            func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).label("total_overdue"),
        )
        .filter(Invoice.status != "paid", Invoice.customer_id.in_(ids))
        .group_by(Invoice.customer_id)
        .all()
    )
    for sr in stat_rows:
        stats[sr[0]] = {"overdue_count": int(sr[1] or 0), "total_overdue": float(sr[2] or 0)}

    items = []
    for idx, score in ranked:
        r = rows[idx]
        s = stats.get(r[0], {"overdue_count": 0, "total_overdue": 0.0})
        items.append({
            "id": r[0],
            "ragione_sociale": r[1],
            "partita_iva": r[2],
            "excluded": bool(r[3]),
            "score": score,
            "overdue_count": s["overdue_count"],
            "total_overdue": s["total_overdue"],
        })
    return {"query": q, "items": items}


@router.get("/audit-summary")
async def customers_audit_summary(
    include_paid: bool = Query(False, description="Considera anche le fatture pagate"),
    session: Session = Depends(get_session),
):
    """Conteggio "da sanificare" per la lista Clienti: quanti clienti hanno
    almeno una fattura con esito audit warn/bad (non già verificata) o un
    suggerimento pendente. Restituisce anche l'elenco degli id, così il
    frontend può marcare le righe senza un secondo giro.

    Definito PRIMA di /{customer_id} così 'audit-summary' non è letto come id.
    """
    ids = _audit_customer_ids(session, include_paid=include_paid)
    return {"to_sanitize_count": len(ids), "customer_ids": sorted(ids)}


@router.get("/bonifica-suggestions")
async def bonifica_suggestions(session: Session = Depends(get_session)):
    """Lista di revisione della bonifica P.IVA in blocco: TUTTI i clienti
    bonificabili in un colpo (l'owner ne ha ~115).

    Un cliente entra col criterio IDENTICO a bonifica_piva dell'audit: SENZA
    P.IVA valida sul profilo + le sue fatture non-pagate che portano una P.IVA
    valida ne condividono UNA SOLA. I clienti con P.IVA DIVERSE sulle fatture
    (conflict = "forse due clienti") NON entrano — sono un caso a parte.

    Ordinati per confidence desc, poi total_overdue desc (i più certi e più
    pesanti in cima), con l'id come tiebreaker stabile.

    EFFICIENZA: UNA sola query aggregata (Invoice ⋈ Customer delle non-pagate),
    poi raggruppamento in Python. NIENTE query-per-cliente → nessun N+1 su
    115+. Definito PRIMA di /{customer_id} così 'bonifica-suggestions' non è
    letto come id.
    """
    rows = (
        session.query(Invoice, Customer)
        .join(Customer, Invoice.customer_id == Customer.id)
        .filter(Invoice.status != "paid")
        .all()
    )
    by_customer = {}  # customer_id → {"customer": Customer, "invoices": [Invoice]}
    for inv, cust in rows:
        entry = by_customer.setdefault(cust.id, {"customer": cust, "invoices": []})
        entry["invoices"].append(inv)

    items = []
    for cid, entry in by_customer.items():
        cust = entry["customer"]
        outcome = _single_shared_piva(cust, entry["invoices"])
        if not outcome or outcome[0] != "single":
            # Esclusi: già con P.IVA, senza P.IVA da assegnare, o CONFLITTO.
            continue
        _, the_piva, carrier_ids, confidence = outcome
        # Scaduto = universo overdue_clause (status != paid & days_overdue > 0);
        # le fatture qui sono già non-pagate, resta il solo giorni>0.
        total_overdue = sum(
            inv.amount_due for inv in entry["invoices"] if (inv.days_overdue or 0) > 0
        )
        items.append({
            "customer_id": cid,
            "ragione_sociale": cust.ragione_sociale,
            "piva_suggerita": the_piva,
            "invoice_count": len(carrier_ids),
            "confidence": confidence,
            "total_overdue": round(float(total_overdue), 2),
        })

    items.sort(key=lambda i: (-i["confidence"], -i["total_overdue"], i["customer_id"]))
    return {"total": len(items), "items": items}


class BonificaBulkRequest(BaseModel):
    customer_ids: List[int]


@router.post("/bonifica-piva/bulk")
async def bonifica_piva_bulk(
    body: BonificaBulkRequest,
    session: Session = Depends(get_session),
):
    """Applica la bonifica P.IVA a PIÙ clienti in un colpo (dalla lista di
    revisione). Per ogni id RI-VALIDA server-side il criterio (non ci si fida
    del client) e poi assegna la P.IVA al cliente — stessa logica di
    assign-piva-to-customer (customer.partita_iva = P.IVA valida). La cascade è
    automatica: verify è calcolato dal vivo, quindi assegnata la P.IVA tutte le
    fatture del cliente (presenti e future) con quella P.IVA diventano verdi.

    Esito per-id:
    - applied          — P.IVA assegnata;
    - skipped_conflict — nel frattempo le fatture portano P.IVA diverse;
    - skipped_has_piva — il cliente ha già una P.IVA valida (idempotenza:
                         ri-eseguire su un bonificato NON è un errore);
    - skipped_no_piva  — nessuna P.IVA valida da assegnare (dati cambiati);
    - not_found        — cliente inesistente.

    Definito PRIMA di /{customer_id} (path a due segmenti, ma per coerenza sta
    con l'altra rotta di bonifica). Efficiente anche qui: i clienti e le loro
    fatture non-pagate si caricano in DUE query, non una per id.
    """
    try:
        # Dedup preservando l'ordine ricevuto (l'esito segue l'ordine input).
        ids = list(dict.fromkeys(body.customer_ids or []))
        if not ids:
            return {"applied": 0, "results": []}

        customers = {
            c.id: c
            for c in session.query(Customer).filter(Customer.id.in_(ids)).all()
        }
        inv_by_customer = {}
        for inv in session.query(Invoice).filter(
            Invoice.customer_id.in_(ids),
            Invoice.status != "paid",
        ).all():
            inv_by_customer.setdefault(inv.customer_id, []).append(inv)

        results = []
        applied = 0
        for cid in ids:
            cust = customers.get(cid)
            if cust is None:
                results.append({"customer_id": cid, "result": "not_found"})
                continue
            # Idempotenza + guardia: se ha già una P.IVA valida, niente da fare.
            if validate_piva(cust.partita_iva) is not None:
                results.append({"customer_id": cid, "result": "skipped_has_piva"})
                continue
            outcome = _single_shared_piva(cust, inv_by_customer.get(cid, []))
            if outcome and outcome[0] == "single":
                _, the_piva, carrier_ids, confidence = outcome
                # Stessa scrittura di assign-piva-to-customer: solo la P.IVA
                # (ragione_sociale_normalized resta invariato).
                cust.partita_iva = the_piva
                session.add(ActivityLog(
                    action="audit_assign_piva",
                    entity_type="customer",
                    entity_id=cid,
                    details={
                        "customer_id": cid,
                        "piva": the_piva,
                        "invoice_count": len(carrier_ids),
                        "confidence": confidence,
                        "bulk": True,
                    },
                ))
                applied += 1
                results.append({
                    "customer_id": cid, "result": "applied", "piva": the_piva,
                })
            elif outcome and outcome[0] == "conflict":
                results.append({
                    "customer_id": cid, "result": "skipped_conflict",
                    "pivas": outcome[1],
                })
            else:
                results.append({"customer_id": cid, "result": "skipped_no_piva"})

        session.commit()
        logger.info(
            f"Bonifica P.IVA in blocco: {applied}/{len(ids)} clienti bonificati"
        )
        return {"applied": applied, "results": results}

    except Exception as e:
        logger.error(f"Error in bulk P.IVA bonifica: {e}", exc_info=True)
        session.rollback()
        raise


@router.get("/{customer_id}")
async def get_customer_detail(
    customer_id: int,
    session: Session = Depends(get_session),
):
    """Get detailed information for a customer including their invoices."""
    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        # Get all invoices for this customer
        invoices = session.query(Invoice).filter(
            Invoice.customer_id == customer_id
        ).order_by(Invoice.due_date.desc()).all()

        # Calculate totals excluding paid invoices
        total_amount = sum(inv.amount for inv in invoices if inv.status != "paid")
        total_due = sum(inv.amount_due for inv in invoices if inv.status != "paid")

        invoice_list = [
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "amount": float(inv.amount),
                "amount_due": float(inv.amount_due),
                "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
                "due_date": inv.due_date.isoformat() if inv.due_date else None,
                "due_date_source": inv.due_date_source or ("assumed" if inv.due_date else None),
                "days_overdue": inv.days_overdue,
                "status": inv.status,
                "source_platform": inv.source_platform,
                "shopify_order_number": inv.shopify_order_number,
                # Controllo puntuale P.IVA + ragione sociale (istantaneo:
                # confronta dati già in DB, nessun sync). Semaforo per-riga.
                "verification": verify_invoice_customer(inv, customer),
                # "Verificata a mano" (Segna verificato): senza questo campo
                # lo stato vive solo nella sessione del browser e un
                # hard-reload fa ricomparire il ⚠ su una fattura già
                # controllata dall'operatore.
                "reviewed": inv.audit_reviewed_at is not None,
            }
            for inv in invoices
        ]

        # Fatture in QUARANTENA suggerite a QUESTO cliente (customer_id NULL +
        # suggested_customer_id → lui): senza questa lista non compaiono MAI
        # sul profilo e l'operatore che le cerca qui conclude che il sistema
        # non le conosce (caso Belfiore 655/2026). Le 'paid' sono INCLUSE:
        # una quarantenata marcata pagata sparirebbe altrimenti ovunque
        # (/positions/suggestions filtra status != 'paid').
        pending_invoices = (
            session.query(Invoice)
            .filter(
                Invoice.customer_id.is_(None),
                Invoice.suggested_customer_id == customer_id,
            )
            .order_by(Invoice.due_date.desc())
            .all()
        )

        pending_suggestions = [
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "amount": float(inv.amount),
                "amount_due": float(inv.amount_due),
                "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
                "due_date": inv.due_date.isoformat() if inv.due_date else None,
                "days_overdue": inv.days_overdue,
                "status": inv.status,
                "suggested_method": inv.suggested_method,
                "suggested_score": inv.suggested_score,
                "customer_name_raw": inv.customer_name_raw,
                "source_platform": inv.source_platform,
                # Verifica anche il suggerimento contro QUESTO cliente:
                # aiuta a decidere Conferma/Rifiuta con lo stesso semaforo.
                "verification": verify_invoice_customer(inv, customer),
            }
            for inv in pending_invoices
        ]

        # Get recovery actions
        actions = (
            session.query(RecoveryAction)
            .filter(RecoveryAction.customer_id == customer_id)
            .order_by(RecoveryAction.created_at.desc())
            .limit(20)
            .all()
        )

        action_list = [
            {
                "id": a.id,
                "action_type": a.action_type,
                "scheduled_date": a.scheduled_date.isoformat() if a.scheduled_date else None,
                "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                "outcome": a.outcome,
                "notes": a.notes,
                "channel": a.channel,
                "invoice_ids": a.invoice_ids or [],
                "cancelled": bool(a.cancelled),
                "cancelled_reason": a.cancelled_reason,
                "case_id": a.case_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in actions
        ]

        # Pratica aperta: numerazione e tono contano SOLO le azioni di
        # questo ciclo di debito (più gli eventuali contatti ereditati da
        # una pratica archiviata), mai tutta la storia del cliente.
        open_case = get_open_case(session, customer_id)
        case_block = None
        contact_action_count = 0
        if open_case:
            contact_action_count = contact_count(session, open_case)
            today_start = business_day_start()
            sollecito_today = (
                session.query(func.count(RecoveryAction.id))
                .filter(
                    RecoveryAction.case_id == open_case.id,
                    RecoveryAction.channel.in_(["whatsapp_copy", "whatsapp_link"]),
                    RecoveryAction.completed_at >= today_start,
                    RecoveryAction.cancelled.isnot(True),
                )
                .scalar() or 0
            ) > 0
            case_block = {
                "id": open_case.id,
                "opened_at": open_case.opened_at.isoformat() if open_case.opened_at else None,
                "contact_count": contact_action_count,
                "inherited_contacts": open_case.inherited_contacts or 0,
                "reopened_after_archive": bool(open_case.reopened_after_archive),
                "sollecito_registered_today": sollecito_today,
            }

        return {
            "id": customer.id,
            "ragione_sociale": customer.ragione_sociale,
            "partita_iva": customer.partita_iva,
            "codice_fiscale": customer.codice_fiscale,
            "phone": customer.phone,
            "phones": customer.phones_json or [],
            "email": customer.email,
            "excluded": customer.excluded,
            # Nome bloccato dalla bonifica manuale (assign-name-to-customer):
            # la UI mostra il badge + "Sblocca nome" solo quando è True.
            "ragione_sociale_locked": bool(customer.ragione_sociale_locked),
            "source": customer.source,
            "phone_validated": customer.phone_validated,
            "shopify_id": customer.shopify_id,
            "tags": customer.tags,
            "recovery_status": customer.recovery_status,
            "next_action_date": customer.next_action_date.isoformat() if customer.next_action_date else None,
            "next_action_type": customer.next_action_type,
            "created_at": customer.created_at.isoformat(),
            "updated_at": customer.updated_at.isoformat(),
            "invoices": {
                "total_amount": float(total_amount),
                "total_due": float(total_due),
                "count": len(invoices),
                "items": invoice_list,
            },
            "pending_suggestions": pending_suggestions,
            "recovery_actions": action_list,
            "contact_action_count": contact_action_count,
            "case": case_block,
            # Intestazioni accettate (bonifica durevole): il tratto d'identità
            # che rende verdi le fatture con quella grafia, presenti e future.
            "accepted_names": [
                _accepted_name_dict(an) for an in customer.accepted_names
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching customer detail: {e}", exc_info=True)
        raise


@router.get("/{customer_id}/audit")
async def audit_customer(
    customer_id: int,
    include_paid: bool = Query(False, description="Audita anche le fatture pagate"),
    include_reviewed: bool = Query(False, description="Includi anche le fatture già segnate verificate"),
    session: Session = Depends(get_session),
):
    """Audit abbinamenti del SINGOLO cliente aperto: per ogni sua fattura
    confronta destinatario/P.IVA col cliente (verify_invoice_customer) e
    restituisce i problemi (warn/bad) con il metodo/score di abbinamento, più
    i suggerimenti pendenti verso questo cliente.

    A differenza dell'audit globale (/system/match-audit, che fa .all() su
    TUTTO il DB), qui si scandiscono SOLO le fatture di questo cliente: niente
    scansione dell'intera tabella, niente N+1 (nessun caricamento cliente
    ripetuto: il cliente è già in mano). È lo stesso motore di verify, quindi
    i livelli/verdetti coincidono col semaforo per-riga e con l'audit globale.

    Le fatture già verificate a mano (audit_reviewed_at) escono dai problemi,
    salvo include_reviewed=true; con include_paid=true copre anche le pagate.
    """
    customer = session.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    q = session.query(Invoice).filter(Invoice.customer_id == customer_id)
    if not include_paid:
        q = q.filter(Invoice.status != "paid")
    invoices = q.order_by(Invoice.due_date.desc()).all()

    # counts conta i problemi AZIONABILI: warn/bad già "Segnati verificati"
    # NON entrano (salvo include_reviewed=true) — restano in reviewed_count.
    # Così counts/total_problems/worst_verdict descrivono sempre ciò che
    # items mostra: senza questo, un cliente col suo unico problema già
    # verificato dichiarava total_problems=1 e tile rossa accanto al badge
    # "In ordine ✓" (il frontend affianca counts e problem_count).
    counts = {"ok": 0, "warn": 0, "bad": 0}
    items = []
    reviewed_count = 0
    worst = "ok"
    # TIER 1 — "Completa anagrafica" a livello CLIENTE: fra i problemi mostrati
    # raccolgo le P.IVA VALIDE portate dalle fatture. Se il cliente non ha
    # P.IVA e i problemi ne condividono UNA SOLA → si offre la bonifica a
    # livello cliente (l'assegnazione CASCADE su tutte, presenti e future).
    # Se ne portano di DIVERSE è segnale di cliente mis-raggruppato: niente
    # offerta, si espone la lista delle P.IVA distinte.
    piva_carriers = []  # (invoice_id, validated_piva)
    for inv in invoices:
        v = verify_invoice_customer(inv, customer)
        verdict = v["verdict"]
        if verdict == "ok":
            counts[verdict] += 1
            continue
        is_reviewed = inv.audit_reviewed_at is not None
        if is_reviewed:
            reviewed_count += 1
            if not include_reviewed:
                continue
        counts[verdict] += 1
        if verdict == "bad":
            worst = "bad"
        elif worst != "bad":
            worst = "warn"
        inv_piva = validate_piva(inv.customer_piva_raw)
        if inv_piva:
            # Confidence della bonifica: somiglianza fra la ragione sociale del
            # cliente e l'intestazione GREZZA della fattura — lo STESSO scorer
            # che verify riporta come "Somiglianza nomi: X%" (name_score), così
            # il pannello per-riga e la percentuale di certezza coincidono.
            name_conf = name_similarity_score(
                inv.customer_name_raw or "", customer.ragione_sociale or ""
            )
            piva_carriers.append((inv.id, inv_piva, name_conf))
        items.append({
            "invoice_id": inv.id,
            "invoice_number": inv.invoice_number,
            "amount_due": float(inv.amount_due),
            "status": inv.status,
            "match_method": inv.match_method,
            "match_score": inv.match_score,
            "name_score": v["name_score"],
            "verdict": verdict,
            "verification": v,
            "reviewed": is_reviewed,
            # La fattura ha una P.IVA valida e il cliente no: si può copiare
            # la P.IVA della fattura sul cliente con un click.
            "can_assign_piva": (
                validate_piva(inv.customer_piva_raw) is not None
                and validate_piva(customer.partita_iva) is None
            ),
        })

    # Suggerimenti pendenti in quarantena verso QUESTO cliente (customer_id
    # NULL + suggested_customer_id → lui): l'audit li segnala come "da
    # abbinare". Query scoped, nessun verify sull'intero DB.
    pending = (
        session.query(Invoice)
        .filter(
            Invoice.customer_id.is_(None),
            Invoice.suggested_customer_id == customer_id,
        )
        .order_by(Invoice.due_date.desc())
        .all()
    )
    pending_suggestions = [
        {
            "id": p.id,
            "invoice_number": p.invoice_number,
            "amount_due": float(p.amount_due),
            "customer_name_raw": p.customer_name_raw,
            "suggested_method": p.suggested_method,
            "suggested_score": p.suggested_score,
            "verification": verify_invoice_customer(p, customer),
        }
        for p in pending
    ]

    # TIER 1 — bonifica_piva a livello cliente. Solo se il cliente NON ha una
    # P.IVA valida: se ce l'ha, non c'è anagrafica da completare.
    bonifica_piva = None
    bonifica_piva_conflict = None
    if validate_piva(customer.partita_iva) is None and piva_carriers:
        distinct = sorted({p for _, p, _ in piva_carriers})
        if len(distinct) == 1:
            the_piva = distinct[0]
            carriers = [(iid, conf) for iid, p, conf in piva_carriers if p == the_piva]
            bonifica_piva = {
                "piva": the_piva,
                "invoice_count": len(carriers),
                # Una qualsiasi delle fatture del gruppo: l'endpoint
                # assign-piva-to-customer copia la P.IVA sul cliente (CASCADE).
                "invoice_id": carriers[0][0],
                # Certezza (0-100) = somiglianza nome MINIMA del gruppo (il caso
                # PEGGIORE, conservativo): 100 = ragione sociale identica →
                # quasi certezza; più bassa = da guardare prima di applicare.
                "confidence": min(conf for _, conf in carriers),
            }
        else:
            # P.IVA diverse fra le fatture: forse due clienti fusi per errore.
            bonifica_piva_conflict = distinct

    total_problems = counts["warn"] + counts["bad"]
    return {
        "customer_id": customer.id,
        "customer_name": customer.ragione_sociale,
        "customer_piva": customer.partita_iva,
        "counts": counts,
        "total_invoices": len(invoices),
        "total_problems": total_problems,
        "problem_count": len(items),
        "reviewed_count": reviewed_count,
        "pending_count": len(pending_suggestions),
        "worst_verdict": worst if items else ("warn" if pending_suggestions else "ok"),
        "items": items,
        "pending_suggestions": pending_suggestions,
        # TIER 1: presente SOLO quando c'è una singola P.IVA da assegnare al
        # cliente (altrimenti null); conflict = lista P.IVA distinte se le
        # fatture ne portano di diverse (avviso "forse due clienti").
        "bonifica_piva": bonifica_piva,
        "bonifica_piva_conflict": bonifica_piva_conflict,
    }


@router.get("/{customer_id}/neighbors")
async def get_customer_neighbors(
    customer_id: int,
    session: Session = Depends(get_session),
):
    """
    Get previous and next customer IDs for navigation.
    Based on overdue customers sorted by total_overdue desc.
    """
    from sqlalchemy import case
    try:
        # Get ordered list of customer IDs with overdue invoices (same as default sort)
        overdue_customers = (
            session.query(
                Customer.id,
                func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).label("total_overdue"),
            )
            .join(Invoice, Invoice.customer_id == Customer.id)
            .filter(
                Invoice.status != "paid",
                Customer.excluded.is_(False),
            )
            .group_by(Customer.id)
            .having(func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)) > 0)
            # Tiebreaker id: senza, i pari-merito su total_overdue rendono
            # prev/next non deterministici (riproducibile perfino su SQLite).
            .order_by(
                func.sum(case((Invoice.days_overdue > 0, Invoice.amount_due), else_=0)).desc(),
                Customer.id,
            )
            .all()
        )

        ids = [row[0] for row in overdue_customers]
        prev_id = None
        next_id = None

        if customer_id in ids:
            idx = ids.index(customer_id)
            if idx > 0:
                prev_id = ids[idx - 1]
            if idx < len(ids) - 1:
                next_id = ids[idx + 1]

        return {
            "prev_id": prev_id,
            "next_id": next_id,
            "position": ids.index(customer_id) + 1 if customer_id in ids else None,
            "total": len(ids),
        }

    except Exception as e:
        logger.error(f"Error fetching customer neighbors: {e}", exc_info=True)
        raise


@router.put("/{customer_id}/exclude")
async def toggle_customer_exclusion(
    customer_id: int,
    exclude: bool,
    session: Session = Depends(get_session),
):
    """Toggle the excluded flag for a customer."""
    try:
        customer = session.query(Customer).filter(Customer.id == customer_id).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        customer.excluded = exclude
        customer.updated_at = datetime.utcnow()
        session.commit()

        # Log activity
        activity = ActivityLog(
            action="customer_excluded" if exclude else "customer_included",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "excluded": exclude,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Customer {customer_id} exclusion status changed to {exclude}")

        return {
            "id": customer.id,
            "excluded": customer.excluded,
            "updated_at": customer.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating customer exclusion: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{customer_id}/unlock-name")
async def unlock_customer_name(
    customer_id: int,
    session: Session = Depends(get_session),
):
    """Sblocca la ragione sociale bonificata a mano (rename amministrativo).

    Via di ritorno da un rinomino sbagliato (assign-name-to-customer): senza
    questo endpoint un cliente con una sola fattura resterebbe nel limbo per
    sempre — il lock ferma il sync, nessuna fattura porta il vecchio nome,
    nessun endpoint modifica il nome. Rimosso il lock, il sync Shopify torna
    a governare il nome dal giro successivo.
    """
    try:
        customer = session.query(Customer).filter(
            Customer.id == customer_id
        ).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")
        if not customer.ragione_sociale_locked:
            raise HTTPException(
                status_code=400,
                detail="Il nome di questo cliente non è bloccato",
            )

        customer.ragione_sociale_locked = False
        session.commit()

        session.add(ActivityLog(
            action="audit_unlock_name",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
            },
        ))
        session.commit()

        logger.info(
            f"Nome del cliente {customer_id} "
            f"('{customer.ragione_sociale}') sbloccato: il sync torna a "
            f"governarlo"
        )
        return {"ok": True, "customer_id": customer.id, "locked": False}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unlocking customer name: {e}", exc_info=True)
        session.rollback()
        raise


@router.post("/{customer_id}/clear-piva")
async def clear_customer_piva(
    customer_id: int,
    session: Session = Depends(get_session),
):
    """Rimuove la P.IVA assegnata per errore: via di ritorno dalla bonifica.

    Simmetrico ad assign-piva-to-customer / bonifica-piva/bulk: se una P.IVA è
    stata copiata sul cliente sbagliato, la si azzera e le fatture tornano al
    loro esito naturale (verify dal vivo). Reversibilità dichiarata come nota #4
    dell'analisi.

    NB sul sync: azzerare qui è sicuro. Il sync scrive customer.partita_iva
    SOLO da Shopify (_sync_customers_task) e SOLO se Shopify riporta una P.IVA
    checksum-valida; un cliente bonificato senza P.IVA su Shopify non viene
    ri-toccato. (Il sync fatture FatturaPro non scrive mai la P.IVA sul cliente,
    solo su customer_piva_raw della fattura.)

    Guardie:
    - 404 se il cliente non esiste;
    - 400 se il cliente non ha una P.IVA da rimuovere (niente da azzerare).
    """
    try:
        customer = session.query(Customer).filter(
            Customer.id == customer_id
        ).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")
        old_piva = (customer.partita_iva or "").strip()
        if not old_piva:
            raise HTTPException(
                status_code=400,
                detail="Il cliente non ha una P.IVA da rimuovere",
            )

        customer.partita_iva = None
        session.commit()

        session.add(ActivityLog(
            action="audit_clear_piva",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "removed_piva": old_piva,
            },
        ))
        session.commit()

        logger.info(
            f"P.IVA '{old_piva}' rimossa dal cliente {customer_id} "
            f"('{customer.ragione_sociale}')"
        )
        return {"ok": True, "customer_id": customer.id, "partita_iva": None}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error clearing customer P.IVA: {e}", exc_info=True)
        session.rollback()
        raise


class AcceptedNameRequest(BaseModel):
    # Una delle due: il nome grezzo da accettare, oppure la fattura da cui
    # prenderlo (customer_name_raw). invoice_id ha la precedenza.
    name: Optional[str] = None
    invoice_id: Optional[int] = None


@router.post("/{customer_id}/accepted-names")
async def add_accepted_name(
    customer_id: int,
    body: AcceptedNameRequest,
    session: Session = Depends(get_session),
):
    """Conferma d'identità DUREVOLE: aggiunge un'intestazione accettata al
    cliente (tratto letto dal vivo da verify_invoice_customer).

    A differenza di mark-reviewed (per-fattura, one-off), questa conferma vale
    per TUTTE le fatture del cliente con quella intestazione — presenti e
    future — in un colpo, senza scritture per-fattura. Idempotente
    sull'UNIQUE (no-op se la grafia è già accettata) + ActivityLog.

    Guardie:
    - 404 se il cliente o la fattura non esistono;
    - 400 se manca sia name sia invoice_id, o l'intestazione è vuota / non
      normalizzabile.
    """
    try:
        customer = session.query(Customer).filter(
            Customer.id == customer_id
        ).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        raw = None
        source_invoice_id = None
        if body.invoice_id is not None:
            inv = session.query(Invoice).filter(
                Invoice.id == body.invoice_id
            ).first()
            if not inv:
                raise HTTPException(status_code=404, detail="Fattura non trovata")
            raw = (inv.customer_name_raw or "").strip()
            source_invoice_id = inv.id
        elif body.name is not None:
            raw = body.name.strip()
        else:
            raise HTTPException(
                status_code=400,
                detail="Serve 'name' o 'invoice_id' da cui prendere l'intestazione",
            )

        if not raw:
            raise HTTPException(
                status_code=400,
                detail="L'intestazione da accettare è vuota",
            )
        norm = normalize_ragione_sociale(raw)
        if not norm:
            raise HTTPException(
                status_code=400,
                detail="L'intestazione da accettare non è normalizzabile",
            )

        # Idempotente: no-op se la grafia (normalizzata) è già accettata.
        existing = session.query(CustomerAcceptedName).filter(
            CustomerAcceptedName.customer_id == customer_id,
            CustomerAcceptedName.name_normalized == norm,
        ).first()
        if existing:
            return {
                "ok": True,
                "already_present": True,
                "accepted_name": _accepted_name_dict(existing),
                "accepted_names": [
                    _accepted_name_dict(a) for a in customer.accepted_names
                ],
            }

        an = CustomerAcceptedName(
            customer_id=customer_id, name_normalized=norm, note=raw,
        )
        session.add(an)
        session.commit()

        session.add(ActivityLog(
            action="audit_accept_name",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "accepted_raw": raw,
                "name_normalized": norm,
                "invoice_id": source_invoice_id,
            },
        ))
        session.commit()

        logger.info(
            f"Intestazione '{raw}' (norm '{norm}') accettata per il cliente "
            f"{customer_id} ('{customer.ragione_sociale}')"
        )
        session.refresh(customer)
        return {
            "ok": True,
            "already_present": False,
            "accepted_name": _accepted_name_dict(an),
            "accepted_names": [
                _accepted_name_dict(a) for a in customer.accepted_names
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding accepted name: {e}", exc_info=True)
        session.rollback()
        raise


@router.delete("/{customer_id}/accepted-names/{name_or_id}")
async def remove_accepted_name(
    customer_id: int,
    name_or_id: str,
    session: Session = Depends(get_session),
):
    """Rimuove un'intestazione accettata (reversibilità della conferma).

    `name_or_id` può essere l'id della riga o l'intestazione normalizzata.
    Rimosso l'ultimo appiglio, le fatture con quella grafia tornano al loro
    esito naturale (warning/discordante) — il tratto d'identità è reversibile.
    """
    try:
        customer = session.query(Customer).filter(
            Customer.id == customer_id
        ).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        base = session.query(CustomerAcceptedName).filter(
            CustomerAcceptedName.customer_id == customer_id
        )
        row = None
        if name_or_id.isdigit():
            row = base.filter(CustomerAcceptedName.id == int(name_or_id)).first()
        if row is None:
            norm = normalize_ragione_sociale(name_or_id)
            if norm:
                row = base.filter(
                    CustomerAcceptedName.name_normalized == norm
                ).first()
        if not row:
            raise HTTPException(
                status_code=404, detail="Intestazione accettata non trovata"
            )

        removed = _accepted_name_dict(row)
        session.delete(row)
        session.commit()

        session.add(ActivityLog(
            action="audit_unaccept_name",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "name_normalized": removed["name_normalized"],
                "note": removed["note"],
            },
        ))
        session.commit()

        logger.info(
            f"Intestazione accettata '{removed['name_normalized']}' rimossa "
            f"dal cliente {customer_id}"
        )
        session.refresh(customer)
        return {
            "ok": True,
            "removed": removed,
            "accepted_names": [
                _accepted_name_dict(a) for a in customer.accepted_names
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing accepted name: {e}", exc_info=True)
        session.rollback()
        raise


@router.put("/{customer_id}/phone")
async def update_customer_phone(
    customer_id: int,
    phone: str,
    session: Session = Depends(get_session),
):
    """Update the phone number for a customer and sync to Shopify."""
    try:
        customer = session.query(Customer).filter(
            Customer.id == customer_id
        ).first()
        if not customer:
            raise HTTPException(
                status_code=404, detail="Customer not found"
            )

        old_phone = customer.phone
        customer.phone = phone
        customer.phone_validated = False
        customer.updated_at = datetime.utcnow()

        # Add/update in phones_json
        phones = customer.phones_json or []
        # Check if there's already a "Manuale" entry
        manual_found = False
        for p in phones:
            if p.get("source") == "manual":
                p["number"] = phone
                manual_found = True
                break
        if not manual_found:
            phones.insert(0, {
                "number": phone,
                "source": "manual",
                "label": "Manuale",
            })
        customer.phones_json = phones

        session.commit()

        # Sync to Shopify if customer has shopify_id
        shopify_synced = False
        if customer.shopify_id:
            try:
                from backend.connectors.shopify import (
                    ShopifyConnector,
                )
                shopify = ShopifyConnector()
                shopify_synced = shopify.update_customer_phone(
                    customer.shopify_id, phone
                )
            except Exception as e:
                logger.warning(
                    f"Could not sync phone to Shopify: {e}"
                )

        # Log activity
        activity = ActivityLog(
            action="phone_updated",
            entity_type="customer",
            entity_id=customer_id,
            details={
                "ragione_sociale": customer.ragione_sociale,
                "old_phone": old_phone,
                "new_phone": phone,
                "shopify_synced": shopify_synced,
            }
        )
        session.add(activity)
        session.commit()

        logger.info(
            f"Customer {customer_id} phone updated: "
            f"{old_phone} -> {phone} "
            f"(shopify={'ok' if shopify_synced else 'skip'})"
        )

        return {
            "id": customer.id,
            "phone": customer.phone,
            "phones": customer.phones_json or [],
            "phone_validated": customer.phone_validated,
            "shopify_synced": shopify_synced,
            "updated_at": customer.updated_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating customer phone: {e}", exc_info=True)
        session.rollback()
        raise


class CreateCustomerRequest(BaseModel):
    ragione_sociale: str
    partita_iva: Optional[str] = None
    codice_fiscale: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


@router.post("")
async def create_customer(
    body: CreateCustomerRequest,
    session: Session = Depends(get_session),
):
    """Create a new customer manually (for data corrections)."""
    try:
        ragione_sociale = body.ragione_sociale
        partita_iva = body.partita_iva
        codice_fiscale = body.codice_fiscale
        phone = body.phone
        email = body.email

        existing = session.query(Customer).filter(
            Customer.ragione_sociale == ragione_sociale
        ).first()
        if existing:
            return {
                "id": existing.id,
                "ragione_sociale": existing.ragione_sociale,
                "partita_iva": existing.partita_iva,
                "already_existed": True,
            }

        import re
        normalized = re.sub(r'[^a-z0-9]', '', ragione_sociale.lower())

        customer = Customer(
            ragione_sociale=ragione_sociale,
            ragione_sociale_normalized=normalized,
            partita_iva=partita_iva,
            codice_fiscale=codice_fiscale,
            phone=phone,
            email=email,
            source="manual",
        )
        session.add(customer)
        session.commit()

        activity = ActivityLog(
            action="customer_created",
            entity_type="customer",
            entity_id=customer.id,
            details={
                "ragione_sociale": ragione_sociale,
                "source": "manual",
                "reason": "data_correction",
            }
        )
        session.add(activity)
        session.commit()

        logger.info(f"Customer created manually: {ragione_sociale} (ID: {customer.id})")

        return {
            "id": customer.id,
            "ragione_sociale": customer.ragione_sociale,
            "partita_iva": customer.partita_iva,
            "already_existed": False,
        }

    except Exception as e:
        logger.error(f"Error creating customer: {e}", exc_info=True)
        session.rollback()
        raise
