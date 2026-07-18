"""Dashboard API endpoints."""

import logging
from datetime import date, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import func, extract
from sqlalchemy.orm import Session, joinedload

from backend.database import (
    get_session, Invoice, Customer, RecoveryAction, RecoveryCase,
    OverdueSnapshot,
)
from backend.engine.overdue import (
    overdue_clause, workable_clause, stage_expr,
    compute_overdue_buckets, compute_recuperato_certo,
    first_recovery_action_subquery, recovered_invoice_clause,
    OVERDUE_BUCKETS, CASE_STAGES, RECOVERY_ACTION_TYPES,
)
from backend.engine.cases import business_day_start

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_dashboard(session: Session = Depends(get_session)):
    """
    Get dashboard overview with key statistics.
    Optimized: only returns the stats actually used by the frontend.

    total_scaduto è l'UNIVERSO dello scaduto (contestati/esclusi/orfane
    compresi): è la cima della cascata di /riconciliazione, che lo spiega
    riga per riga. Il numero di testa e la cascata che lo scompone devono
    essere lo stesso numero, o i conti non tornano di nuovo.
    """
    try:
        # Total OVERDUE amount (only invoices past due date)
        total_scaduto = session.query(func.sum(Invoice.amount_due)).filter(
            overdue_clause()
        ).scalar() or 0.0

        total_fatture_scadute = session.query(func.count(Invoice.id)).filter(
            overdue_clause()
        ).scalar() or 0

        total_clienti_scaduti = session.query(
            func.count(func.distinct(Invoice.customer_id))
        ).filter(
            overdue_clause(),
            Invoice.customer_id.isnot(None),
        ).scalar() or 0

        # Total number of positions (excluding paid)
        total_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status != "paid"
        ).scalar() or 0

        total_customers = session.query(func.count(Customer.id)).scalar() or 0

        return {
            "total_scaduto": float(total_scaduto),
            "total_fatture_scadute": total_fatture_scadute,
            "total_clienti_scaduti": total_clienti_scaduti,
            "total_positions": total_positions,
            "total_customers": total_customers,
        }

    except Exception as e:
        logger.error(f"Error fetching dashboard data: {e}", exc_info=True)
        raise


@router.get("/search")
async def search_dashboard(
    q: str,
    session: Session = Depends(get_session),
):
    """Search customers by ragione sociale or partita IVA. Returns top 20 matches with overdue stats."""
    from sqlalchemy import case, or_
    try:
        search_term = f"%{q.strip()}%"
        results = (
            session.query(
                Customer,
                func.sum(case((overdue_clause(), Invoice.amount_due), else_=0)).label("total_overdue"),
                func.count(case((overdue_clause(), 1), else_=None)).label("overdue_count"),
            )
            .outerjoin(Invoice, (Invoice.customer_id == Customer.id) & (Invoice.status != "paid"))
            .filter(
                Customer.excluded.is_(False),
                or_(
                    Customer.ragione_sociale.ilike(search_term),
                    Customer.partita_iva.ilike(search_term),
                )
            )
            .group_by(Customer.id)
            .order_by(func.sum(case((overdue_clause(), Invoice.amount_due), else_=0)).desc())
            .limit(20)
            .all()
        )

        return {
            "results": [
                {
                    "id": c.id,
                    "ragione_sociale": c.ragione_sociale,
                    "partita_iva": c.partita_iva,
                    "phone": c.phone,
                    "recovery_status": c.recovery_status,
                    "total_overdue": float(total_overdue or 0),
                    "overdue_count": int(overdue_count or 0),
                }
                for c, total_overdue, overdue_count in results
            ],
            "total": len(results),
        }
    except Exception as e:
        logger.error(f"Error searching dashboard: {e}", exc_info=True)
        raise


