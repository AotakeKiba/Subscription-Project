import os
import re
import secrets
import sqlite3
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["DATABASE"] = str(Path(__file__).resolve().with_name("subscriptions.db"))


def format_amount(amount: Decimal) -> str:
    return f"{amount:,.2f}"


app.add_template_filter(format_amount, "amount")


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


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        database_path = Path(app.config["DATABASE"])
        database_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(database_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g.db = db
    return g.db


def init_db() -> None:
    get_db().executescript(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL,
            frequency TEXT NOT NULL CHECK (frequency IN ('monthly', 'yearly')),
            next_due TEXT NOT NULL,
            start_date TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY,
            subscription_id INTEGER NOT NULL
                REFERENCES subscriptions(id) ON DELETE CASCADE,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL,
            paid_on TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS payments_subscription_date
            ON payments (subscription_id, paid_on DESC);
        """
    )
    db = get_db()
    with db:
        payment_columns = {row["name"] for row in db.execute("PRAGMA table_info(payments)")}
        if "created_at" not in payment_columns:
            db.execute("ALTER TABLE payments ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        db.execute(
            """
            UPDATE payments
            SET created_at = paid_on || 'T00:00:00+00:00'
            WHERE created_at = ''
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS payments_subscription_created
                ON payments (subscription_id, created_at DESC, id DESC)
            """
        )


@app.before_request
def ensure_database() -> None:
    init_db()


@app.teardown_appcontext
def close_db(_error: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def subscription_from_row(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"],
        name=row["name"],
        amount=Decimal(row["amount"]),
        currency=row["currency"],
        frequency=row["frequency"],
        next_due=date.fromisoformat(row["next_due"]),
        start_date=date.fromisoformat(row["start_date"]),
        category=row["category"],
        notes=row["notes"],
        active=bool(row["active"]),
    )


def payment_from_row(row: sqlite3.Row) -> Payment:
    return Payment(
        amount=Decimal(row["amount"]),
        currency=row["currency"],
        paid_on=date.fromisoformat(row["paid_on"]),
        note=row["note"],
    )


def parse_amount(value: str) -> Decimal:
    value = value.strip()
    if re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value) is None:
        return Decimal("0")
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return Decimal("0")


def list_subscriptions() -> list[Subscription]:
    rows = get_db().execute(
        "SELECT * FROM subscriptions ORDER BY active DESC, next_due"
    ).fetchall()
    return [subscription_from_row(row) for row in rows]


def get_subscription(subscription_id: int) -> Subscription:
    row = get_db().execute(
        "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
    ).fetchone()
    if row is None:
        abort(404)
    return subscription_from_row(row)


def list_payments(subscription_id: int) -> list[Payment]:
    rows = get_db().execute(
        """
        SELECT * FROM payments
        WHERE subscription_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (subscription_id,),
    ).fetchall()
    return [payment_from_row(row) for row in rows]


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

    amount = parse_amount(request.form.get("amount", ""))

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


def dashboard_totals(today: date, subscriptions: list[Subscription]):
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
    subscriptions = list_subscriptions()
    monthly_by_currency, upcoming = dashboard_totals(today, subscriptions)
    return render_template(
        "index.html",
        subscriptions=subscriptions,
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

        with get_db():
            get_db().execute(
                """
                INSERT INTO subscriptions
                    (name, amount, currency, frequency, start_date, next_due, category, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["name"],
                    str(values["amount"]),
                    values["currency"],
                    values["frequency"],
                    values["start_date"].isoformat(),
                    values["next_due"].isoformat(),
                    values["category"],
                    values["notes"],
                ),
            )
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

        with get_db():
            get_db().execute(
                """
                UPDATE subscriptions
                SET name = ?, amount = ?, currency = ?, frequency = ?, start_date = ?,
                    next_due = ?, category = ?, notes = ?
                WHERE id = ?
                """,
                (
                    values["name"],
                    str(values["amount"]),
                    values["currency"],
                    values["frequency"],
                    values["start_date"].isoformat(),
                    values["next_due"].isoformat(),
                    values["category"],
                    values["notes"],
                    subscription_id,
                ),
            )
        flash(f"{values['name']} was updated.", "success")
        return redirect(url_for("subscription_detail", subscription_id=subscription_id))

    return render_template("subscription_form.html", subscription=subscription, values={})


@app.get("/subscriptions/<int:subscription_id>")
def subscription_detail(subscription_id: int):
    subscription = get_subscription(subscription_id)
    return render_template(
        "subscription_detail.html",
        subscription=subscription,
        payments=list_payments(subscription_id),
        today=date.today(),
    )


@app.post("/subscriptions/<int:subscription_id>/payments")
def add_payment(subscription_id: int):
    subscription = get_subscription(subscription_id)
    try:
        amount = parse_amount(request.form.get("amount", ""))
        paid_on = date.fromisoformat(request.form.get("paid_on", ""))
    except (ValueError, AttributeError):
        flash("Enter a valid payment amount and date.", "error")
        return redirect(url_for("subscription_detail", subscription_id=subscription.id))

    if not amount.is_finite() or amount <= 0:
        flash("Payment amount must be greater than zero.", "error")
        return redirect(url_for("subscription_detail", subscription_id=subscription.id))

    next_due = advance_due_date(subscription.next_due, subscription.frequency)
    with get_db():
        db = get_db()
        db.execute(
            """
            INSERT INTO payments
                (subscription_id, amount, currency, paid_on, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                subscription.id,
                str(amount),
                subscription.currency,
                paid_on.isoformat(),
                request.form.get("note", "").strip(),
                datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            ),
        )
        db.execute(
            "UPDATE subscriptions SET next_due = ? WHERE id = ?",
            (next_due.isoformat(), subscription.id),
        )
    flash("Payment recorded and next renewal date advanced.", "success")
    return redirect(url_for("subscription_detail", subscription_id=subscription.id))


@app.post("/subscriptions/<int:subscription_id>/status")
def toggle_subscription_status(subscription_id: int):
    subscription = get_subscription(subscription_id)
    active = not subscription.active
    with get_db():
        get_db().execute(
            "UPDATE subscriptions SET active = ? WHERE id = ?",
            (int(active), subscription_id),
        )
    message = "Subscription reactivated." if active else "Subscription cancelled."
    flash(message, "success")
    return redirect(url_for("subscription_detail", subscription_id=subscription_id))


@app.get("/api/health")
def health():
    return {"status": "ok"}
