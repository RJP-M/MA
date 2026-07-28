#!/usr/bin/env python3
"""
Meister Parfumerie Dashboard — Backend

Ein lokaler Server, der
  1. das Dashboard (neo-dashboard.html) ausliefert,
  2. die NEO REST-API proxied (loest das CORS-Problem),
  3. Umsaetze, Artikelstamm und Bestaende in einer lokalen SQLite-Datei cached,
  4. daraus Auswertungen rechnet (Marken-Trends, Perioden-Vergleiche, Reichweite).

Der Cache ist noetig, weil /bewegungsdaten/umsatz/liste immer nur EINEN Tag
liefert und die API ein Rate-Limit hat (HTTP 509). 90 Tage = 90 Requests.
Einmal laden, danach nur noch neue Tage nachziehen.

Start:
    python neo-proxy.py
    -> http://localhost:8080

Optionen:
    --port 9000
    --target https://stage.neo-wws.de/neo-server-stage
    --db  /pfad/zu/neo-cache.db
    --delay 0.6      Pause zwischen API-Requests in Sekunden

Nur Python-Standardbibliothek. Ab Python 3.8.

Zugangsdaten werden NICHT gespeichert. Der Authorization-Header kommt bei jedem
Aufruf vom Browser und wird nur durchgereicht.
"""

import argparse
import base64
import http.server
import json
import os
import socketserver
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

try:
    import neo_auth            # Benutzeranmeldung (nur im Server-/Auth-Modus)
except Exception:              # noqa: BLE001
    neo_auth = None

try:
    import neo_shopify         # Onlineshop-Kennzahlen direkt aus Shopify
except Exception:              # noqa: BLE001
    neo_shopify = None

# Fassung dieses Servers. Das Dashboard vergleicht sie mit seiner eigenen und
# weist darauf hin, wenn eine der beiden Dateien beim Hochladen vergessen wurde.
VERSION = "2026-07-28.60"

DEFAULT_TARGET = "https://portal.neo-wws.de/neo-server-prod"
HERE = Path(__file__).resolve().parent
DASHBOARD = HERE / "neo-dashboard.html"

# Standard-Ladespanne der Auswertungen/des Erstabrufs in Tagen.
# 730 = rund 24 Monate, damit der Vorjahresvergleich sofort verfügbar ist.
# Über die Umgebungsvariable START_TAGE anpassbar.
try:
    START_TAGE = int(os.environ.get("START_TAGE", "730"))
except ValueError:
    START_TAGE = 730

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "accept-encoding",
}

KANAELE = {
    "verkaeufeFiliale": "filiale",
    "verkaeufeWebshop": "webshop",
    "verkaeufeVertrieb": "vertrieb",
}

CFG = {"target": DEFAULT_TARGET, "db": str(HERE / "neo-cache.db"), "delay": 0.6,
       "sonntag": True,   # True = alle Kalendertage zaehlen (Standard)
       "auth": False,     # True = Benutzeranmeldung erforderlich (Server-Modus)
       "https": False}    # True = Cookies mit Secure-Flag (hinter TLS)

# Optional: Sonntage als Schliesstage ueberall ausklammern (--ohne-sonntag).
# Dann werden sie nicht synchronisiert, gelten nicht als Datenluecke und
# fliessen nicht in Tagesdurchschnitte oder Reichweiten ein.
SQL_KEIN_SONNTAG = "strftime('%w', {t}.datum) <> '0'"


def ist_sonntag(d):
    return date.fromisoformat(d).weekday() == 6 if isinstance(d, str) else d.weekday() == 6


def verkaufstage(von, bis):
    """Kalendertage im Zeitraum ohne Sonntage."""
    d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
    n = 0
    d = d0
    while d <= d1:
        if CFG["sonntag"] or d.weekday() != 6:
            n += 1
        d += timedelta(days=1)
    return max(n, 1)

# Laufender Sync-Job (nur einer gleichzeitig)
JOB = {"running": False, "label": "", "done": 0, "total": 0, "log": [], "error": None}
JOB_LOCK = threading.Lock()


# ----------------------------------------------------------------- Datenbank
SCHEMA = """
CREATE TABLE IF NOT EXISTS artikel(
  artikelNr INTEGER PRIMARY KEY,
  bezeichnung TEXT, status TEXT, artikeltyp TEXT, gtin TEXT,
  lieferant TEXT, markeNr INTEGER, marke TEXT,
  submarkeNr INTEGER, submarke TEXT, linieNr INTEGER, linie TEXT,
  warengruppeNr TEXT, warengruppe TEXT,
  oberwarengruppeNr TEXT, oberwarengruppe TEXT,
  kategorie TEXT, version INTEGER,
  ekPreis REAL, vkPreis REAL, uvpPreis REAL, baPreis REAL
);
CREATE INDEX IF NOT EXISTS ix_art_marke ON artikel(marke);
CREATE INDEX IF NOT EXISTS ix_art_wg ON artikel(warengruppe);

CREATE TABLE IF NOT EXISTS umsatz(
  datum TEXT, filialeNr INTEGER, artikelNr INTEGER, kanal TEXT,
  stueck INTEGER, stueckMitKunde INTEGER,
  bruttoOhneRabatt REAL, bruttoMitRabatt REAL, bruttoMitRabattMitKunde REAL,
  mwstOhne REAL, mwstMit REAL, belege INTEGER,
  PRIMARY KEY(datum, filialeNr, artikelNr, kanal)
);
CREATE INDEX IF NOT EXISTS ix_ums_datum ON umsatz(datum);
CREATE INDEX IF NOT EXISTS ix_ums_art ON umsatz(artikelNr);

CREATE TABLE IF NOT EXISTS umsatz_tage(
  datum TEXT PRIMARY KEY, geladen TEXT, belege INTEGER, positionen INTEGER
);

CREATE TABLE IF NOT EXISTS bestand(
  artikelNr INTEGER, filialeNr INTEGER,
  bestand INTEGER, avisiert INTEGER, reserviert INTEGER, meldemenge INTEGER,
  stand TEXT, PRIMARY KEY(artikelNr, filialeNr)
);

CREATE TABLE IF NOT EXISTS filiale(
  filialeNr INTEGER PRIMARY KEY, bezeichnung TEXT, kurzbezeichnung TEXT,
  status TEXT, webshopfiliale INTEGER, anzahlKassen INTEGER
);

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

-- Zaehler fuer die kostenpflichtigen NEO-Aufrufe, je Kalendermonat
CREATE TABLE IF NOT EXISTS api_zaehler(monat TEXT PRIMARY KEY, anzahl INTEGER NOT NULL DEFAULT 0);
"""


def norm_txt(s):
    """Text zum Vergleichen vereinheitlichen.

    Markennamen kommen aus der Warenwirtschaft oft mit Leerzeichen am Rand,
    doppelten Leerzeichen oder geschuetzten Leerzeichen. Ein direkter Vergleich
    scheitert daran stillschweigend. Hier wird alles auf einfache Leerzeichen
    reduziert und kleingeschrieben."""
    if s is None:
        return ""
    return " ".join(str(s).replace(" ", " ").split()).strip().lower()


def db():
    con = sqlite3.connect(CFG["db"], timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # In SQL nutzbar machen, damit Filter unabhaengig von solchen Feinheiten greifen
    con.create_function("norm_txt", 1, norm_txt)
    return con


def init_db():
    con = db()
    con.executescript(SCHEMA)
    # Migration fuer aeltere Caches: Preisspalten nachruesten
    have = {r["name"] for r in con.execute("PRAGMA table_info(artikel)")}
    for col in ("ekPreis", "vkPreis", "uvpPreis", "baPreis"):
        if col not in have:
            con.execute("ALTER TABLE artikel ADD COLUMN %s REAL" % col)
    con.commit()
    if neo_auth is not None:
        neo_auth.init(con)     # Nutzer- und Meta-Tabellen anlegen
    if neo_shopify is not None:
        neo_shopify.init(con)  # Tabellen für die Onlineshop-Zahlen
    con.close()


def meta_get(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)))


# ------------------------------------------------------------- NEO API-Zugriff
class NeoError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- Aufrufbremse
# Die NEO-API wird pro Aufruf abgerechnet (bis 1000 im Monat pauschal, darueber
# je Aufruf). Damit ein Fehler -- eine Endlosschleife, ein versehentlich
# mehrfach gestarteter Vollabruf -- nicht ins Geld geht, zaehlen wir jeden
# Aufruf mit und stoppen hart am Monatslimit.
API_LOCK = threading.Lock()


def api_limit():
    try:
        return max(0, int(os.environ.get("NEO_API_LIMIT", "1000")))
    except ValueError:
        return 1000


def api_monat():
    return date.today().strftime("%Y-%m")


def api_stand(con=None):
    """Aufrufe des laufenden Monats, Limit und Restbudget."""
    eigen = con is None
    if eigen:
        con = db()
    try:
        r = con.execute("SELECT anzahl FROM api_zaehler WHERE monat=?",
                        (api_monat(),)).fetchone()
        n = (r["anzahl"] if r else 0) or 0
    except Exception:                                    # noqa: BLE001
        n = 0
    finally:
        if eigen:
            con.close()
    grenze = api_limit()
    return {"monat": api_monat(), "anzahl": n, "limit": grenze,
            "rest": max(0, grenze - n) if grenze else None,
            "anteil": (n / grenze * 100) if grenze else None,
            "gesperrt": bool(grenze) and n >= grenze}


def api_zaehlen():
    """Einen Aufruf verbuchen. Gibt den neuen Stand zurueck, oder None wenn das
    Monatslimit bereits erreicht ist (dann darf nicht gerufen werden)."""
    grenze = api_limit()
    with API_LOCK:
        con = db()
        try:
            con.execute("INSERT INTO api_zaehler(monat,anzahl) VALUES(?,0) "
                        "ON CONFLICT(monat) DO NOTHING", (api_monat(),))
            r = con.execute("SELECT anzahl FROM api_zaehler WHERE monat=?",
                            (api_monat(),)).fetchone()
            n = (r["anzahl"] if r else 0) or 0
            if grenze and n >= grenze:
                return None
            con.execute("UPDATE api_zaehler SET anzahl=anzahl+1 WHERE monat=?",
                        (api_monat(),))
            con.commit()
            return n + 1
        finally:
            con.close()


def neo_get(path, params=None, auth=None, accept="application/json", retries=4):
    """Ruft die NEO-API auf. auth = kompletter Authorization-Header."""
    url = CFG["target"].rstrip("/") + "/ws-api" + path
    if params:
        pairs = []
        for k, v in params.items():
            if v is None or v == "":
                continue
            if isinstance(v, (list, tuple)):
                pairs.extend((k, str(x)) for x in v)
            else:
                pairs.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
        if pairs:
            url += "?" + urllib.parse.urlencode(pairs)

    # Vor dem ersten Versuch das Monatsbudget pruefen und den Aufruf verbuchen.
    # Wiederholungen wegen Rate-Limit (509) zaehlen als derselbe Aufruf.
    stand = api_zaehlen()
    if stand is None:
        g = api_limit()
        raise NeoError(429,
                       "Monatslimit erreicht: %d von %d NEO-Aufrufen in %s verbraucht. "
                       "Weitere Abrufe sind gesperrt, damit keine zusätzlichen Kosten "
                       "entstehen. Das Limit setzt sich am Monatsanfang zurück; "
                       "es lässt sich über NEO_API_LIMIT anheben."
                       % (g, g, api_monat()))

    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, method="GET")
        if auth:
            req.add_header("Authorization", auth)
        req.add_header("Accept", accept)
        req.add_header("Accept-Encoding", "identity")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as r:
                raw = r.read()
                if accept == "application/json":
                    return json.loads(raw) if raw else None
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if e.code == 509 and attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
                last = e
                continue
            raise NeoError(e.code, "HTTP %d bei %s %s" % (e.code, path, body))
        except urllib.error.URLError as e:
            raise NeoError(0, "Verbindung fehlgeschlagen: %s" % e.reason)
    raise NeoError(509, "Rate-Limit bei %s" % path)


# ------------------------------------------------------------------- Job-Utils
def job_start(label, total):
    with JOB_LOCK:
        if JOB["running"]:
            return False
        JOB.update(running=True, label=label, done=0, total=total, log=[], error=None)
        return True


def job_step(done=None, note=None):
    with JOB_LOCK:
        if done is not None:
            JOB["done"] = done
        if note:
            JOB["log"].append(note)
            del JOB["log"][:-40]


def job_end(error=None):
    with JOB_LOCK:
        JOB["running"] = False
        JOB["error"] = error


# ---------------------------------------------------------------- Sync-Routinen
def sync_filialen(auth):
    data = neo_get("/stammdaten/filiale/liste", auth=auth) or []
    con = db()
    for f in data:
        con.execute("""INSERT INTO filiale(filialeNr,bezeichnung,kurzbezeichnung,status,webshopfiliale,anzahlKassen)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(filialeNr) DO UPDATE SET
                       bezeichnung=excluded.bezeichnung, kurzbezeichnung=excluded.kurzbezeichnung,
                       status=excluded.status, webshopfiliale=excluded.webshopfiliale,
                       anzahlKassen=excluded.anzahlKassen""",
                    (f.get("filialeNr"), f.get("bezeichnung"), f.get("kurzbezeichnung"),
                     f.get("status"), 1 if f.get("webshopfiliale") else 0, f.get("anzahlKassen")))
    con.commit()
    con.close()
    return len(data)


def sync_artikel(auth, full=False):
    """Artikelstamm ueber version-Pagination holen.

    Mit kategorie=PREISE liefert die API neben Bezeichnung, Nummern, Status und
    Sortimentszuordnung auch EK-, VK-, UVP- und BA-Preis. Der EK-Preis ist die
    Grundlage der Rohertragsrechnung."""
    con = db()
    version = 0 if full else int(meta_get(con, "artikel_version", 0) or 0)
    con.close()

    batch, total, guard = 20000, 0, 0
    while True:
        guard += 1
        if guard > 200:
            break
        job_step(note="Artikel ab Version %d…" % version)
        # kategorie=PREISE liefert zusaetzlich EK/VK/UVP - Basis fuer die Margenrechnung
        rows = neo_get("/stammdaten/artikel/liste",
                       {"version": version, "anzahl": batch, "kategorie": "PREISE"},
                       auth=auth) or []
        if not rows:
            break
        con = db()
        maxv = version
        for a in rows:
            s = a.get("sortimentzuordnung") or {}
            pr = a.get("preise") or {}
            v = a.get("deltaVersion") or 0
            maxv = max(maxv, v)
            con.execute("""INSERT INTO artikel(artikelNr,bezeichnung,status,artikeltyp,gtin,
                             lieferant,markeNr,marke,submarkeNr,submarke,linieNr,linie,
                             warengruppeNr,warengruppe,oberwarengruppeNr,oberwarengruppe,
                             kategorie,version,ekPreis,vkPreis,uvpPreis,baPreis)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(artikelNr) DO UPDATE SET
                             bezeichnung=excluded.bezeichnung, status=excluded.status,
                             artikeltyp=excluded.artikeltyp, gtin=excluded.gtin,
                             lieferant=excluded.lieferant, markeNr=excluded.markeNr,
                             marke=excluded.marke, submarkeNr=excluded.submarkeNr,
                             submarke=excluded.submarke, linieNr=excluded.linieNr,
                             linie=excluded.linie, warengruppeNr=excluded.warengruppeNr,
                             warengruppe=excluded.warengruppe,
                             oberwarengruppeNr=excluded.oberwarengruppeNr,
                             oberwarengruppe=excluded.oberwarengruppe,
                             kategorie=excluded.kategorie, version=excluded.version,
                             ekPreis=COALESCE(excluded.ekPreis, artikel.ekPreis),
                             vkPreis=COALESCE(excluded.vkPreis, artikel.vkPreis),
                             uvpPreis=COALESCE(excluded.uvpPreis, artikel.uvpPreis),
                             baPreis=COALESCE(excluded.baPreis, artikel.baPreis)""",
                        (a.get("artikelNr"), a.get("bezeichnung"), a.get("artikelStatus"),
                         a.get("artikeltyp"), a.get("gtin"),
                         s.get("lieferant"), s.get("markeNummer"), s.get("marke"),
                         s.get("submarkeNummer"), s.get("submarke"),
                         s.get("linieNummer"), s.get("linie"),
                         s.get("warengruppeNummer"), s.get("warengruppe"),
                         s.get("oberwarengruppeNummer"), s.get("oberwarengruppe"),
                         s.get("kategorie"), v,
                         pr.get("ekPreis"), pr.get("vkPreis"),
                         pr.get("uvpPreis"), pr.get("baPreis")))
        meta_set(con, "artikel_version", maxv + 1)
        meta_set(con, "artikel_sync", datetime.now().isoformat(timespec="seconds"))
        con.commit()
        con.close()
        total += len(rows)
        job_step(done=total)
        if len(rows) < batch or maxv + 1 <= version:
            break
        version = maxv + 1
        time.sleep(CFG["delay"])
    return total