@router.get("/todos")
async def get_todos(session: Session = Depends(get_session)):
    """
    Get todo list for the dashboard — pending recovery actions and customers needing attention.
    Groups: overdue (past due), today, upcoming (next 14 days), and idle customers with overdue invoices.

    Optimized: pre-loads all overdue stats in a single query instead of N+1.
    """
    try:
        today = date.today()

        # Pre-load ALL overdue stats per customer in ONE query (avoids N+1)
        # Definizione LAVORABILE: un todo propone un'azione, e il motore
        # rifiuta i contestati. Contarli qui significa promettere lavoro che
        # "Copia Messaggio" poi respinge con no_overdue.
        overdue_stats_raw = (
            session.query(
                Invoice.customer_id,
                func.count(Invoice.id).label("overdue_count"),
                func.sum(Invoice.amount_due).label("total_overdue"),
                func.min(Invoice.due_date).label("oldest_due_date"),
                func.max(Invoice.days_overdue).label("max_days_overdue"),
            )
            .outerjoin(Customer, Invoice.customer_id == Customer.id)
            .filter(workable_clause())
            .group_by(Invoice.customer_id)
            .all()
        )
        overdue_by_customer = {
            row[0]: {
                "overdue_count": row[1],
                "total_overdue": float(row[2] or 0),
                "oldest_due_date": row[3].isoformat() if row[3] else None,
                "max_days_overdue": row[4] or 0,
            }
            for row in overdue_stats_raw
        }

        # 1. Pending recovery actions (not completed, scheduled within 14 days)
        # Use joinedload to avoid N+1 lazy-load queries with NullPool
        cutoff_date = today + timedelta(days=14)
        pending_actions = (
            session.query(RecoveryAction)
            .join(Customer)
            .options(joinedload(RecoveryAction.customer))
            .filter(
                RecoveryAction.completed_at.is_(None),
                RecoveryAction.cancelled.isnot(True),
                RecoveryAction.scheduled_date.isnot(None),
                RecoveryAction.scheduled_date <= cutoff_date,
                Customer.excluded.is_(False),
            )
            .order_by(RecoveryAction.scheduled_date.asc())
            .all()
        )

        # 2. Idle customers with overdue invoices (need first contact)
        # Stessa definizione LAVORABILE: il cliente con sole fatture
        # contestate non è un todo — il motore non ha nulla da sollecitare.
        idle_customers_with_overdue = (
            session.query(
                Customer,
                func.count(Invoice.id).label("overdue_count"),
                func.sum(Invoice.amount_due).label("total_overdue"),
            )
            .join(Invoice, Invoice.customer_id == Customer.id)
            .filter(
                Customer.recovery_status == "idle",
                workable_clause(),
            )
            .group_by(Customer.id)
            .having(func.count(Invoice.id) > 0)
            .all()
        )

        todos = []
        seen_customer_ids = set()

        # Build action-based todos (NO extra queries — use pre-loaded stats)
        for action in pending_actions:
            cid = action.customer_id
            seen_customer_ids.add(cid)
            stats = overdue_by_customer.get(cid, {
                "overdue_count": 0, "total_overdue": 0,
                "oldest_due_date": None, "max_days_overdue": 0,
            })

            sched = action.scheduled_date
            if sched < today:
                priority = "overdue"
            elif sched == today:
                priority = "today"
            else:
                priority = "upcoming"

            todos.append({
                "id": f"action_{action.id}",
                "type": "action",
                "priority": priority,
                "customer_id": cid,
                "customer_name": action.customer.ragione_sociale,
                "partita_iva": action.customer.partita_iva,
                "phone": action.customer.phone,
                "action_type": action.action_type,
                "scheduled_date": sched.isoformat(),
                "notes": action.notes,
                "overdue_count": stats["overdue_count"],
                "total_overdue": stats["total_overdue"],
                "oldest_due_date": stats["oldest_due_date"],
                "max_days_overdue": stats["max_days_overdue"],
                "recovery_status": action.customer.recovery_status,
            })

        # Build idle-customer todos (need first contact)
        for cust, overdue_count, total_overdue in idle_customers_with_overdue:
            if cust.id in seen_customer_ids:
                continue
            stats = overdue_by_customer.get(cust.id, {
                "oldest_due_date": None, "max_days_overdue": 0,
            })
            todos.append({
                "id": f"idle_{cust.id}",
                "type": "new_contact",
                "priority": "new",
                "customer_id": cust.id,
                "customer_name": cust.ragione_sociale,
                "partita_iva": cust.partita_iva,
                "phone": cust.phone,
                "action_type": "first_contact",
                "scheduled_date": today.isoformat(),
                "notes": None,
                "overdue_count": overdue_count,
                "total_overdue": float(total_overdue or 0),
                "oldest_due_date": stats.get("oldest_due_date"),
                "max_days_overdue": stats.get("max_days_overdue", 0),
                "recovery_status": "idle",
            })

        # Sort: overdue first, then today, then new, then upcoming
        priority_order = {"overdue": 0, "today": 1, "new": 2, "upcoming": 3}
        todos.sort(key=lambda t: (priority_order.get(t["priority"], 9), t.get("scheduled_date", "")))

        return {
            "todos": todos,
            "total": len(todos),
            "counts": {
                "overdue": sum(1 for t in todos if t["priority"] == "overdue"),
                "today": sum(1 for t in todos if t["priority"] == "today"),
                "new": sum(1 for t in todos if t["priority"] == "new"),
                "upcoming": sum(1 for t in todos if t["priority"] == "upcoming"),
            },
        }

    except Exception as e:
        logger.error(f"Error fetching todos: {e}", exc_info=True)
        raise


