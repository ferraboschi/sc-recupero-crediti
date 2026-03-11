"""Tests for the deduplicator module."""

import pytest
from datetime import date, datetime
from backend.engine.deduplicator import deduplicate_invoices, find_duplicates
from backend.database import Invoice


class TestDeduplicateInvoices:
    """Tests for the deduplicate_invoices function."""

    def test_no_duplicates(self, test_db_session):
        """Test with list of unique invoices."""
        invoices = [
            Invoice(
                invoice_number="INV001",
                amount=1000.0,
                amount_due=1000.0,
                issue_date=date(2024, 1, 15),
                customer_name_raw="Company A",
                source_platform="fatturapro",
                status="open"
            ),
            Invoice(
                invoice_number="INV002",
                amount=2000.0,
                amount_due=2000.0,
                issue_date=date(2024, 1, 20),
                customer_name_raw="Company B",
                source_platform="fatturapro",
                status="open"
            ),
        ]

        result = deduplicate_invoices(invoices)
        assert len(result) == 2
        assert result[0].invoice_number == "INV001"
        assert result[1].invoice_number == "INV002"

    def test_empty_list(self):
        """Test with empty invoice list."""
        result = deduplicate_invoices([])
        assert result == []

    def test_duplicate_by_invoice_number(self, test_db_session):
        """Test removing duplicates by invoice number."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV001",  # Same invoice number
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2])
        assert len(result) == 1

    def test_duplicate_prefers_fatturapro(self, test_db_session):
        """Test that FatturaPro is preferred over Fattura24."""
        invoice_fattura24 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )
        invoice_fatturapro = Invoice(
            invoice_number="INV001",  # Same invoice number
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )

        result = deduplicate_invoices([invoice_fattura24, invoice_fatturapro])
        assert len(result) == 1
        assert result[0].source_platform == "fatturapro"

    def test_duplicate_prefers_fatturapro_order(self, test_db_session):
        """Test that FatturaPro is preferred regardless of order."""
        invoice_fatturapro = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice_fattura24 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice_fatturapro, invoice_fattura24])
        assert len(result) == 1
        assert result[0].source_platform == "fatturapro"

    def test_duplicate_by_composite_key(self, test_db_session):
        """Test removing duplicates by composite key (name, amount, date)."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV999",  # Different invoice number
            amount=1000.0,  # Same amount
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),  # Same date
            customer_name_raw="Company A",  # Same customer name
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2])
        assert len(result) == 1

    def test_composite_key_case_insensitive_name(self, test_db_session):
        """Test that composite key matching is case-insensitive for names."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV999",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="COMPANY A",  # Different case
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2])
        assert len(result) == 1

    def test_composite_key_amount_rounding(self, test_db_session):
        """Test that composite key amount is rounded to 2 decimals."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.00,
            amount_due=1000.00,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV999",
            amount=1000.001,  # Slightly different, but rounds to same
            amount_due=1000.001,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2])
        assert len(result) == 1

    def test_different_amount_not_duplicate(self, test_db_session):
        """Test that different amounts don't trigger composite match."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV002",
            amount=2000.0,  # Different amount
            amount_due=2000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2])
        assert len(result) == 2

    def test_different_date_not_duplicate(self, test_db_session):
        """Test that different dates don't trigger composite match."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV002",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 16),  # Different date
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2])
        assert len(result) == 2

    def test_missing_composite_key_data(self, test_db_session):
        """Test that invoices missing composite key data are not matched by composite."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV002",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=None,  # Missing date
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2])
        assert len(result) == 2

    def test_multiple_duplicates(self, test_db_session):
        """Test handling multiple sets of duplicates."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )
        invoice3 = Invoice(
            invoice_number="INV002",
            amount=2000.0,
            amount_due=2000.0,
            issue_date=date(2024, 1, 20),
            customer_name_raw="Company B",
            source_platform="fatturapro",
            status="open"
        )
        invoice4 = Invoice(
            invoice_number="INV002",
            amount=2000.0,
            amount_due=2000.0,
            issue_date=date(2024, 1, 20),
            customer_name_raw="Company B",
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2, invoice3, invoice4])
        assert len(result) == 2

    def test_invoice_number_priority_over_composite(self, test_db_session):
        """Test that invoice_number duplicate is found before composite."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV001",  # Same invoice number
            amount=2000.0,  # Different amount
            amount_due=2000.0,
            issue_date=date(2024, 1, 20),  # Different date
            customer_name_raw="Company B",  # Different name
            source_platform="fattura24",
            status="open"
        )

        result = deduplicate_invoices([invoice1, invoice2])
        assert len(result) == 1

    def test_returns_list(self):
        """Test that function returns a list."""
        result = deduplicate_invoices([])
        assert isinstance(result, list)


class TestFindDuplicates:
    """Tests for the find_duplicates function."""

    def test_find_duplicates_none(self, test_db_session):
        """Test finding duplicates with no duplicates in database."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV002",
            amount=2000.0,
            amount_due=2000.0,
            issue_date=date(2024, 1, 20),
            customer_name_raw="Company B",
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add_all([invoice1, invoice2])
        test_db_session.commit()

        result = find_duplicates(test_db_session)
        assert result == []

    def test_find_duplicates_by_invoice_number(self, test_db_session):
        """Test finding duplicates by invoice number."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )
        test_db_session.add_all([invoice1, invoice2])
        test_db_session.commit()

        result = find_duplicates(test_db_session)
        assert len(result) == 1
        assert result[0]['duplicate_count'] == 1
        assert len(result[0]['invoice_numbers']) == 2

    def test_find_duplicates_by_composite_key(self, test_db_session):
        """Test finding duplicates by composite key."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV999",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )
        test_db_session.add_all([invoice1, invoice2])
        test_db_session.commit()

        result = find_duplicates(test_db_session)
        assert len(result) == 1
        assert result[0]['duplicate_count'] == 1

    def test_find_duplicates_prefers_fatturapro(self, test_db_session):
        """Test that preferred invoice is FatturaPro."""
        invoice_fattura24 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )
        invoice_fatturapro = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add_all([invoice_fattura24, invoice_fatturapro])
        test_db_session.commit()

        result = find_duplicates(test_db_session)
        assert len(result) == 1
        assert result[0]['preferred'].source_platform == "fatturapro"

    def test_find_duplicates_multiple_groups(self, test_db_session):
        """Test finding multiple duplicate groups."""
        # First group
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )

        # Second group
        invoice3 = Invoice(
            invoice_number="INV002",
            amount=2000.0,
            amount_due=2000.0,
            issue_date=date(2024, 1, 20),
            customer_name_raw="Company B",
            source_platform="fatturapro",
            status="open"
        )
        invoice4 = Invoice(
            invoice_number="INV002",
            amount=2000.0,
            amount_due=2000.0,
            issue_date=date(2024, 1, 20),
            customer_name_raw="Company B",
            source_platform="fattura24",
            status="open"
        )

        # Unique invoice
        invoice5 = Invoice(
            invoice_number="INV003",
            amount=3000.0,
            amount_due=3000.0,
            issue_date=date(2024, 1, 25),
            customer_name_raw="Company C",
            source_platform="fatturapro",
            status="open"
        )

        test_db_session.add_all([invoice1, invoice2, invoice3, invoice4, invoice5])
        test_db_session.commit()

        result = find_duplicates(test_db_session)
        assert len(result) == 2

    def test_find_duplicates_group_structure(self, test_db_session):
        """Test duplicate group has correct structure."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )
        test_db_session.add_all([invoice1, invoice2])
        test_db_session.commit()

        result = find_duplicates(test_db_session)
        assert len(result) == 1

        group = result[0]
        assert 'invoice_numbers' in group
        assert 'customer_names' in group
        assert 'duplicate_count' in group
        assert 'preferred' in group
        assert 'duplicates' in group
        assert isinstance(group['invoice_numbers'], list)
        assert isinstance(group['customer_names'], list)
        assert isinstance(group['duplicate_count'], int)
        assert isinstance(group['duplicates'], list)

    def test_find_duplicates_empty_database(self, test_db_session):
        """Test finding duplicates in empty database."""
        result = find_duplicates(test_db_session)
        assert result == []

    def test_find_duplicates_returns_list(self, test_db_session):
        """Test that function returns a list."""
        result = find_duplicates(test_db_session)
        assert isinstance(result, list)

    def test_find_duplicates_three_way_duplicate(self, test_db_session):
        """Test finding a group with 3 duplicates."""
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fatturapro",
            status="open"
        )
        invoice2 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )
        invoice3 = Invoice(
            invoice_number="INV999",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_name_raw="Company A",
            source_platform="fattura24",
            status="open"
        )
        test_db_session.add_all([invoice1, invoice2, invoice3])
        test_db_session.commit()

        result = find_duplicates(test_db_session)
        assert len(result) == 1
        assert result[0]['duplicate_count'] == 2
        assert len(result[0]['invoice_numbers']) == 3
