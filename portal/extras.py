"""Uitbreidingen van de wifi-portal:

- gebeurtenissen + API voor de Homey-app (Flow-kaarten)
- waakhond voor kritieke apparaten, melding bij nieuwe apparaten
- gastsleutels met tijdslimiet (QR + printbaar kaartje)
- internetpauze per apparaat (direct, tijdelijk of op schema)
- onderhoud: zwak signaal, VLAN-check, opruimlijst, backup/herstel
- ACL-pagina: VLAN-matrix (standaard dicht) + uitzonderingen per apparaat/poort

Wordt onderaan app.py geïmporteerd en gebruikt de helpers daaruit.
"""
import ipaddress
import json
import os
import shutil
import sqlite3
import threading
import time

from flask import Response, abort, flash, redirect, render_template, request, url_for

import app as A
import db
from omada import OmadaError, is_random_mac, norm_mac

app = A.app
OMADA = A.OMADA
ACL_FILE = os.path.join(A.DATA, "acl.json")
BACKUP_DIR = os.path.join(A.DATA, "backups")
# tweede backupmap: BACKUP_DIR, of standaard /backup zodra die gemount is (bv. /DATA/Backup/wifiportaal:/backup)
EXTRA_BACKUP_DIR = os.environ.get("BACKUP_DIR") or ("/backup" if os.path.isdir("/backup") else "")
SNAP_DIR = os.path.join(A.DATA, "acl_snapshots")
PREFIX = "portal: "

# -- gedeelde cache, gevuld door de monitor (elke 30 s) ----------------------
CACHE = {"clients": [], "ts": 0, "nets": []}
WATCH = {}  # mac -> {"last_on": ts, "alerted": bool}
_lock = threading.Lock()


def cfg_get(key, default):
    v = A.cfg().get(key)
    return default if v is None else v


def cfg_set(**kw):
    c = A.cfg()
    c.update(kw)
    A._save(A.CFG_FILE, c)


def nets():
    if CACHE["nets"] and time.time() - CACHE.get("nets_ts", 0) < 300:
        return CACHE["nets"]
    n = OMADA.networks(A.cfg()["site"])
    CACHE.update(nets=n, nets_ts=time.time())
    return n


def clients(fresh=False):
    if fresh or time.time() - CACHE["ts"] > 45:
        CACHE.update(clients=OMADA.clients(A.cfg()["site"]), ts=time.time())
    return CACHE["clients"]


def vname_map():
    return {n["vlan"]: n["name"] for n in nets()}


def dev_name(mac, cl=None, inv=None):
    cl = cl or {}
    if cl.get("name") and cl["name"] != cl.get("mac"):
        return cl["name"]
    d = (inv or {}).get(mac) or db.device(mac) or {}
    return d.get("name") or cl.get("hostName") or d.get("hostname") or mac


def now_local():
    return time.localtime()


# =============================================================================
# Database-uitbreiding: signaalmetingen
# =============================================================================
def _sigdb():
    c = sqlite3.connect(db._path, timeout=15)
    c.row_factory = sqlite3.Row
    return c


with _sigdb() as _c:
    _c.executescript("""
    CREATE TABLE IF NOT EXISTS signal (mac TEXT, ts INTEGER, rssi INTEGER, ap TEXT, radio INTEGER);
    CREATE INDEX IF NOT EXISTS sig_mac ON signal(mac, ts);
    """)


def _sample_signal(cl):
    now = int(time.time())
    rows = [(norm_mac(c["mac"]), now, int(c["rssi"]), c.get("apName") or "", c.get("radioId"))
            for c in cl if c.get("wireless") and c.get("rssi") is not None]
    with db._lock, _sigdb() as c:
        c.executemany("INSERT INTO signal VALUES (?,?,?,?,?)", rows)
        c.execute("DELETE FROM signal WHERE ts < ?", (now - 8 * 86400,))


def signal_stats():
    now = int(time.time())
    with _sigdb() as c:
        q = c.execute("""SELECT mac, AVG(CASE WHEN ts > ? THEN rssi END) a24, AVG(rssi) a7, MIN(rssi) mn, COUNT(*) n
                         FROM signal WHERE ts > ? GROUP BY mac""", (now - 86400, now - 7 * 86400))
        return {r["mac"]: dict(r) for r in q}


# =============================================================================
# Monitor: nieuwe apparaten, waakhond, internetpauze, gastsleutels, backup
# =============================================================================
def _watch_tick(cl):
    ov = A.overrides()
    active = {norm_mac(c["mac"]): c for c in cl if c.get("active", True)}
    now = time.time()
    inv = None
    for mac, o in ov.items():
        if not o.get("critical"):
            WATCH.pop(mac, None)
            continue
        w = WATCH.setdefault(mac, {"last_on": now, "alerted": False})
        if w.get("init") is None:  # na een herstart: laatst gezien uit de database
            d = db.device(mac) or {}
            w["last_on"] = d.get("last_seen") or now
            w["init"] = True
        mins = max(1, int(o.get("crit_min") or 5))
        if mac in active:
            if w["alerted"]:
                inv = inv or db.devices()
                down = int((now - w["last_on"]) / 60)
                db.log(mac, "online", f"{dev_name(mac, active[mac], inv)} is weer online (was ±{down} min weg)", "waakhond")
            w.update(last_on=now, alerted=False)
        elif not w["alerted"] and now - w["last_on"] >= mins * 60:
            inv = inv or db.devices()
            db.log(mac, "offline", f"{dev_name(mac, None, inv)} is al {int((now - w['last_on']) / 60)} min offline", "waakhond")
            w["alerted"] = True


def in_schedule(sched, t=None):
    if not sched or not sched.get("on"):
        return False
    t = t or now_local()
    days = sched.get("days") or list(range(7))
    try:
        fh, fm = map(int, sched.get("from", "21:00").split(":"))
        th, tm = map(int, sched.get("to", "07:00").split(":"))
    except ValueError:
        return False
    cur, frm, to = t.tm_hour * 60 + t.tm_min, fh * 60 + fm, th * 60 + tm
    if frm == to:
        return False
    if frm < to:
        return t.tm_wday in days and frm <= cur < to
    # over middernacht: het deel na middernacht hoort bij de dag ervoor
    if cur >= frm:
        return t.tm_wday in days
    return cur < to and (t.tm_wday - 1) % 7 in days


def pause_wanted(o):
    return bool(o.get("pause") or (o.get("pause_until") or 0) > time.time() or in_schedule(o.get("sched")))