@router.get("/calendar")
async def get_calendar(
    session: Session = Depends(get_session),
    year: int = None,
    month: int = None,
):
    """
    Get calendar data for recovery actions.
    Returns actions grouped by date for a given month.
    Also returns overdue counts (past actions not completed).
    """
    try:
        today = date.today()
        if not year:
            year = today.year
        if not month:
            month = today.month

        # Get first and last day of month (with buffer for display)
        first_day = date(year, month, 1)
        if month == 12:
            last_day = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(year, month + 1, 1) - timedelta(days=1)

        # Extend range to show surrounding weeks
        start = first_day - timedelta(days=first_day.weekday())  # Monday of first week
        end = last_day + timedelta(days=(6 - last_day.weekday()))  # Sunday of last week

        # Get all scheduled actions in range (both pending and completed)
        # Use joinedload to avoid N+1 lazy-load queries with NullPool
        actions = (
            session.query(RecoveryAction)
            .join(Customer)
            .options(joinedload(RecoveryAction.customer))
            .filter(
                RecoveryAction.scheduled_date >= start,
                RecoveryAction.scheduled_date <= end,
                RecoveryAction.cancelled.isnot(True),
                Customer.excluded.is_(False),
            )
            .order_by(RecoveryAction.scheduled_date.asc())
            .all()
        )

        # Pre-load overdue stats
        overdue_stats_raw = (
            session.query(
                Invoice.customer_id,
                func.count(Invoice.id).label("cnt"),
                func.sum(Invoice.amount_due).label("tot"),
            )
            .filter(
                overdue_clause(),
                Invoice.customer_id.isnot(None),
            )
            .group_by(Invoice.customer_id)
            .all()
        )
        overdue_map = {r[0]: {"count": r[1], "total": float(r[2] or 0)} for r in overdue_stats_raw}

        # Group by date
        by_date = {}
        for a in actions:
            d = a.scheduled_date.isoformat()
            if d not in by_date:
                by_date[d] = []
            stats = overdue_map.get(a.customer_id, {"count": 0, "total": 0})
            by_date[d].append({
                "id": a.id,
                "customer_id": a.customer_id,
                "customer_name": a.customer.ragione_sociale,
                "phone": a.customer.phone,
                "action_type": a.action_type,
                "notes": a.notes,
                "recovery_status": a.customer.recovery_status,
                "overdue_count": stats["count"],
                "total_overdue": stats["total"],
                "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                "outcome": a.outcome,
            })

        # Count overdue actions (scheduled before today, not completed)
        overdue_count = (
            session.query(func.count(RecoveryAction.id))
            .join(Customer)
            .filter(
                RecoveryAction.scheduled_date < today,
                RecoveryAction.completed_at.is_(None),
                RecoveryAction.cancelled.isnot(True),
                Customer.excluded.is_(False),
            )
            .scalar() or 0
        )

        return {
            "year": year,
            "month": month,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "overdue_count": overdue_count,
            "days": by_date,
        }

    except Exception as e:
        logger.error(f"Error fetching calendar: {e}", exc_info=True)
        raise


@router.get("/stats")
async def get_stats(session: Session = Depends(get_session)):
    """
    Get summary statistics for the dashboard.

    Returns simple counts and totals for quick display.
    """
    try:
        total_crediti = session.query(func.sum(Invoice.amount_due)).scalar() or 0.0
        total_positions = session.query(func.count(Invoice.id)).scalar() or 0
        total_customers = session.query(func.count(Customer.id)).scalar() or 0

        # Count by status
        open_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "open"
        ).scalar() or 0

        contacted_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "contacted"
        ).scalar() or 0

        escalated_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "escalated"
        ).scalar() or 0

        paid_positions = session.query(func.count(Invoice.id)).filter(
            Invoice.status == "paid"
        ).scalar() or 0

        # Pratiche di recupero
        open_cases = session.query(func.count(RecoveryCase.id)).filter(
            RecoveryCase.status == "open"
        ).scalar() or 0

        solleciti_inviati = session.query(func.count(RecoveryAction.id)).filter(
            RecoveryAction.completed_at.isnot(None),
            RecoveryAction.cancelled.isnot(True),
            RecoveryAction.action_type.in_(["first_contact", "second_contact"]),
        ).scalar() or 0

        return {
            "total_crediti": float(total_crediti),
            "total_positions": total_positions,
            "total_customers": total_customers,
            "open_positions": open_positions,
            "contacted_positions": contacted_positions,
            "escalated_positions": escalated_positions,
            "paid_positions": paid_positions,
            "open_cases": open_cases,
            "solleciti_inviati": solleciti_inviati,
        }

    except Exception as e:
        logger.error(f"Error fetching statistics: {e}", exc_info=True)
        raise


