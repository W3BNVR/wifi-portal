"""Client vastzetten op een AP (Omada 'Lock to AP') en AP-scan: welk AP geeft het beste bereik?

- Historie: de signaalmetingen (elke 5 min, tabel `signal`) per AP voor dit apparaat.
- Actieve scan: zet het apparaat om de beurt vast op elk AP, wacht tot het daar verbonden is en meet
  het signaal. Daarna wordt de oorspronkelijke instelling hersteld (of het beste AP vastgezet).
  De scan staat in aplock.json; na een herstart van de portal wordt een onderbroken scan netjes
  teruggedraaid, zodat een apparaat nooit per ongeluk op een verkeerd AP vast blijft staan.
"""
import os
import threading
import time

from flask import flash, jsonify, redirect, request, url_for

import app as A
import db
import extras as X
from omada import norm_mac

app = A.app
SCAN_FILE = os.path.join(A.DATA, "aplock.json")
_lock = threading.Lock()
_threads = {}

CONNECT_TIMEOUT = int(os.environ.get("APSCAN_TIMEOUT", 75))   # s wachten tot het apparaat op het nieuwe AP zit
SETTLE = int(os.environ.get("APSCAN_SETTLE", 6))            # s na verbinden voordat we meten
SAMPLES = 3            # metingen per AP
SAMPLE_GAP = int(os.environ.get("APSCAN_GAP", 4))        # s tussen metingen


def _nm(m):
    """norm_mac die leeg/ongeldig verdraagt."""
    try:
        return norm_mac(m) if m else ""
    except ValueError:
        return ""


def _scans():
    return A._load(SCAN_FILE, {})


def _put(mac, **kw):
    with _lock:
        s = _scans()
        s.setdefault(mac, {}).update(kw)
        A._save(SCAN_FILE, s)
        return s[mac]


def _sid():
    return A.cfg()["site"]


def lockable_aps(sid=None):
    sid = sid or _sid()
    try:
        aps = A.OMADA.call("GET", f"/v2/{{cid}}/sites/{sid}/lockToAp/aps") or []
    except Exception:  # noqa: BLE001 - oudere controller: gewone apparatenlijst
        aps = [d for d in A.OMADA.pages(f"/v1/{{cid}}/sites/{sid}/devices") if str(d.get("type")).lower() == "ap"]
        for d in aps:
            d["supportLockToAp"] = True
    out = [{"mac": norm_mac(a["mac"]), "name": a.get("name") or a["mac"], "model": a.get("showModel") or a.get("model") or "",
            "online": a.get("statusCategory", 1) == 1 or a.get("status") == 1}
           for a in aps if a.get("supportLockToAp", True)]
    return sorted(out, key=lambda a: a["name"].lower())


def _lock_macs(setting):
    """clientLockToApSetting → lijst AP-MAC's (of [] als uit)."""
    if not setting or not setting.get("enable"):
        return []
    res = []
    for a in setting.get("aps") or []:
        res.append(_nm(a if isinstance(a, str) else (a.get("mac") or a.get("apMac"))))
    return [m for m in res if m]


def set_lock(sid, mac, aps):
    body = {"enable": bool(aps)}
    if aps:
        body["aps"] = [norm_mac(a) for a in aps]
    A.OMADA.call("POST", f"/v1/{{cid}}/sites/{sid}/clients/{norm_mac(mac)}/lock-to-ap", json=body)


def history(mac, days=8):
    """Signaal per AP uit de 5-minuten-metingen."""
    since = int(time.time()) - days * 86400
    with X._sigdb() as c:
        rows = c.execute("""SELECT ap, AVG(rssi) a, MAX(rssi) mx, MIN(rssi) mn, COUNT(*) n, MAX(ts) last,
                                   AVG(CASE WHEN ts > ? THEN rssi END) a24
                            FROM signal WHERE mac = ? AND ts > ? AND ap IS NOT NULL AND ap != ''
                            GROUP BY ap ORDER BY a DESC""", (int(time.time()) - 86400, mac, since)).fetchall()
    tot = sum(r[4] for r in rows) or 1
    return [{"ap": r[0], "avg": round(r[1]), "max": r[2], "min": r[3], "n": r[4], "share": round(r[4] * 100 / tot),
             "last": r[5], "a24": round(r[6]) if r[6] is not None else None} for r in rows]


