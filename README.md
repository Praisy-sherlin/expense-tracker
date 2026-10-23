# 💰 Expense Tracker

A Python + SQLite app that turns a bank statement (CSV) into spending insights on a Streamlit dashboard. Built to practise real-world SQL and DBMS concepts.

**Live demo:** _add your Streamlit link here_

> After opening the app, upload `data/sample.csv` from the sidebar to see the dashboard.

## Design
A Material 3 dark interface built on Streamlit with custom CSS: tonal surfaces, rounded cards, an animated spending ring, a step-by-step setup guide in the sidebar, and five tabs (Overview, Insights, Transactions, Manage, Database lab). Charts use Plotly.

## Features
- Upload bank statement CSV with all-or-nothing import (validation + ROLLBACK on any bad row)
- Rule-based auto-categorization, rules stored in the database and editable from the UI
- Monthly budgets with alerts
- Month-over-month comparison, running total, top 5 expenses
- Anomaly detection (expenses far above their category average)
- Search and date-range filter
- Audit log filled automatically by database triggers
- Built-in index performance test (100,000 rows, before/after `EXPLAIN QUERY PLAN`)

## SQL / DBMS concepts used
| Concept | Where |
|---|---|
| Normalization, Primary/Foreign keys | `categories`, `category_rules`, `transactions`, `budgets` |
| JOIN (2 and 3 tables) | budget alerts, anomaly detection, category rules |
| GROUP BY, HAVING, aggregates | budget alerts, monthly totals |
| Window functions | `LAG()` month-over-month, `SUM() OVER` running total |
| Subqueries | anomaly detection (`AVG` per category) |
| Views | `transaction_details`, `monthly_summary` |
| Triggers | `audit_log` (INSERT, DELETE, RECATEGORIZE) |
| Indexes | `(category_id, date)`, about 11x faster on 100k rows |
| Transactions (ACID) | CSV import commits or rolls back as one unit |
| Constraints | `UNIQUE`, `NOT NULL`, `FOREIGN KEY`, `INSERT OR IGNORE` |
| Parameterized queries | search and filters (SQL injection safe) |

## ER diagram
```
categories (id PK, name)
   ├── category_rules (id PK, keyword, category_id FK)
   ├── transactions   (id PK, date, description, amount, category_id FK)
   └── budgets        (category_id PK/FK, monthly_limit)

audit_log (id PK, action, tx_id, description, amount, details, logged_at)
```

## Tech stack
Python, SQLite, Pandas, Streamlit, Plotly

## Project structure
```
expense-tracker
├── app.py              # Streamlit app + all database logic
├── requirements.txt
├── .streamlit
│   └── config.toml     # dark theme
├── data
│   ├── sample.csv      # demo data (Sep-Oct 2026)
│   ├── test.csv        # extra rows to test uploads
│   └── bad_test.csv    # has an invalid row, shows ROLLBACK
└── README.md
```
The database (`expenses.db`) is created automatically on first run.

## Run locally
```
pip install -r requirements.txt
python -m streamlit run app.py
```

## CSV format
```
date,description,amount
2026-10-01,Zomato Order,300
```
Dates use `YYYY-MM-DD`; amounts must be greater than 0.
