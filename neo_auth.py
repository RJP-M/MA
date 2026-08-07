#!/usr/bin/env python3
"""
neo_auth.py — Benutzeranmeldung für das Meister Parfumerie Dashboard.

Ausschließlich Python-Standardbibliothek. Keine Fremdpakete, damit keine
zusätzliche Angriffsfläche entsteht und der Server ohne Installation läuft.

Bausteine:
  * Passwörter werden mit scrypt (hashlib) gehasht — nie im Klartext gespeichert.
  * Sitzungen laufen über ein HMAC-signiertes Token (kein serverseitiger
    Sitzungsspeicher nötig); das Token trägt Benutzer-ID und Ablaufzeit.
  * Der Signaturschlüssel kommt aus der Umgebungsvariable SECRET_KEY oder wird
    einmalig erzeugt und in der Tabelle meta abgelegt.

Die Funktionen erwarten eine offene sqlite3-Verbindung (con) mit row_factory
= sqlite3.Row, wie sie neo-proxy.py über db() liefert.
"""

import base64
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time
import urllib.parse
from datetime import datetime

# scrypt-Parameter (bewusst kräftig, für Login unkritisch bei der Laufzeit)
_SCRYPT = dict(n=16384, r=8, p=1, dklen=32)
SESSION_STUNDEN = 12          # Gültigkeit eines Tokens, sofern nicht anders gesetzt


# ----------------------------------------------------- Schutz vor Durchprobieren
# Passwörter und 2FA-Codes lassen sich sonst unbegrenzt durchprobieren — bei
# einem 6-stelligen Code reichen dafür wenige Stunden. Nach zu vielen
# Fehlversuchen wird das Konto kurz gesperrt (nur im Arbeitsspeicher; ein
# Neustart setzt die Zähler zurück, das ist für diesen Zweck ausreichend).
MAX_FEHLVERSUCHE = 8
SPERRE_SEKUNDEN = 300
_VERSUCHE = {}                # schluessel -> [anzahl, gesperrt_bis]
_VERSUCHE_LOCK = threading.Lock()


def _sperre_aktiv(schluessel):
    with _VERSUCHE_LOCK:
        eintrag = _VERSUCHE.get(schluessel)
        if not eintrag:
            return 0
        rest = eintrag[1] - time.time()
        if rest <= 0 and eintrag[0] >= MAX_FEHLVERSUCHE:
            del _VERSUCHE[schluessel]     # Sperre abgelaufen: Zähler zurücksetzen
            return 0
        return int(rest) if (rest > 0 and eintrag[0] >= MAX_FEHLVERSUCHE) else 0


def _fehlversuch(schluessel):
    with _VERSUCHE_LOCK:
        eintrag = _VERSUCHE.setdefault(schluessel, [0, 0])
        eintrag[0] += 1
        if eintrag[0] >= MAX_FEHLVERSUCHE:
            eintrag[1] = time.time() + SPERRE_SEKUNDEN


def _versuche_zuruecksetzen(schluessel):
    with _VERSUCHE_LOCK:
        _VERSUCHE.pop(schluessel, None)


def sperre_sekunden(email):
    """Restdauer einer Login-Sperre in Sekunden (0 = nicht gesperrt)."""
    return _sperre_aktiv("pw:" + (email or "").strip().lower())


# --------------------------------------------------------------------------- DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  name TEXT,
  pass_hash TEXT NOT NULL,
  pass_salt TEXT NOT NULL,
  rolle TEXT NOT NULL DEFAULT 'user',   -- 'admin' oder 'user'
  aktiv INTEGER NOT NULL DEFAULT 1,
  erstellt TEXT,
  letzter_login TEXT
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def init(con):
    con.executescript(SCHEMA)
    # Migration: 2FA-Spalten nachruesten, falls die Tabelle aelter ist
    have = {r["name"] for r in con.execute("PRAGMA table_info(app_user)")}
    for col, typ in (("totp_secret", "TEXT"), ("totp_tmp", "TEXT"),
                     ("totp_aktiv", "INTEGER NOT NULL DEFAULT 0"),
                     # 1 = muss beim naechsten Login ein neues Passwort setzen
                     ("pw_wechsel", "INTEGER NOT NULL DEFAULT 0")):
        if col not in have:
            con.execute("ALTER TABLE app_user ADD COLUMN %s %s" % (col, typ))
    con.commit()