def sync_umsatz(auth, von, bis, force=False, tage=None):
    con = db()
    have = {r["datum"] for r in con.execute("SELECT datum FROM umsatz_tage")}
    con.close()

    if tage:
        # Ausdruecklich benannte Tage gezielt nachladen (z. B. Reparatur
        # einzelner Tage, die zu frueh geholt wurden)
        days = [t for t in tage if t]
    else:
        d0 = date.fromisoformat(von)
        d1 = date.fromisoformat(bis)
        days = []
        d = d0
        while d <= d1:
            s = d.isoformat()
            # Sonntage sind Schliesstage - nicht abfragen, das spart rund 14 %
            # der Requests und damit Druck auf das Rate-Limit.
            if (CFG["sonntag"] or d.weekday() != 6) and (force or s not in have):
                days.append(s)
            d += timedelta(days=1)

    job_start("Umsätze", len(days))
    for i, tag in enumerate(days, 1):
        job_step(done=i, note=tag)
        data = neo_get("/bewegungsdaten/umsatz/liste", {"datum": tag}, auth=auth) or []
        con = db()
        con.execute("DELETE FROM umsatz WHERE datum=?", (tag,))
        belege = pos = 0
        for f in data:
            fnr = f.get("filialeNr")
            belege += f.get("anzahlBelegeGesamt") or 0
            for av in (f.get("artikelVerkaeufe") or []):
                anr = av.get("artikelNr")
                for src, kanal in KANAELE.items():
                    v = av.get(src)
                    if not v:
                        continue
                    if not (v.get("verkaeufeAnzahlGesamt") or v.get("bruttoUmsatzMitRabatt")):
                        continue
                    pos += 1
                    con.execute("""INSERT OR REPLACE INTO umsatz VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (tag, fnr, anr, kanal,
                                 v.get("verkaeufeAnzahlGesamt") or 0,
                                 v.get("verkaeufeAnzahlMitKunde") or 0,
                                 v.get("bruttoUmsatzOhneRabatt") or 0.0,
                                 v.get("bruttoUmsatzMitRabatt") or 0.0,
                                 v.get("bruttoUmsatzMitRabattMitKunde") or 0.0,
                                 v.get("mwstUmsatzOhneRabatt") or 0.0,
                                 v.get("mwstUmsatzMitRabatt") or 0.0,
                                 av.get("anzahlBelegeGesamt") or 0))
        con.execute("INSERT OR REPLACE INTO umsatz_tage VALUES(?,?,?,?)",
                    (tag, datetime.now().isoformat(timespec="seconds"), belege, pos))
        con.commit()
        con.close()
        if i < len(days):
            time.sleep(CFG["delay"])
    return len(days)


def sync_bestand(auth):
    data = neo_get("/bewegungsdaten/artikelbestaende", {"withNullBestand": True}, auth=auth) or []
    stand = datetime.now().isoformat(timespec="seconds")
    con = db()
    con.execute("DELETE FROM bestand")
    con.executemany("INSERT OR REPLACE INTO bestand VALUES(?,?,?,?,?,?,?)",
                    [(b.get("artikelNr"), b.get("filialeNr"), b.get("bestand") or 0,
                      b.get("avisiert") or 0, b.get("reserviert") or 0,
                      b.get("meldemenge") or 0, stand) for b in data])
    meta_set(con, "bestand_sync", stand)
    con.commit()
    con.close()
    return len(data)


def run_sync(auth, von, bis, what, tage=None):
    try:
        if "filialen" in what:
            job_start("Filialen", 1)
            n = sync_filialen(auth)
            job_step(done=1, note="%d Filialen" % n)
            job_end()
        if "artikel" in what:
            job_start("Artikelstamm", 0)
            n = sync_artikel(auth, full=("artikel_full" in what))
            job_step(note="%d Artikel" % n)
            job_end()
        if "umsatz" in what:
            n = sync_umsatz(auth, von, bis, force=("umsatz_force" in what), tage=tage)
            job_step(note="%d Tage geladen" % n)
            job_end()
        if "bestand" in what:
            job_start("Bestände", 1)
            n = sync_bestand(auth)
            job_step(done=1, note="%d Positionen" % n)
            job_end()
    except NeoError as e:
        job_end("HTTP %s – %s" % (e.status, e))
    except Exception as e:  # noqa: BLE001
        job_end(str(e))


# ------------------------------------------------------------------- Analytics
DIMS = {
    "marke": "COALESCE(a.marke,'(ohne Marke)')",
    "submarke": "COALESCE(a.submarke,'(ohne Submarke)')",
    "linie": "COALESCE(a.linie,'(ohne Linie)')",
    "warengruppe": "COALESCE(a.warengruppe,'(ohne WG)')",
    "oberwarengruppe": "COALESCE(a.oberwarengruppe,'(ohne OWG)')",
    "lieferant": "COALESCE(a.lieferant,'(ohne Lieferant)')",
    "kategorie": "COALESCE(a.kategorie,'(ohne Kategorie)')",
    "artikel": "a.artikelNr || ' – ' || COALESCE(a.bezeichnung,'')",
    "filiale": "COALESCE(f.kurzbezeichnung, f.bezeichnung, 'Filiale ' || u.filialeNr)",
}

# Rohertrag = Nettoumsatz minus Wareneinsatz (Stueck x EK).
# Artikel ohne hinterlegten EK wuerden die Marge verfaelschen, deshalb wird
# der Nettoumsatz MIT EK separat mitgefuehrt (nettoMitEk) und die Marge nur
# darauf bezogen. ekAbdeckung zeigt, wie belastbar der Wert ist.
METRICS = """
  SUM(u.bruttoMitRabatt) AS brutto,
  SUM(u.bruttoOhneRabatt) AS bruttoOhne,
  SUM(u.bruttoMitRabatt - u.mwstMit) AS netto,
  SUM(u.mwstMit) AS mwst,
  SUM(u.stueck) AS stueck,
  SUM(u.stueckMitKunde) AS stueckMitKunde,
  SUM(u.belege) AS belege,
  COUNT(DISTINCT u.artikelNr) AS artikel,
  SUM(CASE WHEN a.ekPreis > 0 THEN u.stueck * a.ekPreis ELSE 0 END) AS wareneinsatz,
  SUM(CASE WHEN a.ekPreis > 0 THEN u.bruttoMitRabatt - u.mwstMit ELSE 0 END) AS nettoMitEk,
  SUM(CASE WHEN a.ekPreis > 0 THEN u.stueck ELSE 0 END) AS stueckMitEk,
  SUM(CASE WHEN a.uvpPreis > 0 THEN u.stueck * a.uvpPreis ELSE 0 END) AS uvpWert,
  SUM(CASE WHEN a.uvpPreis > 0 THEN u.bruttoMitRabatt ELSE 0 END) AS bruttoMitUvp,
  SUM(CASE WHEN a.ekPreis > 0 THEN u.bruttoOhneRabatt - u.mwstOhne ELSE 0 END) AS nettoListeMitEk
"""


def marge(d):
    """Ergaenzt ein Kennzahl-Dict um Rohertrag, Marge, Rabatteffekt und UVP-Abweichung."""
    netto_ek = d.get("nettoMitEk") or 0
    ware = d.get("wareneinsatz") or 0
    d["rohertrag"] = netto_ek - ware
    d["marge"] = (d["rohertrag"] / netto_ek * 100) if netto_ek else None
    d["ekAbdeckung"] = (netto_ek / d["netto"] * 100) if d.get("netto") else None
    uvp = d.get("uvpWert") or 0
    d["uvpAbweichung"] = ((d.get("bruttoMitUvp") or 0) / uvp - 1) * 100 if uvp else None

    # Was waere die Marge ohne jede Abschrift? Differenz = durch Rabatt verlorene Marge.
    liste = d.get("nettoListeMitEk") or 0
    d["rohertragOhneRabatt"] = liste - ware
    d["margeOhneRabatt"] = (d["rohertragOhneRabatt"] / liste * 100) if liste else None
    d["rabattwert"] = liste - netto_ek
    d["margenverlust"] = (d["margeOhneRabatt"] - d["marge"]) \
        if (d["margeOhneRabatt"] is not None and d["marge"] is not None) else None
    d["rabattquote"] = (d["rabattwert"] / liste * 100) if liste else None
    return d


def sonntag_aktiv(q):
    """Sollen Sonntage mitgerechnet werden? Standard: nein."""
    v = (q or {}).get("sonntag")
    if v is None:
        return CFG["sonntag"]
    return str(v).lower() in ("1", "true", "ja", "yes")


def where_clause(q, alias_prefix=True):
    """Baut WHERE-Bedingungen + Parameterliste aus den Query-Parametern."""
    cond, args = [], []
    if not sonntag_aktiv(q):
        cond.append(SQL_KEIN_SONNTAG.format(t="u"))
    if q.get("filiale"):
        cond.append("u.filialeNr = ?")
        args.append(int(q["filiale"]))
    kanal = q.get("kanal")
    if kanal and kanal != "all":
        cond.append("u.kanal = ?")
        args.append(kanal)
    for field, col in (("marke", "a.marke"), ("submarke", "a.submarke"),
                       ("linie", "a.linie"), ("warengruppe", "a.warengruppe"),
                       ("oberwarengruppe", "a.oberwarengruppe"),
                       ("lieferant", "a.lieferant"), ("kategorie", "a.kategorie")):
        if q.get(field):
            cond.append("%s = ?" % col)
            args.append(q[field])
    return cond, args


BASE_FROM = """
FROM umsatz u
LEFT JOIN artikel a ON a.artikelNr = u.artikelNr
LEFT JOIN filiale f ON f.filialeNr = u.filialeNr
"""


def q_kpi(con, q):
    """Kennzahlen fuer eine Periode + Vergleichsperioden."""
    von, bis = q["von"], q["bis"]
    cond, args = where_clause(q)
    base = " AND ".join(["u.datum BETWEEN ? AND ?"] + cond)

    def one(v, b):
        row = con.execute("SELECT %s %s WHERE %s" % (METRICS, BASE_FROM, base),
                          [v, b] + args).fetchone()
        return marge({k: (row[k] or 0) for k in row.keys()})

    d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
    span = (d1 - d0).days + 1
    p0, p1 = d0 - timedelta(days=span), d0 - timedelta(days=1)
    y0, y1 = d0.replace(year=d0.year - 1), d1.replace(year=d1.year - 1)

    vt = verkaufstage(von, bis)
    a = one(von, bis)
    a["verkaufstage"] = vt
    a["umsatzProTag"] = a["brutto"] / vt
    return {
        "aktuell": a,
        "vorperiode": one(p0.isoformat(), p1.isoformat()),
        "vorjahr": one(y0.isoformat(), y1.isoformat()),
        "perioden": {
            "aktuell": [von, bis], "vorperiode": [p0.isoformat(), p1.isoformat()],
            "vorjahr": [y0.isoformat(), y1.isoformat()], "tage": span,
            "verkaufstage": vt,
            "verkaufstageVorperiode": verkaufstage(p0.isoformat(), p1.isoformat()),
            "verkaufstageVorjahr": verkaufstage(y0.isoformat(), y1.isoformat()),
            "sonntageEnthalten": sonntag_aktiv(q),
        },
    }


def q_trend(con, q):
    cond, args = where_clause(q)
    where = " AND ".join(["u.datum BETWEEN ? AND ?"] + cond)
    rows = con.execute("""SELECT u.datum AS datum, %s %s WHERE %s GROUP BY u.datum ORDER BY u.datum"""
                       % (METRICS, BASE_FROM, where), [q["von"], q["bis"]] + args).fetchall()
    return [marge(dict(r)) for r in rows]


def q_zeitreihe(con, q):
    """Zeitverlauf je Dimension - Grundlage fuer die Liniendiagramme.

    Bei langen Zeitraeumen wird automatisch auf Wochen bzw. Monate verdichtet,
    sonst ist die Linie nur noch Rauschen."""
    dim = q.get("dim", "filiale")
    expr = DIMS.get(dim, DIMS["filiale"])
    top = int(q.get("top", 8))
    metrik = q.get("metrik", "brutto")
    von, bis = q["von"], q["bis"]
    span = (date.fromisoformat(bis) - date.fromisoformat(von)).days + 1

    gran = q.get("gran") or ("tag" if span <= 45 else "woche" if span <= 200 else "monat")
    bucket = {"tag": "u.datum",
              "woche": "strftime('%Y-KW%W', u.datum)",
              "monat": "substr(u.datum,1,7)"}.get(gran, "u.datum")

    cond, args = where_clause(q)
    where = " AND ".join(["u.datum BETWEEN ? AND ?"] + cond)
    sql = """
    SELECT {expr} AS dim, {bucket} AS periode, MIN(u.datum) AS start, {metrics}
    {frm} WHERE {where}
    GROUP BY dim, periode ORDER BY periode
    """.format(expr=expr, bucket=bucket, metrics=METRICS, frm=BASE_FROM, where=where)

    daten, perioden, summen = {}, [], {}
    for r in con.execute(sql, [von, bis] + args):
        d = marge(dict(r))
        p = r["periode"]
        if p not in perioden:
            perioden.append(p)
        daten.setdefault(r["dim"], {})[p] = d
        summen[r["dim"]] = summen.get(r["dim"], 0) + (d.get("brutto") or 0)

    namen = sorted(summen, key=lambda k: -summen[k])[:top]
    serien = []
    for n in namen:
        werte = []
        for p in perioden:
            d = daten[n].get(p)
            werte.append(None if d is None else d.get(metrik))
        serien.append({"dim": n, "werte": werte, "summe": summen[n]})

    # Gesamtlinie ueber alle Dimensionen, unabhaengig von der Top-Begrenzung
    gesamt = []
    for p in perioden:
        s = sum((daten[k][p].get("brutto") or 0) for k in daten if p in daten[k])
        gesamt.append(s)

    return {"perioden": perioden, "serien": serien, "gesamt": gesamt,
            "granularitaet": gran, "metrik": metrik, "dim": dim,
            "weitere": max(0, len(summen) - len(namen))}


ONLINE_NAMEN = ("onlineshop", "online-shop", "online shop", "webshop",
                "web-shop", "online", "shopify")


def ist_onlinefiliale(name):
    n = (name or "").strip().lower()
    return any(k in n for k in ONLINE_NAMEN)


def shopify_onlinelinie(res, q):
    """Ersetzt in der Filial-Zeitreihe die Onlineshop-Linie durch die Zahlen
    aus Shopify.

    Die NEO-Warenwirtschaft fuehrt den Webshop zwar als Filiale, liefert dort
    aber unvollstaendige Werte. Stationaere Filialen bleiben unveraendert aus
    NEO. Faellt Shopify aus, bleibt einfach alles wie es war."""
    if neo_shopify is None or not neo_shopify.konfiguriert():
        return res
    if q.get("dim", "filiale") != "filiale":
        return res
    if res.get("metrik", "brutto") != "brutto":
        return res          # Rohertrag/Marge liefert Shopify so nicht
    treffer = [s for s in res.get("serien", []) if ist_onlinefiliale(s.get("dim"))]
    if not treffer:
        return res
    try:
        con = db()
        try:
            if not neo_shopify.hat_daten(con, q["von"], q["bis"]):
                return res
            je_periode = neo_shopify.umsatz_perioden(
                con, q["von"], q["bis"], res.get("granularitaet", "monat"))
        finally:
            con.close()
    except Exception:                                    # noqa: BLE001
        return res                                       # lieber NEO als nichts
    if not je_periode:
        return res

    perioden = res.get("perioden", [])
    gesamt = res.get("gesamt") or []
    passt = len(gesamt) == len(perioden)

    for s in treffer:
        alt = list(s.get("werte") or [])
        neu = [je_periode.get(p) for p in perioden]
        s["werte"] = neu
        s["summe"] = sum(v for v in neu if v)
        s["quelle"] = "shopify"
        # Gesamtlinie nur um die Differenz verschieben, damit Filialen
        # ausserhalb der Top-Auswahl erhalten bleiben.
        if passt:
            for i in range(len(perioden)):
                a = alt[i] if i < len(alt) and alt[i] else 0
                n = neu[i] or 0
                gesamt[i] = (gesamt[i] or 0) - a + n
    if passt:
        res["gesamt"] = gesamt
    res["onlineQuelle"] = "shopify"
    return res


def q_ranking(con, q):
    """Ranking einer Dimension inkl. Vergleich zu Vorperiode und Vorjahr."""
    dim = q.get("dim", "marke")
    expr = DIMS.get(dim, DIMS["marke"])
    cond, args = where_clause(q)
    extra = (" AND " + " AND ".join(cond)) if cond else ""

    d0, d1 = date.fromisoformat(q["von"]), date.fromisoformat(q["bis"])
    span = (d1 - d0).days + 1
    p0, p1 = d0 - timedelta(days=span), d0 - timedelta(days=1)
    y0, y1 = d0.replace(year=d0.year - 1), d1.replace(year=d1.year - 1)

    sql = """
    SELECT {expr} AS dim,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS brutto,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.stueck ELSE 0 END) AS stueck,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoOhneRabatt - u.bruttoMitRabatt ELSE 0 END) AS rabatt,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS bruttoVP,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.stueck ELSE 0 END) AS stueckVP,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS bruttoVJ,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.stueck ELSE 0 END) AS stueckVJ,
      COUNT(DISTINCT CASE WHEN u.datum BETWEEN ? AND ? THEN u.artikelNr END) AS artikel,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? AND a.ekPreis > 0
               THEN u.bruttoMitRabatt - u.mwstMit ELSE 0 END) AS nettoMitEk,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? AND a.ekPreis > 0
               THEN u.stueck * a.ekPreis ELSE 0 END) AS wareneinsatz,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt - u.mwstMit ELSE 0 END) AS netto
    {frm}
    WHERE (u.datum BETWEEN ? AND ? OR u.datum BETWEEN ? AND ? OR u.datum BETWEEN ? AND ?){extra}
    GROUP BY dim
    HAVING brutto <> 0 OR bruttoVP <> 0 OR bruttoVJ <> 0
    ORDER BY brutto DESC
    """.format(expr=expr, frm=BASE_FROM, extra=extra)

    cur, vp, vj = (q["von"], q["bis"]), (p0.isoformat(), p1.isoformat()), (y0.isoformat(), y1.isoformat())
    params = list(cur) * 3 + list(vp) * 2 + list(vj) * 2 + list(cur) * 4 \
        + list(cur) + list(vp) + list(vj) + args
    rows = [dict(r) for r in con.execute(sql, params).fetchall()]

    for r in rows:
        r["deltaVP"] = (r["brutto"] or 0) - (r["bruttoVP"] or 0)
        r["deltaVJ"] = (r["brutto"] or 0) - (r["bruttoVJ"] or 0)
        r["pctVP"] = ((r["brutto"] / r["bruttoVP"] - 1) * 100) if r["bruttoVP"] else None
        r["pctVJ"] = ((r["brutto"] / r["bruttoVJ"] - 1) * 100) if r["bruttoVJ"] else None
        nek = r.get("nettoMitEk") or 0
        r["rohertrag"] = nek - (r.get("wareneinsatz") or 0)
        r["marge"] = (r["rohertrag"] / nek * 100) if nek else None
        r["ekAbdeckung"] = (nek / r["netto"] * 100) if r.get("netto") else None
    ges = sum(r["brutto"] or 0 for r in rows) or 1
    gesRoh = sum(r["rohertrag"] or 0 for r in rows) or 1
    run = 0.0
    for r in rows:
        r["anteil"] = (r["brutto"] or 0) / ges * 100
        r["anteilRohertrag"] = (r["rohertrag"] or 0) / gesRoh * 100
        run += r["anteil"]
        r["kumuliert"] = run
    return {"rows": rows, "perioden": {"aktuell": cur, "vorperiode": vp, "vorjahr": vj}}


def _vorjahr(d):
    """Gleiches Datum ein Jahr frueher. Faengt den 29. Februar ab."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                      # 29.02. gibt es im Vorjahr nicht
        return d.replace(year=d.year - 1, day=28)


def q_windows(con, q):
    """Rollierende Fenster, jeweils gegen die direkt davorliegende Periode
    und gegen das Vorjahr.

    Ueber den Parameter 'fenster' laesst sich die Laenge frei waehlen
    (z. B. fenster=90 oder fenster=7,30,90). Ohne Angabe: 7, 30 und 90 Tage."""
    dim = q.get("dim", "marke")
    expr = DIMS.get(dim, DIMS["marke"])
    cond, args = where_clause(q)
    extra = (" AND " + " AND ".join(cond)) if cond else ""
    ende = date.fromisoformat(q.get("bis") or date.today().isoformat())

    fenster = []
    for teil in str(q.get("fenster") or "7,30,90").split(","):
        teil = teil.strip()
        if not teil.isdigit():
            continue
        n = int(teil)
        if 1 <= n <= 1095 and n not in fenster:      # bis zu drei Jahre
            fenster.append(n)
    if not fenster:
        fenster = [7, 30, 90]

    out = {}
    for w in fenster:
        c0, c1 = ende - timedelta(days=w - 1), ende
        p0, p1 = c0 - timedelta(days=w), c0 - timedelta(days=1)
        y0, y1 = _vorjahr(c0), _vorjahr(c1)
        sql = """
        SELECT {expr} AS dim,
          SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS cur,
          SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS prev,
          SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS vj,
          SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.stueck ELSE 0 END) AS stueck
        {frm}
        WHERE (u.datum BETWEEN ? AND ? OR u.datum BETWEEN ? AND ? OR u.datum BETWEEN ? AND ?){extra}
        GROUP BY dim HAVING cur <> 0 OR prev <> 0
        """.format(expr=expr, frm=BASE_FROM, extra=extra)
        p = [c0.isoformat(), c1.isoformat(), p0.isoformat(), p1.isoformat(),
             y0.isoformat(), y1.isoformat(), c0.isoformat(), c1.isoformat(),
             c0.isoformat(), c1.isoformat(), p0.isoformat(), p1.isoformat(),
             y0.isoformat(), y1.isoformat()] + args
        rows = [dict(r) for r in con.execute(sql, p).fetchall()]
        for r in rows:
            r["pct"] = ((r["cur"] / r["prev"] - 1) * 100) if r["prev"] else None
            r["pctVJ"] = ((r["cur"] / r["vj"] - 1) * 100) if r["vj"] else None
            r["delta"] = (r["cur"] or 0) - (r["prev"] or 0)
        rows.sort(key=lambda r: r["cur"], reverse=True)

        # Wochentagsprofil desselben Fensters, gegen die Vorperiode
        wc = _wtag_rows(con, c0.isoformat(), c1.isoformat(), cond, args)
        wp = _wtag_rows(con, p0.isoformat(), p1.isoformat(), cond, args)
        wges = sum((x.get("brutto") or 0) for x in wc.values()) or 1
        wtage = []
        for k in sorted(WTAG_SORT, key=lambda x: WTAG_SORT[x]):
            c = wc.get(k)
            if not c or not c.get("tage"):
                continue
            pt = c["brutto"] / c["tage"]
            v = wp.get(k) or {}
            ptv = (v.get("brutto") / v["tage"]) if v.get("tage") else None
            wtage.append({"wtag": k, "kurz": WTAG_KURZ[k], "name": WTAG_NAME[k],
                          "tage": c["tage"], "brutto": c["brutto"], "proTag": pt,
                          "anteil": c["brutto"] / wges * 100,
                          "belege": c.get("belege") or 0,
                          "bonwert": (c["brutto"] / c["belege"]) if c.get("belege") else None,
                          "proTagVP": ptv,
                          "pctVP": ((pt / ptv - 1) * 100) if ptv else None})

        # Tageslinie des Fensters, dazu die Vorperiode positionsgleich daneben
        def tagesreihe(a, b):
            wh = " AND ".join(["u.datum BETWEEN ? AND ?"] + cond)
            got = {r["datum"]: r["s"] for r in con.execute(
                "SELECT u.datum AS datum, SUM(u.bruttoMitRabatt) AS s %s WHERE %s GROUP BY u.datum"
                % (BASE_FROM, wh), [a, b] + args)}
            reihe, d = [], date.fromisoformat(a)
            e = date.fromisoformat(b)
            while d <= e:
                reihe.append({"datum": d.isoformat(), "brutto": got.get(d.isoformat(), 0) or 0})
                d += timedelta(days=1)
            return reihe

        out[str(w)] = {"rows": rows, "aktuell": [c0.isoformat(), c1.isoformat()],
                       "vorperiode": [p0.isoformat(), p1.isoformat()],
                       "vorjahr": [y0.isoformat(), y1.isoformat()],
                       "wochentage": wtage,
                       "serie": tagesreihe(c0.isoformat(), c1.isoformat()),
                       "seriePrev": tagesreihe(p0.isoformat(), p1.isoformat()),
                       "serieVJ": tagesreihe(y0.isoformat(), y1.isoformat())}
    return out


def q_ytd(con, q):
    """Year to date gegen Vorjahreszeitraum, monatlich aufgeschluesselt."""
    cond, args = where_clause(q)
    extra = (" AND " + " AND ".join(cond)) if cond else ""
    heute = date.fromisoformat(q.get("bis") or date.today().isoformat())
    cy0, cy1 = date(heute.year, 1, 1), heute
    py0, py1 = date(heute.year - 1, 1, 1), heute.replace(year=heute.year - 1)

    sql = """
    SELECT substr(u.datum,1,7) AS monat, substr(u.datum,6,2) AS mm, substr(u.datum,1,4) AS jahr,
      SUM(u.bruttoMitRabatt) AS brutto, SUM(u.stueck) AS stueck, SUM(u.belege) AS belege
    {frm}
    WHERE (u.datum BETWEEN ? AND ? OR u.datum BETWEEN ? AND ?){extra}
    GROUP BY monat ORDER BY monat
    """.format(frm=BASE_FROM, extra=extra)
    rows = [dict(r) for r in con.execute(
        sql, [cy0.isoformat(), cy1.isoformat(), py0.isoformat(), py1.isoformat()] + args).fetchall()]

    # Wie weit reicht der Cache ueberhaupt zurueck? Monate ohne
    # Vorjahresabdeckung duerfen NICHT gegen 0 verglichen werden,
    # sonst entstehen absurde Wachstumsraten.
    cov = con.execute("SELECT MIN(datum) a, MAX(datum) b FROM umsatz_tage").fetchone()
    cov_von = cov["a"]
    cov_bis = cov["b"]

    cur = {r["mm"]: r for r in rows if r["jahr"] == str(heute.year)}
    prev = {r["mm"]: r for r in rows if r["jahr"] == str(heute.year - 1)}
    monate = []
    for m in range(1, heute.month + 1):
        mm = "%02d" % m
        c, p = cur.get(mm), prev.get(mm)
        vj_start = date(heute.year - 1, m, 1)
        vj_ende = min(date(heute.year - 1, m, 28) + timedelta(days=4), py1)
        vj_ende = vj_ende.replace(day=1) - timedelta(days=1) if vj_ende.day < 5 else vj_ende
        vergleichbar = bool(cov_von and cov_bis
                            and cov_von <= vj_start.isoformat()
                            and cov_bis >= min(vj_ende, py1).isoformat())
        monate.append({
            "monat": mm,
            "brutto": (c or {}).get("brutto") or 0,
            "bruttoVJ": ((p or {}).get("brutto") or 0) if vergleichbar else None,
            "stueck": (c or {}).get("stueck") or 0,
            "stueckVJ": ((p or {}).get("stueck") or 0) if vergleichbar else None,
            "vergleichbar": vergleichbar,
        })

    verg = [m for m in monate if m["vergleichbar"]]
    tot_all = sum(m["brutto"] for m in monate)
    tot = sum(m["brutto"] for m in verg)
    totVJ = sum(m["bruttoVJ"] or 0 for m in verg)
    return {
        "monate": monate,
        "summe": tot_all,
        "summeVergleichbar": tot,
        "summeVJ": totVJ,
        "pct": ((tot / totVJ - 1) * 100) if totVJ else None,
        "monateVergleichbar": len(verg),
        "monateGesamt": len(monate),
        "cacheVon": cov_von, "cacheBis": cov_bis,
        "perioden": {"aktuell": [cy0.isoformat(), cy1.isoformat()],
                     "vorjahr": [py0.isoformat(), py1.isoformat()]}}


