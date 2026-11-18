'use strict';

const Homey = require('homey');

class WifiPortalApp extends Homey.App {
  async onInit() {
    this.log('Wifi-portal app gestart');
  }

  /** Roept de wifi-portal op de NAS aan (vanaf Homey zelf, dus geen CORS-gedoe in het dashboard). */
  async portal({ url, token }, method, path, body) {
    if (!url || !token) throw new Error('Vul in de widget-instellingen het portal-adres en het API-token in');
    const base = String(url).trim().replace(/\/+$/, '');
    let res;
    try {
      res = await fetch(base + path, {
        method,
        headers: { Authorization: `Bearer ${String(token).trim()}`, 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(20000),
      });
    } catch (err) {
      throw new Error(`Portal niet bereikbaar op ${base} (${err.message})`);
    }
    let data = {};
    try { data = await res.json(); } catch (_) { /* geen JSON */ }
    if (!res.ok) throw new Error(data.error || `Portal gaf HTTP ${res.status}`);
    return data;
  }
}

module.exports = WifiPortalApp;
