import csv
import html as _html
import inspect
import io
import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from time import perf_counter

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_FILE = "expenses.db"

st.set_page_config(
    page_title="Expense Tracker",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="auto",
)

# =====================================================================
# Database helpers
# =====================================================================
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")  # enforce foreign keys
    return conn


def column_names(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")]


def init_db():
    conn = get_conn()

    # Views / triggers depend on tables, so drop first and recreate at the end
    conn.execute("DROP VIEW IF EXISTS transaction_details")
    conn.execute("DROP VIEW IF EXISTS monthly_summary")

    # 1. Lookup tables (normalization)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS category_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL UNIQUE,
        category_id INTEGER NOT NULL,
        FOREIGN KEY (category_id) REFERENCES categories(id)
    )
    """)

    for name in ["Food", "Travel", "Bills", "Others"]:
        conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))

    default_rules = [
        ("swiggy", "Food"), ("zomato", "Food"),
        ("uber", "Travel"), ("bus", "Travel"), ("petrol", "Travel"),
        ("bill", "Bills"), ("recharge", "Bills"),
    ]
    for keyword, cat in default_rules:
        conn.execute(
            "INSERT OR IGNORE INTO category_rules (keyword, category_id) "
            "SELECT ?, id FROM categories WHERE name = ?",
            (keyword, cat)
        )

    # 2. Migrate old schema (category TEXT) -> new schema (category_id FK)
    old_tx = "category" in column_names(conn, "transactions")
    old_budget = "category" in column_names(conn, "budgets")
    if old_tx:
        conn.execute("ALTER TABLE transactions RENAME TO transactions_old")
    if old_budget:
        conn.execute("ALTER TABLE budgets RENAME TO budgets_old")

    # 3. Main tables
    conn.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        description TEXT,
        amount REAL,
        category_id INTEGER NOT NULL,
        UNIQUE(date, description, amount),
        FOREIGN KEY (category_id) REFERENCES categories(id)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        category_id INTEGER PRIMARY KEY,
        monthly_limit REAL,
        FOREIGN KEY (category_id) REFERENCES categories(id)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT,
        tx_id INTEGER,
        description TEXT,
        amount REAL,
        details TEXT,
        logged_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """)

    if old_tx:
        conn.execute("""
        INSERT OR IGNORE INTO transactions (date, description, amount, category_id)
        SELECT o.date, o.description, o.amount,
               COALESCE(c.id, (SELECT id FROM categories WHERE name = 'Others'))
        FROM transactions_old o
        LEFT JOIN categories c ON c.name = o.category
        """)
        conn.execute("DROP TABLE transactions_old")
    if old_budget:
        conn.execute("""
        INSERT OR IGNORE INTO budgets (category_id, monthly_limit)
        SELECT c.id, o.monthly_limit
        FROM budgets_old o
        JOIN categories c ON c.name = o.category
        """)
        conn.execute("DROP TABLE budgets_old")

    # 4. Indexes (speed up category + date lookups)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tx_category_date ON transactions(category_id, date)"
    )

    # 5. Views
    conn.execute("""
    CREATE VIEW transaction_details AS
    SELECT t.id, t.date, t.description, t.amount, c.name AS category
    FROM transactions t
    JOIN categories c ON c.id = t.category_id
    """)
    conn.execute("""
    CREATE VIEW monthly_summary AS
    SELECT strftime('%Y-%m', date) AS month,
           COUNT(*)                AS transactions,
           SUM(amount)             AS total,
           ROUND(AVG(amount), 2)   AS avg_amount,
           MAX(amount)             AS biggest
    FROM transactions
    GROUP BY month
    """)

    # 6. Triggers: automatic audit log
    conn.execute("DROP TRIGGER IF EXISTS trg_tx_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_tx_delete")
    conn.execute("DROP TRIGGER IF EXISTS trg_tx_recategorize")
    conn.execute("""
    CREATE TRIGGER trg_tx_insert AFTER INSERT ON transactions
    BEGIN
        INSERT INTO audit_log (action, tx_id, description, amount)
        VALUES ('INSERT', NEW.id, NEW.description, NEW.amount);
    END
    """)
    conn.execute("""
    CREATE TRIGGER trg_tx_delete AFTER DELETE ON transactions
    BEGIN
        INSERT INTO audit_log (action, tx_id, description, amount)
        VALUES ('DELETE', OLD.id, OLD.description, OLD.amount);
    END
    """)
    conn.execute("""
    CREATE TRIGGER trg_tx_recategorize AFTER UPDATE OF category_id ON transactions
    WHEN OLD.category_id <> NEW.category_id
    BEGIN
        INSERT INTO audit_log (action, tx_id, description, amount, details)
        VALUES ('RECATEGORIZE', NEW.id, NEW.description, NEW.amount,
                (SELECT name FROM categories WHERE id = OLD.category_id)
                || ' -> ' ||
                (SELECT name FROM categories WHERE id = NEW.category_id));
    END
    """)

    conn.commit()
    conn.close()


def load_rules(conn):
    # Longer keywords first, so specific rules win
    return conn.execute(
        "SELECT keyword, category_id FROM category_rules ORDER BY LENGTH(keyword) DESC"
    ).fetchall()


def categorize(description, rules, others_id):
    desc = description.lower()
    for keyword, cat_id in rules:
        if keyword in desc:
            return cat_id
    return others_id


