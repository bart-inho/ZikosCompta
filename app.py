"""ZikosCompta - compta simple pour musicien indépendant.
Lancer : python app.py  puis ouvrir http://127.0.0.1:5000
"""
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

import segno
from flask import (Flask, abort, g, redirect, render_template, request,
                   send_from_directory, url_for, flash)
from markupsafe import Markup
from werkzeug.utils import secure_filename

BASE = os.environ.get("ZIKOS_HOME") or os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "zikos.db")
RECEIPTS = os.path.join(BASE, "justificatifs")

app = Flask(__name__)
app.secret_key = "zikos-local"

# Catégories volontairement simples
LINE_CATS = {"prestation": "Prestation (revenu)", "defraiement": "Défraiement / frais refacturés"}
EXPENSE_CATS = {
    "transport": "Transports / déplacements",
    "carburant": "Carburant",
    "repas": "Repas",
    "materiel": "Matériel (petit, < 1000 CHF)",
    "investissement": "Matériel durable (investissement)",
    "logiciel": "Logiciels / abonnements",
    "local": "Loyer studio / local",
    "autre": "Autres frais",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, contact TEXT, street TEXT, house_no TEXT,
  zip TEXT, city TEXT, country TEXT DEFAULT 'CH', email TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL,          -- 'facture' | 'devis'
  number TEXT, client_id INTEGER REFERENCES clients(id), title TEXT,
  doc_date TEXT, due_date TEXT, event_date TEXT, intro TEXT,
  status TEXT NOT NULL DEFAULT 'brouillon',            -- facture: brouillon/envoyee/payee/annulee ; devis: brouillon/envoye/accepte/refuse
  paid_date TEXT, source_id INTEGER);
CREATE TABLE IF NOT EXISTS lines (
  id INTEGER PRIMARY KEY, doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  pos INTEGER, description TEXT, qty REAL, unit_price REAL, category TEXT DEFAULT 'prestation');
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY, number TEXT, exp_date TEXT, description TEXT, category TEXT,
  amount REAL, receipt TEXT);
