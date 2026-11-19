'use strict';

const Homey = require('homey');

class WifiPortalApp extends Homey.App {
  async onInit() {
    this.trigNew = this.homey.flow.getTriggerCard('new_device');
    this.trigOff = this.homey.flow.getTriggerCard('device_offline');
    this.trigOn = this.homey.flow.getTriggerCard('device_online');

    this.homey.flow.getActionCard('create_guest_key').registerRunListener(async (args) => {
      const r = await this.portal(this.conn(), 'POST', '/api/v1/guest', { name: args.name || '', hours: args.hours || 24 });
      return { ssid: r.ssid || '', psk: r.psk || '', key: r.name || '' };
    });

    const pause = this.homey.flow.getActionCard('pause_device');
    pause.registerArgumentAutocompleteListener('device', async (query) => {
      const r = await this.portal(this.conn(), 'GET', `/api/v1/devices?q=${encodeURIComponent(query || '')}`);
      return (r.devices || []).map((d) => ({
        name: d.name, mac: d.mac,
        description: `${d.mac}${d.vlan ? ' · ' + d.vlan : ''}${d.online ? '' : ' · offline'}`,
      }));
    });
    pause.registerRunListener(async (args) => {
      const on = args.state === 'on';
      await this.portal(this.conn(), 'POST', '/api/v1/pause', { mac: args.device.mac, pause: on, minutes: on ? (args.minutes || 0) : 0 });
      return true;
    });

    this.homey.settings.on('set', (key) => {
      if (key === 'url' || key === 'token') this.homey.settings.unset('lastEvent');
    });
    this.homey.setInterval(() => this.poll().catch((e) => this.error('poll:', e.message)), 30000);
    this.poll().catch(() => {});
    this.log('Wifi-portal app gestart');
  }

  /** Verbinding uit de app-instellingen (voor Flow-kaarten); valt terug op env.json als die er is. */
  conn() {
    const env = Homey.env || {};
    return {
      url: this.homey.settings.get('url') || env.PORTAL_URL,
      token: this.homey.settings.get('token') || env.PORTAL_TOKEN,
    };
  }

  /** Haalt nieuwe gebeurtenissen op en laat de bijbehorende Flow-kaarten afgaan. */
  async poll() {
    const c = this.conn();
    if (!c.url || !c.token) return;
    const last = this.homey.settings.get('lastEvent');
    const r = await this.portal(c, 'GET', '/api/v1/events' + (last == null ? '' : `?after=${last}`));
    for (const e of r.events || []) {
      const t = { name: e.name || e.mac || '', mac: e.mac || '', vlan: String(e.vlan || ''), detail: e.detail || '' };
      if (e.kind === 'nieuw') await this.trigNew.trigger({ ...t, ip: e.ip || '' });
      else if (e.kind === 'offline') await this.trigOff.trigger(t);
      else if (e.kind === 'online') await this.trigOn.trigger(t);
    }
    if (r.last != null) this.homey.settings.set('lastEvent', r.last);
  }

  /** Roept de wifi-portal op de NAS aan (vanaf Homey zelf, dus geen CORS-gedoe in het dashboard). */
  async portal({ url, token }, method, path, body) {
    if (!url || !token) throw new Error('Vul het portal-adres en het API-token in (app-instellingen of widget-instellingen)');
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