def import_csv(file_bytes, others_id):
    """ACID import: either ALL rows are saved, or NONE (rollback on any bad row)."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    fields = {f.strip().lower() for f in (reader.fieldnames or [])}
    missing = {"date", "description", "amount"} - fields
    if missing:
        raise ValueError("Missing column(s): " + ", ".join(sorted(missing)) + ".")

    conn = get_conn()
    rules = load_rules(conn)
    cur = conn.cursor()
    count = 0
    total = 0
    try:
        for line_no, raw in enumerate(reader, start=2):  # line 1 = header
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}

            try:
                datetime.strptime(row.get("date", ""), "%Y-%m-%d")
            except ValueError:
                raise ValueError("Row " + str(line_no) + ": invalid date '"
                                 + row.get("date", "") + "' (use YYYY-MM-DD).")
            if not row.get("description"):
                raise ValueError("Row " + str(line_no) + ": description is empty.")
            try:
                amount = float(row.get("amount", ""))
            except ValueError:
                raise ValueError("Row " + str(line_no) + ": invalid amount '"
                                 + row.get("amount", "") + "'.")
            if amount <= 0:
                raise ValueError("Row " + str(line_no) + ": amount must be greater than 0.")

            total += 1
            cur.execute(
                "INSERT OR IGNORE INTO transactions (date, description, amount, category_id) "
                "VALUES (?, ?, ?, ?)",
                (row["date"], row["description"], amount,
                 categorize(row["description"], rules, others_id))
            )
            count += cur.rowcount
        conn.commit()      # everything is valid -> save all rows
    except Exception:
        conn.rollback()    # something failed -> save nothing
        raise
    finally:
        conn.close()
    return count, total


def run_performance_test(n_rows=100_000, runs=20):
    """Builds a temporary in-memory table, times a query before/after an index."""
    pconn = sqlite3.connect(":memory:")
    pconn.execute("""
    CREATE TABLE perf_transactions (
        id INTEGER PRIMARY KEY,
        date TEXT,
        description TEXT,
        amount REAL,
        category_id INTEGER
    )
    """)
    random.seed(42)
    start = date(2025, 1, 1)
    rows = [
        ((start + timedelta(days=random.randint(0, 650))).isoformat(),
         "Txn " + str(i), round(random.uniform(10, 5000), 2), random.randint(1, 4))
        for i in range(n_rows)
    ]
    pconn.executemany(
        "INSERT INTO perf_transactions (date, description, amount, category_id) VALUES (?, ?, ?, ?)",
        rows
    )
    pconn.commit()

    query = ("SELECT COUNT(*), SUM(amount) FROM perf_transactions "
             "WHERE category_id = 2 AND date BETWEEN '2026-01-01' AND '2026-01-31'")

    def avg_ms():
        t0 = perf_counter()
        for _ in range(runs):
            pconn.execute(query).fetchone()
        return (perf_counter() - t0) / runs * 1000

    plan_before = [r[3] for r in pconn.execute("EXPLAIN QUERY PLAN " + query)]
    before_ms = avg_ms()

    pconn.execute("CREATE INDEX idx_perf_cat_date ON perf_transactions(category_id, date)")

    plan_after = [r[3] for r in pconn.execute("EXPLAIN QUERY PLAN " + query)]
    after_ms = avg_ms()

    pconn.close()
    return query, plan_before, before_ms, plan_after, after_ms



# =====================================================================
# Design system (Material 3 dark): colours, CSS, small UI helpers
# =====================================================================
FONT = "Outfit, Roboto, Segoe UI, sans-serif"
TEXT, MUTED = "#E3E6EE", "#9AA4B8"
PRIMARY, MINT, AMBER, ERROR = "#A5C8FF", "#7ADBC4", "#FFC56B", "#FFB4AB"
SURFACE_HIGH, OUTLINE = "#232833", "#323948"

CAT_COLORS = {"Food": "#FFB77A", "Travel": "#7DC4FF", "Bills": "#C3A6FF", "Others": "#7ADBC4"}
FALLBACK_COLORS = ["#F28DB2", "#E6E57A", "#8EA8FF", "#FF9E9E", "#9BE28A"]

# st.plotly_chart / st.dataframe changed their width argument across versions
_STRETCH_CHART = {"width": "stretch"} if "width" in inspect.signature(st.plotly_chart).parameters \
    else {"use_container_width": True}
_STRETCH_DF = {"width": "stretch"} if "width" in inspect.signature(st.dataframe).parameters \
    else {"use_container_width": True}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=Roboto:wght@400;500;700&display=swap');
:root{
  --bg:#101318; --s1:#14181F; --s2:#1A1E26; --s3:#232833; --s4:#2C323F;
  --outline:#323948; --soft:#262C38; --text:#E3E6EE; --muted:#9AA4B8;
  --primary:#A5C8FF; --on-primary:#00315F; --mint:#7ADBC4; --amber:#FFC56B;
  --error:#FFB4AB; --error-c:#4A1D1B; --ease:cubic-bezier(.2,0,0,1);
}
html, body, .stApp, [data-testid="stAppViewContainer"]{font-family:'Roboto','Segoe UI',system-ui,sans-serif;}
.stApp{background:radial-gradient(1100px 520px at 6% -8%, rgba(165,200,255,.11), transparent 62%),
       radial-gradient(900px 480px at 100% 0%, rgba(195,166,255,.07), transparent 60%), var(--bg);}
#MainMenu, footer{visibility:hidden;}
header[data-testid="stHeader"]{background:transparent;}
.block-container{padding-top:2.2rem;padding-bottom:4rem;max-width:1280px;}

/* ---------- app bar ---------- */
.appbar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:4px 0 20px;}
.brand{display:flex;align-items:center;gap:14px;}
.logo{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;color:#D4E4FF;
      background:linear-gradient(140deg,#2F6AB3,#143A66);box-shadow:inset 0 0 0 1px rgba(165,200,255,.28),0 8px 24px rgba(20,58,102,.45);}
.brand-title{font:600 26px/1.1 Outfit,Roboto,sans-serif;letter-spacing:-.015em;color:var(--text);}
.brand-sub{margin-top:3px;color:var(--muted);font-size:13.5px;}
.chips{display:flex;gap:8px;flex-wrap:wrap;}
.chip{display:inline-flex;align-items:center;gap:7px;height:32px;padding:0 12px;border-radius:8px;
      border:1px solid var(--outline);background:var(--s2);color:var(--text);font-size:13px;}
.chip svg{color:var(--primary);}

/* ---------- tabs ---------- */
.stTabs [data-baseweb="tab-list"]{gap:4px;border-bottom:1px solid var(--soft);}
.stTabs [data-baseweb="tab"]{height:50px;padding:0 18px;border-radius:12px 12px 0 0;color:var(--muted);
      font-weight:500;transition:background .2s var(--ease),color .2s var(--ease);}
.stTabs [data-baseweb="tab"]:hover{background:rgba(165,200,255,.08);color:var(--text);}
.stTabs [aria-selected="true"]{color:var(--primary);}
.stTabs [data-baseweb="tab-highlight"]{background:var(--primary);height:3px;border-radius:3px 3px 0 0;}
.stTabs [data-baseweb="tab-border"]{display:none;}

/* ---------- cards ---------- */
[class*="st-key-card"]{background:var(--s2);border:1px solid var(--soft);border-radius:20px;padding:22px 24px;}
.card-title{font:600 18px/1.2 Outfit,Roboto,sans-serif;color:var(--text);letter-spacing:-.005em;}
.card-sub{color:var(--muted);font-size:13px;margin:4px 0 14px;}
.sec-title{font:600 20px/1.2 Outfit,Roboto,sans-serif;margin:26px 0 12px;color:var(--text);}

/* ---------- KPI cards ---------- */
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0 8px;}
@media (max-width:980px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr));}}
.kpi{background:var(--s2);border:1px solid var(--soft);border-radius:18px;padding:16px 18px;display:flex;gap:14px;align-items:center;min-width:0;}
.kpi .ic{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;flex:none;}
.kpi .v{font:600 25px/1.1 Outfit,Roboto,sans-serif;letter-spacing:-.01em;font-variant-numeric:tabular-nums;color:var(--text);}
.kpi .l{color:var(--muted);font-size:13px;margin-bottom:3px;}
.kpi .s{color:var(--muted);font-size:12px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

/* ---------- budgets ---------- */
.bud{margin:16px 0 4px;}
.bud .top{display:flex;justify-content:space-between;align-items:baseline;gap:10px;font-size:14px;margin-bottom:8px;color:var(--text);}
.bud .top span:last-child{color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums;}
.track{height:10px;border-radius:999px;background:var(--s4);overflow:hidden;}
.fill{height:100%;border-radius:999px;animation:grow .9s var(--ease) both;}
@keyframes grow{from{width:0;}}
.dotc{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;}

/* ---------- alerts + lists ---------- */
.alert{display:flex;gap:12px;align-items:flex-start;padding:12px 16px;border-radius:14px;margin:10px 0 0;font-size:14px;line-height:1.45;}
.alert.err{background:var(--error-c);color:#FFDAD6;}
.alert.ok{background:rgba(122,219,196,.10);color:#BDF3E5;}
.alert.info{background:rgba(165,200,255,.10);color:#D4E4FF;}
.alert svg{flex:none;margin-top:1px;}
.list{display:flex;flex-direction:column;}
.row{display:flex;align-items:center;gap:14px;padding:11px 0;border-bottom:1px solid var(--soft);}
.row:last-child{border-bottom:0;}
.row .rank{width:26px;height:26px;border-radius:8px;background:var(--s4);display:grid;place-items:center;font:600 13px Outfit,sans-serif;color:var(--muted);flex:none;}
.row .main{flex:1;min-width:0;}
.row .name{font-size:14.5px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.row .meta{font-size:12.5px;color:var(--muted);margin-top:2px;}
.row .amt{font:600 16px Outfit,sans-serif;font-variant-numeric:tabular-nums;color:var(--text);}
.bar{height:5px;border-radius:999px;background:var(--s4);margin-top:8px;overflow:hidden;}
.bar>i{display:block;height:100%;border-radius:999px;animation:grow .9s var(--ease) both;}
.pill{display:inline-flex;align-items:center;height:22px;padding:0 9px;border-radius:7px;font-size:12px;font-weight:500;}

/* ---------- stepper (sidebar) ---------- */
.side-title{font:600 17px/1.2 Outfit,Roboto,sans-serif;margin:4px 0 2px;color:var(--text);}
.side-sub{color:var(--muted);font-size:12.5px;margin-bottom:16px;}
.step{display:flex;gap:12px;position:relative;padding-bottom:18px;}
.step:last-child{padding-bottom:0;}
.step:not(:last-child)::before{content:"";position:absolute;left:12px;top:28px;bottom:2px;width:2px;background:var(--outline);}
.step.done:not(:last-child)::before{background:var(--mint);opacity:.55;}
.dot{width:26px;height:26px;border-radius:50%;flex:none;display:grid;place-items:center;border:2px solid var(--outline);color:var(--muted);font:600 12px Outfit,sans-serif;background:var(--s1);}
.step.done .dot{background:var(--mint);border-color:var(--mint);color:#00382D;}
.step.now .dot{border-color:var(--primary);color:var(--primary);box-shadow:0 0 0 4px rgba(165,200,255,.14);}
.step .t{font-size:14px;color:var(--text);font-weight:500;line-height:1.3;}
.step .d{font-size:12.5px;color:var(--muted);margin-top:2px;line-height:1.4;}
.step.pending .t{color:var(--muted);}

/* ---------- empty state ---------- */
.empty{display:flex;flex-direction:column;align-items:center;text-align:center;padding:46px 20px 40px;background:var(--s2);border:1px dashed var(--outline);border-radius:24px;margin-top:18px;}
.empty .t{font:600 22px Outfit,sans-serif;margin-top:18px;color:var(--text);}
.empty .d{color:var(--muted);font-size:14.5px;margin-top:8px;max-width:440px;line-height:1.55;}

/* ---------- Streamlit widgets ---------- */
section[data-testid="stSidebar"]{background:var(--s1);border-right:1px solid var(--soft);}
section[data-testid="stSidebar"] .block-container{padding-top:1.4rem;}
.stButton>button, .stDownloadButton>button{border-radius:999px;border:1px solid var(--outline);background:var(--s3);color:var(--text);
  font-weight:500;min-height:42px;padding:0 22px;transition:background .18s var(--ease),border-color .18s var(--ease),transform .12s var(--ease);}
.stButton>button:hover{border-color:var(--primary);background:#27324A;color:#fff;}
.stButton>button:active{transform:scale(.98);}
.stButton>button[kind="primary"]{background:var(--primary);color:var(--on-primary);border-color:transparent;}
.stButton>button[kind="primary"]:hover{background:#BAD5FF;color:var(--on-primary);}
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="select"]>div{border-radius:12px !important;}
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="select"]>div{background:var(--s3) !important;border-color:var(--outline) !important;}
[data-baseweb="input"]:focus-within, [data-baseweb="select"]>div:focus-within{border-color:var(--primary) !important;box-shadow:0 0 0 1px var(--primary);}
[data-testid="stFileUploaderDropzone"]{background:var(--s3);border:1.5px dashed var(--outline);border-radius:16px;transition:border-color .2s var(--ease),background .2s var(--ease);}
[data-testid="stFileUploaderDropzone"]:hover{border-color:var(--primary);background:#262E3F;}
[data-testid="stDataFrame"]{border:1px solid var(--soft);border-radius:14px;overflow:hidden;}
[data-testid="stExpander"]{border:1px solid var(--soft);border-radius:16px;background:var(--s2);}
[data-testid="stExpander"] summary{font-weight:500;}
label[data-testid="stWidgetLabel"] p{color:var(--muted);font-size:13px;font-weight:500;}
hr{border-color:var(--soft);}

@media (prefers-reduced-motion:reduce){
  *{animation:none !important;transition:none !important;}
}
</style>
"""

