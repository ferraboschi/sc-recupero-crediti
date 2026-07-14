"""Tests for the matching engine module.

New semantics under test:
- match_invoice_to_customer returns a MatchResult (customer/method/score +
  suggested_customer/suggested_method/suggested_score).
- P.IVA is checksum-validated: an invalid P.IVA is treated as absent.
  All Italian P.IVA fixtures below are checksum-valid (verified against
  backend.engine.piva.validate_piva).
- Fuzzy NEVER auto-matches: it only produces a quarantined suggestion.
- run_matching stats keys: matched_piva / matched_exact / suggested /
  unmatched / total.
"""

import pytest
from datetime import date
from backend.engine.matching import match_invoice_to_customer, run_matching, MatchResult
from backend.engine.piva import validate_piva
from backend.database import Invoice, Customer

# Checksum-valid Italian P.IVA fixtures (see backend/engine/piva.py)
PIVA_A = "12345678903"
PIVA_B = "98765432103"
PIVA_C = "11111111115"
PIVA_D = "22222222220"
PIVA_INVALID = "12345678901"  # fails the official checksum
PIVA_FOREIGN = "CHE123456789"  # foreign format: valid by format only


def test_piva_fixtures_are_checksum_valid():
    """Guard: the P.IVA fixtures used across this file must be valid."""
    for piva in (PIVA_A, PIVA_B, PIVA_C, PIVA_D, PIVA_FOREIGN):
        assert validate_piva(piva) is not None, piva
    assert validate_piva(PIVA_INVALID) is None


def make_invoice(session, name=None, piva=None, number="INV001", **kwargs):
    """Create and persist a minimal invoice."""
    invoice = Invoice(
        invoice_number=number,
        amount=1000.0,
        amount_due=1000.0,
        issue_date=date(2024, 1, 15),
        customer_name_raw=name,
        customer_piva_raw=piva,
        source_platform="fatturapro",
        status="open",
        **kwargs
    )
    session.add(invoice)
    session.commit()
    return invoice


def make_customer(session, name, piva=None):
    """Create and persist a minimal customer."""
    customer = Customer(
        ragione_sociale=name,
        partita_iva=piva,
        source="shopify",
    )
    session.add(customer)
    session.commit()
    return customer


