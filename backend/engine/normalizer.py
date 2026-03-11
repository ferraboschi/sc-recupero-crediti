"""Normalizer module - normalizes Italian company names (ragione sociali) for matching."""

import re
import logging
import unicodedata
from typing import Tuple

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


# Italian legal form abbreviations to remove
LEGAL_FORMS = [
    "S.R.L.", "SRL",
    "S.P.A.", "SPA",
    "S.A.S.", "SAS",
    "S.N.C.", "SNC",
    "S.S.",
    "S.C.",
    "S.C.A.R.L.", "SCARL",
    "S.C.P.A.", "SCPA",
    "S.P.A.M.", "SPAM",
    "S.R.C.", "SRC",
    "S.R.S.", "SRS",
    "P.A.",
    "A.S.",
    "A.S.D.",
    "A.P.S.",
    "O.N.G.",
    "E.T.S.",
]

# Common prefixes to remove
COMMON_PREFIXES = [
    "ditta",
    "impresa",
    "società",
    "azienda",
    "cooperativa",
]


def remove_accents(text: str) -> str:
    """Remove accents and diacritics from text."""
    nfkd_form = unicodedata.normalize("NFKD", text)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])


def normalize_ragione_sociale(name: str) -> str:
    """
    Normalize Italian company name (ragione sociale) for matching.

    Removes:
    - Legal forms (S.R.L., SPA, etc.)
    - "di" + personal name patterns
    - Punctuation and extra whitespace
    - Common prefixes
    - Accents and diacritics

    Converts to lowercase.

    Args:
        name: The raw company name to normalize

    Returns:
        Normalized company name suitable for matching

    Examples:
        >>> normalize_ragione_sociale("SHU&SHU DI SHU KEI S.A.S.")
        "shu&shu"
        >>> normalize_ragione_sociale("ACME S.R.L.")
        "acme"
        >>> normalize_ragione_sociale("Società ROSSI s.p.a.")
        "rossi"
    """
    if not name:
        return ""

    # Remove accents first
    normalized = remove_accents(name)

    # Convert to lowercase
    normalized = normalized.lower()

    # Remove legal forms (must handle both with and without dots)
    # First, build patterns that handle dots flexibly (e.g., S.R.L. or SRL or S.r.l)
    for form in LEGAL_FORMS:
        # Create pattern that matches with or without dots and trailing dot
        escaped = re.escape(form.lower())
        # Also create a dot-flexible version: "s.r.l." matches "s.r.l." and "srl" and "s.r.l"
        normalized = re.sub(rf"(?<!\w){escaped}\.?(?!\w)", "", normalized)
        # Also match without dots
        nodots = form.replace(".", "").lower()
        if nodots != form.lower():
            normalized = re.sub(rf"(?<!\w){re.escape(nodots)}(?!\w)", "", normalized)

    # Clean up before di pattern matching
    normalized = normalized.strip()

    # Handle "di" + personal name pattern
    # E.g., "SHU&SHU DI SHU KEI" -> "SHU&SHU"
    # Pattern: word(s) + "di" + word(s) where the "di" part is a personal name
    di_pattern = re.compile(r"\s+di\s+\w+(?:\s+\w+)*\s*$")
    normalized = di_pattern.sub("", normalized)

    # Remove common prefixes
    prefix_pattern = "|".join(re.escape(prefix.lower()) for prefix in COMMON_PREFIXES)
    prefix_regex = re.compile(rf"^\b({prefix_pattern})\b\s+")
    normalized = prefix_regex.sub("", normalized)

    # Remove punctuation except for & and -
    # Keep & and - as they can be part of company names
    normalized = re.sub(r"[^\w&\-\s]", "", normalized)

    # Remove extra whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized


def are_similar(
    name1: str,
    name2: str,
    threshold: int = 85
) -> Tuple[bool, int]:
    """
    Check if two company names are similar using fuzzy matching.

    Uses rapidfuzz token_set_ratio for robust comparison that handles
    different word order and variations.

    Args:
        name1: First company name (will be normalized)
        name2: Second company name (will be normalized)
        threshold: Minimum similarity score (0-100) to consider a match

    Returns:
        Tuple of (is_similar: bool, score: int where 0-100 is the similarity score)

    Examples:
        >>> are_similar("ACME S.R.L.", "Acme srl")
        (True, 100)
        >>> are_similar("Mario Rossi S.A.S.", "Rossi Mario SAS")
        (True, 90)  # Approximate
        >>> are_similar("Company A", "Company B")
        (False, 15)  # Approximate
    """
    norm1 = normalize_ragione_sociale(name1)
    norm2 = normalize_ragione_sociale(name2)

    # Use token_set_ratio which is better for matching names with different word order
    # and partial matches
    score = fuzz.token_set_ratio(norm1, norm2)

    is_similar = score >= threshold

    logger.debug(
        f"Similarity check: '{name1}' vs '{name2}' -> score={score}, similar={is_similar}"
    )

    return is_similar, score