def _pause_tick():
    ov = A.overrides()
    st = A.state()
    paused = set(st.get("paused", []))
    sid = A.cfg()["site"]
    changed = False
    inv = None
    for mac in set(ov) | paused:
        o = ov.get(mac, {})
        want = pause_wanted(o)
        if want and mac not in paused:
            OMADA.call("POST", f"/v1/{{cid}}/sites/{sid}/clients/{mac}/block")
            paused.add(mac)
            changed = True
            inv = inv or db.devices()
            why = "handmatig" if o.get("pause") else ("tijdelijk" if (o.get("pause_until") or 0) > time.time() else "schema")
            db.log(mac, "pauze", f"Internetpauze aan voor {dev_name(mac, None, inv)} ({why})", "portal")
        elif not want and mac in paused:
            OMADA.call("POST", f"/v1/{{cid}}/sites/{sid}/clients/{mac}/unblock")
            paused.discard(mac)
            changed = True
            inv = inv or db.devices()
            db.log(mac, "pauze", f"Internetpauze uit voor {dev_name(mac, None, inv)}", "portal")
            if o.get("pause_until") and o["pause_until"] <= time.time():
                ov2 = A.overrides()
                ov2.get(mac, {}).pop("pause_until", None)
                A._save(A.OVR_FILE, ov2)
    if changed:
        st = A.state()
        st["paused"] = sorted(paused)
        A._save(A.STATE_FILE, st)


def _guest_tick():
    st = A.state()
    g = st.get("guest", {})
    now = time.time()
    due = [n for n, m in g.items() if m.get("expires", now + 1) <= now]
    if not due:
        return
    c = A.cfg()
    sid, pid = c["site"], c["profile"]
    entries = {e["name"]: e for e in OMADA.ppsk(sid, pid)["entries"]}
    for name in due:
        e = entries.get(name)
        if e and not e["mac"]:
            A.psk_delete(sid, pid, e)
        db.log(None, "verlopen", f"Gastsleutel '{name}' verlopen en verwijderd", "portal")
        s2 = A.state()
        s2.get("guest", {}).pop(name, None)
        A._save(A.STATE_FILE, s2)


def _monitor_loop():
    n = 0
    while True:
        try:
            c = A.cfg()
            if c.get("site") and c.get("profile"):
                cl = clients(fresh=True)
                vn = vname_map()
                A._restore(c["site"], db.sync(cl, [], vn))  # logt nieuwe apparaten direct
                _watch_tick(cl)
                if n % 2 == 0:
                    _pause_tick()
                    _guest_tick()
                    _acl_deadman()
                if n % 10 == 0:
                    _sample_signal(cl)
                    import dashboard
                    dashboard.sample(cl)
                if n % 20 == 0:
                    _backup_if_due()
        except Exception as e:  # noqa: BLE001
            try:
                if n % 20 == 0:
                    db.log(None, "fout", f"Monitor: {e}", "portal")
            except Exception:  # noqa: BLE001
                pass
        n += 1
        time.sleep(30)


# =============================================================================
# Gastsleutels
# =============================================================================
def guest_until(choice, hours=None):
    now = time.time()
    if choice == "weekend":  # tot maandag 09:00
        t = now_local()
        days = (7 - t.tm_wday) % 7 or 7
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days, 9, 0, 0, 0, 0, -1))
    h = float(hours if choice == "custom" else choice)
    if not 0 < h <= 24 * 31:
        raise OmadaError("Kies een duur tussen een uur en een maand")
    return now + h * 3600


def create_guest(label, until):
    c, sid, pid, prof, nets_, vname, st = A.context()
    label = (label or "").strip() or "Gast " + time.strftime("%d-%m %H:%M")
    name = A.psk_name(label)
    if any(e["name"] == name for e in prof["entries"]):
        name = A.psk_name(f"{name}-{time.strftime('%H%M')}")
    psk = A.new_psk({e["psk"] for e in prof["entries"]})
    OMADA.add_psk(sid, pid, {"name": name, "psk": psk, "vlan": c["guest_vlan"]})
    s = A.state()
    s.setdefault("guest", {})[name] = {"created": time.time(), "expires": until, "label": label}
    A._save(A.STATE_FILE, s)
    db.log(None, "gast", f"Gastsleutel '{name}' gemaakt, geldig tot {time.strftime('%d-%m-%Y %H:%M', time.localtime(until))}",
           "portal")
    return name, psk, prof["ssids"], vname.get(c["guest_vlan"], c["guest_vlan"])


@app.post("/guest/new")
@A.login_required
@A.configured
def guest_new():
    try:
        until = guest_until(request.form.get("dur", "24"), request.form.get("hours") or 0)
        name = create_guest(request.form.get("name", ""), until)[0]
    except Exception as e:  # noqa: BLE001
        flash(f"Gastsleutel maken mislukt: {e}", "err")
        return redirect(url_for("index"))
    return redirect(url_for("guest_card", name=name))


@app.get("/guest/<path:name>")
@A.login_required
@A.configured
def guest_card(name):
    try:
        c, sid, pid, prof, nets_, vname, st = A.context()
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)
    meta = A.state().get("guest", {}).get(name)
    e = next((x for x in prof["entries"] if x["name"] == name), None)
    if not meta or not e:
        flash("Deze gastsleutel bestaat niet (meer)", "err")
        return redirect(url_for("index"))
    ssid = request.args.get("ssid") or (prof["ssids"][0] if prof["ssids"] else "")
    return render_template("guest.html", e=e, meta=meta, ssids=prof["ssids"], ssid=ssid,
                           qr=A.wifi_qr(ssid, e["psk"]), vlan=vname.get(e["vlan"], e["vlan"]))