ICONS = {
    "wallet": '<path d="M3 7a2 2 0 0 1 2-2h12v4"/><path d="M3 7v10a2 2 0 0 0 2 2h14a1 1 0 0 0 1-1V9a1 1 0 0 0-1-1H5a2 2 0 0 1-2-2"/><circle cx="16.5" cy="13.5" r="1.1" fill="currentColor"/>',
    "receipt": '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
    "trend": '<polyline points="3 17 9 11 13 15 21 7"/><polyline points="15 7 21 7 21 13"/>',
    "peak": '<path d="M12 3v14"/><path d="m6 11 6 6 6-6"/><path d="M5 21h14"/>',
    "shield": '<path d="M12 3 4 6v6c0 4.5 3.2 8 8 9 4.8-1 8-4.5 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "alert": '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18"/>',
    "database": '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    "upload": '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
}


def icon(name, size=22):
    return ('<svg width="%d" height="%d" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">%s</svg>') % (size, size, ICONS[name])


def esc(text):
    return _html.escape(str(text))


def cat_color(name):
    if name in CAT_COLORS:
        return CAT_COLORS[name]
    return FALLBACK_COLORS[sum(ord(ch) for ch in str(name)) % len(FALLBACK_COLORS)]


def inr(x, decimals=0):
    """Indian digit grouping: 1234567 -> ₹12,34,567"""
    neg = x < 0
    s = ("%." + str(decimals) + "f") % abs(x)
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + "₹" + whole + ("." + frac if decimals else "")


def month_label(m):
    return datetime.strptime(m + "-01", "%Y-%m-%d").strftime("%b %Y")


