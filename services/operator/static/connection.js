/* Shared by the namespace view and the independent showcase subscriber. */
window.demoConnection = function (config) {
  if (config.mqttUrl) return config.mqttUrl;
  if (config.mqttProxy || location.protocol === 'https:') {
    const url = new URL('/mqtt', location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return url.href;
  }
  const url = new URL(location.href);
  url.protocol = 'ws:';
  url.port = '8083';
  url.pathname = '/mqtt';
  url.search = '';
  url.hash = '';
  return url.href;
};
