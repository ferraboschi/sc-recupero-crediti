"""Orario d'ufficio FatturaPro (regola owner, 5/11/2026).

FatturaPro ammette UNA sessione per account: ogni nostro login chiude quella
del personale che sta lavorando. Quindi: un solo accesso automatico al
giorno (FATTURAPRO_SYNC_HOUR) e nessun accesso — nemmeno dai pulsanti
dell'app — nella finestra FATTURAPRO_QUIET_HOURS (default "8-18", ore di
config.TIMEZONE). Modulo PURO: nessuna dipendenza dal connettore, così i
test che sostituiscono il connettore con un finto non lo toccano.
"""
import re
from datetime import datetime
from typing import Optional, Tuple

import pytz

from backend.config import config


def quiet_hours_window() -> Tuple[int, int]:
    """(inizio, fine) in ore locali, finestra [inizio, fine). Valore non
    valido o inizio == fine → (0, 0) = nessun blocco."""
    raw = str(getattr(config, "FATTURAPRO_QUIET_HOURS", "") or "").strip()
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", raw)
    if not m:
        return (0, 0)
    start, end = int(m.group(1)), int(m.group(2))
    if not (0 <= start <= 24 and 0 <= end <= 24) or start == end:
        return (0, 0)
    return (start, end)


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    """True se `now` (default: adesso, in config.TIMEZONE) cade nell'orario
    d'ufficio. Se inizio > fine la finestra scavalca la mezzanotte."""
    start, end = quiet_hours_window()
    if start == end:
        return False
    tz = pytz.timezone(config.TIMEZONE)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = tz.localize(now)
    else:
        now = now.astimezone(tz)
    h = now.hour
    if start < end:
        return start <= h < end
    return h >= start or h < end


def quiet_hours_message() -> str:
    start, end = quiet_hours_window()
    return (
        f"FatturaPro non consultato: orario d'ufficio ({start:02d}:00-{end:02d}:00, ora italiana), "
        f"il personale sta lavorando. Accesso automatico una volta al giorno alle "
        f"{config.FATTURAPRO_SYNC_HOUR:02d}:{config.FATTURAPRO_SYNC_MINUTE:02d}; "
        f"i pulsanti tornano disponibili dalle {end:02d}:00."
    )
