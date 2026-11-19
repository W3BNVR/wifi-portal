"""Wifi-portal: PPSK-sleutels per apparaat beheren via de Omada Open API.

Nieuwe sleutel -> landt in het gast-VLAN zonder MAC-binding -> apparaat verbindt ->
jij koppelt het MAC-adres en kiest het echte VLAN -> apparaat verbindt opnieuw.
Alle keuzes (site, PPSK-profiel, VLAN's, SSID's) komen uit Omada zelf.
"""
import io
import json
import os
import re
import secrets
import string
import time
from functools import wraps

import qrcode
import qrcode.image.svg
from flask import (Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for)

import db
from markupsafe import Markup

from omada import Omada, OmadaError, OmadaWeb, is_random_mac, norm_mac

DATA = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA, exist_ok=True)
CFG_FILE = os.path.join(DATA, "config.json")
STATE_FILE = os.path.join(DATA, "state.json")
SECRET_FILE = os.path.join(DATA, "secret.key")

app = Flask(__name__)
if not os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
app.secret_key = open(SECRET_FILE).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)

PORTAL_PASSWORD = os.environ["PORTAL_PASSWORD"]
OMADA = Omada(os.environ.get("OMADA_URL", "https://10.0.0.2:8043"),
              os.environ["OMADA_CLIENT_ID"], os.environ["OMADA_CLIENT_SECRET"])
# Optioneel: web-login, alleen nodig om type/merk/model in Omada zelf te zetten
WEB = (OmadaWeb(OMADA.url, os.environ["OMADA_WEB_USER"], os.environ["OMADA_WEB_PASS"])
       if os.environ.get("OMADA_WEB_USER") and os.environ.get("OMADA_WEB_PASS") else None)
db.init(os.path.join(DATA, "portal.db"))


# -- opslag -----------------------------------------------------------------
def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def cfg():
    return _load(CFG_FILE, {})


def state():
    """Sleutels die door de portal zijn aangemaakt: {naam: {created: ts}}."""
    return _load(STATE_FILE, {"keys": {}})


# -- helpers ----------------------------------------------------------------
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # zonder 0/o, 1/l/i


def new_psk(existing):
    while True:
        psk = "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(4))
        if psk not in existing:
            return psk


def wifi_qr(ssid, psk):
    def esc(s):
        return "".join("\\" + c if c in '\\;,:"' else c for c in s)
    img = qrcode.make(f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(psk)};;",
                      image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Markup(buf.getvalue().decode())


def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
            flash("Sessie verlopen, probeer opnieuw.", "err")
            return redirect(url_for("index"))
        return f(*a, **kw)
    return wrap


def configured(f):
    @wraps(f)
    def wrap(*a, **kw):
        c = cfg()
        if not all(k in c for k in ("site", "profile", "guest_vlan")):
            return redirect(url_for("setup"))
        return f(*a, **kw)
    return wrap


@app.template_filter("bytes")
def f_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@app.template_filter("dur")
def f_dur(s):
    s = int(s or 0)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h}u" if d else (f"{h}u {m}m" if h else f"{m}m")


@app.template_filter("ago")
def f_ago(ms):
    if not ms:
        return "—"
    return time.strftime("%d-%m-%Y %H:%M", time.localtime(int(ms) / 1000))


@app.context_processor
def inject():
    return {"csrf": session.get("csrf", "")}


@app.before_request
def _count_flashes():
    g.nflash = len(session.get("_flashes", []))


@app.after_request
def _log_actions(resp):
    """Elke actie (POST) met haar melding in het logboek, met het MAC-adres als dat bekend is."""
    if request.method == "POST" and request.endpoint not in ("login", "setup"):
        mac = request.form.get("mac") or (request.view_args or {}).get("mac")
        name = request.form.get("name")
        if not mac and name:
            mac = _key_macs.get(name)
        for cat, msg in session.get("_flashes", [])[getattr(g, "nflash", 0):]:
            try:
                db.log(mac, request.endpoint if cat == "ok" else "fout", msg)
            except Exception:  # noqa: BLE001 - logboek mag een actie nooit breken
                pass
    return resp


_key_macs = {}  # sleutelnaam -> MAC (bijgewerkt bij elk opvragen van het PPSK-profiel)


def context():
    """Haalt alles op wat de pagina's nodig hebben uit Omada."""
    c = cfg()
    sid, pid = c["site"], c["profile"]
    prof = OMADA.ppsk(sid, pid)
    _key_macs.update({e["name"]: e["mac"] for e in prof["entries"] if e["mac"]})
    nets = OMADA.networks(sid)
    vname = {n["vlan"]: n["name"] for n in nets}
    st = state()["keys"]
    return c, sid, pid, prof, nets, vname, st


