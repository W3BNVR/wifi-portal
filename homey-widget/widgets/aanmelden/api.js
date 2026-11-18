'use strict';

module.exports = {
  async create({ homey, body }) {
    const { url, token, ssid, name } = body || {};
    return homey.app.portal({ url, token }, 'POST', '/api/v1/new', { name, ssid });
  },

  async status({ homey, body }) {
    const { url, token, name } = body || {};
    return homey.app.portal({ url, token }, 'GET', `/api/v1/key/${encodeURIComponent(name)}`);
  },
};
