"""Omada Open API client (client-credentials modus) + PPSK/clients/netwerken."""
import re
import threading
import time

import requests
import urllib3

urllib3.disable_warnings()  # controller op eigen netwerk met self-signed certificaat


class OmadaError(Exception):
    pass


def norm_mac(mac):
    h = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(h) != 12:
        raise ValueError(f"Ongeldig MAC-adres: {mac!r}")
    return "-".join(h[i:i + 2] for i in range(0, 12, 2)).upper()


def is_random_mac(mac):
    try:
        return bool(int(norm_mac(mac)[1], 16) & 0x2)
    except ValueError:
        return False


class Omada:
    def __init__(self, url, client_id, client_secret):
        self.url = url.rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self.s = requests.Session()
        self.s.verify = False
        self.cid = None
        self._token, self._exp = None, 0
        self._lock = threading.Lock()

    # -- basis -----------------------------------------------------------
    def _auth(self):
        if self._token and time.time() < self._exp - 60:
            return
        if not self.cid:
            info = self.s.get(f"{self.url}/api/info", timeout=15).json()
            self.cid = info["result"]["omadacId"]
        r = self.s.post(f"{self.url}/openapi/authorize/token",
                        params={"grant_type": "client_credentials"},
                        json={"omadacId": self.cid, "client_id": self.client_id,
                              "client_secret": self.client_secret}, timeout=15).json()
        if r.get("errorCode") != 0:
            raise OmadaError(f"Open API-login mislukt ({r.get('errorCode')}): {r.get('msg')}")
        self._token = r["result"]["accessToken"]
        self._exp = time.time() + int(r["result"].get("expiresIn", 3600))

    def call(self, method, path, **kw):
        """path zonder /openapi-prefix, met {cid} placeholder, bv. '/v1/{cid}/sites'."""
        with self._lock:
            self._auth()
            url = f"{self.url}/openapi" + path.replace("{cid}", self.cid)
            hdr = {"Authorization": f"AccessToken={self._token}"}
            resp = self.s.request(method, url, headers=hdr, timeout=20, **kw)
        try:
            data = resp.json()
        except ValueError:
            raise OmadaError(f"{method} {path}: HTTP {resp.status_code}, geen JSON")
        if data.get("errorCode") == -44112 and self._token:  # token verlopen
            self._token = None
            return self.call(method, path, **kw)
        if data.get("errorCode") != 0:
            raise OmadaError(f"{data.get('msg') or 'fout'} (code {data.get('errorCode')})")
        return data.get("result")

    def pages(self, path, **params):
        page = 1
        while True:
            res = self.call("GET", path, params={"page": page, "pageSize": 1000, **params})
            if isinstance(res, list):
                yield from res
                return
            yield from (res or {}).get("data") or []
            if page * 1000 >= ((res or {}).get("totalRows") or 0):
                return
            page += 1

    # -- lezen -----------------------------------------------------------
    def sites(self):
        return [{"id": s["siteId"], "name": s["name"]} for s in self.pages("/v1/{cid}/sites")]

    def networks(self, sid):
        """VLAN's uit de LAN-netwerken, gesorteerd op VLAN-ID."""
        out = [{"id": n["id"], "name": n["name"], "vlan": int(n.get("vlan") or 1),
                "subnet": n.get("gatewaySubnet", ""),
                "dhcp": bool((n.get("dhcpSettingsVO") or {}).get("enable"))}
               for n in self.pages(f"/v1/{{cid}}/sites/{sid}/lan-networks")]
        return sorted(out, key=lambda n: n["vlan"])

    def ppsk_profiles(self, sid):
        res = self.call("GET", f"/v1/{{cid}}/sites/{sid}/ppsk-profiles")
        res = res if isinstance(res, list) else (res or {}).get("data", [])
        return [{"id": p["id"], "name": p["profileName"], "ssids": p.get("ssid") or []} for p in res]

    def ppsk(self, sid, pid):
        p = self.call("GET", f"/v1/{{cid}}/sites/{sid}/ppsk-profile/{pid}")
        entries = []
        for e in p.get("ppsk") or []:
            entries.append({"name": e["name"], "psk": e.get("psk", ""),
                            "mac": norm_mac(e["mac"]) if e.get("mac") else "",
                            "vlan": e.get("vlan")})
        return {"id": p["id"], "name": p["profileName"], "ssids": p.get("ssid") or [], "entries": entries}

    def clients(self, sid):
        return list(self.pages(f"/v1/{{cid}}/sites/{sid}/clients"))

    def known_clients(self, sid):
        """Alle bekende clients, ook offline (Insight)."""
        return list(self.pages(f"/v1/{{cid}}/sites/{sid}/insight/clients"))

    def client_info(self, sid, mac):
        return self.call("GET", f"/v1/{{cid}}/sites/{sid}/clients/{norm_mac(mac)}")

    def gateway_mac(self, sid):
        for d in self.pages(f"/v1/{{cid}}/sites/{sid}/devices"):
            if str(d.get("type", "")).lower() == "gateway":
                return d["mac"]
        return None

    def switch(self, sid, mac):
        return self.call("GET", f"/v1/{{cid}}/sites/{sid}/switches/{norm_mac(mac)}")

    def set_port_vlan(self, sid, switch_mac, port, profile_id, net_id, name):
        """Zet het untagged (native) VLAN van één switchpoort, zoals Omada dat zelf doet bij een
        poort met eigen instellingen: profiel blijft, override aan, native = gekozen netwerk,
        getagde VLAN's geblokkeerd (eindapparaat)."""
        self.call("PATCH", f"/v1/{{cid}}/sites/{sid}/switches/{norm_mac(switch_mac)}/ports/{int(port)}",
                  json={"name": name or f"Port{int(port)}", "profileId": profile_id, "profileOverrideEnable": True,
                        "profileVlanOverrideEnable": True, "nativeNetworkId": net_id,
                        "networkTagsSetting": 1, "tagNetworkIds": [], "untagNetworkIds": []})

    def lan_profiles(self, sid):
        return list(self.pages(f"/v1/{{cid}}/sites/{sid}/lan-profiles"))

    def set_port_profile(self, sid, switch_mac, port, profile_id):
        """Koppel een poortprofiel aan één switchpoort en laat de poort het profiel volgen
        (zonder eigen overrides), en controleer daarna of het echt is toegepast."""
        mac, port = norm_mac(switch_mac), int(port)
        self.call("PUT", f"/v1/{{cid}}/sites/{sid}/switches/{mac}/ports/{port}/profile",
                  json={"profileId": profile_id})
        p = next((x for x in self.switch(sid, mac).get("portList") or [] if int(x["port"]) == port), {})
        if p.get("profileOverrideEnable"):
            self.call("PATCH", f"/v1/{{cid}}/sites/{sid}/switches/{mac}/ports/{port}",
                      json={"profileId": profile_id, "profileOverrideEnable": False,
                            "profileVlanOverrideEnable": False})
            p = next((x for x in self.switch(sid, mac).get("portList") or [] if int(x["port"]) == port), {})
        if p.get("profileId") != profile_id:
            raise OmadaError(f"Omada heeft het poortprofiel niet overgenomen (poort {port} heeft nog "
                             f"profiel '{p.get('profileName')}')")
        return p

    def correction_options(self):
        r = self.call("GET", "/v1/{cid}/correction-list") or {}
        return ([t["name"] for t in r.get("typeList") or []], [v["name"] for v in r.get("vendorList") or []])

    def set_fixed_ip(self, sid, mac, enable, net_id=None, ip=None, server_mac=None):
        body = {"useFixedAddr": bool(enable)}
        if enable:
            body.update(netId=net_id, ip=ip, serverType="gateway", serverMac=server_mac)
        self.call("PATCH", f"/v1/{{cid}}/network/sites/{sid}/cmd/clients/{norm_mac(mac)}/update-ipSetting",
                  json=body)

    # -- schrijven -------------------------------------------------------
    @staticmethod
    def _entry(e):
        d = {"name": e["name"], "psk": e["psk"]}
        if e.get("mac"):
            d["mac"] = norm_mac(e["mac"])
        if e.get("vlan"):
            d["vlan"] = int(e["vlan"])
        return d

    def add_psk(self, sid, pid, entry):
        self.call("POST", f"/v1/{{cid}}/sites/{sid}/ppsk-profile/{pid}/add-psk",
                  json={"ppskList": [self._entry(entry)]})

    def delete_psk(self, sid, pid, name):
        self.call("POST", f"/v1/{{cid}}/sites/{sid}/ppsk-profile/{pid}/delete-psk",
                  json={"ppskNameList": [name]})

    def replace_psk(self, sid, pid, old, new):
        """Wijzig een sleutel (verwijderen + opnieuw toevoegen); bij een fout de oude terugzetten."""
        self.delete_psk(sid, pid, old["name"])
        try:
            self.add_psk(sid, pid, new)
        except Exception:
            self.add_psk(sid, pid, old)
            raise

    def reconnect(self, sid, mac):
        self.call("POST", f"/v1/{{cid}}/sites/{sid}/clients/{norm_mac(mac)}/reconnect")

    def set_client_name(self, sid, mac, name):
        self.call("PATCH", f"/v1/{{cid}}/sites/{sid}/clients/{norm_mac(mac)}/name", json={"name": name})


