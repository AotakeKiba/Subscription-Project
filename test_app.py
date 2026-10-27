import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import app as subscription_app


class SubscriptionAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test_subscriptions.db"
        subscription_app.app.config.update(
            TESTING=True,
            DATABASE=str(self.database_path),
        )
        self.client = subscription_app.app.test_client()

    def tearDown(self):
        self.client = None
        self.temp_dir.cleanup()

    def subscription_form(self, **overrides):
        values = {
            "name": "Example plan",
            "amount": "9.99",
            "currency": "USD",
            "frequency": "monthly",
            "start_date": "2026-01-31",
            "next_due": "2026-02-28",
            "category": "Entertainment",
            "notes": "",
        }
        values.update(overrides)
        return values

    def test_dashboard_and_health_endpoint_load(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your list is ready when you are", response.data)
        self.assertEqual(self.client.get("/api/health").json, {"status": "ok"})
        self.assertTrue(self.database_path.exists())

    def test_create_subscription_and_record_payment(self):
        response = self.client.post("/subscriptions/new", data=self.subscription_form())
        self.assertEqual(response.status_code, 302)
        with subscription_app.app.app_context():
            subscription = subscription_app.get_subscription(1)
        self.assertEqual(subscription.amount, Decimal("9.99"))

        response = self.client.post(
            f"/subscriptions/{subscription.id}/payments",
            data={"amount": "10.49", "paid_on": "2026-02-28", "note": "Renewed"},
        )
        self.assertEqual(response.status_code, 302)

        with subscription_app.app.app_context():
            subscription = subscription_app.get_subscription(subscription.id)
            payments = subscription_app.list_payments(subscription.id)
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].amount, Decimal("10.49"))
        self.assertEqual(payments[0].currency, "USD")
        self.assertEqual(subscription.next_due, date(2026, 3, 28))
        detail = self.client.get(f"/subscriptions/{subscription.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Payment history", detail.data)
        self.assertIn(b"Renewed", detail.data)

    def test_large_amounts_use_thousands_separators_in_views(self):
        response = self.client.post(
            "/subscriptions/new",
            data=self.subscription_form(
                amount="1,234,567.89",
                next_due=date.today().isoformat(),
            ),
        )
        self.assertEqual(response.status_code, 302)

        dashboard = self.client.get("/")
        self.assertIn(b"USD 1,234,567.89", dashboard.data)
        self.assertIn(b"1,234,567.89", dashboard.data)
        self.assertIn(date.today().strftime("%b %d, %Y").encode(), dashboard.data)
        edit_form = self.client.get("/subscriptions/1/edit")
        self.assertIn(b'value="1,234,567.89"', edit_form.data)

        self.client.post(
            "/subscriptions/1/payments",
            data={"amount": "7,654,321.98", "paid_on": "2026-02-28", "note": "Renewed"},
        )
        detail = self.client.get("/subscriptions/1")
        self.assertIn(b"USD 1,234,567.89", detail.data)
        self.assertIn(b"USD 7,654,321.98", detail.data)
        self.assertIn(b'value="1,234,567.89"', detail.data)
        self.assertIn(b"Jan 31, 2026", detail.data)

    def test_edit_and_cancel_subscription(self):
        self.client.post("/subscriptions/new", data=self.subscription_form())
        response = self.client.post(
            "/subscriptions/1/edit",
            data=self.subscription_form(name="Updated plan", frequency="yearly"),
        )
        self.assertEqual(response.status_code, 302)
        with subscription_app.app.app_context():
            subscription = subscription_app.get_subscription(1)
        self.assertEqual(subscription.name, "Updated plan")
        self.assertEqual(subscription.frequency, "yearly")

        response = self.client.post("/subscriptions/1/status")
        self.assertEqual(response.status_code, 302)
        with subscription_app.app.app_context():
            self.assertFalse(subscription_app.get_subscription(1).active)

    def test_month_end_renewal_is_clamped_to_last_day(self):
        self.assertEqual(subscription_app.advance_due_date(date(2026, 1, 31), "monthly"), date(2026, 2, 28))
        self.assertEqual(subscription_app.advance_due_date(date(2024, 2, 29), "yearly"), date(2025, 2, 28))

    def test_invalid_subscription_is_not_saved(self):
        response = self.client.post(
            "/subscriptions/new",
            data=self.subscription_form(amount="-1", currency="US"),
        )
        self.assertEqual(response.status_code, 400)
        with subscription_app.app.app_context():
            self.assertEqual(subscription_app.list_subscriptions(), [])
        self.assertIn(b"Enter an amount greater than zero.", response.data)

    def test_data_persists_between_app_requests(self):
        self.client.post("/subscriptions/new", data=self.subscription_form())

        db = sqlite3.connect(self.database_path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            row = db.execute(
                "SELECT name, amount FROM subscriptions WHERE id = 1"
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(row, ("Example plan", "9.99"))

        later_client = subscription_app.app.test_client()
        response = later_client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Example plan", response.data)

    def test_payment_and_renewal_update_are_recorded_together(self):
        self.client.post("/subscriptions/new", data=self.subscription_form())
        self.client.post(
            "/subscriptions/1/payments",
            data={"amount": "9.99", "paid_on": "2026-02-28", "note": "Renewed"},
        )

        db = sqlite3.connect(self.database_path)
        try:
            row = db.execute(
                "SELECT next_due FROM subscriptions WHERE id = 1"
            ).fetchone()
            payment_count = db.execute(
                "SELECT COUNT(*) FROM payments WHERE subscription_id = 1"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(row, ("2026-03-28",))
        self.assertEqual(payment_count, 1)

    def test_payment_history_is_sorted_by_creation_not_payment_date(self):
        self.client.post("/subscriptions/new", data=self.subscription_form())
        self.client.post(
            "/subscriptions/1/payments",
            data={"amount": "9.99", "paid_on": "2026-12-01", "note": "Recorded first"},
        )
        self.client.post(
            "/subscriptions/1/payments",
            data={"amount": "9.99", "paid_on": "2020-01-01", "note": "Recorded second"},
        )

        response = self.client.get("/subscriptions/1")
        self.assertEqual(response.status_code, 200)
        self.assertLess(
            response.data.index(b"Recorded second"),
            response.data.index(b"Recorded first"),
        )

    def test_existing_database_is_migrated_with_legacy_payment_history(self):
        db = sqlite3.connect(self.database_path)
        try:
            db.executescript(
                """
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    frequency TEXT NOT NULL,
                    next_due TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE payments (
                    id INTEGER PRIMARY KEY,
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    paid_on TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO subscriptions
                    (id, name, amount, currency, frequency, next_due, start_date)
                VALUES (1, 'Existing plan', '9.99', 'USD', 'monthly', '2026-11-01', '2026-01-01');
                INSERT INTO payments
                    (id, subscription_id, amount, currency, paid_on, note)
                VALUES (1, 1, '9.99', 'USD', '2026-10-01', 'Legacy payment');
                """
            )
        finally:
            db.close()

        response = self.client.get("/subscriptions/1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Legacy payment", response.data)

        db = sqlite3.connect(self.database_path)
        try:
            created_at = db.execute(
                "SELECT created_at FROM payments WHERE id = 1"
            ).fetchone()[0]
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(payments)")
            }
        finally:
            db.close()
        self.assertEqual(created_at, "2026-10-01T00:00:00+00:00")
        self.assertIn("created_at", columns)


if __name__ == "__main__":
    unittest.main()
