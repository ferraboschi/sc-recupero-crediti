"""Seed E2E: dati realistici su SQLite locale per la verifica browser."""

import os
import sys
from datetime import datetime, date, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///data/e2e.db")
os.environ.setdefault("JWT_SECRET", "test-secret-for-local-testing-only")
os.environ.setdefault("AUTH_PASSWORD", "test-password")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.database import (  # noqa: E402
    init_db, get_session_direct, Customer, Invoice, RecoveryAction,
)


def _valid_piva(first10):
    digits = [int(c) for c in first10]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 0:
            total += d
        else:
            doubled = d * 2
            total += doubled if doubled < 10 else doubled - 9
    return first10 + str((10 - (total % 10)) % 10)


def main():
    db_path = "data/e2e.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass

    init_db()
    session = get_session_direct()
    today = date.today()

    # 1. Rooftop SRL — cliente con 2 fatture scadute (una scadenza reale,
    #    una stimata) e telefono per WhatsApp
    rooftop = Customer(
        ragione_sociale="Rooftop S.R.L.",
        partita_iva=_valid_piva("0123456789"),
        phone="+393331112233",
        email="amministrazione@rooftop.example",
        source="shopify",
        shopify_id="gid://shopify/Customer/1001",
    )
    session.add(rooftop)
    session.flush()
    session.add(Invoice(
        invoice_number="FT-2026-101",
        amount=1830.50, amount_due=1830.50,
        issue_date=today - timedelta(days=55),
        due_date=today - timedelta(days=25),
        due_date_source="real",
        days_overdue=25, status="open",
        customer_id=rooftop.id, source_platform="fatturapro",
        match_method="piva", match_score=100,
        customer_name_raw="ROOFTOP SRL",
        customer_piva_raw=rooftop.partita_iva,
    ))
    session.add(Invoice(
        invoice_number="FT-2026-142",
        amount=640.00, amount_due=640.00,
        issue_date=today - timedelta(days=42),
        due_date=today - timedelta(days=12),
        due_date_source="assumed",
        days_overdue=12, status="open",
        customer_id=rooftop.id, source_platform="fatturapro",
        match_method="legacy",
        customer_name_raw="ROOFTOP SRL",
    ))

    # 2. Izakaya8 SRL — stato stantio: 'second_contact' ma tutto pagato
    #    (il backfill allo startup deve resettarlo a idle)
    izakaya = Customer(
        ragione_sociale="Izakaya8 S.R.L.",
        partita_iva=_valid_piva("0463155037"),
        phone="+393334445566",
        recovery_status="second_contact",
        next_action_date=today + timedelta(days=3),
        next_action_type="lawyer",
        source="shopify",
        shopify_id="gid://shopify/Customer/1002",
    )
    session.add(izakaya)
    session.flush()
    session.add(Invoice(
        invoice_number="FT-2026-088",
        amount=920.00, amount_due=0.0,
        issue_date=today - timedelta(days=90),
        due_date=today - timedelta(days=60),
        due_date_source="real",
        days_overdue=0, status="paid",
        customer_id=izakaya.id, source_platform="fatturapro",
        match_method="legacy",
        customer_name_raw="IZAKAYA8 SRL",
    ))
    stale_todo = RecoveryAction(
        customer_id=izakaya.id, action_type="second_contact",
        scheduled_date=today + timedelta(days=3),
    )
    stale_todo.created_at = datetime.utcnow() - timedelta(days=20)
    session.add(stale_todo)

    # 3. QOQA SA — cliente esistente; una fattura NON abbinata con
    #    suggerimento fuzzy in quarantena (coda "Da confermare")
    qoqa = Customer(
        ragione_sociale="QOQA SA",
        partita_iva="CHE123456789",
        source="shopify",
        shopify_id="gid://shopify/Customer/1003",
    )
    session.add(qoqa)
    session.flush()
    session.add(Invoice(
        invoice_number="FT-2026-155",
        amount=451.00, amount_due=451.00,
        issue_date=today - timedelta(days=38),
        due_date=today - timedelta(days=8),
        due_date_source="assumed",
        days_overdue=8, status="open",
        customer_id=None, source_platform="fatturapro",
        customer_name_raw="QOQA S.A. LOSANNA",
        suggested_customer_id=qoqa.id,
        suggested_method="fuzzy",
        suggested_score=82,
    ))

    # 4. Battiato Loris — fattura ABBINATA AL CLIENTE SBAGLIATO (nomi
    #    dissimili): il match-audit in Sistema deve segnalarla 'bad'
    session.add(Invoice(
        invoice_number="FT-2026-160",
        amount=310.00, amount_due=310.00,
        issue_date=today - timedelta(days=45),
        due_date=today - timedelta(days=15),
        due_date_source="assumed",
        days_overdue=15, status="open",
        customer_id=rooftop.id, source_platform="fatturapro",
        match_method="legacy",
        customer_name_raw="BATTIATO LORIS",
    ))

    # 5. Cavo Luigi Beverage Solutions srl — BONIFICABILE IN BLOCCO: cliente
    #    SENZA P.IVA sul profilo, ma 2 fatture con la STESSA P.IVA valida e
    #    nome identico → compare nella lista di revisione (certezza 100%).
    cavo = Customer(
        ragione_sociale="Cavo Luigi Beverage Solutions srl",
        partita_iva=None,
        phone="+393337778899",
        source="fatturapro",
    )
    session.add(cavo)
    session.flush()
    cavo_piva = _valid_piva("0257244099")
    for num, days in (("FT-2026-201", 20), ("FT-2026-202", 6)):
        session.add(Invoice(
            invoice_number=num,
            amount=1200.00, amount_due=1200.00,
            issue_date=today - timedelta(days=days + 30),
            due_date=today - timedelta(days=days),
            due_date_source="real",
            days_overdue=days, status="open",
            customer_id=cavo.id, source_platform="fatturapro",
            match_method="name_exact",
            customer_name_raw="Cavo Luigi Beverage Solutions srl",
            customer_piva_raw=cavo_piva,
        ))

    session.commit()
    session.close()
    print("Seed E2E completato: data/e2e.db")


if __name__ == "__main__":
    main()
