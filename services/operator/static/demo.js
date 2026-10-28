'use strict';
const config = JSON.parse(document.getElementById('demo-config').textContent);
const UI = window.FactoryUI;
const names = UI.names;
const $ = id => document.getElementById(id);
const cardFor = device => $('asset-' + device);
const commandFilters = ['orders/+', 'orders/+/status', 'batches/+/record', '+/+/+/cmd/+',
  '+/+/+/control/#', '+/+/+/_demo/fault', '+/+/+/batch/+/phase/+/command', '+/+/+/state', '+/+/+/state-events']
  .map(suffix => config.prefix + '/' + suffix);
const telemetryFilter = config.prefix + '/+/+/+/telemetry';
const paused = {commands: false, telemetry: false};
const feeds = {
  commands: {lastMessage: 0, count: 0, rate: 0, subscribed: false, refused: []},
  telemetry: {lastMessage: 0, count: 0, rate: 0, subscribed: false, refused: []},
};
const feedback = new Map(), commandFeedback = new Map(), previousClaims = new Map(), inputLeases = new Map();
const turnHighlights = new Map(), ownRequests = new Map(), reservationStates = new Map(), retainedRows = new Map();
let state = null, pending = false, pollConnected = false, mqttClient = null, viewerPhase = 'connecting';
let telemetryLatest = new Map(), orderFeedback = null;

function text(node, value) {
  value = String(value ?? '');
  if (node.textContent !== value) node.textContent = value;
}
function announce(message) { text($('announcements'), message); }
function actionText(button, label) {
  const release = /^Release · (\d+)s$/.exec(label);
  if (!release) { text(button, label); return; }
  let timer = button.querySelector('[data-timer]');
  if (!timer) {
    timer = document.createElement('span');
    timer.dataset.timer = '';
    timer.setAttribute('aria-hidden', 'true');
    button.replaceChildren(document.createTextNode('Release'), timer);
  }
  text(timer, ` · ${release[1]}s`);
}
function notice(message) { $('notice').hidden = !message; text($('notice-message'), message); }
function blocked(button, reason) {
  button.disabled = false;
  button.setAttribute('aria-disabled', String(!!reason));
  button.dataset.reason = reason || '';
}
function showFeedback(device, message, kind = 'info', seconds = 7) {
  feedback.set(device, {text: message, kind, until: Date.now() + seconds * 1000});
  paintFeedback(device);
}
function paintFeedback(device) {
  const card = cardFor(device), local = feedback.get(device), command = commandFeedback.get(device);
  const item = local?.until > Date.now() ? local : state?.visitor.devices[device].mine && command?.until > Date.now() ? command : null;
  const result = card.querySelector('[data-command-result]');
  result.hidden = !item;
  result.dataset.kind = item?.kind || 'info';
  text(result, item?.text || '');
  // A transient reply replaces the overlay chip, keeping the illustration
  // region usable even in a short window. The heading action stays visible.
  card.querySelector('[data-claim-badge]').hidden = !!item || !card.dataset.claimBadge;
}
function showOrderFeedback(message) {
  orderFeedback = {text: message, until: Date.now() + 8000};
  if (state) render(state);
}