def q_bestand(con, q):
    """Bestand + Abverkauf der letzten N Tage -> Reichweite in Tagen."""
    tage = int(q.get("tage", 30))
    bis = q.get("bis") or date.today().isoformat()
    von = (date.fromisoformat(bis) - timedelta(days=tage - 1)).isoformat()
    cond, args = where_clause(q)
    extra = (" AND " + " AND ".join(cond)) if cond else ""
    filF = " AND b.filialeNr = %d" % int(q["filiale"]) if q.get("filiale") else ""

    sql = """
    WITH verkauf AS (
      SELECT u.artikelNr, u.filialeNr, SUM(u.stueck) AS stueck, SUM(u.bruttoMitRabatt) AS brutto
      {frm} WHERE u.datum BETWEEN ? AND ?{extra}
      GROUP BY u.artikelNr, u.filialeNr
    )
    SELECT b.artikelNr, b.filialeNr,
      COALESCE(f.kurzbezeichnung, f.bezeichnung, 'Filiale '||b.filialeNr) AS filiale,
      COALESCE(a.marke,'(ohne)') AS marke, COALESCE(a.submarke,'') AS submarke,
      COALESCE(a.bezeichnung,'') AS bezeichnung, COALESCE(a.warengruppe,'') AS warengruppe,
      b.bestand, b.reserviert, b.avisiert, b.meldemenge,
      COALESCE(v.stueck,0) AS abverkauf, COALESCE(v.brutto,0) AS umsatz
    FROM bestand b
    LEFT JOIN artikel a ON a.artikelNr = b.artikelNr
    LEFT JOIN filiale f ON f.filialeNr = b.filialeNr
    LEFT JOIN verkauf v ON v.artikelNr = b.artikelNr AND v.filialeNr = b.filialeNr
    WHERE 1=1 {filF}
    """.format(frm=BASE_FROM, extra=extra, filF=filF)

    # Dimensionsfilter auch auf die Bestandsseite anwenden
    dimcond, dimargs = [], []
    for field, col in (("marke", "a.marke"), ("submarke", "a.submarke"),
                       ("warengruppe", "a.warengruppe"), ("lieferant", "a.lieferant")):
        if q.get(field):
            dimcond.append("%s = ?" % col)
            dimargs.append(q[field])
    if dimcond:
        sql += " AND " + " AND ".join(dimcond)

    vt = verkaufstage(von, bis)
    rows = [dict(r) for r in con.execute(sql, [von, bis] + args + dimargs).fetchall()]
    for r in rows:
        pro_tag = (r["abverkauf"] or 0) / vt      # pro Verkaufstag, nicht pro Kalendertag
        r["proTag"] = pro_tag
        r["verfuegbar"] = (r["bestand"] or 0) - (r["reserviert"] or 0)
        r["reichweite"] = (r["bestand"] / pro_tag) if pro_tag > 0 else None
        r["umschlag"] = (r["abverkauf"] / r["bestand"]) if r["bestand"] else None
        if (r["bestand"] or 0) <= 0:
            r["status"] = "null"
        elif r["meldemenge"] and r["bestand"] < r["meldemenge"]:
            r["status"] = "unter"
        elif r["reichweite"] is not None and r["reichweite"] > 180:
            r["status"] = "ueber"
        elif pro_tag == 0:
            r["status"] = "steher"
        else:
            r["status"] = "ok"
    rows.sort(key=lambda r: r["umsatz"], reverse=True)
    return {"rows": rows[:5000], "tage": tage, "verkaufstage": vt,
            "von": von, "bis": bis, "gesamt": len(rows)}


# SQLite liefert 0 = Sonntag. Wir sortieren nach deutscher Woche ab Montag.
WTAG_NAME = {0: "Sonntag", 1: "Montag", 2: "Dienstag", 3: "Mittwoch",
             4: "Donnerstag", 5: "Freitag", 6: "Samstag"}
WTAG_KURZ = {0: "So", 1: "Mo", 2: "Di", 3: "Mi", 4: "Do", 5: "Fr", 6: "Sa"}
WTAG_SORT = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6}


def _wtag_rows(con, von, bis, cond, args):
    """Kennzahlen je Wochentag fuer einen Zeitraum."""
    where = " AND ".join(["u.datum BETWEEN ? AND ?"] + cond)
    sql = """
    SELECT CAST(strftime('%%w', u.datum) AS INTEGER) AS wtag,
           COUNT(DISTINCT u.datum) AS tage, %s
    %s WHERE %s GROUP BY wtag
    """ % (METRICS, BASE_FROM, where)
    out = {}
    for r in con.execute(sql, [von, bis] + args):
        d = marge(dict(r))
        out[r["wtag"]] = d
    return out


def q_wochentag(con, q):
    """Umsatz nach Wochentag - aktueller Zeitraum, Vorperiode und Vorjahr."""
    cond, args = where_clause(q)
    von, bis = q["von"], q["bis"]
    d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
    span = (d1 - d0).days + 1
    p0, p1 = d0 - timedelta(days=span), d0 - timedelta(days=1)
    y0, y1 = d0.replace(year=d0.year - 1), d1.replace(year=d1.year - 1)

    cur = _wtag_rows(con, von, bis, cond, args)
    vp = _wtag_rows(con, p0.isoformat(), p1.isoformat(), cond, args)
    vj = _wtag_rows(con, y0.isoformat(), y1.isoformat(), cond, args)

    ges = sum((c.get("brutto") or 0) for c in cur.values()) or 1
    schnitt_tage = sum((c.get("tage") or 0) for c in cur.values()) or 1
    schnitt = ges / schnitt_tage

    rows = []
    for w in sorted(WTAG_SORT, key=lambda x: WTAG_SORT[x]):
        c = cur.get(w)
        if not c and w not in vp and w not in vj:
            continue
        c = c or {}
        tage = c.get("tage") or 0
        brutto = c.get("brutto") or 0
        pro_tag = (brutto / tage) if tage else 0
        v, j = vp.get(w) or {}, vj.get(w) or {}
        pt_vp = ((v.get("brutto") or 0) / v["tage"]) if v.get("tage") else None
        pt_vj = ((j.get("brutto") or 0) / j["tage"]) if j.get("tage") else None
        rows.append({
            "wtag": w, "name": WTAG_NAME[w], "kurz": WTAG_KURZ[w],
            "tage": tage, "brutto": brutto, "stueck": c.get("stueck") or 0,
            "belege": c.get("belege") or 0, "rohertrag": c.get("rohertrag") or 0,
            "marge": c.get("marge"),
            "proTag": pro_tag,
            "anteil": brutto / ges * 100,
            "index": (pro_tag / schnitt * 100) if schnitt else None,
            "bonwert": (brutto / c["belege"]) if c.get("belege") else None,
            "belegeProTag": (c.get("belege") or 0) / tage if tage else None,
            "proTagVP": pt_vp, "proTagVJ": pt_vj,
            "pctVP": ((pro_tag / pt_vp - 1) * 100) if pt_vp else None,
            "pctVJ": ((pro_tag / pt_vj - 1) * 100) if pt_vj else None,
        })

    aktiv = [r for r in rows if r["tage"]]
    bester = max(aktiv, key=lambda r: r["proTag"]) if aktiv else None
    schwaechster = min(aktiv, key=lambda r: r["proTag"]) if aktiv else None
    return {"rows": rows, "schnittProTag": schnitt, "gesamt": ges,
            "bester": bester["name"] if bester else None,
            "schwaechster": schwaechster["name"] if schwaechster else None,
            "spreizung": (bester["proTag"] / schwaechster["proTag"])
                         if (bester and schwaechster and schwaechster["proTag"]) else None,
            "perioden": {"aktuell": [von, bis],
                         "vorperiode": [p0.isoformat(), p1.isoformat()],
                         "vorjahr": [y0.isoformat(), y1.isoformat()]}}


def q_luecken(con, q):
    """Welche Tage im Zeitraum fehlen im Cache?

    Das ist der gefaehrlichste stille Fehler: ein fehlender Tag senkt jeden
    Perioden- und Vorjahresvergleich, ohne dass es auffaellt."""
    bis = q.get("bis") or date.today().isoformat()
    von = q.get("von") or (date.fromisoformat(bis) - timedelta(days=START_TAGE)).isoformat()
    mit_so = sonntag_aktiv(q)
    have = {r["datum"] for r in con.execute(
        "SELECT datum FROM umsatz_tage WHERE datum BETWEEN ? AND ?", (von, bis))}
    fehlend, sonntage, d = [], 0, date.fromisoformat(von)
    d1 = date.fromisoformat(bis)
    while d <= d1:
        if not mit_so and d.weekday() == 6:
            sonntage += 1          # Schliesstag, keine Luecke
        elif d.isoformat() not in have:
            fehlend.append(d.isoformat())
        d += timedelta(days=1)

    # Zusammenhaengende Luecken zu Bloecken buendeln. Ein dazwischenliegender
    # Sonntag trennt einen Block nicht, sonst zerfaellt jede Luecke in Wochen.
    bloecke = []
    for t in fehlend:
        if bloecke:
            luecke = (date.fromisoformat(t) - date.fromisoformat(bloecke[-1]["bis"])).days
            nur_sonntag = (not mit_so and luecke == 2
                           and date.fromisoformat(bloecke[-1]["bis"]).weekday() == 5)
            if luecke == 1 or nur_sonntag:
                bloecke[-1]["bis"] = t
                bloecke[-1]["tage"] += 1
                continue
        bloecke.append({"von": t, "bis": t, "tage": 1})

    # Tage ohne jede Umsatzzeile (geladen, aber leer) - meist Sonntage/Feiertage
    leer = [r["datum"] for r in con.execute(
        "SELECT datum FROM umsatz_tage WHERE datum BETWEEN ? AND ? AND positionen = 0 ORDER BY datum",
        (von, bis))]
    # Verdaechtig sind leere Tage, die keine Sonntage sind: meist wurden sie zu
    # frueh am Tag geholt, als noch nichts gebucht war. Die lassen sich gezielt
    # noch einmal laden.
    verdaechtig = [t for t in leer if date.fromisoformat(t).weekday() != 6]
    kalender = (d1 - date.fromisoformat(von)).days + 1
    gesamt = kalender - sonntage          # nur Verkaufstage sind relevant
    # Wurde an Sonntagen trotzdem gebucht? Meist ein angebundener Webshop.
    so = con.execute(
        "SELECT COUNT(DISTINCT datum) t, COALESCE(SUM(bruttoMitRabatt),0) s FROM umsatz "
        "WHERE datum BETWEEN ? AND ? AND strftime('%w', datum) = '0'", (von, bis)).fetchone()
    return {"von": von, "bis": bis, "tageGesamt": gesamt, "kalendertage": kalender,
            "sonntageUebersprungen": sonntage,
            "geladen": gesamt - len(fehlend), "fehlend": len(fehlend),
            "abdeckung": (gesamt - len(fehlend)) / gesamt * 100 if gesamt else 0,
            "bloecke": bloecke[:200], "leereTage": leer[:200],
            "verdaechtigeTage": verdaechtig[:200], "verdaechtig": len(verdaechtig),
            "sonntagsUmsatz": so["s"], "sonntageMitUmsatz": so["t"]}


def q_markenmonat(con, q):
    """Abverkauf ausgewaehlter Marken je Monat.

    Ohne 'monat' kommen die Monatssummen samt Linie je Marke zurueck.
    Mit 'monat=2026-07' die Artikel, die in genau diesem Monat verkauft wurden."""
    bis = q.get("bis") or date.today().isoformat()
    von = q.get("von") or (date.fromisoformat(bis) - timedelta(days=364)).isoformat()
    # Mehrere Marken kommen mit | getrennt (Komma waere unsicher, weil
    # Markennamen selbst Kommas enthalten koennen). Aeltere Aufrufe mit Komma
    # funktionieren weiterhin.
    roh = q.get("marken") or ""
    trenner = q.get("sep") or ("|" if "|" in roh else ",")
    marken = [m.strip() for m in roh.split(trenner) if m.strip()][:40]

    # Die Standardfilter (Filiale, Kanal, Sonntage, weitere Dimensionen) kommen
    # aus where_clause -- sie kennt Feinheiten wie den Kanalwert "all" fuer
    # „Alle Kanaele". Eigene Filterlogik hier waere fehleranfaellig.
    bed, args = ["u.datum BETWEEN ? AND ?"], [von, bis]
    if marken:
        # Die gewaehlten Namen zuerst im kleinen Artikelstamm auf die exakten
        # Werte aufloesen (Leerzeichen, Gross-/Kleinschreibung egal). Danach
        # laeuft der eigentliche Filter ohne Umwege auf der grossen Umsatztabelle.
        echte = [r["marke"] for r in con.execute(
            "SELECT DISTINCT marke FROM artikel WHERE norm_txt(marke) IN (%s)"
            % ",".join("?" * len(marken)), [norm_txt(m) for m in marken])
            if r["marke"] is not None]
        werte = echte or marken
        bed.append("COALESCE(a.marke,'(ohne Marke)') IN (%s)"
                   % ",".join("?" * len(werte)))
        args += werte
    std_bed, std_args = where_clause(q)
    bed += std_bed
    args += std_args
    where = " AND ".join(bed)

    # --- Einzelmonat: welche Artikel liefen in diesem Monat?
    monat = (q.get("monat") or "").strip()
    if monat:
        sql = """
        SELECT u.artikelNr AS artikelNr, COALESCE(a.bezeichnung,'') AS bezeichnung,
          TRIM(COALESCE(a.marke,'(ohne Marke)')) AS marke,
          COALESCE(a.submarke,'') AS submarke,
          COALESCE(a.warengruppe,'') AS warengruppe, COALESCE(a.ekPreis,0) AS ekPreis,
          {metrics}
        {frm} WHERE {where} AND substr(u.datum,1,7) = ?
        GROUP BY u.artikelNr ORDER BY brutto DESC
        """.format(metrics=METRICS, frm=BASE_FROM, where=where)
        artikel = [marge(dict(r)) for r in con.execute(sql, args + [monat])]
        # Summen fuer die Fusszeile. Sell-out = Umsatz mit Kunden,
        # Sell-in = Wareneinsatz der verkauften Artikel (Stueck x EK).
        s_brutto = sum(a["brutto"] or 0 for a in artikel)
        s_ware = sum(a.get("wareneinsatz") or 0 for a in artikel)
        s_rohertrag = sum(a["rohertrag"] or 0 for a in artikel)
        s_nettoEk = sum(a.get("nettoMitEk") or 0 for a in artikel)
        return {"monat": monat, "artikel": artikel, "anzahl": len(artikel),
                "stueck": sum(a["stueck"] or 0 for a in artikel),
                "brutto": s_brutto, "sellOut": s_brutto,
                "wareneinsatz": s_ware, "sellIn": s_ware,
                "rohertrag": s_rohertrag,
                "marge": (s_rohertrag / s_nettoEk * 100) if s_nettoEk else None,
                "ekAbdeckung": (s_nettoEk / sum(a["netto"] or 0 for a in artikel) * 100)
                               if sum(a["netto"] or 0 for a in artikel) else None}

    # --- Uebersicht
    # Die Kurve laesst sich feiner aufloesen als die Tabelle: bei einem einzelnen
    # Monat sind Tageswerte aussagekraeftiger als ein einziger Punkt.
    gran = (q.get("gran") or "monat").lower()
    kurve_ausdruck = "u.datum" if gran == "tag" else "substr(u.datum,1,7)"

    def gruppiert(ausdruck):
        sql = """
        SELECT {ausdruck} AS periode,
          TRIM(COALESCE(a.marke,'(ohne Marke)')) AS marke,
          {metrics}
        {frm} WHERE {where}
        GROUP BY periode, marke ORDER BY periode
        """.format(ausdruck=ausdruck, metrics=METRICS, frm=BASE_FROM, where=where)
        je_periode, je_marke, perioden = {}, {}, []
        for r in con.execute(sql, args):
            d = marge(dict(r))
            p, m = r["periode"], r["marke"]
            if p not in perioden:
                perioden.append(p)
            z = je_periode.setdefault(p, {"monat": p, "stueck": 0, "brutto": 0.0,
                                          "netto": 0.0, "rohertrag": 0.0, "belege": 0,
                                          "artikel": 0, "nettoMitEk": 0.0})
            z["stueck"] += d["stueck"] or 0
            z["brutto"] += d["brutto"] or 0
            z["netto"] += d["netto"] or 0
            z["rohertrag"] += d["rohertrag"] or 0
            z["belege"] += d["belege"] or 0
            z["artikel"] += d["artikel"] or 0
            z["nettoMitEk"] += d.get("nettoMitEk") or 0
            je_marke.setdefault(m, {})[p] = d
        for z in je_periode.values():
            z["marge"] = (z["rohertrag"] / z["nettoMitEk"] * 100) if z["nettoMitEk"] else None
            z["bonwert"] = (z["brutto"] / z["belege"]) if z["belege"] else None
        return je_periode, je_marke, perioden

    # Tabelle immer nach Monaten
    je_monat, marken_monat, monats_perioden = gruppiert("substr(u.datum,1,7)")
    # Kurve in der gewuenschten Aufloesung
    if gran == "tag":
        _, je_marke, perioden = gruppiert(kurve_ausdruck)
    else:
        je_marke, perioden = marken_monat, monats_perioden

    monate = [je_monat[p] for p in monats_perioden]
    # Veraenderung zum Vormonat und zum gleichen Monat im Vorjahr
    for i, z in enumerate(monate):
        vor = monate[i - 1] if i else None
        z["pctVM"] = ((z["stueck"] / vor["stueck"] - 1) * 100) \
            if (vor and vor["stueck"]) else None
        vj = je_monat.get("%04d-%s" % (int(z["monat"][:4]) - 1, z["monat"][5:]))
        z["stueckVJ"] = vj["stueck"] if vj else None
        z["pctVJ"] = ((z["stueck"] / vj["stueck"] - 1) * 100) \
            if (vj and vj["stueck"]) else None

    serien = []
    for m in sorted(je_marke, key=lambda k: -sum((v.get("brutto") or 0)
                                                 for v in je_marke[k].values())):
        serien.append({"dim": m,
                       "werte": [(je_marke[m].get(p, {}).get("stueck") or 0)
                                 for p in perioden],
                       "umsatz": [(je_marke[m].get(p, {}).get("brutto") or 0)
                                  for p in perioden],
                       "summe": sum((v.get("brutto") or 0) for v in je_marke[m].values()),
                       "stueckSumme": sum((v.get("stueck") or 0) for v in je_marke[m].values())})

    erg = {"von": von, "bis": bis, "marken": marken, "perioden": perioden,
           "granularitaet": gran, "monate": monate, "serien": serien,
           "gesamt": {"stueck": sum(z["stueck"] for z in monate),
                      "brutto": sum(z["brutto"] for z in monate),
                      "rohertrag": sum(z["rohertrag"] for z in monate),
                      "monate": len(monate)}}

    # Nichts gefunden, obwohl gefiltert wurde? Dann nachsehen, ob es die Marke
    # ueberhaupt gibt und ob im Zeitraum Umsatz vorliegt -- sonst raetselt man
    # vor einer leeren Seite.
    if marken and not monate:
        platz = ",".join("?" * len(marken))
        norm = [norm_txt(m) for m in marken]
        vorhanden = con.execute(
            "SELECT COUNT(*) n FROM artikel WHERE norm_txt(marke) IN (%s)" % platz,
            norm).fetchone()["n"]
        zeitraum = con.execute(
            "SELECT COUNT(*) n FROM umsatz WHERE datum BETWEEN ? AND ?",
            (von, bis)).fetchone()["n"]
        # Gab es zu dieser Marke ueberhaupt jemals Verkaeufe? Bewusst mit
        # denselben Filtern wie oben, nur ohne Datumsgrenze -- sonst koennte
        # die Meldung dem Ergebnis widersprechen.
        ohne_datum = [b for b in bed if not b.startswith("u.datum BETWEEN")]
        je = con.execute(
            "SELECT COUNT(*) n, MIN(u.datum) erster, MAX(u.datum) letzter %s WHERE %s"
            % (BASE_FROM, " AND ".join(ohne_datum) or "1=1"), args[2:]).fetchone()
        namen = ", ".join(marken)
        if not zeitraum:
            erg["hinweis"] = ("Im Zeitraum %s bis %s sind überhaupt keine Umsätze "
                              "gespeichert." % (von, bis))
        elif not vorhanden:
            erg["hinweis"] = ("Zu %s gibt es keine Artikel im Artikelstamm. Steht die "
                              "Marke dort vielleicht anders geschrieben?" % namen)
        elif not je["n"]:
            erg["hinweis"] = (
                "%d Artikel von %s stehen im Artikelstamm, es sind aber zu keinem "
                "davon Verkäufe gespeichert — auch nicht außerhalb des Zeitraums. "
                "Das deutet darauf hin, dass die Artikelnummern in den Umsätzen "
                "nicht zum Artikelstamm passen." % (vorhanden, namen))
        else:
            erg["hinweis"] = (
                "%s wurde im Zeitraum %s bis %s nicht verkauft. Verkäufe liegen vor "
                "von %s bis %s (%d Positionen) — bitte den Zeitraum anpassen."
                % (namen, von, bis, je["erster"], je["letzter"], je["n"]))
        erg["diagnose"] = {"artikelImStamm": vorhanden, "positionenGesamt": je["n"],
                           "ersterVerkauf": je["erster"], "letzterVerkauf": je["letzter"],
                           "umsatzzeilenImZeitraum": zeitraum}
    return erg