def day_label(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return str(dt.day) + " " + dt.strftime("%b %Y")


def card_title(title, sub=""):
    st.markdown('<div class="card-title">' + esc(title) + '</div>'
                + ('<div class="card-sub">' + esc(sub) + '</div>' if sub else '<div style="height:12px"></div>'),
                unsafe_allow_html=True)


def kpi(ic, color, label, value, sub):
    return ('<div class="kpi"><div class="ic" style="background:%s22;color:%s">%s</div>'
            '<div style="min-width:0"><div class="l">%s</div><div class="v">%s</div><div class="s">%s</div></div></div>'
            % (color, color, icon(ic, 22), esc(label), esc(value), esc(sub)))


def alert(kind, text, ic="alert"):
    return '<div class="alert %s">%s<div>%s</div></div>' % (kind, icon(ic, 18), text)


def style_fig(fig, height=300, legend=True):
    fig.update_layout(
        height=height, margin=dict(l=4, r=4, t=10, b=4),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=MUTED, size=12),
        hoverlabel=dict(bgcolor=SURFACE_HIGH, bordercolor=OUTLINE, font=dict(family=FONT, color=TEXT)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, bgcolor="rgba(0,0,0,0)"),
        showlegend=legend, bargap=0.38,
    )
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor=OUTLINE, ticks="")
    fig.update_yaxes(gridcolor="rgba(255,255,255,0.06)", zeroline=False, tickprefix="₹", separatethousands=True)
    return fig


def show_chart(fig):
    st.plotly_chart(fig, config={"displayModeBar": False}, **_STRETCH_CHART)


def empty_state(title, text):
    st.markdown(
        '<div class="empty">'
        '<svg width="120" height="96" viewBox="0 0 120 96" fill="none">'
        '<rect x="20" y="22" width="80" height="58" rx="10" fill="#232833" stroke="#323948"/>'
        '<rect x="30" y="12" width="60" height="58" rx="10" fill="#2C323F" stroke="#3A4256"/>'
        '<path d="M42 30h36M42 42h24M42 54h30" stroke="#A5C8FF" stroke-width="3" stroke-linecap="round" opacity=".85"/>'
        '<circle cx="92" cy="72" r="16" fill="#143A66" stroke="#A5C8FF" stroke-width="2"/>'
        '<path d="M92 64v16M85 72h14" stroke="#D4E4FF" stroke-width="3" stroke-linecap="round"/></svg>'
        '<div class="t">' + esc(title) + '</div><div class="d">' + esc(text) + '</div></div>',
        unsafe_allow_html=True)