async function post(path, body = {}, device = null) {
  if (pending || !state || !pollConnected) {
    const reason = pending ? 'Waiting for the previous request.' : 'Factory connection unavailable.';
    if (device) showFeedback(device, reason); else showOrderFeedback(reason);
    return null;
  }
  const requestId = crypto.randomUUID();
  ownRequests.set(requestId, device);
  if (ownRequests.size > 100) ownRequests.delete(ownRequests.keys().next().value);
  pending = true;
  render(state);
  try {
    const response = await fetch(path, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Demo-CSRF': state.visitor.csrf},
      body: JSON.stringify({request_id: requestId, ...body}), signal: AbortSignal.timeout(10000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'The request could not be completed.');
    notice('');
    return result;
  } catch (error) {
    const message = error.name === 'TimeoutError' ? 'Request timed out. Check the device state before retrying.' : error.message;
    if (device) showFeedback(device, message, 'error', 12); else showOrderFeedback(message);
    return null;
  } finally {
    pending = false;
    if (state) render(state);
  }
}
async function deviceCommand(device, action, values = {}) {
  const plc = state?.plcs[device] || {};
  if (action === 'status') showFeedback(device, 'Requesting snapshot…', 'info', 12);
  else feedback.delete(device);
  const result = await post(`/api/devices/${device}/command`, {action, ...values,
    lease_id: state?.visitor.devices[device].lease_id, batch_id: plc.batch_id, phase: plc.phase}, device);
  if (result?.snapshot) showFeedback(device, UI.snapshotSummary(device, result.snapshot), 'snapshot', 8);
}

function updateInputs(device, claim, values) {
  const inputs = cardFor(device).querySelectorAll('[data-settings] input, [data-settings] select');
  if (!claim.mine || claim.kind === 'batch') {
    inputs.forEach(input => { input.value = ''; });
    inputLeases.delete(device);
  } else if (claim.owned && inputLeases.get(device) !== claim.lease_id) {
    const proposed = device === 'hvac-1'
      ? {temperature: values.temp_setpoint_c ?? 22, fan: values.fan_speed_pct ?? 50, mode: values.mode_cmd ?? 5, enabled: values.enable_cmd ?? 1}
      : {rpm: values.manual_rpm > 0 ? values.manual_rpm : 1500};
    inputs.forEach(input => { input.value = String(proposed[input.name]); });
    inputLeases.set(device, claim.lease_id);
  }
}

function render(data) {
  state = data;
  const visitor = data.visitor, connected = data.connected && pollConnected;
  const requestDevice = Object.keys(names).find(device => visitor.devices[device].mine || visitor.devices[device].position);
  const currentRequest = names[requestDevice];
  $('connection').className = 'connection ' + (connected && data.ready ? 'good' : 'bad');
  text($('connection-label'), !connected ? 'Device data unavailable' : data.ready ? 'Device data live' : 'Device data stale');
  $('connection').title = 'Factory monitoring connection · separate from this browser’s MQTT viewer';

  for (const device of Object.keys(names)) {
    const card = cardFor(device), asset = data.assets[device] || {}, values = asset.values || {};
    const plc = data.plcs[device] || {}, claim = visitor.devices[device], previous = previousClaims.get(device);
    const fresh = connected && !!asset.fresh, batch = claim.mine && claim.kind === 'batch';
    const machine = UI.machineState(device, {...asset, fresh}, plc);
    const presentation = UI.claimPresentation(claim, {fresh, connected, reachable: pollConnected, currentRequest, seconds: visitor.claim_seconds});
    const activeElement = document.activeElement;
    card.classList.toggle('fresh', fresh);
    card.classList.toggle('stale', !fresh);
    card.classList.toggle('claimed', claim.owned);
    card.classList.toggle('faulted', machine.fault);
    $('gateway-' + device).classList.toggle('live', fresh);
    const inFlight = claim.mine && claim.command?.status === 'sent';
    $('gateway-' + device).classList.toggle('command-pending', inFlight);
    text(card.querySelector('[data-metric]'), UI.number(values[device === 'hvac-1' ? 'supply_air_temp_c' : 'spindle_rpm'], device === 'hvac-1' ? 1 : 0));
    text(card.querySelector('[data-state]'), machine.text);
    const dot = card.querySelector('.asset-status-dot');
    dot.title = machine.fault ? 'Device fault' : fresh ? 'Fresh device telemetry' : 'Waiting for fresh device telemetry';
    dot.setAttribute('aria-label', dot.title);
    text(card.querySelector('[data-claim-state]'), presentation.description + (presentation.reason ? ' ' + presentation.reason : ''));

    if (claim.owned && !previous?.owned && claim.kind === 'manual') {
      turnHighlights.set(device, Date.now() + 5000);
      announce(`Your turn on ${names[device]}. ${claim.remaining} seconds of control.`);
      if (previous) { card.classList.add('new-turn'); setTimeout(() => card.classList.remove('new-turn'), 1000); }
    }
    if (previous?.mine && !claim.mine && !claim.position) {
      showFeedback(device, 'Turn ended · defaults restored', 'info', 8);
      commandFeedback.delete(device);
      announce(`Your turn on ${names[device]} ended. Device defaults restored.`);
    } else if (claim.position && previous?.position !== claim.position) {
      announce(`You are number ${claim.position} in the ${names[device]} queue.`);
    } else if (previous?.position && !claim.position && !claim.mine) {
      announce(`You left the ${names[device]} queue.`);
    }
    const chip = card.querySelector('[data-claim-badge]');
    const chipText = presentation.badge || (claim.owned && turnHighlights.get(device) > Date.now() ? `Your turn · ${claim.remaining}s` : '');
    card.dataset.claimBadge = chipText;
    text(chip, chipText);
    chip.dataset.kind = claim.owned ? 'mine' : presentation.kind;

    const claimButton = card.querySelector('[data-claim]'), release = card.querySelector('[data-release]');
    claimButton.hidden = presentation.release;
    release.hidden = !presentation.release;
    const actionButton = presentation.release ? release : claimButton;
    actionText(actionButton, presentation.button);
    blocked(actionButton, pending ? 'Waiting for the previous request.' : presentation.reason);
    actionButton.title = presentation.description;

    updateInputs(device, claim, values);
    card.querySelector('[data-settings]').hidden = batch;
    const writeReason = !connected ? 'Factory connection unavailable.' : !fresh ? 'Waiting for fresh device data.'
      : !claim.owned ? claim.mine ? presentation.reason : 'Claim this device before changing its settings.'
        : batch ? 'Use the batch controls while your batch is running.' : inFlight ? 'Waiting for the previous write to confirm.'
          : pending ? 'Waiting for the previous request.' : '';
    card.dataset.writeReason = writeReason;
    card.querySelectorAll('[data-settings] input, [data-settings] select').forEach(input => {
      input.disabled = !claim.owned || batch || !fresh;
      if (input.tagName === 'INPUT') input.readOnly = inFlight;
    });
    card.querySelectorAll('[data-write]').forEach(button => blocked(button, writeReason));
    blocked(card.querySelector('[data-action="status"]'), !connected ? 'Factory connection unavailable.' : pending ? 'Waiting for the previous request.' : '');
    const batchControls = card.querySelector('.batch-controls');
    if (batchControls) {
      batchControls.hidden = !batch;
      const allowed = {hold: ['running'], resume: ['holding'], inject: ['running'], clear: ['faulted']};
      batchControls.querySelectorAll('button').forEach(button => blocked(button,
        !claim.owned || !fresh ? 'Waiting for your batch and fresh device data.' : pending ? 'Waiting for the previous request.'
          : !allowed[button.dataset.action].includes(plc.state) ? `${button.textContent} is unavailable while the batch is ${plc.state || 'starting'}.` : ''));
    }
    if (claim.mine && claim.command) {
      const key = claim.command.id + ':' + claim.command.status;
      if (commandFeedback.get(device)?.key !== key) commandFeedback.set(device, {
        key, text: claim.command.status === 'observed' ? '✓ Confirmed in telemetry'
          : claim.command.status === 'failed' ? claim.command.error : 'Waiting for confirmation…',
        kind: claim.command.status === 'failed' ? 'error' : 'info',
        until: Date.now() + (claim.command.status === 'sent' ? 20000 : 8000),
      });
    }
    paintFeedback(device);
    if (card.contains(activeElement) && ((activeElement === claimButton && claimButton.hidden)
      || (activeElement === release && release.hidden) || (activeElement.matches('input,select') && activeElement.disabled))) actionButton.focus({preventScroll: true});
    previousClaims.set(device, {...claim});
  }

  const selectedDevice = $('order-device').value, selected = visitor.devices[selectedDevice];
  const orderReason = !connected ? 'Waiting for device data.' : visitor.has_request ? `Finish with ${currentRequest} first.`
    : selected.waiting >= selected.capacity ? 'Queue full · try another CNC.' : pending ? 'Sending request…' : '';
  blocked($('send-order'), orderReason);
  const orderHint = orderFeedback?.until > Date.now() ? orderFeedback.text : orderReason;
  $('order-hint').hidden = !orderHint;
  text($('order-hint'), orderHint);
  $('order-form').closest('.applications').classList.toggle('has-reason', !!orderHint);
  for (const option of $('order-device').options) text(option, names[option.value] + (visitor.devices[option.value].busy ? ' · in use' : ''));
  text($('order-summary'), UI.batchSummary(data.orders || []));
  document.querySelectorAll('[data-traffic]').forEach(node => {
    node.classList.toggle('live', connected && node.dataset.traffic.split(' ').some(key => data.activity[key]));
  });
}

function messageLabel(topic, body) {
  const device = names[topic.split('/')[4]] || names[body.plc_id];
  const withDevice = label => device ? `${device} · ${label}` : label;
  if (topic.endsWith('/cmd/req')) return withDevice('write → Neuron');
  if (topic.endsWith('/cmd/ack')) return withDevice(body.error === 0 ? 'write acknowledged' : 'write rejected');
  if (topic.endsWith('/control/request')) return `${ownRequests.has(body.id) ? 'You' : 'Visitor'} → ${device || 'device'} · ${body.action === 'rpm' ? 'set RPM' : body.action === 'hvac' ? 'set HVAC' : body.action}`;
  if (topic.endsWith('/control/response')) return withDevice('snapshot · ' + (body.fresh ? 'fresh' : 'stale'));
  if (topic.endsWith('/control/reservation/ack')) return withDevice(body.accepted ? 'MES reservation accepted' : 'MES · device busy');
  if (topic.endsWith('/control/reservation')) return withDevice(body.release ? 'reservation released' : 'reservation requested');
  if (topic.endsWith('/control/released')) return withDevice('defaults restored');
  if (topic.endsWith('/status')) return withDevice('MES · order ' + (body.state || 'updated'));
  if (topic.includes('/orders/')) return withDevice(body.cancel ? 'ERP · end batch' : 'ERP → MES · batch order');
  if (topic.endsWith('/record')) return withDevice('MES · batch record');
  if (topic.endsWith('/command')) return withDevice('MES → ' + (body.action || 'command'));
  if (topic.endsWith('/_demo/fault')) return withDevice(body.action + ' fault');
  if (topic.endsWith('/state-events')) return withDevice('device event');
  return withDevice(UI.title(body.state || 'state'));
}

function addMessage(log, item, telemetry = false) {
  const key = item.topic + ':' + JSON.stringify(item.body);
  if (item.retained) {
    const previous = retainedRows.get(item.topic);
    if (previous?.key === key && previous.row.isConnected) return;
    if (previous?.retained) previous.row.remove();
  }
  log.querySelector('.empty')?.remove();
  const nearBottom = log.scrollHeight - log.clientHeight - log.scrollTop < 50;
  const row = document.createElement('li');
  row.className = 'packet ' + (telemetry ? 'telemetry-packet' : item.topic.endsWith('/cmd/req') || item.topic.endsWith('/control/request') ? 'outgoing' : 'incoming');
  row.classList.toggle('your-message', ownRequests.has(item.body.id || item.body.uuid));
  const details = document.createElement('details'), summary = document.createElement('summary');
  const heading = document.createElement('span'); heading.className = 'packet-heading';
  const name = document.createElement('strong'), time = document.createElement('time');
  name.textContent = telemetry ? names[item.topic.split('/')[4]] + ' · telemetry' : messageLabel(item.topic, item.body);
  time.textContent = item.retained ? 'retained · on connect' : new Date(item.time).toLocaleTimeString();
  time.title = item.retained ? 'Previously retained state delivered when this viewer subscribed.' : 'Received by this viewer';
  heading.append(name, time);
  const metadata = document.createElement('span'); metadata.className = 'packet-meta';
  const topic = document.createElement('code'); topic.className = 'packet-topic';
  topic.textContent = item.topic.startsWith(config.prefix + '/') ? '…/' + item.topic.slice(config.prefix.length + 1) : item.topic;
  topic.title = item.topic;
  const cue = document.createElement('span'); cue.className = 'json-cue'; cue.setAttribute('aria-hidden', 'true');
  cue.append(document.createTextNode('JSON '));
  const chevron = document.createElement('span'); chevron.textContent = '›'; cue.append(chevron);
  metadata.append(topic, cue);
  const brief = document.createElement('span'); brief.className = 'packet-brief';
  const values = item.body.values || {};
  brief.textContent = telemetry ? typeof values.spindle_rpm === 'number'
    ? `${UI.number(values.spindle_rpm)} rpm · ${UI.number(values.power_kw, 1)} kW · ${UI.title(values.state || '')}`
    : `${UI.number(values.supply_air_temp_c, 1)} °C · fan ${UI.number(values.fan_speed_pct)}% · ${UI.number(values.fan_kw, 1)} kW`
    : UI.writeSummary(item.body) || (item.body.phase ? 'Phase · ' + UI.title(item.body.phase) : '');
  brief.hidden = !brief.textContent;
  summary.append(heading, metadata, brief);
  const fullTopic = document.createElement('code'); fullTopic.className = 'full-topic'; fullTopic.textContent = item.topic;
  const payload = document.createElement('pre'); payload.textContent = JSON.stringify(item.body, null, 2);
  details.append(summary, fullTopic, payload); row.append(details); log.append(row);
  // Remember live states too, so replaying their retained copy on reconnect
  // does not turn the same event into a second history entry.
  retainedRows.set(item.topic, {key, row, retained: item.retained});
  if (retainedRows.size > 100) retainedRows.delete(retainedRows.keys().next().value);
  while (log.children.length > (telemetry ? 60 : 100)) log.firstElementChild.remove();
  if (nearBottom) log.scrollTop = log.scrollHeight;
}

function showFilters(id, filters, refused = []) {
  $(id).replaceChildren(...filters.map(filter => {
    const code = document.createElement('code'); code.textContent = filter;
    code.classList.toggle('filter-refused', refused.includes(filter));
    code.title = refused.includes(filter) ? 'Subscription refused' : filter;
    return code;
  }));
}
function paintTraffic() {
  for (const kind of ['commands', 'telemetry']) {
    const status = UI.feedState({kind, phase: viewerPhase, paused: paused[kind], ...feeds[kind], refused: feeds[kind].refused.length});
    const node = $(kind === 'commands' ? 'commands-state' : 'telemetry-rate');
    text(node, status.text); node.dataset.tone = status.tone;
    node.title = viewerPhase === 'standby' ? 'The bounded MQTT viewer is active in another tab. Choose Watch here to move it here.'
      : paused[kind] ? 'This message window is paused; device monitoring and controls continue.' : 'This browser’s MQTT feed';
  }
  $('mqtt-light').classList.toggle('live', viewerPhase === 'connected' && feeds.commands.subscribed && !paused.commands);
  $('watch-here').hidden = viewerPhase !== 'standby';
  text($('mqtt-status'), viewerPhase === 'standby' ? 'MQTT is watched in another tab. Watch here to move the feed here.'
    : viewerPhase === 'connected' ? 'MQTT viewer connected. Each message expands to JSON.' : 'MQTT viewer reconnecting. Device monitoring uses a separate connection.');
}

async function connectTraffic() {
  try {
    mqttClient = await demoMqttConnect(config, 'demo');
    function subscribe() {
      viewerPhase = 'connected';
      const filters = [...commandFilters, telemetryFilter];
      mqttClient.subscribe(filters, {qos: 1}, (error, granted) => {
        for (const kind of ['commands', 'telemetry']) {
          const requested = kind === 'commands' ? commandFilters : [telemetryFilter];
          feeds[kind].refused = requested.filter(filter => !granted?.some(item => item.topic === filter && item.qos < 128));
          feeds[kind].subscribed = !!granted?.some(item => requested.includes(item.topic) && item.qos < 128);
        }
        text($('command-filter-count'), feeds.commands.refused.length ? `${feeds.commands.refused.length} of ${commandFilters.length} refused ·` : commandFilters.length);
        $('command-filter-button').classList.toggle('filter-refused', !!feeds.commands.refused.length);
        $('telemetry-filters').classList.toggle('filter-refused', !!feeds.telemetry.refused.length);
        showFilters('command-filters', commandFilters, feeds.commands.refused);
        paintTraffic();
      });
      paintTraffic();
    }
    mqttClient.on('connect', subscribe);
    if (mqttClient.connected) subscribe();
    mqttClient.on('viewer-standby', () => { viewerPhase = 'standby'; paintTraffic(); });
    mqttClient.on('viewer-resuming', () => { viewerPhase = 'connecting'; paintTraffic(); });
    mqttClient.on('message', (topic, payload, packet) => {
      if (!payload.length) return;
      let body;
      try { body = JSON.parse(payload.toString()); } catch { return; }
      if (!body || typeof body !== 'object' || Array.isArray(body)) return;
      const kind = topic.endsWith('/telemetry') ? 'telemetry' : 'commands';
      feeds[kind].subscribed = true;
      if (!packet.retain) { feeds[kind].lastMessage = Date.now(); feeds[kind].count++; }
      if (topic.endsWith('/control/response') && ownRequests.has(body.id)) {
        const device = names[topic.split('/')[4]] && topic.split('/')[4];
        if (device) showFeedback(device, UI.snapshotSummary(device, body), 'snapshot', 8);
      }
      if (paused[kind]) return;
      const item = {topic, body, retained: !!packet.retain, time: Date.now()};
      if (kind === 'telemetry') telemetryLatest.set(topic, item);
      else {
        if (topic.includes('/control/reservation')) {
          const semantic = JSON.stringify([body.id, body.accepted, body.release, body.kind, body.order_id, body.reason]);
          if (reservationStates.get(topic) === semantic) return;
          reservationStates.set(topic, semantic);
          if (reservationStates.size > 30) reservationStates.delete(reservationStates.keys().next().value);
        }
        addMessage($('command-log'), item);
      }
    });
    mqttClient.on('close', () => {
      if (viewerPhase !== 'standby') viewerPhase = 'reconnecting';
      feeds.commands.subscribed = feeds.telemetry.subscribed = false;
      telemetryLatest.clear();
      paintTraffic();
    });
    mqttClient.on('error', error => { $('commands-state').title = 'MQTT viewer: ' + error.message; });
  } catch (error) {
    viewerPhase = 'reconnecting';
    paintTraffic();
    $('commands-state').title = error.message;
    setTimeout(connectTraffic, 5000);
  }
}
setInterval(() => {
  for (const feed of Object.values(feeds)) { feed.rate = feed.count; feed.count = 0; }
  if (!paused.telemetry) for (const item of telemetryLatest.values()) addMessage($('telemetry-log'), item, true);
  telemetryLatest.clear();
  paintTraffic();
  for (const device of Object.keys(names)) paintFeedback(device);
}, 1000);

async function poll() {
  try {
    const response = await fetch('/api/demo/state', {cache: 'no-store', signal: AbortSignal.timeout(4500)});
    if (!response.ok) throw new Error('Factory connection unavailable');
    pollConnected = true;
    render(await response.json());
  } catch {
    pollConnected = false;
    if (state) render(state);
    else { $('connection').className = 'connection bad'; text($('connection-label'), 'Device data unavailable'); }
  } finally { setTimeout(poll, 700); }
}

for (const device of Object.keys(names)) {
  const card = cardFor(device);
  function explain(button) {
    if (button.getAttribute('aria-disabled') !== 'true') return false;
    showFeedback(device, button.dataset.reason || 'Waiting for device data.');
    return true;
  }
  card.querySelector('[data-claim]').addEventListener('click', async event => {
    if (explain(event.currentTarget)) return;
    feedback.delete(device);
    await post(`/api/devices/${device}/claim`, {}, device);
  });
  card.querySelector('[data-release]').addEventListener('click', event => {
    if (!explain(event.currentTarget)) post(`/api/devices/${device}/release`, {lease_id: state?.visitor.devices[device].lease_id}, device);
  });
  card.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => {
    if (explain(button)) return;
    deviceCommand(device, button.dataset.action === 'stop' ? 'rpm' : button.dataset.action, button.dataset.action === 'stop' ? {value: 0} : {});
  }));
  card.querySelector('[data-settings]').addEventListener('submit', event => {
    event.preventDefault();
    if (card.dataset.writeReason || !state) { showFeedback(device, card.dataset.writeReason || 'Waiting for device data.'); return; }
    const values = Object.fromEntries([...new FormData(event.currentTarget)].map(([key, value]) => [key, Number(value)]));
    deviceCommand(device, device === 'hvac-1' ? 'hvac' : 'rpm', device === 'hvac-1' ? values : {value: values.rpm});
  });
}
$('order-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('send-order').getAttribute('aria-disabled') === 'true') { showOrderFeedback($('send-order').dataset.reason || 'Waiting for device data.'); return; }
  orderFeedback = null;
  await post('/api/place-order', {plc_id: $('order-device').value, recipe_id: $('order-recipe').value, qty: 1});
});
$('order-device').addEventListener('change', () => { orderFeedback = null; if (state) render(state); });
for (const kind of ['commands', 'telemetry']) {
  $('pause-' + kind).addEventListener('click', () => {
    paused[kind] = !paused[kind];
    text($('pause-' + kind), paused[kind] ? 'Resume' : 'Pause');
    $('pause-' + kind).setAttribute('aria-pressed', String(paused[kind]));
    if (kind === 'telemetry') telemetryLatest.clear();
    paintTraffic();
  });
  $('clear-' + kind).addEventListener('click', () => {
    $(kind === 'commands' ? 'command-log' : 'telemetry-log').replaceChildren();
    if (kind === 'commands') { retainedRows.clear(); reservationStates.clear(); }
  });
}
$('watch-here').addEventListener('click', () => {
  mqttClient?.watchHere();
  $('pause-commands').focus({preventScroll: true});
});
$('dismiss-notice').addEventListener('click', () => notice(''));
showFilters('command-filters', commandFilters);
showFilters('telemetry-filters', [telemetryFilter]);
poll();
connectTraffic();
window.addEventListener('pagehide', () => mqttClient?.end(true));