@app.post("/guest/delete")
@A.login_required
@A.configured
def guest_delete():
    name = request.form["name"]
    try:
        c, sid, pid, prof, *_ = A.context()
        if name not in A.state().get("guest", {}):
            raise OmadaError("Geen gastsleutel van de portal")
        e = next((x for x in prof["entries"] if x["name"] == name), None)
        if e:
            A.psk_delete(sid, pid, e)
        s = A.state()
        s["guest"].pop(name, None)
        A._save(A.STATE_FILE, s)
        flash(f"Gastsleutel '{name}' verwijderd", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Verwijderen mislukt: {e}", "err")
    return redirect(url_for("index"))


# =============================================================================
# Waakhond en pauze per apparaat (kaart op de apparaatpagina)
# =============================================================================
@app.post("/client/<mac>/extra")
@A.login_required
@A.configured
def client_extra(mac):
    mac = norm_mac(mac)
    act = request.form.get("action")
    ov = A.overrides()
    o = ov.setdefault(mac, {})
    try:
        if act == "watch":
            o["critical"] = request.form.get("critical") == "1"
            o["crit_min"] = max(1, min(1440, int(request.form.get("crit_min") or 5)))
            if not o["critical"]:
                o.pop("critical"), o.pop("crit_min")
            flash("Waakhond " + ("aan" if o.get("critical") else "uit"), "ok")
        elif act == "pause":
            what = request.form.get("what")
            if what == "on":
                o["pause"] = True
                o.pop("pause_until", None)
            elif what == "off":
                o.pop("pause", None)
                o.pop("pause_until", None)
            elif what == "for":
                o.pop("pause", None)
                o["pause_until"] = time.time() + max(1, int(request.form.get("minutes") or 30)) * 60
            flash("Internetpauze bijgewerkt (binnen een minuut actief)", "ok")
        elif act == "sched":
            days = [int(d) for d in request.form.getlist("days")]
            fr, to = request.form.get("from", "21:00"), request.form.get("to", "07:00")
            for v in (fr, to):
                h, m = map(int, v.split(":"))
                assert 0 <= h < 24 and 0 <= m < 60
            o["sched"] = {"on": request.form.get("on") == "1", "from": fr, "to": to, "days": days or list(range(7))}
            if not o["sched"]["on"]:
                o.pop("sched")
            flash("Pauzeschema opgeslagen", "ok")
        if not o:
            ov.pop(mac, None)
        A._save(A.OVR_FILE, ov)
        if act in ("pause", "sched"):
            try:
                _pause_tick()
            except Exception as e:  # noqa: BLE001
                flash(f"Pauze direct toepassen mislukt ({e}); de portal probeert het over een minuut opnieuw", "err")
    except Exception as e:  # noqa: BLE001
        flash(f"Mislukt: {e}", "err")
    return redirect(url_for("client", mac=mac) + "#extra")


@app.template_global()
def client_extra_info(mac):
    o = A.overrides().get(mac, {})
    w = WATCH.get(mac, {})
    return {"critical": bool(o.get("critical")), "crit_min": o.get("crit_min", 5), "alerted": w.get("alerted"),
            "pause": bool(o.get("pause")), "pause_until": o.get("pause_until") if (o.get("pause_until") or 0) > time.time() else None,
            "sched": o.get("sched") or {"on": False, "from": "21:00", "to": "07:00", "days": list(range(7))},
            "paused_now": mac in set(A.state().get("paused", [])), "in_sched": in_schedule(o.get("sched"))}


@app.template_global()
def index_extra():
    """Gastsleutels + waakhond-overzicht voor de Sleutels-pagina (uit de cache van de monitor)."""
    st = A.state()
    now = time.time()
    guests = sorted(({"name": n, **m} for n, m in st.get("guest", {}).items()), key=lambda g: g.get("expires", 0))
    ov = A.overrides()
    try:
        cl = clients()
    except Exception:  # noqa: BLE001
        cl = []
    active = {norm_mac(c["mac"]): c for c in cl if c.get("active", True)}
    inv = db.devices()
    crit = []
    for mac, o in ov.items():
        if o.get("critical"):
            w = WATCH.get(mac, {})
            crit.append({"mac": mac, "name": dev_name(mac, active.get(mac), inv), "online": mac in active,
                         "since": w.get("last_on"), "alerted": w.get("alerted"), "min": o.get("crit_min", 5)})
    crit.sort(key=lambda x: (x["online"], x["name"].lower()))
    paused = [{"mac": m, "name": dev_name(m, active.get(m), inv)} for m in st.get("paused", [])]
    return {"guests": guests, "crit": crit, "paused": paused, "now": now}


# =============================================================================
# API voor de Homey-app
# =============================================================================
EVENT_KINDS = ("nieuw", "offline", "online", "gast", "verlopen", "pauze", "acl")


@app.get("/api/v1/events")
@A.api_auth
def api_events():
    after = request.args.get("after", type=int)
    with db._conn() as c:
        last = c.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        if after is None:
            return {"last": last, "events": []}
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM events WHERE id > ? AND kind IN ({','.join('?' * len(EVENT_KINDS))}) ORDER BY id LIMIT 100",
            (after, *EVENT_KINDS))]
    inv = db.devices()
    vn = vname_map()
    out = []
    for r in rows:
        d = inv.get(r["mac"] or "", {})
        out.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "detail": r["detail"] or "", "mac": r["mac"] or "",
                    "name": d.get("name") or d.get("hostname") or r["mac"] or "", "ip": d.get("ip") or "",
                    "vlan": vn.get(d.get("vlan"), d.get("vlan") or "")})
    return {"last": rows[-1]["id"] if len(rows) == 100 else last, "events": out}


@app.get("/api/v1/devices")
@A.api_auth
def api_devices():
    q = (request.args.get("q") or "").lower()
    inv = db.devices()
    act = {norm_mac(c["mac"]) for c in clients() if c.get("active", True)}
    out = []
    for mac, d in inv.items():
        nm = d.get("name") or d.get("hostname") or mac
        if q and q not in f"{nm} {mac} {d.get('ip') or ''}".lower():
            continue
        out.append({"mac": mac, "name": nm, "ip": d.get("ip") or "", "online": mac in act,
                    "vlan": d.get("network") or ""})
    out.sort(key=lambda x: (not x["online"], x["name"].lower()))
    return {"devices": out[:50]}


@app.post("/api/v1/guest")
@A.api_auth
def api_guest():
    j = request.get_json(silent=True) or {}
    try:
        until = guest_until("custom", j.get("hours") or 24)
        name, psk, ssids, vlan = create_guest(str(j.get("name") or ""), until)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}, 400
    ssid = j.get("ssid") if j.get("ssid") in ssids else (ssids[0] if ssids else "")
    return {"name": name, "psk": psk, "ssid": ssid, "vlan": vlan, "expires": until,
            "qr": str(A.wifi_qr(ssid, psk))}


@app.post("/api/v1/pause")
@A.api_auth
def api_pause():
    j = request.get_json(silent=True) or {}
    try:
        mac = norm_mac(j.get("mac") or "")
    except Exception:  # noqa: BLE001
        return {"error": "Ongeldig MAC-adres"}, 400
    ov = A.overrides()
    o = ov.setdefault(mac, {})
    if j.get("pause"):
        if j.get("minutes"):
            o.pop("pause", None)
            o["pause_until"] = time.time() + int(j["minutes"]) * 60
        else:
            o["pause"] = True
    else:
        o.pop("pause", None)
        o.pop("pause_until", None)
    if not o:
        ov.pop(mac, None)
    A._save(A.OVR_FILE, ov)
    try:
        _pause_tick()
    except Exception as e:  # noqa: BLE001
        return {"error": f"Opgeslagen, maar direct toepassen mislukt: {e}"}, 502
    return {"mac": mac, "paused": mac in set(A.state().get("paused", []))}