def _measure(sid, mac):
    i = A.OMADA.client_info(sid, mac) or {}
    return i


def _run_scan(sid, mac, aps, orig, apply_best):
    """Achtergrondthread: elk AP afgaan, meten, herstellen."""
    results = []
    try:
        cur = _measure(sid, mac)
        cur_ap = _nm(cur.get("apMac"))
        order = sorted(aps, key=lambda a: a["mac"] != cur_ap)  # huidige AP eerst: meten zonder te wisselen
        for n, ap in enumerate(order, 1):
            if _scans().get(mac, {}).get("cancel"):
                break
            r = {"ap": ap["name"], "mac": ap["mac"], "ok": False}
            _put(mac, step=n, total=len(order), now=ap["name"], phase="wisselen", results=results)
            t0 = time.time()
            if ap["mac"] != cur_ap or n > 1:
                set_lock(sid, mac, [ap["mac"]])
            info = {}
            while time.time() - t0 < CONNECT_TIMEOUT:
                time.sleep(3)
                try:
                    info = _measure(sid, mac)
                except Exception:  # noqa: BLE001 - even weg tijdens het wisselen
                    info = {}
                if _nm(info.get("apMac")) == ap["mac"] and info.get("rssi") is not None and info.get("active", True):
                    break
                if _scans().get(mac, {}).get("cancel"):
                    break
            else:
                info = {}
            if _nm(info.get("apMac")) != ap["mac"]:
                r["why"] = f"geen verbinding binnen {CONNECT_TIMEOUT} s (buiten bereik?)"
                results.append(r)
                continue
            r["switch_s"] = round(time.time() - t0)
            _put(mac, phase="meten", results=results)
            time.sleep(SETTLE)
            vals, snr, rate, ch = [], [], [], None
            for _ in range(SAMPLES):
                try:
                    i = _measure(sid, mac)
                    if _nm(i.get("apMac")) == ap["mac"] and i.get("rssi") is not None:
                        vals.append(i["rssi"])
                        snr.append(i.get("snr") or 0)
                        rate.append(((i.get("rxRate") or 0) + (i.get("txRate") or 0)) / 2000)
                        ch = i.get("channel")
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(SAMPLE_GAP)
            if vals:
                r.update(ok=True, rssi=round(sum(vals) / len(vals)), snr=round(sum(snr) / len(snr)),
                         rate=round(sum(rate) / len(rate)), band="5 GHz" if (ch or 0) > 14 else "2,4 GHz", channel=ch)
            else:
                r["why"] = "verbonden, maar geen meting"
            results.append(r)
            _put(mac, results=results)
        ok = [r for r in results if r["ok"]]
        best = max(ok, key=lambda r: (r["rssi"], r.get("snr", 0))) if ok else None
        if apply_best and best:
            set_lock(sid, mac, [best["mac"]])
            end = f"vastgezet op {best['ap']}"
        else:
            set_lock(sid, mac, orig)
            end = "oorspronkelijke instelling hersteld"
        txt = ", ".join(f"{r['ap']} {r['rssi']} dBm" if r["ok"] else f"{r['ap']} –" for r in results)
        db.log(mac, "ap-scan", f"AP-scan: {txt}. Beste: {best['ap'] if best else 'geen'}; {end}", "portal")
        _put(mac, status="klaar", phase="", now="", best=best["mac"] if best else None, ended=time.time(), end=end,
             results=results, cancel=False)
    except Exception as e:  # noqa: BLE001
        try:
            set_lock(sid, mac, orig)
        except Exception:  # noqa: BLE001
            pass
        db.log(mac, "fout", f"AP-scan afgebroken: {e}", "portal")
        _put(mac, status="fout", error=str(e), ended=time.time(), results=results)
    finally:
        _threads.pop(mac, None)


def _recover():
    """Na een herstart: onderbroken scans terugdraaien naar de oorspronkelijke instelling."""
    time.sleep(10)
    for mac, s in list(_scans().items()):
        if s.get("status") == "bezig":
            try:
                set_lock(_sid(), mac, s.get("orig") or [])
                db.log(mac, "ap-scan", "AP-scan onderbroken door herstart; oorspronkelijke instelling hersteld", "portal")
                _put(mac, status="fout", error="onderbroken door herstart van de portal", ended=time.time())
            except Exception:  # noqa: BLE001
                pass