def q_penner(con, q):
    """Ladenhueter: Artikel mit Bestand, die im Zeitraum kein einziges Mal
    verkauft wurden -- nach Marke gebuendelt.

    Das ist totes Kapital im Regal. Der Zeitraum ist frei waehlbar; mit dem
    Jahresanfang als Startdatum sieht man, was dieses Jahr noch gar nicht
    gelaufen ist."""
    bis = q.get("bis") or date.today().isoformat()
    von = q.get("von") or date(date.fromisoformat(bis).year, 1, 1).isoformat()
    nur_bestand = q.get("nurBestand", "1") != "0"     # ohne Bestand meist uninteressant
    filF = " AND b.filialeNr = %d" % int(q["filiale"]) if q.get("filiale") else ""

    # Dimensionsfilter auf den Artikelstamm
    dimcond, dimargs = [], []
    for feld, spalte in (("marke", "a.marke"), ("submarke", "a.submarke"),
                         ("warengruppe", "a.warengruppe"), ("lieferant", "a.lieferant")):
        if q.get(feld):
            dimcond.append("%s = ?" % spalte)
            dimargs.append(q[feld])
    dimwhere = (" AND " + " AND ".join(dimcond)) if dimcond else ""

    sql = """
    WITH lager AS (
      SELECT b.artikelNr,
             SUM(b.bestand) AS bestand,
             SUM(COALESCE(b.reserviert,0)) AS reserviert
      FROM bestand b WHERE 1=1 {filF}
      GROUP BY b.artikelNr
    ),
    verkauf AS (
      SELECT u.artikelNr, SUM(u.stueck) AS stueck, SUM(u.bruttoMitRabatt) AS umsatz
      FROM umsatz u WHERE u.datum BETWEEN ? AND ?
      GROUP BY u.artikelNr
    ),
    letzter AS (
      SELECT artikelNr, MAX(datum) AS tag FROM umsatz GROUP BY artikelNr
    )
    SELECT a.artikelNr, COALESCE(a.bezeichnung,'') AS bezeichnung,
      COALESCE(a.marke,'(ohne Marke)') AS marke,
      COALESCE(a.submarke,'') AS submarke,
      COALESCE(a.warengruppe,'') AS warengruppe,
      COALESCE(a.lieferant,'') AS lieferant,
      COALESCE(a.ekPreis,0) AS ekPreis, COALESCE(a.vkPreis,0) AS vkPreis,
      COALESCE(l.bestand,0) AS bestand, COALESCE(l.reserviert,0) AS reserviert,
      COALESCE(v.stueck,0) AS stueck, COALESCE(v.umsatz,0) AS umsatz,
      lz.tag AS letzterVerkauf
    FROM artikel a
    LEFT JOIN lager l ON l.artikelNr = a.artikelNr
    LEFT JOIN verkauf v ON v.artikelNr = a.artikelNr
    LEFT JOIN letzter lz ON lz.artikelNr = a.artikelNr
    WHERE COALESCE(v.stueck,0) = 0 {bestandF}{dimwhere}
    """.format(filF=filF, dimwhere=dimwhere,
               bestandF=" AND COALESCE(l.bestand,0) > 0" if nur_bestand else "")

    heute = date.fromisoformat(bis)
    artikel = []
    for r in con.execute(sql, [von, bis] + dimargs):
        d = dict(r)
        d["kapital"] = (d["bestand"] or 0) * (d["ekPreis"] or 0)
        d["potenzial"] = (d["bestand"] or 0) * (d["vkPreis"] or 0)
        if d["letzterVerkauf"]:
            d["tageOhneVerkauf"] = (heute - date.fromisoformat(d["letzterVerkauf"])).days
        else:
            d["tageOhneVerkauf"] = None       # noch nie verkauft
        artikel.append(d)

    # Nach Marke buendeln
    marken = {}
    for a in artikel:
        m = marken.setdefault(a["marke"], {
            "dim": a["marke"], "artikel": 0, "bestand": 0, "kapital": 0.0,
            "potenzial": 0.0, "nieVerkauft": 0, "aeltester": None})
        m["artikel"] += 1
        m["bestand"] += a["bestand"] or 0
        m["kapital"] += a["kapital"]
        m["potenzial"] += a["potenzial"]
        if a["letzterVerkauf"] is None:
            m["nieVerkauft"] += 1
        elif m["aeltester"] is None or a["letzterVerkauf"] < m["aeltester"]:
            m["aeltester"] = a["letzterVerkauf"]

    reihen = sorted(marken.values(), key=lambda r: -r["kapital"])
    gesamt_kapital = sum(r["kapital"] for r in reihen)
    for r in reihen:
        r["anteil"] = (r["kapital"] / gesamt_kapital * 100) if gesamt_kapital else 0

    # Vergleichsgroesse: wie viele Artikel mit Bestand gibt es ueberhaupt?
    mit_bestand = con.execute(
        "SELECT COUNT(DISTINCT b.artikelNr) n, COALESCE(SUM(b.bestand * "
        "COALESCE(a.ekPreis,0)),0) k FROM bestand b "
        "LEFT JOIN artikel a ON a.artikelNr=b.artikelNr "
        "WHERE b.bestand > 0%s" % filF.replace(" AND b.filialeNr", " AND b.filialeNr")
    ).fetchone()

    artikel.sort(key=lambda r: -r["kapital"])
    return {
        "von": von, "bis": bis,
        "marken": reihen,
        "artikel": artikel[:2000],
        "anzahl": len(artikel),
        "bestandStueck": sum(a["bestand"] or 0 for a in artikel),
        "kapital": gesamt_kapital,
        "potenzial": sum(a["potenzial"] for a in artikel),
        "nieVerkauft": sum(1 for a in artikel if a["letzterVerkauf"] is None),
        "artikelMitBestand": mit_bestand["n"] or 0,
        "kapitalGesamt": mit_bestand["k"] or 0.0,
    }


def q_kapital(con, q):
    """Bestandswert und gebundenes Kapital. Braucht EK-Preise im Artikelstamm."""
    dim = q.get("dim", "marke")
    expr = {"marke": "COALESCE(a.marke,'(ohne Marke)')",
            "submarke": "COALESCE(a.submarke,'(ohne Submarke)')",
            "warengruppe": "COALESCE(a.warengruppe,'(ohne WG)')",
            "lieferant": "COALESCE(a.lieferant,'(ohne Lieferant)')",
            "filiale": "COALESCE(f.kurzbezeichnung, f.bezeichnung, 'Filiale '||b.filialeNr)",
            }.get(dim, "COALESCE(a.marke,'(ohne Marke)')")
    tage = int(q.get("tage", 90))
    bis = q.get("bis") or (con.execute("SELECT MAX(datum) d FROM umsatz_tage").fetchone()["d"]
                           or date.today().isoformat())
    von = (date.fromisoformat(bis) - timedelta(days=tage - 1)).isoformat()

    dimcond, dimargs = [], []
    for field, col in (("marke", "a.marke"), ("submarke", "a.submarke"),
                       ("warengruppe", "a.warengruppe"), ("lieferant", "a.lieferant")):
        if q.get(field):
            dimcond.append("%s = ?" % col)
            dimargs.append(q[field])
    if q.get("filiale"):
        dimcond.append("b.filialeNr = ?")
        dimargs.append(int(q["filiale"]))
    where = (" AND " + " AND ".join(dimcond)) if dimcond else ""

    vt = verkaufstage(von, bis)
    so_filter = "" if sonntag_aktiv(q) else " AND strftime('%w', datum) <> '0'"
    sql = """
    WITH v AS (SELECT artikelNr, filialeNr, SUM(stueck) s, SUM(bruttoMitRabatt) umsatz
               FROM umsatz WHERE datum BETWEEN ? AND ?""" + so_filter + """
               GROUP BY artikelNr, filialeNr)
    SELECT {expr} AS dim,
      SUM(b.bestand) AS stueck,
      SUM(b.bestand * COALESCE(a.ekPreis,0)) AS wert,
      SUM(CASE WHEN a.ekPreis > 0 THEN b.bestand ELSE 0 END) AS stueckMitEk,
      COUNT(*) AS positionen,
      COUNT(DISTINCT b.artikelNr) AS artikel,
      SUM(COALESCE(v.s,0)) AS abverkauf,
      SUM(COALESCE(v.umsatz,0)) AS umsatz,
      SUM(CASE WHEN COALESCE(v.s,0) = 0 THEN b.bestand * COALESCE(a.ekPreis,0) ELSE 0 END) AS wertSteher,
      SUM(CASE WHEN COALESCE(v.s,0) > 0
                AND b.bestand / (v.s / {tage}.0) > 180
               THEN b.bestand * COALESCE(a.ekPreis,0) ELSE 0 END) AS wertUeber
    FROM bestand b
    LEFT JOIN artikel a ON a.artikelNr = b.artikelNr
    LEFT JOIN filiale f ON f.filialeNr = b.filialeNr
    LEFT JOIN v ON v.artikelNr = b.artikelNr AND v.filialeNr = b.filialeNr
    WHERE 1=1 {where}
    GROUP BY dim ORDER BY wert DESC
    """.format(expr=expr, tage=vt, where=where)
    rows = [dict(r) for r in con.execute(sql, [von, bis] + dimargs).fetchall()]
    ges = sum(r["wert"] or 0 for r in rows) or 1
    # Reichweite in Verkaufstagen: an Sonntagen wird nichts abverkauft,
    # also darf auch nicht durch Sonntage geteilt werden.
    vt_jahr = verkaufstage("%d-01-01" % (date.fromisoformat(bis).year - 1),
                           "%d-12-31" % (date.fromisoformat(bis).year - 1))
    for r in rows:
        pro_tag = (r["abverkauf"] or 0) / vt
        r["anteil"] = (r["wert"] or 0) / ges * 100
        r["reichweite"] = (r["stueck"] / pro_tag) if pro_tag > 0 else None
        # Wie oft dreht sich der Bestandswert im Jahr?
        r["umschlagJahr"] = (r["abverkauf"] / r["stueck"] * (vt_jahr / vt)) if r["stueck"] else None
        r["totesKapital"] = (r["wertSteher"] or 0) + (r["wertUeber"] or 0)
        r["totesKapitalAnteil"] = (r["totesKapital"] / r["wert"] * 100) if r["wert"] else None
    return {"rows": rows, "tage": tage, "verkaufstage": vt, "von": von, "bis": bis,
            "gesamtwert": ges, "totesKapitalGesamt": sum(r["totesKapital"] for r in rows)}


def q_sortiment(con, q):
    """Sortimentsbreite gegen Ertrag: wie viele Listplaetze traegt eine Marke wirklich?"""
    dim = q.get("dim", "marke")
    expr = DIMS.get(dim, DIMS["marke"])
    cond, args = where_clause(q)
    where = " AND ".join(["u.datum BETWEEN ? AND ?"] + cond)

    # Umsatz je Artikel innerhalb der Dimension
    sql = """
    SELECT {expr} AS dim, u.artikelNr AS art,
      SUM(u.bruttoMitRabatt) AS brutto,
      SUM(CASE WHEN a.ekPreis > 0 THEN u.bruttoMitRabatt - u.mwstMit - u.stueck*a.ekPreis
               ELSE 0 END) AS rohertrag
    {frm} WHERE {where}
    GROUP BY dim, u.artikelNr
    """.format(expr=expr, frm=BASE_FROM, where=where)
    per_art = {}
    for r in con.execute(sql, [q["von"], q["bis"]] + args):
        per_art.setdefault(r["dim"], []).append((r["brutto"] or 0, r["rohertrag"] or 0))

    # Gelistete Artikel je Dimension (aus dem Bestand, unabhaengig vom Verkauf)
    gelistet = {}
    col = {"marke": "a.marke", "submarke": "a.submarke", "warengruppe": "a.warengruppe",
           "lieferant": "a.lieferant"}.get(dim)
    if col:
        for r in con.execute("""SELECT COALESCE(%s,'(ohne)') d, COUNT(DISTINCT b.artikelNr) c
                                FROM bestand b LEFT JOIN artikel a ON a.artikelNr=b.artikelNr
                                WHERE b.bestand > 0 GROUP BY d""" % col):
            gelistet[r["d"]] = r["c"]

    rows = []
    for name, arts in per_art.items():
        arts.sort(reverse=True)
        ges = sum(a[0] for a in arts) or 1
        roh = sum(a[1] for a in arts)
        # Wie viele Artikel machen 80 % des Umsatzes?
        run, kern = 0.0, 0
        for b, _ in arts:
            run += b
            kern += 1
            if run / ges >= 0.8:
                break
        tail = [a for a in arts if a[0] / ges < 0.002]   # unter 0,2 % Umsatzanteil
        rows.append({
            "dim": name, "brutto": ges, "rohertrag": roh,
            "artikelMitUmsatz": len(arts), "artikelGelistet": gelistet.get(name),
            "kernartikel": kern, "kernanteil": kern / len(arts) * 100,
            "ertragProArtikel": roh / len(arts),
            "umsatzProArtikel": ges / len(arts),
            "tailArtikel": len(tail), "tailUmsatz": sum(a[0] for a in tail),
            "tailAnteil": sum(a[0] for a in tail) / ges * 100,
            "karteileichen": (gelistet.get(name) - len(arts)) if gelistet.get(name) else None,
        })
    rows.sort(key=lambda r: r["rohertrag"], reverse=True)
    return {"rows": rows, "dim": dim}


def q_filialbenchmark(con, q):
    """Filialvergleich normalisiert - absolute Umsaetze bevorzugen immer die groesste Filiale."""
    cond, args = where_clause(q)
    where = " AND ".join(["u.datum BETWEEN ? AND ?"] + cond)
    tage = verkaufstage(q["von"], q["bis"])   # Sonntage zaehlen nicht als Verkaufstag
    sql = """
    SELECT u.filialeNr AS nr,
      COALESCE(f.kurzbezeichnung, f.bezeichnung, 'Filiale '||u.filialeNr) AS dim,
      COALESCE(f.anzahlKassen,0) AS kassen, COALESCE(f.webshopfiliale,0) AS webshop,
      {metrics}
    {frm} WHERE {where}
    GROUP BY u.filialeNr ORDER BY brutto DESC
    """.format(metrics=METRICS, frm=BASE_FROM, where=where)
    rows = [marge(dict(r)) for r in con.execute(sql, [q["von"], q["bis"]] + args).fetchall()]
    for r in rows:
        k = r["kassen"] or 0
        r["umsatzProKasse"] = (r["brutto"] / k) if k else None
        r["umsatzProTag"] = r["brutto"] / tage
        r["umsatzProKasseTag"] = (r["brutto"] / k / tage) if k else None
        r["bonwert"] = (r["brutto"] / r["belege"]) if r["belege"] else None
        r["stueckProBeleg"] = (r["stueck"] / r["belege"]) if r["belege"] else None
    # Onlineshop einmal zentral bestimmen. Der Name geht vor, das
    # NEO-Kennzeichen zaehlt nur, wenn es genau eine Zeile markiert -- sonst
    # wuerde eine falsch gesetzte Markierung ganze Ladenfilialen ausschliessen.
    nach_name = [r for r in rows if ist_onlinefiliale(r.get("dim"))]
    if nach_name:
        online_zeilen = nach_name
    else:
        markiert = [r for r in rows if r.get("webshop")]
        online_zeilen = markiert if len(markiert) == 1 else []
    for r in rows:
        r["istOnline"] = r in online_zeilen

    rows = shopify_filialzeile(rows, q)

    # Der Vergleichsschnitt gilt nur fuer stationaere Filialen
    ref = [r["umsatzProKasseTag"] for r in rows
           if r["umsatzProKasseTag"] and not r["istOnline"]]
    schnitt = sum(ref) / len(ref) if ref else None
    for r in rows:
        r["vsSchnitt"] = ((r["umsatzProKasseTag"] / schnitt - 1) * 100) \
            if (schnitt and r["umsatzProKasseTag"] and not r["istOnline"]) else None
    rows.sort(key=lambda r: -(r.get("brutto") or 0))
    online = any(r.get("quelle") == "shopify" for r in rows)
    return {"rows": rows, "tage": tage, "schnittProKasseTag": schnitt,
            "onlineQuelle": "shopify" if online else None}


def shopify_filialzeile(rows, q):
    """Ersetzt im Filialvergleich die Onlineshop-Zeile durch die Shopify-Zahlen.

    Umsatz, Bestellungen, Stueck und Bonwert sind dann Tatsachen aus Shopify.
    Die Marge kennt Shopify nicht (dort fehlen die Einkaufspreise); sie bleibt
    die Rate aus dem NEO-Sortiment, der Rohertrag wird passend dazu auf den
    echten Umsatz hochgerechnet."""
    if neo_shopify is None or not neo_shopify.konfiguriert():
        return rows

    # istOnline wurde vorher zentral bestimmt (Name vor NEO-Kennzeichen).
    online = [r for r in rows if r.get("istOnline")]
    if not online:
        return rows
    haupt = max(online, key=lambda r: (r.get("brutto") or 0))

    try:
        c2 = db()
        try:
            s = neo_shopify.filialzeile(c2, q["von"], q["bis"])
        finally:
            c2.close()
    except Exception:                                   # noqa: BLE001
        return rows
    if not s or not s.get("brutto"):
        return rows

    rate = haupt.get("marge")            # Margenrate aus dem NEO-Sortiment
    haupt["brutto"] = s["brutto"]
    haupt["belege"] = s["belege"] or haupt.get("belege")
    if s.get("stueck"):
        haupt["stueck"] = s["stueck"]
    haupt["bonwert"] = s["bonwert"]
    haupt["stueckProBeleg"] = s["stueckProBeleg"]
    haupt["umsatzProTag"] = s["brutto"] / max(1, verkaufstage(q["von"], q["bis"]))
    k = haupt.get("kassen") or 0
    haupt["umsatzProKasse"] = (s["brutto"] / k) if k else None
    haupt["umsatzProKasseTag"] = (haupt["umsatzProTag"] / k) if k else None
    if rate is not None:
        haupt["rohertrag"] = s["brutto"] * rate / 100.0
        haupt["margeGeschaetzt"] = True
    haupt["quelle"] = "shopify"
    return rows


def q_neuheiten(con, q):
    """Artikel nach erstem Verkaufstag - tragen Neulistungen oder kosten sie nur Platz?"""
    tage = int(q.get("tage", 90))
    bis = q.get("bis") or (con.execute("SELECT MAX(datum) d FROM umsatz_tage").fetchone()["d"]
                           or date.today().isoformat())
    ab = (date.fromisoformat(bis) - timedelta(days=tage - 1)).isoformat()
    # Cache-Start kennen: Artikel, die am ersten Cache-Tag schon liefen, sind nicht "neu"
    start = con.execute("SELECT MIN(datum) d FROM umsatz_tage").fetchone()["d"]

    dimcond, dimargs = [], []
    for field, col in (("marke", "a.marke"), ("submarke", "a.submarke"),
                       ("warengruppe", "a.warengruppe"), ("lieferant", "a.lieferant")):
        if q.get(field):
            dimcond.append("%s = ?" % col)
            dimargs.append(q[field])
    where = (" AND " + " AND ".join(dimcond)) if dimcond else ""

    sql = """
    WITH erst AS (SELECT artikelNr, MIN(datum) d FROM umsatz GROUP BY artikelNr)
    SELECT u.artikelNr AS artikelNr, e.d AS ersterVerkauf,
      COALESCE(a.bezeichnung,'') AS bezeichnung, COALESCE(a.marke,'') AS marke,
      COALESCE(a.warengruppe,'') AS warengruppe,
      SUM(u.bruttoMitRabatt) AS brutto, SUM(u.stueck) AS stueck,
      SUM(CASE WHEN a.ekPreis > 0 THEN u.bruttoMitRabatt - u.mwstMit - u.stueck*a.ekPreis
               ELSE 0 END) AS rohertrag,
      COUNT(DISTINCT u.datum) AS verkaufstage,
      COUNT(DISTINCT u.filialeNr) AS filialen
    FROM umsatz u
    JOIN erst e ON e.artikelNr = u.artikelNr
    LEFT JOIN artikel a ON a.artikelNr = u.artikelNr
    WHERE e.d >= ? AND e.d > ? {where}
    GROUP BY u.artikelNr ORDER BY brutto DESC
    """.format(where=where)
    rows = [dict(r) for r in con.execute(sql, [ab, start] + dimargs).fetchall()]
    for r in rows:
        alter = (date.fromisoformat(bis) - date.fromisoformat(r["ersterVerkauf"])).days + 1
        r["alterTage"] = alter
        r["umsatzProTag"] = r["brutto"] / alter
        r["marge"] = (r["rohertrag"] / (r["brutto"] / 1.19) * 100) if r["brutto"] else None
    return {"rows": rows[:2000], "tage": tage, "ab": ab, "bis": bis,
            "cacheStart": start, "anzahl": len(rows),
            "umsatz": sum(r["brutto"] for r in rows),
            "rohertrag": sum(r["rohertrag"] for r in rows)}


