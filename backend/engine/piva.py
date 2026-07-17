"""Validazione Partita IVA.

La P.IVA è l'identificatore canonico di un'entità commerciale: una P.IVA
sporca (scrappata male, troncata, appartenente al venditore) produce
abbinamenti fattura→cliente sbagliati a cascata. Qui si valida PRIMA di
usarla per qualsiasi match.

- Italiana: esattamente 11 cifre + checksum ufficiale (algoritmo Luhn-like
  dell'Agenzia delle Entrate).
- Estera: prefisso paese di 2-3 lettere + 8-15 cifre (es. CHE..., DE...).
  Nessun checksum (ogni paese ha il suo): solo validazione di formato,
  il match resta per uguaglianza esatta di stringa.
"""

import re
from typing import Optional

_FOREIGN_RE = re.compile(r"^[A-Z]{2,3}\d{8,15}$")
_ITALIAN_RE = re.compile(r"^\d{11}$")

# Prefissi paese italiani: 'IT' (ISO alpha-2, lo standard VIES/EU) e 'ITA'
# (alpha-3, dai campi-paese di molti gestionali). Davanti a sole cifre sono
# entrambi ridondanti: l'Italia ha solo P.IVA di 11 cifre, quindi se il
# resto è numerico la P.IVA è italiana — valida o corrotta che sia — e deve
# passare dal checksum, non da _FOREIGN_RE.
_IT_PREFIXES = ("ITA", "IT")  # il più lungo per primo


def normalize_piva(raw: Optional[str]) -> str:
    """Uppercase, senza spazi né prefisso 'IT' ridondante."""
    if not raw:
        return ""
    piva = re.sub(r"[\s.\-]", "", raw.strip().upper())
    # 'IT12345678901' e '12345678901' sono la stessa P.IVA italiana.
    # Il prefisso va tolto ogni volta che il resto è tutto CIFRE, non solo
    # quando sono esattamente 11: altrimenti una P.IVA italiana corrotta
    # (10 o 12 cifre) resta 'ITxxx', passa per estera (_FOREIGN_RE) e salta
    # il checksum.
    for prefix in _IT_PREFIXES:
        if piva.startswith(prefix) and piva[len(prefix):].isdigit():
            piva = piva[len(prefix):]
            break
    return piva


def _italian_checksum_ok(piva: str) -> bool:
    """Checksum ufficiale P.IVA italiana (11 cifre)."""
    digits = [int(c) for c in piva]
    total = 0
    for i, d in enumerate(digits[:10]):
        if i % 2 == 0:  # posizioni dispari (1-based)
            total += d
        else:
            doubled = d * 2
            total += doubled if doubled < 10 else doubled - 9
    check = (10 - (total % 10)) % 10
    return check == digits[10]


def validate_piva(raw: Optional[str]) -> Optional[str]:
    """Ritorna la P.IVA normalizzata se valida, altrimenti None.

    Una P.IVA invalida va trattata come ASSENTE: meglio nessun match
    automatico che un match su un identificatore corrotto.
    """
    piva = normalize_piva(raw)
    if not piva:
        return None
    if _ITALIAN_RE.match(piva):
        return piva if _italian_checksum_ok(piva) else None
    if _FOREIGN_RE.match(piva):
        return piva
    return None


def is_valid_piva(raw: Optional[str]) -> bool:
    return validate_piva(raw) is not None


def is_checksum_backed(raw: Optional[str]) -> bool:
    """True SOLO se la P.IVA è garantita da checksum (italiana, 11 cifre).

    Le P.IVA estere passano la sola validazione di FORMATO: due entità
    diverse potrebbero condividere una stringa estera format-valida ma
    inventata. Per una GARANZIA forte (semaforo verde) serve il checksum.
    """
    piva = validate_piva(raw)
    return bool(piva and _ITALIAN_RE.match(piva))