if os.environ.get("PORTAL_NO_SYNC") != "1":
    threading.Thread(target=_recover, daemon=True, name="aplock-recover").start()


@app.template_global()
def ap_lock_info(mac, info):
    mac = norm_mac(mac)
    sid = _sid()
    try:
        aps = lockable_aps(sid)
    except Exception:  # noqa: BLE001
        aps = []
    locked = _lock_macs((info or {}).get("clientLockToApSetting"))
    names = {a["mac"]: a["name"] for a in aps}
    hist = history(mac)
    s = _scans().get(mac) or {}
    return {"aps": aps, "locked": locked, "locked_names": [names.get(m, m) for m in locked],
            "hist": hist, "hist_best": hist[0]["ap"] if hist and hist[0]["n"] >= 3 else None,
            "scan": s, "busy": s.get("status") == "bezig", "cur": _nm((info or {}).get("apMac")),
            "per_ap": round((CONNECT_TIMEOUT + SETTLE + SAMPLES * SAMPLE_GAP) / 60, 1)}


@app.post("/client/<mac>/aplock")
@A.login_required
@A.configured
def client_aplock(mac):
    mac = norm_mac(mac)
    sid = _sid()
    act = request.form.get("action")
    back = url_for("client", mac=mac) + "#aplock"
    try:
        if act == "set":
            aps = [norm_mac(a) for a in request.form.getlist("aps")]
            if not aps:
                flash("Kies minstens één AP, of zet vastzetten uit", "err")
                return redirect(back)
            set_lock(sid, mac, aps)
            names = {a["mac"]: a["name"] for a in lockable_aps(sid)}
            db.log(mac, "ap-lock", "Vastgezet op " + ", ".join(names.get(a, a) for a in aps), "portal")
            flash("Vastgezet op " + ", ".join(names.get(a, a) for a in aps) + ". Het apparaat verbindt zo nodig opnieuw.", "ok")
        elif act == "off":
            set_lock(sid, mac, [])
            db.log(mac, "ap-lock", "Vastzetten op AP uitgezet", "portal")
            flash("Vastzetten uit: het apparaat mag weer elk AP kiezen", "ok")
        elif act == "scan":
            if mac in _threads:
                flash("Er loopt al een scan voor dit apparaat", "err")
                return redirect(back)
            info = A.OMADA.client_info(sid, mac) or {}
            if not info.get("wireless") or not info.get("apMac"):
                flash("Scannen kan alleen als het apparaat nu draadloos verbonden is", "err")
                return redirect(back)
            aps = [a for a in lockable_aps(sid) if a["online"]]
            pick = set(norm_mac(a) for a in request.form.getlist("scan_aps"))
            if pick:
                aps = [a for a in aps if a["mac"] in pick]
            if len(aps) < 2:
                flash("Kies minstens twee AP's om te vergelijken", "err")
                return redirect(back)
            orig = _lock_macs(info.get("clientLockToApSetting"))
            apply_best = request.form.get("apply") == "1"
            _put(mac, status="bezig", started=time.time(), orig=orig, apply=apply_best, results=[], step=0,
                 total=len(aps), now="", phase="starten", best=None, error=None, end=None, cancel=False)
            db.log(mac, "ap-scan", f"AP-scan gestart over {len(aps)} AP's", "portal")
            t = threading.Thread(target=_run_scan, args=(sid, mac, aps, orig, apply_best), daemon=True, name=f"apscan-{mac}")
            _threads[mac] = t
            t.start()
        elif act == "cancel":
            _put(mac, cancel=True)
            flash("Scan wordt gestopt; de oorspronkelijke instelling wordt hersteld", "ok")
        elif act == "best":
            ap = norm_mac(request.form.get("ap", ""))
            set_lock(sid, mac, [ap])
            names = {a["mac"]: a["name"] for a in lockable_aps(sid)}
            db.log(mac, "ap-lock", f"Vastgezet op {names.get(ap, ap)} (na scan)", "portal")
            flash(f"Vastgezet op {names.get(ap, ap)}", "ok")
    except Exception as e:  # noqa: BLE001
        flash(f"Mislukt: {e}", "err")
    return redirect(back)


@app.get("/client/<mac>/apscan.json")
@A.login_required
def client_apscan(mac):
    s = _scans().get(norm_mac(mac)) or {}
    return jsonify({k: s.get(k) for k in ("status", "step", "total", "now", "phase", "results", "best", "error", "end")})
