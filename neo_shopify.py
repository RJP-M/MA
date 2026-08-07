#!/usr/bin/env python3
"""
neo_shopify.py — Onlineshop-Kennzahlen direkt aus Shopify.

Hintergrund: Die NEO-Warenwirtschaft führt den Webshop zwar als Kanal, die
Zahlen sind dort aber unvollständig (bei uns brechen sie ab März ab). Dieses
Modul holt die Onlineshop-Daten deshalb direkt bei Shopify — inklusive
Besucherzahlen, Conversion und abgebrochenen Warenkörben, die NEO gar nicht
kennt.

Ausschließlich Python-Standardbibliothek, wie der Rest des Projekts.

Zugang über Umgebungsvariablen (nie im Code):
    SHOPIFY_SHOP           z. B. meinshop.myshopify.com
    SHOPIFY_CLIENT_ID      Client-ID der App aus dem Dev-Dashboard
    SHOPIFY_CLIENT_SECRET  Schlüssel der App (shpss_…)
    SHOPIFY_API_VERSION    optional, Standard siehe unten

Seit Januar 2026 vergibt Shopify keine dauerhaften Admin-Token mehr. Der Server
tauscht stattdessen Client-ID und Schlüssel selbst gegen ein Zugriffstoken
(Client-Credentials-Verfahren); das Token gilt 24 Stunden und wird hier
automatisch erneuert.

Ältere Zugänge mit festem Token funktionieren weiterhin:
    SHOPIFY_TOKEN   Admin-API-Token (shpat_…)
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

API_VERSION_STD = "2026-07"
TIMEOUT = 30

# Zeitstempel in deutscher Zeit (der Server läuft z. B. auf Render in UTC)
try:
    from zoneinfo import ZoneInfo
    _TZ_DE = ZoneInfo("Europe/Berlin")
except Exception:                                       # noqa: BLE001
    _TZ_DE = None


def _zeitstempel():
    n = datetime.now(_TZ_DE) if _TZ_DE else datetime.now()
    return n.strftime("%Y-%m-%d %H:%M:%S")


class ShopifyFehler(Exception):
    pass


def _shop_adresse():
    shop = (os.environ.get("SHOPIFY_SHOP") or "").strip()
    shop = shop.replace("https://", "").replace("http://", "").strip("/")
    # „meinshop“ genügt, „.myshopify.com“ wird ergänzt
    if shop and "." not in shop:
        shop += ".myshopify.com"
    return shop


def konfiguriert():
    if not _shop_adresse():
        return False
    if os.environ.get("SHOPIFY_TOKEN"):
        return True
    return bool(os.environ.get("SHOPIFY_CLIENT_ID")
                and os.environ.get("SHOPIFY_CLIENT_SECRET"))


# Zwischenspeicher für das selbst geholte Token (gilt 24 Stunden)
_TOKEN = {"wert": None, "gueltig_bis": 0.0}


def _token_holen():
    """Client-ID und Schlüssel gegen ein Zugriffstoken tauschen."""
    shop = _shop_adresse()
    cid = (os.environ.get("SHOPIFY_CLIENT_ID") or "").strip()
    secret = (os.environ.get("SHOPIFY_CLIENT_SECRET") or "").strip()

    if _TOKEN["wert"] and time.time() < _TOKEN["gueltig_bis"] - 60:
        return _TOKEN["wert"]

    daten = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
    }).encode("utf-8")
    url = "https://%s/admin/oauth/access_token" % shop
    req = urllib.request.Request(url, data=daten, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            antwort = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        rumpf = ""
        try:
            rumpf = e.read().decode("utf-8", "replace")[:300]
        except Exception:                                       # noqa: BLE001
            pass
        if "shop_not_permitted" in rumpf:
            raise ShopifyFehler(
                "Shopify verweigert den Tausch (shop_not_permitted): App und Shop "
                "müssen in derselben Organisation im Dev-Dashboard liegen.")
        if e.code in (400, 401):
            raise ShopifyFehler("Client-ID oder Schlüssel stimmen nicht (HTTP %d). %s"
                                % (e.code, rumpf))
        raise ShopifyFehler("Token-Abruf fehlgeschlagen (HTTP %d). %s" % (e.code, rumpf))
    except urllib.error.URLError as e:
        raise ShopifyFehler("Shopify ist nicht erreichbar: %s" % e.reason)

    tok = antwort.get("access_token")
    if not tok:
        raise ShopifyFehler("Shopify hat kein Token zurückgegeben.")
    _TOKEN["wert"] = tok
    _TOKEN["gueltig_bis"] = time.time() + float(antwort.get("expires_in") or 86399)
    return tok


def _zugang():
    shop = _shop_adresse()
    if not shop:
        raise ShopifyFehler(
            "Shopify ist noch nicht verbunden. Bitte SHOPIFY_SHOP in den "
            "Servereinstellungen hinterlegen.")
    version = (os.environ.get("SHOPIFY_API_VERSION") or API_VERSION_STD).strip()

    festes = (os.environ.get("SHOPIFY_TOKEN") or "").strip()
    if festes:
        return shop, festes, version          # älterer Zugang mit festem Token
    if not (os.environ.get("SHOPIFY_CLIENT_ID")
            and os.environ.get("SHOPIFY_CLIENT_SECRET")):
        raise ShopifyFehler(
            "Shopify ist noch nicht verbunden. Bitte SHOPIFY_CLIENT_ID und "
            "SHOPIFY_CLIENT_SECRET in den Servereinstellungen hinterlegen.")
    return shop, _token_holen(), version


def graphql(query, variables=None):
    """Eine GraphQL-Abfrage an die Shopify-Admin-API."""
    shop, token, version = _zugang()
    url = "https://%s/admin/api/%s/graphql.json" % (shop, version)
    daten = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(url, data=daten, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Shopify-Access-Token", token)
    req.add_header("Accept", "application/json")

    for versuch in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                antwort = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            rumpf = ""
            try:
                rumpf = e.read().decode("utf-8", "replace")[:300]
            except Exception:                                  # noqa: BLE001
                pass
            if e.code == 429 and versuch < 2:                  # gedrosselt
                time.sleep(2 * (versuch + 1))
                continue
            if e.code == 401:
                # Selbst geholtes Token evtl. abgelaufen: einmal neu holen
                if not os.environ.get("SHOPIFY_TOKEN") and versuch < 2:
                    _TOKEN["wert"] = None
                    _TOKEN["gueltig_bis"] = 0.0
                    shop, token, version = _zugang()
                    req = urllib.request.Request(url, data=daten, method="POST")
                    req.add_header("Content-Type", "application/json")
                    req.add_header("X-Shopify-Access-Token", token)
                    req.add_header("Accept", "application/json")
                    continue
                raise ShopifyFehler("Shopify lehnt den Zugang ab (401). "
                                    "Stimmen Client-ID und Schlüssel noch?")
            if e.code == 403:
                raise ShopifyFehler("Shopify verweigert den Zugriff (403). "
                                    "Fehlt der App eine Berechtigung, etwa "
                                    "read_reports oder read_orders?")
            if e.code == 404:
                raise ShopifyFehler("Shopify-Adresse oder API-Version stimmt "
                                    "nicht (404): %s" % url)
            raise ShopifyFehler("Shopify antwortet mit HTTP %d. %s" % (e.code, rumpf))
        except urllib.error.URLError as e:
            if versuch < 2:
                time.sleep(1.5 * (versuch + 1))
                continue
            raise ShopifyFehler("Shopify ist nicht erreichbar: %s" % e.reason)
    else:                                                       # pragma: no cover
        raise ShopifyFehler("Shopify ist nicht erreichbar.")

    if antwort.get("errors"):
        erste = antwort["errors"][0]
        raise ShopifyFehler("Shopify meldet: %s" %
                            (erste.get("message") if isinstance(erste, dict) else erste))
    return antwort.get("data") or {}


def shopifyql(abfrage):
    """ShopifyQL-Analytics. Gibt eine Liste von Zeilen als Dicts zurück."""
    d = graphql("""
        query($q: String!) {
          shopifyqlQuery(query: $q) {
            parseErrors
            tableData { columns { name dataType } rows }
          }
        }""", {"q": abfrage})
    res = (d.get("shopifyqlQuery") or {})
    fehler = res.get("parseErrors")
    if fehler:
        raise ShopifyFehler("Analytics-Abfrage abgelehnt: %s" % fehler)
    tab = res.get("tableData") or {}
    zeilen = tab.get("rows") or []
    # rows kommt als JSON-Skalar: Liste von Objekten {spalte: wert}
    if zeilen and isinstance(zeilen[0], list):
        namen = [c["name"] for c in (tab.get("columns") or [])]
        zeilen = [dict(zip(namen, z)) for z in zeilen]
    return zeilen


# ----------------------------------------------------------------- Hilfsfunktionen
def _z(w):
    """Wert robust in eine Zahl wandeln (Shopify liefert oft Strings)."""
    if w is None or w == "":
        return 0.0
    try:
        return float(w)
    except (TypeError, ValueError):
        return 0.0


def _i(w):
    return int(_z(w))


def _tag(zeile):
    """Datum aus einer Zeitreihen-Zeile ziehen (Shopify nennt die Spalte 'day')."""
    for k in ("day", "week", "month", "hour"):
        if k in zeile:
            return str(zeile[k])[:10]
    return None


# ============================================================================
# Speicherung
# ----------------------------------------------------------------------------
# Wie bei der NEO-Warenwirtschaft werden die Zahlen einmal taeglich abgeholt und
# in der lokalen Datenbank abgelegt. Das Dashboard liest danach nur noch von
# dort: sofort da, unabhaengig davon ob Shopify gerade erreichbar ist, und ohne
# bei jedem Seitenaufruf erneut anzufragen.
# ============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS shop_tag(
  datum TEXT PRIMARY KEY,
  umsatz REAL, brutto REAL, rabatte REAL, retouren REAL, netto REAL,
  bestellungen INTEGER, stueck INTEGER,
  besuche INTEGER, besucher INTEGER,
  warenkorb INTEGER, checkout INTEGER, kaeufe INTEGER,
  kunden INTEGER, stammkunden INTEGER
);
CREATE TABLE IF NOT EXISTS shop_dim_tag(
  datum TEXT, art TEXT, name TEXT,
  umsatz REAL, bestellungen INTEGER, besuche INTEGER,
  PRIMARY KEY(datum, art, name)
);
CREATE TABLE IF NOT EXISTS shop_warenkorb(
  id TEXT PRIMARY KEY, datum TEXT, wert REAL, artikel TEXT
);
CREATE INDEX IF NOT EXISTS idx_shop_dim ON shop_dim_tag(art, datum);
CREATE INDEX IF NOT EXISTS idx_shop_wk ON shop_warenkorb(datum);
"""


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def _meta_setzen(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def _meta_lesen(con, k, standard=None):
    try:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    except Exception:                                       # noqa: BLE001
        return standard
    return r["v"] if r else standard


def stand(con):
    """Wann wurde zuletzt abgerufen und welcher Zeitraum liegt vor?"""
    try:
        r = con.execute("SELECT MIN(datum) a, MAX(datum) b, COUNT(*) n "
                        "FROM shop_tag").fetchone()
    except Exception:                                       # noqa: BLE001
        return {"tage": 0, "von": None, "bis": None, "sync": None}
    return {"tage": r["n"] or 0, "von": r["a"], "bis": r["b"],
            "sync": _meta_lesen(con, "shopify_sync"),
            "fehler": _meta_lesen(con, "shopify_sync_fehler") or None}


# ------------------------------------------------------------------ Abholen
def _reihe(abfrage):
    """Zeitreihe als {datum: zeile}."""
    raus = {}
    for z in shopifyql(abfrage):
        t = _tag(z)
        if t:
            raus[t] = z
    return raus


def _dim_reihe(abfrage, feld):
    """Aufschluesselung je Tag als Liste (datum, name, zeile)."""
    raus = []
    for z in shopifyql(abfrage):
        t = _tag(z)
        if not t:
            continue
        name = (z.get(feld) or "").strip() or "Direkt / unbekannt"
        raus.append((t, name, z))
    return raus


CHUNK_TAGE = 31


def sync(con, von, bis):
    """Holt alle Onlineshop-Zahlen des Zeitraums und legt sie in der Datenbank ab.

    Lange Zeitraeume werden in Monatsstuecke geteilt: ShopifyQL kappt grosse
    Antworten bei rund 1000 Zeilen. Ein 2-Jahres-Erstabruf der Aufschluesselungen
    (Produkt je Tag!) kaeme sonst stillschweigend unvollstaendig zurueck — und
    weil vor dem Einfuegen der ganze Bereich geloescht wurde, gingen dabei sogar
    vorhandene Daten verloren."""
    d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
    gesamt = 0
    a = d0
    while a <= d1:
        b = min(a + timedelta(days=CHUNK_TAGE - 1), d1)
        gesamt += _sync_zeitraum(con, a.isoformat(), b.isoformat())
        a = b + timedelta(days=1)
    if gesamt:
        # Nur bei tatsaechlich gespeicherten Tagen den Zeitstempel setzen —
        # sonst zeigt das Dashboard "frisch abgerufen", obwohl nichts kam.
        _meta_setzen(con, "shopify_sync", _zeitstempel())
        con.commit()
    return gesamt


def _sync_zeitraum(con, von, bis):
    """Ein Teilzeitraum (hoechstens CHUNK_TAGE Tage) in die Datenbank."""
    verkauf = _reihe("FROM sales SHOW orders, gross_sales, discounts, returns, "
                     "net_sales, total_sales TIMESERIES day SINCE %s UNTIL %s" % (von, bis))
    sitzung = _reihe("FROM sessions SHOW sessions, online_store_visitors, "
                     "sessions_with_cart_additions, sessions_that_reached_checkout, "
                     "sessions_that_completed_checkout TIMESERIES day "
                     "SINCE %s UNTIL %s" % (von, bis))
    try:
        stueck = _reihe("FROM inventory SHOW inventory_units_sold TIMESERIES day "
                        "SINCE %s UNTIL %s" % (von, bis))
    except ShopifyFehler:
        stueck = {}
    try:
        kunden = _reihe("FROM sales SHOW customers, returning_customers "
                        "TIMESERIES day SINCE %s UNTIL %s" % (von, bis))
    except ShopifyFehler:
        kunden = {}

    tage = set(verkauf) | set(sitzung) | set(stueck) | set(kunden)
    for t in sorted(tage):
        v, s = verkauf.get(t, {}), sitzung.get(t, {})
        k = kunden.get(t, {})
        con.execute("""INSERT INTO shop_tag(datum,umsatz,brutto,rabatte,retouren,netto,
                         bestellungen,stueck,besuche,besucher,warenkorb,checkout,kaeufe,
                         kunden,stammkunden)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(datum) DO UPDATE SET
                         umsatz=excluded.umsatz, brutto=excluded.brutto,
                         rabatte=excluded.rabatte, retouren=excluded.retouren,
                         netto=excluded.netto, bestellungen=excluded.bestellungen,
                         stueck=excluded.stueck, besuche=excluded.besuche,
                         besucher=excluded.besucher, warenkorb=excluded.warenkorb,
                         checkout=excluded.checkout, kaeufe=excluded.kaeufe,
                         kunden=excluded.kunden, stammkunden=excluded.stammkunden""",
                    (t, _z(v.get("total_sales")), _z(v.get("gross_sales")),
                     abs(_z(v.get("discounts"))), abs(_z(v.get("returns"))),
                     _z(v.get("net_sales")), _i(v.get("orders")),
                     _i(stueck.get(t, {}).get("inventory_units_sold")),
                     _i(s.get("sessions")), _i(s.get("online_store_visitors")),
                     _i(s.get("sessions_with_cart_additions")),
                     _i(s.get("sessions_that_reached_checkout")),
                     _i(s.get("sessions_that_completed_checkout")),
                     _i(k.get("customers")), _i(k.get("returning_customers"))))

    # Aufschluesselungen: Produkte, Herkunft, Geraete, Laender
    aufteilungen = (
        ("produkt", "product_title",
         "FROM sales SHOW gross_sales, orders GROUP BY product_title "
         "TIMESERIES day SINCE %s UNTIL %s" % (von, bis), "umsatz"),
        ("quelle", "order_referrer_source",
         "FROM sales SHOW total_sales, orders GROUP BY order_referrer_source "
         "TIMESERIES day SINCE %s UNTIL %s" % (von, bis), "umsatz"),
        ("geraet", "session_device_type",
         "FROM sessions SHOW sessions GROUP BY session_device_type "
         "TIMESERIES day SINCE %s UNTIL %s" % (von, bis), "besuche"),
        ("land", "session_country",
         "FROM sessions SHOW sessions GROUP BY session_country "
         "TIMESERIES day SINCE %s UNTIL %s" % (von, bis), "besuche"),
    )
    for art, feld, abfrage, _ in aufteilungen:
        try:
            zeilen = _dim_reihe(abfrage, feld)
        except ShopifyFehler:
            continue
        con.execute("DELETE FROM shop_dim_tag WHERE art=? AND datum BETWEEN ? AND ?",
                    (art, von, bis))
        for t, name, z in zeilen:
            umsatz = _z(z.get("gross_sales") if "gross_sales" in z else z.get("total_sales"))
            con.execute("""INSERT INTO shop_dim_tag(datum,art,name,umsatz,bestellungen,besuche)
                           VALUES(?,?,?,?,?,?)
                           ON CONFLICT(datum,art,name) DO UPDATE SET
                             umsatz=excluded.umsatz,
                             bestellungen=excluded.bestellungen,
                             besuche=excluded.besuche""",
                        (t, art, name, umsatz, _i(z.get("orders")), _i(z.get("sessions"))))

    # Liegengebliebene Warenkoerbe
    try:
        for w in _warenkoerbe_holen(von, bis):
            if w.get("abgeschlossen"):
                # Spaeter doch gekauft: alten "abgebrochen"-Eintrag entfernen,
                # sonst zaehlt er fuer immer als verlorener Umsatz.
                con.execute("DELETE FROM shop_warenkorb WHERE id=?", (w["id"],))
                continue
            con.execute("INSERT INTO shop_warenkorb(id,datum,wert,artikel) VALUES(?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET datum=excluded.datum, "
                        "wert=excluded.wert, artikel=excluded.artikel",
                        (w["id"], w["datum"], w["wert"], w["artikel"]))
    except ShopifyFehler:
        pass

    con.commit()
    return len(tage)


def _warenkoerbe_holen(von, bis, seiten=None):
    """Abgebrochene Checkouts, seitenweise (100 je Seite)."""
    if seiten is None:
        # Seitenzahl an die Zeitraumlaenge koppeln; frueher war bei 500 Stueck
        # Schluss, was lange Erstabrufe still abschnitt.
        tage_n = (date.fromisoformat(bis) - date.fromisoformat(von)).days + 1
        seiten = max(5, min(30, tage_n // 7 + 1))
    # created_at:<=YYYY-MM-DD meint Mitternacht — der letzte Tag fiele weg.
    # Deshalb exklusiv bis zum Folgetag filtern.
    tag_danach = (date.fromisoformat(bis) + timedelta(days=1)).isoformat()
    raus, cursor = [], None
    for _ in range(seiten):
        d = graphql("""
            query($q: String!, $n: Int!, $c: String) {
              abandonedCheckouts(first: $n, query: $q, after: $c) {
                edges { cursor node {
                  id createdAt completedAt
                  totalPriceSet { shopMoney { amount } }
                  lineItems(first: 5) { edges { node { title } } }
                } }
                pageInfo { hasNextPage endCursor }
              }
            }""", {"q": "created_at:>=%s created_at:<%s" % (von, tag_danach),
                   "n": 100, "c": cursor})
        block = d.get("abandonedCheckouts") or {}
        for k in (block.get("edges") or []):
            n = k.get("node") or {}
            artikel = [(e.get("node") or {}).get("title")
                       for e in ((n.get("lineItems") or {}).get("edges") or [])]
            raus.append({
                "id": n.get("id"),
                "datum": (n.get("createdAt") or "")[:10],
                "wert": _z(((n.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount")),
                "artikel": ", ".join(a for a in artikel if a),
                "abgeschlossen": bool(n.get("completedAt")),
            })
        seite = block.get("pageInfo") or {}
        if not seite.get("hasNextPage"):
            break
        cursor = seite.get("endCursor")
    return raus


# --------------------------------------------------------------- Auswertungen
def _summe(con, von, bis):
    r = con.execute("""SELECT COALESCE(SUM(umsatz),0) umsatz, COALESCE(SUM(brutto),0) brutto,
                         COALESCE(SUM(rabatte),0) rabatte, COALESCE(SUM(retouren),0) retouren,
                         COALESCE(SUM(netto),0) netto, COALESCE(SUM(bestellungen),0) bestellungen,
                         COALESCE(SUM(stueck),0) stueck, COALESCE(SUM(besuche),0) besuche,
                         COALESCE(SUM(besucher),0) besucher, COALESCE(SUM(warenkorb),0) warenkorb,
                         COALESCE(SUM(checkout),0) checkout, COALESCE(SUM(kaeufe),0) kaeufe,
                         COALESCE(SUM(kunden),0) kunden, COALESCE(SUM(stammkunden),0) stammkunden,
                         COUNT(*) tage
                       FROM shop_tag WHERE datum BETWEEN ? AND ?""", (von, bis)).fetchone()
    return dict(r) if r else {}


def hat_daten(con, von=None, bis=None):
    try:
        if von and bis:
            r = con.execute("SELECT COUNT(*) n FROM shop_tag WHERE datum BETWEEN ? AND ?",
                            (von, bis)).fetchone()
        else:
            r = con.execute("SELECT COUNT(*) n FROM shop_tag").fetchone()
        return bool(r and r["n"])
    except Exception:                                       # noqa: BLE001
        return False


def umsatz_perioden(con, von, bis, gran="monat"):
    """Onlineumsatz je Periode, beschriftet wie die NEO-Zeitreihe."""
    ausdruck = {"tag": "datum",
                "woche": "strftime('%Y-KW%W', datum)",
                "monat": "substr(datum,1,7)"}.get(gran, "substr(datum,1,7)")
    raus = {}
    for r in con.execute("SELECT %s AS p, SUM(umsatz) s FROM shop_tag "
                         "WHERE datum BETWEEN ? AND ? GROUP BY p" % ausdruck, (von, bis)):
        raus[r["p"]] = r["s"] or 0.0
    return raus


def filialzeile(con, von, bis):
    """Kennzahlen des Onlineshops fuer den Filialvergleich."""
    s = _summe(con, von, bis)
    if not s or not s.get("bestellungen"):
        if not s or not s.get("umsatz"):
            return None
    b = s.get("bestellungen") or 0
    return {"brutto": s.get("umsatz") or 0.0, "belege": b, "stueck": s.get("stueck") or 0,
            "bonwert": ((s["umsatz"] / b) if b else None),
            "stueckProBeleg": ((s["stueck"] / b) if b else None)}


def _dim(con, von, bis, art, wert="umsatz", limit=10):
    spalte = "SUM(umsatz)" if wert == "umsatz" else "SUM(besuche)"
    zeilen = []
    for r in con.execute(
            "SELECT name, SUM(umsatz) u, SUM(bestellungen) b, SUM(besuche) v "
            "FROM shop_dim_tag WHERE art=? AND datum BETWEEN ? AND ? "
            "GROUP BY name HAVING %s > 0 ORDER BY %s DESC LIMIT ?" % (spalte, spalte),
            (art, von, bis, limit)):
        zeilen.append({"name": r["name"], "umsatz": r["u"] or 0.0,
                       "bestellungen": r["b"] or 0, "besuche": r["v"] or 0})
    return zeilen


def uebersicht(con, von, bis):
    """Alle Onlineshop-Kennzahlen des Zeitraums, ausschliesslich aus der Datenbank."""
    s = _summe(con, von, bis)
    b = s.get("bestellungen") or 0
    umsatz = s.get("umsatz") or 0.0

    besuche = s.get("besuche") or 0
    warenkorb = s.get("warenkorb") or 0
    checkout = s.get("checkout") or 0
    kaeufe = s.get("kaeufe") or 0
    quote = lambda a, c: ((a / c) * 100) if c else None      # noqa: E731

    kunden = s.get("kunden") or 0
    stamm = s.get("stammkunden") or 0

    wk = con.execute("SELECT COUNT(*) n, COALESCE(SUM(wert),0) w FROM shop_warenkorb "
                     "WHERE datum BETWEEN ? AND ?", (von, bis)).fetchone()
    liste = [{"datum": r["datum"], "wert": r["wert"],
              "artikel": [a for a in (r["artikel"] or "").split(", ") if a]}
             for r in con.execute(
                 "SELECT datum, wert, artikel FROM shop_warenkorb "
                 "WHERE datum BETWEEN ? AND ? ORDER BY datum DESC LIMIT 20", (von, bis))]

    # Verlauf: passende Verdichtung wie im uebrigen Dashboard
    tage = (s.get("tage") or 0)
    gran = "tag" if tage <= 45 else "woche" if tage <= 200 else "monat"
    ausdruck = {"tag": "datum", "woche": "strftime('%Y-KW%W', datum)",
                "monat": "substr(datum,1,7)"}[gran]
    perioden, u_werte, b_werte, v_werte = [], [], [], []
    for r in con.execute("SELECT %s AS p, SUM(umsatz) u, SUM(bestellungen) b, "
                         "SUM(besuche) v FROM shop_tag WHERE datum BETWEEN ? AND ? "
                         "GROUP BY p ORDER BY p" % ausdruck, (von, bis)):
        perioden.append(r["p"])
        u_werte.append(r["u"] or 0.0)
        b_werte.append(r["b"] or 0)
        v_werte.append(r["v"] or 0)

    st = stand(con)
    return {
        "von": von, "bis": bis,
        "kennzahlen": {
            "bestellungen": b, "bruttoUmsatz": s.get("brutto") or 0.0,
            "rabatte": s.get("rabatte") or 0.0, "retouren": s.get("retouren") or 0.0,
            "nettoUmsatz": s.get("netto") or 0.0, "gesamtUmsatz": umsatz,
            "bonwert": (umsatz / b) if b else 0.0,
        },
        "trichter": {
            "besuche": besuche, "besucher": s.get("besucher") or 0,
            "mitWarenkorb": warenkorb, "imCheckout": checkout, "gekauft": kaeufe,
            "conversion": quote(kaeufe, besuche) or 0.0,
            "warenkorbQuote": quote(warenkorb, besuche),
            "checkoutQuote": quote(checkout, warenkorb),
            "kaufQuote": quote(kaeufe, checkout),
            "abbruchWarenkorb": max(0, warenkorb - checkout),
            "abbruchCheckout": max(0, checkout - kaeufe),
            "abbruchCheckoutQuote": quote(checkout - kaeufe, checkout),
        },
        "verlauf": {
            "granularitaet": {"tag": "Tageswerte", "woche": "Wochenwerte",
                              "monat": "Monatswerte"}[gran],
            "perioden": perioden, "umsatz": u_werte,
            "bestellungen": b_werte, "besuche": v_werte,
        },
        "topProdukte": _dim(con, von, bis, "produkt", "umsatz"),
        "herkunft": _dim(con, von, bis, "quelle", "umsatz"),
        "geraete": _dim(con, von, bis, "geraet", "besuche"),
        "laender": _dim(con, von, bis, "land", "besuche", limit=8),
        "kunden": {"kunden": kunden, "stammkunden": stamm,
                   "neukunden": max(0, kunden - stamm),
                   "stammkundenQuote": quote(stamm, kunden) or 0.0},
        "warenkoerbe": {
            "anzahl": wk["n"] or 0, "wert": wk["w"] or 0.0,
            "durchschnitt": ((wk["w"] / wk["n"]) if wk["n"] else 0.0),
            "anteilAmUmsatz": ((wk["w"] / umsatz * 100) if umsatz else None),
            "liste": liste,
        },
        "tageMitDaten": s.get("tage") or 0,
        "abgerufen": st.get("sync"),
        "datenVon": st.get("von"), "datenBis": st.get("bis"),
    }