# =============================================================================
# Onderhoud: zwak signaal, VLAN-check, opruimlijst, backup
# =============================================================================
DEFAULT_VLAN_RULES = "IoT => IoT\nUser => User\nSecurity => Security\nGuest => Guest\nipc => Security\ncamera => Security\ndoorbell => Security"
RADIO = {0: "2,4 GHz", 1: "5 GHz", 2: "5 GHz-2", 3: "6 GHz"}


def parse_vlan_rules(text, nets_):
    by_name = {n["name"].lower(): n for n in nets_}
    by_vlan = {str(n["vlan"]): n for n in nets_}
    rules = []
    for line in (text or "").splitlines():
        if "=>" not in line:
            continue
        term, target = (x.strip() for x in line.split("=>", 1))
        t = target.lower()
        n = by_vlan.get(t) or by_name.get(t) or next((v for k, v in by_name.items() if k.startswith(t)), None)
        if term and n:
            rules.append((term.lower(), n))
    return rules


def expected_vlan(c, d, ov, rules):
    """Welk VLAN hoort dit apparaat volgens de regels? Eerst het naam-voorvoegsel ('IoT - ...'),
    dan type/categorie/merk/model."""
    name = (c.get("name") or d.get("name") or "")
    prefix = name.split(" - ", 1)[0].strip().lower() if " - " in name else ""
    for term, n in rules:
        if prefix and prefix == term:
            return n, f"naam begint met '{name.split(' - ', 1)[0].strip()}'"
    hay = " ".join(str(x or "") for x in (ov.get("type"), d.get("corr_type"), c.get("deviceType"), c.get("deviceCategory"),
                                         d.get("dtype"), d.get("category"))).lower()
    for term, n in rules:
        if term in hay:
            return n, f"type bevat '{term}'"
    low = name.lower()
    for term, n in rules:
        if term in low:
            return n, f"naam bevat '{term}'"
    return None, ""


@app.route("/onderhoud", methods=["GET", "POST"])
@A.login_required
@A.configured
def maintenance():
    if request.method == "POST":
        act = request.form.get("action")
        try:
            if act == "settings":
                cfg_set(vlan_rules=request.form.get("vlan_rules", "")[:4000],
                        signal_threshold=int(request.form.get("signal_threshold") or -72),
                        cleanup_days=max(1, int(request.form.get("cleanup_days") or 30)))
                flash("Instellingen opgeslagen", "ok")
            elif act == "retention":
                keep = max(1, min(3650, int(request.form.get("backup_keep") or 14)))
                dev_days = max(0, min(3650, int(request.form.get("backup_device_days") or 0)))
                log_days = max(0, min(3650, int(request.form.get("backup_log_days") or 0)))
                cfg_set(backup_keep=keep, backup_device_days=dev_days, backup_log_days=log_days)
                n = prune_backups()
                flash(f"Backups worden {keep} dagen bewaard; apparaten: "
                      + (f"gezien in de laatste {dev_days} dagen" if dev_days else "alles")
                      + "; logboek: " + (f"laatste {log_days} dagen" if log_days else "niet")
                      + (f"; {n} oudere backup(s) verwijderd" if n else ""), "ok")
            elif act == "cleanup":
                c, sid, pid, prof, *_ = A.context()
                n = 0
                for name in request.form.getlist("key"):
                    e = next((x for x in prof["entries"] if x["name"] == name and x["mac"]), None)
                    if e:
                        A.psk_delete(sid, pid, e)
                        db.log(e["mac"], "opgeruimd", f"Sleutel '{name}' opgeruimd (lang niet gezien)", "portal")
                        n += 1
                m = 0
                for mac in request.form.getlist("forget"):
                    with db._lock, db._conn() as cx:
                        cx.execute("DELETE FROM devices WHERE mac=?", (norm_mac(mac),))
                    m += 1
                flash(f"{n} sleutel(s) verwijderd, {m} apparaat/apparaten uit de portal-inventaris gehaald", "ok")
            elif act == "backup":
                path = make_backup()
                flash(f"Backup gemaakt: {os.path.basename(path)}", "ok")
            elif act == "restore":
                parts = request.form.getlist("part")
                if not parts:
                    raise OmadaError("Kies minstens één onderdeel om te herstellen")
                flash(restore_backup(request.form["file"], parts), "ok")
            elif act == "upload":
                f = request.files.get("backup")
                if not f or not f.filename:
                    raise OmadaError("Kies een backupbestand")
                try:
                    data = json.loads(f.read().decode("utf-8"))
                except ValueError:
                    raise OmadaError("Dit is geen geldig backupbestand (geen JSON)")
                if not isinstance(data, dict) or "ppsk" not in data or "version" not in data:
                    raise OmadaError("Dit is geen backup van de wifi-portal")
                os.makedirs(BACKUP_DIR, exist_ok=True)
                name = time.strftime("wifi-portal-upload-%Y%m%d-%H%M%S.json")
                with open(os.path.join(BACKUP_DIR, name), "w") as out:
                    json.dump(data, out, indent=1)
                os.chmod(os.path.join(BACKUP_DIR, name), 0o600)
                parts = request.form.getlist("part")
                if parts:
                    flash(restore_backup(name, parts), "ok")
                else:
                    flash(f"Backup ingelezen als {name}; kies hieronder wat je wilt herstellen", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"Mislukt: {e}", "err")
        anchor = request.form.get("action") or ""
        return redirect(url_for("maintenance") + "#" + ("backup" if anchor in ("retention", "restore", "upload") else anchor))

    try:
        c, sid, pid, prof, nets_, vname, st = A.context()
        cl = clients(fresh=True)
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)
    inv = db.devices()
    ov_all = A.overrides()
    now = time.time()

    # zwak signaal
    thr = int(cfg_get("signal_threshold", -72))
    stats = signal_stats()
    weak = []
    for x in cl:
        if not x.get("wireless") or x.get("rssi") is None:
            continue
        mac = norm_mac(x["mac"])
        s = stats.get(mac, {})
        a7 = s.get("a7")
        if x["rssi"] < thr or (a7 is not None and a7 < thr):
            weak.append({"mac": mac, "name": dev_name(mac, x, inv), "rssi": x["rssi"], "a24": s.get("a24"), "a7": a7,
                         "mn": s.get("mn"), "ap": x.get("apName"), "band": RADIO.get(x.get("radioId"), "?"),
                         "ch": x.get("channel"), "snr": x.get("snr"),
                         "structural": a7 is not None and a7 < thr and (s.get("n") or 0) >= 12})
    weak.sort(key=lambda w: (w["a7"] if w["a7"] is not None else w["rssi"]))

    # VLAN-check
    rules_text = cfg_get("vlan_rules", DEFAULT_VLAN_RULES)
    rules = parse_vlan_rules(rules_text, nets_)
    wrong = []
    for x in cl:
        mac = norm_mac(x["mac"])
        d = inv.get(mac, {})
        exp, why = expected_vlan(x, d, ov_all.get(mac, {}), rules)
        vid = x.get("vid")
        if exp and vid and exp["vlan"] != vid:
            wrong.append({"mac": mac, "name": dev_name(mac, x, inv), "now": vname.get(vid, vid), "exp": exp["name"],
                          "why": why, "wireless": x.get("wireless")})
    wrong.sort(key=lambda w: w["name"].lower())

    # opruimlijst
    days = int(cfg_get("cleanup_days", 30))
    act_macs = {norm_mac(x["mac"]) for x in cl}
    old_keys = []
    for e in prof["entries"]:
        if e["mac"] and e["mac"] not in act_macs:
            ls = (inv.get(e["mac"]) or {}).get("last_seen")
            if not ls or now - ls > days * 86400:
                old_keys.append({"name": e["name"], "mac": e["mac"], "last": ls, "vlan": vname.get(e["vlan"], e["vlan"])})
    bound = {e["mac"] for e in prof["entries"] if e["mac"]}
    old_inv = sorted(({"mac": m, "name": d.get("name") or d.get("hostname") or m, "last": d.get("last_seen")}
                      for m, d in inv.items()
                      if m not in act_macs and m not in bound and m not in ov_all
                      and (d.get("last_seen") or 0) < now - max(days, 90) * 86400),
                     key=lambda r: r["last"] or 0)

    return render_template("maintenance.html", weak=weak, thr=thr, wrong=wrong, rules_text=rules_text,
                           old_keys=old_keys, old_inv=old_inv, days=days, backups=list_backups(),
                           keep=int(cfg_get("backup_keep", 14)), extra_dir=EXTRA_BACKUP_DIR, nets=nets_,
                           dev_days=int(cfg_get("backup_device_days", 0)), log_days=int(cfg_get("backup_log_days", 0)))


