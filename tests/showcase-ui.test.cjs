const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const UI = require('../services/operator/static/showcase-model.js');

const available = {mine:false, owned:false, busy:false, stage:'available', remaining:0, position:null, waiting:0, capacity:3};
const context = {connected:true, fresh:true, currentRequest:null, seconds:30};

test('finished batches do not label an idle physical machine as failed or aborted', () => {
  for (const state of ['completed', 'aborted']) {
    assert.deepEqual(UI.machineState('plc-1', {fresh:true, values:{state:'idle', phase_state:state, spindle_rpm:0}}, {state, phase:'load'}), {text:'Idle', fault:false});
  }
  assert.equal(UI.machineState('plc-1', {fresh:true, values:{state:'idle', spindle_rpm:1500}}, {}).text, 'Manual test');
  assert.deepEqual(UI.machineState('plc-1', {fresh:true, values:{phase_state:'faulted', current_phase:'load'}}, {}), {text:'Batch · Faulted · Load', fault:true});
});

test('missing HVAC command tags do not falsely label running ventilation as off', () => {
  assert.equal(UI.machineState('hvac-1', {fresh:true, values:{unit_running:1}}).text, 'Ventilating');
  assert.equal(UI.machineState('hvac-1', {fresh:true, values:{unit_running:0, enable_cmd:0}}).text, 'Off');
});

test('idle cards stay quiet while busy, queued and full states explain their actions', () => {
  assert.equal(UI.claimPresentation(available, context).badge, '');
  const busy = {...available, busy:true, remaining:23, waiting:2, kind:'manual', stage:'active'};
  assert.equal(UI.claimPresentation(busy, context).button, 'Join queue');
  assert.equal(UI.claimPresentation(busy, context).badge, 'In use · 23s · 2 waiting');
  assert.equal(UI.claimPresentation({...busy, position:2}, {...context, currentRequest:'CNC 01'}).button, 'Leave queue');
  assert.equal(UI.claimPresentation({...busy, waiting:3}, context).button, 'Queue full');
  assert.match(UI.claimPresentation(available, {...context, currentRequest:'CNC 01'}).reason, /finish with CNC 01/);
});

test('owner recovery cannot expose a live release/write action', () => {
  const recovering = UI.claimPresentation({...available, mine:true, busy:true, stage:'recovering'}, context);
  assert.equal(recovering.button, 'Restoring…');
  assert.match(recovering.reason, /Restoring device defaults/);
});

test('stale devices block new control but never trap an owner or queue waiter', () => {
  const offline = {...context, connected:false, fresh:false, reachable:true};
  assert.match(UI.claimPresentation(available, offline).reason, /unavailable/);
  const owner = {...available, mine:true, owned:true, busy:true, stage:'active', kind:'manual', remaining:23};
  assert.equal(UI.claimPresentation(owner, offline).reason, '');
  assert.equal(UI.claimPresentation({...available, busy:true, position:2}, offline).reason, '');
  assert.match(UI.claimPresentation(owner, {...offline, reachable:false}).reason, /unavailable/);
});

test('working subscriptions stay live when another filter is refused; pause is explicit', () => {
  const live = {kind:'telemetry', phase:'connected', subscribed:true, refused:1, rate:3, lastMessage:1000, now:2000};
  assert.equal(UI.feedState(live).text, '3 msg/s');
  assert.equal(UI.feedState({...live, paused:true}).text, 'Paused');
  assert.equal(UI.feedState({...live, phase:'standby'}).text, 'Other tab');
  assert.equal(UI.feedState({...live, now:7000}).text, 'No messages');
  assert.equal(UI.feedState({...live, phase:'reconnecting'}).text, 'Reconnecting…');
  assert.equal(UI.feedState({...live, kind:'commands', lastMessage:0}).text, 'Listening');
});

test('write summaries show the changed settings and omit lease deadlines and IDs', () => {
  assert.equal(UI.writeSummary({tags:[{tag:'manual_rpm', value:1500},{tag:'manual_until',value:1793200000}]}), 'Target RPM = 1,500');
  const hvac = UI.writeSummary({values:{temp_setpoint_c:23, fan_speed_pct:60, mode_cmd:5, enable_cmd:0}});
  assert.match(hvac, /23 °C/);
  assert.match(hvac, /Fan = 60%/);
  assert.match(hvac, /Power = Off/);
});

test('status summaries identify cached freshness and historical batches stay historical', () => {
  assert.match(UI.snapshotSummary('plc-1', {fresh:true, values:{spindle_rpm:0, state:'idle'}}), /^Snapshot · fresh · 0 rpm · Idle$/);
  assert.match(UI.snapshotSummary('hvac-1', {fresh:false, values:{}, observed_at:0}), /stale · no observed data/);
  assert.equal(UI.batchSummary([{state:'failed', reason:'Turn ended before dispatch.'}]), 'No batch running · last ended');
  assert.equal(UI.batchSummary([{state:'failed', reason:'Gateway timeout'}]), 'No batch running · last failed');
});

test('a taken-over viewer yields its bounded connection until Watch here is requested', async () => {
  const timers = new Map();
  let nextTimer = 0, credentials = 0, reconnects = 0;
  const client = new EventEmitter();
  client.end = () => {};
  client.reconnect = () => { reconnects++; };
  const scope = {URL, location:{protocol:'http:', href:'http://localhost:8080/'},
    fetch:async () => ({ok:true, json:async () => { credentials++; return {username:'visitor', password:'test-token', clientId:'bounded-view'}; }}),
    mqtt:{connect:(_url, options) => { client.options = options; return client; }},
    setTimeout:fn => { timers.set(++nextTimer, fn); return nextTimer; }, clearTimeout:id => timers.delete(id)};
  scope.window = scope;
  vm.runInNewContext(fs.readFileSync('services/operator/static/connection.js', 'utf8'), scope);
  await scope.demoMqttConnect({}, 'showcase');
  let standby = false;
  client.on('viewer-standby', () => { standby = true; });
  client.emit('disconnect', {reasonCode:0x8e});
  client.emit('close');
  assert.equal(standby, true);
  assert.equal(timers.size, 0);
  assert.equal(reconnects, 0);
  client.watchHere();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(credentials, 2);
  assert.equal(reconnects, 1);
  assert.equal(client.options.clientId, 'bounded-view');
  client.end(true);
  client.emit('close');
  assert.equal(timers.size, 0);
});
