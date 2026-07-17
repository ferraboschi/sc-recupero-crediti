"""Database setup and models using SQLAlchemy (PostgreSQL/SQLite).

Nota storica: le tabelle `messages` e `conversations` (pipeline di invio
automatico Twilio, mai attivata in produzione) non sono più mappate dal
codice ma restano nel database di produzione come archivio. Possono essere
eliminate manualmente quando si è certi di non doverle più consultare.
"""

import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, Date, Text, ForeignKey, JSON, Index, event, text
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
    phones_json = Column(JSON, nullable=True)  # [{"number": "+39...", "source": "shopify_billing", "label": "Fatturazione"}]
    email = Column(String, nullable=True)
    excluded = Column(Boolean, default=False)
    source = Column(String, default="shopify")  # shopify / fatturapro / fatture24 / manual
    tags = Column(String, nullable=True)
    # Recovery workflow — cache dello stato della pratica aperta, per liste/filtri.
    # La fonte di verità del ciclo di recupero è RecoveryCase.
    recovery_status = Column(String, default="idle")  # idle / first_contact / second_contact / lawyer / archived / waiting
    next_action_date = Column(Date, nullable=True)
    next_action_type = Column(String, nullable=True)  # first_contact / second_contact / lawyer / archive / wait
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Relationships
    invoices = relationship("Invoice", back_populates="customer", foreign_keys="Invoice.customer_id")
    recovery_actions = relationship("RecoveryAction", back_populates="customer", order_by="RecoveryAction.created_at.desc()")
    recovery_cases = relationship("RecoveryCase", back_populates="customer", order_by="RecoveryCase.opened_at.desc()")


class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_number = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    amount_due = Column(Float, nullable=False)
    issue_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    # Provenienza della scadenza: 'real' (dal gestionale/CSV), 'assumed'
    # (sintetizzata emissione+30gg), 'manual'. NULL è trattato come 'assumed'.
    due_date_source = Column(String, nullable=True)
    days_overdue = Column(Integer, default=0)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    case_id = Column(Integer, ForeignKey("recovery_cases.id"), nullable=True)
    source_platform = Column(String, nullable=False)  # fatturapro / fatture24
    source_id = Column(String, nullable=True)
    shopify_order_id = Column(String, nullable=True)
    shopify_order_number = Column(String, nullable=True)  # e.g. "#SAK1234"
    status = Column(String, default="open")  # open / contacted / promised / paid / disputed / escalated
    customer_name_raw = Column(String, nullable=True)  # Original name from invoice
    customer_piva_raw = Column(String, nullable=True)  # Original P.IVA from invoice
    # Provenienza dell'abbinamento cliente:
    # piva / name_exact / fuzzy_confirmed / manual / auto_created / order /
    # legacy (abbinata prima dell'introduzione della provenance) /
    # unlinked (scollegata a mano: mai più auto-abbinata, solo suggerimenti)
    match_method = Column(String, nullable=True)
    match_score = Column(Integer, nullable=True)
    # Suggerimento in quarantena (fuzzy/P.IVA ambigua): richiede conferma manuale.
    suggested_customer_id = Column(Integer, nullable=True)
    suggested_score = Column(Integer, nullable=True)
    # fuzzy / piva_ambiguous / piva_name_mismatch / name_ambiguous /
    # name_exact_piva_unverified / legal_form_conflict (stessa insegna ma
    # ditta individuale vs società: entità giuridiche diverse);
    # per fatture 'unlinked' anche piva / name_exact (il match sarebbe stato
    # automatico, ma lo scollegamento manuale lo declassa a suggerimento)
    suggested_method = Column(String, nullable=True)
    # Payment detection per assenza: numero di fetch COMPLETI consecutivi
    # in cui la fattura è mancata dalla lista "Da incassare". Si marca paid
    # solo oltre soglia (vedi sync.PAID_ABSENCE_STREAK); azzerato quando la
    # fattura ricompare.
    missing_streak = Column(Integer, default=0)
    # Ultimo tentativo di enrichment dal dettaglio FatturaPro: permette la
    # rotazione del cap (prima le mai tentate, poi le più vecchie).
    detail_attempted_at = Column(DateTime, nullable=True)
    # Audit abbinamenti: quando l'operatore ha verificato a mano un
    # abbinamento dubbio/critico e lo considera ok. Valorizzato = esce dai
    # problemi dell'audit (a meno di include_reviewed).
    audit_reviewed_at = Column(DateTime, nullable=True)
    # Data di pagamento VERA: scritta nel momento in cui il sync marca la
    # fattura 'paid', azzerata se la fattura riapre. Da non confondere con
    # updated_at (onupdate: cambia a ogni modifica di riga, non è una data
    # di pagamento). NULL sulle righe già pagate prima della migrazione:
    # per quelle una data di pagamento vera non esiste e non va inventata —
    # il KPI le tiene separate come "storico stimato".
    paid_at = Column(DateTime, nullable=True)
    # Residuo fotografato all'atto del pagamento. Serve perché i punti che
    # marcano 'paid' azzerano amount_due: senza questo scatto, sommare il
    # residuo delle pagate darebbe sempre 0. È il valore da sommare per il
    # "recuperato" (l'importo PIENO sovrastima i pagamenti parziali).
    amount_due_at_paid = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Relationships
    customer = relationship("Customer", back_populates="invoices", foreign_keys=[customer_id])
    case = relationship("RecoveryCase", back_populates="invoices")


