"""Dashboard (startpagina): tegels met kerncijfers, grafiek van online apparaten, verdeling per VLAN/AP,
waakhond, zwak signaal, recente gebeurtenissen en grootste verbruikers.

De monitor (extras) roept sample() elke 5 minuten aan; die meting vult de grafiek.
Voor de periode vóór de eerste meting vult de grafiek 'draadloos' aan uit de signaalmetingen.
"""
import json
import time

from flask import render_template

import app as A
import db
import extras as X
from omada import norm_mac

app = A.app

with X._sigdb() as _c:
    _c.executescript("""
    CREATE TABLE IF NOT EXISTS stats (ts INTEGER PRIMARY KEY, online INTEGER, wireless INTEGER, wired INTEGER,
                                      vlans TEXT, down INTEGER, up INTEGER);
    """)


def _act(cl):
    return [c for c in cl if c.get("active", True)]


def sample(cl):
    act = _act(cl)
    now = int(time.time())
    w = sum(1 for c in act if c.get("wireless"))
    vl = {}
    for c in act:
        vl[str(c.get("vid") or 0)] = vl.get(str(c.get("vid") or 0), 0) + 1
    down = sum(int(c.get("activity") or 0) for c in act)
    up = sum(int(c.get("uploadActivity") or 0) for c in act)
    with db._lock, X._sigdb() as c:
        c.execute("INSERT OR REPLACE INTO stats VALUES (?,?,?,?,?,?,?)", (now, len(act), w, len(act) - w, json.dumps(vl), down, up))
        c.execute("DELETE FROM stats WHERE ts < ?", (now - 31 * 86400,))


