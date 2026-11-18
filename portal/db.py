"""Eigen database van de portal (SQLite): inventaris van alle apparaten ooit gezien + logboek.

Omada blijft leidend voor de actuele toestand; deze database onthoudt wat Omada vergeet
(namen, type/merk/model, laatst bekende IP/VLAN) en legt vast wie wat wanneer veranderde.
"""
import json
import sqlite3
import threading
import time

from omada import norm_mac

_path = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac TEXT PRIMARY KEY,
    name TEXT, hostname TEXT, ip TEXT, vlan INTEGER, network TEXT,
    wireless INTEGER, conn TEXT, vendor TEXT, dtype TEXT, model TEXT, category TEXT,
    corr_type TEXT, corr_vendor TEXT, corr_model TEXT,
    first_seen INTEGER, last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, mac TEXT, kind TEXT NOT NULL, detail TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS ev_mac ON events(mac, ts);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
"""


def init(path):
    global _path
    _path = path
    with _conn() as c:
        c.executescript(SCHEMA)


def _conn():
    c = sqlite3.connect(_path, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _unknown(v):
    return not v or str(v).lower() in ("unknown", "others")


def log(mac, kind, detail="", source="portal"):
    with _lock, _conn() as c:
        c.execute("INSERT INTO events(ts, mac, kind, detail, source) VALUES (?,?,?,?,?)",
                  (int(time.time()), norm_mac(mac) if mac else None, kind, detail, source))


def set_correction(mac, dtype, vendor, model):
    """Onthoud wat de gebruiker als type/merk/model koos (om terug te zetten als Omada het vergeet)."""
    mac = norm_mac(mac)
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO devices(mac, first_seen, last_seen) VALUES (?,?,?)",
                  (mac, int(time.time()), int(time.time())))
        c.execute("UPDATE devices SET corr_type=?, corr_vendor=?, corr_model=? WHERE mac=?",
                  (dtype or None, vendor or None, model or None, mac))


def remember_name(mac, name):
    mac = norm_mac(mac)
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO devices(mac, first_seen, last_seen) VALUES (?,?,?)",
                  (mac, int(time.time()), int(time.time())))
        c.execute("UPDATE devices SET name=? WHERE mac=?", (name, mac))


def sync(active, known, vname):
    """Werk de inventaris bij met wat Omada nu weet. Logt nieuwe apparaten en IP/VLAN/naam-
    wijzigingen. Geeft een lijst terug van apparaten waarvan Omada gegevens kwijt is:
    [(mac, row, {"name": bool, "corr": bool}), ...]."""
    now = int(time.time())
    lost = []
    with _lock, _conn() as c:
        rows = {r["mac"]: dict(r) for r in c.execute("SELECT * FROM devices")}
        first = not rows  # allereerste keer: niet elk apparaat als "nieuw" loggen
        seen = {}
        for k in known:  # bekende (ook offline) clients: alleen basisgegevens
            mac = norm_mac(k["mac"])
            seen[mac] = {"name": k.get("name") if k.get("name") != k["mac"] else "", "wireless": k.get("wireless"),
                         "vlan": k.get("vid"), "last": int((k.get("lastSeen") or 0) / 1000) or None, "active": False}
        for a in active:
            mac = norm_mac(a["mac"])
            seen[mac] = {"name": a.get("name") if a.get("name") != a["mac"] else "", "hostname": a.get("hostName"),
                         "ip": a.get("ip"), "vlan": a.get("vid"), "wireless": a.get("wireless"),
                         "conn": a.get("ssid") if a.get("wireless") else
                         f"{a.get('switchName') or a.get('gatewayName') or ''} poort {a.get('port') or '?'}",
                         "vendor": a.get("vendor"), "dtype": a.get("deviceType"), "model": a.get("model"),
                         "category": a.get("deviceCategory"), "last": now, "active": True}
        for mac, s in seen.items():
            old = rows.get(mac)
            if not old:
                c.execute("INSERT INTO devices(mac, first_seen, last_seen) VALUES (?,?,?)",
                          (mac, s.get("last") or now, s.get("last") or now))
                old = {"mac": mac}
                if s["active"] and not first:
                    c.execute("INSERT INTO events(ts, mac, kind, detail, source) VALUES (?,?,?,?,?)",
                              (now, mac, "nieuw", f"Nieuw apparaat gezien: {s.get('name') or s.get('hostname') or mac}"
                               f" ({vname.get(s.get('vlan'), s.get('vlan'))}, {s.get('ip') or '?'})", "omada"))
            upd = {}
            # Een "echte" naam is er een die iemand heeft ingesteld: niet leeg, niet het MAC en niet
            # gewoon de hostname (die toont Omada als er geen naam is).
            host = s.get("hostname") or old.get("hostname")
            real = s.get("name") if s.get("name") not in (None, "", mac, host) else ""
            if real:
                if old.get("name") and old["name"] != real and s["active"]:
                    c.execute("INSERT INTO events(ts, mac, kind, detail, source) VALUES (?,?,?,?,?)",
                              (now, mac, "naam", f"Naam: {old['name']} → {real}", "omada"))
                upd["name"] = real
            elif old.get("name") and s["active"] and old["name"] not in (host, mac):
                # Omada toont het apparaat zonder (eigen) naam terwijl wij er een kennen -> terugzetten
                lost.append((mac, old, {"name": True, "corr": False}))
            elif not old.get("name") and s.get("name") and s.get("name") != mac:
                upd["name"] = s["name"]  # hostname als voorlopige naam onthouden
            if s["active"]:
                if old.get("ip") and s.get("ip") and old["ip"] != s["ip"]:
                    c.execute("INSERT INTO events(ts, mac, kind, detail, source) VALUES (?,?,?,?,?)",
                              (now, mac, "ip", f"IP: {old['ip']} → {s['ip']}", "omada"))
                if old.get("vlan") and s.get("vlan") and old["vlan"] != s["vlan"]:
                    c.execute("INSERT INTO events(ts, mac, kind, detail, source) VALUES (?,?,?,?,?)",
                              (now, mac, "vlan", f"VLAN: {vname.get(old['vlan'], old['vlan'])} → "
                               f"{vname.get(s['vlan'], s['vlan'])}", "omada"))
                for f in ("hostname", "ip", "conn"):
                    if s.get(f):
                        upd[f] = s[f]
                for f in ("vendor", "dtype", "model", "category"):
                    if not _unknown(s.get(f)):
                        upd[f] = s[f]
                if old.get("corr_type") and _unknown(s.get("dtype")):
                    hit = next((x for x in lost if x[0] == mac), None)
                    if hit:
                        hit[2]["corr"] = True
                    else:
                        lost.append((mac, old, {"name": False, "corr": True}))
            if s.get("vlan"):
                upd["vlan"] = s["vlan"]
                upd["network"] = vname.get(s["vlan"])
            if s.get("wireless") is not None:
                upd["wireless"] = 1 if s["wireless"] else 0
            if s.get("last"):
                upd["last_seen"] = max(s["last"], old.get("last_seen") or 0)
            if upd:
                c.execute(f"UPDATE devices SET {', '.join(k + '=?' for k in upd)} WHERE mac=?",
                          (*upd.values(), mac))
        if first and seen:
            c.execute("INSERT INTO events(ts, mac, kind, detail, source) VALUES (?,?,?,?,?)",
                      (now, None, "start", f"Inventaris gestart met {len(seen)} apparaten uit Omada", "portal"))
    return lost


def devices():
    with _conn() as c:
        return {r["mac"]: dict(r) for r in c.execute("SELECT * FROM devices")}


def device(mac):
    with _conn() as c:
        r = c.execute("SELECT * FROM devices WHERE mac=?", (norm_mac(mac),)).fetchone()
        return dict(r) if r else None


def events(mac=None, limit=200):
    with _conn() as c:
        if mac:
            q = c.execute("SELECT * FROM events WHERE mac=? ORDER BY ts DESC, id DESC LIMIT ?", (norm_mac(mac), limit))
        else:
            q = c.execute("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        return [dict(r) for r in q]


def export():
    """Alles als JSON (voor backup/download)."""
    return json.dumps({"devices": list(devices().values()), "events": events(limit=100000)}, indent=1)