def q_artikelsuche(con, q):
    """Volltextsuche über Artikelnummer, Bezeichnung, GTIN und Marke."""
    begriff = (q.get("q") or "").strip()
    if len(begriff) < 2:
        return {"treffer": [], "hinweis": "Bitte mindestens zwei Zeichen eingeben."}
    von = q.get("von") or (date.today() - timedelta(days=364)).isoformat()
    bis = q.get("bis") or date.today().isoformat()
    like = "%" + begriff.replace("%", "").lower() + "%"
    ist_nr = begriff.isdigit()

    sql = """
    SELECT a.artikelNr, a.bezeichnung, a.marke, a.submarke, a.warengruppe, a.lieferant,
           a.status, a.gtin, a.ekPreis, a.vkPreis, a.uvpPreis,
           COALESCE(u.brutto,0) AS brutto, COALESCE(u.stueck,0) AS stueck,
           COALESCE(b.bestand,0) AS bestand
    FROM artikel a
    LEFT JOIN (SELECT artikelNr, SUM(bruttoMitRabatt) brutto, SUM(stueck) stueck
               FROM umsatz WHERE datum BETWEEN ? AND ? GROUP BY artikelNr) u
           ON u.artikelNr = a.artikelNr
    LEFT JOIN (SELECT artikelNr, SUM(bestand) bestand FROM bestand GROUP BY artikelNr) b
           ON b.artikelNr = a.artikelNr
    WHERE lower(COALESCE(a.bezeichnung,'')) LIKE ?
       OR lower(COALESCE(a.marke,'')) LIKE ?
       OR COALESCE(a.gtin,'') LIKE ?
       OR (? = 1 AND CAST(a.artikelNr AS TEXT) LIKE ?)
    ORDER BY (CAST(a.artikelNr AS TEXT) = ?) DESC, brutto DESC
    LIMIT 100
    """
    rows = [dict(r) for r in con.execute(
        sql, [von, bis, like, like, like, 1 if ist_nr else 0,
              begriff + "%" if ist_nr else "", begriff]).fetchall()]
    return {"treffer": rows, "anzahl": len(rows), "von": von, "bis": bis, "begriff": begriff}


def q_artikel(con, q):
    """Alles zu einem Artikel: Kennzahlen, Monatsverlauf, Filialen, Bestand."""
    nr = int(q.get("artikelNr", 0))
    von, bis = q["von"], q["bis"]
    d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
    span = (d1 - d0).days + 1
    p0, p1 = d0 - timedelta(days=span), d0 - timedelta(days=1)
    y0, y1 = d0.replace(year=d0.year - 1), d1.replace(year=d1.year - 1)

    stamm = con.execute("SELECT * FROM artikel WHERE artikelNr=?", (nr,)).fetchone()
    if not stamm:
        return {"gefunden": False, "artikelNr": nr}
    stamm = dict(stamm)

    def kennz(a, b):
        r = con.execute("""SELECT %s FROM umsatz u LEFT JOIN artikel a ON a.artikelNr=u.artikelNr
                           WHERE u.artikelNr=? AND u.datum BETWEEN ? AND ?"""
                        % METRICS, (nr, a, b)).fetchone()
        return marge({k: (r[k] or 0) for k in r.keys()})

    monate = [dict(r) for r in con.execute(
        """SELECT substr(datum,1,7) monat, SUM(bruttoMitRabatt) brutto, SUM(stueck) stueck
           FROM umsatz WHERE artikelNr=? AND datum BETWEEN ? AND ?
           GROUP BY monat ORDER BY monat""", (nr, von, bis))]
    monateVJ = {r["monat"]: r["brutto"] for r in con.execute(
        """SELECT substr(datum,1,7) monat, SUM(bruttoMitRabatt) brutto
           FROM umsatz WHERE artikelNr=? AND datum BETWEEN ? AND ?
           GROUP BY monat""", (nr, y0.isoformat(), y1.isoformat()))}

    filialen = [dict(r) for r in con.execute(
        """SELECT u.filialeNr,
                  COALESCE(f.kurzbezeichnung,f.bezeichnung,'Filiale '||u.filialeNr) filiale,
                  SUM(u.bruttoMitRabatt) brutto, SUM(u.stueck) stueck
           FROM umsatz u LEFT JOIN filiale f ON f.filialeNr=u.filialeNr
           WHERE u.artikelNr=? AND u.datum BETWEEN ? AND ?
           GROUP BY u.filialeNr ORDER BY brutto DESC""", (nr, von, bis))]

    bestand = [dict(r) for r in con.execute(
        """SELECT b.filialeNr,
                  COALESCE(f.kurzbezeichnung,f.bezeichnung,'Filiale '||b.filialeNr) filiale,
                  b.bestand, b.meldemenge, b.reserviert, b.avisiert
           FROM bestand b LEFT JOIN filiale f ON f.filialeNr=b.filialeNr
           WHERE b.artikelNr=? ORDER BY b.bestand DESC""", (nr,))]

    erst = con.execute("SELECT MIN(datum) d, MAX(datum) l FROM umsatz WHERE artikelNr=?",
                       (nr,)).fetchone()
    tage_mit = con.execute(
        "SELECT COUNT(DISTINCT datum) t FROM umsatz WHERE artikelNr=? AND datum BETWEEN ? AND ?",
        (nr, von, bis)).fetchone()["t"]

    a = kennz(von, bis)
    ges_bestand = sum(x["bestand"] or 0 for x in bestand)
    pro_tag = (a["stueck"] / span) if span else 0
    return {
        "gefunden": True, "stamm": stamm,
        "aktuell": a, "vorperiode": kennz(p0.isoformat(), p1.isoformat()),
        "vorjahr": kennz(y0.isoformat(), y1.isoformat()),
        "monate": [dict(m, bruttoVJ=monateVJ.get(
            "%d-%s" % (int(m["monat"][:4]) - 1, m["monat"][5:]))) for m in monate],
        "filialen": filialen, "bestand": bestand,
        "bestandGesamt": ges_bestand,
        "reichweite": (ges_bestand / pro_tag) if pro_tag > 0 else None,
        "ersterVerkauf": erst["d"], "letzterVerkauf": erst["l"],
        "verkaufstage": tage_mit, "zeitraumTage": span,
        "perioden": {"aktuell": [von, bis], "vorperiode": [p0.isoformat(), p1.isoformat()],
                     "vorjahr": [y0.isoformat(), y1.isoformat()]},
    }


def q_jahresgespraech(con, q):
    """Vollstaendige Unterlage fuer ein Jahresgespraech - Zeitraum ist immer
    das laufende Jahr bis zum Stichtag, verglichen mit demselben Zeitraum
    des Vorjahres. Respektiert alle gesetzten Filter (Marke, Lieferant, ...)."""
    bis = q.get("bis") or (con.execute("SELECT MAX(datum) d FROM umsatz_tage").fetchone()["d"]
                           or date.today().isoformat())
    d1 = date.fromisoformat(bis)
    von = date(d1.year, 1, 1).isoformat()
    vj_von, vj_bis = date(d1.year - 1, 1, 1).isoformat(), d1.replace(year=d1.year - 1).isoformat()
    basis = dict(q, von=von, bis=bis)

    kpi = q_kpi(con, basis)
    # Anteil am Gesamtgeschaeft (ohne die Dimensionsfilter)
    ohne = {k: v for k, v in basis.items()
            if k not in ("marke", "submarke", "linie", "warengruppe", "lieferant", "kategorie")}
    gesamt = q_kpi(con, ohne)

    artikel = q_ranking(con, dict(basis, dim="artikel"))["rows"]
    marken = q_ranking(con, dict(basis, dim="marke"))["rows"]
    wg = q_ranking(con, dict(basis, dim="warengruppe"))["rows"]
    fil = q_filialbenchmark(con, basis)["rows"]
    ytd = q_ytd(con, basis)
    kap = q_kapital(con, dict(basis, dim="marke", tage=90))
    neu = q_neuheiten(con, dict(basis, tage=(d1 - date.fromisoformat(von)).days + 1))

    # Nur Artikel mit Vorjahresumsatz sind ueberhaupt vergleichbar.
    # Verlierer sind ausschliesslich Artikel mit echtem Minus - sonst stehen
    # unter "Rueckgang" Zuwaechse, nur eben kleinere.
    mit_vj = [r for r in artikel if (r.get("bruttoVJ") or 0) > 0]
    gewinner = [r for r in sorted(mit_vj, key=lambda r: -(r.get("deltaVJ") or 0))
                if (r.get("deltaVJ") or 0) > 0][:15]
    verlierer = [r for r in sorted(mit_vj, key=lambda r: (r.get("deltaVJ") or 0))
                 if (r.get("deltaVJ") or 0) < 0][:15]
    # Artikel mit Bestand, aber ohne einen einzigen Verkauf im Jahr
    verkauft = {r["dim"].split(" – ")[0] for r in artikel}
    dimcond, dimargs = [], []
    for f, c in (("marke", "a.marke"), ("submarke", "a.submarke"),
                 ("warengruppe", "a.warengruppe"), ("lieferant", "a.lieferant")):
        if q.get(f):
            dimcond.append("%s = ?" % c)
            dimargs.append(q[f])
    where = (" AND " + " AND ".join(dimcond)) if dimcond else ""
    steher = [dict(r) for r in con.execute("""
        SELECT b.artikelNr, COALESCE(a.bezeichnung,'') bezeichnung, COALESCE(a.marke,'') marke,
               SUM(b.bestand) bestand, SUM(b.bestand*COALESCE(a.ekPreis,0)) wert
        FROM bestand b LEFT JOIN artikel a ON a.artikelNr=b.artikelNr
        WHERE b.bestand > 0 {w}
          AND b.artikelNr NOT IN (SELECT DISTINCT artikelNr FROM umsatz
                                  WHERE datum BETWEEN ? AND ?)
        GROUP BY b.artikelNr ORDER BY wert DESC LIMIT 25
    """.format(w=where), dimargs + [von, bis])]

    a, v = kpi["aktuell"], kpi["vorjahr"]
    return {
        "stichtag": bis, "von": von, "bis": bis,
        "vorjahr": [vj_von, vj_bis], "jahr": d1.year,
        # "alle Kanäle" ist kein Filter und gehoert nicht in die Ueberschrift
        "filter": {k: q.get(k) for k in
                   ("marke", "submarke", "warengruppe", "lieferant", "filiale", "kanal")
                   if q.get(k) and q.get(k) != "all"},
        "kpi": kpi,
        "anteilUmsatz": (a["brutto"] / gesamt["aktuell"]["brutto"] * 100)
                        if gesamt["aktuell"]["brutto"] else None,
        "anteilRohertrag": (a["rohertrag"] / gesamt["aktuell"]["rohertrag"] * 100)
                           if gesamt["aktuell"]["rohertrag"] else None,
        # Wachstum ausschliesslich aus Monaten, fuer die auch Vorjahresdaten
        # im Cache liegen - sonst entstehen absurde Raten wie +175 %.
        "wachstum": ytd["pct"],
        "umsatzVergleichbar": ytd["summeVergleichbar"], "umsatzVJ": ytd["summeVJ"],
        "vollstaendigVergleichbar": ytd["monateVergleichbar"] == ytd["monateGesamt"],
        "wachstumRoh": ((a["rohertrag"] / v["rohertrag"] - 1) * 100)
                       if (v["rohertrag"] and ytd["monateVergleichbar"] == ytd["monateGesamt"]) else None,
        "topArtikel": artikel[:25], "gewinner": gewinner, "verlierer": verlierer,
        # Gesamtzahlen, nicht die auf 15 gekuerzten Listen
        "zuwachsAnzahl": sum(1 for r in mit_vj if (r.get("deltaVJ") or 0) > 0),
        "rueckgangAnzahl": sum(1 for r in mit_vj if (r.get("deltaVJ") or 0) < 0),
        "vergleichbareArtikel": len(mit_vj),
        "marken": marken[:15], "warengruppen": wg[:15], "filialen": fil,
        "monate": ytd["monate"], "monateVergleichbar": ytd["monateVergleichbar"],
        "monateGesamt": ytd["monateGesamt"], "cacheVon": ytd["cacheVon"],
        "kapital": kap["rows"][:10], "kapitalGesamt": kap["gesamtwert"],
        "totesKapital": kap["totesKapitalGesamt"],
        "neuheiten": neu["rows"][:15], "neuheitenAnzahl": neu["anzahl"],
        "steher": steher,
        "artikelAnzahl": len(artikel),
    }


# =============================================================================
# PDF-Erzeugung — reine Standardbibliothek, kein reportlab noetig.
# Erzeugt ein gesetztes Dokument (Deckblatt, Kopf-/Fusszeile, Tabellen,
# Balkengrafik), kein Abzug der Webseite.
# =============================================================================
_HELV = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584")
_HELV_B = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 "
    "975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 "
    "333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 "
    "611 611 389 556 333 611 556 778 556 556 500 389 280 389 584")


def _breiten(tabelle):
    w = {}
    for i, v in enumerate(tabelle.split()):
        w[32 + i] = int(v)
    return w


BREITE_N = _breiten(_HELV)
BREITE_F = _breiten(_HELV_B)
# Umlaute und Sonderzeichen (WinAnsiEncoding)
for _c, _n, _f in ((196, 667, 722), (214, 778, 778), (220, 722, 722),
                   (228, 556, 556), (246, 556, 611), (252, 556, 611),
                   (223, 556, 611), (128, 556, 556), (183, 278, 278),
                   (150, 556, 556), (151, 1000, 1000)):
    BREITE_N[_c] = _n
    BREITE_F[_c] = _f


def _pdf_bytes(s):
    """Text nach WinAnsi. Das Euro-Zeichen liegt dort auf Position 128."""
    return (str(s).replace("€", "\x80").replace("–", "\x96").replace("—", "\x97")
            .replace("·", "\xb7").replace("„", '"').replace("“", '"')
            .encode("latin-1", "replace"))


class PDF:
    """Minimaler, aber sauber gesetzter PDF-Schreiber."""

    A4 = (595.28, 841.89)

    def __init__(self, breite=None, hoehe=None):
        self.b, self.h = breite or self.A4[0], hoehe or self.A4[1]
        self.seiten = []
        self.strom = []

    # ---- Seiten
    def neue_seite(self):
        if self.strom:
            self.seiten.append("".join(self.strom))
        self.strom = []

    def _fertig(self):
        if self.strom:
            self.seiten.append("".join(self.strom))
            self.strom = []

    # ---- Zeichnen (y wird von oben gemessen)
    def _y(self, y):
        return self.h - y

    def text(self, x, y, s, size=9.5, fett=False, farbe=(0.06, 0.09, 0.16),
             mono=False, rechts=None):
        if s is None or s == "":
            return
        f = ("F4" if fett else "F3") if mono else ("F2" if fett else "F1")
        if rechts is not None:
            x = rechts - self.breite_von(s, size, fett, mono)
        roh = _pdf_bytes(s).decode("latin-1")
        roh = roh.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        self.strom.append(
            "BT /%s %.2f Tf %.3f %.3f %.3f rg %.2f %.2f Td (%s) Tj ET\n"
            % (f, size, farbe[0], farbe[1], farbe[2], x, self._y(y), roh))

    def breite_von(self, s, size=9.5, fett=False, mono=False):
        if mono:
            return len(str(s)) * 0.6 * size
        t = BREITE_F if fett else BREITE_N
        return sum(t.get(ord(c) if ord(c) < 256 else 32, 556)
                   for c in _pdf_bytes(s).decode("latin-1")) / 1000.0 * size

    def kuerzen(self, s, max_b, size=9.5, fett=False):
        s = str(s or "")
        if self.breite_von(s, size, fett) <= max_b:
            return s
        while s and self.breite_von(s + "…", size, fett) > max_b:
            s = s[:-1]
        return s + "…"

    def rechteck(self, x, y, w, hh, fuell=None, rand=None, lw=0.6):
        if fuell:
            self.strom.append("%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f\n"
                              % (fuell[0], fuell[1], fuell[2], x, self._y(y + hh), w, hh))
        if rand:
            self.strom.append("%.3f %.3f %.3f RG %.2f w %.2f %.2f %.2f %.2f re S\n"
                              % (rand[0], rand[1], rand[2], lw, x, self._y(y + hh), w, hh))

    def linie(self, x1, y1, x2, y2, farbe=(0.85, 0.87, 0.90), lw=0.6):
        self.strom.append("%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S\n"
                          % (farbe[0], farbe[1], farbe[2], lw, x1, self._y(y1), x2, self._y(y2)))

    # ---- Ausgabe
    def ausgeben(self):
        self._fertig()
        objs, n = [], len(self.seiten)
        fonts = ("Helvetica", "Helvetica-Bold", "Courier", "Courier-Bold")
        # 1 Katalog, 2 Pages, dann je Seite Page+Contents, dann Fonts
        kids = " ".join("%d 0 R" % (3 + 2 * i) for i in range(n))
        objs.append("<< /Type /Catalog /Pages 2 0 R >>")
        objs.append("<< /Type /Pages /Count %d /Kids [%s] >>" % (n, kids))
        f_start = 3 + 2 * n
        res = ("<< /Font << " + " ".join(
            "/F%d %d 0 R" % (i + 1, f_start + i) for i in range(4)) + " >> >>")
        for i, inhalt in enumerate(self.seiten):
            objs.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
                        "/Resources %s /Contents %d 0 R >>" % (self.b, self.h, res, 4 + 2 * i))
            objs.append(("STREAM", inhalt))
        for f in fonts:
            objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /%s "
                        "/Encoding /WinAnsiEncoding >>" % f)

        aus = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        pos = []
        for i, o in enumerate(objs, start=1):
            pos.append(len(aus))
            if isinstance(o, tuple):
                daten = _pdf_bytes(o[1])
                aus += b"%d 0 obj\n<< /Length %d >>\nstream\n" % (i, len(daten))
                aus += daten + b"\nendstream\nendobj\n"
            else:
                aus += b"%d 0 obj\n" % i + _pdf_bytes(o) + b"\nendobj\n"
        xref = len(aus)
        aus += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for p in pos:
            aus += b"%010d 00000 n \n" % p
        aus += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objs) + 1, xref))
        return bytes(aus)


# ---- Farben und Masse des Berichts
C_TEXT = (0.06, 0.09, 0.16)
C_MUT = (0.35, 0.40, 0.47)
C_LINIE = (0.85, 0.87, 0.90)
C_KOPF = (0.95, 0.96, 0.97)
C_ZEBRA = (0.980, 0.985, 0.99)
C_AKZENT = (0.557, 0.0, 0.0)   # #8E0000
C_GRUEN = (0.06, 0.48, 0.24)
C_ROT = (0.75, 0.22, 0.17)