def psk_name(label, fallback="apparaat"):
    """Omada staat in een PPSK-naam alleen zichtbare ASCII toe (geen spaties, geen é).
    'User - Sophie' -> 'User-Sophie', 'Café TV' -> 'Cafe-TV'."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(label)).encode("ascii", "ignore").decode()
    t = re.sub(r"[^\x21-\x7e]+", "-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return (t or fallback)[:64]


def is_shared(e):
    """Gedeelde sleutel = niet aan een MAC gebonden en niet door de portal aangemaakt (wachtend)."""
    st = state()
    return not e.get("mac") and e["name"] not in st["keys"] and e["name"] not in st.get("guest", {})


def psk_replace(sid, pid, old, new):
    """Enige weg om een sleutel te wijzigen: gedeelde sleutels blijven altijd onaangeroerd."""
    if is_shared(old):
        raise OmadaError(f"'{old['name']}' is een gedeelde sleutel; de portal wijzigt gedeelde sleutels nooit")
    return OMADA.replace_psk(sid, pid, old, new)


def psk_delete(sid, pid, old):
    if is_shared(old):
        raise OmadaError(f"'{old['name']}' is een gedeelde sleutel; gedeelde sleutels worden nooit verwijderd")
    return OMADA.delete_psk(sid, pid, old["name"])


def find(prof, name):
    for e in prof["entries"]:
        if e["name"] == name:
            return e
    abort(404)


# -- login ------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("pw", ""), PORTAL_PASSWORD):
            session.clear()
            session.update(ok=True, csrf=secrets.token_hex(16))
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        time.sleep(1)
        flash("Wachtwoord onjuist", "err")
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -- instellingen -----------------------------------------------------------
@app.route("/setup", methods=["GET", "POST"])
@login_required
def setup():
    c = cfg()
    try:
        sites = OMADA.sites()
        sid = request.values.get("site") or c.get("site") or (sites[0]["id"] if len(sites) == 1 else None)
        profiles = OMADA.ppsk_profiles(sid) if sid else []
        nets = OMADA.networks(sid) if sid else []
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)

    if request.method == "POST" and request.form.get("newtoken"):
        c["api_token"] = secrets.token_urlsafe(24)
        _save(CFG_FILE, c)
        flash("Nieuw API-token gemaakt; vul het ook in bij de Homey-widget", "ok")
        return redirect(url_for("setup"))
    if request.method == "POST" and request.form.get("save"):
        c.update(site=sid, profile=request.form["profile"], guest_vlan=int(request.form["guest_vlan"]))
        for k in ("random_key_hours", "pending_key_hours"):
            try:
                c[k] = max(0.0, float(request.form.get(k, "5").replace(",", ".")))
            except ValueError:
                c[k] = 5.0
        _save(CFG_FILE, c)
        flash("Instellingen opgeslagen", "ok")
        return redirect(url_for("index"))

    guess = next((n["vlan"] for n in nets if n["name"].lower() in ("guest", "gast", "gasten")), None)
    return render_template("setup.html", token=api_token(), sites=sites, sid=sid, profiles=profiles, nets=nets, rk_hours=random_key_hours(), pk_hours=pending_key_hours(),
                           c=c, guess_vlan=c.get("guest_vlan") or guess,
                           guess_profile=c.get("profile") or (profiles[0]["id"] if len(profiles) == 1 else None))


# -- overzicht --------------------------------------------------------------
@app.get("/sleutels")
@login_required
@configured
def index():
    try:
        c, sid, pid, prof, nets, vname, st = context()
        clients = OMADA.clients(sid)
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)

    by_mac = {norm_mac(cl["mac"]): cl for cl in clients}
    bound = {e["mac"] for e in prof["entries"] if e["mac"]}
    pending, devices, shared = [], [], []
    for e in prof["entries"]:
        e["vlan_name"] = vname.get(e["vlan"] or 1, f"VLAN {e['vlan'] or 1}")
        e["vgrp"] = int(e["vlan"] or 1)
        if e["mac"]:
            cl = by_mac.get(e["mac"])
            e["online"] = bool(cl and cl.get("active", True))
            e["ip"] = (cl or {}).get("ip", "")
            e["ssid"] = (cl or {}).get("ssid", "")
            e["random"] = is_random_mac(e["mac"])
            e["expires"] = _expires_at(e["name"], e["mac"])
            devices.append(e)
        elif e["name"] in st:
            e["created"] = st[e["name"]].get("created", 0)
            pending.append(e)
        elif e["name"] in state().get("guest", {}):
            pass  # gastsleutel: eigen kaart (extras)
        else:
            shared.append(e)

    # Kandidaten: verbonden via dit PPSK-profiel, in het gast-VLAN, nog niet gekoppeld.
    # Omada meldt niet wélke sleutel een apparaat gebruikt; per wachtende sleutel tonen we daarom
    # de apparaten die ná het aanmaken van die sleutel verbonden (nieuwste eerst), de rest apart.
    now = time.time()
    cands = []
    for cl in clients:
        mac = norm_mac(cl["mac"])
        if (cl.get("wireless") and cl.get("ppskProfileName") == prof["name"]
                and int(cl.get("vid") or 0) == c["guest_vlan"] and mac not in bound):
            cands.append({"mac": mac, "name": cl.get("name") or cl.get("hostName") or "",
                          "host": cl.get("hostName", ""), "ip": cl.get("ip", ""),
                          "ssid": cl.get("ssid", ""), "uptime": cl.get("uptime", 0),
                          "since": now - (cl.get("uptime") or 0), "random": is_random_mac(mac)})
    cands.sort(key=lambda x: x["uptime"])
    for e in pending:
        e["label"] = st[e["name"]].get("label") or ""
        e["expires"] = _pending_expires(st[e["name"]])
        e["after"] = [x for x in cands if x["since"] >= e["created"] - 60]
        e["before"] = [x for x in cands if x["since"] < e["created"] - 60]

    pending.sort(key=lambda e: -e["created"])
    devices.sort(key=lambda e: e["name"].lower())
    return render_template("index.html", prof=prof, nets=nets, pending=pending, devices=devices,
                           shared=shared, cands=cands, guest=vname.get(c["guest_vlan"], c["guest_vlan"]),
                           guest_vlan=c["guest_vlan"], phours=pending_key_hours())


# -- API voor de Homey-widget ----------------------------------------------
def api_token():
    c = cfg()
    if not c.get("api_token"):
        c["api_token"] = secrets.token_urlsafe(24)
        _save(CFG_FILE, c)
    return c["api_token"]


def api_auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        got = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not got or not secrets.compare_digest(got, api_token()):
            return {"error": "Ongeldig API-token"}, 401
        if not all(k in cfg() for k in ("site", "profile", "guest_vlan")):
            return {"error": "Portal is nog niet ingesteld"}, 503
        return f(*a, **kw)
    return wrap


@app.post("/api/v1/new")
@api_auth
def api_new():
    j = request.get_json(silent=True) or {}
    label = str(j.get("name", "")).strip()[:64]
    if not label:
        return {"error": "Geef het apparaat een naam"}, 400
    try:
        name, psk, ssids = _create_pending(label)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}, 409 if isinstance(e, OmadaError) else 502
    ssid = j.get("ssid") or (ssids[0] if ssids else "")
    if ssid not in ssids:
        ssid = ssids[0] if ssids else ssid
    c = cfg()
    vname = {n["vlan"]: n["name"] for n in OMADA.networks(c["site"])}
    db.log(None, "api", f"Sleutel '{name}' aangemaakt via Homey", "portal")
    return {"name": name, "label": label, "psk": psk, "ssid": ssid, "qr": str(wifi_qr(ssid, psk)),
            "hours": pending_key_hours(),
            "vlan": vname.get(c["guest_vlan"], c["guest_vlan"])}


@app.get("/api/v1/key/<path:name>")
@api_auth
def api_key(name):
    c = cfg()
    e = next((x for x in OMADA.ppsk(c["site"], c["profile"])["entries"] if x["name"] == name), None)
    if not e:
        return {"exists": False, "mac": ""}
    out = {"exists": True, "mac": e["mac"] or "", "random": bool(e["mac"]) and is_random_mac(e["mac"])}
    if e["mac"]:
        cl = next((x for x in OMADA.clients(c["site"]) if norm_mac(x["mac"]) == e["mac"]), {})
        out.update(ip=cl.get("ip", ""), host=cl.get("hostName", ""))
    return out


# -- acties -----------------------------------------------------------------
@app.post("/new")
@login_required
@configured
def new_key():
    name = request.form.get("name", "").strip()[:64]
    if not name:
        flash("Geef het apparaat een naam", "err")
        return redirect(url_for("index"))
    try:
        name = _create_pending(name)[0]
    except Exception as e:  # noqa: BLE001
        flash(f"Aanmaken mislukt: {e}", "err")
        return redirect(url_for("index"))
    return redirect(url_for("key", name=name))


def _create_pending(label):
    """Nieuwe, nog niet gebonden sleutel in het gast-VLAN; je kiest daarna zelf welk apparaat hem houdt.
    label = naam zoals ingetypt (wordt later de apparaatnaam), name = geldige PPSK-naam."""
    c, sid, pid, prof, *_ = context()
    name = psk_name(label)
    if any(e["name"] == name for e in prof["entries"]):
        raise OmadaError(f"Er bestaat al een sleutel met de naam '{name}'")
    psk = new_psk({e["psk"] for e in prof["entries"]})
    OMADA.add_psk(sid, pid, {"name": name, "psk": psk, "vlan": c["guest_vlan"]})
    s = state()
    s["keys"][name] = {"created": time.time(), "label": label}
    _save(STATE_FILE, s)
    return name, psk, prof["ssids"]


@app.get("/key/<path:name>")
@login_required
@configured
def key(name):
    try:
        c, sid, pid, prof, nets, vname, st = context()
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)
    e = find(prof, name)
    qrs = [{"ssid": ss, "svg": wifi_qr(ss, e["psk"])} for ss in prof["ssids"]]
    return render_template("key.html", e=e, qrs=qrs, vlan_name=vname.get(e["vlan"] or 1, e["vlan"]), nets=nets,
                           waiting=not e["mac"] and name in st, random=bool(e["mac"]) and is_random_mac(e["mac"]),
                           expires=_expires_at(name, e["mac"]), hours=random_key_hours(),
                           pending_expires=_pending_expires(st.get(name) or {}) if not e["mac"] else None)


@app.get("/keystatus/<path:name>")
@login_required
def key_status(name):
    """Voor de QR-pagina: is de sleutel al aan een apparaat gekoppeld?"""
    c = cfg()
    e = next((x for x in OMADA.ppsk(c["site"], c["profile"])["entries"] if x["name"] == name), None)
    return {"mac": (e or {}).get("mac") or "", "exists": bool(e)}


@app.post("/bind")
@login_required
@configured
def bind():
    """Koppel MAC aan een wachtende sleutel en zet hem in het gekozen VLAN."""
    name, mac, vlan = request.form["name"], request.form["mac"], int(request.form["vlan"])
    try:
        c, sid, pid, prof, nets, vname, st = context()
        old = find(prof, name)
        if old["mac"]:
            raise OmadaError(f"'{name}' is al gekoppeld aan {old['mac']}; een sleutel hoort bij maximaal één apparaat")
        if any(e["mac"] == norm_mac(mac) for e in prof["entries"]):
            raise OmadaError("Dit MAC-adres is al aan een andere sleutel gekoppeld")
        psk_replace(sid, pid, old, {**old, "mac": mac, "vlan": vlan})
        try:
            label = (st.get(name) or {}).get("label") or name
            OMADA.set_client_name(sid, mac, label)
            db.remember_name(norm_mac(mac), label)
        except Exception:  # noqa: BLE001 - naam is bijzaak
            pass
        _reconnect(sid, mac)
        s = state()
        s["keys"].pop(name, None)
        _save(STATE_FILE, s)
        flash(f"'{name}' gekoppeld aan {norm_mac(mac)} en verplaatst naar {vname.get(vlan, vlan)}", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Koppelen mislukt: {e}", "err")
    return redirect(url_for("index"))


@app.post("/move")
@login_required
@configured
def move():
    name, vlan = request.form["name"], int(request.form["vlan"])
    try:
        c, sid, pid, prof, nets, vname, st = context()
        old = find(prof, name)
        if (old["vlan"] or 1) == vlan:
            return redirect(url_for("index"))
        psk_replace(sid, pid, old, {**old, "vlan": vlan})
        if old["mac"]:
            _reconnect(sid, old["mac"])
        flash(f"'{name}' verplaatst naar {vname.get(vlan, vlan)}", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Verplaatsen mislukt: {e}", "err")
    return redirect(url_for("index"))


@app.post("/rekey")
@login_required
@configured
def rekey():
    """Nieuwe sleutel voor hetzelfde apparaat; de oude werkt daarna niet meer."""
    name = request.form["name"]
    try:
        c, sid, pid, prof, *_ = context()
        old = find(prof, name)
        psk_replace(sid, pid, old, {**old, "psk": new_psk({e["psk"] for e in prof["entries"]})})
        flash(f"Nieuwe sleutel voor '{name}'. Voer hem in op het apparaat.", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Vervangen mislukt: {e}", "err")
        return redirect(url_for("index"))
    return redirect(url_for("key", name=name))


@app.post("/unbind")
@login_required
@configured
def unbind():
    """MAC-binding loslaten en terug naar gast-VLAN (bv. ander apparaat, zelfde sleutel)."""
    name = request.form["name"]
    try:
        c, sid, pid, prof, *_ = context()
        old = find(prof, name)
        psk_replace(sid, pid, old, {**old, "mac": "", "vlan": c["guest_vlan"]})
        if old["mac"]:
            _reconnect(sid, old["mac"])
        s = state()
        s["keys"][name] = {"created": time.time()}
        _save(STATE_FILE, s)
        flash(f"'{name}' ontkoppeld en terug in het gast-VLAN", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Ontkoppelen mislukt: {e}", "err")
    return redirect(url_for("index"))


def _shared_users(prof, e, clients):
    """Apparaten die (vrijwel zeker) deze gedeelde sleutel gebruiken: draadloos via dit PPSK-profiel,
    in het VLAN van de sleutel en zonder eigen sleutel. Omada meldt niet welke sleutel precies."""
    vlan = e["vlan"] or 1
    bound = {x["mac"] for x in prof["entries"] if x["mac"]}
    out = []
    for cl in clients:
        mac = norm_mac(cl["mac"])
        if (cl.get("wireless") and cl.get("ppskProfileName") == prof["name"]
                and int(cl.get("vid") or 1) == vlan and mac not in bound):
            out.append({**cl, "mac": mac, "random": is_random_mac(mac)})
    out.sort(key=lambda x: (x.get("name") or x.get("hostName") or "~").lower())
    return out


def _shared_for(prof, vid):
    """De gedeelde sleutel(s) die een apparaat in dit VLAN kan gebruiken."""
    return [x for x in prof["entries"] if is_shared(x) and (x["vlan"] or 1) == (vid or 1)]


@app.get("/shared/<path:name>")
@login_required
@configured
def shared(name):
    """Gedeelde sleutel bekijken (alleen lezen): wie gebruikt hem, en per apparaat een eigen sleutel maken."""
    try:
        c, sid, pid, prof, nets, vname, st = context()
        clients = OMADA.clients(sid)
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)
    e = find(prof, name)
    if not is_shared(e):
        return redirect(url_for("key", name=name))
    vlan = e["vlan"] or 1
    others = [x["name"] for x in _shared_for(prof, vlan) if x["name"] != name]
    return render_template("shared.html", e=e, users=_shared_users(prof, e, clients), others=others,
                           prof_name=prof["name"], vlan_name=f"{vname.get(vlan, vlan)} (VLAN {vlan})",
                           hours=random_key_hours())


def _own_key(sid, pid, prof, mac, vlan, info):
    """Nieuwe privé-sleutel, direct gebonden aan dit MAC-adres. Raakt geen bestaande sleutel aan."""
    if any(x["mac"] == mac for x in prof["entries"]):
        raise OmadaError("Dit apparaat heeft al een eigen sleutel")
    name = psk_name(info.get("name") or info.get("hostName") or mac, mac)
    if any(x["name"] == name for x in prof["entries"]):
        name = f"{name[:58]}-{mac[-5:]}"
    psk = new_psk({x["psk"] for x in prof["entries"]})
    OMADA.add_psk(sid, pid, {"name": name, "psk": psk, "mac": mac, "vlan": vlan})
    _key_macs[name] = mac
    return name


@app.post("/ownkey")
@login_required
@configured
def own_key():
    """Apparaat op een gedeelde sleutel krijgt een eigen privé-sleutel in hetzelfde VLAN."""
    mac = norm_mac(request.form["mac"])
    back = request.form.get("back", "")
    back = back if back.startswith("/") and not back.startswith("//") else url_for("client", mac=mac)
    try:
        c, sid, pid, prof, nets, vname, st = context()
        cl = next((x for x in OMADA.clients(sid) if norm_mac(x["mac"]) == mac), None)
        if not cl or not cl.get("wireless") or cl.get("ppskProfileName") != prof["name"]:
            raise OmadaError("Het apparaat moet nu draadloos verbonden zijn via een PPSK-netwerk "
                             f"({', '.join(prof['ssids'])})")
        vlan = int(cl.get("vid") or 1)
        name = _own_key(sid, pid, prof, mac, vlan, cl)
        flash(f"Eigen sleutel '{name}' aangemaakt in {vname.get(vlan, vlan)} en gebonden aan {mac}. "
              "Voer deze sleutel in op het apparaat; de gedeelde sleutel blijft ongewijzigd.", "ok")
        return redirect(url_for("key", name=name))
    except Exception as e:  # noqa: BLE001
        flash(f"Eigen sleutel maken mislukt: {e}", "err")
        return redirect(back)


@app.post("/delete")
@login_required
@configured
def delete():
    name = request.form["name"]
    try:
        c, sid, pid, prof, nets, vname, st = context()
        old = find(prof, name)
        psk_delete(sid, pid, old)
        if old["mac"]:
            _reconnect(sid, old["mac"])
        s = state()
        s["keys"].pop(name, None)
        _save(STATE_FILE, s)
        flash(f"Sleutel '{name}' verwijderd", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Verwijderen mislukt: {e}", "err")
    back = request.form.get("back", "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("index"))


# -- alle apparaten (ook bestaande / bekabelde / offline) ---------------------
OVR_FILE = os.path.join(DATA, "overrides.json")
ICONS = {  # eigen icoonset (Omada's iconen zijn niet via de API beschikbaar)
    "Telefoon": "📱", "Tablet": "📱", "Laptop": "💻", "Computer": "🖥️", "Desktop": "🖥️", "Server/NAS": "🗄️",
    "TV": "📺", "Mediaspeler": "📺", "Speaker": "🔊", "Camera": "📷", "Deurbel": "🔔", "Printer": "🖨️",
    "Stekker": "🔌", "Lamp": "💡", "Thermostaat": "🌡️", "Energie": "⚡", "Laadpaal": "🔋", "Huishoudelijk": "🍳",
    "Wasmachine": "🧺", "Router/AP": "📡", "Smart home hub": "🏠", "Spelcomputer": "🎮", "Horloge": "⌚",
    "Fotolijst": "🖼️", "Auto": "🚗", "Overig": "🔹",
}
AUTO_ICON = [  # (zoekterm in categorie/type/naam, icoon)
    ("printer", "🖨️"), ("ipc", "📷"), ("camera", "📷"), ("deurbel", "🔔"), ("doorbell", "🔔"), ("tv", "📺"),
    ("shield", "📺"), ("media", "📺"), ("mobile", "📱"), ("phone", "📱"), ("android", "📱"), ("iphone", "📱"),
    ("tablet", "📱"), ("laptop", "💻"), ("desktop", "🖥️"), ("computer", "🖥️"), ("nas", "🗄️"), ("plug", "🔌"),
    ("light", "💡"), ("hue", "💡"), ("power", "⚡"), ("energy", "⚡"), ("zaptec", "🔋"), ("watch", "⌚"),
    ("fotolijst", "🖼️"), ("router", "📡"), ("smart appliance", "🍳"), ("vloerverwarming", "🌡️"),
    ("engineering", "🌡️"), ("homey", "🏠"), ("smart home", "🏠"),
]
PORT_RISKY = ("ap", "eap", "switch", "uplink", "poe", "lag", "trunk", "router", "gateway")


def overrides():
    return _load(OVR_FILE, {})


def icon_for(c, ov):
    if ov.get("icon"):
        return ov["icon"]
    hay = " ".join(str(c.get(k) or "") for k in ("deviceCategory", "deviceType", "name", "hostName")).lower()
    hay += " " + str(ov.get("type") or "").lower()
    for term, ic in AUTO_ICON:
        if term in hay:
            return ic
    return "🔹"


def _known(v):
    return v if v and str(v).lower() not in ("unknown", "others") else ""


def describe(c, ov):
    """Type · merk · model, met eigen aanpassingen voorrang boven Omada's herkenning."""
    t = ov.get("type") or _known(c.get("deviceType")) or _known(c.get("deviceCategory"))
    v = ov.get("vendor") or _known(c.get("vendor"))
    m = ov.get("model") or _known(c.get("model"))
    return " · ".join(x for x in (t, v, m) if x)


