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

## SQLite storage

The app uses Python's built-in `sqlite3` module, so no additional database package or SQLite VS Code extension is needed. On the first page request, it creates `subscriptions.db` in the project folder and creates the `subscriptions` and `payments` tables if they do not already exist. Subscription and payment data remains in that file when the Flask server stops.

The database file is excluded from Git so personal records are not committed. To inspect it visually, you can optionally install a SQLite viewer extension in VS Code; the app itself does not require one. This local prototype does not include user accounts or production security.

## Tests

Run the focused application tests with:

```powershell
python -m unittest -v test_app
```
