"""Pytest configuration and shared fixtures for SC Recupero Crediti tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
import tempfile
from pathlib import Path

from backend.database import Base, Invoice, Customer, Message, Conversation, ActivityLog
from backend.main import app
from fastapi.testclient import TestClient


@pytest.fixture(scope="function")
def test_db_engine():
    """Create an in-memory SQLite database for testing."""
    # Use in-memory SQLite with StaticPool for test isolation
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture(scope="function")
def test_db_session(test_db_engine):
    """Create a database session for testing."""
    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=test_db_engine,
    )
    session = TestingSessionLocal()
    yield session
    session.close()


@pytest.fixture(scope="function")
def test_client(test_db_session, monkeypatch):
    """Create a TestClient for FastAPI."""
    # Override the get_session dependency with our test session
    def override_get_session():
        return test_db_session

    from backend.database import get_session
    app.dependency_overrides[get_session] = override_get_session

    client = TestClient(app)
    yield client

    # Clear dependency overrides after test
    app.dependency_overrides.clear()


@pytest.fixture
def sample_customer(test_db_session):
    """Create a sample customer for testing."""
    customer = Customer(
        shopify_id="gid://shopify/Customer/12345",
        ragione_sociale="ACME S.R.L.",
        ragione_sociale_normalized="acme",
        partita_iva="12345678901",
        codice_fiscale="ABCDEF12G34H567I",
        phone="+39 333 123 4567",
        phone_validated=True,
        email="customer@example.com",
        excluded=False,
        source="shopify",
    )
    test_db_session.add(customer)
    test_db_session.commit()
    return customer


@pytest.fixture
def sample_invoice(test_db_session, sample_customer):
    """Create a sample invoice for testing."""
    from datetime import date, datetime
    invoice = Invoice(
        invoice_number="INV001",
        amount=1000.50,
        amount_due=1000.50,
        issue_date=date(2024, 1, 15),
        due_date=date(2024, 2, 15),
        days_overdue=25,
        customer_id=sample_customer.id,
        source_platform="fatturapro",
        source_id="fp_12345",
        status="open",
        customer_name_raw="ACME S.R.L.",
        customer_piva_raw="12345678901",
    )
    test_db_session.add(invoice)
    test_db_session.commit()
    return invoice


@pytest.fixture
def sample_message(test_db_session, sample_invoice, sample_customer):
    """Create a sample message for testing."""
    message = Message(
        invoice_id=sample_invoice.id,
        customer_id=sample_customer.id,
        escalation_level=1,
        template="reminder_1",
        body="Please pay your invoice",
        status="draft",
    )
    test_db_session.add(message)
    test_db_session.commit()
    return message


@pytest.fixture
def multiple_customers(test_db_session):
    """Create multiple customers for testing matching scenarios."""
    customers_data = [
        {
            "ragione_sociale": "ACME S.R.L.",
            "partita_iva": "12345678901",
            "phone": "+39 333 123 4567",
            "email": "acme@example.com",
        },
        {
            "ragione_sociale": "Mario Rossi S.A.S.",
            "partita_iva": "98765432100",
            "phone": "+39 334 567 8901",
            "email": "rossi@example.com",
        },
        {
            "ragione_sociale": "Ditta Bianchi",
            "partita_iva": "11111111111",
            "phone": "+39 335 901 2345",
            "email": "bianchi@example.com",
        },
        {
            "ragione_sociale": "Société Generali S.p.a.",
            "partita_iva": "22222222222",
            "phone": "+39 336 234 5678",
            "email": "sg@example.com",
        },
    ]

    customers = []
    for data in customers_data:
        customer = Customer(
            shopify_id=f"gid://shopify/Customer/{len(customers)}",
            ragione_sociale=data["ragione_sociale"],
            partita_iva=data["partita_iva"],
            phone=data["phone"],
            email=data["email"],
            excluded=False,
            source="shopify",
        )
        test_db_session.add(customer)
        customers.append(customer)

    test_db_session.commit()
    return customers


@pytest.fixture
def activity_log(test_db_session):
    """Create an activity log entry for testing."""
    from datetime import datetime
    log = ActivityLog(
        timestamp=datetime.utcnow(),
        action="sync",
        details={"source": "fatturapro", "count": 5},
        entity_type="invoice",
        entity_id=1,
    )
    test_db_session.add(log)
    test_db_session.commit()
    return log
