"""Formati condivisi per testi ed email ai clienti e per i PDF (fpdf2).

Un solo posto per: importi in italiano ("1.234,56 €"), date (gg/mm/aaaa),
giorno italiano, testo compatibile con i font core di fpdf (latin-1) e nomi
di file sicuri. Modulo puro, testato in tests/test_reminder_email.py.
"""
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from backend.config import config


def fmt_eur(value) -> str:
    """1234.5 → '1.234,50 €' (separatori italiani)."""
    amount = float(value or 0)
    text = f"{amount:,.2f}"  # 1,234.50
    text = text.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
    return f"{text} €"


def fmt_eur_pdf(value) -> str:
    """Come fmt_eur ma con 'EUR': i font core di fpdf non hanno il simbolo €."""
    return fmt_eur(value).replace(" €", " EUR")


def fmt_date(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def fmt_date_pdf(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "-"


def italian_today(now_utc: Optional[datetime] = None) -> date:
    """Giorno di calendario italiano (il server gira in UTC)."""
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(config.TIMEZONE)).date()


# Caratteri tipografici frequenti nei nomi presi da Shopify/FatturaPro che i
# font core di fpdf (latin-1) non sanno stampare: mappati a un equivalente.
_PDF_REPLACEMENTS = {
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", "€": "EUR",
    " ": " ", "•": "-",
}


def pdf_text(value) -> str:
    """Testo stampabile con i font core di fpdf2 (latin-1): sostituisce i
    caratteri tipografici, toglie gli accenti dove latin-1 non arriva (es.
    giapponese → '?'), senza mai sollevare eccezioni."""
    text = "" if value is None else str(value)
    for src, dst in _PDF_REPLACEMENTS.items():
        text = text.replace(src, dst)
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        pass
    decomposed = unicodedata.normalize("NFKD", text)
    out = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            continue
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def safe_filename(value: str, max_len: int = 80) -> str:
    """Nome file ASCII: accenti tolti (Domò → Domo), il resto → '_'."""
    ascii_text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_text).strip("_")
    return (cleaned or "documento")[:max_len]