# -- backup ------------------------------------------------------------------
def _backup_dirs():
    return [BACKUP_DIR] + ([EXTRA_BACKUP_DIR] if EXTRA_BACKUP_DIR and os.path.isdir(EXTRA_BACKUP_DIR) else [])


def _bpath(name):
    """Pad van een backup: eerst /data/backups, anders de tweede backupmap (bv. na een verse installatie)."""
    for d in _backup_dirs():
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(BACKUP_DIR, name)


def list_backups():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    out, seen = [], set()
    files = []
    for d in _backup_dirs():
        files += [(f, d) for f in os.listdir(d) if f.startswith("wifi-portal-") and f.endswith(".json")]
    for f, d in sorted(files, key=lambda x: x[0], reverse=True):
        if f in seen:
            continue
        seen.add(f)
        if True:
            p = os.path.join(d, f)
            info = ""
            try:  # korte samenvatting van de inhoud
                d = json.load(open(p))
                ct = d.get("content") or {}
                info = (f"{len(d.get('ppsk') or [])} sleutels, {len(d.get('devices') or [])} apparaten"
                        + (f" (gezien ≤{ct['device_days']} d)" if ct.get("device_days") else "")
                        + (f", logboek {ct['log_days']} d" if ct.get("log_days") else ""))
            except Exception:  # noqa: BLE001
                pass
            out.append({"file": f, "size": os.path.getsize(p), "ts": os.path.getmtime(p), "upload": "-upload-" in f,
                        "info": info, "only_extra": d != BACKUP_DIR})
    return out


def make_backup():
    c = A.cfg()
    sid, pid = c["site"], c["profile"]
    now = int(time.time())
    ppsk = OMADA.ppsk(sid, pid)["entries"]
    ov = A.overrides()
    dev_days = int(cfg_get("backup_device_days", 0))
    log_days = int(cfg_get("backup_log_days", 0))
    keep_macs = {e["mac"] for e in ppsk if e["mac"]} | set(ov)  # met sleutel of eigen instellingen: altijd mee
    devices = [d for d in db.devices().values()
               if not dev_days or d["mac"] in keep_macs or (d.get("last_seen") or 0) >= now - dev_days * 86400]
    data = {
        "version": 1, "ts": now,
        "ppsk": ppsk,
        "state": A.state(), "overrides": ov,
        "config": {k: v for k, v in c.items() if k != "api_token"},
        "acl": _load_acl(),
        "devices": devices,
        "content": {"device_days": dev_days, "log_days": log_days},
    }
    if log_days:
        with db._conn() as cx:
            data["events"] = [dict(r) for r in cx.execute("SELECT * FROM events WHERE ts >= ? ORDER BY id",
                                                          (now - log_days * 86400,))]
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = time.strftime("wifi-portal-%Y%m%d-%H%M.json")
    path = os.path.join(BACKUP_DIR, name)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)
    os.chmod(path, 0o600)
    if EXTRA_BACKUP_DIR and os.path.isdir(EXTRA_BACKUP_DIR):
        dst = os.path.join(EXTRA_BACKUP_DIR, name)
        shutil.copy2(path, dst)
        try:  # eigenaar gelijk aan de map, zodat je hem via de share kunt openen
            st = os.stat(EXTRA_BACKUP_DIR)
            os.chown(dst, st.st_uid, st.st_gid)
        except OSError:
            pass
    prune_backups()
    return path


def prune_backups():
    """Backups ouder dan de bewaartermijn weg (de nieuwste blijft altijd staan)."""
    keep = int(cfg_get("backup_keep", 14))
    n = 0
    for d in _backup_dirs():
        if not os.path.isdir(d):
            continue
        files = sorted((f for f in os.listdir(d) if f.startswith("wifi-portal-") and f.endswith(".json")),
                       key=lambda f: os.path.getmtime(os.path.join(d, f)))
        for f in files[:-1]:
            p = os.path.join(d, f)
            if time.time() - os.path.getmtime(p) > keep * 86400:
                os.remove(p)
                n += 1
    return n


def _backup_if_due():
    t = now_local()
    last = A.state().get("last_backup", 0)
    if t.tm_hour == 3 and time.time() - last > 20 * 3600:
        make_backup()
        st = A.state()
        st["last_backup"] = time.time()
        A._save(A.STATE_FILE, st)


