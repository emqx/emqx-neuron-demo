'use strict';
const config = JSON.parse(document.getElementById('demo-config').textContent);
const $ = id => document.getElementById(id);
const set = (id, text) => { $(id).textContent = text; };
let state = null;
let client = null;
let clientConnected = false;
let subscriberCount = 0;
let liveCount = 0;
let retainedCount = 0;
let powers = {};
let points = [];
let lastPointAt = 0;
let lastCycle = null;
let lastReset = null;
let autoAttached = null;
let shownEvent = null;
let requestPending = false;
let clockOffset = 0;

function notice(message) {
  $('notice').hidden = !message;
  $('notice').textContent = message || '';
}

async function action(name, body = {}) {
  if (requestPending) return;
  requestPending = true;
  try {
    const response = await fetch('/api/demo/' + name, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body), signal: AbortSignal.timeout(10000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'The action could not be completed.');
    notice('');
  } catch (error) { notice(error.message); }
  finally { requestPending = false; }
}

function formatNumber(value, decimals = 0) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString(undefined, {maximumFractionDigits: decimals, minimumFractionDigits: decimals}) : '—';
}

function render(data) {
  state = data;
  clockOffset = data.server_time * 1000 - Date.now();
  const scene = data.scene;
  const stale = !data.ready;
  $('connection').className = 'connection ' + (stale ? 'bad' : 'good');
  $('connection').replaceChildren(Object.assign(document.createElement('i'), {}),
    document.createTextNode(!data.connected ? 'Broker disconnected' : stale ? 'Waiting for fresh data' : 'All sources live'));
  set('story-step', scene.loop ? 'AUTO LOOP · RUN ' + scene.iteration : scene.step === 'ready' ? 'READY TO EXPLORE' : 'LIVE WALKTHROUGH · ' + scene.step.toUpperCase());
  set('story-title', scene.title);
  set('caption', scene.caption);
  set('messages', data.messages.toLocaleString() + ' messages observed');
  const freshCount = Object.values(data.assets).filter(asset => asset.fresh).length;
  set('sources-count', freshCount + ' of 3 sources live');
  $('neuron-light').className = freshCount === 3 ? 'good' : '';
  for (const id of ['plc-1', 'plc-2', 'hvac-1']) {
    const node = $('asset-' + id), asset = data.assets[id], plc = data.plcs[id] || {};
    const hvac = id === 'hvac-1';
    const values = asset?.values || {};
    const fresh = asset?.fresh;
    node.classList.toggle('stale', !fresh);
    node.classList.toggle('fresh', !!fresh);
    node.classList.toggle('faulted', plc.state === 'faulted');
    $('gateway-' + id).classList.toggle('live', !!fresh);
    node.querySelector('[data-metric]').textContent = formatNumber(values[hvac ? 'supply_air_temp_c' : 'spindle_rpm'], hvac ? 1 : 0);
    node.querySelector('[data-secondary]').textContent = formatNumber(values[hvac ? 'fan_kw' : 'power_kw'], 1) + ' kW';
    node.querySelector('[data-state]').textContent = !fresh ? (asset?.error ? 'Source read error' : 'No fresh data') :
      hvac ? (values.unit_running ? 'Ventilation running' : 'Standby') :
      plc.state === 'faulted' ? 'Over-temperature · paused' : (plc.state || 'idle') + (plc.phase ? ' · ' + plc.phase : '');
    node.querySelector('[data-age]').textContent = !asset?.received_at ? '—' : asset.age > 60 ? '> 1 min old' : asset.age.toFixed(1) + 's ago';
  }
  for (const name of ['acquisition', 'distribution', 'commands']) {
    $('flow-' + name).classList.toggle('live', !!data.activity[name] && data.connected);
  }
  document.querySelectorAll('[data-traffic]').forEach(node => {
    const active = node.dataset.traffic.split(' ').some(key => data.activity[key]);
    node.classList.toggle('live', !!active && data.connected);
  });
  const plc = data.plcs[scene.plc_id] || {};
  // A PLC completes each phase; only the order status proves the whole part is complete.
  const status = data.order.state === 'completed' ? 'completed' : data.order.state === 'failed' ? 'failed' : plc.state === 'completed' ? 'running' : plc.state || 'ready';
  set('order-state', status);
  $('order-state').className = 'state-pill ' + status;
  set('order-phase', status === 'completed' ? 'Part completed' : status === 'faulted' ? 'Machining paused' : status === 'failed' ? 'Order stopped' : plc.phase ? (plc.phase[0].toUpperCase() + plc.phase.slice(1)) + ' phase' : 'Ready for the next part');
  set('order-meta', scene.order_id ? scene.order_id + (data.order.batch_id ? ' · ' + data.order.batch_id : '') : 'ERP → MES → machine → completion');
  const phases = ['load', 'rough', 'finish', 'unload'];
  document.querySelectorAll('[data-phase]').forEach(node => {
    const index = phases.indexOf(node.dataset.phase), current = phases.indexOf(plc.phase);
    node.className = status === 'completed' || current > index ? 'done' : current === index ? 'current' : '';
  });
  const stopping = scene.step === 'stopping';
  $('start').disabled = !data.ready || scene.active || stopping;
  $('loop').disabled = !data.ready || scene.active || stopping;
  $('reset').disabled = stopping;
  $('inject').disabled = !data.ready || !scene.active || plc.state !== 'running' || stopping;
  $('clear').disabled = !data.connected || plc.state !== 'faulted' || stopping;
  $('attach').disabled = !data.connected;

  if ((lastReset !== null && data.reset_version !== lastReset) || (scene.cycle && scene.cycle !== lastCycle)) {
    detachDashboard();
    autoAttached = null;
  }
  lastReset = data.reset_version;
  lastCycle = scene.cycle;
  if (scene.attach_dashboard && autoAttached !== scene.cycle) {
    autoAttached = scene.cycle;
    attachDashboard();
  }
  if (data.sample) {
    set('sample-topic', data.sample.topic);
    set('sample-payload', JSON.stringify(data.sample.payload) + '   · unit: rpm');
    const stamp = data.sample.payload.ts;
    const age = typeof stamp === 'number' ? data.server_time - stamp / 1000 : Infinity;
    set('sample-freshness', age < 6 && data.connected ? 'Current value · rpm' : 'Last known value · check source');
  }
  const eventKey = data.events[0]?.id || 'empty';
  if (shownEvent !== eventKey) {
    shownEvent = eventKey;
    const rows = data.events.slice(0, 5).map(event => {
      const li = document.createElement('li'), time = document.createElement('time');
      time.textContent = new Date(event.time * 1000).toLocaleTimeString();
      const label = document.createElement('span'); label.className = 'event-label'; label.textContent = event.label;
      const detail = document.createElement('span'); detail.className = 'event-detail'; detail.textContent = event.detail;
      li.append(time, label, detail); return li;
    });
    if (!rows.length) {
      const li = document.createElement('li'); li.className = 'empty'; li.textContent = 'Start a walkthrough to follow the order.'; rows.push(li);
    }
    $('events').replaceChildren(...rows);
  }
  updateDashboard();
}

