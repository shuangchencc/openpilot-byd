"""Offline host receiver checks. No SSH, keys, target files or device I/O."""
import base64
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


ENTRY = Path(__file__).resolve().parents[2] / 'tools/capture_current_host.py'
SPEC = importlib.util.spec_from_file_location('capture_entry_under_test', ENTRY)
entry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entry)


def block_fixture(raw=b'firmware-test-bytes', object_id='partition_ImageFv_a'):
    packed = gzip.compress(raw, mtime=0)
    expected = {'id': object_id, 'node': '/dev/sde17', 'source_offset_bytes': 0,
                'expected_bytes': len(raw)}
    before = {'device': 'block', 'size': len(raw), 'mapping_digest': 'fixture'}
    common = {'schema': entry.SCHEMAS['block'], 'object_id': object_id}
    begin = dict(common, event='begin', expected_bytes=len(raw), target_writes=False,
                 source={'node': 'sde17', 'offset': 0}, before=before)
    end = dict(common, event='end', status='COMPLETE', actual_bytes=len(raw),
               metadata_stable=True, after=before.copy(), gzip_bytes=len(packed),
               gzip_sha256=entry.sha(packed), raw_sha256=entry.sha(raw))
    result = {'exit_code': 0, 'bytes': len(packed), 'sha256': entry.sha(packed),
              'stderr': (json.dumps(begin) + '\n' + json.dumps(end) + '\n').encode(),
              'elapsed_seconds': 0.01}
    return expected, packed, result