@app.get("/onderhoud/backup/<name>")
@A.login_required
def backup_download(name):
    if not (name.startswith("wifi-portal-") and name.endswith(".json") and "/" not in name):
        abort(404)
    p = _bpath(name)
    if not os.path.exists(p):
        abort(404)
    return Response(open(p, "rb").read(), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def restore_backup(name, parts):
    if not (name.startswith("wifi-portal-") and name.endswith(".json") and "/" not in name):
        raise OmadaError("Onbekende backup")
    data = json.load(open(_bpath(name)))
    c, sid, pid, prof, *_ = A.context()
    done = []
    if "names" in parts:
        cur = {norm_mac(x["mac"]): x for x in clients(fresh=True)}
        n = 0
        for d in data.get("devices", []):
            mac = d["mac"]
            if d.get("name") and mac in cur and (cur[mac].get("name") in (None, "", cur[mac]["mac"], cur[mac].get("hostName"))):
                OMADA.set_client_name(sid, mac, d["name"])
                db.remember_name(mac, d["name"])
                n += 1
        done.append(f"{n} naam/namen teruggezet")
    if "keys" in parts:
        have = {e["name"] for e in prof["entries"]}
        have_mac = {e["mac"] for e in prof["entries"] if e["mac"]}
        n = 0
        for e in data.get("ppsk", []):
            # alleen eigen (aan een MAC gebonden) sleutels; gedeelde sleutels raakt de portal nooit aan
            if e.get("mac") and e["name"] not in have and e["mac"] not in have_mac:
                OMADA.add_psk(sid, pid, e)
                n += 1
        done.append(f"{n} ontbrekende eigen sleutel(s) teruggezet")
    if "overrides" in parts:
        ov = A.overrides()
        for mac, o in (data.get("overrides") or {}).items():
            ov[mac] = {**o, **ov.get(mac, {})}
        A._save(A.OVR_FILE, ov)
        done.append("notities, iconen, waakhond en pauze-instellingen teruggezet")
    if "acl" in parts and data.get("acl"):
        a = _load_acl()
        a["allow"], a["exceptions"] = data["acl"].get("allow", []), data["acl"].get("exceptions", [])
        _save_acl(a)
        done.append("ACL-instellingen teruggezet (nog niet toegepast; doe dat op de ACL-pagina)")
    db.log(None, "hersteld", f"Backup {name}: " + "; ".join(done), "portal")
    return "Hersteld uit " + name + ": " + ("; ".join(done) or "niets gekozen")


# =============================================================================
# ACL: VLAN-matrix (standaard dicht) + uitzonderingen
# =============================================================================
PROTO = {"all": ([256], "alles"), "tcp": ([6], "TCP"), "udp": ([17], "UDP"), "tcpudp": ([6, 17], "TCP+UDP"),
         "icmp": ([1], "ICMP")}


def _load_acl():
    a = A._load(ACL_FILE, {})
    a.setdefault("enabled", False)
    a.setdefault("allow", [])          # ["25>50", ...] = VLAN 25 mag verbindingen starten naar VLAN 50
    a.setdefault("exceptions", [])     # zie acl_add_exception
    a.setdefault("versions", [])       # toegepaste versies (voor terugzetten)
    return a


def _save_acl(a):
    A._save(ACL_FILE, a)


def _acl_osg():
    sid = A.cfg()["site"]
    return list(OMADA.pages(f"/v1/{{cid}}/sites/{sid}/acls/osg-acls"))


def _groups():
    sid = A.cfg()["site"]
    r = OMADA.call("GET", f"/v1/{{cid}}/sites/{sid}/profiles/groups")
    return r if isinstance(r, list) else (r or {}).get("data", [])


def _ex_label(x, inv=None):
    inv = inv or {}
    src = x.get("src_name") or (inv.get(x["src"]) or {}).get("name") or x["src"]
    if x["dst_kind"] == "vlan":
        dst = f"VLAN {x.get('dst_name') or x['dst']}"
    else:
        dst = x.get("dst_name") or (inv.get(x["dst"]) or {}).get("name") or x["dst"]
    p = PROTO.get(x.get("proto") or "all", PROTO["all"])[1]
    ports = f" poort {x['ports']}" if x.get("ports") else ""
    return f"{src} → {dst} ({p}{ports})"


def desired_rules(a, nets_):
    """Bouwt de gewenste portal-regels in volgorde: uitzonderingen, toegestane VLAN-paren, blokkade."""
    by_vlan = {n["vlan"]: n for n in nets_}
    groups, rules = {}, []

    def ip_group(ip, mask=32, ports=None):
        if ports:
            name = f"{PREFIX}{ip}/{mask} :{ports}"[:64]
            groups[name] = {"name": name, "type": 1, "ipList": [{"ip": ip, "mask": mask}], "portType": 0,
                            "portList": [p.strip() for p in ports.split(",") if p.strip()]}
        else:
            name = f"{PREFIX}{ip}/{mask}"[:64]
            groups[name] = {"name": name, "type": 0, "ipList": [{"ip": ip, "mask": mask}]}
        return name

    for i, x in enumerate(a["exceptions"], 1):
        if not x.get("src_ip"):
            continue
        proto, _ = PROTO.get(x.get("proto") or "all", PROTO["all"])
        ports = (x.get("ports") or "").replace(" ", "")
        src = ip_group(x["src_ip"])
        if x["dst_kind"] == "vlan":
            n = by_vlan.get(int(x["dst"]))
            if not n:
                continue
            if ports:
                net = ipaddress.ip_interface(n["subnet"]).network
                dst, dtype = ip_group(str(net.network_address), net.prefixlen, ports), 2
            else:
                dst, dtype = ("net", n["id"]), 0
        else:
            if not x.get("dst_ip"):
                continue
            dst, dtype = ip_group(x["dst_ip"], 32, ports or None), (2 if ports else 1)
        rules.append({"desc": f"{PREFIX}[{i:02d}] uitz. {_ex_label(x)}"[:512], "policy": 1, "protocols": proto,
                      "src": ("grp", src), "stype": 1, "dst": dst if dtype == 0 else ("grp", dst), "dtype": dtype,
                      "state": "auto"})
    for pair in sorted(a["allow"]):
        s, d = (int(v) for v in pair.split(">"))
        if s in by_vlan and d in by_vlan:
            rules.append({"desc": f"{PREFIX}{by_vlan[s]['name']} → {by_vlan[d]['name']} toegestaan", "policy": 1,
                          "protocols": [256], "src": ("net", by_vlan[s]["id"]), "stype": 0,
                          "dst": ("net", by_vlan[d]["id"]), "dtype": 0, "state": "auto"})
    # blokkade: per VLAN één regel naar alle andere VLAN's (Omada staat niet toe dat bron en doel
    # hetzelfde netwerk bevatten); alleen nieuwe verbindingen, zodat antwoorden altijd terugkomen
    for n in nets_:
        others = [m["id"] for m in nets_ if m["id"] != n["id"]]
        if others:
            rules.append({"desc": f"{PREFIX}{n['name']} → andere VLAN's dicht (nieuwe verbindingen)", "policy": 0,
                          "protocols": [256], "src": ("net", n["id"]), "stype": 0, "dst": ("nets", others), "dtype": 0,
                          "state": "new"})
    return rules, groups


def _rule_body(r, gid):
    def ids(v):
        kind, val = v
        if kind == "net":
            return [val]
        if kind == "nets":
            return list(val)
        return [gid[val]]
    b = {"description": r["desc"], "status": True, "policy": r["policy"], "protocols": r["protocols"],
         "sourceType": r["stype"], "sourceIds": ids(r["src"]), "destinationType": r["dtype"],
         "destinationIds": ids(r["dst"]), "syslog": False,
         "direction": {"lanToWan": False, "lanToLan": True, "wanInIds": [], "vpnInIds": []}}
    if r["state"] == "new":
        b.update(stateMode=1, states={"stateNew": True, "established": False, "related": False, "invalid": True})
    else:
        b["stateMode"] = 0
    return b


def acl_apply(a, reason="toegepast"):
    """Zet de portal-regels op de gateway. Bestaande (eigen) regels van de gebruiker blijven ongemoeid."""
    sid = A.cfg()["site"]
    base = f"/v1/{{cid}}/sites/{sid}"
    nets_ = OMADA.networks(sid)
    existing = _acl_osg()
    groups_now = _groups()
    # snapshot vooraf
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = os.path.join(SNAP_DIR, time.strftime("acl-%Y%m%d-%H%M%S.json"))
    json.dump({"acls": existing, "groups": [g for g in groups_now if str(g.get("name", "")).startswith(PREFIX)],
               "config": {"enabled": a["enabled"], "allow": a["allow"], "exceptions": a["exceptions"]}},
              open(snap, "w"), indent=1)
    for f in sorted(os.listdir(SNAP_DIR))[:-30]:
        os.remove(os.path.join(SNAP_DIR, f))

    mine = [x for x in existing if str(x.get("description", "")).startswith(PREFIX)]
    want_rules, want_groups = desired_rules(a, nets_) if a["enabled"] else ([], {})
    # 1. oude portal-regels weg (eerst: zo is er nooit een moment met een blokkade zonder uitzonderingen)
    for x in mine:
        OMADA.call("DELETE", f"{base}/acls/{x['id']}")
    # 2. groepen: ontbrekende maken, gewijzigde bijwerken
    gid = {}
    have = {g["name"]: g for g in groups_now if str(g.get("name", "")).startswith(PREFIX)}
    for name, g in want_groups.items():
        if name in have:
            gid[name] = have[name]["groupId"]
        else:
            r = OMADA.call("POST", f"{base}/profiles/groups", json=g)
            gid[name] = (r or {}).get("id")
    if any(v is None for v in gid.values()):
        fresh = {g["name"]: g["groupId"] for g in _groups()}
        gid = {k: v or fresh.get(k) for k, v in gid.items()}
    # 3. nieuwe regels in volgorde aanmaken (uitzonderingen, toegestaan, blokkade als laatste)
    for r in want_rules:
        OMADA.call("POST", f"{base}/acls/osg-acls", json=_rule_body(r, gid))
    # 4. volgorde: regels van de gebruiker eerst (ongewijzigd), dan de portal-regels in onze volgorde
    if want_rules:
        after = _acl_osg()
        user = sorted((x for x in after if not str(x.get("description", "")).startswith(PREFIX)), key=lambda x: x["index"])
        pos = {r["desc"]: i for i, r in enumerate(want_rules)}
        portal = sorted((x for x in after if x.get("description") in pos), key=lambda x: pos[x["description"]])
        order = {x["id"]: i + 1 for i, x in enumerate(user + portal)}
        OMADA.call("POST", f"{base}/acls/modifyIndex", json={"type": "gateway", "indexes": order})
    # 5. ongebruikte portal-groepen opruimen
    for name, g in have.items():
        if name not in want_groups:
            try:
                OMADA.call("DELETE", f"{base}/profiles/groups/{g['type']}/{g['groupId']}")
            except Exception:  # noqa: BLE001
                pass
    a["versions"] = ([{"ts": int(time.time()), "enabled": a["enabled"], "allow": list(a["allow"]),
                       "exceptions": json.loads(json.dumps(a["exceptions"])), "reason": reason}] + a["versions"])[:20]
    a["applied"] = int(time.time())
    _save_acl(a)
    db.log(None, "acl", f"ACL {reason}: {len(want_rules)} portal-regel(s) op de gateway", "portal")
    return len(want_rules)


def _acl_deadman():
    """Na toepassen moet de pagina binnen 3 minuten 'bevestigen'; anders terug naar de vorige versie
    (voor als je jezelf hebt buitengesloten)."""
    a = _load_acl()
    if not a.get("confirm_by") or time.time() < a["confirm_by"]:
        return
    prev = a.get("rollback") or {"enabled": False, "allow": [], "exceptions": []}
    a.update(enabled=prev["enabled"], allow=prev["allow"], exceptions=prev["exceptions"])
    a.pop("confirm_by", None)
    a.pop("rollback", None)
    _save_acl(a)
    acl_apply(a, "automatisch teruggezet (niet bevestigd binnen 3 minuten)")


def _client_net(ip, nets_):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return next((n for n in nets_ if n.get("subnet") and addr in ipaddress.ip_interface(n["subnet"]).network), None)


def _ensure_fixed_ip(sid, mac, nets_):
    """Zorg dat het apparaat een vast IP heeft (nodig: de gateway-ACL werkt op IP-adressen)."""
    if is_random_mac(mac):
        raise OmadaError("Dit apparaat gebruikt een privé-MAC en krijgt nooit een vast IP; kies een ander apparaat "
                         "of zet het privé-adres uit")
    info = OMADA.client_info(sid, mac) or {}
    ips = info.get("ipSetting") or {}
    cur = info.get("ip") or (db.device(mac) or {}).get("ip")
    if ips.get("useFixedAddr") and ips.get("ip"):
        return ips["ip"], "al vast"
    if not cur:
        raise OmadaError("Het apparaat heeft nu geen IP-adres; verbind het eerst")
    net = _client_net(cur, nets_)
    if not net:
        raise OmadaError(f"{cur} valt in geen enkel netwerk")
    if not net.get("dhcp"):
        return cur, f"let op: {net['name']} heeft geen DHCP in Omada; zorg zelf dat {cur} vast blijft"
    server = ips.get("serverMac") or OMADA.gateway_mac(sid)
    OMADA.set_fixed_ip(sid, mac, True, net["id"], cur, server)
    db.log(mac, "vast-ip", f"Vast IP {cur} ingesteld voor een ACL-uitzondering", "portal")
    return cur, "vast gezet"


@app.route("/acl", methods=["GET", "POST"])
@A.login_required
@A.configured
def acl_page():
    a = _load_acl()
    sid = A.cfg()["site"]
    if request.method == "POST":
        act = request.form.get("action")
        try:
            nets_ = OMADA.networks(sid)
            if act == "matrix":
                vl = {n["vlan"] for n in nets_}
                a["allow"] = sorted(p for p in request.form.getlist("allow")
                                    if ">" in p and all(int(v) in vl for v in p.split(">")) and p.split(">")[0] != p.split(">")[1])
                _save_acl(a)
                flash("Matrix opgeslagen (nog niet toegepast)", "ok")
            elif act == "add_ex":
                src = norm_mac(request.form["src"])
                kind = request.form.get("dst_kind", "vlan")
                proto = request.form.get("proto", "all")
                ports = request.form.get("ports", "").replace(" ", "")
                if ports:
                    for p in ports.split(","):
                        lo, _, hi = p.partition("-")
                        if not (lo.isdigit() and 0 < int(lo) < 65536 and (not hi or (hi.isdigit() and int(lo) <= int(hi) < 65536))):
                            raise OmadaError(f"Ongeldige poort '{p}' (gebruik bv. 443,1883 of 8000-8010)")
                    if proto not in ("tcp", "udp", "tcpudp"):
                        raise OmadaError("Kies bij poorten TCP, UDP of TCP+UDP")
                inv = db.devices()
                notes = []
                src_ip, how = _ensure_fixed_ip(sid, src, nets_)
                notes.append(f"bron {src_ip} ({how})")
                x = {"id": int(time.time() * 1000), "src": src, "src_ip": src_ip,
                     "src_name": (inv.get(src) or {}).get("name") or src, "dst_kind": kind, "proto": proto,
                     "ports": ports, "note": request.form.get("note", "")[:200]}
                if kind == "vlan":
                    v = int(request.form["dst_vlan"])
                    n = next(n for n in nets_ if n["vlan"] == v)
                    x.update(dst=v, dst_name=n["name"])
                else:
                    dst = norm_mac(request.form["dst_dev"])
                    if dst == src:
                        raise OmadaError("Bron en doel zijn hetzelfde apparaat")
                    dst_ip, how2 = _ensure_fixed_ip(sid, dst, nets_)
                    notes.append(f"doel {dst_ip} ({how2})")
                    x.update(dst=dst, dst_ip=dst_ip, dst_name=(inv.get(dst) or {}).get("name") or dst)
                a["exceptions"].append(x)
                _save_acl(a)
                flash(f"Uitzondering toegevoegd: {_ex_label(x)} — {'; '.join(notes)}. Nog niet toegepast.", "ok")
            elif act == "del_ex":
                xid = int(request.form["id"])
                a["exceptions"] = [x for x in a["exceptions"] if x["id"] != xid]
                _save_acl(a)
                flash("Uitzondering verwijderd (nog niet toegepast)", "ok")
            elif act in ("apply", "disable", "restore"):
                prev = {"enabled": (a["versions"][0]["enabled"] if a["versions"] else False),
                        "allow": (a["versions"][0]["allow"] if a["versions"] else []),
                        "exceptions": (a["versions"][0]["exceptions"] if a["versions"] else [])}
                if act == "disable":
                    a["enabled"] = False
                elif act == "restore":
                    v = a["versions"][int(request.form["v"])]
                    a.update(enabled=v["enabled"], allow=v["allow"], exceptions=v["exceptions"])
                else:
                    a["enabled"] = True
                    me = _client_net(request.remote_addr or "", nets_)
                    portal_net = _client_net(request.host.split(":")[0], nets_) or next((n for n in nets_ if n["vlan"] == 1), None)
                    if (me and portal_net and me["vlan"] != portal_net["vlan"]
                            and f"{me['vlan']}>{portal_net['vlan']}" not in a["allow"]
                            and request.form.get("force") != "1"):
                        raise OmadaError(f"Je zit zelf in {me['name']} en de portal staat in {portal_net['name']}. Met deze "
                                         f"instellingen kun je de portal daarna niet meer bereiken. Zet {me['name']} → "
                                         f"{portal_net['name']} open, of vink 'toch toepassen' aan.")
                a["rollback"] = prev
                n = acl_apply(a, {"apply": "toegepast", "disable": "uitgezet", "restore": "versie teruggezet"}[act])
                a = _load_acl()
                a["confirm_by"] = time.time() + 180
                a["rollback"] = prev
                _save_acl(a)
                flash(f"Toegepast: {n} portal-regel(s) op de gateway. Deze pagina bevestigt automatisch; lukt dat niet "
                      "binnen 3 minuten, dan zet de portal de vorige instellingen terug.", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"Mislukt: {e}", "err")
        return redirect(url_for("acl_page"))

    try:
        nets_ = OMADA.networks(sid)
        existing = _acl_osg()
        cl = clients()
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)
    inv = db.devices()
    user_rules = [x for x in existing if not str(x.get("description", "")).startswith(PREFIX)]
    portal_rules = [x for x in existing if str(x.get("description", "")).startswith(PREFIX)]
    want, _ = desired_rules({**a, "enabled": True}, nets_)
    act = {norm_mac(x["mac"]): x for x in cl}
    devs = []
    for mac, d in inv.items():
        x = act.get(mac, {})
        ip = x.get("ip") or d.get("ip")
        if not ip:
            continue
        devs.append({"mac": mac, "name": dev_name(mac, x, inv), "ip": ip, "online": mac in act,
                     "vlan": d.get("network") or "", "random": is_random_mac(mac)})
    devs.sort(key=lambda r: (r["vlan"], r["name"].lower()))
    me = _client_net(request.remote_addr or "", nets_)
    pending = sorted(a["allow"]) != sorted((a["versions"][0]["allow"] if a["versions"] else [])) or \
        a["exceptions"] != (a["versions"][0]["exceptions"] if a["versions"] else []) or \
        a["enabled"] != (a["versions"][0]["enabled"] if a["versions"] else False)
    return render_template("acl.html", a=a, nets=nets_, user_rules=user_rules, portal_rules=portal_rules, want=want,
                           devs=devs, inv=inv, ex_label=_ex_label, me=me, pending=pending, proto=PROTO,
                           confirm_left=int(a["confirm_by"] - time.time()) if a.get("confirm_by") else 0)


@app.post("/acl/confirm")
@A.login_required
def acl_confirm():
    a = _load_acl()
    if a.get("confirm_by"):
        a.pop("confirm_by", None)
        a.pop("rollback", None)
        _save_acl(a)
        db.log(None, "acl", "ACL-wijziging bevestigd (portal nog bereikbaar)", "portal")
    return {"ok": True}


# =============================================================================
if os.environ.get("PORTAL_NO_SYNC") != "1":
    threading.Thread(target=_monitor_loop, daemon=True, name="monitor").start()


import dashboard  # noqa: E402,F401  - startpagina
import aplock  # noqa: E402,F401  - AP vastzetten + AP-scan