def _conn(cl):
    if cl.get("wireless"):
        return cl.get("ssid") or "wifi"
    sw = cl.get("switchName") or cl.get("gatewayName") or ""
    return f"{sw} poort {cl['port']}" if cl.get("port") else (sw or "kabel")


def port_check(cl, active, ports):
    """Mag de VLAN van de switchpoort van dit bekabelde apparaat veilig gewijzigd worden?
    Geeft (ok, reden, port_info) terug."""
    ctrl_host = re.sub(r"^https?://|[:/].*$", "", OMADA.url)
    if cl.get("ip") and cl.get("ip") == ctrl_host:
        return False, "Dit is de server waar Omada en deze portal op draaien", None
    if not cl.get("switchMac") or not cl.get("port"):
        return False, "Zit niet direct op een Omada-switch (bv. gateway of onbekende switch)", None
    p = ports.get((norm_mac(cl["switchMac"]), int(cl["port"])))
    if not p:
        return False, "Poortinformatie niet beschikbaar", None
    others = [x for x in active if not x.get("wireless") and x.get("switchMac") == cl["switchMac"]
              and str(x.get("port")) == str(cl["port"]) and norm_mac(x["mac"]) != norm_mac(cl["mac"])]
    if others:
        return False, f"Poort {cl['port']} wordt gedeeld met {len(others)} ander(e) apparaat/apparaten", p
    if p.get("downlink"):
        return False, f"Op poort {cl['port']} zit ook {p['downlink']}", p
    if p.get("lagPort"):
        return False, f"Poort {cl['port']} is een LAG-poort", p
    name = (p.get("name") or "").lower()
    if any(w in name.replace("-", " ").split() or name.startswith(w) for w in PORT_RISKY):
        return False, f"Poort {cl['port']} heet '{p.get('name')}' en lijkt naar een AP/switch te gaan", p
    return True, "", p


