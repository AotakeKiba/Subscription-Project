import os
import re
import secrets
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import count

from flask import Flask, abort, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)


@dataclass
class Payment:
    amount: Decimal
    currency: str
    paid_on: date
    note: str = ""


@dataclass
class Subscription:
    id: int
    name: str
    amount: Decimal
    currency: str
    frequency: str
    next_due: date
    start_date: date
    category: str = ""
    notes: str = ""
    active: bool = True
    payments: list[Payment] = field(default_factory=list)


# Temporary storage for this prototype; data is lost when the server stops.
subscriptions: list[Subscription] = []
subscription_ids = count(1)


def get_subscription(subscription_id: int) -> Subscription:
    for subscription in subscriptions:
        if subscription.id == subscription_id:
            return subscription
    abort(404)


def shift_month(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def advance_due_date(value: date, frequency: str) -> date:
    if frequency == "monthly":
        return shift_month(value, 1)
    next_year = value.year + 1
    return date(next_year, value.month, min(value.day, monthrange(next_year, value.month)[1]))


def parse_subscription_form():
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip()
    notes = request.form.get("notes", "").strip()
    currency = request.form.get("currency", "").strip().upper()
    frequency = request.form.get("frequency", "")

    try:
        amount = Decimal(request.form.get("amount", "").strip())
    except (InvalidOperation, AttributeError):
        amount = Decimal("0")

    try:
        start_date = date.fromisoformat(request.form.get("start_date", ""))
        next_due = date.fromisoformat(request.form.get("next_due", ""))
    except ValueError:
        start_date = None
        next_due = None

    errors = []
    if not name:
        errors.append("Enter a subscription name.")
    if not amount.is_finite() or amount <= 0:
        errors.append("Enter an amount greater than zero.")
    if re.fullmatch(r"[A-Z]{3}", currency) is None:
        errors.append("Enter a three-letter currency code, such as IDR or USD.")
    if frequency not in {"monthly", "yearly"}:
        errors.append("Choose a monthly or yearly billing frequency.")
    if start_date is None or next_due is None:
        errors.append("Enter valid start and next renewal dates.")

    return {
        "name": name,
        "category": category,
        "notes": notes,
        "amount": amount,
        "currency": currency,
        "frequency": frequency,
        "start_date": start_date,
        "next_due": next_due,
    }, errors


def dashboard_totals(today: date):
    monthly_by_currency: dict[str, Decimal] = {}
    upcoming = []
    for subscription in subscriptions:
        if not subscription.active:
            continue
        monthly_amount = subscription.amount
        if subscription.frequency == "yearly":
            monthly_amount /= Decimal("12")
        monthly_by_currency[subscription.currency] = (
            monthly_by_currency.get(subscription.currency, Decimal("0")) + monthly_amount
        )
        if (subscription.next_due - today).days <= 30:
            upcoming.append(subscription)

    upcoming.sort(key=lambda item: item.next_due)
    return monthly_by_currency, upcoming


@app.get("/")
def home():
    today = date.today()
    monthly_by_currency, upcoming = dashboard_totals(today)
    return render_template(
        "index.html",
        subscriptions=sorted(subscriptions, key=lambda item: (not item.active, item.next_due)),
        active_count=sum(item.active for item in subscriptions),
        monthly_totals=monthly_by_currency,
        upcoming=upcoming[:5],
        today=today,
    )


@app.route("/subscriptions/new", methods=["GET", "POST"])
def new_subscription():
    if request.method == "POST":
        values, errors = parse_subscription_form()
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("subscription_form.html", subscription=None, values=request.form), 400

        subscriptions.append(Subscription(id=next(subscription_ids), **values))
        flash(f"{values['name']} was added.", "success")
        return redirect(url_for("home"))

    return render_template("subscription_form.html", subscription=None, values={})


@app.route("/subscriptions/<int:subscription_id>/edit", methods=["GET", "POST"])
def edit_subscription(subscription_id: int):
    subscription = get_subscription(subscription_id)
    if request.method == "POST":
        values, errors = parse_subscription_form()
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template(
                "subscription_form.html", subscription=subscription, values=request.form
            ), 400

        for key, value in values.items():
            setattr(subscription, key, value)
        flash(f"{subscription.name} was updated.", "success")
        return redirect(url_for("subscription_detail", subscription_id=subscription.id))

    return render_template("subscription_form.html", subscription=subscription, values={})


@app.get("/subscriptions/<int:subscription_id>")
def subscription_detail(subscription_id: int):
    subscription = get_subscription(subscription_id)
    return render_template(
        "subscription_detail.html",
        subscription=subscription,
        payments=sorted(subscription.payments, key=lambda payment: payment.paid_on, reverse=True),
        today=date.today(),
    )


@app.post("/subscriptions/<int:subscription_id>/payments")
def add_payment(subscription_id: int):
    subscription = get_subscription(subscription_id)
    try:
        amount = Decimal(request.form.get("amount", "").strip())
        paid_on = date.fromisoformat(request.form.get("paid_on", ""))
    except (InvalidOperation, ValueError, AttributeError):
        flash("Enter a valid payment amount and date.", "error")
        return redirect(url_for("subscription_detail", subscription_id=subscription.id))

    if not amount.is_finite() or amount <= 0:
        flash("Payment amount must be greater than zero.", "error")
        return redirect(url_for("subscription_detail", subscription_id=subscription.id))

    payment = Payment(
        amount=amount,
        currency=subscription.currency,
        paid_on=paid_on,
        note=request.form.get("note", "").strip(),
    )
    subscription.payments.append(payment)
    subscription.next_due = advance_due_date(subscription.next_due, subscription.frequency)
    flash("Payment recorded and next renewal date advanced.", "success")
    return redirect(url_for("subscription_detail", subscription_id=subscription.id))


@app.post("/subscriptions/<int:subscription_id>/status")
def toggle_subscription_status(subscription_id: int):
    subscription = get_subscription(subscription_id)
    subscription.active = not subscription.active
    message = "Subscription reactivated." if subscription.active else "Subscription cancelled."
    flash(message, "success")
    return redirect(url_for("subscription_detail", subscription_id=subscription.id))


@app.get("/api/health")
def health():
    return {"status": "ok"}
