import unittest
from datetime import date
from decimal import Decimal
from itertools import count

import app as subscription_app


class SubscriptionAppTests(unittest.TestCase):
    def setUp(self):
        subscription_app.subscriptions.clear()
        subscription_app.subscription_ids = count(1)
        subscription_app.app.config["TESTING"] = True
        self.client = subscription_app.app.test_client()

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

    def test_create_subscription_and_record_payment(self):
        response = self.client.post("/subscriptions/new", data=self.subscription_form())
        self.assertEqual(response.status_code, 302)
        subscription = subscription_app.subscriptions[0]
        self.assertEqual(subscription.amount, Decimal("9.99"))

        response = self.client.post(
            f"/subscriptions/{subscription.id}/payments",
            data={"amount": "10.49", "paid_on": "2026-02-28", "note": "Renewed"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(subscription.payments), 1)
        self.assertEqual(subscription.payments[0].amount, Decimal("10.49"))
        self.assertEqual(subscription.payments[0].currency, "USD")
        self.assertEqual(subscription.next_due, date(2026, 3, 28))
        detail = self.client.get(f"/subscriptions/{subscription.id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Payment history", detail.data)
        self.assertIn(b"Renewed", detail.data)

    def test_edit_and_cancel_subscription(self):
        self.client.post("/subscriptions/new", data=self.subscription_form())
        subscription = subscription_app.subscriptions[0]

        response = self.client.post(
            f"/subscriptions/{subscription.id}/edit",
            data=self.subscription_form(name="Updated plan", frequency="yearly"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(subscription.name, "Updated plan")
        self.assertEqual(subscription.frequency, "yearly")

        response = self.client.post(f"/subscriptions/{subscription.id}/status")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(subscription.active)

    def test_month_end_renewal_is_clamped_to_last_day(self):
        self.assertEqual(subscription_app.advance_due_date(date(2026, 1, 31), "monthly"), date(2026, 2, 28))
        self.assertEqual(subscription_app.advance_due_date(date(2024, 2, 29), "yearly"), date(2025, 2, 28))

    def test_invalid_subscription_is_not_saved(self):
        response = self.client.post(
            "/subscriptions/new",
            data=self.subscription_form(amount="-1", currency="US"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(subscription_app.subscriptions, [])
        self.assertIn(b"Enter an amount greater than zero.", response.data)


if __name__ == "__main__":
    unittest.main()
