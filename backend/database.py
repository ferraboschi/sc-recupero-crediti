"""Database setup and models using SQLAlchemy (PostgreSQL/SQLite)."""

import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, Date, Text, ForeignKey, JSON, event
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from backend.config import config

Base = declarative_base()


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    shopify_id = Column(String, unique=True, nullable=True)
    ragione_sociale = Column(String, nullable=False)
    ragione_sociale_normalized = Column(String, nullable=True, index=True)
    partita_iva = Column(String, nullable=True, index=True)
    codice_fiscale = Column(String, nullable=True)
    codice_sdi = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    phone_validated = Column(Boolean, default=False)
    email = Column(String, nullable=True)
    excluded = Column(Boolean, default=False)
    source = Column(String, default="shopify")  # shopify / fatturapro / fatture24
    tags = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Relationships
    invoices = relationship("Invoice", back_populates="customer")
    messages = relationship("Message", back_populates="customer")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_number = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    amount_due = Column(Float, nullable=False)
    issue_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    days_overdue = Column(Integer, default=0)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    source_platform = Column(String, nullable=False)  # fatturapro / fatture24
    source_id = Column(String, nullable=True)
    status = Column(String, default="open")  # open / contacted / promised / paid / disputed / escalated
    customer_name_raw = Column(String, nullable=True)  # Original name from invoice
    customer_piva_raw = Column(String, nullable=True)  # Original P.IVA from invoice
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Relationships
    customer = relationship("Customer", back_populates="invoices")
    messages = relationship("Message", back_populates="invoice")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    escalation_level = Column(Integer, default=1)  # 1-4
    template = Column(String, nullable=True)
    body = Column(Text, nullable=True)
    status = Column(String, default="draft")  # draft / approved / sent / delivered / read / replied
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    twilio_sid = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Relationships
    invoice = relationship("Invoice", back_populates="messages")
    customer = relationship("Customer", back_populates="messages")
    conversations = relationship("Conversation", back_populates="message")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False)
    direction = Column(String, nullable=False)  # outbound / inbound
    body = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    intent = Column(String, nullable=True)  # payment_confirm / extension / dispute / info_request / wrong_number / opt_out / unknown

    # Relationships
    message = relationship("Message", back_populates="conversations")


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    action = Column(String, nullable=False)  # sync / match / message_sent / reply_received / phone_updated / status_change
    details = Column(JSON, nullable=True)
    entity_type = Column(String, nullable=True)  # invoice / customer / message
    entity_id = Column(Integer, nullable=True)


# Database engine and session
_engine = None


def get_engine():
    """Create database engine (PostgreSQL or SQLite based on DATABASE_URL)."""
    global _engine
    if _engine is not None:
        return _engine

    db_url = config.DATABASE_URL

    if db_url.startswith("sqlite"):
        _engine = create_engine(db_url, echo=False)
    else:
        # PostgreSQL (Supabase) — use minimal connection pooling
        # Supabase Session Pooler has limited max_clients
        _engine = create_engine(
            db_url,
            echo=False,
            pool_size=2,
            max_overflow=3,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_timeout=30,
        )

    return _engine


def init_db():
    """Initialize database tables."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session():
    """Create a new database session."""
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()


# Enable WAL mode for SQLite only
@event.listens_for(get_engine(), "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    """Set SQLite pragmas (skipped for PostgreSQL)."""
    db_url = config.DATABASE_URL
    if not db_url.startswith("sqlite"):
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
