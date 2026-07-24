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

API_VERSION_STD = "2026-07"
TIMEOUT = 30

# Kleiner Zwischenspeicher, damit mehrfaches Öffnen des Tabs nicht jedes Mal
# bei Shopify anfragt. Shopify drosselt Abfragen, und die Zahlen ändern sich
# nicht sekündlich.
_CACHE = {}
CACHE_SEKUNDEN = 900          # 15 Minuten


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


def _eine_zeile(abfrage):
    z = shopifyql(abfrage)
    return z[0] if z else {}


def _granularitaet(tage):
    if tage <= 31:
        return "day"
    if tage <= 180:
        return "week"
    return "month"


def _tage(von, bis):
    from datetime import date
    try:
        d0 = date.fromisoformat(von)
        d1 = date.fromisoformat(bis)
        return max(1, (d1 - d0).days + 1)
    except Exception:                                          # noqa: BLE001
        return 30


# --------------------------------------------------------------------- Bausteine
def kennzahlen(von, bis):
    """Umsatz, Bestellungen, Rabatte, Retouren, Ø Bestellwert."""
    r = _eine_zeile(
        "FROM sales SHOW orders, gross_sales, discounts, returns, net_sales, "
        "total_sales, average_order_value SINCE %s UNTIL %s" % (von, bis))
    return {
        "bestellungen": int(_z(r.get("orders"))),
        "bruttoUmsatz": _z(r.get("gross_sales")),
        "rabatte": abs(_z(r.get("discounts"))),
        "retouren": abs(_z(r.get("returns"))),
        "nettoUmsatz": _z(r.get("net_sales")),
        "gesamtUmsatz": _z(r.get("total_sales")),
        "bonwert": _z(r.get("average_order_value")),
    }


def trichter(von, bis):
    """Besuche → Warenkorb → Checkout → Kauf, inkl. Abbruchquoten."""
    r = _eine_zeile(
        "FROM sessions SHOW sessions, online_store_visitors, "
        "sessions_with_cart_additions, sessions_that_reached_checkout, "
        "sessions_that_completed_checkout, conversion_rate "
        "SINCE %s UNTIL %s" % (von, bis))
    besuche = int(_z(r.get("sessions")))
    warenkorb = int(_z(r.get("sessions_with_cart_additions")))
    checkout = int(_z(r.get("sessions_that_reached_checkout")))
    kauf = int(_z(r.get("sessions_that_completed_checkout")))
    quote = lambda a, b: ((a / b) * 100) if b else None        # noqa: E731
    return {
        "besuche": besuche,
        "besucher": int(_z(r.get("online_store_visitors"))),
        "mitWarenkorb": warenkorb,
        "imCheckout": checkout,
        "gekauft": kauf,
        # Shopify liefert die Conversion als Anteil (0,012 = 1,2 %)
        "conversion": _z(r.get("conversion_rate")) * 100,
        "warenkorbQuote": quote(warenkorb, besuche),
        "checkoutQuote": quote(checkout, warenkorb),
        "kaufQuote": quote(kauf, checkout),
        # Wie viele springen in welchem Schritt ab
        "abbruchWarenkorb": warenkorb - checkout,
        "abbruchCheckout": checkout - kauf,
        "abbruchCheckoutQuote": quote(checkout - kauf, checkout),
    }


def verlauf(von, bis):
    """Umsatz, Bestellungen und Besuche im Zeitverlauf."""
    g = _granularitaet(_tage(von, bis))
    umsatz = shopifyql("FROM sales SHOW total_sales, orders TIMESERIES %s "
                       "SINCE %s UNTIL %s" % (g, von, bis))
    sitz = shopifyql("FROM sessions SHOW sessions TIMESERIES %s "
                     "SINCE %s UNTIL %s" % (g, von, bis))

    def schluessel(zeile):
        for k in (g, "day", "week", "month", "hour"):
            if k in zeile:
                return str(zeile[k])[:10]
        return ""

    punkte = {}
    for z in umsatz:
        k = schluessel(z)
        if k:
            punkte.setdefault(k, {})["umsatz"] = _z(z.get("total_sales"))
            punkte[k]["bestellungen"] = int(_z(z.get("orders")))
    for z in sitz:
        k = schluessel(z)
        if k:
            punkte.setdefault(k, {})["besuche"] = int(_z(z.get("sessions")))

    perioden = sorted(punkte)
    return {
        "granularitaet": {"day": "Tageswerte", "week": "Wochenwerte",
                          "month": "Monatswerte"}.get(g, g),
        "perioden": perioden,
        "umsatz": [punkte[p].get("umsatz", 0) for p in perioden],
        "bestellungen": [punkte[p].get("bestellungen", 0) for p in perioden],
        "besuche": [punkte[p].get("besuche", 0) for p in perioden],
    }


def umsatz_perioden(von, bis, gran="monat"):
    """Onlineumsatz je Periode, beschriftet wie die NEO-Zeitreihe.

    gran: 'tag' -> 2026-01-15, 'woche' -> 2026-KW03, 'monat' -> 2026-01
    So lässt sich die Shopify-Linie direkt in die Filial-Zeitreihe einsetzen.
    """
    from datetime import date as _date
    einheit = {"tag": "day", "woche": "week", "monat": "month"}.get(gran, "month")
    zeilen = shopifyql("FROM sales SHOW total_sales TIMESERIES %s SINCE %s UNTIL %s"
                       % (einheit, von, bis))

    def etikett(zeile):
        roh = ""
        for k in (einheit, "day", "week", "month", "hour"):
            if k in zeile:
                roh = str(zeile[k])[:10]
                break
        if not roh:
            return None
        if gran == "monat":
            return roh[:7]
        if gran == "tag":
            return roh
        try:                                   # Woche: gleiche Zählung wie SQLite
            return _date.fromisoformat(roh).strftime("%Y-KW%W")
        except ValueError:
            return roh

    raus = {}
    for z in zeilen:
        e = etikett(z)
        if e:
            raus[e] = raus.get(e, 0.0) + _z(z.get("total_sales"))
    return raus


