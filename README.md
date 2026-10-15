# Subtrack

A small Flask app for tracking recurring subscriptions, renewal dates, and payment history.

## Run locally

From the project folder, create and activate a virtual environment, then install the project dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m flask --app app run --debug
```

Open `http://127.0.0.1:5000` in your browser.

## Prototype storage

This first version keeps subscriptions and payments in memory. The data is lost when the Flask server stops or restarts. It is intended for local prototyping and does not include user accounts or production security. SQLite persistence can be added later.

## Tests

Run the focused application tests with:

```powershell
python -m unittest -v test_app
```
