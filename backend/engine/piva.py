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


def normalize_piva(raw: Optional[str]) -> str:
    """Uppercase, senza spazi né prefisso 'IT' ridondante."""
    if not raw:
        return ""
    piva = re.sub(r"[\s.\-]", "", raw.strip().upper())
    # 'IT12345678901' e '12345678901' sono la stessa P.IVA italiana
    if piva.startswith("IT") and _ITALIAN_RE.match(piva[2:]):
        piva = piva[2:]
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