@router.get("/attivita")
async def get_attivita(session: Session = Depends(get_session)):
    """
    Get data for the Attività page:
    1. contacted: customers with recovery actions (not idle/archived)
    2. incassati: ONLY customers who received recovery actions AND then paid

    Performance: all data fetched in batch queries (no N+1).
    """
    from sqlalchemy import case  # noqa: F811
    try:
        # ── CONTACTED ACCOUNTS ──
        contacted_raw = (
            session.query(
                Customer,
                func.count(func.distinct(
                    case((overdue_clause(), Invoice.id), else_=None)
                )).label("overdue_count"),
                func.sum(
                    case((overdue_clause(), Invoice.amount_due), else_=0)
                ).label("total_overdue"),
            )
            .outerjoin(Invoice, Invoice.customer_id == Customer.id)
            .filter(
                Customer.excluded.is_(False),
                Customer.recovery_status.notin_(["idle", "archived"]),
            )
            .group_by(Customer.id)
            # Exclude customers with no overdue invoices (nothing to recover)
            .having(
                func.count(func.distinct(
                    case((overdue_clause(), Invoice.id), else_=None)
                )) > 0
            )
            .order_by(Customer.next_action_date.asc().nullslast())
            .all()
        )

        # Batch: get last recovery action per customer (avoid N+1)
        contacted_ids = [c.id for c, _, _ in contacted_raw]
        last_actions_map = {}
        if contacted_ids:
            # Subquery: max created_at per customer for contact actions
            from sqlalchemy import and_
            ra_sub = (
                session.query(
                    RecoveryAction.customer_id,
                    func.max(RecoveryAction.created_at).label("max_created"),
                )
                .filter(
                    RecoveryAction.customer_id.in_(contacted_ids),
                    RecoveryAction.action_type.in_(
                        ["first_contact", "second_contact", "lawyer"]
                    ),
                )
                .group_by(RecoveryAction.customer_id)
                .subquery()
            )
            last_actions_raw = (
                session.query(RecoveryAction)
                .join(
                    ra_sub,
                    and_(
                        RecoveryAction.customer_id == ra_sub.c.customer_id,
                        RecoveryAction.created_at == ra_sub.c.max_created,
                    ),
                )
                .all()
            )
            for la in last_actions_raw:
                last_actions_map[la.customer_id] = la

        contacted = []
        for cust, overdue_count, total_overdue in contacted_raw:
            last_action = last_actions_map.get(cust.id)
            last_date = None
            if last_action:
                if last_action.completed_at:
                    last_date = last_action.completed_at.strftime("%Y-%m-%d")
                else:
                    last_date = last_action.created_at.strftime("%Y-%m-%d")

            contacted.append({
                "id": cust.id,
                "ragione_sociale": cust.ragione_sociale,
                "partita_iva": cust.partita_iva,
                "phone": cust.phone,
                "recovery_status": cust.recovery_status,
                "next_action_date": (
                    cust.next_action_date.isoformat()
                    if cust.next_action_date else None
                ),
                "next_action_type": cust.next_action_type,
                "last_contact_date": last_date,
                "last_action_type": (
                    last_action.action_type if last_action else None
                ),
                "last_outcome": (
                    last_action.outcome if last_action else None
                ),
                "overdue_count": int(overdue_count or 0),
                "total_overdue": float(total_overdue or 0),
            })

        # ── INCASSATI ──
        # ONLY invoices paid AFTER the first recovery action on that customer,
        # and issued BEFORE it (una fattura nuova non era da recuperare).
        # Definizione condivisa (engine/overdue.py): niente azioni annullate
        # (5a), niente fatture emesse dopo il sollecito (5c).
        first_action_sub = first_recovery_action_subquery(session)

        incassati_raw = (
            session.query(
                Customer.id,
                Customer.ragione_sociale,
                Customer.partita_iva,
                Customer.recovery_status,
                func.count(Invoice.id).label("paid_count"),
                func.sum(Invoice.amount).label("total_paid"),
                func.max(Invoice.updated_at).label("last_payment"),
            )
            .join(Invoice, Invoice.customer_id == Customer.id)
            .join(
                first_action_sub,
                Customer.id == first_action_sub.c.customer_id,
            )
            .filter(
                Invoice.status == "paid",
                Invoice.updated_at >= first_action_sub.c.first_action,
                Customer.excluded.is_(False),
                recovered_invoice_clause(first_action_sub),
            )
            .group_by(
                Customer.id, Customer.ragione_sociale,
                Customer.partita_iva, Customer.recovery_status,
            )
            .order_by(func.max(Invoice.updated_at).desc())
            .all()
        )

        # Batch: remaining overdue per customer (avoid N+1)
        incassati_ids = [row[0] for row in incassati_raw]
        remaining_map = {}
        if incassati_ids:
            remaining_raw = (
                session.query(
                    Invoice.customer_id,
                    func.count(Invoice.id),
                )
                .filter(
                    Invoice.customer_id.in_(incassati_ids),
                    overdue_clause(),
                )
                .group_by(Invoice.customer_id)
                .all()
            )
            for cid, cnt in remaining_raw:
                remaining_map[cid] = cnt

        incassati = []
        for row in incassati_raw:
            remaining = remaining_map.get(row[0], 0)
            incassati.append({
                "id": row[0],
                "ragione_sociale": row[1],
                "partita_iva": row[2],
                "recovery_status": row[3],
                "paid_count": row[4],
                "total_paid": float(row[5] or 0),
                "last_payment": (
                    row[6].strftime("%Y-%m-%d") if row[6] else None
                ),
                "fully_resolved": remaining == 0,
            })

        return {
            "contacted": contacted,
            "incassati": incassati,
            "summary": {
                "total_contacted": len(contacted),
                "total_incassati": len(incassati),
                "total_recovered": sum(
                    i["total_paid"] for i in incassati
                ),
                "fully_resolved": sum(
                    1 for i in incassati if i["fully_resolved"]
                ),
            },
        }

    except Exception as e:
        logger.error(f"Error fetching attivita data: {e}", exc_info=True)
        raise