class RecoveryCase(Base):
    """Pratica di recupero: un ciclo di debito di un cliente.

    Si apre quando il cliente ha fatture scadute non pagate, si chiude a
    saldo (o per archiviazione/esclusione). Numerazione e tono dei solleciti
    contano le azioni della pratica, non tutta la storia del cliente.
    """
    __tablename__ = "recovery_cases"
    __table_args__ = (
        # Una sola pratica aperta per cliente — l'invariante su cui poggiano
        # numerazione, dedup e chiusure. Definito sul modello così vale
        # anche nei DB creati da create_all (test inclusi), oltre che nella
        # migrazione raw per il DB live.
        Index(
            "uq_open_case_per_customer", "customer_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    status = Column(String, default="open", nullable=False)  # open / closed
    opened_at = Column(DateTime, default=datetime.datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    # paid: tutte le fatture del ciclo saldate.
    # no_overdue: svuotata senza saldo (es. scadenze corrette nel futuro) — riapribile.
    # resolved: rimaste solo fatture contestate.
    # archived: archiviata dall'operatore. excluded: cliente escluso.
    closed_reason = Column(String, nullable=True)
    # Contatti ereditati dalla pratica precedente chiusa per archiviazione /
    # passata all'avvocato: il tono non riparte mai dal sollecito cordiale.
    inherited_contacts = Column(Integer, default=0)
    reopened_after_archive = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Relationships
    customer = relationship("Customer", back_populates="recovery_cases")
    invoices = relationship("Invoice", back_populates="case")
    actions = relationship("RecoveryAction", back_populates="case")


class RecoveryAction(Base):
    """Tracks recovery workflow actions per customer."""
    __tablename__ = "recovery_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    case_id = Column(Integer, ForeignKey("recovery_cases.id"), nullable=True)
    action_type = Column(String, nullable=False)  # first_contact / second_contact / lawyer / archive / wait / note
    scheduled_date = Column(Date, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    outcome = Column(String, nullable=True)  # contacted / promised / partial_payment / paid / unreachable / disputed / no_answer
    notes = Column(Text, nullable=True)
    # Canale del sollecito registrato automaticamente: whatsapp_copy / whatsapp_link / phone / email
    channel = Column(String, nullable=True)
    # Fatture citate nel sollecito (lista di invoice id)
    invoice_ids = Column(JSON, nullable=True)
    # Azione annullata (non conta, nascosta da todos/calendario).
    # cancelled_reason: case_closed / superseded_by_sollecito / customer_excluded / undo
    cancelled = Column(Boolean, default=False)
    cancelled_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    customer = relationship("Customer", back_populates="recovery_actions")
    case = relationship("RecoveryCase", back_populates="actions")


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    action = Column(String, nullable=False)  # sync / match / status_change / case_opened / case_closed / sollecito ...
    details = Column(JSON, nullable=True)
    entity_type = Column(String, nullable=True)  # invoice / customer / case / recovery_action
    entity_id = Column(Integer, nullable=True)


class SyncState(Base):
    """Persists sync status across server restarts."""
    __tablename__ = "sync_state"

    key = Column(String, primary_key=True)  # invoices / customers / matching / cases / case_backfill
    last_sync = Column(DateTime, nullable=True)
    result = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class OverdueSnapshot(Base):
    """Fotografia giornaliera dello scaduto: la storia che la dashboard non ha.

    La dashboard fotografa solo l'ISTANTE presente; qui si persiste la cascata
    giorno per giorno, così l'evoluzione dello scaduto (totale, lavorabile,
    recuperato) diventa una serie storica per il grafico.

    Un solo snapshot per giorno (`date` UNIQUE): il sync fa UPSERT sulla riga
    del giorno lavorativo corrente — due sync nello stesso giorno la
    aggiornano, non la duplicano. La "data di oggi" è quella del giorno
    lavorativo italiano (business_day_start), non date.today() UTC.

    Gli importi sono la cascata di /riconciliazione (definizione condivisa in
    engine/overdue.py): la serie storica non può divergere dal numero live.
    `recuperato_certo` è CUMULATO — tutto ciò che è rientrato dopo il primo
    sollecito a quella data.

    Tabella NUOVA: la crea create_all (nessuna migrazione ALTER necessaria —
    servono solo per colonne su tabelle esistenti). RLS abilitata in
    _enable_rls come per ogni altra tabella (requisito Supabase).
    """
    __tablename__ = "overdue_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Un solo snapshot per giorno. UNIQUE: l'UPSERT del sync aggiorna questa
    # riga; l'indice impedisce anche una duplicazione da race a livello DB.
    date = Column(Date, nullable=False, unique=True, index=True)

    # Importi (euro) della cascata dello scaduto
    scaduto_totale = Column(Float, nullable=False, default=0.0)
    non_abbinati = Column(Float, nullable=False, default=0.0)
    esclusi = Column(Float, nullable=False, default=0.0)
    contestati = Column(Float, nullable=False, default=0.0)
    lavorabile = Column(Float, nullable=False, default=0.0)
    # Recuperato certo, CUMULATO (pagato dopo il primo sollecito, a residuo)
    recuperato_certo = Column(Float, nullable=False, default=0.0)

    # Conteggi fatture per bucket (stessa cascata)
    scaduto_totale_fatture = Column(Integer, nullable=False, default=0)
    non_abbinati_fatture = Column(Integer, nullable=False, default=0)
    esclusi_fatture = Column(Integer, nullable=False, default=0)
    contestati_fatture = Column(Integer, nullable=False, default=0)
    lavorabile_fatture = Column(Integer, nullable=False, default=0)
    recuperato_certo_fatture = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


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
        # PostgreSQL (Supabase Session Pooler) — use small QueuePool
        # to keep connections alive and avoid SSL handshake per request
        _engine = create_engine(
            db_url,
            echo=False,
            pool_size=3,
            max_overflow=2,
            pool_timeout=10,
            pool_recycle=300,
            pool_pre_ping=True,
        )

    # Register connection listeners
    event.listen(_engine, "connect", _set_sqlite_pragma)
    event.listen(_engine, "connect", _set_pg_timeouts)

    return _engine


def init_db():
    """Initialize database tables and run lightweight migrations."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _run_migrations(engine)
    return engine


def _run_migrations(engine):
    """Add missing columns/indexes to existing tables (lightweight migration).

    Uses a single raw DBAPI connection to minimise round-trips to Supabase.
    Each statement is wrapped in a try/except so that 'already exists'
    errors are silently ignored (idempotent).
    """
    import logging
    _logger = logging.getLogger(__name__)
    _alters = [
        "ALTER TABLE recovery_actions ADD COLUMN outcome VARCHAR",
        "ALTER TABLE customers ADD COLUMN phones_json JSONB",
        "ALTER TABLE invoices ADD COLUMN shopify_order_id VARCHAR",
        "ALTER TABLE invoices ADD COLUMN shopify_order_number VARCHAR",
        # Pratiche di recupero + provenance abbinamenti + provenance scadenze
        "ALTER TABLE invoices ADD COLUMN case_id INTEGER",
        "ALTER TABLE invoices ADD COLUMN due_date_source VARCHAR",
        "ALTER TABLE invoices ADD COLUMN match_method VARCHAR",
        "ALTER TABLE invoices ADD COLUMN match_score INTEGER",
        "ALTER TABLE invoices ADD COLUMN suggested_customer_id INTEGER",
        "ALTER TABLE invoices ADD COLUMN suggested_score INTEGER",
        "ALTER TABLE invoices ADD COLUMN suggested_method VARCHAR",
        "ALTER TABLE recovery_actions ADD COLUMN case_id INTEGER",
        "ALTER TABLE recovery_actions ADD COLUMN channel VARCHAR",
        "ALTER TABLE recovery_actions ADD COLUMN invoice_ids JSONB",
        "ALTER TABLE recovery_actions ADD COLUMN cancelled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE recovery_actions ADD COLUMN cancelled_reason VARCHAR",
        # Una sola pratica aperta per cliente (vale anche su SQLite)
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_case_per_customer "
        "ON recovery_cases (customer_id) WHERE status = 'open'",
        # Normalizza i NULL del BOOLEAN aggiunto via ALTER
        "UPDATE recovery_actions SET cancelled = FALSE WHERE cancelled IS NULL",
        # Backfill provenance (idempotenti: toccano solo righe non classificate).
        # 'legacy' = abbinata prima dell'introduzione della provenance.
        "UPDATE invoices SET match_method = 'legacy' "
        "WHERE customer_id IS NOT NULL AND match_method IS NULL",
        # Scadenze: emissione+30 esatti = quasi certamente sintetizzata dal
        # ricalcolo storico (qualsiasi piattaforma) → 'assumed'; le altre con
        # una scadenza sono vere ('real').
        "UPDATE invoices SET due_date_source = 'assumed' "
        "WHERE due_date_source IS NULL AND due_date IS NOT NULL "
        "AND issue_date IS NOT NULL AND due_date = issue_date + 30",
        "UPDATE invoices SET due_date_source = 'real' "
        "WHERE due_date_source IS NULL AND due_date IS NOT NULL",
        # Payment detection a doppia assenza + rotazione enrichment dettaglio
        "ALTER TABLE invoices ADD COLUMN missing_streak INTEGER DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN detail_attempted_at TIMESTAMP",
        "UPDATE invoices SET missing_streak = 0 WHERE missing_streak IS NULL",
        # Audit abbinamenti: "Segna verificato" per le fatture già controllate
        "ALTER TABLE invoices ADD COLUMN audit_reviewed_at TIMESTAMP",
        # Riconciliazione: data di pagamento vera + residuo all'atto del
        # pagamento. NESSUN backfill: le righe già 'paid' non hanno una data
        # di pagamento vera (updated_at non lo è) né un residuo recuperabile
        # (amount_due è già stato azzerato). Restano NULL e il KPI le
        # dichiara "storico stimato" invece di spacciarle per certe.
        "ALTER TABLE invoices ADD COLUMN paid_at TIMESTAMP",
        "ALTER TABLE invoices ADD COLUMN amount_due_at_paid DOUBLE PRECISION",
    ]
    try:
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            for stmt in _alters:
                try:
                    stmt_exec = stmt
                    if config.DATABASE_URL.startswith("sqlite"):
                        # SQLite non ha JSONB né l'aritmetica date di Postgres
                        stmt_exec = stmt.replace("JSONB", "JSON").replace(
                            "due_date = issue_date + 30",
                            "due_date = date(issue_date, '+30 days')",
                        )
                    cur.execute(stmt_exec)
                    raw.commit()
                except Exception:
                    raw.rollback()  # column/index already exists
            cur.close()
        finally:
            raw.close()
        _logger.info("Migrations checked (lightweight)")
    except Exception as e:
        _logger.warning(f"Migration warning (non-fatal): {e}")

    # Enable Row Level Security on all tables (Supabase requirement)
    _enable_rls(engine)


def _enable_rls(engine):
    """Enable Row Level Security on all public tables for Supabase.

    - Enables RLS on each table (idempotent — no-op if already enabled)
    - Creates a permissive policy for the 'postgres' role (our backend connection)
    - This blocks anon/authenticated Supabase client access via PostgREST
      while allowing our SQLAlchemy backend full access.

    Skipped entirely for SQLite (local dev).
    """
    import logging
    _logger = logging.getLogger(__name__)

    if config.DATABASE_URL.startswith("sqlite"):
        return

    tables = [
        "customers", "invoices", "recovery_cases",
        "recovery_actions", "activity_log", "sync_state",
        "overdue_snapshots",
        # legacy, non più mappate dal codice ma presenti nel DB
        "messages", "conversations",
    ]

    try:
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            for table in tables:
                try:
                    # Enable RLS
                    cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
                    raw.commit()
                except Exception:
                    raw.rollback()

                try:
                    # Create policy allowing full access for postgres role (our backend)
                    # DROP + CREATE for idempotency
                    cur.execute(
                        f"DROP POLICY IF EXISTS backend_full_access ON {table}"
                    )
                    raw.commit()
                except Exception:
                    raw.rollback()

                try:
                    cur.execute(
                        f"CREATE POLICY backend_full_access ON {table} "
                        f"FOR ALL TO postgres USING (true) WITH CHECK (true)"
                    )
                    raw.commit()
                except Exception:
                    raw.rollback()  # policy already exists

            cur.close()
            _logger.info(f"RLS enabled on {len(tables)} tables with backend_full_access policy")
        finally:
            raw.close()
    except Exception as e:
        _logger.warning(f"RLS setup warning (non-fatal): {e}")


def get_session():
    """Create a new database session as a FastAPI dependency with auto-close."""
    engine = get_engine()
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_session_direct():
    """Create a new database session for non-FastAPI use (sync code).
    Caller MUST close the session manually."""
    engine = get_engine()
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


# SQLite WAL mode listener — registered lazily inside get_engine()
# to avoid creating the engine at import time.
def _set_sqlite_pragma(dbapi_conn, connection_record):
    """Set SQLite pragmas (skipped for PostgreSQL)."""
    db_url = config.DATABASE_URL
    if not db_url.startswith("sqlite"):
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _set_pg_timeouts(dbapi_conn, connection_record):
    """Set PostgreSQL session timeouts to prevent stale connections."""
    db_url = config.DATABASE_URL
    if db_url.startswith("sqlite"):
        return
    try:
        cursor = dbapi_conn.cursor()
        # Kill idle-in-transaction sessions after 5 minutes
        cursor.execute(
            "SET idle_in_transaction_session_timeout = '300000'"
        )
        # Kill any statement running longer than 10 minutes
        cursor.execute(
            "SET statement_timeout = '600000'"
        )
        cursor.close()
    except Exception:
        pass  # Non-fatal
