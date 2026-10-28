/* Presentation rules shared by the live page and its state regression checks. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FactoryUI = factory();
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const names = {'plc-1': 'CNC 01', 'plc-2': 'CNC 02', 'hvac-1': 'HVAC'};
  const title = value => String(value || '').replace(/_/g, ' ').replace(/^./, letter => letter.toUpperCase());
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const number = (value, decimals = 0) => numeric(value)
    ? value.toLocaleString(undefined, {maximumFractionDigits: decimals}) : '—';

  function machineState(device, asset = {}, plc = {}) {
    const values = asset.values || {};
    if (!asset.fresh) return {text: 'Data stale', fault: false};
    if (device === 'hvac-1') return {
      text: values.fault_active ? 'Fault' : values.enable_cmd === 0 || values.enable_cmd === false ? 'Off' : values.unit_running ? 'Ventilating' : 'Standby',
      fault: !!values.fault_active,
    };
    const phaseState = values.phase_state || plc.state;
    if (['running', 'holding', 'faulted'].includes(phaseState)) return {
      text: ['Batch', title(phaseState), title(values.current_phase || plc.phase)].filter(Boolean).join(' · '),
      fault: phaseState === 'faulted',
    };
    return {text: values.spindle_rpm > 0 ? 'Manual test' : 'Idle', fault: false};
  }

  function claimPresentation(claim, context) {
    const {fresh, connected, currentRequest, seconds} = context;
    const reachable = context.reachable ?? connected;
    const queued = !!claim.position, full = claim.waiting >= claim.capacity;
    const release = claim.mine || queued;
    let badge = '', kind = 'neutral';
    if (queued) badge = `You're #${claim.position}`;
    else if (claim.stage === 'recovering') badge = 'Restoring defaults…';
    else if (claim.stage === 'reserving') badge = claim.mine ? 'Starting your turn…' : 'Device reserved';
    else if (claim.mine && claim.kind === 'batch') { badge = `Your batch · ${claim.remaining}s`; kind = 'mine'; }
    else if (claim.busy && !claim.mine) badge = `${claim.kind === 'batch' ? 'ERP batch' : 'In use'} · ${claim.remaining}s` + (claim.waiting ? ` · ${claim.waiting} waiting` : '');
    // Leaving a queue or releasing a lease must not depend on live device data.
    // The server can accept cleanup now and restore defaults after reconnect.
    let reason = release ? reachable ? '' : 'Factory connection unavailable.'
      : !connected ? 'Factory connection unavailable.' : !fresh ? 'Waiting for fresh device data.'
      : currentRequest && !release ? `One request at a time · finish with ${currentRequest} first.`
        : full && !release ? 'Queue full · try another device or wait.' : '';
    if (release && !queued && !claim.owned) reason = claim.stage === 'recovering' ? 'Restoring device defaults before releasing control.' : 'Waiting for the device reservation.';
    const button = queued ? 'Leave queue' : claim.mine
      ? claim.stage === 'recovering' ? 'Restoring…' : claim.stage === 'reserving' ? 'Starting…'
        : claim.kind === 'batch' ? 'End batch' : `Release · ${claim.remaining}s`
      : full ? 'Queue full' : claim.busy ? 'Join queue' : `Claim · ${seconds}s`;
    const description = queued ? `You are number ${claim.position} in this device's queue.`
      : claim.mine ? claim.kind === 'batch' ? 'Your ERP batch controls this device.' : 'You control this device.'
        : claim.busy ? 'Another visitor controls this device. Join the queue to wait for a turn.' : 'Claim this device to change its settings.';
    return {badge, kind, release, button, reason, description};
  }

  function feedState({kind, phase, paused, subscribed, refused = 0, rate = 0, lastMessage = 0, now = Date.now()}) {
    if (phase === 'standby') return {text: 'Other tab', tone: 'neutral'};
    if (phase === 'connecting') return {text: 'Connecting…', tone: 'neutral'};
    if (phase !== 'connected') return {text: 'Reconnecting…', tone: 'warn'};
    if (paused) return {text: 'Paused', tone: 'neutral'};
    if (!subscribed) return {text: refused ? 'Unavailable' : 'Subscribing…', tone: 'warn'};
    if (kind === 'commands') return {text: lastMessage && now - lastMessage < 5000 ? 'Live' : 'Listening', tone: 'live'};
    return lastMessage && now - lastMessage < 5000
      ? {text: `${rate} msg/s`, tone: 'live'} : {text: 'No messages', tone: 'warn'};
  }

  function snapshotSummary(device, snapshot) {
    const values = snapshot.values || {};
    const reading = device === 'hvac-1' ? `${number(values.supply_air_temp_c, 1)} °C · fan ${number(values.fan_speed_pct)}%`
      : `${number(values.spindle_rpm)} rpm · ${title(values.state || 'unknown')}`;
    return snapshot.fresh ? `Snapshot · fresh · ${reading}`
      : `Snapshot · stale · ${snapshot.observed_at ? 'last data ' + new Date(snapshot.observed_at * 1000).toLocaleTimeString() : 'no observed data'}`;
  }

  function writeSummary(body) {
    const values = body.values || Object.fromEntries((body.tags || []).map(tag => [tag.tag, tag.value]));
    const parts = [];
    if (numeric(values.manual_rpm)) parts.push(`Target RPM = ${number(values.manual_rpm)}`);
    if (numeric(values.temp_setpoint_c)) parts.push(`Target = ${number(values.temp_setpoint_c, 1)} °C`);
    if (numeric(values.fan_speed_pct)) parts.push(`Fan = ${number(values.fan_speed_pct)}%`);
    if (numeric(values.mode_cmd)) parts.push(`Mode = ${{1: 'Off', 2: 'Cool', 3: 'Heat', 4: 'Fan', 5: 'Auto'}[values.mode_cmd] || values.mode_cmd}`);
    if (numeric(values.enable_cmd)) parts.push(`Power = ${values.enable_cmd ? 'On' : 'Off'}`);
    if (values.command_action) parts.push(title(values.command_action));
    if (values.command_phase) parts.push(title(values.command_phase));
    return parts.join(' · ');
  }

  function batchSummary(orders) {
    const running = orders.filter(order => order.state === 'running');
    if (running.length) return running.map(order => `${names[order.plc_id] || order.plc_id} · batch running`).join(' / ');
    const latest = orders.at(-1);
    const ended = latest?.state === 'failed' && /turn ended|cancelled by visitor/i.test(latest.reason || '');
    return 'No batch running' + (latest ? ` · last ${ended ? 'ended' : latest.state}` : '');
  }

  return {names, title, number, machineState, claimPresentation, feedState, snapshotSummary, writeSummary, batchSummary};
});
