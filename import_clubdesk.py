"""Import d'un export CSV ClubDesk (journal) dans ZikosCompta.
Usage : python import_clubdesk.py chemin\\export.csv
- R-xxxx -> factures (positions regroupées), Z-xxxx -> date de paiement, B-xxx -> frais
- les factures avec « Ristourne » sont importées comme annulées
- les clients sont créés par nom (adresses à compléter ensuite dans l'app)
"""
import csv
import sqlite3
import sys
from datetime import datetime, timedelta

import app as A

EXP_MAP = {"6210": "carburant", "4450": "transport", "4452": "repas", "6000": "local",
           "1520": "investissement", "1510": "investissement", "6700": "logiciel"}


def iso(d):
    return datetime.strptime(d, "%d.%m.%Y").date().isoformat()


def amount(s):
    return float(s.replace("'", "")) if s else 0.0


def split(text):
    """'Motif (Client)' -> ('Motif', 'Client')"""
    if text.endswith(")") and " (" in text:
        i = text.rfind(" (")
        return text[:i].strip(), text[i + 2:-1].strip()
    return text.strip(), ""


def main(path):
    con = sqlite3.connect(A.DB_PATH)
    con.row_factory = sqlite3.Row
    if con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] or con.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]:
        sys.exit("La base contient déjà des factures/frais : import annulé.")
    with open(path, encoding="cp1252", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))

    invoices, payments, expenses = {}, {}, []
    for r in rows:
        j, typ, text = r["Justificatif"], r["Type"], r["Texte"]
        cr, val = r["Crédit (Numéro)"], amount(r["Montant (CHF)"])
        if j.startswith("Z-"):
            payments[j[2:]] = iso(r["Date"])
        elif j.startswith("B-"):
            expenses.append((j, iso(r["Date"]), text.strip(), EXP_MAP.get(r["Débit (Numéro)"], "autre"), val))
        elif j.startswith("R-"):
            inv = invoices.setdefault(j[2:], {"lines": [], "cancelled": False, "date": None, "title": None, "client": None})
            rist = text.startswith("Ristourne:")
            if rist:
                inv["cancelled"] = True
                continue
            if typ == "Facture (collective)" or (typ == "Facture" and inv["title"] is None):
                inv["date"] = inv["date"] or iso(r["Date"])
                inv["title"], inv["client"] = split(text)
            if typ != "Facture (collective)" and val:
                desc = split(text)[0] if typ == "Facture (position de collecte)" else "Performance musicale"
                cat = "prestation" if cr.startswith("3") else "defraiement"
                inv["lines"].append((desc, val, cat))

    clients = {}
    for num, inv in sorted(invoices.items()):
        name = inv["client"] or "Client inconnu"
        if name not in clients:
            clients[name] = con.execute("INSERT INTO clients (name, country) VALUES (?, 'CH')", (name,)).lastrowid
        paid = payments.get(num)
        status = "annulee" if inv["cancelled"] else ("payee" if paid else "envoyee")
        due = (datetime.fromisoformat(inv["date"]) + timedelta(days=30)).date().isoformat()
        did = con.execute("""INSERT INTO documents (kind, number, client_id, title, doc_date, due_date, intro, status, paid_date)
            VALUES ('facture',?,?,?,?,?,?,?,?)""", (num, clients[name], inv["title"], inv["date"], due,
            A.DEFAULT_SETTINGS["intro_facture"], status, paid if status == "payee" else None)).lastrowid
        for pos, (desc, val, cat) in enumerate(inv["lines"], 1):
            con.execute("INSERT INTO lines (doc_id,pos,description,qty,unit_price,category) VALUES (?,?,?,1,?,?)",
                        (did, pos, desc, val, cat))
    for e in expenses:
        con.execute("INSERT INTO expenses (number,exp_date,description,category,amount) VALUES (?,?,?,?,?)", e)

    last_inv = max(int(n) for n in invoices)
    last_exp = max(int(e[0][2:]) for e in expenses)
    con.execute("UPDATE settings SET value=? WHERE key='next_invoice'", (str(last_inv + 1),))
    con.execute("UPDATE settings SET value=? WHERE key='next_expense'", (str(last_exp + 1),))
    con.commit()
    print(f"{len(clients)} clients, {len(invoices)} factures, {len(payments)} paiements, {len(expenses)} frais importés.")


if __name__ == "__main__":
    main(sys.argv[1])
