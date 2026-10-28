/* Layout-only previews. All commands and MQTT messages are simulated locally. */
(() => {
  'use strict';

  const prefix = 'acme-mfg/plant-1';
  const devices = [
    { id: 'plc-1', name: 'CNC 01', number: '01', protocol: 'OPC UA', image: 'cnc-device.svg', value: 0, target: 1500, unit: 'rpm', status: 'Idle' },
    { id: 'plc-2', name: 'CNC 02', number: '02', protocol: 'OPC UA', image: 'cnc-device.svg', value: 0, target: 1500, unit: 'rpm', status: 'Idle' },
    { id: 'hvac-1', name: 'Building HVAC', short: 'HVAC', number: '03', protocol: 'BACnet/IP', image: 'hvac-device.svg', value: 24.1, temperature: 22, fan: 50, mode: '5', enabled: '1', unit: '°C', status: 'Ventilating' },
  ];
  const descriptions = {
    a: 'Compact controls on every device',
    b: 'Select a device, use the shared tray',
    c: 'Send commands beside the MQTT conversation',
  };
  const filters = [
    `${prefix}/orders/+`, `${prefix}/orders/+/status`, `${prefix}/batches/+/record`,
    `${prefix}/+/+/+/cmd/+`, `${prefix}/+/+/+/control/#`, `${prefix}/+/+/+/_demo/fault`,
    `${prefix}/+/+/+/batch/+/phase/+/command`, `${prefix}/+/+/+/state`, `${prefix}/+/+/+/state-events`,
  ];
  const stage = document.getElementById('draft-stage');
  const state = {
    variant: 'a', selected: 'plc-1', claim: null, batch: null,
    paused: { commands: false, telemetry: false }, feedback: {},
    orderDevice: 'plc-1', recipe: 'program-quick', lastBatch: 'Latest batch completed',
  };
  const logs = { commands: [], telemetry: [] };
  let tick = 0;
  let toastTimer;

  const escape = (value) => String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
  const deviceById = (id) => devices.find((device) => device.id === id);
  const topicBase = (id) => `${prefix}/${id === 'hvac-1' ? 'utilities/compressor-room' : 'machining/line-1'}/${id}`;
  const owns = (id) => state.claim?.device === id && state.claim.until > Date.now();
  const hasBatch = (id) => state.batch?.device === id;
  const canClaim = (id) => !state.batch && (!state.claim || owns(id));
  const blocked = (id) => owns(id) ? '' : ' disabled';
  const short = (device) => device.short || device.name;
  const currentTime = () => new Date().toLocaleTimeString([], { hour12: false });

  function applications() {
    return `<section class="applications" aria-label="Business applications">
      <form class="erp" id="erp-order"><h2>ERP</h2>
        <select name="device" aria-label="Batch machine">${devices.filter((device) => device.id !== 'hvac-1').map((device) => `<option value="${device.id}"${state.orderDevice === device.id ? ' selected' : ''}>${device.name}</option>`).join('')}</select>
        <select name="recipe" aria-label="Batch recipe"><option value="program-quick"${state.recipe === 'program-quick' ? ' selected' : ''}>Quick program</option><option value="program-a"${state.recipe === 'program-a' ? ' selected' : ''}>Full program</option></select>
        <button class="primary" type="submit">Send batch ↗</button>
      </form>
      <a href="http://localhost:8090/" target="_blank" rel="noopener" class="mes-status"><strong>MES ↗</strong><span data-batch-status>${escape(state.lastBatch)}</span></a>
      <a class="analytics" href="http://localhost:3000/" target="_blank" rel="noopener">Analytics ↗</a>
    </section>`;
  }

  function wires(className) {
    return `<div class="${className}" aria-hidden="true">${devices.map(() => '<span class="wire"><i></i><i class="down"></i></span>').join('')}</div>`;
  }

  function broker() {
    return `<section class="broker" aria-label="EMQX MQTT backbone">
      <div class="broker-main"><img src="../brands/emqx.svg" width="40" height="40" alt=""><div><p class="broker-kicker">THE MQTT DATA BACKBONE</p><h2><a href="http://localhost:18083/" target="_blank" rel="noopener">EMQX Enterprise <span>↗</span></a></h2></div><span class="broker-tag">JSON / MQTT</span></div>
      <div class="broker-foot"><a href="/uns" target="_blank" rel="noopener"><code>${prefix}/# ↗</code></a><span class="flow-key"><span><b class="up">↑</b>Data &amp; events</span><span><b class="down">↓</b>Commands</span></span></div>
    </section>`;
  }

  function gateways() {
    return `<div class="gateways">${devices.map((device) => `<a class="gateway" href="http://localhost:${8084 + Number(device.number)}/" target="_blank" rel="noopener"><img src="../brands/neuron.svg" alt="" width="23" height="23"><span><strong>Neuron ${device.number}</strong><small>${device.protocol} ↔ MQTT</small></span></a>`).join('')}</div>`;
  }

  function reading(device) {
    return `<span class="reading"><strong data-value="${device.id}">${device.value}</strong><span class="unit">${device.unit}</span><span class="device-state" data-device-status="${device.id}">${device.status}</span></span>`;
  }

  function claimText(id) {
    if (hasBatch(id)) return state.batch.fault ? 'Batch · faulted' : state.batch.paused ? 'Batch · held' : 'Batch · roughing';
    return owns(id) ? `Yours · ${Math.max(0, Math.ceil((state.claim.until - Date.now()) / 1000))}s` : 'Available';
  }

  function ownership(device, selectable) {
    const active = owns(device.id);
    if (state.variant === 'c') return `<div class="ownership"><span class="${active ? 'claimed' : ''}" data-owner="${device.id}">${claimText(device.id)}</span><span class="control-location">${state.selected === device.id ? 'Controls on the right →' : 'Select to control'}</span></div>`;
    return `<div class="ownership"><span class="${active ? 'claimed' : ''}" data-owner="${device.id}">${claimText(device.id)}</span>
      ${hasBatch(device.id) ? `<button data-action="end" data-device="${device.id}">End batch</button>` : `<button class="claim" data-action="claim" data-device="${device.id}"${canClaim(device.id) ? '' : ' disabled'}>${active ? 'Release' : 'Claim · 30s'}</button>`}
      ${selectable ? '' : `<button data-action="status" data-device="${device.id}">Status ↗</button>`}
    </div>`;
  }

  function batchControls(device) {
    return `<div class="settings-form batch-controls"><button data-action="hold" data-device="${device.id}"${state.batch.paused ? ' disabled' : ''}>Hold</button><button data-action="resume" data-device="${device.id}"${state.batch.paused ? '' : ' disabled'}>Resume</button><button data-action="inject" data-device="${device.id}"${state.batch.fault ? ' disabled' : ''}>Inject fault</button><button data-action="clear-fault" data-device="${device.id}"${state.batch.fault ? '' : ' disabled'}>Clear fault</button></div>`;
  }

  function settings(device, context = 'inline') {
    const disabled = blocked(device.id);
    if (hasBatch(device.id)) return batchControls(device);
    const feedback = state.feedback[device.id] || '';
    const note = `<p class="settings-note${feedback ? ' confirmed' : ''}" role="status">${escape(feedback)}</p>`;
    if (device.id !== 'hvac-1') return `<form class="settings-form cnc-form" data-settings="${device.id}"><label class="rpm-field">Spindle RPM<input type="number" name="rpm" min="0" max="6000" step="100" value="${device.target}" aria-label="${device.name} spindle RPM"${disabled}></label><button type="submit"${disabled}>Set RPM ↗</button><button type="button" data-action="stop" data-device="${device.id}"${disabled}>Stop</button>${note}</form>`;
    return `<form class="settings-form hvac-form" data-settings="${device.id}" data-context="${context}">
      <label class="temperature-field">Temp · °C<input type="number" name="temperature" min="18" max="28" step="0.5" value="${device.temperature}" aria-label="HVAC temperature"${disabled}></label>
      <label class="fan-field">Fan · %<input type="number" name="fan" min="0" max="100" step="5" value="${device.fan}" aria-label="HVAC fan speed"${disabled}></label>
      <label class="mode-field">Mode<select name="mode" aria-label="HVAC mode"${disabled}>${[['5', 'Auto'], ['2', 'Cool'], ['3', 'Heat'], ['4', 'Fan'], ['1', 'Off']].map(([value, name]) => `<option value="${value}"${device.mode === value ? ' selected' : ''}>${name}</option>`).join('')}</select></label>
      <label class="unit-field">Unit<select name="enabled" aria-label="HVAC enabled"${disabled}><option value="1"${device.enabled === '1' ? ' selected' : ''}>Enabled</option><option value="0"${device.enabled === '0' ? ' selected' : ''}>Disabled</option></select></label>
      <button class="apply" type="submit"${disabled}>Apply ↗</button>${context === 'composer' ? '<button class="quiet more-settings" type="button" popovertarget="hvac-advanced">Mode / unit ⌄</button>' : ''}${note}</form>`;
  }

  function deviceCards(selectable = false) {
    return `<div class="devices">${devices.map((device) => {
      const selected = state.selected === device.id;
      const visual = `<span class="device-heading"><span class="device-name">${device.name}</span>${selectable && selected ? '<span class="selection-label">Selected</span>' : '<i class="status-dot" aria-label="Online"></i>'}</span><img class="illustration" src="${device.image}" alt="${device.id === 'hvac-1' ? 'Illustrated HVAC unit' : 'Illustrated CNC machine'}">${reading(device)}`;
      return `<article class="device${selectable ? ' selectable' : ''}${selected ? ' selected' : ''}" aria-label="${device.name}">
        ${selectable ? `<button class="device-pick" data-action="select" data-device="${device.id}" aria-pressed="${selected}" aria-label="Select ${device.name}">${visual}</button>` : `<div class="device-visual">${visual}</div>`}
        ${ownership(device, selectable)}
        ${selectable ? '' : `<div class="inline-controls">${settings(device)}${device.id === 'hvac-1' ? '<div class="advanced"><button class="quiet" popovertarget="hvac-advanced">Mode &amp; unit ⌄</button></div>' : ''}</div>`}
      </article>`;
    }).join('')}</div>`;
  }

  function tray() {
    const device = deviceById(state.selected);
    return `<section class="control-tray" aria-label="Selected device controls"><div class="tray-heading"><h2>${device.name}</h2><span>${hasBatch(device.id) ? 'Batch controls' : 'Device controls'}</span><div class="tray-actions">${device.id === 'hvac-1' ? '<button class="narrow-advanced quiet" popovertarget="hvac-advanced">Mode &amp; unit ⌄</button>' : ''}<button data-action="status" data-device="${device.id}">Request status ↗</button></div></div>${settings(device, 'tray')}</section>`;
  }

  function composer() {
    const device = deviceById(state.selected);
    return `<section class="composer" aria-label="Command composer"><div class="composer-heading"><h3>Send to</h3><select id="command-device" aria-label="Command device">${devices.map((option) => `<option value="${option.id}"${option.id === device.id ? ' selected' : ''}>${short(option)}</option>`).join('')}</select><button class="quiet" data-action="status" data-device="${device.id}">Status ↗</button>${hasBatch(device.id) ? `<button class="claim" data-action="end" data-device="${device.id}">End batch</button>` : `<button class="claim" data-action="claim" data-device="${device.id}"${canClaim(device.id) ? '' : ' disabled'}>${owns(device.id) ? 'Release' : 'Claim · 30s'}</button>`}</div>${settings(device, 'composer')}</section>`;
  }

  function traffic(composerVisible = false) {
    return `<aside class="traffic" aria-label="MQTT traffic">
      <section class="traffic-panel command-panel" aria-label="Commands and events"><div class="traffic-heading"><h2>Commands &amp; events</h2><div class="traffic-actions"><button class="quiet" data-pause="commands" aria-pressed="${state.paused.commands}">${state.paused.commands ? 'Resume' : 'Pause'}</button><button class="quiet" data-clear="commands">Clear</button></div></div><div class="topic-line"><code>${prefix}/…</code><button data-filters="commands" popovertarget="topic-filters">9 filters ⌄</button></div><ol class="traffic-log" id="commands-log" aria-label="Command messages"></ol>${composerVisible ? composer() : ''}</section>
      <section class="traffic-panel telemetry-panel" aria-label="Telemetry"><div class="traffic-heading"><h2>Telemetry <span class="rate">3/s</span></h2><div class="traffic-actions"><button class="quiet" data-pause="telemetry" aria-pressed="${state.paused.telemetry}">${state.paused.telemetry ? 'Resume' : 'Pause'}</button><button class="quiet" data-clear="telemetry">Clear</button></div></div><div class="topic-line"><code>${prefix}/+/+/+/telemetry</code><button data-filters="telemetry" popovertarget="topic-filters" aria-label="Show telemetry subscription topic filter">⌄</button></div><ol class="traffic-log" id="telemetry-log" aria-label="Telemetry messages"></ol></section>
    </aside>`;
  }

  function advanced() {
    return `<section class="advanced-popover" id="hvac-advanced" popover><h2>HVAC · mode &amp; unit</h2>${settings(deviceById('hvac-1'), 'advanced')}</section>`;
  }

  function render() {
    const variant = new URLSearchParams(location.search).get('v');
    state.variant = ['a', 'b', 'c'].includes(variant) ? variant : 'a';
    // The selected design is now the live main page.
    if (state.variant === 'a') { location.replace('/'); return; }
    stage.className = `stage variant-${state.variant}`;
    document.title = `Draft ${state.variant.toUpperCase()} · Factory demo`;
    document.getElementById('draft-description').textContent = descriptions[state.variant];
    document.querySelectorAll('[data-variant]').forEach((link) => {
      if (link.dataset.variant === state.variant) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    const equipment = `${wires('edge-wires')}${gateways()}${wires('device-wires')}${deviceCards(state.variant !== 'a')}`;
    if (state.variant === 'c') {
      stage.innerHTML = `${applications()}${broker()}<div class="lower-layout"><section class="factory" aria-label="Factory devices">${equipment}</section>${traffic(true)}</div>${advanced()}`;
    } else {
      stage.innerHTML = `<section class="factory" aria-label="Factory controls">${applications()}${wires('app-wires')}${broker()}${equipment}${state.variant === 'b' ? tray() : ''}</section>${traffic()}${advanced()}`;
    }
    paintLog('commands');
    paintLog('telemetry');
  }

  function paintLog(kind) {
    const target = document.getElementById(`${kind}-log`);
    if (!target) return;
    const expanded = new Set(Array.from(target.querySelectorAll('details[open]'), (details) => details.dataset.packet));
    target.innerHTML = logs[kind].length ? logs[kind].slice(0, 40).map((packet) => {
      const key = `${packet.topic}:${packet.time}:${packet.label}`;
      return `<li class="packet${packet.outgoing ? ' outgoing' : ''}"><details data-packet="${escape(key)}"${expanded.has(key) ? ' open' : ''}><summary><span class="packet-title"><span>${escape(packet.label)}</span><time>${escape(packet.time)}</time></span><code title="${escape(packet.topic)}">${escape(packet.topic.replace(prefix, '…'))}</code>${packet.brief ? `<p class="packet-brief">${escape(packet.brief)}</p>` : ''}</summary><pre>${escape(JSON.stringify({ topic: packet.topic, payload: packet.payload }, null, 2))}</pre></details></li>`;
    }).join('') : '<li class="empty-log">Waiting for the next message…</li>';
  }

  function addMessage(kind, packet) {
    if (state.paused[kind]) return;
    logs[kind].unshift({ time: currentTime(), ...packet });
    logs[kind] = logs[kind].slice(0, 40);
    paintLog(kind);
  }

  function telemetry(device) {
    const payload = device.id === 'hvac-1' ? { values: { supply_air_temp_c: device.value, fan_speed_pct: device.fan, mode: Number(device.mode), enabled: Number(device.enabled) } } : { values: { spindle_rpm: device.value, power_kw: device.value ? 2.3 : 0, phase: device.status.toLowerCase() } };
    addMessage('telemetry', { label: `${device.name} · telemetry`, topic: `${topicBase(device.id)}/telemetry`, payload, brief: device.id === 'hvac-1' ? `${device.value} °C · fan ${device.fan}%` : `${device.value.toLocaleString()} rpm · ${device.status.toLowerCase()}` });
  }

  function toast(message) {
    const target = document.getElementById('toast');
    clearTimeout(toastTimer);
    target.textContent = message;
    target.hidden = false;
    toastTimer = setTimeout(() => { target.hidden = true; }, 3000);
  }

  function release() {
    if (!state.claim) return;
    const device = deviceById(state.claim.device);
    if (device.id !== 'hvac-1') { device.value = 0; device.status = 'Idle'; }
    else { device.temperature = 22; device.fan = 50; device.mode = '5'; device.enabled = '1'; }
    state.claim = null;
    state.feedback[device.id] = '';
    addMessage('commands', { label: `${short(device)} · control released`, topic: `${topicBase(device.id)}/control/released`, payload: { released: true, defaults_restored: true } });
    telemetry(device);
  }

  function claim(id) {
    if (owns(id)) { release(); render(); return; }
    if (!canClaim(id)) { toast('Release your current device first.'); return; }
    state.claim = { device: id, until: Date.now() + 30000 };
    state.selected = id;
    state.feedback[id] = '';
    addMessage('commands', { label: `You → ${short(deviceById(id))} · claim`, topic: `${topicBase(id)}/control/claim`, payload: { expires_in: 30 }, outgoing: true });
    render();
  }

  function recordCommand(device, command, values) {
    state.feedback[device.id] = '✓ Preview updated';
    toast(`${device.name} · preview updated`);
    addMessage('commands', { label: `You → ${short(device)} · ${command}`, topic: `${topicBase(device.id)}/control/request`, payload: { command, ...values }, outgoing: true });
    addMessage('commands', { label: `${short(device)} · write acknowledged`, topic: `${topicBase(device.id)}/cmd/ack`, payload: { error: 0 } });
    telemetry(device);
  }

  function startBatch(form) {
    const data = new FormData(form);
    state.orderDevice = data.get('device');
    state.recipe = data.get('recipe');
    if (state.claim || state.batch) { toast('Release your device or end the current batch first.'); return; }
    const device = deviceById(state.orderDevice);
    state.selected = device.id;
    state.batch = { device: device.id, until: Date.now() + 18000, paused: false, fault: false };
    state.lastBatch = `${device.name} · roughing`;
    device.value = 2400; device.status = 'Roughing';
    addMessage('commands', { label: `ERP → MES · batch order`, topic: `${prefix}/orders/preview-001`, outgoing: true, payload: { recipe: state.recipe, quantity: 1, device: device.id } });
    addMessage('commands', { label: `MES → ${device.name} · start roughing`, topic: `${topicBase(device.id)}/batch/preview-001/phase/rough/command`, payload: { command: 'start', phase: 'rough' } });
    telemetry(device);
    render();
    toast('Preview batch started. Try Hold or Inject fault.');
  }

  function endBatch(completed = false) {
    if (!state.batch) return;
    const device = deviceById(state.batch.device);
    state.batch = null;
    state.feedback[device.id] = '';
    state.lastBatch = completed ? 'Latest batch completed' : 'Batch ended';
    device.value = 0; device.status = 'Idle';
    addMessage('commands', { label: `${device.name} · batch ${completed ? 'completed' : 'ended'}`, topic: `${prefix}/orders/preview-001/status`, payload: { state: completed ? 'completed' : 'cancelled' } });
    telemetry(device);
    render();
  }

  stage.addEventListener('submit', (event) => {
    event.preventDefault();
    const form = event.target;
    if (form.id === 'erp-order') { startBatch(form); return; }
    const id = form.dataset.settings;
    if (!id || !owns(id)) return;
    const device = deviceById(id);
    const data = new FormData(form);
    if (id === 'hvac-1') {
      if (form.dataset.context === 'advanced') {
        device.mode = String(data.get('mode'));
        device.enabled = String(data.get('enabled'));
      } else {
        device.temperature = Number(data.get('temperature'));
        device.fan = Number(data.get('fan'));
        if (form.dataset.context === 'tray') {
          device.mode = String(data.get('mode'));
          device.enabled = String(data.get('enabled'));
        }
      }
      device.status = device.enabled === '0' ? 'Disabled' : 'Ventilating';
      recordCommand(device, 'set HVAC', { temperature: device.temperature, fan: device.fan, mode: Number(device.mode), enabled: Number(device.enabled) });
    } else {
      device.target = Number(data.get('rpm'));
      device.value = device.target;
      device.status = device.value ? 'Manual' : 'Idle';
      recordCommand(device, 'set RPM', { rpm: device.target });
    }
    render();
  });

  stage.addEventListener('change', (event) => {
    if (event.target.id === 'command-device') { state.selected = event.target.value; render(); }
  });

  stage.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button) return;
    const { action, device: id, pause, clear, filters: filterKind } = button.dataset;
    if (pause) { state.paused[pause] = !state.paused[pause]; button.textContent = state.paused[pause] ? 'Resume' : 'Pause'; button.setAttribute('aria-pressed', String(state.paused[pause])); return; }
    if (clear) { logs[clear] = []; paintLog(clear); return; }
    if (filterKind) { document.getElementById('filter-list').innerHTML = (filterKind === 'telemetry' ? [`${prefix}/+/+/+/telemetry`] : filters).map((filter) => `<code>${escape(filter)}</code>`).join(''); return; }
    if (!action || !id) return;
    const device = deviceById(id);
    if (action === 'select') { state.selected = id; render(); return; }
    if (action === 'claim') { claim(id); return; }
    if (action === 'status') {
      addMessage('commands', { label: `You → ${short(device)} · request status`, topic: `${topicBase(id)}/control/request`, payload: { command: 'status' }, outgoing: true });
      addMessage('commands', { label: `${short(device)} · status snapshot`, topic: `${topicBase(id)}/control/response`, payload: { state: device.status, reading: device.value, unit: device.unit }, brief: `${device.value} ${device.unit} · ${device.status.toLowerCase()}` });
      toast(`${device.name} · ${device.value} ${device.unit} · ${device.status.toLowerCase()}`);
      return;
    }
    if (action === 'stop' && owns(id)) { device.value = 0; device.target = 0; device.status = 'Idle'; recordCommand(device, 'stop', { rpm: 0 }); render(); return; }
    if (action === 'end') { endBatch(); return; }
    if (hasBatch(id)) {
      if (action === 'hold') state.batch.paused = true;
      if (action === 'resume') state.batch.paused = false;
      if (action === 'inject') { state.batch.fault = true; state.batch.paused = true; }
      if (action === 'clear-fault') { state.batch.fault = false; state.batch.paused = false; }
      device.status = state.batch.fault ? 'Faulted' : state.batch.paused ? 'Held' : 'Roughing';
      device.value = state.batch.paused ? 0 : 2400;
      state.lastBatch = `${device.name} · ${device.status.toLowerCase()}`;
      recordCommand(device, action.replace('-fault', ''), { state: device.status.toLowerCase() });
      render();
    }
  });

  document.querySelectorAll('[data-variant]').forEach((link) => link.addEventListener('click', (event) => {
    if (link.dataset.variant === 'a') return;
    event.preventDefault();
    history.pushState({}, '', link.getAttribute('href'));
    document.querySelectorAll('[popover]:popover-open').forEach((popover) => popover.hidePopover());
    render();
  }));
  addEventListener('popstate', render);

  logs.commands = [
    { label: 'CNC 02 · batch completed', topic: `${prefix}/orders/preview-000/status`, time: '14:32:18', payload: { state: 'completed', quantity: 1 } },
    { label: 'Neuron 02 · write acknowledged', topic: `${topicBase('plc-2')}/cmd/ack`, time: '14:32:17', payload: { error: 0 } },
    { label: 'MES → CNC 02 · unload', topic: `${topicBase('plc-2')}/batch/preview-000/phase/unload/command`, time: '14:32:12', outgoing: true, payload: { command: 'start', phase: 'unload' } },
    { label: 'ERP → MES · batch order', topic: `${prefix}/orders/preview-000`, time: '14:32:00', outgoing: true, payload: { recipe: 'program-quick', quantity: 1 } },
  ];
  devices.forEach(telemetry);
  render();

  setInterval(() => {
    tick += 1;
    if (state.claim && state.claim.until <= Date.now()) { release(); render(); }
    else if (state.claim) document.querySelectorAll(`[data-owner="${state.claim.device}"]`).forEach((label) => { label.textContent = claimText(state.claim.device); });
    if (state.batch && !state.batch.paused && state.batch.until <= Date.now()) endBatch(true);
    if (state.batch?.paused) state.batch.until += 1000;
    if (tick % 3 === 0) {
      const hvac = deviceById('hvac-1');
      hvac.value = tick % 2 ? 24.1 : 24.2;
      document.querySelectorAll('[data-value="hvac-1"]').forEach((reading) => { reading.textContent = hvac.value; });
      devices.forEach(telemetry);
    }
  }, 1000);
})();