@router.get("/riconciliazione")
async def get_riconciliazione(session: Session = Depends(get_session)):
    """La cascata che spiega il numero di testa, scalino per scalino.

    Nasce da una segnalazione precisa: "vedo da pagare 674.378, poi sotto
    460.932, e 197.033 recuperato: i conti non tornano". Non tornavano per
    costruzione — erano tre risposte a tre domande diverse, su tre
    popolazioni diverse, in due colonne diverse, su due pagine diverse.

    Qui c'è UNA popolazione (l'universo dello scaduto) divisa in categorie
    MUTUAMENTE ESCLUSIVE, e vale l'identità:

        scaduto_totale == non_abbinati + esclusi + contestati + lavorabile

    L'esclusività è strutturale, non aritmetica: un CASE SQL (bucket_expr)
    assegna ogni riga a un solo ramo. Le condizioni si sovrappongono nella
    realtà — una fattura può essere orfana E contestata, un cliente escluso
    può avere fatture contestate — e sommare secchielli sovrapposti avrebbe
    ricostruito esattamente il bug che questo endpoint cura.

    Un credito escluso o contestato ESISTE ancora: non lo si insegue, ma
    resta tracciato e la cascata lo chiude.
    """
    try:
        # ── La cascata: UNA query, un CASE, nessuna somma sovrapposta ──
        # compute_overdue_buckets vive in engine/overdue.py e lo condivide con
        # lo snapshot storico: una definizione sola, non due che divergono.
        buckets = compute_overdue_buckets(session)
        per_bucket = {b: buckets[b] for b in OVERDUE_BUCKETS}

        # ── Il lavorabile per stato pratica (deve chiudere a sua volta) ──
        stage_rows = (
            session.query(
                stage_expr().label("stato"),
                func.count(Invoice.id).label("fatture"),
                func.sum(Invoice.amount_due).label("importo"),
                func.count(func.distinct(Invoice.customer_id)).label("clienti"),
            )
            .outerjoin(Customer, Invoice.customer_id == Customer.id)
            .filter(workable_clause())
            .group_by(stage_expr())
            .all()
        )
        per_stato = {
            s: {"fatture": 0, "importo": 0.0, "clienti": 0} for s in CASE_STAGES
        }
        for stato, fatture, importo, clienti in stage_rows:
            per_stato[stato] = {
                "fatture": int(fatture or 0),
                "importo": float(importo or 0),
                "clienti": int(clienti or 0),
            }

        totale = buckets["scaduto_totale"]

        cascata = {
            "scaduto_totale": {
                **totale,
                "label": "Scaduto totale",
                "descrizione": "Tutto il non pagato oltre la scadenza.",
            },
            "non_abbinati": {
                **per_bucket["non_abbinati"],
                "label": "Non abbinati",
                "descrizione": "Fatture senza cliente: da abbinare prima di poterle inseguire.",
            },
            "esclusi": {
                **per_bucket["esclusi"],
                "label": "Esclusi",
                "descrizione": "Clienti esclusi dal recupero: il credito esiste, non lo si insegue.",
            },
            "contestati": {
                **per_bucket["contestati"],
                "label": "Contestati",
                "descrizione": "Fatture contestate: il motore non le sollecita.",
            },
            "lavorabile": {
                **per_bucket["lavorabile"],
                "label": "Lavorabile",
                "descrizione": "Lo scaduto che il motore insegue davvero.",
                "per_stato": per_stato,
            },
        }

        return {
            "cascata": cascata,
            "recuperato": _recuperato(session),
            "precedenza": list(OVERDUE_BUCKETS),
        }

    except Exception as e:
        logger.error(f"Error fetching riconciliazione: {e}", exc_info=True)
        raise