class Bericht:
    """Setzt den Jahresgespraechs-Bericht mit Kopf-, Fusszeile und Umbruch."""

    RAND = 42
    OBEN = 68
    UNTEN = 56

    def __init__(self, titel, untertitel, fuss):
        self.p = PDF()
        self.titel, self.untertitel, self.fuss = titel, untertitel, fuss
        self.nr = 0
        self.y = 0
        self.deckblatt_offen = False

    @property
    def breite(self):
        return self.p.b - 2 * self.RAND

    def seite(self, mit_kopf=True):
        if self.nr:
            self._fusszeile()
        self.p.neue_seite()
        self.nr += 1
        self.y = self.OBEN
        if mit_kopf and self.nr > 1:
            self.p.text(self.RAND, 38, self.titel, 8.5, farbe=C_MUT)
            self.p.text(0, 38, self.untertitel, 8.5, farbe=C_MUT,
                        rechts=self.p.b - self.RAND)
            self.p.linie(self.RAND, 44, self.p.b - self.RAND, 44)

    def _fusszeile(self):
        y = self.p.h - 34
        self.p.linie(self.RAND, y - 10, self.p.b - self.RAND, y - 10)
        self.p.text(self.RAND, y, self.fuss, 7.5, farbe=C_MUT)
        self.p.text(0, y, "Seite %d" % self.nr, 7.5, farbe=C_MUT, mono=True,
                    rechts=self.p.b - self.RAND)

    def platz(self, hoehe):
        """Sorgt dafuer, dass hoehe Punkte auf der Seite frei sind."""
        if self.y + hoehe > self.p.h - self.UNTEN:
            self.seite()
            return True
        return False

    def abstand(self, h):
        self.y += h

    def ueberschrift(self, text, hinweis=None):
        self.platz(58)
        self.p.text(self.RAND, self.y, text, 12.5, fett=True)
        self.y += 5
        self.p.linie(self.RAND, self.y, self.p.b - self.RAND, self.y,
                     farbe=(0.75, 0.78, 0.83), lw=0.9)
        self.y += 14
        if hinweis:
            self.p.text(self.RAND, self.y, hinweis, 8.5, farbe=C_MUT)
            self.y += 12

    def tabelle(self, spalten, zeilen, zeilenhoehe=15.5):
        """spalten: Liste (Titel, Anteil, Ausrichtung r/l, mono)"""
        gesamt = sum(s[1] for s in spalten)
        breiten = [self.breite * s[1] / gesamt for s in spalten]

        def kopf():
            self.p.rechteck(self.RAND, self.y - 3, self.breite, 17, fuell=C_KOPF)
            x = self.RAND
            for (t, _, aus, _), w in zip(spalten, breiten):
                if aus == "r":
                    self.p.text(0, self.y + 9, t, 7.8, fett=True, farbe=C_MUT,
                                rechts=x + w - 6)
                else:
                    self.p.text(x + 6, self.y + 9, t, 7.8, fett=True, farbe=C_MUT)
                x += w
            self.y += 17
            self.p.linie(self.RAND, self.y, self.p.b - self.RAND, self.y,
                         farbe=(0.75, 0.78, 0.83))

        self.platz(17 + zeilenhoehe * 3)
        kopf()
        for i, z in enumerate(zeilen):
            if self.platz(zeilenhoehe + 4):
                kopf()
            if i % 2:
                self.p.rechteck(self.RAND, self.y, self.breite, zeilenhoehe, fuell=C_ZEBRA)
            x = self.RAND
            for (wert, farbe), (t, _, aus, mono), w in zip(z, spalten, breiten):
                yy = self.y + zeilenhoehe - 5
                if aus == "r":
                    self.p.text(0, yy, wert, 8.6, farbe=farbe or C_TEXT, mono=mono,
                                rechts=x + w - 6)
                else:
                    self.p.text(x + 6, yy, self.p.kuerzen(wert, w - 12, 8.6),
                                8.6, farbe=farbe or C_TEXT)
                x += w
            self.y += zeilenhoehe
            self.p.linie(self.RAND, self.y, self.p.b - self.RAND, self.y,
                         farbe=(0.92, 0.93, 0.95), lw=0.4)
        self.y += 16

    def kennzahlen(self, paare, spalten=4):
        """Kachelraster mit Kennzahlen."""
        zeilen = (len(paare) + spalten - 1) // spalten
        kh, luecke = 46, 8
        self.platz(zeilen * (kh + luecke))
        kb = (self.breite - luecke * (spalten - 1)) / spalten
        for i, (label, wert, farbe) in enumerate(paare):
            sp, ze = i % spalten, i // spalten
            x = self.RAND + sp * (kb + luecke)
            y = self.y + ze * (kh + luecke)
            self.p.rechteck(x, y, kb, kh, fuell=(1, 1, 1), rand=C_LINIE, lw=0.7)
            self.p.rechteck(x, y, 2.5, kh, fuell=C_AKZENT)
            self.p.text(x + 10, y + 15,
                        self.p.kuerzen(label.upper(), kb - 20, 6.8), 6.8, farbe=C_MUT)
            self.p.text(x + 10, y + 33, self.p.kuerzen(wert, kb - 20, 13.5, True),
                        13.5, fett=True, farbe=farbe or C_TEXT)
        self.y += zeilen * (kh + luecke) + 8

    def balken(self, labels, werte, werte_vj, hoehe=120):
        """Zwei Balkenreihen nebeneinander je Kategorie."""
        self.platz(hoehe + 34)
        x0, y0 = self.RAND, self.y
        b = self.breite
        maxv = max([v for v in werte + werte_vj if v] or [1])
        n = max(len(labels), 1)
        gruppe = b / n
        bb = min(gruppe * 0.36, 26)
        # Grundlinie und zwei Hilfslinien
        for f in (0.5, 1.0):
            yy = y0 + hoehe - hoehe * f
            self.p.linie(x0, yy, x0 + b, yy, farbe=(0.93, 0.94, 0.96), lw=0.5)
            self.p.text(0, yy + 3, _kurz(maxv * f), 6.5, farbe=C_MUT, mono=True, rechts=x0 - 4)
        self.p.linie(x0, y0 + hoehe, x0 + b, y0 + hoehe, farbe=(0.75, 0.78, 0.83))
        for i, lab in enumerate(labels):
            xc = x0 + gruppe * i + gruppe / 2
            v, vv = werte[i] or 0, (werte_vj[i] if i < len(werte_vj) else 0) or 0
            hv = hoehe * (v / maxv) if maxv else 0
            hvv = hoehe * (vv / maxv) if maxv else 0
            self.p.rechteck(xc - bb - 1, y0 + hoehe - hvv, bb, hvv, fuell=(0.80, 0.83, 0.87))
            self.p.rechteck(xc + 1, y0 + hoehe - hv, bb, hv, fuell=C_AKZENT)
            self.p.text(0, y0 + hoehe + 12, lab, 7,
                        farbe=C_MUT, rechts=xc + self.p.breite_von(lab, 7) / 2)
        # Legende
        ly = y0 + hoehe + 26
        self.p.rechteck(x0, ly - 6, 9, 7, fuell=C_AKZENT)
        self.p.text(x0 + 14, ly, "Laufendes Jahr", 7.5, farbe=C_MUT)
        self.p.rechteck(x0 + 100, ly - 6, 9, 7, fuell=(0.80, 0.83, 0.87))
        self.p.text(x0 + 114, ly, "Vorjahr", 7.5, farbe=C_MUT)
        self.y += hoehe + 40

    def hinweiskasten(self, text, art="warn"):
        farbe = {"warn": (0.99, 0.96, 0.89), "info": (0.93, 0.96, 1.0)}[art]
        rand = {"warn": (0.85, 0.65, 0.20), "info": (0.55, 0.70, 0.95)}[art]
        zeilen = _umbrechen(self.p, text, self.breite - 24, 8.6)
        hh = 12 + len(zeilen) * 12
        self.platz(hh + 10)
        self.p.rechteck(self.RAND, self.y, self.breite, hh, fuell=farbe, rand=rand, lw=0.8)
        self.p.rechteck(self.RAND, self.y, 3, hh, fuell=rand)
        for i, z in enumerate(zeilen):
            self.p.text(self.RAND + 14, self.y + 16 + i * 12, z, 8.6, farbe=(0.30, 0.22, 0.05))
        self.y += hh + 14

    def ausgeben(self):
        self._fusszeile()
        return self.p.ausgeben()


def _umbrechen(pdf, text, breite, size):
    worte, zeilen, akt = str(text).split(), [], ""
    for w in worte:
        pr = (akt + " " + w).strip()
        if pdf.breite_von(pr, size) <= breite:
            akt = pr
        else:
            if akt:
                zeilen.append(akt)
            akt = w
    if akt:
        zeilen.append(akt)
    return zeilen


def _kurz(v):
    a = abs(v or 0)
    if a >= 1e6:
        return ("%.1f Mio" % (v / 1e6)).replace(".", ",")
    if a >= 1e3:
        return "%d Tsd" % round(v / 1e3)
    return "%d" % (v or 0)


def _eur0(v):
    return ("{:,.0f}".format(v or 0).replace(",", ".")) + " €"


def _pz(v, mit_vorzeichen=True):
    if v is None:
        return "—"
    s = ("%+.1f" if mit_vorzeichen else "%.1f") % v
    return s.replace(".", ",") + " %"


def _farbe(v):
    if v is None:
        return C_MUT
    return C_GRUEN if v > 0.5 else (C_ROT if v < -0.5 else C_MUT)


def jahres_pdf(con, q):
    """Baut die vollstaendige Gespraechsunterlage als PDF."""
    d = q_jahresgespraech(con, q)
    k, vj = d["kpi"]["aktuell"], d["kpi"]["vorjahr"]
    partner = " · ".join(str(v) for v in d["filter"].values()) or "Gesamtsortiment"
    heute = datetime.now().strftime("%d.%m.%Y")

    b = Bericht("Jahresgespräch %d — %s" % (d["jahr"], partner),
                "%s bis %s" % (d["von"], d["bis"]),
                "Meister Parfumerie · erstellt am %s · Quelle NEO-WWS" % heute)

    # ---------- Deckblatt ----------
    b.seite(mit_kopf=False)
    p = b.p
    p.rechteck(0, 0, p.b, 6, fuell=C_AKZENT)
    b.y = 120
    p.text(b.RAND, b.y, "MEISTER PARFÜMERIE", 9, fett=True, farbe=C_AKZENT)
    b.y += 34
    p.text(b.RAND, b.y, "Jahresgespräch %d" % d["jahr"], 30, fett=True)
    b.y += 30
    p.text(b.RAND, b.y, partner, 19, farbe=C_MUT)
    b.y += 26
    p.linie(b.RAND, b.y, b.RAND + 90, b.y, farbe=C_AKZENT, lw=2)
    b.y += 26
    p.text(b.RAND, b.y, "Berichtszeitraum      %s bis %s" % (d["von"], d["bis"]), 10)
    b.y += 15
    p.text(b.RAND, b.y, "Vorjahresvergleich    %s bis %s" % (d["vorjahr"][0], d["vorjahr"][1]),
           10, farbe=C_MUT)
    b.y += 15
    p.text(b.RAND, b.y, "Erstellt am           %s" % heute, 10, farbe=C_MUT)
    b.y += 44

    if not d["vollstaendigVergleichbar"]:
        b.hinweiskasten(
            "Eingeschränkte Vergleichbarkeit: Nur %d von %d Monaten haben Vorjahresdaten "
            "im Datenbestand (dieser reicht bis %s zurück). Alle Vorjahresvergleiche in "
            "dieser Unterlage beziehen sich ausschließlich auf diese Monate."
            % (d["monateVergleichbar"], d["monateGesamt"], d["cacheVon"]))

    # Bei unvollstaendiger Vorjahresabdeckung darf der volle Jahresumsatz nicht
    # neben einem gekuerzten Vorjahreswert stehen - sonst liest man einen
    # Einbruch, den es gar nicht gibt.
    kacheln = [("Umsatz brutto", _eur0(k["brutto"]), None)]
    if not d["vollstaendigVergleichbar"]:
        kacheln.append(("Vergleichbar %d/%d Mon."
                        % (d["monateVergleichbar"], d["monateGesamt"]),
                        _eur0(d["umsatzVergleichbar"]), None))
    kacheln += [
        ("Vorjahreszeitraum", _eur0(d["umsatzVJ"]), None),
        ("Veränderung", _pz(d["wachstum"]), _farbe(d["wachstum"])),
        ("Verkaufte Stück", "{:,.0f}".format(k["stueck"]).replace(",", "."), None),
    ]
    b.kennzahlen(kacheln, spalten=4)

    # Kurzfazit in Worten
    satz = []
    if d["wachstum"] is not None:
        satz.append("Der Umsatz liegt %s gegenüber dem Vorjahreszeitraum."
                    % (("%.1f %% höher" % d["wachstum"]).replace(".", ",")
                       if d["wachstum"] >= 0 else
                       ("%.1f %% niedriger" % abs(d["wachstum"])).replace(".", ",")))
    satz.append("%d Artikel mit Umsatz; von den %d im Vorjahr ebenfalls verkauften liegen "
                "%d über und %d unter dem Vorjahreswert."
                % (d["artikelAnzahl"], d["vergleichbareArtikel"],
                   d["zuwachsAnzahl"], d["rueckgangAnzahl"]))
    b.abstand(6)
    for z in _umbrechen(p, " ".join(satz), b.breite, 10):
        p.text(b.RAND, b.y, z, 10)
        b.y += 15

    # ---------- Monatsverlauf ----------
    b.seite()
    MN = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul",
          "Aug", "Sep", "Okt", "Nov", "Dez"]
    b.ueberschrift("Monatsverlauf",
                   "Umsatz je Monat im Vergleich zum Vorjahreszeitraum")
    mon = d["monate"]
    b.balken([MN[int(m["monat"]) - 1] for m in mon],
             [m["brutto"] for m in mon],
             [(m["bruttoVJ"] or 0) for m in mon])
    zeilen = []
    for m in mon:
        if not m["vergleichbar"]:
            zeilen.append([(MN[int(m["monat"]) - 1], None), (_eur0(m["brutto"]), None),
                           ("keine Vorjahresdaten", C_MUT), ("—", C_MUT), ("—", C_MUT)])
            continue
        pct = ((m["brutto"] / m["bruttoVJ"] - 1) * 100) if m["bruttoVJ"] else None
        zeilen.append([(MN[int(m["monat"]) - 1], None), (_eur0(m["brutto"]), None),
                       (_eur0(m["bruttoVJ"]), None),
                       (("+" if m["brutto"] >= m["bruttoVJ"] else "") +
                        _eur0(m["brutto"] - m["bruttoVJ"]), _farbe(pct)),
                       (_pz(pct), _farbe(pct))])
    b.tabelle([("Monat", 1.3, "l", False), ("Umsatz", 1.4, "r", True),
               ("Vorjahr", 1.4, "r", True), ("Differenz", 1.4, "r", True),
               ("Veränderung", 1.1, "r", True)], zeilen)

    # ---------- Artikel ----------
    b.ueberschrift("Meistverkaufte Artikel",
                   "Die 20 umsatzstärksten Artikel im Berichtszeitraum")
    b.tabelle([("Artikel", 3.6, "l", False), ("Umsatz", 1.4, "r", True),
               ("Anteil", 0.9, "r", True), ("Stück", 1.0, "r", True),
               ("Vorjahr", 1.2, "r", True), ("Veränderung", 1.1, "r", True)],
              [[(r["dim"], None), (_eur0(r["brutto"]), None),
                (_pz(r["anteil"], False), None),
                ("{:,.0f}".format(r["stueck"]).replace(",", "."), None),
                (_eur0(r["bruttoVJ"]), None),
                (_pz(r["pctVJ"]), _farbe(r["pctVJ"]))] for r in d["topArtikel"][:20]])

    if d["gewinner"]:
        b.ueberschrift("Größte Zuwächse gegenüber Vorjahr")
        b.tabelle([("Artikel", 3.4, "l", False), ("Umsatz", 1.3, "r", True),
                   ("Vorjahr", 1.3, "r", True), ("Differenz", 1.3, "r", True),
                   ("Veränderung", 1.1, "r", True)],
                  [[(r["dim"], None), (_eur0(r["brutto"]), None), (_eur0(r["bruttoVJ"]), None),
                    ("+" + _eur0(r["deltaVJ"]), C_GRUEN), (_pz(r["pctVJ"]), C_GRUEN)]
                   for r in d["gewinner"][:12]])

    b.ueberschrift("Größte Rückgänge gegenüber Vorjahr")
    if d["verlierer"]:
        b.tabelle([("Artikel", 3.4, "l", False), ("Umsatz", 1.3, "r", True),
                   ("Vorjahr", 1.3, "r", True), ("Differenz", 1.3, "r", True),
                   ("Veränderung", 1.1, "r", True)],
                  [[(r["dim"], None), (_eur0(r["brutto"]), None), (_eur0(r["bruttoVJ"]), None),
                    (_eur0(r["deltaVJ"]), C_ROT), (_pz(r["pctVJ"]), C_ROT)]
                   for r in d["verlierer"][:12]])
    else:
        b.p.text(b.RAND, b.y, "Kein Artikel mit Vorjahresumsatz liegt unter dem Vorjahreswert.",
                 9, farbe=C_MUT)
        b.abstand(24)

    # ---------- Struktur ----------
    if d["marken"]:
        b.ueberschrift("Marken")
        b.tabelle([("Marke", 2.8, "l", False), ("Umsatz", 1.4, "r", True),
                   ("Anteil", 1.0, "r", True), ("Stück", 1.1, "r", True),
                   ("Vorjahr", 1.3, "r", True), ("Veränderung", 1.1, "r", True)],
                  [[(r["dim"], None), (_eur0(r["brutto"]), None), (_pz(r["anteil"], False), None),
                    ("{:,.0f}".format(r["stueck"]).replace(",", "."), None),
                    (_eur0(r["bruttoVJ"]), None),
                    (_pz(r["pctVJ"]), _farbe(r["pctVJ"]))] for r in d["marken"][:12]])
    if d["warengruppen"]:
        b.ueberschrift("Warengruppen")
        b.tabelle([("Warengruppe", 2.8, "l", False), ("Umsatz", 1.4, "r", True),
                   ("Anteil", 1.0, "r", True), ("Stück", 1.1, "r", True),
                   ("Vorjahr", 1.3, "r", True), ("Veränderung", 1.1, "r", True)],
                  [[(r["dim"], None), (_eur0(r["brutto"]), None), (_pz(r["anteil"], False), None),
                    ("{:,.0f}".format(r["stueck"]).replace(",", "."), None),
                    (_eur0(r["bruttoVJ"]), None), (_pz(r["pctVJ"]), _farbe(r["pctVJ"]))]
                   for r in d["warengruppen"][:12]])
    if d["filialen"]:
        b.ueberschrift("Filialen", "Umsatz je Kasse und Tag macht Standorte vergleichbar")
        b.tabelle([("Filiale", 2.4, "l", False), ("Umsatz", 1.4, "r", True),
                   ("Kassen", 0.8, "r", True), ("Umsatz/Kasse/Tag", 1.4, "r", True),
                   ("Ø Bon", 1.1, "r", True), ("Stück", 1.1, "r", True)],
                  [[(r["dim"], None), (_eur0(r["brutto"]), None), (str(r["kassen"] or "—"), None),
                    (_eur0(r["umsatzProKasseTag"]) if r["umsatzProKasseTag"] else "—", None),
                    (_eur0(r["bonwert"]) if r["bonwert"] else "—", None),
                    ("{:,.0f}".format(r["stueck"]).replace(",", "."), None)]
                   for r in d["filialen"]])

    # ---------- Sortiment und Kapital ----------
    if d["neuheiten"]:
        b.ueberschrift("Neu gelistete Artikel",
                       "%d Neulistungen im Berichtszeitraum, hier die umsatzstärksten"
                       % d["neuheitenAnzahl"])
        b.tabelle([("Artikel", 3.4, "l", False), ("Erster Verkauf", 1.2, "l", True),
                   ("Umsatz", 1.3, "r", True), ("Stück", 0.9, "r", True),
                   ("Filialen", 0.8, "r", True)],
                  [[("%d – %s" % (r["artikelNr"], r["bezeichnung"]), None),
                    (r["ersterVerkauf"], None), (_eur0(r["brutto"]), None),
                    ("{:,.0f}".format(r["stueck"]).replace(",", "."), None),
                    (str(r["filialen"]), None)] for r in d["neuheiten"][:12]])
    if d["steher"]:
        b.ueberschrift("Artikel mit Bestand, aber ohne Verkauf",
                       "Kandidaten für Auslistung, Rückgabe oder Abverkaufsaktion")
        b.tabelle([("Artikel", 4.0, "l", False), ("Marke", 2.0, "l", False),
                   ("Bestand", 1.4, "r", True)],
                  [[("%d – %s" % (r["artikelNr"], r["bezeichnung"]), None), (r["marke"], None),
                    ("{:,.0f}".format(r["bestand"]).replace(",", "."), None)]
                   for r in d["steher"][:15]])
    if False and d["kapital"]:
        b.ueberschrift("Gebundenes Kapital",
                       "Bestandswert gesamt %s, davon %s in Ladenhütern und Überbeständen"
                       % (_eur0(d["kapitalGesamt"]), _eur0(d["totesKapital"])))
        b.tabelle([("Marke", 2.4, "l", False), ("Bestandswert", 1.4, "r", True),
                   ("Reichweite", 1.1, "r", True), ("Umschlag/Jahr", 1.1, "r", True),
                   ("Totes Kapital", 1.4, "r", True)],
                  [[(r["dim"], None), (_eur0(r["wert"]), None),
                    ("%d T" % r["reichweite"] if r["reichweite"] else "—", None),
                    (("%.1f" % r["umschlagJahr"]).replace(".", ",") if r["umschlagJahr"] else "—", None),
                    (_eur0(r["totesKapital"]),
                     C_ROT if (r["totesKapitalAnteil"] or 0) > 25 else None)]
                   for r in d["kapital"][:10]])

    return b.ausgeben(), partner


def q_alerts(con, q):
    """Regelbasierte Auffaelligkeiten aus dem Cache. Braucht keine API-Rechte,
    rechnet ausschliesslich auf bereits geladenen Daten."""
    bis = q.get("bis") or (con.execute("SELECT MAX(datum) d FROM umsatz_tage").fetchone()["d"]
                           or date.today().isoformat())
    schwelle = float(q.get("schwelle", 15))       # Prozent Umsatzeinbruch
    min_umsatz = float(q.get("minUmsatz", 500))   # Rauschfilter
    reichweite_min = float(q.get("reichweite", 7))
    cond, args = where_clause(q)
    extra = (" AND " + " AND ".join(cond)) if cond else ""
    out = []

    d1 = date.fromisoformat(bis)
    c0, p0, p1 = d1 - timedelta(days=6), d1 - timedelta(days=13), d1 - timedelta(days=7)
    y0, y1 = c0.replace(year=c0.year - 1), d1.replace(year=d1.year - 1)

    # 1) Marken mit Umsatzeinbruch (7 Tage gegen Vorwoche)
    sql = """
    SELECT COALESCE(a.marke,'(ohne Marke)') AS dim,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS cur,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS prev,
      SUM(CASE WHEN u.datum BETWEEN ? AND ? THEN u.bruttoMitRabatt ELSE 0 END) AS vj
    {frm} WHERE (u.datum BETWEEN ? AND ? OR u.datum BETWEEN ? AND ? OR u.datum BETWEEN ? AND ?){extra}
    GROUP BY dim
    """.format(frm=BASE_FROM, extra=extra)
    p = [c0.isoformat(), bis, p0.isoformat(), p1.isoformat(), y0.isoformat(), y1.isoformat(),
         c0.isoformat(), bis, p0.isoformat(), p1.isoformat(), y0.isoformat(), y1.isoformat()] + args
    for r in con.execute(sql, p):
        if (r["prev"] or 0) < min_umsatz:
            continue
        ch = (r["cur"] / r["prev"] - 1) * 100
        if ch <= -schwelle:
            out.append({
                "typ": "Umsatzeinbruch", "schwere": "hoch" if ch <= -30 else "mittel",
                "objekt": r["dim"],
                "text": "Umsatz 7 Tage %.1f %% unter Vorwoche (%s statt %s)" % (
                    ch, _eur(r["cur"]), _eur(r["prev"])),
                "wert": ch, "vj": ((r["cur"] / r["vj"] - 1) * 100) if r["vj"] else None,
            })

    # 2) Bestandsprobleme mit Abverkaufsbezug
    v30 = (d1 - timedelta(days=29)).isoformat()
    vt30 = verkaufstage(v30, bis)
    sql2 = """
    WITH v AS (SELECT u.artikelNr, u.filialeNr, SUM(u.stueck) s
               {frm} WHERE u.datum BETWEEN ? AND ?{extra}
               GROUP BY u.artikelNr, u.filialeNr)
    SELECT b.artikelNr, b.filialeNr, COALESCE(a.marke,'') marke, COALESCE(a.bezeichnung,'') bez,
           COALESCE(f.kurzbezeichnung, f.bezeichnung, 'Filiale '||b.filialeNr) fil,
           b.bestand, b.meldemenge, COALESCE(v.s,0) abv
    FROM bestand b
    LEFT JOIN artikel a ON a.artikelNr=b.artikelNr
    LEFT JOIN filiale f ON f.filialeNr=b.filialeNr
    LEFT JOIN v ON v.artikelNr=b.artikelNr AND v.filialeNr=b.filialeNr
    WHERE COALESCE(v.s,0) > 0
    """.format(frm=BASE_FROM, extra=extra)
    for r in con.execute(sql2, [v30, bis] + args):
        pro_tag = r["abv"] / vt30
        rw = r["bestand"] / pro_tag if pro_tag > 0 else None
        name = "%d %s" % (r["artikelNr"], r["bez"][:40])
        if r["bestand"] <= 0:
            out.append({"typ": "Nullbestand trotz Abverkauf", "schwere": "hoch",
                        "objekt": name, "ort": r["fil"],
                        "text": "Bestand 0, aber %d Stück an %d Verkaufstagen verkauft" % (
                            r["abv"], vt30),
                        "wert": 0})
        elif rw is not None and rw < reichweite_min:
            out.append({"typ": "Reichweite kritisch",
                        "schwere": "hoch" if rw < 3 else "mittel",
                        "objekt": name, "ort": r["fil"],
                        "text": "nur %.1f Verkaufstage Reichweite (Bestand %d, %d Stück in %d Tagen)" % (
                            rw, r["bestand"], r["abv"], vt30),
                        "wert": rw})
        elif r["meldemenge"] and r["bestand"] < r["meldemenge"]:
            out.append({"typ": "Unter Meldemenge", "schwere": "niedrig",
                        "objekt": name, "ort": r["fil"],
                        "text": "Bestand %d unter Meldemenge %d" % (r["bestand"], r["meldemenge"]),
                        "wert": r["bestand"] - r["meldemenge"]})

    # 3) Gesamtumsatz des letzten Tages gegen denselben Wochentag der letzten 8 Wochen
    letzter = con.execute(
        "SELECT MAX(datum) d FROM umsatz_tage" +
        ("" if sonntag_aktiv(q) else " WHERE strftime('%w', datum) <> '0'")).fetchone()["d"]
    if letzter:
        ld = date.fromisoformat(letzter)
        vgl = [(ld - timedelta(days=7 * k)).isoformat() for k in range(1, 9)]
        cur = con.execute("SELECT SUM(bruttoMitRabatt) s FROM umsatz WHERE datum=?",
                          (letzter,)).fetchone()["s"] or 0
        ref = con.execute(
            "SELECT AVG(t) a FROM (SELECT datum, SUM(bruttoMitRabatt) t FROM umsatz "
            "WHERE datum IN (%s) GROUP BY datum)" % ",".join("?" * len(vgl)), vgl).fetchone()["a"]
        if ref and cur:
            ch = (cur / ref - 1) * 100
            if abs(ch) >= schwelle:
                out.append({
                    "typ": "Tagesumsatz auffällig",
                    "schwere": "mittel" if abs(ch) < 30 else "hoch",
                    "objekt": letzter,
                    "text": "%s am %s vs. Ø %s am gleichen Wochentag (%+.1f %%)" % (
                        _eur(cur), letzter, _eur(ref), ch),
                    "wert": ch})

    rang = {"hoch": 0, "mittel": 1, "niedrig": 2}
    out.sort(key=lambda x: (rang.get(x["schwere"], 9), -abs(x.get("wert") or 0)))
    return {"stand": bis, "anzahl": len(out), "alerts": out[:400],
            "regeln": {"umsatzeinbruchAb": schwelle, "minUmsatz": min_umsatz,
                       "reichweiteUnter": reichweite_min}}