class TestMatchInvoiceToCustomer:
    """Tests for the match_invoice_to_customer function."""

    def test_piva_exact_match_priority(self, test_db_session):
        """P.IVA exact match wins over any name-based strategy."""
        customer1 = make_customer(test_db_session, "ACME S.R.L.", PIVA_A)
        customer2 = make_customer(test_db_session, "ACME Global S.P.A.", PIVA_B)

        # Name would fuzzy-match customer2, but P.IVA belongs to customer1
        invoice = make_invoice(test_db_session, name="ACME Global", piva=PIVA_A)

        result = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        assert isinstance(result, MatchResult)
        assert result.customer is not None
        assert result.customer.id == customer1.id
        assert result.method == "piva"
        assert result.score == 100
        assert result.suggested_customer is None

    def test_piva_match_case_insensitive(self, test_db_session):
        """P.IVA matching is case-insensitive (foreign prefixes uppercased)."""
        customer = make_customer(test_db_session, "Swiss Sake GmbH", PIVA_FOREIGN)

        invoice = make_invoice(test_db_session, name="Swiss Sake", piva="che123456789")

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is not None
        assert result.customer.id == customer.id
        assert result.method == "piva"

    def test_piva_match_with_leading_trailing_spaces(self, test_db_session):
        """P.IVA matching ignores leading/trailing spaces."""
        customer = make_customer(test_db_session, "ACME S.R.L.", PIVA_A)

        invoice = make_invoice(test_db_session, name="ACME SRL", piva="  " + PIVA_A + "  ")

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is not None
        assert result.customer.id == customer.id
        assert result.method == "piva"

    def test_piva_invalid_checksum_treated_as_absent(self, test_db_session):
        """An invalid P.IVA must NOT produce a P.IVA match (falls to name)."""
        customer = make_customer(test_db_session, "ACME S.R.L.", PIVA_INVALID)

        # Same invalid P.IVA on both sides; name matches exactly after
        # normalization -> the match must come from name_exact, not piva.
        invoice = make_invoice(test_db_session, name="ACME SRL", piva=PIVA_INVALID)

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is not None
        assert result.customer.id == customer.id
        assert result.method == "name_exact"

    def test_piva_foreign_exact_match(self, test_db_session):
        """A foreign-format P.IVA (e.g. CHE...) auto-matches by exact string."""
        customer = make_customer(test_db_session, "Swiss Sake GmbH", PIVA_FOREIGN)
        other = make_customer(test_db_session, "ACME S.R.L.", PIVA_A)

        invoice = make_invoice(test_db_session, name="Swiss Sake", piva=PIVA_FOREIGN)

        result = match_invoice_to_customer(invoice, [customer, other], test_db_session)
        assert result.customer is not None
        assert result.customer.id == customer.id
        assert result.method == "piva"
        assert result.score == 100

    def test_piva_ambiguous_two_customers_same_piva(self, test_db_session):
        """2+ customers with the same P.IVA -> quarantined suggestion."""
        customer1 = make_customer(test_db_session, "Rossi Costruzioni S.R.L.", PIVA_A)
        customer2 = make_customer(test_db_session, "Bianchi Impianti S.R.L.", PIVA_A)

        invoice = make_invoice(test_db_session, name="Bianchi Impianti", piva=PIVA_A)

        result = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        assert result.customer is None
        assert result.method is None
        assert result.suggested_method == "piva_ambiguous"
        # The suggestion is the duplicate whose name is most similar
        assert result.suggested_customer is not None
        assert result.suggested_customer.id == customer2.id
        assert result.suggested_score == 100

    def test_piva_name_mismatch_quarantined(self, test_db_session):
        """Same P.IVA but completely different names -> NOT auto-matched."""
        customer = make_customer(test_db_session, "ACME S.R.L.", PIVA_A)

        # token_set similarity "trattoria da gino" vs "acme" is < 40
        invoice = make_invoice(test_db_session, name="Trattoria da Gino", piva=PIVA_A)

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is None
        assert result.method is None
        assert result.suggested_customer is not None
        assert result.suggested_customer.id == customer.id
        assert result.suggested_method == "piva_name_mismatch"
        assert result.suggested_score < 40

    def test_exact_normalized_name_match(self, test_db_session):
        """Normalized ragione sociale exact match (unique) auto-matches."""
        customer = make_customer(test_db_session, "ACME S.R.L.", None)

        invoice = make_invoice(test_db_session, name="acme s.r.l.", piva=None)

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is not None
        assert result.customer.id == customer.id
        assert result.method == "name_exact"
        assert result.score == 100

    def test_exact_normalized_name_with_di_pattern(self, test_db_session):
        """Normalized match with 'di + nome cognome' pattern removal."""
        customer = make_customer(test_db_session, "SHU&SHU S.A.S.", None)

        invoice = make_invoice(
            test_db_session, name="SHU&SHU DI SHU KEI S.A.S.", piva=None
        )

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is not None
        assert result.customer.id == customer.id
        assert result.method == "name_exact"

    def test_name_ambiguous_two_customers_same_normalized_name(self, test_db_session):
        """2+ customers sharing the same normalized name -> quarantined."""
        customer1 = make_customer(test_db_session, "ACME S.R.L.", None)
        customer2 = make_customer(test_db_session, "ACME S.P.A.", None)

        invoice = make_invoice(test_db_session, name="ACME", piva=None)

        result = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        assert result.customer is None
        assert result.method is None
        assert result.suggested_customer is not None
        assert result.suggested_customer.id in (customer1.id, customer2.id)
        assert result.suggested_method == "name_ambiguous"
        assert result.suggested_score == 100

    def test_short_normalized_name_never_auto_matches(self, test_db_session):
        """A normalized name shorter than 4 chars is not distinctive enough
        for an automatic match: at most a fuzzy suggestion."""
        customer = make_customer(test_db_session, "ABC S.R.L.", None)

        invoice = make_invoice(test_db_session, name="ABC", piva=None)

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is None
        assert result.method is None
        assert result.suggested_method == "fuzzy"
        assert result.suggested_customer.id == customer.id

    def test_fuzzy_is_only_a_suggestion(self, test_db_session):
        """Fuzzy NEVER auto-matches: it fills suggested_* only."""
        customer = make_customer(test_db_session, "ACME Global Solutions S.R.L.", None)

        invoice = make_invoice(test_db_session, name="ACME Global S.P.A.", piva=None)

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is None
        assert result.method is None
        assert result.score is None
        assert result.suggested_customer is not None
        assert result.suggested_customer.id == customer.id
        assert result.suggested_method == "fuzzy"
        assert result.suggested_score >= 75  # config.FUZZY_MATCH_THRESHOLD

    def test_fuzzy_suggestion_best_score(self, test_db_session):
        """The fuzzy suggestion is the candidate with the highest score."""
        # Both above threshold: ~96 vs 100
        customer1 = make_customer(test_db_session, "ACME Commercial S.R.L.", None)
        customer2 = make_customer(test_db_session, "ACME Comercial Import S.R.L.", None)

        invoice = make_invoice(test_db_session, name="ACME Comercial", piva=None)

        result = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        assert result.customer is None
        assert result.suggested_customer is not None
        assert result.suggested_customer.id == customer2.id
        assert result.suggested_method == "fuzzy"
        assert result.suggested_score == 100

    def test_fuzzy_below_threshold_no_suggestion(self, test_db_session):
        """Below FUZZY_MATCH_THRESHOLD nothing is suggested."""
        customer = make_customer(
            test_db_session, "Completely Different Company S.R.L.", None
        )

        invoice = make_invoice(test_db_session, name="Unrelated Bakery", piva=None)

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is None
        assert result.suggested_customer is None
        assert result.suggested_method is None

    def test_conflicting_piva_blocks_name_and_fuzzy(self, test_db_session):
        """Two different VALID P.IVA = different entities: even an identical
        name must not match (neither exact nor fuzzy)."""
        customer = make_customer(test_db_session, "ACME S.R.L.", PIVA_B)

        invoice = make_invoice(test_db_session, name="ACME S.R.L.", piva=PIVA_A)

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is None
        assert result.method is None
        assert result.suggested_customer is None
        assert result.suggested_method is None

    def test_no_customer_data(self, test_db_session):
        """Invoice without name and P.IVA -> empty result."""
        customer = make_customer(test_db_session, "ACME S.R.L.", PIVA_A)

        invoice = make_invoice(test_db_session, name=None, piva=None)

        result = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert result.customer is None
        assert result.suggested_customer is None

    def test_empty_customer_list(self, test_db_session):
        """Empty customer list -> empty result."""
        invoice = make_invoice(test_db_session, name="ACME S.R.L.", piva=PIVA_A)

        result = match_invoice_to_customer(invoice, [], test_db_session)
        assert result.customer is None
        assert result.suggested_customer is None

    def test_exact_name_match_preferred_over_fuzzy(self, test_db_session):
        """Exact normalized name auto-matches even with a fuzzy candidate."""
        customer1 = make_customer(test_db_session, "ACME S.R.L.", None)
        customer2 = make_customer(test_db_session, "ACME Solutions S.P.A.", None)

        invoice = make_invoice(test_db_session, name="ACME S.R.L.", piva=None)

        result = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        assert result.customer is not None
        assert result.customer.id == customer1.id
        assert result.method == "name_exact"


