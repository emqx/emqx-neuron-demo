/* Shared by the namespace view and the independent demo subscriber. */
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

// Refresh authentication on every reconnect, including broker-enforced expiry.
window.demoMqttConnect = async function (config, slot) {
  async function credentials() {
    const response = await fetch('/api/mqtt-credentials?slot=' + encodeURIComponent(slot), {cache: 'no-store'});
    if (!response.ok) throw new Error('Could not authenticate the live subscriber. Reload or try again shortly.');
    return response.json();
  }
  const initial = await credentials();
  const client = mqtt.connect(demoConnection(config), {
    ...initial, clean: true, protocolVersion: 5, reconnectPeriod: 0, connectTimeout: 10000,
    properties: {sessionExpiryInterval: 0},
  });
  let ended = false, standby = false, timer = null;
  async function reconnect() {
    if (ended || standby) return;
    try {
      const next = await credentials();
      if (ended || standby) return;
      client.options.username = next.username;
      client.options.password = next.password;
      client.reconnect();
    } catch {
      if (!ended && !standby) timer = setTimeout(reconnect, 5000);
    }
  }
  // The server deliberately bounds each visitor/view to one MQTT client ID.
  // Do not let two tabs continually disconnect and reconnect each other.
  client.on('disconnect', packet => {
    if (packet.reasonCode !== 0x8e) return; // MQTT 5: session taken over.
    standby = true;
    clearTimeout(timer);
    client.emit('viewer-standby');
  });
  client.on('close', () => { if (!ended && !standby) timer = setTimeout(reconnect, 2000); });
  client.watchHere = () => {
    if (ended) return;
    standby = false;
    clearTimeout(timer);
    client.emit('viewer-resuming');
    reconnect();
  };
  const end = client.end.bind(client);
  client.end = (...args) => { ended = true; clearTimeout(timer); return end(...args); };
  return client;
};