def _eur(v):
    return ("%.2f" % (v or 0)).replace(".", ",") + " EUR"


# ------------------------------------------------------- Backup & Wochenreport
def backup_db(ziel_ordner=None):
    """Konsistente Kopie des Caches ueber die SQLite-Backup-API.

    Der Cache ist mit der Zeit das wertvollste Stueck: historische Tagesdaten,
    die man sonst Tag fuer Tag neu aus der API ziehen muesste."""
    ordner = Path(ziel_ordner or (Path(CFG["db"]).parent / "backups"))
    ordner.mkdir(parents=True, exist_ok=True)
    ziel = ordner / ("neo-cache-%s.db" % datetime.now().strftime("%Y%m%d-%H%M"))
    src = sqlite3.connect(CFG["db"])
    dst = sqlite3.connect(str(ziel))
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    # Nur die letzten 12 Sicherungen behalten
    alt = sorted(ordner.glob("neo-cache-*.db"))
    for f in alt[:-12]:
        try:
            f.unlink()
        except OSError:
            pass
    return {"datei": str(ziel), "groesseMB": round(ziel.stat().st_size / 1048576, 2),
            "vorhanden": len(alt[-12:])}


def _h(s):
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def wochenreport(con, ordner=None, bis=None):
    """Erzeugt einen eigenstaendigen HTML-Report (druck- und PDF-tauglich)
    plus CSV-Dateien mit den Rohwerten."""
    bis = bis or (con.execute("SELECT MAX(datum) d FROM umsatz_tage").fetchone()["d"]
                  or date.today().isoformat())
    von = (date.fromisoformat(bis) - timedelta(days=6)).isoformat()
    q = {"von": von, "bis": bis, "kanal": "all"}

    kpi = q_kpi(con, q)
    marken = q_ranking(con, dict(q, dim="marke"))["rows"]
    fil = q_filialbenchmark(con, q)["rows"]
    alerts = q_alerts(con, {"bis": bis})
    luecken = q_luecken(con, {"von": (date.fromisoformat(bis) - timedelta(days=START_TAGE)).isoformat(),
                              "bis": bis})
    kap = q_kapital(con, {"dim": "marke", "bis": bis, "tage": 90})

    ordner = Path(ordner or (Path(CFG["db"]).parent / "reports"))
    ordner.mkdir(parents=True, exist_ok=True)
    stamm = "neo-wochenreport-%s" % bis

    def csv_write(name, rows):
        if not rows:
            return
        p = ordner / ("%s-%s.csv" % (stamm, name))
        # Nicht alle Zeilen haben dieselben Schluessel (z. B. "ort" nur bei
        # bestandsbezogenen Alerts) - deshalb die Vereinigung bilden.
        keys = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            f.write(";".join(keys) + "\r\n")
            for r in rows:
                werte = []
                for k in keys:
                    v = r.get(k)
                    werte.append(str(v).replace(".", ",") if isinstance(v, (int, float))
                                 else '"%s"' % str(v if v is not None else "").replace('"', '""'))
                f.write(";".join(werte) + "\r\n")

    csv_write("marken", marken)
    csv_write("wochentage", q_wochentag(con, q)["rows"])
    csv_write("filialen", fil)
    csv_write("alerts", alerts["alerts"])
    csv_write("kapital", kap["rows"])

    a, vp, vj = kpi["aktuell"], kpi["vorperiode"], kpi["vorjahr"]

    def d(c, r):
        return ((c / r - 1) * 100) if r else None

    def pz(v):
        return "—" if v is None else ("%+.1f %%" % v).replace(".", ",")

    def ez(v):
        return ("{:,.0f}".format(v or 0).replace(",", ".")) + " €"

    def farbe(v):
        return "up" if (v or 0) > 0.5 else ("down" if (v or 0) < -0.5 else "")

    kpis = [("Bruttoumsatz", ez(a["brutto"]), d(a["brutto"], vp["brutto"]), d(a["brutto"], vj["brutto"])),
            ("Rohertrag", ez(a["rohertrag"]), d(a["rohertrag"], vp["rohertrag"]), d(a["rohertrag"], vj["rohertrag"])),
            ("Marge", ("%.1f %%" % (a["marge"] or 0)).replace(".", ","), None, None),
            ("Belege", "{:,.0f}".format(a["belege"]).replace(",", "."), d(a["belege"], vp["belege"]), d(a["belege"], vj["belege"])),
            ("Ø Bonwert", ez(a["brutto"] / a["belege"] if a["belege"] else 0), None, None),
            ("Verkaufte Stück", "{:,.0f}".format(a["stueck"]).replace(",", "."), d(a["stueck"], vp["stueck"]), d(a["stueck"], vj["stueck"]))]

    gew = sorted(marken, key=lambda r: -(r["deltaVP"] or 0))[:8]
    verl = sorted(marken, key=lambda r: (r["deltaVP"] or 0))[:8]
    hoch = [x for x in alerts["alerts"] if x["schwere"] == "hoch"][:25]

    html = ["""<!doctype html><meta charset="utf-8">
<title>Meister Parfumerie — Wochenreport %s</title>
<style>
 body{font:13px/1.6 Inter,-apple-system,Segoe UI,Roboto,sans-serif;color:#0F172A;max-width:1000px;margin:32px auto;padding:0 24px}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:14px;color:#0F172A;margin:34px 0 12px;border-bottom:1px solid #E7EAEF;padding-bottom:6px;font-weight:600}
 .sub{color:#57606a;margin-bottom:24px}
 .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
 .kpi{border:1px solid #d8dee4;border-radius:8px;padding:14px 16px}
 .kpi .v{font-size:21px;font-weight:650} .kpi .l{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#57606a}
 .kpi .d{font-size:12px;color:#57606a;margin-top:6px}
 table{width:100%%;border-collapse:collapse;font-size:12.5px} th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:#57606a;border-bottom:1px solid #d8dee4;padding:7px 9px}
 td{padding:7px 9px;border-bottom:1px solid #E7EAEF;font-variant-numeric:tabular-nums} .num{text-align:right;font-variant-numeric:tabular-nums}
 .up{color:#0F7A3D} .down{color:#C0392B}
 .warn{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:12px 14px;margin:14px 0}
 .two{display:grid;grid-template-columns:1fr 1fr;gap:26px}
 footer{margin-top:40px;color:#57606a;font-size:11.5px;border-top:1px solid #d8dee4;padding-top:12px}
 @media print{body{margin:0}h2{page-break-after:avoid}}
</style>
<h1>Meister Parfumerie — Wochenreport</h1>
<div class="sub">Berichtswoche %s bis %s &middot; Vorperiode %s bis %s &middot; Vorjahr %s bis %s</div>
""" % (bis, von, bis, kpi["perioden"]["vorperiode"][0], kpi["perioden"]["vorperiode"][1],
       kpi["perioden"]["vorjahr"][0], kpi["perioden"]["vorjahr"][1])]

    if luecken["fehlend"]:
        html.append('<div class="warn"><b>Datenlücken:</b> %d von %d Tagen fehlen im Cache '
                    '(Abdeckung %.1f %%). Die Vergleichswerte sind dadurch zu niedrig.</div>'
                    % (luecken["fehlend"], luecken["tageGesamt"], luecken["abdeckung"]))
    if (a.get("ekAbdeckung") or 0) < 98:
        html.append('<div class="warn"><b>EK-Abdeckung nur %.1f %%:</b> Rohertrag und Marge '
                    'beziehen sich nur auf diesen Teil des Umsatzes.</div>' % (a.get("ekAbdeckung") or 0))

    html.append('<div class="kpis">')
    for label, wert, dv, dj in kpis:
        html.append('<div class="kpi"><div class="v">%s</div><div class="l">%s</div>'
                    '<div class="d"><span class="%s">VP %s</span> &nbsp; <span class="%s">VJ %s</span></div></div>'
                    % (_h(wert), _h(label), farbe(dv), pz(dv), farbe(dj), pz(dj)))
    html.append("</div>")

    def tab(titel, kopf, zeilen):
        html.append("<h2>%s</h2><table><thead><tr>%s</tr></thead><tbody>" %
                    (_h(titel), "".join("<th class='%s'>%s</th>" % (c, _h(t)) for t, c in kopf)))
        for z in zeilen:
            html.append("<tr>" + "".join("<td class='%s'>%s</td>" % (c, v) for v, c in z) + "</tr>")
        html.append("</tbody></table>")

    tab("Marken – Top 15", [("Marke", ""), ("Umsatz", "num"), ("Rohertrag", "num"),
                            ("Marge", "num"), ("Δ% VP", "num"), ("Δ% VJ", "num")],
        [[(_h(r["dim"]), ""), (ez(r["brutto"]), "num"), (ez(r["rohertrag"]), "num"),
          (("%.1f %%" % (r["marge"] or 0)).replace(".", ","), "num"),
          ("<span class='%s'>%s</span>" % (farbe(r["pctVP"]), pz(r["pctVP"])), "num"),
          ("<span class='%s'>%s</span>" % (farbe(r["pctVJ"]), pz(r["pctVJ"])), "num")]
         for r in marken[:15]])

    html.append('<div class="two"><div>')
    tab("Gewinner", [("Marke", ""), ("Δ", "num"), ("Δ%", "num")],
        [[(_h(r["dim"]), ""), (ez(r["deltaVP"]), "num"),
          ("<span class='%s'>%s</span>" % (farbe(r["pctVP"]), pz(r["pctVP"])), "num")] for r in gew])
    html.append("</div><div>")
    tab("Verlierer", [("Marke", ""), ("Δ", "num"), ("Δ%", "num")],
        [[(_h(r["dim"]), ""), (ez(r["deltaVP"]), "num"),
          ("<span class='%s'>%s</span>" % (farbe(r["pctVP"]), pz(r["pctVP"])), "num")] for r in verl])
    html.append("</div></div>")

    tab("Filialen", [("Filiale", ""), ("Umsatz", "num"), ("Kassen", "num"),
                     ("Umsatz/Kasse/Tag", "num"), ("Ø Bon", "num"), ("vs. Schnitt", "num")],
        [[(_h(r["dim"]), ""), (ez(r["brutto"]), "num"), (str(r["kassen"] or "—"), "num"),
          (ez(r["umsatzProKasseTag"]) if r["umsatzProKasseTag"] else "—", "num"),
          (ez(r["bonwert"]) if r["bonwert"] else "—", "num"),
          ("<span class='%s'>%s</span>" % (farbe(r["vsSchnitt"]), pz(r["vsSchnitt"])), "num")]
         for r in fil])

    wt = q_wochentag(con, q)
    tab("Umsatz nach Wochentag (Index 100 = Tagesschnitt)",
        [("Wochentag", ""), ("Tage", "num"), ("Umsatz", "num"), ("Ø je Tag", "num"),
         ("Index", "num"), ("Anteil", "num"), ("Ø Bon", "num")],
        [[(_h(r["name"]), ""), (str(r["tage"]), "num"), (ez(r["brutto"]), "num"),
          (ez(r["proTag"]), "num"),
          ("%.0f" % (r["index"] or 0), "num"),
          (("%.1f %%" % r["anteil"]).replace(".", ","), "num"),
          (ez(r["bonwert"]) if r["bonwert"] else "—", "num")] for r in wt["rows"]])

    tab("Gebundenes Kapital – Top 10 Marken",
        [("Marke", ""), ("Bestandswert", "num"), ("Reichweite", "num"),
         ("Umschlag/Jahr", "num"), ("Totes Kapital", "num")],
        [[(_h(r["dim"]), ""), (ez(r["wert"]), "num"),
          (("%.0f T" % r["reichweite"]) if r["reichweite"] else "—", "num"),
          (("%.1f" % r["umschlagJahr"]).replace(".", ",") if r["umschlagJahr"] else "—", "num"),
          (ez(r["totesKapital"]), "num")] for r in kap["rows"][:10]])

    if hoch:
        tab("Kritische Alerts (%d gesamt, %d hoch)" % (alerts["anzahl"], len(
            [x for x in alerts["alerts"] if x["schwere"] == "hoch"])),
            [("Typ", ""), ("Objekt", ""), ("Filiale", ""), ("Befund", "")],
            [[(_h(x["typ"]), ""), (_h(x["objekt"]), ""), (_h(x.get("ort") or "—"), ""),
              (_h(x["text"]), "")] for x in hoch])

    html.append("<footer>Erzeugt am %s aus dem lokalen NEO-Cache. "
                "Rohdaten als CSV liegen im selben Ordner.</footer>"
                % datetime.now().strftime("%d.%m.%Y %H:%M"))

    pfad = ordner / ("%s.html" % stamm)
    pfad.write_text("\n".join(html), encoding="utf-8")
    return {"report": str(pfad), "ordner": str(ordner), "von": von, "bis": bis,
            "alerts": alerts["anzahl"], "luecken": luecken["fehlend"]}


def q_dimensions(con):
    def vals(col):
        return [r[0] for r in con.execute(
            "SELECT DISTINCT %s FROM artikel WHERE %s IS NOT NULL AND %s<>'' ORDER BY 1" % (col, col, col))]
    fil = [dict(r) for r in con.execute("SELECT * FROM filiale ORDER BY filialeNr")]
    tage = con.execute("SELECT MIN(datum) a, MAX(datum) b, COUNT(*) n FROM umsatz_tage").fetchone()
    so = con.execute("SELECT COUNT(DISTINCT datum) t, COALESCE(SUM(bruttoMitRabatt),0) s "
                     "FROM umsatz WHERE strftime('%w', datum) = '0'").fetchone()
    ges = con.execute("SELECT COALESCE(SUM(bruttoMitRabatt),0) s FROM umsatz").fetchone()["s"] or 1
    return {
        "filialen": fil,
        "marken": vals("marke"), "submarken": vals("submarke"), "linien": vals("linie"),
        "warengruppen": vals("warengruppe"), "oberwarengruppen": vals("oberwarengruppe"),
        "lieferanten": vals("lieferant"), "kategorien": vals("kategorie"),
        "cache": {
            "umsatzVon": tage["a"], "umsatzBis": tage["b"], "umsatzTage": tage["n"],
            "artikel": con.execute("SELECT COUNT(*) c FROM artikel").fetchone()["c"],
            "bestandPositionen": con.execute("SELECT COUNT(*) c FROM bestand").fetchone()["c"],
            "artikelSync": meta_get(con, "artikel_sync"),
            "bestandSync": meta_get(con, "bestand_sync"),
            "autosyncLetzter": meta_get(con, "autosync_letzter"),
            "autosyncStatus": meta_get(con, "autosync_status"),
        },
        "api": api_stand(con),
        "version": VERSION,
        "sonntag": {
            "beruecksichtigt": CFG["sonntag"],
            "tageMitUmsatz": so["t"], "umsatz": so["s"],
            "anteil": (so["s"] / ges * 100) if ges else 0,
        },
    }