class TestRunMatching:
    """Tests for the run_matching batch function."""

    def test_run_matching_empty_database(self, test_db_session):
        """No invoices: every stat is zero, with the new keys."""
        stats = run_matching(test_db_session)
        assert stats == {
            'matched_piva': 0,
            'matched_exact': 0,
            'suggested': 0,
            'unmatched': 0,
            'total': 0,
        }

    def test_run_matching_no_customers(self, test_db_session):
        """Invoices but no customers: all unmatched."""
        make_invoice(test_db_session, name="ACME S.R.L.", piva=PIVA_A)

        stats = run_matching(test_db_session)
        assert stats['total'] == 1
        assert stats['unmatched'] == 1
        assert stats['matched_piva'] == 0
        assert stats['matched_exact'] == 0
        assert stats['suggested'] == 0

    def test_run_matching_piva_matches(self, test_db_session):
        """Batch P.IVA matches set customer_id + provenance fields."""
        customer = make_customer(test_db_session, "ACME S.R.L.", PIVA_A)

        invoices = [
            make_invoice(test_db_session, name="Some Name", piva=PIVA_A, number="INV%03d" % i)
            for i in range(3)
        ]

        stats = run_matching(test_db_session)
        assert stats['total'] == 3
        assert stats['matched_piva'] == 3
        assert stats['matched_exact'] == 0
        assert stats['suggested'] == 0
        assert stats['unmatched'] == 0

        for invoice in invoices:
            test_db_session.refresh(invoice)
            assert invoice.customer_id == customer.id
            assert invoice.match_method == "piva"
            assert invoice.match_score == 100
            assert invoice.suggested_customer_id is None
            assert invoice.suggested_method is None
            assert invoice.suggested_score is None

    def test_run_matching_exact_name_matches(self, test_db_session):
        """Batch exact-name matches counted as matched_exact."""
        customer = make_customer(test_db_session, "ACME S.R.L.", None)

        invoices = [
            make_invoice(test_db_session, name="acme s.r.l.", piva=None, number="INV%03d" % i)
            for i in range(2)
        ]

        stats = run_matching(test_db_session)
        assert stats['total'] == 2
        assert stats['matched_exact'] == 2
        assert stats['matched_piva'] == 0
        assert stats['suggested'] == 0

        for invoice in invoices:
            test_db_session.refresh(invoice)
            assert invoice.customer_id == customer.id
            assert invoice.match_method == "name_exact"

    def test_run_matching_fuzzy_goes_to_quarantine(self, test_db_session):
        """Fuzzy candidates are ONLY suggested: customer_id stays None."""
        customer = make_customer(test_db_session, "ACME Global Solutions S.R.L.", None)

        invoice = make_invoice(test_db_session, name="ACME Global S.P.A.", piva=None)

        stats = run_matching(test_db_session)
        assert stats['total'] == 1
        assert stats['suggested'] == 1
        assert stats['matched_piva'] == 0
        assert stats['matched_exact'] == 0
        assert stats['unmatched'] == 0

        test_db_session.refresh(invoice)
        assert invoice.customer_id is None
        assert invoice.match_method is None
        assert invoice.suggested_customer_id == customer.id
        assert invoice.suggested_method == "fuzzy"
        assert invoice.suggested_score >= 75

    def test_run_matching_mixed_results(self, test_db_session):
        """Batch with P.IVA match, exact match, suggestion and unmatched."""
        customer1 = make_customer(test_db_session, "ACME S.R.L.", PIVA_A)
        customer2 = make_customer(test_db_session, "ACME Global Solutions S.R.L.", None)

        # P.IVA match (name similar enough not to trip the mismatch guard)
        invoice1 = make_invoice(test_db_session, name="Some Name", piva=PIVA_A, number="INV001")
        # Exact name match on customer2
        invoice2 = make_invoice(
            test_db_session, name="acme global solutions srl", piva=None, number="INV002"
        )
        # Fuzzy -> suggestion only
        invoice3 = make_invoice(
            test_db_session, name="ACME Global S.P.A.", piva=None, number="INV003"
        )
        # No match at all
        invoice4 = make_invoice(
            test_db_session, name="Trattoria Da Gino", piva=None, number="INV004"
        )

        stats = run_matching(test_db_session)
        assert stats['total'] == 4
        assert stats['matched_piva'] == 1
        assert stats['matched_exact'] == 1
        assert stats['suggested'] == 1
        assert stats['unmatched'] == 1

        test_db_session.refresh(invoice1)
        assert invoice1.customer_id == customer1.id
        test_db_session.refresh(invoice2)
        assert invoice2.customer_id == customer2.id
        test_db_session.refresh(invoice3)
        assert invoice3.customer_id is None
        assert invoice3.suggested_customer_id is not None
        test_db_session.refresh(invoice4)
        assert invoice4.customer_id is None
        assert invoice4.suggested_customer_id is None

    def test_run_matching_does_not_match_already_matched(self, test_db_session):
        """run_matching only processes invoices with customer_id = NULL."""
        customer1 = make_customer(test_db_session, "Customer Uno", PIVA_C)
        customer2 = make_customer(test_db_session, "Customer Due", PIVA_D)

        # Already matched invoice: must not be reprocessed
        make_invoice(
            test_db_session,
            name="Customer Uno",
            piva=PIVA_C,
            number="INV001",
            customer_id=customer1.id,
        )
        # Unmatched invoice
        invoice2 = make_invoice(
            test_db_session, name="Customer Due", piva=PIVA_D, number="INV002"
        )

        stats = run_matching(test_db_session)
        assert stats['total'] == 1
        assert stats['matched_piva'] == 1

        test_db_session.refresh(invoice2)
        assert invoice2.customer_id == customer2.id

    def test_run_matching_unlinked_never_auto_assigned(self, test_db_session):
        """A manually unlinked invoice is NEVER auto-matched again:
        even a perfect P.IVA match becomes only a suggestion."""
        customer = make_customer(test_db_session, "ACME S.R.L.", PIVA_A)

        invoice = make_invoice(
            test_db_session,
            name="ACME SRL",
            piva=PIVA_A,
            match_method="unlinked",
        )

        stats = run_matching(test_db_session)
        assert stats['total'] == 1
        assert stats['matched_piva'] == 0
        assert stats['matched_exact'] == 0
        assert stats['suggested'] == 1

        test_db_session.refresh(invoice)
        assert invoice.customer_id is None
        assert invoice.suggested_customer_id == customer.id
        assert invoice.suggested_method == "piva"
        assert invoice.suggested_score == 100