def load_ports(sid, active):
    ports = {}
    for swmac in {norm_mac(x["switchMac"]) for x in active if not x.get("wireless") and x.get("switchMac")}:
        try:
            for p in OMADA.switch(sid, swmac).get("portList") or []:
                ports[(swmac, int(p["port"]))] = p
        except Exception:  # noqa: BLE001
            pass
        if WEB:  # poorten waar een AP/switch op hangt (downlinks) altijd blokkeren
            try:
                for d in WEB.switch(sid, swmac).get("downlinkList") or []:
                    key = (swmac, int(d["port"]))
                    if key in ports:
                        ports[key]["downlink"] = f"{d.get('name') or d.get('model')} ({d.get('type')})"
            except Exception:  # noqa: BLE001
                pass
    return ports


def client_rows():
    """Alle apparaten (portal-database + Omada bekend + actief) als platte lijst, één rij per apparaat."""
    c, sid, pid, prof, nets, vname, st = context()
    active_list = OMADA.clients(sid)
    known = OMADA.known_clients(sid)
    active = {norm_mac(x["mac"]): x for x in active_list}
    keys = {e["mac"]: e for e in prof["entries"] if e["mac"]}
    ov_all = overrides()
    try:
        _restore(sid, db.sync(active_list, known, vname))
    except Exception:  # noqa: BLE001
        pass
    inv = db.devices()
    rows = {}
    for mac, d in inv.items():  # eerst wat de portal onthoudt (ook wat Omada vergeten is)
        rows[mac] = {"mac": mac, "active": False, "name": d.get("name") or "", "hostName": d.get("hostname"),
                     "vid": d.get("vlan"), "wireless": bool(d.get("wireless")), "ip": d.get("ip"),
                     "deviceType": d.get("dtype"), "vendor": d.get("vendor"), "model": d.get("model"),
                     "deviceCategory": d.get("category"), "lastSeen": (d.get("last_seen") or 0) * 1000,
                     "only_db": True, "conn_db": d.get("conn") or ""}
    for k in known:
        mac = norm_mac(k["mac"])
        base = rows.get(mac, {})
        rows[mac] = {**base, **{x: y for x, y in k.items() if y not in (None, "")}, "mac": mac, "active": False,
                     "only_db": False,
                     "name": (k.get("name") if k.get("name") != k["mac"] else "") or base.get("name", "")}
    for mac, a in active.items():
        rows[mac] = {**rows.get(mac, {}), **a, "mac": mac, "active": True, "only_db": False}
        if not rows[mac].get("name") or rows[mac]["name"] == a["mac"]:
            rows[mac]["name"] = (inv.get(mac) or {}).get("name") or ""
    out = []
    for r in rows.values():
        ov = ov_all.get(r["mac"], {})
        r["icon"] = icon_for(r, ov)
        r["desc"] = describe(r, ov)
        r["vlan"] = r.get("vid")
        r["vlan_name"] = vname.get(r["vlan"], f"VLAN {r['vlan']}" if r["vlan"] else "")
        r["conn"] = _conn(r) if r["active"] else (r.get("conn_db") or "")
        r["random"] = is_random_mac(r["mac"])
        r["key"] = keys.get(r["mac"])
        r["label"] = r.get("name") or r.get("hostName") or ""
        r["ap"] = (r.get("apName") or "") if r["active"] and r.get("wireless") else ""
        r["rssi"] = r.get("rssi") if r["active"] and r.get("wireless") else None
        r["seen"] = int(time.time() * 1000) if r["active"] else (int(r.get("lastSeen") or 0) if (r.get("lastSeen") or 0) > 1e12 else 0)
        r["search"] = " ".join(str(x or "") for x in (r["label"], r["mac"], r.get("ip"), r["vlan_name"], r["conn"],
                                                      r["desc"], r.get("hostName"), r["ap"])).lower()
        out.append(r)
    out.sort(key=lambda r: (not r["active"], (r["label"] or "~" + r["mac"]).lower()))
    return out