def _meta_get(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def _meta_set(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    con.commit()


# ----------------------------------------------------------------- Signaturschlüssel
def signaturschluessel(con):
    """Aus Umgebungsvariable oder einmalig erzeugt und persistiert."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env.encode("utf-8")
    s = _meta_get(con, "secret_key")
    if not s:
        s = secrets.token_hex(32)
        _meta_set(con, "secret_key", s)
    return s.encode("utf-8")


# --------------------------------------------------------------------- Passwörter
def _hash(passwort, salt_hex):
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.scrypt(passwort.encode("utf-8"), salt=salt, **_SCRYPT)
    return dk.hex()


def passwort_setzen(con, user_id, passwort):
    if len(passwort) < 8:
        raise ValueError("Passwort muss mindestens 8 Zeichen haben.")
    salt = secrets.token_hex(16)
    con.execute("UPDATE app_user SET pass_hash=?, pass_salt=? WHERE id=?",
                (_hash(passwort, salt), salt, user_id))
    con.commit()


def _passwort_pruefen(row, passwort):
    erwartet = row["pass_hash"]
    ist = _hash(passwort, row["pass_salt"])
    return hmac.compare_digest(erwartet, ist)


# ------------------------------------------------------------------ Nutzerverwaltung
def nutzer_anlegen(con, email, passwort, name=None, rolle="user", pw_wechsel=True):
    """Legt ein Konto an. pw_wechsel=True erzwingt beim ersten Login ein neues
    Passwort (Standard für vom Admin angelegte Mitarbeiterkonten)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("Bitte eine gültige E-Mail-Adresse angeben.")
    if rolle not in ("admin", "user"):
        raise ValueError("Rolle muss 'admin' oder 'user' sein.")
    if con.execute("SELECT 1 FROM app_user WHERE email=?", (email,)).fetchone():
        raise ValueError("Diese E-Mail ist bereits vergeben.")
    if len(passwort) < 8:
        raise ValueError("Passwort muss mindestens 8 Zeichen haben.")
    salt = secrets.token_hex(16)
    con.execute(
        "INSERT INTO app_user(email,name,pass_hash,pass_salt,rolle,aktiv,erstellt,pw_wechsel) "
        "VALUES(?,?,?,?,?,1,?,?)",
        (email, name or email.split("@")[0], _hash(passwort, salt), salt, rolle,
         datetime.now().isoformat(timespec="seconds"), 1 if pw_wechsel else 0))
    con.commit()
    return con.execute("SELECT id FROM app_user WHERE email=?", (email,)).fetchone()["id"]


def passwort_selbst_aendern(con, user_id, alt, neu):
    """Eigenes Passwort ändern: altes muss stimmen, neues muss sich unterscheiden.
    Löscht zugleich die Pflicht zum Passwortwechsel."""
    row = con.execute("SELECT * FROM app_user WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise ValueError("Konto nicht gefunden.")
    if not _passwort_pruefen(row, alt or ""):
        raise ValueError("Das bisherige Passwort ist nicht korrekt.")
    neu = neu or ""
    if len(neu) < 8:
        raise ValueError("Das neue Passwort muss mindestens 8 Zeichen haben.")
    if _passwort_pruefen(row, neu):
        raise ValueError("Bitte ein anderes als das bisherige Passwort wählen.")
    salt = secrets.token_hex(16)
    con.execute("UPDATE app_user SET pass_hash=?, pass_salt=?, pw_wechsel=0 WHERE id=?",
                (_hash(neu, salt), salt, user_id))
    con.commit()
    return True


def nutzer_liste(con):
    rows = [dict(r) for r in con.execute(
        "SELECT id,email,name,rolle,aktiv,erstellt,letzter_login,totp_aktiv "
        "FROM app_user ORDER BY erstellt")]
    for r in rows:
        r["twofa"] = bool(r.pop("totp_aktiv", 0))
    return rows


def nutzer_loeschen(con, user_id):
    con.execute("DELETE FROM app_user WHERE id=?", (user_id,))
    con.commit()


def nutzer_aktiv_setzen(con, user_id, aktiv):
    con.execute("UPDATE app_user SET aktiv=? WHERE id=?", (1 if aktiv else 0, user_id))
    con.commit()


def anzahl_nutzer(con):
    return con.execute("SELECT COUNT(*) c FROM app_user").fetchone()["c"]


def admin_bootstrap(con):
    """Legt beim ersten Start einen Admin an, wenn noch kein Nutzer existiert
    und ADMIN_EMAIL / ADMIN_PASS gesetzt sind. Gibt eine Statuszeile zurück."""
    if anzahl_nutzer(con) > 0:
        return None
    email = os.environ.get("ADMIN_EMAIL")
    pw = os.environ.get("ADMIN_PASS")
    if not (email and pw):
        return ("Noch kein Benutzer vorhanden. Setze ADMIN_EMAIL und ADMIN_PASS "
                "(einmalig) und starte neu, um den ersten Admin anzulegen.")
    try:
        # Der erste Admin setzt sein Passwort selbst über die Umgebungsvariable,
        # daher kein erzwungener Passwortwechsel. 2FA muss er dennoch einrichten.
        nutzer_anlegen(con, email, pw, rolle="admin", pw_wechsel=False)
        return "Erster Admin angelegt: %s" % email.strip().lower()
    except ValueError as e:
        return "Admin konnte nicht angelegt werden: %s" % e


# ---------------------------------------------------------------------- Anmeldung
def pruefen(con, email, passwort):
    """Gibt bei Erfolg das Nutzer-Dict zurück, sonst None."""
    email = (email or "").strip().lower()
    if _sperre_aktiv("pw:" + email):
        return None
    row = con.execute("SELECT * FROM app_user WHERE email=?", (email,)).fetchone()
    if not row or not row["aktiv"]:
        # Auch bei unbekanntem Nutzer einmal hashen, um Zeitmessung zu erschweren
        _hash(passwort or "", secrets.token_hex(16))
        _fehlversuch("pw:" + email)
        return None
    if not _passwort_pruefen(row, passwort or ""):
        _fehlversuch("pw:" + email)
        return None
    _versuche_zuruecksetzen("pw:" + email)
    con.execute("UPDATE app_user SET letzter_login=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), row["id"]))
    con.commit()
    twofa = bool(row["totp_aktiv"])
    mussPw = bool(row["pw_wechsel"])
    return {"id": row["id"], "email": row["email"], "name": row["name"],
            "rolle": row["rolle"], "twofa": twofa,
            "mussPasswort": mussPw, "mussZweifa": (not twofa),
            "einrichtungOffen": (mussPw or not twofa)}


# -------------------------------------------------------------------- Sitzungen
def _salt_fingerabdruck(con, pass_salt):
    """Kurzer Fingerabdruck des Passwort-Salts. Er wandert mit ins Token:
    Ändert sich das Passwort (und damit der Salt), werden alle bestehenden
    Sitzungen dieses Kontos sofort ungültig — vorher blieben gestohlene
    Tokens auch nach einem Passwortwechsel bis zu 12 Stunden gültig."""
    return hmac.new(signaturschluessel(con), (pass_salt or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()[:12]


def token_erzeugen(con, user_id, stunden=SESSION_STUNDEN):
    r = con.execute("SELECT pass_salt FROM app_user WHERE id=?", (user_id,)).fetchone()
    fp = _salt_fingerabdruck(con, r["pass_salt"] if r else "")
    ablauf = int(time.time()) + stunden * 3600
    payload = "%d.%d.%s" % (user_id, ablauf, fp)
    sig = hmac.new(signaturschluessel(con), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    roh = "%s.%s" % (payload, sig)
    return base64.urlsafe_b64encode(roh.encode("utf-8")).decode("ascii")


def token_pruefen(con, token):
    """Gibt das Nutzer-Dict zurück, wenn das Token gültig und nicht abgelaufen
    ist und der Nutzer noch aktiv existiert — sonst None."""
    if not token:
        return None
    try:
        roh = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        uid_s, abl_s, fp, sig = roh.split(".")
        payload = "%s.%s.%s" % (uid_s, abl_s, fp)
    except Exception:
        return None
    erwartet = hmac.new(signaturschluessel(con), payload.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(erwartet, sig):
        return None
    if int(abl_s) < int(time.time()):
        return None
    row = con.execute("SELECT id,email,name,rolle,aktiv,totp_aktiv,pw_wechsel,pass_salt "
                      "FROM app_user WHERE id=?", (int(uid_s),)).fetchone()
    if not row or not row["aktiv"]:
        return None
    if not hmac.compare_digest(_salt_fingerabdruck(con, row["pass_salt"]), fp):
        return None                     # Passwort wurde inzwischen geändert
    twofa = bool(row["totp_aktiv"])
    mussPw = bool(row["pw_wechsel"])
    return {"id": row["id"], "email": row["email"], "name": row["name"],
            "rolle": row["rolle"], "twofa": twofa,
            "mussPasswort": mussPw, "mussZweifa": (not twofa),
            "einrichtungOffen": (mussPw or not twofa)}


# -------------------------------------------------------- Vertrauenswürdige Geräte
# „Angemeldet bleiben": Nach vollständigem Login (inkl. 2FA) kann sich ein Gerät
# merken lassen. Danach genügen E-Mail + Passwort; der zweite Faktor entfällt für
# die Gültigkeitsdauer. Das Token ist HMAC-signiert und an das aktuelle 2FA-
# Geheimnis gebunden — wird 2FA neu eingerichtet, verlieren alte Geräte-Tokens
# automatisch ihre Gültigkeit.
VERTRAUEN_TAGE = 30


def _totp_fingerabdruck(con, secret):
    return hmac.new(signaturschluessel(con), (secret or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()[:16]


def vertrauen_token_erzeugen(con, user_id, tage=VERTRAUEN_TAGE):
    row = con.execute("SELECT totp_secret FROM app_user WHERE id=?", (user_id,)).fetchone()
    fp = _totp_fingerabdruck(con, row["totp_secret"] if row else "")
    ablauf = int(time.time()) + tage * 86400
    payload = "trust.%d.%d.%s" % (user_id, ablauf, fp)
    sig = hmac.new(signaturschluessel(con), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(("%s.%s" % (payload, sig)).encode("utf-8")).decode("ascii")


def vertrauen_pruefen(con, token, user_id):
    """True, wenn das Geräte-Token gültig, nicht abgelaufen und für diesen Nutzer
    (mit passendem 2FA-Geheimnis) ist."""
    if not token:
        return False
    try:
        roh = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        marker, uid_s, abl_s, fp, sig = roh.split(".")
        payload = "trust.%s.%s.%s" % (uid_s, abl_s, fp)
    except Exception:
        return False
    if marker != "trust":
        return False
    erwartet = hmac.new(signaturschluessel(con), payload.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(erwartet, sig):
        return False
    if int(uid_s) != int(user_id) or int(abl_s) < int(time.time()):
        return False
    row = con.execute("SELECT totp_secret,aktiv FROM app_user WHERE id=?",
                      (int(uid_s),)).fetchone()
    if not row or not row["aktiv"]:
        return False
    return hmac.compare_digest(_totp_fingerabdruck(con, row["totp_secret"]), fp)


# ============================================================================
# Zwei-Faktor-Authentisierung (TOTP nach RFC 6238) — Standardbibliothek.
# Kompatibel mit Google Authenticator, Microsoft Authenticator, Authy usw.
# ============================================================================
AUSGEBER = "Meister Parfumerie"


def totp_secret_neu():
    """160-Bit-Geheimnis als Base32 (ohne Auffüllzeichen), wie Authenticator-Apps es erwarten."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_code(secret_b32, zeit=None, schritt=30, stellen=6):
    if zeit is None:
        zeit = time.time()
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    zaehler = struct.pack(">Q", int(zeit // schritt))
    h = hmac.new(key, zaehler, hashlib.sha1).digest()
    off = h[-1] & 0x0F
    code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** stellen)
    return str(code).zfill(stellen)


def totp_pruefen(secret_b32, code, fenster=1):
    """Prüft einen Code, ±fenster Zeitschritte Toleranz (Uhr-Ungenauigkeit)."""
    if not secret_b32 or not code:
        return False
    code = str(code).strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return False
    jetzt = time.time()
    for d in range(-fenster, fenster + 1):
        if hmac.compare_digest(_totp_code(secret_b32, jetzt + d * 30), code):
            return True
    return False


def otpauth_uri(secret_b32, email):
    """otpauth://-URI für den QR-Code."""
    label = urllib.parse.quote("%s:%s" % (AUSGEBER, email))
    p = urllib.parse.urlencode({"secret": secret_b32, "issuer": AUSGEBER,
                                "algorithm": "SHA1", "digits": "6", "period": "30"})
    return "otpauth://totp/%s?%s" % (label, p)


def zweifa_aktiv(con, user_id):
    r = con.execute("SELECT totp_aktiv FROM app_user WHERE id=?", (user_id,)).fetchone()
    return bool(r and r["totp_aktiv"])


def zweifa_start(con, user_id, email):
    """Erzeugt ein vorläufiges Geheimnis (noch nicht aktiv) und gibt QR-Daten zurück."""
    secret = totp_secret_neu()
    con.execute("UPDATE app_user SET totp_tmp=? WHERE id=?", (secret, user_id))
    con.commit()
    return {"secret": secret, "uri": otpauth_uri(secret, email)}


def zweifa_aktivieren(con, user_id, code):
    """Prüft den ersten Code gegen das vorläufige Geheimnis und schaltet 2FA scharf."""
    r = con.execute("SELECT totp_tmp FROM app_user WHERE id=?", (user_id,)).fetchone()
    if not r or not r["totp_tmp"]:
        raise ValueError("Bitte zuerst die Einrichtung starten.")
    if not totp_pruefen(r["totp_tmp"], code):
        raise ValueError("Der Code stimmt nicht. Bitte den aktuell angezeigten Code eingeben.")
    con.execute("UPDATE app_user SET totp_secret=?, totp_tmp=NULL, totp_aktiv=1 WHERE id=?",
                (r["totp_tmp"], user_id))
    con.commit()
    return True


def zweifa_aus(con, user_id):
    con.execute("UPDATE app_user SET totp_secret=NULL, totp_tmp=NULL, totp_aktiv=0 WHERE id=?",
                (user_id,))
    con.commit()


def zweifa_login_pruefen(con, user_id, code):
    """Prüft den 6-stelligen Code beim Login gegen das aktive Geheimnis.
    Nach zu vielen Fehlversuchen wird kurz gesperrt (6-stellige Codes sind
    sonst in vertretbarer Zeit durchprobierbar)."""
    schluessel = "2fa:%d" % int(user_id)
    if _sperre_aktiv(schluessel):
        return False
    r = con.execute("SELECT totp_secret FROM app_user WHERE id=?", (user_id,)).fetchone()
    ok = bool(r and r["totp_secret"] and totp_pruefen(r["totp_secret"], code))
    if ok:
        _versuche_zuruecksetzen(schluessel)
    else:
        _fehlversuch(schluessel)
    return ok
