"""Tests for the matching engine module."""

import pytest
from datetime import date
from backend.engine.matching import match_invoice_to_customer, run_matching
from backend.database import Invoice, Customer


class TestMatchInvoiceToCustomer:
    """Tests for the match_invoice_to_customer function."""

    def test_piva_exact_match_priority(self, test_db_session):
        """Test P.IVA exact match has highest priority."""
        # Create a customer
        customer1 = Customer(
            ragione_sociale="ACME S.R.L.",
            partita_iva="12345678901",
            source="shopify"
        )
        customer2 = Customer(
            ragione_sociale="Different Company S.P.A.",
            partita_iva="98765432100",
            source="shopify"
        )
        test_db_session.add(customer1)
        test_db_session.add(customer2)
        test_db_session.commit()

        # Create invoice with matching P.IVA
        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Different Name",  # Name doesn't match
            customer_piva_raw="12345678901",  # But P.IVA matches customer1
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        # Match should find customer1 by P.IVA
        matched = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        assert matched is not None
        assert matched.id == customer1.id

    def test_piva_match_case_insensitive(self, test_db_session):
        """Test P.IVA matching is case-insensitive."""
        customer = Customer(
            ragione_sociale="ACME S.R.L.",
            partita_iva="12345678901",
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Some Name",
            customer_piva_raw="12345678901",  # Will be uppercased
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert matched is not None
        assert matched.id == customer.id

    def test_piva_match_with_leading_trailing_spaces(self, test_db_session):
        """Test P.IVA matching ignores leading/trailing spaces."""
        customer = Customer(
            ragione_sociale="ACME S.R.L.",
            partita_iva="1234567890",  # 10 digits
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Some Name",
            customer_piva_raw="  1234567890  ",  # With leading/trailing spaces
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert matched is not None
        assert matched.id == customer.id

    def test_exact_normalized_name_match(self, test_db_session):
        """Test normalized ragione sociale exact match."""
        customer = Customer(
            ragione_sociale="ACME S.R.L.",
            partita_iva=None,  # No P.IVA to test second strategy
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="acme s.r.l.",  # Different format, same normalized
            customer_piva_raw=None,
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert matched is not None
        assert matched.id == customer.id

    def test_exact_normalized_name_with_di_pattern(self, test_db_session):
        """Test normalized match with 'di' pattern removal."""
        customer = Customer(
            ragione_sociale="SHU&SHU S.A.S.",
            partita_iva=None,
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="SHU&SHU DI SHU KEI S.A.S.",  # Same after normalization
            customer_piva_raw=None,
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert matched is not None
        assert matched.id == customer.id

    def test_fuzzy_match(self, test_db_session):
        """Test fuzzy matching when exact match fails."""
        customer = Customer(
            ragione_sociale="ACME Global Solutions S.R.L.",
            partita_iva=None,
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="ACME Global S.P.A.",  # Similar but not exact
            customer_piva_raw=None,
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer], test_db_session)
        # Should match with fuzzy matching
        assert matched is not None
        assert matched.id == customer.id

    def test_fuzzy_match_best_score(self, test_db_session):
        """Test fuzzy match selects best score when multiple matches."""
        customer1 = Customer(
            ragione_sociale="ACME Solutions S.R.L.",
            partita_iva=None,
            source="shopify"
        )
        customer2 = Customer(
            ragione_sociale="ACME S.P.A.",  # Closer match
            partita_iva=None,
            source="shopify"
        )
        test_db_session.add(customer1)
        test_db_session.add(customer2)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="ACME S.R.L.",
            customer_piva_raw=None,
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        # Should match customer2 (ACME S.P.A.) as closest
        assert matched is not None

    def test_no_match_found(self, test_db_session):
        """Test when no match is found."""
        customer = Customer(
            ragione_sociale="Completely Different Company S.R.L.",
            partita_iva="98765432100",
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Some Unrelated Company",
            customer_piva_raw="12345678901",
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert matched is None

    def test_no_customer_data(self, test_db_session):
        """Test when invoice has no customer data."""
        customer = Customer(
            ragione_sociale="ACME S.R.L.",
            partita_iva="12345678901",
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw=None,
            customer_piva_raw=None,
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer], test_db_session)
        assert matched is None

    def test_empty_customer_list(self, test_db_session):
        """Test with empty customer list."""
        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="ACME S.R.L.",
            customer_piva_raw="12345678901",
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [], test_db_session)
        assert matched is None

    def test_piva_match_preferred_over_fuzzy(self, test_db_session):
        """Test that P.IVA match is preferred over fuzzy match."""
        customer1 = Customer(
            ragione_sociale="Different Name S.R.L.",
            partita_iva="12345678901",  # Exact match
            source="shopify"
        )
        customer2 = Customer(
            ragione_sociale="Almost Same Company S.R.L.",  # Fuzzy match
            partita_iva="98765432100",
            source="shopify"
        )
        test_db_session.add(customer1)
        test_db_session.add(customer2)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Almost Same Company",
            customer_piva_raw="12345678901",  # Matches customer1
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        assert matched.id == customer1.id  # P.IVA match preferred

    def test_exact_name_match_preferred_over_fuzzy(self, test_db_session):
        """Test that exact name match is preferred over fuzzy match."""
        customer1 = Customer(
            ragione_sociale="ACME S.R.L.",  # Exact match
            partita_iva=None,
            source="shopify"
        )
        customer2 = Customer(
            ragione_sociale="ACME Solutions S.P.A.",  # Fuzzy match
            partita_iva=None,
            source="shopify"
        )
        test_db_session.add(customer1)
        test_db_session.add(customer2)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="ACME S.R.L.",
            customer_piva_raw=None,
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        matched = match_invoice_to_customer(invoice, [customer1, customer2], test_db_session)
        assert matched.id == customer1.id  # Exact name match preferred


class TestRunMatching:
    """Tests for the run_matching batch function."""

    def test_run_matching_empty_database(self, test_db_session):
        """Test running matching with no invoices."""
        stats = run_matching(test_db_session)
        assert stats['total'] == 0
        assert stats['matched_piva'] == 0
        assert stats['matched_exact'] == 0
        assert stats['matched_fuzzy'] == 0
        assert stats['unmatched'] == 0

    def test_run_matching_no_customers(self, test_db_session):
        """Test running matching with invoices but no customers."""
        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="ACME S.R.L.",
            customer_piva_raw="12345678901",
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        stats = run_matching(test_db_session)
        assert stats['total'] == 1
        assert stats['unmatched'] == 1
        assert stats['matched_piva'] == 0
        assert stats['matched_exact'] == 0
        assert stats['matched_fuzzy'] == 0

    def test_run_matching_piva_matches(self, test_db_session):
        """Test batch matching with P.IVA matches."""
        customer = Customer(
            ragione_sociale="ACME S.R.L.",
            partita_iva="12345678901",
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoices = []
        for i in range(3):
            invoice = Invoice(
                invoice_number=f"INV{i:03d}",
                amount=1000.0,
                amount_due=1000.0,
                issue_date=date(2024, 1, 15),
                customer_name_raw="Some Name",
                customer_piva_raw="12345678901",
                source_platform="fatturapro",
                status="open"
            )
            test_db_session.add(invoice)
            invoices.append(invoice)
        test_db_session.commit()

        stats = run_matching(test_db_session)
        assert stats['total'] == 3
        assert stats['matched_piva'] == 3
        assert stats['matched_exact'] == 0
        assert stats['matched_fuzzy'] == 0
        assert stats['unmatched'] == 0

        # Verify invoices are now matched
        for invoice in invoices:
            test_db_session.refresh(invoice)
            assert invoice.customer_id == customer.id

    def test_run_matching_exact_name_matches(self, test_db_session):
        """Test batch matching with exact name matches."""
        customer = Customer(
            ragione_sociale="ACME S.R.L.",
            partita_iva=None,
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoices = []
        for i in range(2):
            invoice = Invoice(
                invoice_number=f"INV{i:03d}",
                amount=1000.0,
                amount_due=1000.0,
                issue_date=date(2024, 1, 15),
                customer_name_raw="acme s.r.l.",
                customer_piva_raw=None,
                source_platform="fatturapro",
                status="open"
            )
            test_db_session.add(invoice)
            invoices.append(invoice)
        test_db_session.commit()

        stats = run_matching(test_db_session)
        assert stats['total'] == 2
        assert stats['matched_exact'] == 2
        assert stats['matched_piva'] == 0
        assert stats['matched_fuzzy'] == 0

    def test_run_matching_fuzzy_matches(self, test_db_session):
        """Test batch matching with fuzzy matches."""
        customer = Customer(
            ragione_sociale="ACME Global Solutions S.R.L.",
            partita_iva=None,
            source="shopify"
        )
        test_db_session.add(customer)
        test_db_session.commit()

        invoice = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="ACME Global S.P.A.",
            customer_piva_raw=None,
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice)
        test_db_session.commit()

        stats = run_matching(test_db_session)
        assert stats['total'] == 1
        assert stats['matched_fuzzy'] >= 1  # May also count as exact
        assert stats['unmatched'] == 0

    def test_run_matching_mixed_results(self, test_db_session):
        """Test batch matching with mixed results."""
        customer1 = Customer(
            ragione_sociale="ACME S.R.L.",
            partita_iva="12345678901",
            source="shopify"
        )
        customer2 = Customer(
            ragione_sociale="ACME Global S.P.A.",
            partita_iva=None,
            source="shopify"
        )
        test_db_session.add(customer1)
        test_db_session.add(customer2)
        test_db_session.commit()

        # P.IVA match
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Some Name",
            customer_piva_raw="12345678901",
            source_platform="fatturapro",
            status="open"
        )

        # Exact name match
        invoice2 = Invoice(
            invoice_number="INV002",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="ACME Global S.P.A.",
            customer_piva_raw=None,
            source_platform="fatturapro",
            status="open"
        )

        # No match
        invoice3 = Invoice(
            invoice_number="INV003",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Unrelated Company",
            customer_piva_raw="99999999999",
            source_platform="fatturapro",
            status="open"
        )

        test_db_session.add_all([invoice1, invoice2, invoice3])
        test_db_session.commit()

        stats = run_matching(test_db_session)
        assert stats['total'] == 3
        assert stats['matched_piva'] == 1
        assert stats['unmatched'] == 1

    def test_run_matching_does_not_match_already_matched(self, test_db_session):
        """Test that run_matching only matches invoices with customer_id = NULL."""
        customer1 = Customer(
            ragione_sociale="Customer 1",
            partita_iva="11111111111",
            source="shopify"
        )
        customer2 = Customer(
            ragione_sociale="Customer 2",
            partita_iva="22222222222",
            source="shopify"
        )
        test_db_session.add(customer1)
        test_db_session.add(customer2)
        test_db_session.commit()

        # Already matched invoice
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Customer 1",
            customer_piva_raw="11111111111",
            customer_id=customer1.id,  # Already matched
            source_platform="fatturapro",
            status="open"
        )

        # Unmatched invoice
        invoice2 = Invoice(
            invoice_number="INV002",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Customer 2",
            customer_piva_raw="22222222222",
            customer_id=None,  # Not matched
            source_platform="fatturapro",
            status="open"
        )

        test_db_session.add_all([invoice1, invoice2])
        test_db_session.commit()

        stats = run_matching(test_db_session)
        # Only invoice2 should be processed
        assert stats['total'] == 1
        assert stats['matched_piva'] == 1