CREATE TABLE IF NOT EXISTS years (year INTEGER PRIMARY KEY, opening_bank REAL DEFAULT 0);
"""

DEFAULT_SETTINGS = {
    # Valeurs neutres : les vraies coordonnées se saisissent dans Réglages (stockées dans data/)
    "name": "Prénom Nom", "street": "Rue", "house_no": "1",
    "zip": "1000", "city": "Lausanne", "iban": "CH00 0000 0000 0000 0000 0",
    "email": "", "phone": "", "payment_days": "30",
    "next_invoice": "1", "next_quote": "1", "next_expense": "1",
    "intro_facture": "Pour le paiement de la facture dans le délai fixé, nous vous remercions d'avance.",
    "intro_devis": "Nous avons le plaisir de vous soumettre l'offre suivante.",
    "closing": "Meilleures salutations",
}


# ---------------------------------------------------------------- DB helpers
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(RECEIPTS, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))
    con.commit()
    con.close()


def settings():
    return {r["key"]: r["value"] for r in db().execute("SELECT * FROM settings")}


def next_number(key):
    s = settings()
    n = int(s[key])
    db().execute("UPDATE settings SET value=? WHERE key=?", (str(n + 1), key))
    return n


def doc_total(doc_id):
    r = db().execute("SELECT COALESCE(SUM(qty*unit_price),0) t FROM lines WHERE doc_id=?", (doc_id,)).fetchone()
    return round(r["t"], 2)


# ---------------------------------------------------------------- filters
@app.template_filter("chf")
def chf(v):
    v = float(v or 0)
    s = f"{abs(v):,.2f}".replace(",", "'")
    return ("-" if v < 0 else "") + s


@app.template_filter("dch")
def dch(v):
    """ISO -> JJ.MM.AAAA"""
    if not v:
        return ""
    try:
        return datetime.strptime(v, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return v


@app.context_processor
def inject():
    return {"LINE_CATS": LINE_CATS, "EXPENSE_CATS": EXPENSE_CATS, "today": date.today().isoformat()}


# ---------------------------------------------------------------- Dashboard
@app.route("/")
def index():
    year = int(request.args.get("year", date.today().year))
    r = report_data(year)
    open_inv = db().execute("""SELECT d.*, c.name cname FROM documents d LEFT JOIN clients c ON c.id=d.client_id
        WHERE kind='facture' AND status='envoyee' ORDER BY due_date""").fetchall()
    open_inv = [dict(d, total=doc_total(d["id"]), late=(d["due_date"] or "9") < date.today().isoformat()) for d in open_inv]
    return render_template("index.html", r=r, year=year, open_inv=open_inv)


# ---------------------------------------------------------------- Clients
@app.route("/clients")
def clients():
    rows = db().execute("SELECT * FROM clients ORDER BY name").fetchall()
    return render_template("clients.html", clients=rows)


@app.route("/clients/new", methods=["GET", "POST"])
@app.route("/clients/<int:cid>", methods=["GET", "POST"])
def client_edit(cid=None):
    c = db().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone() if cid else None
    if request.method == "POST":
        f = request.form
        vals = [f.get(k, "").strip() for k in ("name", "contact", "street", "house_no", "zip", "city", "country", "email")]
        if cid:
            db().execute("UPDATE clients SET name=?,contact=?,street=?,house_no=?,zip=?,city=?,country=?,email=? WHERE id=?", vals + [cid])
        else:
            db().execute("INSERT INTO clients (name,contact,street,house_no,zip,city,country,email) VALUES (?,?,?,?,?,?,?,?)", vals)
        db().commit()
        return redirect(request.args.get("next") or url_for("clients"))
    return render_template("client_form.html", c=c)


# ---------------------------------------------------------------- Documents (factures / devis)
@app.route("/<kind>s")
def doc_list(kind):
    if kind not in ("facture", "devis"):
        abort(404)
    year = request.args.get("year", str(date.today().year))
    rows = db().execute("""SELECT d.*, c.name cname FROM documents d LEFT JOIN clients c ON c.id=d.client_id
        WHERE kind=? AND substr(doc_date,1,4)=? ORDER BY doc_date DESC, id DESC""", (kind, year)).fetchall()
    rows = [dict(d, total=doc_total(d["id"])) for d in rows]
    return render_template("doc_list.html", docs=rows, kind=kind, year=year)


@app.route("/doc/new/<kind>", methods=["GET", "POST"])
@app.route("/doc/<int:did>/edit", methods=["GET", "POST"])
def doc_edit(kind=None, did=None):
    d = db().execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone() if did else None
    if d:
        kind = d["kind"]
        if d["status"] == "payee":
            flash("Facture payée : elle n'est plus modifiable.")
            return redirect(url_for("doc_view", did=did))
    s = settings()
    if request.method == "POST":
        f = request.form
        fields = (f["client_id"] or None, f["title"].strip(), f["doc_date"], f.get("due_date") or None,
                  f.get("event_date") or None, f.get("intro", ""))
        if d:
            db().execute("UPDATE documents SET client_id=?,title=?,doc_date=?,due_date=?,event_date=?,intro=? WHERE id=?", fields + (did,))
        else:
            cur = db().execute("INSERT INTO documents (kind,client_id,title,doc_date,due_date,event_date,intro) VALUES (?,?,?,?,?,?,?)", (kind,) + fields)
            did = cur.lastrowid
        db().execute("DELETE FROM lines WHERE doc_id=?", (did,))
        pos = 0
        for desc, qty, price, cat in zip(f.getlist("l_desc"), f.getlist("l_qty"), f.getlist("l_price"), f.getlist("l_cat")):
            if not desc.strip():
                continue
            pos += 1
            db().execute("INSERT INTO lines (doc_id,pos,description,qty,unit_price,category) VALUES (?,?,?,?,?,?)",
                         (did, pos, desc.strip(), float(qty or 1), float(price or 0), cat))
        db().commit()
        return redirect(url_for("doc_view", did=did))
    lines = db().execute("SELECT * FROM lines WHERE doc_id=? ORDER BY pos", (did,)).fetchall() if did else []
    today = date.today()
    defaults = {"doc_date": today.isoformat(),
                "due_date": (today + timedelta(days=int(s["payment_days"]))).isoformat(),
                "intro": s["intro_" + kind]}
    cl = db().execute("SELECT * FROM clients ORDER BY name").fetchall()
    return render_template("doc_form.html", d=d, kind=kind, lines=lines, clients=cl, defaults=defaults)


@app.route("/doc/<int:did>")
def doc_view(did):
    d, c, lines = load_doc(did)
    return render_template("doc_view.html", d=d, c=c, lines=lines, total=doc_total(did))


def load_doc(did):
    d = db().execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone() or abort(404)
    c = db().execute("SELECT * FROM clients WHERE id=?", (d["client_id"],)).fetchone()
    lines = db().execute("SELECT * FROM lines WHERE doc_id=? ORDER BY pos", (did,)).fetchall()
    return d, c, lines


@app.route("/doc/<int:did>/action", methods=["POST"])
def doc_action(did):
    d = db().execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone() or abort(404)
    a = request.form["action"]
    if a == "emettre":  # attribue le numéro définitif
        if not d["number"]:
            key = "next_invoice" if d["kind"] == "facture" else "next_quote"
            prefix = "" if d["kind"] == "facture" else "D-"
            db().execute("UPDATE documents SET number=? WHERE id=?", (f"{prefix}{next_number(key)}", did))
        db().execute("UPDATE documents SET status=? WHERE id=?", ("envoyee" if d["kind"] == "facture" else "envoye", did))
    elif a == "payee":
        db().execute("UPDATE documents SET status='payee', paid_date=? WHERE id=?", (request.form.get("paid_date") or date.today().isoformat(), did))
    elif a == "impayee":
        db().execute("UPDATE documents SET status='envoyee', paid_date=NULL WHERE id=?", (did,))
    elif a == "annuler":
        db().execute("UPDATE documents SET status='annulee' WHERE id=?", (did,))
    elif a in ("accepte", "refuse"):
        db().execute("UPDATE documents SET status=? WHERE id=?", (a, did))
    elif a == "supprimer" and d["status"] == "brouillon":
        db().execute("DELETE FROM documents WHERE id=?", (did,))
        db().commit()
        return redirect(url_for("doc_list", kind=d["kind"]))
    elif a == "to_facture":  # devis -> nouvelle facture brouillon
        s = settings()
        today = date.today()
        cur = db().execute("""INSERT INTO documents (kind,client_id,title,doc_date,due_date,event_date,intro,source_id)
            VALUES ('facture',?,?,?,?,?,?,?)""", (d["client_id"], d["title"], today.isoformat(),
            (today + timedelta(days=int(s["payment_days"]))).isoformat(), d["event_date"], s["intro_facture"], did))
        new = cur.lastrowid
        db().execute("""INSERT INTO lines (doc_id,pos,description,qty,unit_price,category)
            SELECT ?,pos,description,qty,unit_price,category FROM lines WHERE doc_id=?""", (new, did))
        db().execute("UPDATE documents SET status='accepte' WHERE id=?", (did,))
        db().commit()
        flash("Facture créée à partir du devis : vérifie et modifie les lignes si besoin.")
        return redirect(url_for("doc_edit", did=new))
    db().commit()
    return redirect(url_for("doc_view", did=did))


@app.route("/doc/<int:did>/print")
def doc_print(did):
    d, c, lines = load_doc(did)
    s = settings()
    total = doc_total(did)
    qr = None
    if d["kind"] == "facture" and total > 0:
        qr = Markup(qr_svg(qr_payload(s, c, d, total)))
    return render_template("doc_print.html", d=d, c=c, lines=lines, total=total, s=s, qr=qr)


# ---------------------------------------------------------------- QR-facture suisse
def qr_payload(s, c, d, total):
    info = f"N° de facture: {d['number'] or ''} {d['title'] or ''}".strip()[:140]
    c = c or {}
    g_ = lambda k: (c[k] if c and c[k] else "")
    lines = [
        "SPC", "0200", "1", s["iban"].replace(" ", ""),
        "S", s["name"][:70], s["street"][:70], s["house_no"][:16], s["zip"][:16], s["city"][:35], "CH",
        "", "", "", "", "", "", "",
        f"{total:.2f}", "CHF",
    ]
    if c and g_("name"):
        lines += ["S", g_("name")[:70], g_("street")[:70], g_("house_no")[:16], g_("zip")[:16], g_("city")[:35], g_("country") or "CH"]
    else:
        lines += ["", "", "", "", "", "", ""]
    lines += ["NON", "", info, "EPD"]
    return "\r\n".join(lines)


def qr_svg(payload):
    q = segno.make(payload, error="m", micro=False, encoding="utf-8")
    svg = q.svg_inline(scale=1, border=0, dark="#000")
    n = q.symbol_size(scale=1, border=0)[0]
    # croix suisse (7mm sur 46mm) superposée
    cs = n * 7 / 46
    o = (n - cs) / 2
    u = cs / 7  # unité
    cross = (f'<rect x="{o}" y="{o}" width="{cs}" height="{cs}" fill="#fff"/>'
             f'<rect x="{o+u*0.5}" y="{o+u*0.5}" width="{u*6}" height="{u*6}" fill="#000"/>'
             f'<rect x="{o+u*3}" y="{o+u*1.6}" width="{u}" height="{u*3.8}" fill="#fff"/>'
             f'<rect x="{o+u*1.6}" y="{o+u*3}" width="{u*3.8}" height="{u}" fill="#fff"/>')
    svg = re.sub(r'width="\d+" height="\d+"', f'width="46mm" height="46mm" viewBox="0 0 {n} {n}"', svg, count=1)
    return svg.replace("</svg>", cross + "</svg>")


# ---------------------------------------------------------------- Frais
@app.route("/frais")
def expenses():
    year = request.args.get("year", str(date.today().year))
    rows = db().execute("SELECT * FROM expenses WHERE substr(exp_date,1,4)=? ORDER BY exp_date DESC, id DESC", (year,)).fetchall()
    return render_template("expenses.html", rows=rows, year=year, total=sum(r["amount"] for r in rows))


@app.route("/frais/new", methods=["GET", "POST"])
@app.route("/frais/<int:eid>", methods=["GET", "POST"])
def expense_edit(eid=None):
    e = db().execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone() if eid else None
    if request.method == "POST":
        f = request.form
        if f.get("delete") and e:
            db().execute("DELETE FROM expenses WHERE id=?", (eid,))
            db().commit()
            flash("Frais supprimé (le justificatif reste dans le dossier).")
            return redirect(url_for("expenses"))
        number = e["number"] if e else f"B-{next_number('next_expense'):03d}"
        receipt = e["receipt"] if e else None
        file = request.files.get("receipt")
        if file and file.filename:
            y, m = f["exp_date"][:4], f["exp_date"][5:7]
            folder = os.path.join(RECEIPTS, y, m)
            os.makedirs(folder, exist_ok=True)
            ext = os.path.splitext(file.filename)[1].lower()
            base = secure_filename(f"{f['exp_date']}_{number}_{f['description'][:40]}") or number
            receipt = f"{y}/{m}/{base}{ext}"
            file.save(os.path.join(RECEIPTS, receipt))
        vals = (number, f["exp_date"], f["description"].strip(), f["category"], float(f["amount"]), receipt)
        if e:
            db().execute("UPDATE expenses SET number=?,exp_date=?,description=?,category=?,amount=?,receipt=? WHERE id=?", vals + (eid,))
        else:
            db().execute("INSERT INTO expenses (number,exp_date,description,category,amount,receipt) VALUES (?,?,?,?,?,?)", vals)
        db().commit()
        flash(f"Frais {number} enregistré.")
        return redirect(url_for("expense_edit") if f.get("again") else url_for("expenses"))
    return render_template("expense_form.html", e=e)


@app.route("/justificatifs/<path:p>")
def receipt(p):
    return send_from_directory(RECEIPTS, p)


# ---------------------------------------------------------------- Rapports
def report_data(year):
    y = str(year)
    row = db().execute("SELECT opening_bank FROM years WHERE year=?", (year,)).fetchone()
    opening = row["opening_bank"] if row else 0.0
    # Revenus = factures payées dans l'année (comptabilité de caisse)
    inc = db().execute("""SELECT l.category, SUM(l.qty*l.unit_price) t FROM lines l JOIN documents d ON d.id=l.doc_id
        WHERE d.kind='facture' AND d.status='payee' AND substr(d.paid_date,1,4)=? GROUP BY l.category""", (y,)).fetchall()
    income = {k: 0.0 for k in LINE_CATS}
    for r in inc:
        income[r["category"]] = r["t"]
    exp = db().execute("SELECT category, SUM(amount) t FROM expenses WHERE substr(exp_date,1,4)=? GROUP BY category", (y,)).fetchall()
    costs = {k: 0.0 for k in EXPENSE_CATS}
    for r in exp:
        costs[r["category"]] = r["t"]
    invest = costs.pop("investissement")
    invest_before = db().execute("SELECT COALESCE(SUM(amount),0) t FROM expenses WHERE category='investissement' AND substr(exp_date,1,4)<?", (y,)).fetchone()["t"]
    total_income = sum(income.values())
    total_costs = sum(costs.values())
    result = total_income - total_costs
    bank = opening + total_income - total_costs - invest
    debtors = 0.0
    for d in db().execute("SELECT id FROM documents WHERE kind='facture' AND status='envoyee' AND substr(doc_date,1,4)<=?", (y,)):
        debtors += doc_total(d["id"])
    equipment = invest_before + invest
    capital = opening + invest_before
    return dict(year=year, opening=opening, income=income, costs=costs, invest=invest,
                total_income=total_income, total_costs=total_costs, result=result, bank=bank,
                equipment=equipment, capital=capital, debtors=debtors,
                total_assets=bank + equipment, total_liab=capital + result)


@app.route("/rapport")
def report():
    year = int(request.args.get("year", date.today().year))
    return render_template("report.html", r=report_data(year), s=settings(), printed=date.today().isoformat())


# ---------------------------------------------------------------- Réglages
@app.route("/reglages", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        for k in DEFAULT_SETTINGS:
            if k in request.form:
                db().execute("UPDATE settings SET value=? WHERE key=?", (request.form[k].strip(), k))
        if request.form.get("year"):
            db().execute("INSERT INTO years VALUES (?,?) ON CONFLICT(year) DO UPDATE SET opening_bank=excluded.opening_bank",
                         (int(request.form["year"]), float(request.form.get("opening_bank") or 0)))
        db().commit()
        flash("Réglages enregistrés.")
        return redirect(url_for("settings_page"))
    yrs = db().execute("SELECT * FROM years ORDER BY year DESC").fetchall()
    return render_template("settings.html", s=settings(), years=yrs)


init_db()

if __name__ == "__main__":
    import webbrowser
    webbrowser.open("http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