function detachDashboard() {
  const previous = client;
  client = null;
  clientConnected = false;
  if (previous) previous.end(true);
  subscriberCount = liveCount = retainedCount = 0;
  powers = {}; points = []; lastPointAt = 0;
  $('dashboard-empty').hidden = false;
  $('dashboard-live').hidden = true;
  $('extra-app').classList.remove('attached');
  set('power-value', '—');
  $('spark-line').setAttribute('d', ''); $('spark-fill').setAttribute('d', '');
}

function attachDashboard() {
  if (client) return;
  if (!window.mqtt) { notice('The local MQTT browser library is unavailable. Reload the page.'); return; }
  $('dashboard-empty').hidden = true;
  $('dashboard-live').hidden = false;
  $('extra-app').classList.add('attached');
  set('subscriber-status', 'Connecting a new subscriber…');
  const current = mqtt.connect(demoConnection(config), {
    clientId: 'booth-dashboard-' + Math.random().toString(16).slice(2, 10), clean: true,
    reconnectPeriod: 2000, connectTimeout: 10000,
  });
  client = current;
  current.on('connect', () => {
    if (client !== current) return;
    clientConnected = true;
    // A real, separate subscriber. These values never come from /api/showcase.
    current.subscribe(config.machinePrefix + '/+/telemetry/power_kw', {qos: 0}, error => {
      if (error && client === current) set('subscriber-status', 'Subscription failed: ' + error.message);
    });
    updateDashboard();
  });
  current.on('message', (topic, payload, packet) => {
    if (client !== current) return;
    let sample;
    try { sample = JSON.parse(payload.toString()); } catch { return; }
    const asset = topic.split('/')[4];
    if (!['plc-1', 'plc-2'].includes(asset) || typeof sample.value !== 'number' || !Number.isFinite(sample.value) || typeof sample.ts !== 'number') return;
    subscriberCount++;
    if (packet.retain) retainedCount++; else liveCount++;
    powers[asset] = {value: sample.value, timestamp: sample.ts, received: Date.now()};
    updateDashboard();
  });
  current.on('close', () => { if (client === current) { clientConnected = false; updateDashboard(); } });
  current.on('error', error => { if (client === current) set('subscriber-status', 'Connection error: ' + error.message); });
}

