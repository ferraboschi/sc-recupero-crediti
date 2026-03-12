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

    # Twilio / WhatsApp
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_WHATSAPP_NUMBER_BUSINESS = os.getenv("TWILIO_WHATSAPP_NUMBER_BUSINESS", "")
    TWILIO_WHATSAPP_NUMBER_RECOVERY = os.getenv("TWILIO_WHATSAPP_NUMBER_RECOVERY", "")
    TWILIO_WEBHOOK_URL = os.getenv("TWILIO_WEBHOOK_URL", "")

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

    # CORS - frontend origin (GitHub Pages)
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "https://recupero.sakecompany.com,http://localhost:5173").split(",")

    # Business rules
    FUZZY_MATCH_THRESHOLD = 75  # Minimum score for fuzzy matching (lowered from 85 for short names like F-T SRL)
    BUSINESS_HOURS_START = 9
    BUSINESS_HOURS_END = 18
    ESCALATION_DAYS = [7, 14, 21, 30]  # Days between escalation levels

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
            "twilio": bool(cls.TWILIO_ACCOUNT_SID and cls.TWILIO_AUTH_TOKEN),
        }


config = Config()