def startup_fixture():
    raw = b'#!/bin/sh\n# inert fixture\n'
    return {'schema': entry.SCHEMAS['startup'],
            'boundary': dict(profile='UNBOUND', **{name: False for name in
                ('services_changed', 'target_files_written', 'device_data_read',
                 'target_programs_imported_or_executed', 'slot_changed',
                 'panda_reset_or_flash', 'can_tx')}),
            'files': {'/data/continue.sh': {'status': 'collected',
                'content_base64': base64.b64encode(raw).decode(),
                'metadata': {'bytes': len(raw), 'mode': 0o755}, 'sha256': entry.sha(raw)},
                '/etc/not-present': {'status': 'missing'}},
            'systemd_loaded': {}, 'dynamic_paths': [], 'gaps': [],
            'consistency': 'BEST_EFFORT'}


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def test_block_validates_both_hashes_and_complete_decompression(self):
        expected, packed, result = block_fixture()
        path = self.directory / 'image.gz'
        path.write_bytes(packed)
        checked = entry.validate_block(result, expected, path)
        self.assertTrue(checked['local_decompression']['full_decompression_verified'])
        self.assertFalse(checked['local_decompression']['source_second_read_performed'])
        self.assertFalse(checked['cross_object_atomic'])

    def test_block_wrong_node_is_rejected(self):
        expected, packed, result = block_fixture()
        expected['node'] = '/dev/sde18'
        with self.assertRaisesRegex(ValueError, 'source_range'):
            entry.validate_block(result, expected, self.directory / 'unused')

    def test_block_wrong_offset_is_rejected(self):
        expected, packed, result = block_fixture()
        expected['source_offset_bytes'] = 4096
        with self.assertRaisesRegex(ValueError, 'source_range'):
            entry.validate_block(result, expected, self.directory / 'unused')

    def test_block_wrong_object_id_is_rejected(self):
        expected, packed, result = block_fixture()
        expected['id'] = 'partition_other'
        with self.assertRaisesRegex(ValueError, 'stream_identity'):
            entry.validate_block(result, expected, self.directory / 'unused')

    def test_nonzero_exit_cannot_be_successful(self):
        expected, packed, result = block_fixture()
        result['exit_code'] = 1
        with self.assertRaisesRegex(ValueError, 'ssh_or_collector_failed'):
            entry.validate_block(result, expected, self.directory / 'unused')

    def test_missing_end_receipt_is_rejected(self):
        expected, packed, result = block_fixture()
        result['stderr'] = result['stderr'].splitlines()[0]
        with self.assertRaisesRegex(ValueError, 'stream_receipts'):
            entry.validate_block(result, expected, self.directory / 'unused')

    def test_changed_metadata_is_rejected(self):
        expected, packed, result = block_fixture()
        events = [json.loads(line) for line in result['stderr'].splitlines()]
        events[-1]['after']['mapping_digest'] = 'changed'
        result['stderr'] = '\n'.join(map(json.dumps, events)).encode()
        with self.assertRaisesRegex(ValueError, 'source_metadata_drift'):
            entry.validate_block(result, expected, self.directory / 'unused')

    def test_archive_transport_hash_mismatch_is_rejected(self):
        expected, packed, result = block_fixture()
        result['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'gzip_stream_mismatch'):
            entry.validate_block(result, expected, self.directory / 'unused')

    def test_raw_hash_mismatch_is_rejected(self):
        expected, packed, result = block_fixture()
        path = self.directory / 'image.gz'
        path.write_bytes(packed)
        with self.assertRaisesRegex(ValueError, 'expanded_stream_mismatch'):
            entry.verify_gzip(path, expected['expected_bytes'], '0' * 64)

    def test_expansion_limit_is_enforced(self):
        path = self.directory / 'image.gz'
        path.write_bytes(gzip.compress(b'abcdefgh'))
        with self.assertRaisesRegex(ValueError, 'expanded_size_exceeded'):
            entry.verify_gzip(path, 7, entry.sha(b'abcdefgh'))

    def test_gzip_footer_corruption_is_rejected(self):
        path = self.directory / 'image.gz'
        packed = bytearray(gzip.compress(b'abcdefgh'))
        packed[-8] ^= 1
        path.write_bytes(packed)
        with self.assertRaises(OSError):
            entry.verify_gzip(path, 8, entry.sha(b'abcdefgh'))

    def test_truncated_gzip_is_rejected(self):
        path = self.directory / 'image.gz'
        path.write_bytes(gzip.compress(b'abcdefgh')[:-4])
        with self.assertRaises((EOFError, OSError)):
            entry.verify_gzip(path, 8, entry.sha(b'abcdefgh'))

    def test_second_gzip_member_cannot_hide_extra_bytes(self):
        path = self.directory / 'image.gz'
        path.write_bytes(gzip.compress(b'abc') + gzip.compress(b'x'))
        with self.assertRaisesRegex(ValueError, 'expanded_size_exceeded'):
            entry.verify_gzip(path, 3, entry.sha(b'abc'))

    def test_json_duplicate_key_and_nonfinite_are_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                entry.strict_json(raw)

    def test_startup_files_have_fixed_local_names_and_private_mode(self):
        data = startup_fixture()
        # A source path is evidence only, never a destination path.
        data['files']['/../../escape'] = data['files'].pop('/data/continue.sh')
        path = self.directory / 'startup.partial'
        path.write_text(json.dumps(data))
        checked = entry.validate_startup({'exit_code': 0}, path, self.directory)
        verified = next(x for x in checked['files'] if x['status'] == 'FILE_BYTES_VERIFIED')
        artifact = self.directory / verified['artifact']
        self.assertEqual(artifact.parent, self.directory)
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        self.assertEqual(entry.sha(artifact.read_bytes()), verified['sha256'])

    def test_startup_digest_mismatch_does_not_write_file(self):
        data = startup_fixture()
        data['files']['/data/continue.sh']['sha256'] = '0' * 64
        path = self.directory / 'startup.partial'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'source_file_mismatch'):
            entry.validate_startup({'exit_code': 0}, path, self.directory)
        self.assertEqual(list(self.directory.glob('source-*')), [])

    def test_startup_boundary_violation_is_rejected(self):
        data = startup_fixture()
        data['boundary']['can_tx'] = True
        path = self.directory / 'startup.partial'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'startup_boundary'):
            entry.validate_startup({'exit_code': 0}, path, self.directory)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.root = self.base / 'engineering'
        self.area = self.root / 'hardware/current-host-recovery'
        self.area.mkdir(parents=True)
        self.destination = self.base / 'private'
        self.destination.mkdir(mode=0o700)
        for name in ('startup_collector.py', 'block_stream_collector.py'):
            (self.area / name).write_bytes(b'# inert collector fixture\n')
        self.expected, self.packed, self.result = block_fixture()
        (self.area / 'capture-scope.json').write_text(json.dumps({
            'partition_objects': [self.expected], 'gpt_metadata_objects': []}))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(entry, 'ROOT', self.root).start()
        mock.patch.object(entry, 'AREA', self.area).start()
        self.command = mock.patch.object(entry, 'ssh_command', return_value=['fake-ssh']).start()
        # Capacity is a fixture; no dependency on the test machine's disk size.
        mock.patch.object(entry.os, 'statvfs', return_value=type('Space', (), {
            'f_bavail': 100 * 1024**3, 'f_frsize': 1})()).start()

    def run_main(self, argv):
        with mock.patch('sys.argv', ['capture_current_host.py'] + argv), contextlib.redirect_stdout(io.StringIO()) as text:
            status = entry.main()
        return status, text.getvalue()

    def fake_receive(self, command, source, output, maximum, timeout, object_id):
        entry.private_write(output, self.packed)
        return self.result.copy()

    def test_startup_preview_never_reads_connection_or_receives(self):
        with mock.patch.object(entry, 'receive') as receive:
            status, output = self.run_main(['startup'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)['mode'], 'PREVIEW_NO_CONNECTION')
        self.command.assert_not_called()
        receive.assert_not_called()
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_block_preview_uses_fixed_scope_and_does_not_connect(self):
        status, output = self.run_main(['block', '--object', self.expected['id']])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)['expected_bytes'], self.expected['expected_bytes'])
        self.command.assert_not_called()

    def test_unknown_scope_id_is_rejected_before_connection(self):
        with self.assertRaisesRegex(ValueError, 'fixed_object_required'):
            self.run_main(['block', '--object', 'arbitrary'])
        self.command.assert_not_called()

    def test_destination_inside_engineering_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'destination_must_be_external'):
            self.run_main(['startup', '--connect', '--destination', str(self.area)])
        self.command.assert_not_called()

    def test_public_destination_is_rejected(self):
        self.destination.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'private_directory_mode'):
            self.run_main(['startup', '--connect', '--destination', str(self.destination)])
        self.command.assert_not_called()

    def test_symlink_destination_is_rejected(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.destination)
        with self.assertRaisesRegex(ValueError, 'symlink_destination'):
            self.run_main(['startup', '--connect', '--destination', str(alias)])
        self.command.assert_not_called()

    def test_valid_block_is_private_and_finalized_only_after_verification(self):
        with mock.patch.object(entry, 'receive', side_effect=self.fake_receive):
            status, output = self.run_main(['block', '--object', self.expected['id'], '--connect',
                                          '--destination', str(self.destination)])
        self.assertEqual(status, 0)
        directory = self.destination / self.expected['id']
        receipt = json.loads((directory / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'VERIFIED_OBJECT')
        self.assertTrue((directory / 'data.img.gz').is_file())
        self.assertFalse((directory / 'data.img.gz.partial').exists())
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_valid_startup_is_saved_without_running_collected_files(self):
        payload = json.dumps(startup_fixture()).encode()

        def receive(command, source, output, maximum, timeout, object_id):
            entry.private_write(output, payload)
            return {'exit_code': 0, 'bytes': len(payload), 'sha256': entry.sha(payload),
                    'stderr': b'', 'elapsed_seconds': 0.01}

        with mock.patch.object(entry, 'receive', side_effect=receive), \
             mock.patch.object(entry.subprocess, 'Popen') as popen:
            status, output = self.run_main(['startup', '--connect', '--destination', str(self.destination)])
        self.assertEqual(status, 0)
        popen.assert_not_called()
        directory = self.destination / 'startup'
        receipt = json.loads((directory / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'VERIFIED_SELECTED_FILES')
        self.assertTrue((directory / 'startup.json').is_file())
        self.assertFalse((directory / 'startup.json.partial').exists())
        self.assertEqual(len(list(directory.glob('source-*.bin'))), 1)

    def test_receive_failure_preserves_collected_statistics_and_diagnostics(self):
        partial_data = b'partial'
        partial = {'exit_code': None, 'bytes': len(partial_data), 'sha256': entry.sha(partial_data),
                   'stderr': b'bounded diagnostic', 'elapsed_seconds': 1,
                   'receive_failure': 'local_ssh_timeout'}

        def fail_receive(command, source, output, maximum, timeout, object_id):
            entry.private_write(output, partial_data)
            raise entry.ReceiveError('local_ssh_timeout', partial)

        with mock.patch.object(entry, 'receive', side_effect=fail_receive):
            status, output = self.run_main(['block', '--object', self.expected['id'], '--connect',
                                          '--destination', str(self.destination)])
        self.assertEqual(status, 2)
        directory = self.destination / self.expected['id']
        receipt = json.loads((directory / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'FAILED_PARTIAL_RETAINED')
        for key in ('bytes', 'sha256', 'elapsed_seconds', 'receive_failure'):
            self.assertEqual(receipt[key], partial[key])
        self.assertEqual((directory / 'stderr.jsonl').read_bytes(), partial['stderr'])
        self.assertEqual((directory / 'data.img.gz.partial').read_bytes(), partial_data)

    def test_wrong_hash_preserves_partial_and_failure_receipt(self):
        self.result['sha256'] = '0' * 64
        with mock.patch.object(entry, 'receive', side_effect=self.fake_receive):
            status, output = self.run_main(['block', '--object', self.expected['id'], '--connect',
                                          '--destination', str(self.destination)])
        self.assertEqual(status, 2)
        directory = self.destination / self.expected['id']
        self.assertTrue((directory / 'data.img.gz.partial').is_file())
        self.assertFalse((directory / 'data.img.gz').exists())
        self.assertEqual(json.loads((directory / 'receipt.json').read_text())['status'], 'FAILED_PARTIAL_RETAINED')

    def test_truncated_gzip_preserves_failure_receipt(self):
        self.packed = self.packed[:-4]
        events = [json.loads(line) for line in self.result['stderr'].splitlines()]
        events[-1].update(gzip_bytes=len(self.packed), gzip_sha256=entry.sha(self.packed))
        self.result.update(bytes=len(self.packed), sha256=entry.sha(self.packed),
                           stderr='\n'.join(map(json.dumps, events)).encode())
        with mock.patch.object(entry, 'receive', side_effect=self.fake_receive):
            status, output = self.run_main(['block', '--object', self.expected['id'], '--connect',
                                          '--destination', str(self.destination)])
        self.assertEqual(status, 2)
        directory = self.destination / self.expected['id']
        self.assertTrue((directory / 'data.img.gz.partial').is_file())
        self.assertEqual(json.loads((directory / 'receipt.json').read_text())['status'], 'FAILED_PARTIAL_RETAINED')

    def test_existing_object_directory_is_never_overwritten(self):
        directory = self.destination / self.expected['id']
        directory.mkdir()
        prior = directory / 'previous'
        prior.write_bytes(b'original')
        with mock.patch.object(entry, 'receive') as receive, self.assertRaises(FileExistsError):
            self.run_main(['block', '--object', self.expected['id'], '--connect',
                           '--destination', str(self.destination)])
        receive.assert_not_called()
        self.assertEqual(prior.read_bytes(), b'original')


class ReceiverTests(unittest.TestCase):
    """Use anonymous local pipes and a fake Popen; never launch a subprocess."""
    class Process:
        def __init__(self, stdout, stderr):
            self.stdin = io.BytesIO()
            self.stdout = self.make_pipe(stdout)
            self.stderr = self.make_pipe(stderr)

        @staticmethod
        def make_pipe(payload):
            read, write = os.pipe()
            os.write(write, payload)
            os.close(write)
            return os.fdopen(read, 'rb')

        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

    def test_fake_process_stream_is_hashed_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'data.partial'
            process = self.Process(b'abc', b'fixture diagnostic')
            with mock.patch.object(entry.subprocess, 'Popen', return_value=process) as popen:
                result = entry.receive(['fake-only'], b'# inert\n', path, 3, 5, 'fixture')
            self.assertEqual(result['bytes'], 3)
            self.assertEqual(result['sha256'], hashlib.sha256(b'abc').hexdigest())
            self.assertEqual(result['stderr'], b'fixture diagnostic')
            self.assertFalse(popen.call_args.kwargs['shell'])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_fake_process_over_budget_keeps_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'data.partial'
            process = self.Process(b'abcd', b'')
            with mock.patch.object(entry.subprocess, 'Popen', return_value=process), \
                 self.assertRaisesRegex(entry.ReceiveError, 'stdout_budget') as failure:
                entry.receive(['fake-only'], b'# inert\n', path, 3, 5, 'fixture')
            self.assertTrue(path.exists())
            self.assertEqual(failure.exception.partial['receive_failure'], 'stdout_budget')
            self.assertEqual(failure.exception.partial['bytes'], len(path.read_bytes()))
            self.assertEqual(failure.exception.partial['sha256'], entry.sha(path.read_bytes()))

    def test_receiver_timeout_has_partial_receipt_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'data.partial'
            process = self.Process(b'abc', b'')
            with mock.patch.object(entry.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(entry.time, 'monotonic', side_effect=[0, 6, 6]), \
                 self.assertRaisesRegex(entry.ReceiveError, 'local_ssh_timeout') as failure:
                entry.receive(['fake-only'], b'# inert\n', path, 3, 5, 'fixture')
            self.assertEqual(failure.exception.partial['receive_failure'], 'local_ssh_timeout')
            self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