# ---------------------------------------------------------------------- Server
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---- Auth-Hilfen
    def _nutzer(self):
        """Aktuell angemeldeter Nutzer aus dem Sitzungs-Cookie oder None."""
        if not CFG["auth"] or neo_auth is None:
            return None
        ck = SimpleCookie(self.headers.get("Cookie", ""))
        tok = ck["sitzung"].value if "sitzung" in ck else None
        if not tok:
            return None
        con = db()
        try:
            return neo_auth.token_pruefen(con, tok)
        finally:
            con.close()

    def _body(self):
        # Body genau einmal von der Leitung lesen und zwischenspeichern.
        # Wichtig für Keep-Alive: wird der Body nicht gelesen, verschiebt er
        # die nächste Anfrage auf derselben Verbindung.
        if getattr(self, "_body_cache", None) is not None:
            return self._body_cache
        length = int(self.headers.get("Content-Length") or 0)
        roh = self.rfile.read(length) if length else b""
        self._body_roh = roh
        ct = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ct:
            try:
                self._body_cache = json.loads(roh or b"{}")
            except Exception:
                self._body_cache = {}
        else:
            self._body_cache = {k: v[0] for k, v in
                                urllib.parse.parse_qs(roh.decode("utf-8", "replace")).items()}
        return self._body_cache

    def _cookie_wert(self, name):
        ck = SimpleCookie(self.headers.get("Cookie", ""))
        return ck[name].value if name in ck else None

    def _cookie_setzen(self, token, loeschen=False):
        teile = ["sitzung=%s" % ("" if loeschen else token),
                 "Path=/", "HttpOnly", "SameSite=Lax"]
        if loeschen:
            teile.append("Max-Age=0")
        else:
            teile.append("Max-Age=%d" % (neo_auth.SESSION_STUNDEN * 3600))
        if CFG["https"]:
            teile.append("Secure")
        self.send_header("Set-Cookie", "; ".join(teile))

    def _vertrauen_cookie_setzen(self, token, loeschen=False):
        """Langlebiges Cookie für 'diesem Gerät vertrauen' (überspringt 2FA)."""
        teile = ["geraet=%s" % ("" if loeschen else token),
                 "Path=/", "HttpOnly", "SameSite=Lax"]
        if loeschen:
            teile.append("Max-Age=0")
        else:
            teile.append("Max-Age=%d" % (neo_auth.VERTRAUEN_TAGE * 86400))
        if CFG["https"]:
            teile.append("Secure")
        self.send_header("Set-Cookie", "; ".join(teile))

    def _auth_routen(self, method, p, q):
        """Behandelt /auth/* und /admin/*. Gibt True zurueck, wenn zustaendig."""
        if neo_auth is None:
            return False

        if p == "/auth/login" and method == "POST":
            b = self._body()
            con = db()
            try:
                u = neo_auth.pruefen(con, b.get("email"), b.get("passwort") or b.get("password"))
                if not u:
                    self.json_out(401, {"error": "E-Mail oder Passwort falsch."})
                    return True
                merken = bool(b.get("angemeldet_bleiben") or b.get("merken"))
                vertrauen_token = None
                # Zweiter Faktor, falls für diesen Nutzer aktiv
                if u.get("twofa"):
                    # Ist dieses Gerät bereits als vertrauenswürdig hinterlegt?
                    vertraut = neo_auth.vertrauen_pruefen(
                        con, self._cookie_wert("geraet"), u["id"])
                    if not vertraut:
                        code = (b.get("code") or "").strip()
                        if not code:
                            self.json_out(200, {"twofa": True})   # Frontend fragt den Code ab
                            return True
                        if not neo_auth.zweifa_login_pruefen(con, u["id"], code):
                            self.json_out(401, {"error": "Der Bestätigungscode stimmt nicht.",
                                                "twofa": True})
                            return True
                    # Bei „angemeldet bleiben" dieses Gerät (weiter) merken
                    if merken:
                        vertrauen_token = neo_auth.vertrauen_token_erzeugen(con, u["id"])
                tok = neo_auth.token_erzeugen(con, u["id"])
            finally:
                con.close()
            body = json.dumps({"ok": True, "nutzer": u}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self._cookie_setzen(tok)
            if vertrauen_token:
                self._vertrauen_cookie_setzen(vertrauen_token)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        # ---- 2FA-Selbstverwaltung (angemeldeter Nutzer)
        if p == "/auth/2fa/start" and method == "POST":
            self._body()   # Body immer lesen (sonst stört er die Keep-Alive-Verbindung)
            me = self._nutzer()
            if not me:
                self.json_out(401, {"error": "Nicht angemeldet."})
                return True
            con = db()
            try:
                self.json_out(200, neo_auth.zweifa_start(con, me["id"], me["email"]))
            finally:
                con.close()
            return True

        if p == "/auth/2fa/aktivieren" and method == "POST":
            me = self._nutzer()
            if not me:
                self.json_out(401, {"error": "Nicht angemeldet."})
                return True
            b = self._body()
            con = db()
            try:
                neo_auth.zweifa_aktivieren(con, me["id"], b.get("code") or "")
                self.json_out(200, {"ok": True})
            except ValueError as e:
                self.json_out(400, {"error": str(e)})
            finally:
                con.close()
            return True

        if p == "/auth/2fa/aus" and method == "POST":
            me = self._nutzer()
            if not me:
                self.json_out(401, {"error": "Nicht angemeldet."})
                return True
            # Zum Ausschalten den aktuellen Code verlangen
            b = self._body()
            con = db()
            try:
                if not neo_auth.zweifa_login_pruefen(con, me["id"], b.get("code") or ""):
                    self.json_out(400, {"error": "Zum Ausschalten bitte den aktuellen Code eingeben."})
                else:
                    neo_auth.zweifa_aus(con, me["id"])
                    self.json_out(200, {"ok": True})
            finally:
                con.close()
            return True

        # ---- Eigenes Passwort ändern (auch für erzwungenen Erst-Wechsel)
        if p == "/auth/passwort" and method == "POST":
            me = self._nutzer()
            if not me:
                self.json_out(401, {"error": "Nicht angemeldet."})
                return True
            b = self._body()
            con = db()
            try:
                neo_auth.passwort_selbst_aendern(
                    con, me["id"], b.get("alt") or "", b.get("neu") or "")
                self.json_out(200, {"ok": True})
            except ValueError as e:
                self.json_out(400, {"error": str(e)})
            finally:
                con.close()
            return True

        if p == "/auth/logout" and method in ("POST", "GET"):
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self._cookie_setzen("", loeschen=True)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        if p == "/auth/me" and method == "GET":
            u = self._nutzer()
            self.json_out(200, {"angemeldet": bool(u), "nutzer": u,
                                "authModus": CFG["auth"]})
            return True

        if p == "/admin/users":
            u = self._nutzer()
            if not u or u["rolle"] != "admin":
                self.json_out(403, {"error": "Nur für Administratoren."})
                return True
            if u.get("einrichtungOffen"):
                self.json_out(403, {"error": "Bitte zuerst die Einrichtung abschließen.",
                                    "setup": True})
                return True
            con = db()
            try:
                if method == "GET":
                    self.json_out(200, {"nutzer": neo_auth.nutzer_liste(con)})
                elif method == "POST":
                    b = self._body()
                    try:
                        uid = neo_auth.nutzer_anlegen(
                            con, b.get("email"), b.get("passwort") or b.get("password") or "",
                            name=b.get("name"), rolle=b.get("rolle") or "user")
                        self.json_out(200, {"ok": True, "id": uid})
                    except ValueError as e:
                        self.json_out(400, {"error": str(e)})
                elif method == "DELETE":
                    uid = int(q.get("id", 0))
                    if uid == u["id"]:
                        self.json_out(400, {"error": "Das eigene Konto lässt sich nicht löschen."})
                    else:
                        neo_auth.nutzer_loeschen(con, uid)
                        self.json_out(200, {"ok": True})
                else:
                    self.json_out(405, {"error": "Methode nicht erlaubt."})
            finally:
                con.close()
            return True

        return False

    def shopify_uebersicht(self, q):
        """Onlineshop-Kennzahlen direkt aus Shopify (unabhängig von NEO)."""
        if neo_shopify is None:
            return self.json_out(501, {"error": "Modul neo_shopify.py fehlt neben neo-proxy.py.",
                                       "shopify": False})
        if not neo_shopify.konfiguriert():
            return self.json_out(200, {
                "verbunden": False,
                "hinweis": "Shopify ist noch nicht verbunden. Hinterlege SHOPIFY_SHOP, "
                           "SHOPIFY_CLIENT_ID und SHOPIFY_CLIENT_SECRET in den "
                           "Servereinstellungen."})
        bis = q.get("bis") or date.today().isoformat()
        von = q.get("von") or (date.fromisoformat(bis) - timedelta(days=29)).isoformat()
        con = db()
        try:
            # Auf Wunsch vorher frisch abholen, sonst kommt alles aus der Datenbank
            if q.get("frisch") == "1":
                # Beim allerersten Mal die volle Historie holen, danach nur den
                # angezeigten Zeitraum.
                if neo_shopify.stand(con).get("tage"):
                    s_von, s_bis = von, bis
                else:
                    s_bis = date.today().isoformat()
                    s_von = (date.today() - timedelta(days=START_TAGE)).isoformat()
                try:
                    neo_shopify.sync(con, s_von, s_bis)
                except neo_shopify.ShopifyFehler as e:
                    return self.json_out(502, {"error": str(e), "verbunden": True})
            st = neo_shopify.stand(con)
            if not st.get("tage"):
                return self.json_out(200, {
                    "verbunden": True, "leer": True, "stand": st,
                    "hinweis": "Noch keine Onlineshop-Daten gespeichert. "
                               "Mit „Jetzt abrufen“ einmalig laden — danach "
                               "aktualisiert der Server täglich von selbst."})
            daten = neo_shopify.uebersicht(con, von, bis)
            daten["verbunden"] = True
            daten["stand"] = st
            return self.json_out(200, daten)
        finally:
            con.close()

    def _geschuetzt(self, p):
        """Pfade, die im Auth-Modus eine Anmeldung erfordern."""
        if not CFG["auth"]:
            return False
        oeffentlich = ("/", "/index.html", "/neo-dashboard.html", "/favicon.ico",
                       "/auth/login", "/auth/logout", "/auth/me", "/ping")
        if p in oeffentlich:
            return False
        return (p.startswith("/data/") or p.startswith("/api/") or p.startswith("/sync")
                or p in ("/backup", "/report", "/jahresgespraech.pdf") or p.startswith("/admin"))

    def _blockiert(self, p):
        """True (und Antwort bereits gesendet), wenn der geschützte Pfad nicht
        freigegeben ist: nicht angemeldet oder Ersteinrichtung noch offen."""
        if not self._geschuetzt(p):
            return False
        nz = self._nutzer()
        if not nz:
            self.json_out(401, {"error": "Nicht angemeldet.", "login": True})
            return True
        if nz.get("einrichtungOffen"):
            self.json_out(403, {"error": "Bitte zuerst die Einrichtung abschließen "
                                         "(neues Passwort und Zwei-Faktor).", "setup": True})
            return True
        return False

    # ---- Routing
    def do_GET(self):
        self._body_cache = None
        self._body_roh = None
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = u.path
        # Health-Check (z. B. für Render): immer ohne Anmeldung, kein NEO-Zugriff
        if p in ("/healthz", "/ping"):
            return self.json_out(200, {"ok": True, "dienst": "meister-dashboard"})
        try:
            if self._auth_routen("GET", p, q):
                return
            if self._blockiert(p):
                return
            if p.startswith("/api/"):
                return self.proxy("GET")
            if p == "/sync/status":
                with JOB_LOCK:
                    return self.json_out(200, dict(JOB))
            if p == "/sync/start":
                return self.sync_start(q)
            if p == "/jahresgespraech.pdf":
                con = db()
                try:
                    q.setdefault("bis", date.today().isoformat())
                    q.setdefault("von", q["bis"][:4] + "-01-01")
                    daten, partner = jahres_pdf(con, q)
                finally:
                    con.close()
                name = "Jahresgespraech-%s-%s.pdf" % (
                    q["bis"][:4], "".join(c if c.isalnum() else "-" for c in partner)[:40])
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
                self.send_header("Content-Length", str(len(daten)))
                self.end_headers()
                return self.wfile.write(daten)
            if p == "/backup":
                return self.json_out(200, backup_db(q.get("ordner")))
            if p == "/report":
                con = db()
                try:
                    return self.json_out(200, wochenreport(con, q.get("ordner"), q.get("bis")))
                finally:
                    con.close()
            if p == "/data/shopify":
                return self.shopify_uebersicht(q)
            if p.startswith("/data/"):
                return self.data(p[len("/data/"):], q)
            return self.serve_static(p)
        except NeoError as e:
            self.json_out(502 if e.status == 0 else e.status, {"error": str(e), "status": e.status})
        except Exception as e:  # noqa: BLE001
            self.json_out(500, {"error": str(e)})

    def do_POST(self):
        self._body_cache = None
        self._body_roh = None
        self._body()   # Body sofort von der Leitung nehmen (Keep-Alive-sicher)
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if self._auth_routen("POST", u.path, q):
                return
            if self._blockiert(u.path):
                return
        except Exception as e:  # noqa: BLE001
            return self.json_out(500, {"error": str(e)})
        self.proxy("POST")

    def do_PUT(self):
        self._body_cache = None
        self._body_roh = None
        if self._blockiert(urllib.parse.urlparse(self.path).path):
            return
        self.proxy("PUT")

    def do_DELETE(self):
        self._body_cache = None
        self._body_roh = None
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if self._auth_routen("DELETE", u.path, q):
            return
        if self._blockiert(u.path):
            return
        self.proxy("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- Handler
    def sync_start(self, q):
        # Im Server-Modus kommen die NEO-Zugangsdaten aus der Umgebung
        # (env_auth), nicht vom Browser — der Nutzer kennt sie nicht.
        auth = env_auth() if CFG["auth"] else self.headers.get("Authorization")
        if not auth:
            return self.json_out(401, {"error":
                "Kein NEO-Zugang hinterlegt." if CFG["auth"]
                else "Kein Authorization-Header."})
        with JOB_LOCK:
            if JOB["running"]:
                return self.json_out(409, {"error": "Es läuft bereits ein Sync."})
        what = set((q.get("what") or "filialen,artikel,umsatz,bestand").split(","))
        bis = q.get("bis") or date.today().isoformat()
        von = q.get("von") or (date.fromisoformat(bis) - timedelta(days=START_TAGE)).isoformat()
        # Einzelne Tage gezielt nachladen, z. B. zum Reparieren leerer Tage
        tage = [t.strip() for t in (q.get("tage") or "").split(",") if t.strip()]
        tage = [t for t in tage if len(t) == 10 and t[4] == "-" and t[7] == "-"][:400] or None
        threading.Thread(target=run_sync, args=(auth, von, bis, what, tage),
                         daemon=True).start()
        return self.json_out(202, {"gestartet": True, "von": von, "bis": bis,
                                   "what": sorted(what),
                                   "tage": len(tage) if tage else 0})

    def data(self, name, q):
        con = db()
        try:
            if name == "dimensions":
                return self.json_out(200, q_dimensions(con))
            q.setdefault("bis", date.today().isoformat())
            q.setdefault("von", (date.fromisoformat(q["bis"]) - timedelta(days=29)).isoformat())
            if name == "kpi":
                return self.json_out(200, q_kpi(con, q))
            if name == "trend":
                return self.json_out(200, q_trend(con, q))
            if name == "zeitreihe":
                return self.json_out(200, shopify_onlinelinie(q_zeitreihe(con, q), q))
            if name == "ranking":
                return self.json_out(200, q_ranking(con, q))
            if name == "windows":
                return self.json_out(200, q_windows(con, q))
            if name == "ytd":
                return self.json_out(200, q_ytd(con, q))
            if name == "bestand":
                return self.json_out(200, q_bestand(con, q))
            if name == "alerts":
                return self.json_out(200, q_alerts(con, q))
            if name == "wochentag":
                return self.json_out(200, q_wochentag(con, q))
            if name == "artikelsuche":
                return self.json_out(200, q_artikelsuche(con, q))
            if name == "artikel":
                return self.json_out(200, q_artikel(con, q))
            if name == "jahresgespraech":
                return self.json_out(200, q_jahresgespraech(con, q))
            if name == "luecken":
                return self.json_out(200, q_luecken(con, q))
            if name == "kapital":
                return self.json_out(200, q_kapital(con, q))
            if name == "sortiment":
                return self.json_out(200, q_sortiment(con, q))
            if name == "filialbenchmark":
                return self.json_out(200, q_filialbenchmark(con, q))
            if name == "neuheiten":
                return self.json_out(200, q_neuheiten(con, q))
            if name == "penner":
                return self.json_out(200, q_penner(con, q))
            if name == "markenmonat":
                return self.json_out(200, q_markenmonat(con, q))
            return self.json_out(404, {"error": "Unbekannt: " + name})
        finally:
            con.close()

    def serve_static(self, p):
        if p in ("/", "/index.html", "/neo-dashboard.html"):
            if not DASHBOARD.exists():
                return self.plain(500, "neo-dashboard.html liegt nicht neben diesem Skript.")
            body = DASHBOARD.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        if p == "/favicon.ico":
            return self.plain(404, "")
        return self.plain(404, "Nicht gefunden. Dashboard liegt unter /")

    def proxy(self, method):
        if not self.path.startswith("/api/"):
            return self.plain(404, "API-Aufrufe muessen mit /api/ beginnen.")
        url = CFG["target"].rstrip("/") + self.path[len("/api"):]
        if getattr(self, "_body_roh", None) is not None:
            payload = self._body_roh or None        # bereits gelesener Body (Keep-Alive)
        else:
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=payload, method=method)
        weglassen = HOP_BY_HOP | {"cookie"}      # Sitzungs-Cookie nie an NEO weiterreichen
        for name, value in self.headers.items():
            if name.lower() not in weglassen:
                req.add_header(name, value)
        # Server-Modus: NEO-Zugang serverseitig setzen, Browser-Auth ignorieren
        if CFG["auth"]:
            sa = env_auth()
            if sa:
                req.add_header("Authorization", sa)
        req.add_header("Accept-Encoding", "identity")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as r:
                self.relay(r.status, r.headers, r.read())
        except urllib.error.HTTPError as e:
            self.relay(e.code, e.headers, e.read())
        except urllib.error.URLError as e:
            self.plain(502, "Verbindung zum NEO-Server fehlgeschlagen: %s" % e.reason)

    # ---- Ausgabe
    def relay(self, status, headers, body):
        self.send_response(status)
        for name, value in (headers or {}).items():
            n = name.lower()
            if n in HOP_BY_HOP or n == "content-length" or n == "www-authenticate":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def json_out(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def plain(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ------------------------------------------------------------ Automatischer Sync
def env_auth():
    """Zugangsdaten aus Umgebungsvariablen - nur fuer den automatischen Sync.

    Setze vorher selbst:
        NEO_USER     Benutzername
        NEO_MANDANT  Mandantennummer
        NEO_PASS     Passwort

    Wird eine davon nicht gesetzt, laeuft der Auto-Sync nicht an.
    Der Wert wird nirgends gespeichert oder protokolliert."""
    import os
    u, m, p = os.environ.get("NEO_USER"), os.environ.get("NEO_MANDANT"), os.environ.get("NEO_PASS")
    if not (u and m and p):
        return None
    return "Basic " + base64.b64encode(("%s;%s:%s" % (u, m, p)).encode()).decode()


def shopify_nachziehen(von, bis, still=False):
    """Onlineshop-Zahlen nachziehen. Laeuft im Auto-Sync mit; Fehler duerfen den
    NEO-Sync nicht stoeren."""
    if neo_shopify is None or not neo_shopify.konfiguriert():
        return None
    con = db()
    try:
        n = neo_shopify.sync(con, von, bis)
        if not still:
            print("  [%s] Shopify: %d Tage aktualisiert."
                  % (datetime.now().strftime("%H:%M"), n))
        return n
    except Exception as e:                                # noqa: BLE001
        if not still:
            print("  [%s] Shopify-Sync fehlgeschlagen: %s"
                  % (datetime.now().strftime("%H:%M"), e))
        return None
    finally:
        con.close()


def auto_sync_loop(uhrzeit, tage_zurueck):
    """Wartet bis zur naechsten faelligen Uhrzeit und zieht dann nach."""
    auth = env_auth()
    shop = neo_shopify is not None and neo_shopify.konfiguriert()
    if not auth and not shop:
        print("  Auto-Sync: NEO_USER / NEO_MANDANT / NEO_PASS nicht gesetzt - deaktiviert.")
        return
    if not auth:
        print("  Auto-Sync: NEO-Zugang fehlt - es wird nur Shopify nachgezogen.")
    hh, mm = (int(x) for x in uhrzeit.split(":"))
    print("  Auto-Sync: täglich um %02d:%02d (letzte %d Tage + Bestand%s)"
          % (hh, mm, tage_zurueck, " + Onlineshop" if shop else ""))
    while True:
        now = datetime.now()
        ziel = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if ziel <= now:
            ziel += timedelta(days=1)
        time.sleep(max(30, (ziel - now).total_seconds()))
        with JOB_LOCK:
            if JOB["running"]:
                continue
        bis = date.today()
        von = bis - timedelta(days=tage_zurueck)
        print("  [%s] Auto-Sync startet…" % datetime.now().strftime("%Y-%m-%d %H:%M"))
        err = None
        if auth:
            # WICHTIG: umsatz_force. Ohne das Erzwingen wuerde der heutige Tag,
            # der morgens um 6 Uhr noch fast leer ist, als "geladen" vermerkt
            # und nie wieder geholt -- der Umsatz jedes Tages bliebe auf null.
            # Deshalb wird das ganze Fenster jede Nacht neu geholt.
            run_sync(auth, von.isoformat(), bis.isoformat(),
                     {"umsatz", "umsatz_force", "bestand", "artikel"})
            with JOB_LOCK:
                err = JOB["error"]
        shopify_nachziehen(von.isoformat(), bis.isoformat())
        con = db()
        meta_set(con, "autosync_letzter", datetime.now().isoformat(timespec="seconds"))
        meta_set(con, "autosync_status", err or "ok")
        con.commit()
        con.close()
        print("  [%s] Auto-Sync fertig: %s" % (
            datetime.now().strftime("%H:%M"), err or "ok"))

        if err:
            continue
        # Taegliche Sicherung, dazu montags der Wochenreport
        try:
            b = backup_db()
            print("      Sicherung: %s (%.1f MB)" % (b["datei"], b["groesseMB"]))
        except Exception as e:  # noqa: BLE001
            print("      Sicherung fehlgeschlagen: %s" % e)
        if date.today().weekday() == 0:
            try:
                con = db()
                r = wochenreport(con)
                con.close()
                print("      Wochenreport: %s" % r["report"])
            except Exception as e:  # noqa: BLE001
                print("      Wochenreport fehlgeschlagen: %s" % e)


def main():
    ap = argparse.ArgumentParser(description="Meister Parfumerie Dashboard — Backend")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")),
                    help="Port. Standard aus Umgebungsvariable PORT (z. B. bei Render) oder 8080.")
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--db", default=str(HERE / "neo-cache.db"))
    ap.add_argument("--delay", type=float, default=0.6,
                    help="Pause zwischen API-Requests in Sekunden (Rate-Limit)")
    ap.add_argument("--auto-sync", metavar="HH:MM", default=None,
                    help="Taeglich um diese Uhrzeit nachziehen. Braucht die "
                         "Umgebungsvariablen NEO_USER, NEO_MANDANT, NEO_PASS.")
    ap.add_argument("--auto-sync-tage", type=int, default=7,
                    help="Wie viele Tage der Auto-Sync rueckwirkend prueft (Standard 7)")
    ap.add_argument("--ohne-sonntag", action="store_true",
                    help="Sonntage als Schliesstage ueberall ausklammern. "
                         "Standard: aus, es zaehlen alle Kalendertage.")
    ap.add_argument("--auth", action="store_true",
                    help="Benutzeranmeldung erzwingen (Server-Modus). NEO-Zugang "
                         "kommt dann aus NEO_USER/NEO_MANDANT/NEO_PASS, der erste "
                         "Admin aus ADMIN_EMAIL/ADMIN_PASS.")
    ap.add_argument("--https", action="store_true",
                    help="Sitzungs-Cookies mit Secure-Flag (nur hinter HTTPS).")
    ap.add_argument("--host", default=None,
                    help="Bind-Adresse. Standard 127.0.0.1 lokal, 0.0.0.0 im Auth-Modus.")
    args = ap.parse_args()

    CFG.update(target=args.target, db=args.db, delay=args.delay,
               sonntag=not args.ohne_sonntag, auth=args.auth, https=args.https)
    if args.auth and neo_auth is None:
        print("Fehler: --auth verlangt neo_auth.py neben neo-proxy.py.")
        sys.exit(1)
    init_db()

    if CFG["auth"]:
        con = db()
        try:
            status = neo_auth.admin_bootstrap(con)
        finally:
            con.close()
        if status:
            print("  " + status)

    host = args.host or ("0.0.0.0" if CFG["auth"] else "127.0.0.1")

    print("Meister Parfumerie Dashboard — Backend")
    print("  Ziel      : %s" % CFG["target"])
    print("  Cache     : %s" % CFG["db"])
    print("  Pause     : %.1f s zwischen API-Requests" % CFG["delay"])
    print("  Sonntage  : %s" % ("werden mitgerechnet" if CFG["sonntag"]
                                else "ausgeklammert (--ohne-sonntag)"))
    print("  Anmeldung : %s" % ("erforderlich (Server-Modus)" if CFG["auth"]
                                else "aus (lokaler Modus)"))
    if CFG["auth"] and not env_auth():
        print("  ACHTUNG   : NEO_USER/NEO_MANDANT/NEO_PASS nicht gesetzt — "
              "Sync und Live-Abruf funktionieren erst danach.")
    print("  Adresse   : http://%s:%d" % (host, args.port))
    if args.auto_sync:
        threading.Thread(target=auto_sync_loop,
                         args=(args.auto_sync, args.auto_sync_tage), daemon=True).start()
    print("\n  Beenden mit Strg+C\n")

    try:
        with Server((host, args.port), Handler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")
    except OSError as e:
        print("Start fehlgeschlagen: %s" % e)
        print("Port belegt? Dann: python neo-proxy.py --port 8081")
        sys.exit(1)


if __name__ == "__main__":
    main()