def _filter_rows(rows, args):
    q = (args.get("q") or "").lower().strip()
    t = args.get("t") or ""
    on = args.get("online") == "1"
    return [r for r in rows if (not q or q in r["search"]) and (not on or r["active"])
            and (t != "w" or r.get("wireless")) and (t != "b" or not r.get("wireless"))]


EXPORT_FIELDS = ("name", "mac", "ip", "online", "connection", "vlan", "vlan_name", "conn", "ap", "rssi",
                 "type", "own_key", "random_mac", "last_seen")


def _export_rec(r):
    return {"name": r["label"], "mac": r["mac"], "ip": r.get("ip") or "", "online": bool(r["active"]),
            "connection": "wifi" if r.get("wireless") else "kabel", "vlan": r["vlan"] or "",
            "vlan_name": r["vlan_name"], "conn": r["conn"], "ap": r["ap"],
            "rssi": r["rssi"] if r["rssi"] is not None else "", "type": r["desc"],
            "own_key": (r["key"] or {}).get("name", ""), "random_mac": bool(r["random"]),
            "last_seen": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["seen"] / 1000)) if r["seen"] else ""}


@app.get("/clients")
@login_required
@configured
def all_clients():
    try:
        rows = client_rows()
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)
    return render_template("clients.html", rows=rows, online=sum(r["active"] for r in rows), total=len(rows),
                           nwifi=sum(1 for r in rows if r.get("wireless")))