@router.get("/evoluzione")
async def get_evoluzione(
    giorni: int = 90, session: Session = Depends(get_session)
):
    """La serie storica dello scaduto: il grafico dell'evoluzione nel tempo.

    Ogni punto è lo snapshot di un giorno (uno solo per giorno): scaduto
    totale, i bucket della cascata, il lavorabile e il recuperato certo
    (cumulato). Ordinata per data CRESCENTE, così il grafico scorre da
    sinistra a destra.

    `giorni` è la finestra (default 90). Se nessuno snapshot è stato ancora
    registrato — lo storico parte dal primo sync dopo il rilascio di questa
    funzione — la serie è VUOTA, non un 500: il grafico mostrerà "nessun
    dato" invece di rompersi.

    Ogni punto espone `stimato`: True per le righe ricostruite dal backfill
    storico (proiezione dalle date fattura), False per gli snapshot veri.
    Il grafico tratteggia le stime; i punti veri le sostituiscono man mano.
    """
    try:
        # Clamp difensivo: niente finestre negative o assurde.
        giorni = max(1, min(giorni, 3650))
        cutoff = business_day_start().date() - timedelta(days=giorni)
        rows = (
            session.query(OverdueSnapshot)
            .filter(OverdueSnapshot.date >= cutoff)
            .order_by(OverdueSnapshot.date.asc())
            .all()
        )
        serie = [
            {
                "data": s.date.isoformat(),
                "scaduto_totale": s.scaduto_totale,
                "non_abbinati": s.non_abbinati,
                "esclusi": s.esclusi,
                "contestati": s.contestati,
                "lavorabile": s.lavorabile,
                "recuperato_certo": s.recuperato_certo,
                # bool() copre i NULL delle righe pre-ALTER sul DB live
                "stimato": bool(s.estimated),
                "fatture": {
                    "scaduto_totale": s.scaduto_totale_fatture,
                    "non_abbinati": s.non_abbinati_fatture,
                    "esclusi": s.esclusi_fatture,
                    "contestati": s.contestati_fatture,
                    "lavorabile": s.lavorabile_fatture,
                    "recuperato_certo": s.recuperato_certo_fatture,
                },
            }
            for s in rows
        ]
        return {"giorni": giorni, "serie": serie}

    except Exception as e:
        logger.error(f"Error fetching evoluzione: {e}", exc_info=True)
        raise


