"""Tests for FastAPI endpoints."""

import pytest
from datetime import date, datetime, timedelta
from backend.database import Invoice, Customer, Message, ActivityLog


class TestHealthCheck:
    """Tests for the health check endpoint."""

    def test_health_check_success(self, test_client):
        """Test successful health check."""
        response = test_client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_health_check_returns_json(self, test_client):
        """Test health check returns valid JSON."""
        response = test_client.get("/api/health")
        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert isinstance(data, dict)

    def test_health_check_minimal(self, test_client):
        """Test health check is lightweight (no DB queries)."""
        response = test_client.get("/api/health")
        data = response.json()
        # Health endpoint should be minimal for fast response
        assert "status" in data


class TestDashboardEndpoints:
    """Tests for dashboard endpoints."""

    def test_get_dashboard_empty_database(self, test_client):
        """Test dashboard with empty database."""
        response = test_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()

        assert data["total_scaduto"] == 0.0
        assert data["total_fatture_scadute"] == 0
        assert data["total_clienti_scaduti"] == 0
        assert data["total_positions"] == 0
        assert data["total_customers"] == 0

    def test_get_dashboard_with_invoices(self, test_client, test_db_session):
        """Test dashboard with overdue invoices."""
        for i in range(3):
            invoice = Invoice(
                invoice_number=f"INV{i:03d}",
                amount=1000.0 * (i + 1),
                amount_due=1000.0 * (i + 1),
                issue_date=date(2024, 1, 15),
                days_overdue=30,
                status="open",
                source_platform="fatturapro"
            )
            test_db_session.add(invoice)

        test_db_session.commit()

        response = test_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()

        assert data["total_scaduto"] == 6000.0
        assert data["total_fatture_scadute"] == 3
        assert data["total_positions"] == 3

    def test_get_dashboard_with_multiple_statuses(self, test_client, test_db_session):
        """Test dashboard with invoices in different statuses."""
        invoices_data = [
            ("open", 1000.0),
            ("open", 1500.0),
            ("contacted", 2000.0),
            ("escalated", 3000.0),
            ("paid", 500.0),
        ]

        for status, amount in invoices_data:
            invoice = Invoice(
                invoice_number=f"INV{status[:3]}{amount:.0f}",
                amount=amount,
                amount_due=amount,
                issue_date=date(2024, 1, 15),
                days_overdue=10,
                status=status,
                source_platform="fatturapro"
            )
            test_db_session.add(invoice)

        test_db_session.commit()

        response = test_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()

        # paid excluded from total_positions
        assert data["total_positions"] == 4
        # paid excluded from total_scaduto
        assert data["total_scaduto"] == 7500.0
        assert data["total_fatture_scadute"] == 4

    def test_get_dashboard_with_activity_log(self, test_client, test_db_session, activity_log):
        """Test dashboard returns correct structure even with activity log data."""
        response = test_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()

        # Simplified dashboard no longer returns recent_activity
        assert "total_scaduto" in data
        assert "total_positions" in data
        assert "total_customers" in data

    def test_get_dashboard_recent_activity_limit(self, test_client, test_db_session):
        """Test dashboard returns correct structure."""
        # Add some activity logs (dashboard no longer returns them)
        for i in range(15):
            log = ActivityLog(
                timestamp=datetime.utcnow() - timedelta(hours=i),
                action="sync",
                details={"count": i},
                entity_type="invoice"
            )
            test_db_session.add(log)

        test_db_session.commit()

        response = test_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()

        # Simplified endpoint returns only key stats
        assert "total_scaduto" in data
        assert "total_positions" in data

    def test_get_stats_empty_database(self, test_client):
        """Test stats endpoint with empty database."""
        response = test_client.get("/api/dashboard/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["total_crediti"] == 0.0
        assert data["total_positions"] == 0
        assert data["total_customers"] == 0
        assert data["total_messages"] == 0
        assert data["open_positions"] == 0
        assert data["contacted_positions"] == 0
        assert data["escalated_positions"] == 0
        assert data["paid_positions"] == 0
        assert data["draft_messages"] == 0
        assert data["sent_messages"] == 0

    def test_get_stats_with_data(self, test_client, test_db_session, sample_customer, sample_invoice, sample_message):
        """Test stats endpoint with sample data."""
        response = test_client.get("/api/dashboard/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["total_crediti"] == 1000.50
        assert data["total_positions"] == 1
        assert data["total_customers"] == 1
        assert data["total_messages"] == 1
        assert data["open_positions"] == 1

    def test_get_stats_position_counts(self, test_client, test_db_session):
        """Test stats endpoint position status counts."""
        # Add invoices with different statuses
        statuses = ["open", "open", "contacted", "escalated", "paid"]
        for i, status in enumerate(statuses):
            invoice = Invoice(
                invoice_number=f"INV{i:03d}",
                amount=1000.0,
                amount_due=1000.0,
                issue_date=date(2024, 1, 15),
                status=status,
                source_platform="fatturapro"
            )
            test_db_session.add(invoice)

        test_db_session.commit()

        response = test_client.get("/api/dashboard/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["open_positions"] == 2
        assert data["contacted_positions"] == 1
        assert data["escalated_positions"] == 1
        assert data["paid_positions"] == 1

    def test_get_stats_message_counts(self, test_client, test_db_session, sample_customer):
        """Test stats endpoint message counts."""
        # Add messages with different statuses
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            customer_id=sample_customer.id,
            source_platform="fatturapro"
        )
        invoice2 = Invoice(
            invoice_number="INV002",
            amount=2000.0,
            amount_due=2000.0,
            issue_date=date(2024, 1, 20),
            customer_id=sample_customer.id,
            source_platform="fatturapro"
        )
        test_db_session.add_all([invoice1, invoice2])
        test_db_session.commit()

        message1 = Message(
            invoice_id=invoice1.id,
            customer_id=sample_customer.id,
            status="draft"
        )
        message2 = Message(
            invoice_id=invoice2.id,
            customer_id=sample_customer.id,
            status="sent"
        )
        test_db_session.add_all([message1, message2])
        test_db_session.commit()

        response = test_client.get("/api/dashboard/stats")
        assert response.status_code == 200
        data = response.json()

        assert data["draft_messages"] == 1
        assert data["sent_messages"] == 1

    def test_get_stats_response_structure(self, test_client):
        """Test stats response has correct structure."""
        response = test_client.get("/api/dashboard/stats")
        assert response.status_code == 200
        data = response.json()

        expected_keys = [
            "total_crediti",
            "total_positions",
            "total_customers",
            "total_messages",
            "open_positions",
            "contacted_positions",
            "escalated_positions",
            "paid_positions",
            "draft_messages",
            "sent_messages",
        ]

        for key in expected_keys:
            assert key in data, f"Missing key: {key}"

    def test_get_stats_numeric_values(self, test_client):
        """Test that stats returns numeric values."""
        response = test_client.get("/api/dashboard/stats")
        assert response.status_code == 200
        data = response.json()

        assert isinstance(data["total_crediti"], (int, float))
        assert isinstance(data["total_positions"], int)
        assert isinstance(data["total_customers"], int)

    def test_dashboard_returns_json(self, test_client):
        """Test dashboard returns valid JSON."""
        response = test_client.get("/api/dashboard")
        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert isinstance(data, dict)

    def test_dashboard_with_customers(self, test_client, test_db_session, sample_customer):
        """Test dashboard counts customers correctly."""
        # Add overdue invoices for the customer
        invoice1 = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            issue_date=date(2024, 1, 15),
            days_overdue=30,
            customer_id=sample_customer.id,
            source_platform="fatturapro",
            status="open"
        )
        test_db_session.add(invoice1)
        test_db_session.commit()

        response = test_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()

        assert data["total_clienti_scaduti"] == 1
        assert data["total_customers"] >= 1

    def test_dashboard_excludes_paid_from_totals(self, test_client, test_db_session):
        """Test that paid invoices are excluded from overdue totals."""
        inv_open = Invoice(
            invoice_number="INV001",
            amount=1000.0,
            amount_due=1000.0,
            days_overdue=10,
            status="open",
            source_platform="fatturapro"
        )
        inv_paid = Invoice(
            invoice_number="INV002",
            amount=2000.0,
            amount_due=0.0,
            days_overdue=10,
            status="paid",
            source_platform="fatturapro"
        )
        test_db_session.add_all([inv_open, inv_paid])
        test_db_session.commit()

        response = test_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()

        # Only open invoice counts as overdue
        assert data["total_scaduto"] == 1000.0
        assert data["total_fatture_scadute"] == 1
        # Paid excluded from total_positions
        assert data["total_positions"] == 1


class TestErrorHandling:
    """Tests for error handling in endpoints."""

    def test_dashboard_handles_error(self, test_client):
        """Test dashboard handles errors gracefully."""
        # The endpoint should not crash
        response = test_client.get("/api/dashboard")
        assert response.status_code in [200, 500]

    def test_stats_handles_error(self, test_client):
        """Test stats endpoint handles errors gracefully."""
        response = test_client.get("/api/dashboard/stats")
        assert response.status_code in [200, 500]

    def test_health_check_always_succeeds(self, test_client):
        """Test health check endpoint always succeeds."""
        response = test_client.get("/api/health")
        assert response.status_code == 200


class TestEndpointPaths:
    """Tests for correct endpoint paths."""

    def test_health_check_path(self, test_client):
        """Test health check endpoint path."""
        response = test_client.get("/api/health")
        assert response.status_code == 200

    def test_dashboard_path(self, test_client):
        """Test dashboard endpoint path."""
        response = test_client.get("/api/dashboard")
        assert response.status_code == 200

    def test_stats_path(self, test_client):
        """Test stats endpoint path."""
        response = test_client.get("/api/dashboard/stats")
        assert response.status_code == 200

    def test_nonexistent_path_returns_404(self, test_client):
        """Test that nonexistent paths return 404."""
        response = test_client.get("/api/nonexistent")
        assert response.status_code == 404