@app.get("/clients/export.<fmt>")
@login_required
@configured
def clients_export(fmt):
    if fmt not in ("json", "csv", "xml"):
        abort(404)
    recs = [_export_rec(r) for r in _filter_rows(client_rows(), request.args)]
    fn = f"apparaten-{time.strftime('%Y%m%d-%H%M')}.{fmt}"
    hdr = {"Content-Disposition": f"attachment; filename={fn}"}
    if fmt == "json":
        return Response(json.dumps(recs, indent=1, ensure_ascii=False), mimetype="application/json", headers=hdr)
    if fmt == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, delimiter=";")
        w.writeheader()
        w.writerows({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in r.items()} for r in recs)
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8", headers=hdr)
    from xml.sax.saxutils import escape
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f'<devices exported="{time.strftime("%Y-%m-%dT%H:%M:%S")}">']
    for r in recs:
        parts.append("  <device>" + "".join(
            f"<{k}>{escape(str(v).lower() if isinstance(v, bool) else str(v))}</{k}>" for k, v in r.items()) + "</device>")
    parts.append("</devices>")
    return Response("\n".join(parts) + "\n", mimetype="application/xml", headers=hdr)


def _drop_fixed_ip(sid, mac, net):
    """Valt een vast IP buiten het subnet van het nieuwe VLAN, zet het dan uit (anders klopt de
    DHCP-reservering niet meer). Geeft een melding terug of None."""
    import ipaddress
    try:
        ips = (OMADA.client_info(sid, mac) or {}).get("ipSetting") or {}
    except Exception:  # noqa: BLE001 - offline/onbekend
        return None
    if not ips.get("useFixedAddr") or not ips.get("ip") or not net.get("subnet"):
        return None
    try:
        inside = ipaddress.ip_address(ips["ip"]) in ipaddress.ip_interface(net["subnet"]).network
    except ValueError:
        inside = False
    if inside:
        return None
    OMADA.set_fixed_ip(sid, mac, False)
    return f" Vast IP {ips['ip']} viel buiten {net['name']} ({net['subnet']}) en is uitgezet."


@app.post("/clientvlan")
@login_required
@configured
def client_vlan():
    mac, vlan = norm_mac(request.form["mac"]), int(request.form["vlan"])
    back = request.form.get("back") or url_for("all_clients")
    try:
        c, sid, pid, prof, nets, vname, st = context()
        net = next(n for n in nets if n["vlan"] == vlan)
        key = next((e for e in prof["entries"] if e["mac"] == mac), None)
        active_list = OMADA.clients(sid)
        cl = next((x for x in active_list if norm_mac(x["mac"]) == mac), None)
        if key:
            note = ""
            if (key["vlan"] or 1) != vlan:
                psk_replace(sid, pid, key, {**key, "vlan": vlan})
                note = _drop_fixed_ip(sid, mac, net) or ""
                _reconnect(sid, mac)
            flash(f"'{key['name']}' verplaatst naar {net['name']}.{note}", "ok")
        elif cl and not cl.get("wireless"):
            ok, why, p = port_check(cl, active_list, load_ports(sid, active_list))
            if not ok:
                raise OmadaError(why)
            OMADA.set_port_vlan(sid, cl["switchMac"], cl["port"], p["profileId"], net["id"], p.get("name"))
            if WEB:  # controleren of Omada het echt heeft overgenomen (Open API toont het native VLAN niet)
                wp = next((x for x in WEB.switch(sid, cl["switchMac"]).get("ports") or []
                           if int(x.get("port", 0)) == int(cl["port"])), {})
                if wp.get("nativeNetworkId") != net["id"]:
                    raise OmadaError("Omada meldde succes, maar de poort staat nog niet op het nieuwe VLAN")
            note = _drop_fixed_ip(sid, mac, net) or ""
            flash(f"Poort {cl['port']} van {cl.get('switchName')} staat nu op {net['name']}. "
                  "Het apparaat krijgt een nieuw IP zodra het de verbinding vernieuwt "
                  f"(eventueel kabel even los of herstarten).{note}", "ok")
        else:
            name = _own_key(sid, pid, prof, mac, vlan, cl or {})
            note = _drop_fixed_ip(sid, mac, net) or ""
            flash(f"Eigen sleutel aangemaakt in {net['name']}. Voer deze sleutel in op het apparaat.{note}", "ok")
            return redirect(url_for("key", name=name))
    except Exception as e:  # noqa: BLE001
        flash(f"VLAN wijzigen mislukt: {e}", "err")
    return redirect(back)