def _recuperato(session: Session) -> dict:
    """Quanto è rientrato DOPO il primo sollecito.

    Due secchielli che non si toccano mai, divisi da un fatto tecnico
    onesto: `paid_at` esiste solo dalla migrazione in poi.

    - certo: ha paid_at (data di pagamento vera) e amount_due_at_paid (il
      residuo fotografato all'atto del pagamento). Numero affidabile.
    - storico_stimato: righe già 'paid' prima della migrazione. Per loro
      NON esiste una data di pagamento (updated_at cambia a ogni modifica
      di riga) NÉ il residuo (amount_due era già stato azzerato). Si stima
      con quel che c'è — updated_at e l'importo pieno — e si DICHIARA
      stimato, invece di spacciarlo per certo.
    """
    # Prima azione NON annullata per cliente (5a), definizione condivisa.
    first_action = first_recovery_action_subquery(session)

    # Certo: paid_at valorizzata, dopo il primo sollecito, somma del RESIDUO.
    # Definizione condivisa con lo snapshot storico (engine/overdue.py): il
    # "recuperato" della serie storica e quello della cascata sono lo stesso.
    certo_fatture, certo_importo = compute_recuperato_certo(session)

    # Stimato: SOLO le righe senza paid_at (mai sovrapposto al certo), emesse
    # prima del sollecito come il certo (5c: stessa clausola condivisa).
    stimato = (
        session.query(
            func.count(Invoice.id),
            func.sum(func.coalesce(Invoice.amount, 0.0)),
        )
        .join(first_action, Invoice.customer_id == first_action.c.customer_id)
        .filter(
            Invoice.status == "paid",
            Invoice.paid_at.is_(None),
            Invoice.updated_at >= first_action.c.first_action,
            recovered_invoice_clause(first_action),
        )
        .one()
    )

    return {
        "certo": {
            "fatture": certo_fatture,
            "importo": round(certo_importo, 2),
            "stimato": False,
            "label": "Presunto incassato",
            "nota": (
                "Dedotto dalla sparizione dalla lista da incassare, non da un "
                "pagamento verificato. Solo fatture emesse prima del primo "
                "sollecito e sparite dopo, a residuo."
            ),
        },
        "storico_stimato": {
            "fatture": int(stimato[0] or 0),
            "importo": round(float(stimato[1] or 0), 2),
            "stimato": True,
            "label": "Storico stimato",
            "nota": (
                "Stima: fatture pagate prima che il sistema registrasse la data "
                "di pagamento. La data è dedotta dall'ultima modifica della riga "
                "e l'importo è quello pieno (il residuo non è più recuperabile). "
                "Non sommare a 'Presunto incassato' come se fosse certo."
            ),
        },
    }


@router.get("/pipeline")
async def get_pipeline(session: Session = Depends(get_session)):
    """
    Get pipeline/funnel data for the Attività page.
    Shows customers at each recovery stage — only those with overdue invoices.
    Resolved = ONLY customers who had recovery actions AND then paid.

    Stage e totale parlano la STESSA lingua (il lavorabile): prima il
    totale non filtrava gli esclusi mentre gli stage sì, e la pipeline si
    contraddiceva da sola — il totale era più grande della somma delle sue
    parti.
    """
    try:
        # Only count customers who actually have overdue invoices (INNER join)
        pipeline_raw = (
            session.query(
                Customer.recovery_status,
                func.count(func.distinct(Customer.id)).label("count"),
                func.sum(Invoice.amount_due).label("total_overdue"),
            )
            .join(Invoice, Invoice.customer_id == Customer.id)
            .filter(workable_clause())
            .group_by(Customer.recovery_status)
            .all()
        )

        stages = {
            "idle": {"label": "Da Gestire", "count": 0, "amount": 0},
            "first_contact": {
                "label": "I Contatto", "count": 0, "amount": 0,
            },
            "second_contact": {
                "label": "II Contatto", "count": 0, "amount": 0,
            },
            "lawyer": {"label": "Avvocato", "count": 0, "amount": 0},
            "waiting": {"label": "In Attesa", "count": 0, "amount": 0},
            "archived": {"label": "Archiviato", "count": 0, "amount": 0},
        }

        for status, count, total in pipeline_raw:
            if status in stages:
                stages[status]["count"] = count or 0
                stages[status]["amount"] = float(total or 0)

        # Resolved: all paid invoices (total incassato)
        resolved_count = (
            session.query(
                func.count(func.distinct(Invoice.customer_id))
            )
            .filter(Invoice.status == "paid")
            .scalar() or 0
        )
        resolved_amount = (
            session.query(func.sum(Invoice.amount))
            .filter(Invoice.status == "paid")
            .scalar() or 0
        )

        # Of which: recovered (invoices paid AFTER first recovery action and
        # issued BEFORE it). Definizione condivisa (engine/overdue.py): niente
        # azioni annullate (5a), niente fatture emesse dopo il sollecito (5c),
        # così /pipeline non diverge da /riconciliazione e dallo snapshot.
        first_action_date = first_recovery_action_subquery(session)

        # Only count invoices paid (updated_at) AFTER the first recovery action
        recovered_query = (
            session.query(Invoice)
            .join(
                first_action_date,
                Invoice.customer_id == first_action_date.c.customer_id,
            )
            .filter(
                Invoice.status == "paid",
                Invoice.updated_at >= first_action_date.c.first_action,
                recovered_invoice_clause(first_action_date),
            )
        )
        recovered_invoices = recovered_query.all()
        recovered_count = len(set(inv.customer_id for inv in recovered_invoices))
        recovered_amount = sum(float(inv.amount or 0) for inv in recovered_invoices)

        stages["resolved"] = {
            "label": "Incassato",
            "count": recovered_count,
            "amount": float(recovered_amount or 0),
            "all_paid_count": resolved_count,
            "all_paid_amount": float(resolved_amount),
            "recovered_amount": float(recovered_amount or 0),
        }

        # Total customers with overdue — STESSA definizione degli stage
        # (lavorabile), altrimenti il totale non chiude sulla somma degli
        # stage che dovrebbe riassumere.
        total_with_overdue = (
            session.query(func.count(func.distinct(Invoice.customer_id)))
            .outerjoin(Customer, Invoice.customer_id == Customer.id)
            .filter(workable_clause())
            .scalar() or 0
        )

        return {
            "stages": stages,
            "total_with_overdue": total_with_overdue,
        }

    except Exception as e:
        logger.error(f"Error fetching pipeline data: {e}", exc_info=True)
        raise