class OmadaWeb:
    """Omada's interne web-API (v2), alleen voor wat de Open API niet kan:
    type/merk/model ('clientInfoCorrection') van een client aanpassen. Niet officieel gedocumenteerd."""

    def __init__(self, url, user, password):
        self.url, self.user, self.password = url.rstrip("/"), user, password
        self.s = None
        self.cid = None
        self._lock = threading.Lock()

    def _login(self):
        s = requests.Session()
        s.verify = False
        self.cid = s.get(f"{self.url}/api/info", timeout=15).json()["result"]["omadacId"]
        r = s.post(f"{self.url}/{self.cid}/api/v2/login",
                   json={"username": self.user, "password": self.password}, timeout=15).json()
        if r.get("errorCode") != 0:
            raise OmadaError(f"Inloggen web-API mislukt: {r.get('msg')}")
        s.headers["Csrf-Token"] = r["result"]["token"]
        self.s = s

    def call(self, method, path, retry=True, **kw):
        with self._lock:
            if not self.s:
                self._login()
            resp = self.s.request(method, f"{self.url}/{self.cid}/api/v2{path}", timeout=20, **kw)
        try:
            data = resp.json()
        except ValueError:  # sessie verlopen -> HTML-loginpagina
            data = {"errorCode": -1}
        if data.get("errorCode") != 0 and retry:
            self.s = None
            return self.call(method, path, retry=False, **kw)
        if data.get("errorCode") != 0:
            raise OmadaError(f"{data.get('msg') or 'fout'} (code {data.get('errorCode')})")
        return data.get("result")

    def switch(self, sid, mac):
        return self.call("GET", f"/sites/{sid}/switches/{norm_mac(mac)}")

    def set_client_info(self, sid, mac, name, dtype, vendor, model):
        corr = {k: v for k, v in (("type", dtype), ("vendor", vendor), ("model", model)) if v}
        body = {"name": name, "clientInfoCorrection": corr}
        self.call("PATCH", f"/sites/{sid}/clients/{norm_mac(mac)}", json=body)