@app.route("/client/<mac>", methods=["GET", "POST"])
@login_required
@configured
def client(mac):
    mac = norm_mac(mac)
    try:
        c, sid, pid, prof, nets, vname, st = context()
    except Exception as e:  # noqa: BLE001
        return render_template("error.html", error=e)
    try:
        info = OMADA.client_info(sid, mac) or {}
    except Exception:  # noqa: BLE001 - offline/onbekend: beperkte gegevens
        info = next((dict(k, active=False) for k in OMADA.known_clients(sid) if norm_mac(k["mac"]) == mac),
                    {"mac": mac, "active": False})
        if info.get("name") == info.get("mac"):
            info["name"] = ""
    mem = db.device(mac) or {}
    for f, m in (("name", "name"), ("hostName", "hostname"), ("ip", "ip"), ("vid", "vlan"), ("deviceType", "dtype"),
                 ("vendor", "vendor"), ("model", "model"), ("deviceCategory", "category")):
        if not info.get(f) or str(info.get(f)).lower() in ("unknown", "others") or info.get(f) == info.get("mac"):
            if mem.get(m):
                info[f] = mem[m]
    if "wireless" not in info and mem.get("wireless") is not None:
        info["wireless"] = bool(mem["wireless"])
    if not info.get("lastSeen") and mem.get("last_seen"):
        info["lastSeen"] = mem["last_seen"] * 1000
    active_list = []
    try:  # de detail-API mist o.a. ppskProfileName/ssid/vendor; vul aan uit de actuele lijst
        active_list = OMADA.clients(sid)
        live = next((x for x in active_list if norm_mac(x["mac"]) == mac), None)
        if live:
            info = {**live, **{k: v for k, v in info.items() if v not in (None, "")}}
    except Exception:  # noqa: BLE001
        pass
    key = next((e for e in prof["entries"] if e["mac"] == mac), None)
    ips = info.get("ipSetting") or {}
    vid = info.get("vid")
    net = next((n for n in nets if n["vlan"] == vid), None)
    ov_all = overrides()
    ov = ov_all.get(mac, {})

    if request.method == "POST":
        act = request.form.get("action")
        try:
            if act == "rename":
                name = request.form["name"].strip()[:64]
                if not name:
                    raise OmadaError("Naam mag niet leeg zijn")
                OMADA.set_client_name(sid, mac, name)
                db.remember_name(mac, name)
                kname = psk_name(name, key["name"]) if key else ""
                if key and key["name"] != kname:
                    if any(e["name"] == kname for e in prof["entries"]):
                        raise OmadaError(f"Apparaat hernoemd, maar er bestaat al een sleutel met de naam '{kname}'")
                    psk_replace(sid, pid, key, {**key, "name": kname})
                    _key_macs[kname] = mac
                    if key["name"] in state().get("random", {}):  # verlooptimer meeverhuizen
                        s2 = state()
                        s2["random"][kname] = s2["random"].pop(key["name"])
                        _save(STATE_FILE, s2)
                flash(f"Hernoemd naar '{name}'" + (f" (sleutel: '{kname}')" if key and kname != name else ""), "ok")
            elif act == "details":
                new = {k: request.form.get(k, "").strip()[:64] for k in ("type", "vendor", "model", "icon", "note")}
                new["note"] = request.form.get("note", "").strip()[:500]
                where = "in de portal"
                if WEB:
                    types, vendors = OMADA.correction_options()
                    # Omada accepteert alleen waarden uit de eigen lijsten; hoofdletters gelijktrekken
                    for fld, opts, label in (("type", types, "Type"), ("vendor", vendors, "Merk")):
                        if new[fld]:
                            hit = next((o for o in opts if o.lower() == new[fld].lower()), None)
                            if not hit:
                                raise OmadaError(f"{label} '{new[fld]}' staat niet in de lijst van Omada; kies er een uit de suggesties")
                            new[fld] = hit
                    WEB.set_client_info(sid, mac, info.get("name") or info.get("hostName") or mac,
                                        new["type"], new["vendor"], new["model"])
                    db.set_correction(mac, new["type"], new["vendor"], new["model"])
                    for fld in ("type", "vendor", "model"):  # staat nu in Omada zelf
                        new.pop(fld)
                    where = "in Omada"
                new = {k: v for k, v in new.items() if v}
                if new:
                    ov_all[mac] = new
                else:
                    ov_all.pop(mac, None)
                _save(OVR_FILE, ov_all)
                flash(f"Gegevens opgeslagen {where}", "ok")
            elif act == "fixedip":
                enable = request.form.get("fixed") == "1"
                ip = request.form.get("ip", "").strip()
                if enable and is_random_mac(mac):
                    raise OmadaError("Dit apparaat gebruikt een privé (willekeurig) MAC-adres; zo'n apparaat krijgt "
                                     "nooit een vast IP")
                if enable:
                    import ipaddress
                    if not net:
                        raise OmadaError("Onbekend netwerk voor dit apparaat; het moet eerst verbonden zijn")
                    if not net.get("dhcp"):
                        raise OmadaError(f"{net['name']} heeft geen DHCP-server in Omada (de adressen komen van een "
                                         "andere DHCP-server), dus Omada kan hier geen vast IP reserveren")
                    if ipaddress.ip_address(ip) not in ipaddress.ip_interface(net["subnet"]).network:
                        raise OmadaError(f"{ip} valt niet in {net['name']} ({net['subnet']})")
                    server = ips.get("serverMac") or OMADA.gateway_mac(sid)
                    if not server:
                        raise OmadaError("Kon de DHCP-gateway niet vinden")
                    OMADA.set_fixed_ip(sid, mac, True, net["id"], ip, server)
                    flash(f"Vast IP {ip} ingesteld (actief na de volgende DHCP-vernieuwing of herverbinding)", "ok")
                else:
                    OMADA.set_fixed_ip(sid, mac, False)
                    flash("Vast IP uitgezet", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"Mislukt: {e}", "err")
        return redirect(url_for("client", mac=mac))

    # VLAN-wijzigmogelijkheid bepalen (zelfde regels als in de lijst)
    if info.get("wireless"):
        mode = "key" if key else ("ownkey" if info.get("ppskProfileName") == prof["name"] or not info.get("active") else "")
        why = "" if mode else f"VLAN komt van SSID '{info.get('ssid')}' (geen PPSK)"
    elif info.get("active"):
        ok, why, _ = port_check(info, active_list, load_ports(sid, active_list))
        mode = "port" if ok else ""
    else:
        mode, why = "", "Offline: poort onbekend"
    try:
        types, vendors = OMADA.correction_options()
    except Exception:  # noqa: BLE001
        types, vendors = [], []
    return render_template("client.html", info=info, mac=mac, key=key, ips=ips, net=net, nets=nets,
                           vlan=vid, vlan_name=vname.get(vid, vid), conn=_conn(info) if info.get("active") else "",
                           mode=mode, why=why, random=is_random_mac(mac), ov=ov, icon=icon_for(info, ov),
                           desc=describe(info, ov), icons=ICONS, types=types, vendors=vendors, web=bool(WEB),
                           fixed_wrong=_fixed_wrong(ips, net), mem=mem, history=db.events(mac, 50),
                           expires=_expires_at(key["name"], mac) if key else None,
                           on_shared=bool(not key and info.get("wireless") and info.get("active")
                                          and info.get("ppskProfileName") == prof["name"]),
                           shared_keys=[x["name"] for x in _shared_for(prof, vid)], hours=random_key_hours())


def _fixed_wrong(ips, net):
    import ipaddress
    if not ips.get("useFixedAddr") or not ips.get("ip") or not net:
        return False
    try:
        return ipaddress.ip_address(ips["ip"]) not in ipaddress.ip_interface(net["subnet"]).network
    except ValueError:
        return False


_restored = {}  # mac -> tijdstip laatste herstelpoging (niet blijven hameren)


def _restore(sid, lost):
    """Zet namen en type/merk/model die Omada kwijt is terug vanuit de eigen database."""
    now = time.time()
    for mac, row, what in lost:
        if now - _restored.get(mac, 0) < 6 * 3600:
            continue
        _restored[mac] = now
        try:
            if what.get("name") and row.get("name"):
                OMADA.set_client_name(sid, mac, row["name"])
                db.log(mac, "hersteld", f"Naam '{row['name']}' teruggezet in Omada", "portal")
            if what.get("corr") and WEB and row.get("corr_type"):
                WEB.set_client_info(sid, mac, row.get("name") or mac, row.get("corr_type"),
                                    row.get("corr_vendor"), row.get("corr_model"))
                db.log(mac, "hersteld", f"Type/merk/model ({row.get('corr_type')} · {row.get('corr_vendor') or '-'} · "
                       f"{row.get('corr_model') or '-'}) teruggezet in Omada", "portal")
        except Exception as e:  # noqa: BLE001
            db.log(mac, "fout", f"Herstellen mislukt: {e}", "portal")


def _sync_loop():
    while True:
        try:
            c = cfg()
            if c.get("site"):
                sid = c["site"]
                vname = {n["vlan"]: n["name"] for n in OMADA.networks(sid)}
                _restore(sid, db.sync(OMADA.clients(sid), OMADA.known_clients(sid), vname))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(300)


AUTOBIND_WINDOW = 30 * 60  # zo lang na aanmaken wordt een nieuwe sleutel automatisch gekoppeld


