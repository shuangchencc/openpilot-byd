"""All-fake captures: no device, network, keys or actual backup images."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('capture_set_verifier_test', AREA / 'verify_capture_set.py')
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    path.chmod(0o600)


class CaptureSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.project = base / 'project'
        self.area = self.project / 'hardware/current-host-recovery'
        self.archive = base / 'private-capture'
        self.archive.mkdir(mode=0o700)
        self.scope = json.loads((AREA / 'capture-scope.json').read_bytes())
        save(self.area / 'capture-scope.json', self.scope)
        source = self.scope['source']['path']
        save(self.project / source, (verify.ROOT / source).read_bytes())
        self.scope_sha = verify.sha((self.area / 'capture-scope.json').read_bytes())
        self.allowlist = ('/data/continue.sh', '/usr/lib/systemd/system/comma.service')
        self.frozen = {'entry': b'# inert host entry fixture\n', 'block': b'# inert block source fixture\n',
                       'startup': ('SOURCE_PATHS = %r\nraise RuntimeError("NEVER_EXECUTE")\n' % (self.allowlist,)).encode()}
        save(self.project / 'tools/capture_current_host.py', self.frozen['entry'])
        save(self.area / 'block_stream_collector.py', self.frozen['block'])
        save(self.area / 'startup_collector.py', self.frozen['startup'])
        self.objects = self.scope['partition_objects'] + self.scope['gpt_metadata_objects']
        for expected in self.objects:
            self.make_block(expected)
        self.make_startup()

    def common(self, directory, phase):
        directory.mkdir(mode=0o700)
        save(directory / 'entry.py', self.frozen['entry'])
        save(directory / 'collector.py', self.frozen[phase])
        result = {'schema': verify.RECEIPT_SCHEMA, 'phase': phase,
                  'private_archive_directory': str(directory),
                  'collector_sha256': verify.sha(self.frozen[phase]),
                  'entry_sha256': verify.sha(self.frozen['entry']),
                  'started_utc': '2026-10-19T17:17:00+00:00', 'finished_utc': '2026-10-19T17:18:00+00:00',
                  'exit_code': 0, 'automatic_retries': 0, 'profile': 'UNBOUND'}
        result.update({key: False for key in verify.FALSE_PERMISSIONS})
        return result

    def make_block(self, expected):
        directory = self.archive / expected['id']
        receipt = self.common(directory, 'block')
        # Synthetic envelope only. Full decode is intentionally a retained
        # receipt claim; this verifier must never decompress test/real images.
        image = b'\x1f\x8b\x08FAKE_COMPRESSED_CONTENT' + struct.pack('<II', 0, expected['expected_bytes'] % 2**32)
        gzip_sha = verify.sha(image)
        raw_sha = verify.sha(expected['id'].encode())
        geometry = {'sector_bytes': 4096, 'parent_bytes': expected['lun_bytes']}
        source = {'node': Path(expected['node']).name, 'offset': expected['source_offset_bytes'],
                  'bytes': expected['expected_bytes'], 'lun': expected['lun']}
        if expected['capture_kind'] == 'whole_partition':
            source.update(kind='partition', start_lba=expected['first_lba'],
                          partition_number=expected['partition_number'], name=expected['name'])
            geometry.update(node_bytes=expected['expected_bytes'], start_512_sectors=expected['first_lba']*8,
                            partition_number=expected['partition_number'])
        else:
            source['kind'] = 'gpt_metadata'
            geometry['node_bytes'] = expected['lun_bytes']
        before = {'geometry': geometry, 'layout': {'fixture_mapping': 'stable'}}
        begin = {'schema': verify.BLOCK_SCHEMA, 'object_id': expected['id'], 'event': 'begin',
                 'target_writes': False, 'atomic_snapshot': False, 'source': source,
                 'before': before, 'expected_bytes': expected['expected_bytes'],
                 'consistency': 'BEST_EFFORT_LIVE_READ', 'utc': '2026-10-19T17:17:01+00:00'}
        end = {'schema': verify.BLOCK_SCHEMA, 'object_id': expected['id'], 'event': 'end',
               'status': 'COMPLETE', 'actual_bytes': expected['expected_bytes'], 'gzip_bytes': len(image),
               'raw_sha256': raw_sha, 'gzip_sha256': gzip_sha, 'after': copy.deepcopy(before),
               'metadata_stable': True, 'content_stability_proven': False,
               'utc': '2026-10-19T17:17:59+00:00'}
        receipt.update(object_id=expected['id'], status='VERIFIED_OBJECT', scope_sha256=self.scope_sha,
                       expected_object=expected, artifact='data.img.gz', sha256=gzip_sha, bytes=len(image),
                       validation={'begin': begin, 'end': end, 'local_decompression': {
                           'bytes': expected['expected_bytes'], 'sha256': raw_sha,
                           'full_decompression_verified': True, 'source_second_read_performed': False},
                           'consistency': 'BEST_EFFORT_LIVE_READ', 'cross_object_atomic': False})
        save(directory / 'data.img.gz', image)
        save(directory / 'stderr.jsonl', (json.dumps(begin)+'\n'+json.dumps(end)+'\n').encode())
        save(directory / 'receipt.json', receipt)

    def make_startup(self):
        directory = self.archive / 'startup'
        receipt = self.common(directory, 'startup')
        contents = b'# PRIVATE_CONTENT_NEVER_PRINT\n'
        metadata = {'bytes': len(contents), 'mode': 0o755}
        sample = {'status': 'collected'}
        for channel, data in [('stdout', b'PRIVATE_SYSTEMD_VALUE_NEVER_PRINT'), ('stderr', b'')]:
            sample.update({channel + '_base64': base64.b64encode(data).decode(),
                           channel + '_bytes': len(data), channel + '_sha256': verify.sha(data)})
        payload = {'schema': verify.STARTUP_SCHEMA, 'boundary': dict(profile='UNBOUND',
                   **{key: False for key in verify.STARTUP_FALSE}),
                   'files': {self.allowlist[0]: {'status': 'collected', 'metadata': metadata,
                     'content_base64': base64.b64encode(contents).decode(), 'sha256': verify.sha(contents),
                     'stable_during_read': True, 'executed': False}, self.allowlist[1]: {'status': 'unknown', 'error': 'FileNotFoundError'}},
                   'systemd_loaded': {'before': sample, 'after': copy.deepcopy(sample)},
                   'dynamic_paths': {}, 'gaps': {}, 'consistency': {'atomic_across_objects': False}}
        raw = json.dumps(payload).encode()
        receipt.update(object_id=None, status='VERIFIED_SELECTED_FILES', scope_sha256=None,
                       artifact='startup.json', sha256=verify.sha(raw), bytes=len(raw), validation={
                           'files': [{'source_path': self.allowlist[0], 'status': 'FILE_BYTES_VERIFIED',
                              'artifact': 'source-00.bin', 'bytes': len(contents), 'sha256': verify.sha(contents), 'metadata': metadata},
                             {'source_path': self.allowlist[1], 'status': 'unknown', 'error': 'FileNotFoundError'}],
                           'systemd_loaded': {p: {k: v for k, v in s.items() if not k.endswith('_base64')}
                                             for p, s in payload['systemd_loaded'].items()},
                           'dynamic_paths': {}, 'gaps': {}, 'consistency': payload['consistency']})
        save(directory / 'startup.json', raw)
        save(directory / 'source-00.bin', contents)
        save(directory / 'receipt.json', receipt)

    def check(self, hashes=False):
        return verify.verify(self.archive, hashes, self.project)

    def mutate_receipt(self, change):
        path = self.archive / self.objects[0]['id'] / 'receipt.json'
        data = json.loads(path.read_bytes())
        change(data)
        save(path, data)

    def test_default_preview_never_checks_archive(self):
        with mock.patch.object(verify, 'archive_check', side_effect=AssertionError('archive touched')):
            result = verify.verify(project_root=self.project)
        self.assertEqual(result['status'], 'PREVIEW_NO_ARCHIVE_READ')
        self.assertEqual(result['verified_object_count'], 0)

    def test_all_54_metadata_objects_and_startup_verify(self):
        result = self.check()
        self.assertEqual(result['status'], 'METADATA_VERIFIED')
        self.assertEqual(result['verified_object_count'], 54)
        self.assertEqual(result['verified_raw_bytes'], 25463984128)
        self.assertEqual(result['startup']['file_count'], 1)
        self.assertEqual(len(result['startup']['unavailable_files']), 1)
        self.assertTrue(all(x['current_archive_check']['compressed_archive_sha256_recomputed'] is None for x in result['objects']))

    def test_explicit_hashing_checks_all_compressed_files_without_decode(self):
        with mock.patch('gzip.open', side_effect=AssertionError('must not decompress')):
            result = self.check(True)
        self.assertEqual(result['status'], 'ARCHIVE_HASH_VERIFIED')
        self.assertTrue(all(x['current_archive_check']['compressed_archive_sha256_recomputed'] == x['gzip_sha256'] for x in result['objects']))

    def test_sanitized_output_has_no_raw_contents_base64_or_absolute_private_root(self):
        rendered = json.dumps(self.check())
        for forbidden in ('PRIVATE_CONTENT_NEVER_PRINT', 'PRIVATE_SYSTEMD_VALUE_NEVER_PRINT', '_base64', str(self.archive)):
            self.assertNotIn(forbidden, rendered)

    def test_missing_receipt_keeps_whole_set_incomplete(self):
        (self.archive / self.objects[0]['id'] / 'receipt.json').unlink()
        result = self.check()
        self.assertEqual(result['status'], 'INCOMPLETE_OR_INVALID')
        self.assertEqual(result['verified_object_count'], 53)

    def test_partial_status_is_never_accepted(self):
        self.mutate_receipt(lambda x: x.update(status='FAILED_PARTIAL_RETAINED'))
        result = self.check()
        self.assertEqual(result['gaps'][0]['code'], 'object_not_verified')

    def test_surviving_partial_blocks_completed_object(self):
        save(self.archive / self.objects[0]['id'] / 'data.img.gz.partial', b'partial')
        self.assertEqual(self.check()['gaps'][0]['code'], 'partial_present')

    def test_changed_source_snapshot_is_rejected(self):
        save(self.archive / self.objects[0]['id'] / 'collector.py', b'# altered\n')
        self.assertEqual(self.check()['gaps'][0]['code'], 'snapshot_receipt_hash')

    def test_old_receipts_do_not_silently_match_new_frozen_source(self):
        save(self.area / 'block_stream_collector.py', b'# new source\n')
        result = self.check()
        self.assertEqual(result['verified_object_count'], 0)
        self.assertTrue(all(x['code'] == 'snapshot_frozen_source_mismatch' for x in result['gaps']))

    def test_scope_receipt_change_is_rejected(self):
        self.mutate_receipt(lambda x: x.update(scope_sha256='0'*64))
        self.assertEqual(self.check()['gaps'][0]['code'], 'receipt_scope_mismatch')

    def test_raw_decode_evidence_mismatch_is_rejected(self):
        self.mutate_receipt(lambda x: x['validation']['local_decompression'].update(sha256='0'*64))
        self.assertEqual(self.check()['gaps'][0]['code'], 'retained_decode_evidence')

    def test_original_event_mismatch_is_rejected(self):
        self.mutate_receipt(lambda x: x['validation']['end'].update(metadata_stable=False))
        self.assertEqual(self.check()['gaps'][0]['code'], 'original_event_mismatch')

    def test_false_permissions_and_unbound_are_required(self):
        self.mutate_receipt(lambda x: x.update(can_tx=True))
        self.assertEqual(self.check()['gaps'][0]['code'], 'receipt_permissions')

    def test_changed_gzip_footer_is_rejected_without_hashing(self):
        path = self.archive / self.objects[0]['id'] / 'data.img.gz'
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 1
        save(path, bytes(raw))
        self.assertEqual(self.check()['gaps'][0]['code'], 'gzip_footer_isize')

    def test_interior_corruption_requires_explicit_hashing_and_is_detected(self):
        path = self.archive / self.objects[0]['id'] / 'data.img.gz'
        raw = bytearray(path.read_bytes())
        raw[8] ^= 1
        save(path, bytes(raw))
        self.assertEqual(self.check()['status'], 'METADATA_VERIFIED')
        self.assertEqual(self.check(True)['gaps'][0]['code'], 'archive_hash_mismatch')

    def test_source_file_saved_bytes_are_checked(self):
        save(self.archive / 'startup/source-00.bin', b'changed')
        result = self.check()
        self.assertEqual(result['gaps'][0], {'id': 'startup', 'code': 'startup_saved_file_mismatch'})

    def test_startup_raw_json_hash_is_checked(self):
        path = self.archive / 'startup/startup.json'
        save(path, path.read_bytes() + b' ')
        self.assertEqual(self.check()['gaps'][0]['code'], 'startup_payload_hash')

    def test_source_symlink_is_rejected(self):
        path = self.archive / 'startup/source-00.bin'
        target = self.archive / 'external-source'
        save(target, path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        self.assertEqual(self.check()['gaps'][0]['code'], 'symlink_path')

    def test_nonprivate_archive_file_is_rejected(self):
        (self.archive / self.objects[0]['id'] / 'data.img.gz').chmod(0o644)
        self.assertEqual(self.check()['gaps'][0]['code'], 'private_file_permissions')

    def test_in_project_root_is_rejected(self):
        inside = self.project / 'archive'
        inside.mkdir(mode=0o700)
        with self.assertRaisesRegex(verify.Invalid, 'archive_root_inside_project'):
            verify.verify(inside, project_root=self.project)

    def test_nonprivate_directory_is_rejected(self):
        self.archive.chmod(0o755)
        with self.assertRaisesRegex(verify.Invalid, 'private_directory_mode'):
            self.check()

    def test_hashing_requires_explicit_root(self):
        with self.assertRaisesRegex(verify.Invalid, 'hashing_requires_archive_root'):
            verify.verify(hash_archives=True, project_root=self.project)

    def test_same_count_but_changed_partition_mapping_is_rejected(self):
        altered = copy.deepcopy(self.scope)
        altered['partition_objects'][0]['node'] = '/dev/sde17'
        save(self.area / 'capture-scope.json', altered)
        with self.assertRaisesRegex(verify.Invalid, 'scope_partition_mapping'):
            verify.verify(project_root=self.project)

    def test_gpt_offset_is_bound_to_frozen_metadata_windows(self):
        altered = copy.deepcopy(self.scope)
        altered['gpt_metadata_objects'][0]['source_offset_bytes'] = 4096
        save(self.area / 'capture-scope.json', altered)
        with self.assertRaisesRegex(verify.Invalid, 'scope_gpt_mapping'):
            verify.verify(project_root=self.project)

    def test_archive_open_is_nonblocking_before_regular_file_check(self):
        path = self.archive / self.objects[0]['id'] / 'data.img.gz'
        original_open = os.open
        flags_seen = []

        def observed_open(target, flags, *args, **kwargs):
            flags_seen.append(flags)
            return original_open(target, flags, *args, **kwargs)

        with mock.patch.object(verify.os, 'open', side_effect=observed_open):
            stream, info = verify.regular_open(path)
            stream.close()
        self.assertEqual(len(flags_seen), 1)
        self.assertTrue(flags_seen[0] & os.O_NONBLOCK)
        self.assertTrue(flags_seen[0] & os.O_NOFOLLOW)

    def test_fifo_replacement_is_rejected_without_waiting_for_writer(self):
        path = self.archive / self.objects[0]['id'] / 'data.img.gz'
        path.unlink()
        os.mkfifo(path, 0o600)
        original_open = os.open

        def guarded_open(target, flags, *args, **kwargs):
            # Fail before the syscall if the fix regresses, so this negative
            # test itself can never hang waiting for a FIFO writer.
            self.assertTrue(flags & os.O_NONBLOCK)
            return original_open(target, flags, *args, **kwargs)

        with mock.patch.object(verify.os, 'open', side_effect=guarded_open), \
             self.assertRaisesRegex(verify.Invalid, 'not_regular_file'):
            verify.regular_open(path)


if __name__ == '__main__':
    unittest.main()