HERO_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=Roboto:wght@400;500&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:transparent;font-family:Roboto,'Segoe UI',system-ui,sans-serif;color:#E3E6EE;height:100%}
.card{height:100%;border-radius:28px;padding:26px 30px;display:flex;align-items:center;gap:34px;flex-wrap:nowrap;position:relative;overflow:hidden;
  background:linear-gradient(135deg,#17335A 0%,#16253F 48%,#1A1E26 100%);border:1px solid rgba(165,200,255,.22)}
.card::before{content:"";position:absolute;right:-90px;top:-120px;width:340px;height:340px;border-radius:50%;
  background:radial-gradient(circle,rgba(165,200,255,.20),transparent 68%);pointer-events:none}
.left{flex:1;min-width:230px;position:relative}
.lbl{font-size:14px;color:#B8C7E0}
.amt{font:600 54px/1.05 Outfit,sans-serif;letter-spacing:-.02em;margin:8px 0 14px;font-variant-numeric:tabular-nums;color:#fff}
.delta{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border-radius:10px;font-size:13px;font-weight:500}
.delta.good{background:rgba(122,219,196,.16);color:#9BEBD6}
.delta.bad{background:rgba(255,197,107,.16);color:#FFD596}
.delta.flat{background:rgba(165,200,255,.14);color:#D4E4FF}
.sub{margin-top:14px;font-size:13px;color:#9FB0CC;line-height:1.5}
.right{display:flex;align-items:center;gap:26px;position:relative}
.ring{position:relative;width:176px;height:176px;flex:none}
.ring svg{transform:rotate(-90deg);width:100%;height:100%}
.ring circle.seg{fill:none;stroke-width:20;transition:stroke-dasharray 1.1s cubic-bezier(.2,0,0,1)}
.ring circle.bg{fill:none;stroke:rgba(255,255,255,.07);stroke-width:20}
.mid{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
.mid b{font:600 22px Outfit,sans-serif}
.mid span{font-size:12px;color:#9FB0CC;margin-top:2px}
.legend{display:flex;flex-direction:column;gap:11px;min-width:170px}
.li{display:flex;align-items:center;gap:10px;font-size:13.5px}
.li i{width:10px;height:10px;border-radius:50%;flex:none}
.li .n{flex:1;color:#D9E2F2}
.li .v{font:500 13.5px Outfit,sans-serif;font-variant-numeric:tabular-nums}
.li .p{color:#8FA0BD;font-size:12px;width:36px;text-align:right}
@media (max-width:760px){.legend{display:none}.card{padding:22px;gap:20px}.amt{font-size:42px}}
@media (max-width:520px){.right{display:none}}
</style></head><body>
<div class="card">
  <div class="left">
    <div class="lbl">Total spent</div>
    <div class="amt" id="amt">&#8377;0</div>
    <div id="delta"></div>
    <div class="sub" id="sub"></div>
  </div>
  <div class="right">
    <div class="ring"><svg viewBox="0 0 200 200"><circle class="bg" cx="100" cy="100" r="76"/><g id="segs"></g></svg>
      <div class="mid"><b id="topname"></b><span id="toppct"></span></div></div>
    <div class="legend" id="legend"></div>
  </div>
</div>
<script>
const D = __DATA__;
const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const fmt = n => '\u20B9' + Math.round(n).toLocaleString('en-IN');
const amt = document.getElementById('amt');
if (reduce) { amt.textContent = fmt(D.total); } else {
  const t0 = performance.now(), dur = 1200;
  const tick = now => { const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 4);
    amt.textContent = fmt(D.total * e); if (p < 1) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}
const dl = document.getElementById('delta');
if (D.delta_text) { dl.innerHTML = '<span class="delta ' + D.delta_kind + '">' + D.delta_text + '</span>'; }
document.getElementById('sub').textContent = D.sub;
const C = 2 * Math.PI * 76, gap = D.segs.length > 1 ? 3 : 0;
const g = document.getElementById('segs'), leg = document.getElementById('legend');
let off = 0; const circles = [];
D.segs.forEach((s, i) => {
  const len = Math.max(0, C * s.share - gap);
  const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  c.setAttribute('class', 'seg'); c.setAttribute('cx', 100); c.setAttribute('cy', 100); c.setAttribute('r', 76);
  c.setAttribute('stroke', s.color); c.setAttribute('stroke-dashoffset', -off);
  c.style.transitionDelay = (i * 110) + 'ms';
  c.setAttribute('stroke-dasharray', reduce ? len + ' ' + (C - len) : '0 ' + C);
  g.appendChild(c); circles.push([c, len]); off += C * s.share;
  leg.insertAdjacentHTML('beforeend', '<div class="li"><i style="background:' + s.color + '"></i><span class="n">' + s.name +
    '</span><span class="v">' + fmt(s.value) + '</span><span class="p">' + Math.round(s.share * 100) + '%</span></div>');
});
if (!reduce) requestAnimationFrame(() => requestAnimationFrame(() => circles.forEach(([c, len]) => c.setAttribute('stroke-dasharray', len + ' ' + (C - len)))));
if (D.segs.length) { document.getElementById('topname').textContent = D.segs[0].name; document.getElementById('toppct').textContent = Math.round(D.segs[0].share * 100) + '% of spend'; }
</script></body></html>
"""


def render_hero(total, segs, delta_text, delta_kind, sub):
    data = {"total": total, "segs": segs, "delta_text": delta_text, "delta_kind": delta_kind, "sub": sub}
    page = HERO_HTML.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    if hasattr(st, "iframe"):                      # newer Streamlit
        st.iframe(page, height=262)
    else:                                          # older Streamlit
        import streamlit.components.v1 as components
        components.html(page, height=262)


st.markdown(CSS, unsafe_allow_html=True)


init_db()

# Category list used by forms
conn_meta = get_conn()
cat_rows = conn_meta.execute("SELECT id, name FROM categories ORDER BY id").fetchall()
conn_meta.close()
cat_name_to_id = {name: cid for cid, name in cat_rows}
category_names = [name for _, name in cat_rows]
others_id = cat_name_to_id["Others"]

DEFAULT_KEYWORDS = ["swiggy", "zomato", "uber", "bus", "petrol", "bill", "recharge"]

# =====================================================================
# Page skeleton (filled in below, in the order data is ready)
# =====================================================================
header_slot = st.container()
tab_overview, tab_insights, tab_tx, tab_manage, tab_lab = st.tabs([
    ":material/space_dashboard: Overview",
    ":material/insights: Insights",
    ":material/receipt_long: Transactions",
    ":material/tune: Manage",
    ":material/database: Database lab",
])

# =====================================================================
# Sidebar, step 1: import a statement (all-or-nothing)
# =====================================================================
with st.sidebar:
    st.markdown(
        '<div class="side-title">Import statement</div>'
        '<div class="side-sub">CSV with date, description and amount columns.</div>',
        unsafe_allow_html=True)
    uploaded = st.file_uploader("Bank statement CSV", type="csv", label_visibility="collapsed")
    import_slot = st.container()
    st.divider()
    stepper_slot = st.container()

if uploaded is not None:
    data_bytes = uploaded.getvalue()
    file_id = getattr(uploaded, "file_id", None) or (getattr(uploaded, "name", ""), len(data_bytes))
    if st.session_state.get("import_id") != file_id:      # process each uploaded file once
        st.session_state["import_id"] = file_id
        try:
            added, total_rows = import_csv(data_bytes, others_id)
            skipped = total_rows - added
            if added:
                msg = str(added) + " new transactions added."
                if skipped:
                    msg += " " + str(skipped) + " duplicates skipped."
                st.session_state["import_result"] = ("ok", msg)
                st.toast(msg, icon=":material/check_circle:")
            else:
                st.session_state["import_result"] = (
                    "info", "Nothing new. All " + str(total_rows) + " rows were already imported.")
        except ValueError as e:
            st.session_state["import_result"] = (
                "err", "Upload failed. " + esc(e) + " Nothing was saved (rolled back).")
            st.toast("Upload failed. Nothing was saved.", icon=":material/error:")
        except UnicodeDecodeError:
            st.session_state["import_result"] = (
                "err", "Upload failed. The file is not valid UTF-8 text. Nothing was saved.")
        except Exception as e:
            st.session_state["import_result"] = (
                "err", "Upload failed: " + esc(e) + " Nothing was saved (rolled back).")
else:
    st.session_state.pop("import_id", None)
    st.session_state.pop("import_result", None)

if st.session_state.get("import_result"):
    kind, text = st.session_state["import_result"]
    import_slot.markdown(
        alert({"ok": "ok", "info": "info", "err": "err"}[kind], text,
              {"ok": "check", "info": "check", "err": "alert"}[kind]),
        unsafe_allow_html=True)

# =====================================================================
# Manage tab: budgets, rules, delete (actions run before data is read)
# =====================================================================
with tab_manage:
    st.markdown('<div class="sec-title" style="margin-top:10px">Budgets, rules and clean-up</div>',
                unsafe_allow_html=True)
    m1, m2, m3 = st.columns(3, gap="medium")

    with m1:
        with st.container(key="card_m_budget"):
            card_title("Monthly budget", "Get a warning when a category goes over its limit.")
            b_category = st.selectbox("Category", category_names, key="budget_cat")
            b_limit = st.number_input("Monthly limit (₹)", min_value=0, step=100, key="budget_limit")
            if st.button("Save budget", type="primary", key="save_budget"):
                bconn = get_conn()
                bconn.execute(
                    "INSERT OR REPLACE INTO budgets (category_id, monthly_limit) VALUES (?, ?)",
                    (cat_name_to_id[b_category], b_limit)
                )
                bconn.commit()
                bconn.close()
                st.toast("Budget saved for " + b_category, icon=":material/check_circle:")

    with m2:
        with st.container(key="card_m_rules"):
            card_title("Category rules", "A keyword in the description decides the category.")
            new_keyword = st.text_input("Keyword", placeholder="e.g. netflix", key="new_keyword")
            new_rule_cat = st.selectbox("Category", category_names, key="rule_cat")
            if st.button("Add rule", type="primary", key="add_rule"):
                kw = new_keyword.strip().lower()
                if kw:
                    rconn = get_conn()
                    rconn.execute(
                        "INSERT OR REPLACE INTO category_rules (keyword, category_id) VALUES (?, ?)",
                        (kw, cat_name_to_id[new_rule_cat])
                    )
                    rconn.commit()
                    rconn.close()
                    st.toast("Rule saved: " + kw + " → " + new_rule_cat, icon=":material/check_circle:")
                else:
                    st.toast("Type a keyword first.", icon=":material/info:")

            dconn = get_conn()
            existing_keywords = [r[0] for r in dconn.execute(
                "SELECT keyword FROM category_rules ORDER BY keyword").fetchall()]
            dconn.close()
            del_keyword = st.selectbox("Delete a rule", ["-"] + existing_keywords, key="del_kw")
            if st.button("Delete rule", key="delete_rule") and del_keyword != "-":
                dconn = get_conn()
                dconn.execute("DELETE FROM category_rules WHERE keyword = ?", (del_keyword,))
                dconn.commit()
                dconn.close()
                st.toast("Deleted rule: " + del_keyword, icon=":material/delete:")

            if st.button("Re-apply rules to all transactions", key="reapply_rules"):
                rconn = get_conn()
                rules = load_rules(rconn)
                all_tx = rconn.execute("SELECT id, description FROM transactions").fetchall()
                for tx_id, desc in all_tx:
                    rconn.execute(
                        "UPDATE transactions SET category_id = ? WHERE id = ?",
                        (categorize(desc, rules, others_id), tx_id)
                    )
                rconn.commit()
                rconn.close()
                st.toast("Rules re-applied to " + str(len(all_tx)) + " transactions",
                         icon=":material/check_circle:")

    with m3:
        with st.container(key="card_m_delete"):
            card_title("Delete a transaction", "Removes one row. The audit log records it.")
            del_id = st.number_input("Transaction ID", min_value=1, step=1, key="del_tx_id")
            if st.button("Delete transaction", key="delete_tx"):
                tconn = get_conn()
                cur_del = tconn.execute("DELETE FROM transactions WHERE id = ?", (int(del_id),))
                tconn.commit()
                deleted = cur_del.rowcount
                tconn.close()
                if deleted:
                    st.toast("Deleted transaction " + str(int(del_id)), icon=":material/delete:")
                else:
                    st.toast("No transaction with that ID.", icon=":material/info:")

# =====================================================================
# Read data (after every action above has finished)
# =====================================================================
conn = get_conn()
df = pd.read_sql_query("SELECT * FROM transaction_details", conn)
has_data = not df.empty

tx_count = len(df)
budget_count = conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0]
custom_rules = conn.execute(
    "SELECT COUNT(*) FROM category_rules WHERE keyword NOT IN (" + ",".join("?" * len(DEFAULT_KEYWORDS)) + ")",
    DEFAULT_KEYWORDS).fetchone()[0]

# ---------- sidebar stepper ----------
steps = [
    ("Import a statement", "Upload a CSV with your transactions.", tx_count > 0),
    ("Review categories", "Add keywords for your own merchants.", custom_rules > 0),
    ("Set a budget", "Get warned when a category runs over.", budget_count > 0),
    ("Read the insights", "Trends, unusual spends and totals.", tx_count > 0 and budget_count > 0),
]
first_open = next((i for i, s in enumerate(steps) if not s[2]), None)
step_html = '<div class="side-title">Getting started</div><div class="side-sub">Four steps, in any order.</div>'
for i, (title, desc, done) in enumerate(steps):
    cls = "done" if done else ("now" if i == first_open else "pending")
    mark = icon("check", 14) if done else str(i + 1)
    step_html += ('<div class="step %s"><div class="dot">%s</div><div><div class="t">%s</div>'
                  '<div class="d">%s</div></div></div>') % (cls, mark, esc(title), esc(desc))
stepper_slot.markdown(step_html, unsafe_allow_html=True)

# ---------- app bar ----------
chips = ""
if has_data:
    chips = ('<span class="chip">%s%s to %s</span><span class="chip">%s%d transactions</span>'
             % (icon("calendar", 16), esc(day_label(df["date"].min())), esc(day_label(df["date"].max())),
                icon("receipt", 16), tx_count))
header_slot.markdown(
    '<div class="appbar"><div class="brand"><div class="logo">' + icon("wallet", 26) + '</div>'
    '<div><div class="brand-title">Expense Tracker</div>'
    '<div class="brand-sub">Your bank statement, turned into clear answers.</div></div></div>'
    '<div class="chips">' + chips + '</div></div>',
    unsafe_allow_html=True)

# ---------- shared analytics (SQL) ----------
if has_data:
    # Month-over-month (LAG window function over the monthly_summary VIEW)
    mom_df = pd.read_sql_query("""
        SELECT month,
               total,
               LAG(total) OVER (ORDER BY month) AS prev_total,
               ROUND(
                   (total - LAG(total) OVER (ORDER BY month)) * 100.0
                   / LAG(total) OVER (ORDER BY month), 1
               ) AS change_pct
        FROM monthly_summary
        ORDER BY month
    """, conn)
    latest_month = mom_df.iloc[-1]["month"]

    # Budget progress for the latest month (LEFT JOIN keeps budgets with no spending yet)
    progress_df = pd.read_sql_query("""
        SELECT c.name AS category,
               b.monthly_limit AS budget,
               COALESCE(SUM(t.amount), 0) AS spent
        FROM budgets b
        JOIN categories c ON c.id = b.category_id
        LEFT JOIN transactions t
               ON t.category_id = b.category_id
              AND strftime('%Y-%m', t.date) = ?
        GROUP BY b.category_id
        ORDER BY c.name
    """, conn, params=[latest_month])
    on_track = int((progress_df["spent"] <= progress_df["budget"]).sum()) if not progress_df.empty else 0

# =====================================================================
# Overview
# =====================================================================
with tab_overview:
    if not has_data:
        empty_state("No transactions yet",
                    "Import a CSV from the sidebar. Try data/sample.csv to see the full dashboard.")
    else:
        total_spent = float(df["amount"].sum())
        by_cat = (df.groupby("category")["amount"].sum().sort_values(ascending=False))
        segs = [{"name": n, "value": float(v), "share": float(v) / total_spent, "color": cat_color(n)}
                for n, v in by_cat.items()]

        delta_text, delta_kind = "", "flat"
        latest_row = mom_df.iloc[-1]
        if len(mom_df) > 1 and pd.notna(latest_row["change_pct"]):
            pct = float(latest_row["change_pct"])
            prev_label = month_label(mom_df.iloc[-2]["month"])
            if pct < 0:
                delta_kind, word, arrow = "good", "lower", "\u2193"
            elif pct > 0:
                delta_kind, word, arrow = "bad", "higher", "\u2191"
            else:
                delta_kind, word, arrow = "flat", "the same", "\u2192"
            delta_text = "%s %s%% %s in %s than %s" % (
                arrow, abs(pct), word, month_label(latest_month), prev_label) if pct else \
                "Same spending in " + month_label(latest_month) + " as " + prev_label
        sub = "%d transactions from %s to %s" % (tx_count, day_label(df["date"].min()), day_label(df["date"].max()))
        render_hero(total_spent, segs, delta_text, delta_kind, sub)

        biggest = df.loc[df["amount"].idxmax()]
        budget_value = (str(on_track) + " of " + str(len(progress_df))) if not progress_df.empty else "None set"
        budget_sub = ("on track in " + month_label(latest_month)) if not progress_df.empty else "Add one in Manage"
        st.markdown(
            '<div class="kpis">'
            + kpi("receipt", PRIMARY, "Transactions", str(tx_count), str(len(mom_df)) + " months of data")
            + kpi("trend", "#C3A6FF", "Average expense", inr(df["amount"].mean()), "per transaction")
            + kpi("peak", AMBER, "Biggest expense", inr(biggest["amount"]), biggest["description"])
            + kpi("shield", MINT, "Budgets", budget_value, budget_sub)
            + '</div>', unsafe_allow_html=True)

        left, right = st.columns([3, 2], gap="medium")
        with left:
            with st.container(key="card_monthly"):
                card_title("Spending by month", "Each bar is split by category.")
                stack_df = pd.read_sql_query("""
                    SELECT strftime('%Y-%m', t.date) AS month, c.name AS category, SUM(t.amount) AS total
                    FROM transactions t
                    JOIN categories c ON c.id = t.category_id
                    GROUP BY month, category
                    ORDER BY month
                """, conn)
                fig = go.Figure()
                for cat in by_cat.index:
                    part = stack_df[stack_df["category"] == cat]
                    fig.add_bar(x=[month_label(m) for m in part["month"]], y=part["total"], name=cat,
                                marker_color=cat_color(cat),
                                hovertemplate="%{fullData.name}: ₹%{y:,.0f}<extra></extra>")
                fig.update_layout(barmode="stack", hovermode="x unified")
                try:
                    fig.update_traces(marker_cornerradius=6)
                except Exception:
                    pass
                show_chart(style_fig(fig, 330))
        with right:
            with st.container(key="card_top5"):
                card_title("Top 5 expenses", "The largest single payments.")
                top_df = pd.read_sql_query("""
                    SELECT date, description, category, amount
                    FROM transaction_details
                    ORDER BY amount DESC
                    LIMIT 5
                """, conn)
                peak = float(top_df["amount"].max())
                rows = ""
                for i, r in top_df.iterrows():
                    col = cat_color(r["category"])
                    rows += ('<div class="row"><div class="rank">%d</div><div class="main">'
                             '<div class="name">%s</div><div class="meta">%s, %s</div>'
                             '<div class="bar"><i style="width:%.1f%%;background:%s"></i></div></div>'
                             '<div class="amt">%s</div></div>') % (
                        i + 1, esc(r["description"]), esc(day_label(r["date"])), esc(r["category"]),
                        r["amount"] / peak * 100, col, inr(r["amount"]))
                st.markdown('<div class="list">' + rows + '</div>', unsafe_allow_html=True)

# =====================================================================
# Insights
# =====================================================================
with tab_insights:
    if not has_data:
        empty_state("Insights appear here", "Import some transactions first, then come back for trends and alerts.")
    else:
        a, b = st.columns(2, gap="medium")

        # ----- budgets + alerts (JOIN, GROUP BY, HAVING) -----
        with a:
            with st.container(key="card_budgets"):
                card_title("Budgets", "Spending against your limits in " + month_label(latest_month) + ".")
                if progress_df.empty:
                    st.markdown(alert("info", "No budgets yet. Set one in the Manage tab.", "shield"),
                                unsafe_allow_html=True)
                else:
                    bars = ""
                    for _, r in progress_df.iterrows():
                        ratio = r["spent"] / r["budget"] if r["budget"] else 0
                        colour = MINT if ratio < 0.8 else (AMBER if ratio <= 1 else ERROR)
                        bars += ('<div class="bud"><div class="top"><span><i class="dotc" style="background:%s"></i>%s</span>'
                                 '<span>%s of %s</span></div><div class="track"><div class="fill" '
                                 'style="width:%.1f%%;background:%s"></div></div></div>') % (
                            cat_color(r["category"]), esc(r["category"]), inr(r["spent"]), inr(r["budget"]),
                            min(ratio, 1) * 100, colour)
                    st.markdown(bars, unsafe_allow_html=True)

                alert_df = pd.read_sql_query("""
                    SELECT c.name AS category,
                           strftime('%Y-%m', t.date) AS month,
                           SUM(t.amount) AS spent,
                           b.monthly_limit AS budget
                    FROM transactions t
                    JOIN categories c ON c.id = t.category_id
                    JOIN budgets b ON b.category_id = t.category_id
                    GROUP BY t.category_id, month
                    HAVING SUM(t.amount) > b.monthly_limit
                    ORDER BY month DESC
                """, conn)
                if not progress_df.empty:
                    if alert_df.empty:
                        st.markdown(alert("ok", "All spending is within budget.", "check"), unsafe_allow_html=True)
                    else:
                        out = ""
                        for _, r in alert_df.head(4).iterrows():
                            out += alert("err", "<b>%s</b> in %s: %s spent against a %s budget (%s over)." % (
                                esc(r["category"]), month_label(r["month"]), inr(r["spent"]),
                                inr(r["budget"]), inr(r["spent"] - r["budget"])))
                        if len(alert_df) > 4:
                            out += alert("info", "And " + str(len(alert_df) - 4) + " more over-budget months.")
                        st.markdown(out, unsafe_allow_html=True)

        # ----- anomaly detection (AVG + subquery) -----
        with b:
            with st.container(key="card_anomaly"):
                card_title("Unusual expenses", "Payments far above the average of their category.")
                factor = st.slider("Flag when an expense is more than this many times its category average",
                                   min_value=1.2, max_value=5.0, value=1.5, step=0.1, key="anomaly_factor")
                anomaly_df = pd.read_sql_query("""
                    SELECT t.date,
                           t.description,
                           c.name AS category,
                           t.amount,
                           ROUND(a.avg_amount, 2) AS category_avg,
                           ROUND(t.amount / a.avg_amount, 2) AS times_avg
                    FROM transactions t
                    JOIN categories c ON c.id = t.category_id
                    JOIN (
                        SELECT category_id, AVG(amount) AS avg_amount
                        FROM transactions
                        GROUP BY category_id
                    ) a ON a.category_id = t.category_id
                    WHERE a.avg_amount > 0
                      AND t.amount > ? * a.avg_amount
                    ORDER BY times_avg DESC
                """, conn, params=[factor])
                if anomaly_df.empty:
                    st.markdown(alert("ok", "Nothing unusual at this threshold.", "check"), unsafe_allow_html=True)
                else:
                    rows = ""
                    for _, r in anomaly_df.head(6).iterrows():
                        col = cat_color(r["category"])
                        rows += ('<div class="row"><div class="main"><div class="name">%s</div>'
                                 '<div class="meta"><span class="pill" style="background:%s22;color:%s">%s</span>'
                                 ' &nbsp;%s, average %s</div></div>'
                                 '<div style="text-align:right"><div class="amt">%s</div>'
                                 '<div class="meta">%s&times; average</div></div></div>') % (
                            esc(r["description"]), col, col, esc(r["category"]), esc(day_label(r["date"])),
                            inr(r["category_avg"]), inr(r["amount"]), r["times_avg"])
                    st.markdown('<div class="list">' + rows + '</div>', unsafe_allow_html=True)
                    if len(anomaly_df) > 6:
                        st.caption("Showing 6 of " + str(len(anomaly_df)) + ".")

        c, d = st.columns(2, gap="medium")

        # ----- month over month (LAG) -----
        with c:
            with st.container(key="card_mom"):
                card_title("Month over month", "Total spending per month and the change from the month before.")
                labels = [month_label(m) for m in mom_df["month"]]
                shades = ["rgba(165,200,255,.38)"] * (len(labels) - 1) + [PRIMARY]
                fig = go.Figure(go.Bar(
                    x=labels, y=mom_df["total"], marker_color=shades,
                    text=[inr(v) for v in mom_df["total"]], textposition="outside", cliponaxis=False,
                    textfont=dict(color=TEXT, family=FONT, size=12),
                    hovertemplate="₹%{y:,.0f}<extra></extra>"))
                try:
                    fig.update_traces(marker_cornerradius=8)
                except Exception:
                    pass
                fig.update_yaxes(showticklabels=False, gridcolor="rgba(255,255,255,0.04)")
                show_chart(style_fig(fig, 260, legend=False))
                last = mom_df.iloc[-1]
                if pd.isna(last["prev_total"]):
                    st.markdown(alert("info", "Add a second month of data to see the change."), unsafe_allow_html=True)
                else:
                    pct = float(last["change_pct"])
                    st.markdown(alert("ok" if pct <= 0 else "err",
                                      "<b>%s</b> spending is %s%% %s than the month before."
                                      % (month_label(last["month"]), abs(pct), "lower" if pct <= 0 else "higher"),
                                      "trend"), unsafe_allow_html=True)
                with st.expander("Show the numbers"):
                    st.dataframe(mom_df, hide_index=True, **_STRETCH_DF)

        # ----- running total (SUM OVER) -----
        with d:
            with st.container(key="card_running"):
                card_title("Running total", "Cumulative spending, day by day.")
                running_df = pd.read_sql_query("""
                    SELECT date,
                           SUM(amount) AS daily_total,
                           SUM(SUM(amount)) OVER (ORDER BY date) AS running_total
                    FROM transactions
                    GROUP BY date
                    ORDER BY date
                """, conn)
                fig = go.Figure()
                fig.add_scatter(x=pd.to_datetime(running_df["date"]), y=running_df["running_total"], mode="lines",
                                line=dict(color=PRIMARY, width=3, shape="spline", smoothing=0.5),
                                fill="tozeroy", fillcolor="rgba(165,200,255,0.12)",
                                hovertemplate="%{x|%d %b}: ₹%{y:,.0f}<extra></extra>")
                fig.add_scatter(x=[pd.to_datetime(running_df["date"].iloc[-1])],
                                y=[running_df["running_total"].iloc[-1]], mode="markers",
                                marker=dict(color=PRIMARY, size=10, line=dict(color="#101318", width=3)),
                                hoverinfo="skip")
                fig.update_xaxes(tickformat="%d %b")
                show_chart(style_fig(fig, 260, legend=False))
                with st.expander("Show the numbers"):
                    st.dataframe(running_df, hide_index=True, **_STRETCH_DF)

# =====================================================================
# Transactions (WHERE, LIKE, BETWEEN with parameters)
# =====================================================================
with tab_tx:
    if not has_data:
        empty_state("No transactions to show", "Import a CSV from the sidebar to browse and search your data.")
    else:
        with st.container(key="card_filters"):
            f1, f2, f3, f4 = st.columns([2, 1.2, 1.2, 2], gap="medium")
            search_text = f1.text_input("Search description", placeholder="e.g. swiggy", key="search_text")
            date_from = f2.date_input("From", value=None, key="date_from")
            date_to = f3.date_input("To", value=None, key="date_to")
            categories = f4.multiselect("Categories", options=category_names, default=category_names, key="cat_filter")

        query = "SELECT * FROM transaction_details WHERE 1=1"
        params = []
        if search_text:
            query += " AND description LIKE ?"
            params.append("%" + search_text + "%")
        if date_from and date_to:
            query += " AND date BETWEEN ? AND ?"
            params.append(str(date_from))
            params.append(str(date_to))
        elif date_from:
            query += " AND date >= ?"
            params.append(str(date_from))
        elif date_to:
            query += " AND date <= ?"
            params.append(str(date_to))
        query += " ORDER BY date DESC"

        filtered_df = pd.read_sql_query(query, conn, params=params)
        filtered_df = filtered_df[filtered_df["category"].isin(categories)]

        st.markdown(
            '<div class="chips" style="margin:16px 0 12px"><span class="chip">%s%d found</span>'
            '<span class="chip">%sTotal %s</span></div>' % (
                icon("receipt", 16), len(filtered_df), icon("trend", 16), inr(filtered_df["amount"].sum(), 2)),
            unsafe_allow_html=True)
        st.dataframe(
            filtered_df, hide_index=True, height=440,
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "date": st.column_config.TextColumn("Date", width="small"),
                "description": st.column_config.TextColumn("Description", width="large"),
                "amount": st.column_config.NumberColumn("Amount", format="₹ %.2f"),
                "category": st.column_config.TextColumn("Category", width="small"),
            },
            **_STRETCH_DF)

# ---------- Manage tab, second part: the rules table (JOIN) ----------
with tab_manage:
    with st.container(key="card_rules_table"):
        card_title("All category rules", "Rules are checked longest keyword first.")
        rules_df = pd.read_sql_query("""
            SELECT r.keyword, c.name AS category
            FROM category_rules r
            JOIN categories c ON c.id = r.category_id
            ORDER BY c.name, r.keyword
        """, conn)
        chips_html = "".join(
            '<span class="chip" style="margin:0 8px 8px 0"><i class="dotc" style="background:%s;margin:0"></i>%s'
            '<span style="color:var(--muted)">&rarr; %s</span></span>' % (
                cat_color(r["category"]), esc(r["keyword"]), esc(r["category"]))
            for _, r in rules_df.iterrows())
        st.markdown('<div style="display:flex;flex-wrap:wrap">' + chips_html + '</div>', unsafe_allow_html=True)

# =====================================================================
# Database lab: schema, audit log (triggers), index performance test
# =====================================================================
with tab_lab:
    st.markdown('<div class="sec-title" style="margin-top:10px">What is inside the database</div>',
                unsafe_allow_html=True)
    counts = {t: conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
              for t in ["categories", "category_rules", "transactions", "budgets", "audit_log"]}
    st.markdown(
        '<div class="kpis" style="grid-template-columns:repeat(5,minmax(0,1fr))">'
        + "".join(kpi("database", col, name, str(counts[name]), "rows")
                  for name, col in zip(counts, [PRIMARY, "#C3A6FF", MINT, AMBER, "#FF9E9E"]))
        + '</div>', unsafe_allow_html=True)

    lab_a, lab_b = st.columns(2, gap="medium")

    with lab_a:
        with st.container(key="card_audit"):
            card_title("Audit log", "Written by database triggers, not by the app.")
            audit_df = pd.read_sql_query(
                "SELECT id, logged_at, action, tx_id, description, amount, details "
                "FROM audit_log ORDER BY id DESC LIMIT 15", conn)
            if audit_df.empty:
                st.markdown(alert("info", "Nothing logged yet. Import a new CSV, delete a row or re-apply rules."),
                            unsafe_allow_html=True)
            else:
                st.dataframe(audit_df, hide_index=True, height=340, **_STRETCH_DF)

    with lab_b:
        with st.container(key="card_perf"):
            card_title("Index performance test",
                       "100,000 fake rows in a temporary in-memory database. Your data is not touched.")
            if st.button("Run performance test", type="primary", key="run_perf"):
                with st.spinner("Generating 100,000 rows and timing the query..."):
                    st.session_state["perf"] = run_performance_test()
            if "perf" in st.session_state:
                q, plan_b, before_ms, plan_a, after_ms = st.session_state["perf"]
                fig = go.Figure(go.Bar(
                    y=["Without index", "With index"], x=[before_ms, after_ms], orientation="h",
                    marker_color=[AMBER, MINT],
                    text=[str(round(before_ms, 2)) + " ms", str(round(after_ms, 2)) + " ms"],
                    textposition="outside", cliponaxis=False, textfont=dict(color=TEXT, family=FONT),
                    hovertemplate="%{x:.3f} ms<extra></extra>"))
                try:
                    fig.update_traces(marker_cornerradius=8)
                except Exception:
                    pass
                fig = style_fig(fig, 190, legend=False)
                fig.update_xaxes(showticklabels=False, showgrid=False, range=[0, before_ms * 1.3])
                fig.update_yaxes(tickprefix="", gridcolor="rgba(0,0,0,0)", autorange="reversed")
                show_chart(fig)
                st.markdown(alert("ok", "The index made this query <b>%sx faster</b>." % round(before_ms / after_ms, 1),
                                  "trend"), unsafe_allow_html=True)
                st.markdown('<div style="height:10px"></div>', unsafe_allow_html=True)
                with st.expander("Query and execution plans"):
                    st.code(q, language="sql")
                    st.caption("Before the index")
                    st.code("\n".join(plan_b))
                    st.caption("After CREATE INDEX on (category_id, date)")
                    st.code("\n".join(plan_a))

    if has_data:
        with st.expander("Monthly summary (from the monthly_summary view)"):
            st.dataframe(pd.read_sql_query("SELECT * FROM monthly_summary ORDER BY month", conn),
                         hide_index=True, **_STRETCH_DF)

conn.close()
