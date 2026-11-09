"""Configuration module - reads from .env file."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)


class Config:
    """Application configuration."""

    # Fattura24 (legacy)
    FATTURA24_API_KEY = os.getenv("FATTURA24_API_KEY", "")
    FATTURA24_API_URL = os.getenv("FATTURA24_API_URL", "https://www.app.fattura24.com/api/v0.3")

    # FatturaPro (current)
    FATTURAPRO_API_URL = os.getenv("FATTURAPRO_API_URL", "https://cloud.fatturapro.click")
    FATTURAPRO_API_KEY = os.getenv("FATTURAPRO_API_KEY", "")
    FATTURAPRO_DOMAIN = os.getenv("FATTURAPRO_DOMAIN", "sakecompany.com")
    FATTURAPRO_USERNAME = os.getenv("FATTURAPRO_USERNAME", "")
    FATTURAPRO_PASSWORD = os.getenv("FATTURAPRO_PASSWORD", "")

    # Shopify
    SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL", "")
    SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
    SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
    SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
    SHOPIFY_PIVA_FIELD = os.getenv("SHOPIFY_PIVA_FIELD", "address2")

    # Database
    # Use DATABASE_URL for PostgreSQL (Supabase) or fall back to SQLite
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{os.getenv('DATABASE_PATH', 'data/sc_recupero.db')}"
    )

    # App
    TIMEZONE = os.getenv("TIMEZONE", "Europe/Rome")
    SCHEDULER_HOUR = int(os.getenv("SCHEDULER_HOUR", "8"))
    SCHEDULER_MINUTE = int(os.getenv("SCHEDULER_MINUTE", "30"))

    # Dati aziendali stampati sui PDF (copia di cortesia, riepilogo, dossier).
    # P.IVA e indirizzo li fornisce l'owner: finché sono vuoti la riga non
    # viene stampata (meglio niente che un valore sbagliato su un documento
    # che va ai clienti). L'IBAN conserva il valore storico del progetto.
    COMPANY_NAME = os.getenv("COMPANY_NAME", "Sake Company srl")
    # COMPANY_PIVA è la variabile già presente su Render (nome storico): vale
    # come P.IVA se COMPANY_VAT non è impostata.
    COMPANY_VAT = os.getenv("COMPANY_VAT", "") or os.getenv("COMPANY_PIVA", "")
    COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")
    COMPANY_IBAN = os.getenv("COMPANY_IBAN", "IT44N0200801671000105175151")
    COMPANY_BANK = os.getenv("COMPANY_BANK", "UniCredit")

    # Promemoria email il giorno prima della scadenza (backend/engine/reminder_email.py).
    # Senza REMINDER_EMAIL_ENABLED + RESEND_API_KEY la funzione resta in PROVA:
    # compone e registra, non invia. Mittente deciso dall'owner: fatture@.
    REMINDER_EMAIL_ENABLED = os.getenv("REMINDER_EMAIL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
    RESEND_API_URL = os.getenv("RESEND_API_URL", "https://api.resend.com")
    REMINDER_EMAIL_FROM = os.getenv("REMINDER_EMAIL_FROM", "Sake Company <fatture@sakecompany.com>")
    REMINDER_EMAIL_REPLY_TO = os.getenv("REMINDER_EMAIL_REPLY_TO", "fatture@sakecompany.com")
    REMINDER_EMAIL_BCC = os.getenv("REMINDER_EMAIL_BCC", "")
    REMINDER_EMAIL_TEST_TO = os.getenv("REMINDER_EMAIL_TEST_TO", "")
    REMINDER_EMAIL_HOUR = int(os.getenv("REMINDER_EMAIL_HOUR", "9"))
    REMINDER_EMAIL_MINUTE = int(os.getenv("REMINDER_EMAIL_MINUTE", "15"))  # :15, fuori dal sync orario delle :00

    # CORS - frontend origin (GitHub Pages)
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "https://recupero.sakecompany.com,http://localhost:5173").split(",")

    # Business rules
    # Soglia minima perché un fuzzy match generi un SUGGERIMENTO (mai un
    # abbinamento automatico). 75 copre i nomi corti reali tipo "F-T SRL".
    FUZZY_MATCH_THRESHOLD = 75

    @classmethod
    def shopify_api_base(cls) -> str:
        """Return the Shopify Admin API base URL."""
        return f"{cls.SHOPIFY_STORE_URL}/admin/api/{cls.SHOPIFY_API_VERSION}"

    @classmethod
    def validate(cls) -> dict:
        """Check which credentials are configured."""
        return {
            "fatturapro": bool(cls.FATTURAPRO_API_KEY),
            "fattura24": bool(cls.FATTURA24_API_KEY),
            "shopify": bool(
                cls.SHOPIFY_ACCESS_TOKEN
                or (cls.SHOPIFY_CLIENT_ID and cls.SHOPIFY_CLIENT_SECRET)
            ),
        }


config = Config()
