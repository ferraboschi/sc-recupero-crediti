"""Agente di vendita di un cliente, derivato dai tag cliente di Shopify.

I tag arrivano dal sync clienti Shopify come stringa "B2B, Maggioni"
(colonna ``customers.tags``) e vengono mostrati accanto alla ragione
sociale in tutte le liste. Regole decise dall'owner (9/11/2026):

- tre agenti: Maggioni, Izzo, Scotti;
- «Rendina» si mostra come Izzo; «Sgariglia De Leo» si mostra come Maggioni;
- se un cliente porta più agenti si mostrano TUTTI (nessuna scelta al posto
  dell'operatore), in ordine fisso Maggioni → Izzo → Scotti.

Le grafie su Shopify non sono uniformi ("SCOTTI", "scotti", "Scotti",
"rendina", "sgariglia de leo"): il confronto è per parola intera, senza
distinzione di maiuscole, tag per tag.

Censimento sul DB di produzione (9/11/2026): le sole grafie presenti nei tag
sono «maggioni», «izzo», «scotti», «rendina», «sgariglia de leo» — nessuna
forma di negazione («ex Scotti», «vecchio Maggioni»). Se un giorno comparisse,
il pattern va stretto QUI (con un test sul tag vero), non nei chiamanti.

Modulo PURO (nessun accesso al DB): testato in tests/test_agents.py.
"""
import re
from typing import Iterable, List, Optional, Union

# Ordine di visualizzazione, stabile ovunque.
AGENTS = ("Maggioni", "Izzo", "Scotti")

# Un pattern per agente, applicato a OGNI tag separatamente. Confine di parola:
# "Pizzoccheri" non è Izzo, "Rendina" lo è (alias deciso dall'owner).
_AGENT_PATTERNS = (
    ("Maggioni", re.compile(r"\b(maggioni|sgariglia)\b", re.IGNORECASE)),
    ("Izzo", re.compile(r"\b(izzo|rendina)\b", re.IGNORECASE)),
    ("Scotti", re.compile(r"\bscotti\b", re.IGNORECASE)),
)

TagsInput = Union[None, str, Iterable[str]]


def split_tags(tags: TagsInput) -> List[str]:
    """Tag puliti dalla stringa Shopify ("B2B, Maggioni") o da una lista."""
    if not tags:
        return []
    if isinstance(tags, str):
        parts: Iterable[str] = tags.split(",")
    else:
        parts = tags
    out: List[str] = []
    for part in parts:
        if part is None:
            continue
        clean = str(part).strip()
        if clean:
            out.append(clean)
    return out


def agents_from_tags(tags: TagsInput) -> List[str]:
    """Agenti (nomi canonici) presenti nei tag, nell'ordine di AGENTS.

    Lista vuota se nessun tag corrisponde: il chiamante non mostra nulla.
    """
    found = set()
    for tag in split_tags(tags):
        for name, pattern in _AGENT_PATTERNS:
            if pattern.search(tag):
                found.add(name)
    return [name for name in AGENTS if name in found]


def agents_of(customer: Optional[object]) -> List[str]:
    """Comodità per i serializzatori: accetta un Customer (o None)."""
    if customer is None:
        return []
    return agents_from_tags(getattr(customer, "tags", None))