function updateDashboard() {
  if (!client) return;
  const now = Date.now();
  const fresh = ['plc-1', 'plc-2'].every(id => powers[id] && now - powers[id].received < 6000 && now + clockOffset - powers[id].timestamp < 6000);
  if (!clientConnected) {
    set('subscriber-status', 'Subscriber disconnected · reconnecting…');
    set('power-value', '—'); return;
  }
  if (!fresh) {
    set('power-value', '—');
    set('subscriber-status', subscriberCount ? 'Subscribed · waiting for fresh readings from both CNCs' : 'Subscribed · waiting for machine power data'); return;
  }
  const total = powers['plc-1'].value + powers['plc-2'].value;
  set('power-value', formatNumber(total, 1));
  set('subscriber-status', liveCount ? `${liveCount} live updates · ${retainedCount} retained values · independent subscriber` : `${retainedCount} retained values received · waiting for live updates`);
  if (now - lastPointAt > 800) {
    points.push(total); points = points.slice(-55); lastPointAt = now;
    const ceiling = Math.max(14, ...points);
    const d = points.map((v, i) => `${i ? 'L' : 'M'}${i * 320 / 54},${52 - (v / ceiling) * 46}`).join(' ');
    $('spark-line').setAttribute('d', d);
    $('spark-fill').setAttribute('d', `${d} L${(points.length - 1) * 320 / 54},56 L0,56 Z`);
  }
}

async function poll() {
  try {
    const response = await fetch('/api/showcase', {cache: 'no-store', signal: AbortSignal.timeout(4500)});
    if (response.status === 401) {
      notice('Your demo session has expired. Reload to sign in again.');
      throw new Error('Session expired');
    }
    if (!response.ok) throw new Error('Demo service unavailable');
    render(await response.json());
  } catch (error) {
    $('connection').className = 'connection bad';
    $('connection').replaceChildren(document.createElement('i'), document.createTextNode('Demo connection lost'));
    ['start', 'loop', 'inject', 'clear', 'attach'].forEach(id => { $(id).disabled = true; });
    document.querySelectorAll('.flow,.return-path,.flow-line,.gateway').forEach(node => node.classList.remove('live'));
    document.querySelectorAll('.asset').forEach(node => {
      node.classList.add('stale'); node.classList.remove('fresh');
      node.querySelector('[data-state]').textContent = 'Demo connection lost';
    });
    $('neuron-light').className = '';
    set('sources-count', 'Demo connection lost');
    set('sample-freshness', 'Last known value · connection lost');
  } finally { window.setTimeout(poll, 600); }
}

$('start').addEventListener('click', () => action('start', {loop: false}));
$('loop').addEventListener('click', () => action('start', {loop: true}));
$('reset').addEventListener('click', () => action('reset'));
$('inject').addEventListener('click', () => action('fault', {action: 'inject'}));
$('clear').addEventListener('click', () => action('fault', {action: 'clear'}));
$('attach').addEventListener('click', attachDashboard);
$('detach').addEventListener('click', detachDashboard);
$('fullscreen').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch { notice('This browser does not support full screen. Use its presentation or kiosk mode.'); }
});
window.addEventListener('pagehide', () => { if (client) client.end(true); });
const architecture = document.querySelector('.architecture');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let motionPreference = null;
try { motionPreference = localStorage.getItem('factory-demo-flow-motion'); } catch { /* Storage may be disabled. */ }
function applyMotionPreference() {
  const enabled = motionPreference === 'on' || (motionPreference !== 'off' && !reducedMotion.matches);
  architecture.classList.toggle('motion-enabled', enabled);
  $('flow-motion').setAttribute('aria-pressed', String(enabled));
  set('flow-motion', enabled ? 'Pause packets' : 'Animate packets');
}
$('flow-motion').addEventListener('click', () => {
  motionPreference = architecture.classList.contains('motion-enabled') ? 'off' : 'on';
  try { localStorage.setItem('factory-demo-flow-motion', motionPreference); } catch { /* Keep the choice for this page. */ }
  applyMotionPreference();
});
reducedMotion.addEventListener('change', applyMotionPreference);
applyMotionPreference();
const flowLines = [...document.querySelectorAll('.flow-line')];
const sizePackets = () => flowLines.forEach(line => {
  line.style.setProperty('--packet-travel', Math.max(0, line.clientHeight - 8) + 'px');
});
new ResizeObserver(sizePackets).observe(architecture);
sizePackets();
let mapVisible = true;
const updateMotion = () => architecture.classList.toggle('motion-paused', document.hidden || !mapVisible);
new IntersectionObserver(entries => {
  mapVisible = entries[0].isIntersecting;
  updateMotion();
}).observe(architecture);
document.addEventListener('visibilitychange', updateMotion);
window.setInterval(updateDashboard, 1000);
poll();