def series(hours):
    """Punten [ts, totaal|None, draadloos, bekabeld|None, down, up] voor de grafiek."""
    since = int(time.time()) - hours * 3600
    step = 300 if hours <= 24 else 1800  # 5 min of 30 min per punt
    with X._sigdb() as c:
        st = [dict(r) for r in c.execute("SELECT * FROM stats WHERE ts > ? ORDER BY ts", (since,))]
        first = st[0]["ts"] if st else int(time.time()) + 1
        sig = c.execute("SELECT ts, COUNT(DISTINCT mac) n FROM signal WHERE ts > ? AND ts < ? GROUP BY ts ORDER BY ts",
                        (since, first)).fetchall()
    buckets = {}
    for ts, n in sig:  # aanvulling uit signaalmetingen (alleen draadloos bekend)
        b = buckets.setdefault(ts // step * step, {"n": 0, "w": 0})
        b["w"] = max(b["w"], n)
    for r in st:
        b = buckets.setdefault(r["ts"] // step * step, {"n": 0})
        b["n"] += 1
        for k in ("online", "wireless", "wired", "down", "up"):
            b[k] = max(b.get(k) or 0, r[k] or 0) if k in ("online", "wireless", "wired") else (b.get(k) or 0) + (r[k] or 0)
    pts = []
    for ts in sorted(buckets):
        b = buckets[ts]
        if "online" in b:
            pts.append([ts, b["online"], b["wireless"], b["wired"], b["down"] // b["n"], b["up"] // b["n"]])
        else:
            pts.append([ts, None, b["w"], None, None, None])
    return pts


def _weak(act, inv):
    thr = int(X.cfg_get("signal_threshold", -72))
    stats = X.signal_stats()
    out = []
    for x in act:
        if not x.get("wireless") or x.get("rssi") is None:
            continue
        mac = norm_mac(x["mac"])
        a7 = stats.get(mac, {}).get("a7")
        if x["rssi"] < thr or (a7 is not None and a7 < thr):
            out.append({"mac": mac, "name": X.dev_name(mac, x, inv), "rssi": x["rssi"], "a7": a7, "ap": x.get("apName") or ""})
    out.sort(key=lambda w: w["a7"] if w["a7"] is not None else w["rssi"])
    return out, thr


def _rate(b):
    b = float(b or 0) * 8
    for u in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
        if b < 1000:
            return f"{b:.0f} {u}" if u == "bit/s" else f"{b:.1f} {u}"
        b /= 1000
    return f"{b:.1f} Tbit/s"


def when(ts):
    """Kort tijdstip: 'vandaag 16:44', 'gisteren 09:10' of '17-11 08:02'."""
    if not ts:
        return "—"
    lt, now = time.localtime(ts), time.localtime()
    if lt[:3] == now[:3]:
        return time.strftime("%H:%M", lt)
    if lt[:3] == time.localtime(time.time() - 86400)[:3]:
        return time.strftime("gisteren %H:%M", lt)
    return time.strftime("%d-%m %H:%M", lt)


KIND_ICON = {"nieuw": "🆕", "offline": "🔴", "online": "🟢", "gast": "🎟️", "verlopen": "⌛", "pauze": "⏸️",
             "acl": "🧱", "fout": "⚠️", "hersteld": "♻️", "vlan": "🔀", "naam": "✏️", "auto-gekoppeld": "🔗"}


@app.get("/")
@A.login_required
@A.configured
def dashboard():
    err = None
    try:
        cl = X.clients()
    except Exception as e:  # noqa: BLE001
        cl, err = [], e
    act = _act(cl)
    inv = db.devices()
    vn = {}
    try:
        vn = X.vname_map()
    except Exception:  # noqa: BLE001
        pass
    st = A.state()
    now = time.time()

    # wachtende sleutels = sleutels in de state zonder gekoppeld apparaat (zonder Omada-aanroep: uit het profiel)
    pending = 0
    try:
        c, sid, pid, prof, nets, vname, st2 = A.context()
        pending = sum(1 for e in prof["entries"] if not e["mac"] and e["name"] in st2)
    except Exception:  # noqa: BLE001
        pass

    ex = X.index_extra()
    crit = ex["crit"]
    weak, thr = _weak(act, inv)
    guests = [g for g in ex["guests"] if (g.get("expires") or now + 1) > now]
    new24 = sorted((d for d in inv.values() if (d.get("first_seen") or 0) > now - 86400),
                   key=lambda d: -(d.get("first_seen") or 0))
    wifi = sum(1 for c in act if c.get("wireless"))

    vlans = {}
    for c in act:
        v = c.get("vid") or 0
        vlans.setdefault(v, {"name": vn.get(v, f"VLAN {v}" if v else "Onbekend"), "vlan": v, "n": 0, "w": 0})
        vlans[v]["n"] += 1
        vlans[v]["w"] += 1 if c.get("wireless") else 0
    vlans = sorted(vlans.values(), key=lambda x: -x["n"])

    aps = {}
    for c in act:
        if c.get("wireless"):
            a = c.get("apName") or "Onbekend"
            aps.setdefault(a, {"name": a, "n": 0, "b": {}})
            aps[a]["n"] += 1
            band = X.RADIO.get(c.get("radioId"), "?")
            aps[a]["b"][band] = aps[a]["b"].get(band, 0) + 1
    aps = sorted(aps.values(), key=lambda x: -x["n"])

    top = sorted(({"mac": norm_mac(c["mac"]), "name": X.dev_name(norm_mac(c["mac"]), c, inv),
                   "down": int(c.get("activity") or 0), "up": int(c.get("uploadActivity") or 0),
                   "tot": int(c.get("trafficDown") or 0) + int(c.get("trafficUp") or 0)} for c in act),
                 key=lambda x: -(x["down"] + x["up"]))
    has_rate = any(t["down"] + t["up"] for t in top)
    if not has_rate:
        top.sort(key=lambda x: -x["tot"])
    top = [t for t in top[:7] if (t["down"] + t["up"] if has_rate else t["tot"])]

    evs = []
    for e in db.events(limit=60):
        if e["kind"] in ("ip", "start"):
            continue  # ruis: staat in het logboek
        d = inv.get(e["mac"] or "", {})
        e["who"] = d.get("name") or d.get("hostname") or e["mac"] or ""
        e["icon"] = KIND_ICON.get(e["kind"], "🔧" if e.get("source") == "portal" else "•")
        evs.append(e)
        if len(evs) >= 9:
            break

    s24, s7 = series(24), series(24 * 7)
    return render_template(
        "dashboard.html", err=err, online=len(act), wifi=wifi, wired=len(act) - wifi, total=len(inv),
        pending=pending, crit=crit, crit_off=[x for x in crit if not x["online"]], weak=weak, thr=thr,
        guests=guests, paused=ex["paused"], new24=new24, vlans=vlans, aps=aps, top=top, has_rate=has_rate,
        evs=evs, s24=s24, s7=s7, has_traffic=any(p[4] for p in s7 if p[4]), rate=_rate, when=when, cache_age=int(now - X.CACHE["ts"]) if X.CACHE["ts"] else None)