def _autobind_once():
    """Koppel een nieuwe (wachtende) sleutel automatisch aan het eerste apparaat dat hem gebruikt,
    zodat hij daarna alleen nog op dat ene apparaat werkt.
    Omada vertelt niet wélke sleutel een client gebruikte; daarom alleen als het eenduidig is:
    precies één wachtende sleutel én precies één nieuw apparaat in het gast-VLAN dat na het
    aanmaken van die sleutel verbond. Anders blijft handmatig koppelen mogelijk."""
    c = cfg()
    if not all(k in c for k in ("site", "profile", "guest_vlan")):
        return
    now = time.time()
    pend = {n: v for n, v in state()["keys"].items() if now - v.get("created", 0) < AUTOBIND_WINDOW}
    if not pend:
        return
    sid, pid = c["site"], c["profile"]
    prof = OMADA.ppsk(sid, pid)
    entries = {e["name"]: e for e in prof["entries"]}
    pend = {n: v for n, v in pend.items() if n in entries and not entries[n]["mac"]}
    if len(pend) != 1:
        return
    name, meta = next(iter(pend.items()))
    bound = {e["mac"] for e in prof["entries"] if e["mac"]}
    age = now - meta["created"]
    cands = [x for x in OMADA.clients(sid)
             if x.get("wireless") and x.get("ppskProfileName") == prof["name"]
             and int(x.get("vid") or 0) == c["guest_vlan"] and norm_mac(x["mac"]) not in bound
             and (x.get("uptime") or 0) <= age + 20]
    if len(cands) != 1:
        return
    mac, old = norm_mac(cands[0]["mac"]), entries[name]
    psk_replace(sid, pid, old, {**old, "mac": mac})
    try:
        label = meta.get("label") or name
        OMADA.set_client_name(sid, mac, label)
        db.remember_name(mac, label)
    except Exception:  # noqa: BLE001 - naam is bijzaak
        pass
    s = state()
    s["keys"].pop(name, None)
    _save(STATE_FILE, s)
    db.log(mac, "auto-gekoppeld", f"Sleutel '{name}' automatisch gekoppeld aan {mac} "
           f"({cands[0].get('hostName') or 'eerste apparaat dat hem gebruikte'})", "portal")


def random_key_hours():
    try:
        return float(cfg().get("random_key_hours", 5))
    except (TypeError, ValueError):
        return 5.0


def _expires_at(name, mac):
    """Tijdstip (epoch) waarop een sleutel op een privé-MAC verloopt, of None."""
    h = random_key_hours()
    meta = state().get("random", {}).get(name)
    if h <= 0 or not mac or not is_random_mac(mac) or not meta or meta.get("mac") != mac:
        return None
    return meta["bound"] + h * 3600


def _expire_random_keys():
    """Sleutels die aan een privé (willekeurig) MAC-adres gebonden zijn, na X uur verwijderen.
    De klok start op het moment dat de portal de binding voor het eerst ziet."""
    c = cfg()
    if not all(k in c for k in ("site", "profile")):
        return
    hours = random_key_hours()
    sid, pid = c["site"], c["profile"]
    prof = OMADA.ppsk(sid, pid)
    now = time.time()
    rk = dict(state().get("random", {}))
    current = {e["name"]: e for e in prof["entries"] if e["mac"] and is_random_mac(e["mac"])}
    changed = False
    for name, e in current.items():
        meta = rk.get(name)
        if not meta or meta.get("mac") != e["mac"]:
            rk[name] = {"mac": e["mac"], "bound": now}
            changed = True
        elif hours > 0 and now - meta["bound"] >= hours * 3600:
            psk_delete(sid, pid, e)
            try:
                OMADA.call("POST", f"/v1/{{cid}}/sites/{sid}/clients/{e['mac']}/disconnect")
            except Exception:  # noqa: BLE001 - offline apparaat
                pass
            db.log(e["mac"], "verlopen", f"Sleutel '{name}' verwijderd: gekoppeld aan een privé-MAC, "
                   f"verlopen na {hours:g} uur", "portal")
            rk.pop(name)
            changed = True
    for name in list(rk):
        if name not in current:
            rk.pop(name)
            changed = True
    if changed:
        s = state()
        s["random"] = rk
        _save(STATE_FILE, s)


def pending_key_hours():
    try:
        return float(cfg().get("pending_key_hours", 5))
    except (TypeError, ValueError):
        return 5.0


def _pending_expires(meta):
    h = pending_key_hours()
    return meta.get("created", 0) + h * 3600 if h > 0 and meta.get("created") else None


def _expire_pending_keys():
    """Sleutels die nog op koppeling wachten (discovery) na X uur verwijderen."""
    c = cfg()
    if not all(k in c for k in ("site", "profile")) or pending_key_hours() <= 0:
        return
    now = time.time()
    st = state()["keys"]
    due = [n for n, m in st.items() if (_pending_expires(m) or now + 1) <= now]
    if not due:
        return
    sid, pid = c["site"], c["profile"]
    entries = {e["name"]: e for e in OMADA.ppsk(sid, pid)["entries"]}
    for name in due:
        e = entries.get(name)
        if e and not e["mac"]:
            psk_delete(sid, pid, e)
            db.log(None, "verlopen", f"Wachtende sleutel '{name}' verwijderd: niet gekoppeld binnen "
                   f"{pending_key_hours():g} uur", "portal")
        s2 = state()
        s2["keys"].pop(name, None)
        _save(STATE_FILE, s2)


def _autobind_loop():
    n = 0
    while True:
        if n % 6 == 0:  # ongeveer elke minuut
            try:
                _expire_random_keys()
                _expire_pending_keys()
            except Exception as e:  # noqa: BLE001
                try:
                    db.log(None, "fout", f"Verlopen sleutels opruimen mislukt: {e}", "portal")
                except Exception:  # noqa: BLE001
                    pass
        n += 1
        time.sleep(10)


if os.environ.get("PORTAL_NO_SYNC") != "1":
    import threading
    threading.Thread(target=_sync_loop, daemon=True, name="sync").start()
    threading.Thread(target=_autobind_loop, daemon=True, name="autobind").start()


@app.get("/log")
@login_required
@configured
def logbook():
    inv = db.devices()
    evs = db.events(limit=500)
    for e in evs:
        d = inv.get(e["mac"] or "", {})
        e["who"] = d.get("name") or d.get("hostname") or e["mac"] or ""
    return render_template("log.html", events=evs, total=len(inv))


@app.get("/export.json")
@login_required
def export():
    return Response(db.export(), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=wifi-portal-export.json"})


def _reconnect(sid, mac):
    try:
        OMADA.reconnect(sid, mac)
    except Exception:  # noqa: BLE001 - offline apparaat kan niet herverbinden
        pass


import extras  # noqa: E402,F401  - waakhond, gasten, pauze, onderhoud, ACL, Homey-API


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