def _gruppiert(abfrage, schluesselfeld, wertfelder):
    zeilen = shopifyql(abfrage)
    raus = []
    for z in zeilen:
        eintrag = {"name": (z.get(schluesselfeld) or "Direkt / unbekannt")}
        for ziel, quelle in wertfelder.items():
            eintrag[ziel] = _z(z.get(quelle))
        raus.append(eintrag)
    return raus


def top_produkte(von, bis, limit=10):
    return _gruppiert(
        "FROM sales SHOW gross_sales, orders GROUP BY product_title "
        "ORDER BY gross_sales DESC LIMIT %d SINCE %s UNTIL %s" % (limit, von, bis),
        "product_title", {"umsatz": "gross_sales", "bestellungen": "orders"})


def herkunft(von, bis):
    return _gruppiert(
        "FROM sales SHOW orders, total_sales GROUP BY order_referrer_source "
        "ORDER BY total_sales DESC LIMIT 10 SINCE %s UNTIL %s" % (von, bis),
        "order_referrer_source", {"umsatz": "total_sales", "bestellungen": "orders"})


def geraete(von, bis):
    return _gruppiert(
        "FROM sessions SHOW sessions GROUP BY session_device_type "
        "SINCE %s UNTIL %s" % (von, bis),
        "session_device_type", {"besuche": "sessions"})


def laender(von, bis):
    return _gruppiert(
        "FROM sessions SHOW sessions GROUP BY session_country "
        "ORDER BY sessions DESC LIMIT 8 SINCE %s UNTIL %s" % (von, bis),
        "session_country", {"besuche": "sessions"})


def kunden(von, bis):
    r = _eine_zeile("FROM sales SHOW customers, returning_customers, "
                    "returning_customer_rate SINCE %s UNTIL %s" % (von, bis))
    gesamt = int(_z(r.get("customers")))
    wieder = int(_z(r.get("returning_customers")))
    return {"kunden": gesamt, "stammkunden": wieder,
            "neukunden": max(0, gesamt - wieder),
            "stammkundenQuote": _z(r.get("returning_customer_rate")) * 100}


def abgebrochene_warenkoerbe(von, bis, limit=50):
    """Liegengebliebene Checkouts samt Warenwert — der größte Hebel im Shop."""
    d = graphql("""
        query($q: String!, $n: Int!) {
          abandonedCheckouts(first: $n, query: $q, reverse: true) {
            edges { node {
              id createdAt completedAt
              totalPriceSet { shopMoney { amount } }
              lineItems(first: 5) { edges { node { title quantity } } }
            } }
          }
        }""", {"q": "created_at:>=%s created_at:<=%s" % (von, bis), "n": limit})
    kanten = ((d.get("abandonedCheckouts") or {}).get("edges") or [])
    liste, summe = [], 0.0
    for k in kanten:
        n = k.get("node") or {}
        if n.get("completedAt"):          # doch noch gekauft
            continue
        wert = _z(((n.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount"))
        summe += wert
        artikel = [(e.get("node") or {}).get("title")
                   for e in ((n.get("lineItems") or {}).get("edges") or [])]
        liste.append({
            "datum": (n.get("createdAt") or "")[:10],
            "wert": wert,
            "artikel": [a for a in artikel if a],
        })
    return {"anzahl": len(liste), "wert": summe,
            "durchschnitt": (summe / len(liste)) if liste else 0.0,
            "liste": liste[:20]}


# ------------------------------------------------------------------- Gesamtabruf
def uebersicht(von, bis, frisch=False):
    """Alle Onlineshop-Kennzahlen für den Zeitraum, mit kurzem Zwischenspeicher."""
    schluessel = "%s|%s" % (von, bis)
    if not frisch:
        eintrag = _CACHE.get(schluessel)
        if eintrag and (time.time() - eintrag[0]) < CACHE_SEKUNDEN:
            return eintrag[1]

    daten = {"von": von, "bis": bis}
    daten["kennzahlen"] = kennzahlen(von, bis)
    daten["trichter"] = trichter(von, bis)
    daten["verlauf"] = verlauf(von, bis)
    daten["topProdukte"] = top_produkte(von, bis)
    daten["herkunft"] = herkunft(von, bis)
    daten["geraete"] = geraete(von, bis)
    daten["laender"] = laender(von, bis)
    daten["kunden"] = kunden(von, bis)
    # Abgebrochene Warenkörbe dürfen den Rest nicht blockieren
    try:
        daten["warenkoerbe"] = abgebrochene_warenkoerbe(von, bis)
    except ShopifyFehler as e:
        daten["warenkoerbe"] = {"anzahl": 0, "wert": 0.0, "durchschnitt": 0.0,
                                "liste": [], "hinweis": str(e)}

    # Umsatz, der im Checkout liegen geblieben ist, ins Verhältnis setzen
    k = daten["kennzahlen"]["gesamtUmsatz"]
    w = daten["warenkoerbe"]["wert"]
    daten["warenkoerbe"]["anteilAmUmsatz"] = ((w / k) * 100) if k else None

    daten["abgerufen"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _CACHE[schluessel] = (time.time(), daten)
    return daten
