import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/operator'))
from control import DeviceControl
from visitors import Refused, Limiter


class DeviceClaimTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.demo = Mock(prefix='acme/plant', machine_prefix='acme/plant/machining/line')
        self.demo.lock = threading.RLock()
        self.demo.orders = {}
        self.state = {'scene': {'active': False}, 'assets': {
            device: {'fresh': True, 'received_at': self.now, 'values': {}}
            for device in DeviceControl.DEVICES},
            'plcs': {'plc-1': {'state': 'idle'}, 'plc-2': {'state': 'idle'}}}
        self.demo.snapshot.side_effect = lambda: self.state
        self.controls = DeviceControl(self.demo, clock=lambda: self.now)

    def join(self, owner='alice', device='plc-1', key='request-one', recipe=None):
        return self.controls.join(owner, '127.0.0.1', key, device, recipe)

    def grant(self, device='plc-1'):
        self.controls.tick()
        ticket = self.controls.active[device]
        if device != 'hvac-1':
            self.controls.observe(self.controls.base(device) + '/control/reservation/ack', {'id': ticket['id'], 'accepted': True})
        self.controls.tick()
        return self.controls.snapshot(ticket['owner'])['devices'][device]['lease_id']

    def confirm(self, device, *, ack=True, telemetry=True):
        pending = self.controls.active[device]['pending']
        if ack:
            self.controls.observe(self.controls.base(device) + '/cmd/ack', {'uuid': pending['id'], 'error': 0})
        if telemetry:
            self.now += 0.1
            self.state['assets'][device].update(received_at=self.now, values=pending['values'].copy())
            if device != 'hvac-1':
                self.state['assets'][device]['values']['spindle_rpm'] = pending['values']['manual_rpm']

    def test_devices_are_independent_and_same_device_is_fifo(self):
        self.join()
        lease = self.grant()
        self.join('bob')
        self.join('carol', 'hvac-1')
        self.grant('hvac-1')
        self.assertTrue(self.controls.snapshot('carol')['devices']['hvac-1']['owned'])
        self.assertEqual(self.controls.snapshot('bob')['devices']['plc-1']['position'], 1)
        self.assertIsNone(self.controls.snapshot('bob')['devices']['plc-1']['lease_id'])
        self.controls.release('alice', 'plc-1', lease)
        self.controls.tick()
        self.assertFalse(self.controls.snapshot('bob')['devices']['plc-1']['owned'])
        self.confirm('plc-1', ack=False)
        self.controls.tick()
        self.assertIn('plc-1', self.controls.active, 'Telemetry alone must not prove the write')
        self.confirm('plc-1')
        self.state['assets']['plc-1']['values']['spindle_rpm'] = 1500
        self.controls.tick()
        self.assertIn('plc-1', self.controls.active, 'Handoff must wait for observed zero RPM, not just reset tags')
        self.state['assets']['plc-1']['values']['spindle_rpm'] = 0
        self.controls.tick()
        self.grant()
        self.assertTrue(self.controls.snapshot('bob')['devices']['plc-1']['owned'])

    def test_waiting_queue_capacity_one_request_per_owner_and_idempotency(self):
        key = self.join()
        self.assertEqual(self.join(), key)
        with self.assertRaises(Refused):
            self.join(device='hvac-1')
        with self.assertRaises(Refused):
            self.join(device='hvac-1', key='another-key')
        self.join('bob')
        self.join('carol')
        with self.assertRaises(Refused) as error:
            self.join('dan')
        self.assertEqual(error.exception.status, 429)

    def test_expiry_copied_leases_and_foreign_device_never_allow_writes(self):
        self.join()
        lease = self.grant()
        body = {'request_id': 'command-one', 'action': 'rpm', 'value': 1000, 'lease_id': lease}
        self.demo.send.reset_mock()
        for owner, device in [('bob', 'plc-1'), ('alice', 'plc-2')]:
            with self.assertRaises(Refused):
                self.controls.command(owner, device, body)
        self.now += 31
        with self.assertRaises(Refused):
            self.controls.command('alice', 'plc-1', body)
        self.demo.send.assert_not_called()

    def test_manual_write_has_deadline_and_needs_ack_plus_telemetry(self):
        self.join()
        lease = self.grant()
        body = {'request_id': 'command-one', 'action': 'rpm', 'value': 1500, 'lease_id': lease}
        self.controls.command('alice', 'plc-1', body)
        count = self.demo.send.call_count
        self.controls.command('alice', 'plc-1', body)
        self.assertEqual(self.demo.send.call_count, count)
        with self.assertRaises(Refused):
            self.controls.command('alice', 'plc-1', {**body, 'value': 2000})
        self.assertEqual(self.controls.active['plc-1']['pending']['values'], {'manual_rpm': 1500, 'manual_until': 1030})
        self.confirm('plc-1', telemetry=False)
        self.controls.tick()
        self.assertEqual(self.controls.active['plc-1']['pending']['status'], 'sent')
        self.confirm('plc-1')
        self.controls.tick()
        self.assertEqual(self.controls.active['plc-1']['pending']['status'], 'observed')

    def test_invalid_values_stale_devices_and_wrong_actions_never_write(self):
        self.join()
        lease = self.grant()
        for value in (True, '1000', float('nan'), float('inf'), -1, 6001):
            with self.assertRaises(Refused):
                self.controls.command('alice', 'plc-1', {'request_id': 'command-one', 'action': 'rpm', 'value': value, 'lease_id': lease})
        self.now += 5
        self.state['assets']['plc-1']['fresh'] = False
        with self.assertRaises(Refused):
            self.controls.command('alice', 'plc-1', {'request_id': 'command-one', 'action': 'rpm', 'value': 1000, 'lease_id': lease})

    def test_status_does_not_require_claim_but_is_rate_limited_and_labels_source(self):
        for i in range(6):
            self.controls.command('reader', 'hvac-1', {'action': 'status', 'request_id': f'status-{i:03}'})
        self.assertEqual(self.demo.send.call_args.args[1]['source'], 'observed telemetry')
        with self.assertRaises(Refused) as error:
            self.controls.command('reader', 'hvac-1', {'action': 'status', 'request_id': 'status-end'})
        self.assertEqual(error.exception.status, 429)

    def test_batch_waits_for_mes_reservation_and_keeps_device_until_terminal(self):
        self.join(recipe='program-quick')
        self.controls.tick()
        self.assertFalse(any('/orders/' in c.args[0] for c in self.demo.send.call_args_list))
        self.grant()
        ticket = self.controls.active['plc-1']
        sent = next(c.args[1] for c in self.demo.send.call_args_list if '/orders/' in c.args[0])
        self.assertEqual(sent['plc_id'], 'plc-1')
        self.assertEqual(sent['claim_id'], ticket['id'])
        self.now += 40  # Batch continues after a 30-second manual claim would expire.
        self.controls.tick()
        self.assertEqual(ticket['stage'], 'active')
        self.demo.orders[ticket['order_id']] = {'state': 'completed'}
        self.controls.tick()
        self.assertEqual(ticket['stage'], 'recovering')
        self.controls.tick()
        self.confirm('plc-1')
        self.controls.tick()
        self.assertNotIn('plc-1', self.controls.active)

    def test_disconnected_waiter_expires_and_abandoned_manual_claim_recovers(self):
        self.join()
        self.grant()
        self.join('bob')
        self.now += 21
        self.controls.tick()
        self.assertFalse(self.controls.waiting['plc-1'])
        self.assertEqual(self.controls.active['plc-1']['stage'], 'recovering')

    def test_batch_controls_require_the_owner_current_batch_and_phase(self):
        self.join(recipe='program-a')
        lease = self.grant()
        ticket = self.controls.active['plc-1']
        self.demo.orders[ticket['order_id']] = {'state': 'running', 'batch_id': 'B-one', 'plc_id': 'plc-1'}
        self.state['plcs']['plc-1'] = {'state': 'running', 'batch_id': 'B-one', 'phase': 'rough'}
        body = {'request_id': 'batch-command', 'action': 'hold', 'batch_id': 'B-one', 'phase': 'rough', 'lease_id': lease}
        self.demo.send.reset_mock()
        for owner, change in [('bob', {}), ('alice', {'batch_id': 'B-foreign'}), ('alice', {'phase': 'load'})]:
            with self.assertRaises(Refused):
                self.controls.command(owner, 'plc-1', {**body, **change})
        self.demo.send.assert_not_called()
        self.controls.command('alice', 'plc-1', body)
        self.assertEqual(self.demo.send.call_args.args[0], self.controls.base('plc-1') + '/batch/B-one/phase/rough/command')
        self.controls.command('alice', 'plc-1', {**body, 'action': 'inject'})
        self.assertTrue(self.demo.send.call_args.args[0].endswith('/_demo/fault'))
        self.controls.command('alice', 'plc-1', {**body, 'action': 'abort'})
        self.assertEqual(ticket['stage'], 'recovering')

    def test_restart_restores_ownership_as_recovery_and_preserves_request_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'claims.sqlite')
            self.controls = DeviceControl(self.demo, path, clock=lambda: self.now)
            ticket_id = self.join()
            self.grant()
            self.controls = DeviceControl(self.demo, path, clock=lambda: self.now)
            self.assertFalse(self.controls.snapshot('alice')['devices']['plc-1']['owned'])
            self.assertEqual(self.controls.active['plc-1']['stage'], 'recovering')
            self.assertEqual(self.join(), ticket_id)
            self.assertFalse(self.controls.waiting['plc-1'])
            with self.assertRaises(Refused):
                self.controls.release('bob', 'plc-1', 'stolen')

    def test_limiter_memory_is_bounded(self):
        limiter = Limiter(capacity=2, clock=lambda: self.now)
        limiter.take('a', 1, 60)
        limiter.take('b', 1, 60)
        with self.assertRaises(Refused):
            limiter.take('c', 1, 60)
        self.now += 61
        limiter.take('c', 1, 60)
        self.assertEqual(len(limiter.entries), 1)


if __name__ == '__main__':
    unittest.main()
