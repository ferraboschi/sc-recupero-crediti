"""Test validazione P.IVA (checksum italiano + formati esteri)."""

from backend.engine.piva import normalize_piva, validate_piva, is_valid_piva


def _make_valid_italian(first10: str) -> str:
    """Costruisce una P.IVA italiana valida calcolando la cifra di controllo
    con l'algoritmo ufficiale (implementazione indipendente dal modulo)."""
    digits = [int(c) for c in first10]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 0:
            total += d
        else:
            doubled = d * 2
            total += doubled if doubled < 10 else doubled - 9
    check = (10 - (total % 10)) % 10
    return first10 + str(check)


VALID_IT = _make_valid_italian("0123456789")
VALID_IT_2 = _make_valid_italian("0463155037")


class TestNormalizePiva:
    def test_empty_and_none(self):
        assert normalize_piva(None) == ""
        assert normalize_piva("") == ""
        assert normalize_piva("   ") == ""

    def test_strips_spaces_dots_dashes(self):
        assert normalize_piva(" 01.234-567 890 ") == "01234567890"
        assert normalize_piva("01234 567890") == "01234567890"

    def test_uppercase(self):
        assert normalize_piva("che123456789") == "CHE123456789"

    def test_it_prefix_stripped(self):
        assert normalize_piva(f"IT{VALID_IT}") == VALID_IT

    def test_it_prefix_kept_when_rest_is_not_numeric(self):
        # Il prefisso si toglie solo se il resto è tutto cifre. "IT" davanti
        # a lettere non è un prefisso di P.IVA (es. una ragione sociale
        # finita per sbaglio nel campo) e va conservato.
        assert normalize_piva("ITALIA") == "ITALIA"
        assert normalize_piva("IT") == "IT"


class TestValidatePiva:
    def test_valid_italian_checksum(self):
        assert validate_piva(VALID_IT) == VALID_IT
        assert validate_piva(VALID_IT_2) == VALID_IT_2

    def test_wrong_check_digit_rejected(self):
        # Cambia SOLO la cifra di controllo: deve fallire
        wrong = VALID_IT[:-1] + str((int(VALID_IT[-1]) + 1) % 10)
        assert validate_piva(wrong) is None

    def test_mutated_body_digit_rejected(self):
        mutated = str((int(VALID_IT[0]) + 1) % 10) + VALID_IT[1:]
        assert validate_piva(mutated) is None

    def test_too_short_or_long_rejected(self):
        assert validate_piva("1234567890") is None      # 10 cifre
        assert validate_piva("123456789012") is None    # 12 cifre

    def test_foreign_with_country_prefix_accepted(self):
        assert validate_piva("CHE123456789") == "CHE123456789"
        assert validate_piva("DE129273398") == "DE129273398"

    def test_foreign_bad_shapes_rejected(self):
        assert validate_piva("C123456789") is None      # 1 lettera
        assert validate_piva("CHEX123") is None
        assert validate_piva("ABCD12345678") is None    # 4 lettere

    def test_garbage_rejected(self):
        assert validate_piva("Via Roma 1") is None
        assert validate_piva("N/A") is None
        assert validate_piva(None) is None

    def test_it_prefixed_valid(self):
        assert validate_piva(f"IT{VALID_IT}") == VALID_IT

    def test_is_valid_wrapper(self):
        assert is_valid_piva(VALID_IT) is True
        assert is_valid_piva("badpiva") is False


def test_it_prefix_never_bypasses_italian_checksum():
    """IT + cifre è SEMPRE una P.IVA italiana: deve passare dal checksum.

    Regressione: 'IT1234567890' (10 cifre) veniva accettata come P.IVA
    ESTERA (_FOREIGN_RE), saltando il checksum, mentre '1234567890' nuda
    veniva correttamente rifiutata.
    """
    assert validate_piva("1234567890") is None
    assert validate_piva("IT1234567890") is None
    assert validate_piva("123456789012") is None
    assert validate_piva("IT123456789012") is None
    assert validate_piva("IT12345678903") == "12345678903"
    assert validate_piva("12345678903") == "12345678903"
    # 'ITA' (ISO alpha-3) è ridondante quanto 'IT': non deve aprire una
    # scorciatoia verso _FOREIGN_RE aggirando il checksum con UNA lettera.
    assert validate_piva("ITA1234567890") is None
    assert validate_piva("ITA12345678903") == "12345678903"


def test_foreign_piva_still_valid():
    """Le P.IVA estere vere non devono essere toccate dal fix."""
    assert validate_piva("DE123456789") == "DE123456789"
    assert validate_piva("FR12345678901") == "FR12345678901"