@router.get("/incassato")
async def get_incassato_per_anno(session: Session = Depends(get_session)):
    """
    Get collected amounts grouped by year.
    ONLY counts invoices for customers who had recovery actions
    (first_contact/second_contact/lawyer) AND the invoice was overdue when paid.
    """
    try:
        from sqlalchemy import cast, Date

        overdue_paid_filter = (
            (Invoice.status == "paid")
            & (Invoice.due_date.isnot(None))
            & (Invoice.due_date < cast(Invoice.updated_at, Date))
        )

        # Subquery: customer IDs that had a NON-cancelled recovery action (5a:
        # un cliente col solo sollecito annullato non è un cliente sollecitato).
        recovered_customer_ids = (
            session.query(func.distinct(RecoveryAction.customer_id))
            .filter(
                RecoveryAction.action_type.in_(RECOVERY_ACTION_TYPES),
                RecoveryAction.cancelled.isnot(True),
            )
            .subquery()
        )

        yearly_raw = (
            session.query(
                extract('year', Invoice.updated_at).label("year"),
                func.count(Invoice.id).label("count"),
                func.sum(Invoice.amount).label("total"),
            )
            .filter(
                overdue_paid_filter,
                Invoice.customer_id.in_(recovered_customer_ids),
            )
            .group_by(extract('year', Invoice.updated_at))
            .order_by(extract('year', Invoice.updated_at).asc())
            .all()
        )

        yearly = {}
        grand_total = 0.0
        for row in yearly_raw:
            y = int(row[0]) if row[0] else 0
            amount = float(row[2] or 0)
            yearly[y] = {
                "count": row[1] or 0,
                "total": amount,
            }
            grand_total += amount

        # Ensure years 2022-2026 are always present
        for y in range(2022, 2027):
            if y not in yearly:
                yearly[y] = {"count": 0, "total": 0.0}

        # Recent recovered payments — only from recovery-actioned customers
        recent_paid = (
            session.query(Invoice)
            .filter(
                overdue_paid_filter,
                Invoice.customer_id.in_(recovered_customer_ids),
            )
            .order_by(Invoice.updated_at.desc())
            .limit(20)
            .all()
        )

        # Batch customer names (avoid N+1)
        cust_ids = list({
            inv.customer_id for inv in recent_paid if inv.customer_id
        })
        cust_names = {}
        if cust_ids:
            for cid, name in (
                session.query(Customer.id, Customer.ragione_sociale)
                .filter(Customer.id.in_(cust_ids))
                .all()
            ):
                cust_names[cid] = name

        recent_list = []
        for inv in recent_paid:
            customer_name = (
                cust_names.get(inv.customer_id)
                or inv.customer_name_raw
            )
            recent_list.append({
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "amount": float(inv.amount),
                "customer_name": customer_name,
                "customer_id": inv.customer_id,
                "paid_date": (
                    inv.updated_at.isoformat() if inv.updated_at else None
                ),
                "source_platform": inv.source_platform,
            })

        return {
            "yearly": yearly,
            "grand_total": grand_total,
            "recent_paid": recent_list,
        }

    except Exception as e:
        logger.error(f"Error fetching incassato data: {e}", exc_info=True)
        raise
