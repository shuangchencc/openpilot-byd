#!/usr/bin/env python3
"""Offline verifier: no connection and no image hashing unless explicitly asked.

Without --archive-root, only the engineering scope is read. With it, bounded
receipts/source files/startup material plus each gzip's 3-byte prefix and 8-byte
footer are checked. --hash-archives additionally streams compressed archives;
it never decompresses them or repeats a target read. Historical full-decode
evidence is checked against the retained receipt, not claimed as a fresh decode.
"""
import argparse
import ast
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 'songplus-current-host-object-manifest/v1'
RECEIPT_SCHEMA = 'songplus-current-host-capture-receipt/v1'
BLOCK_SCHEMA = 'songplus-current-host-block-stream/v1'
STARTUP_SCHEMA = 'songplus-current-host-startup-passive/v1'
SCOPE_SCHEMA = 'songplus-current-host-capture-scope/v1'
FALSE_PERMISSIONS = ('target_writes', 'service_control', 'slot_change',
                     'panda_active_io_reset_flash', 'can_tx')
STARTUP_FALSE = ('services_changed', 'target_files_written', 'device_data_read',
                 'target_programs_imported_or_executed', 'slot_changed',
                 'panda_reset_or_flash', 'can_tx')
HEX = re.compile(r'[0-9a-f]{64}\Z')
SAFE_ID = re.compile(r'[A-Za-z0-9_]+\Z')


class Invalid(ValueError):
    """Only fixed, non-sensitive diagnostic codes are passed to this class."""


def require(value, code):
    if not value:
        raise Invalid(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'duplicate_json_key')
            value[key] = item
        return value

    def reject(unused):
        raise Invalid('nonfinite_json')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def no_symlink_path(path):
    require(path.is_absolute(), 'absolute_path_required')
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink_path')


def private_directory(path):
    no_symlink_path(path)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_mode & 0o077 == 0, 'private_directory_mode')
    require(info.st_uid == os.getuid(), 'private_directory_owner')


def regular_open(path, limit=None, private=True):
    no_symlink_path(path)
    # A replaced FIFO/device must not block before the fstat type check.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode), 'not_regular_file')
        require(limit is None or 0 <= info.st_size <= limit, 'small_file_limit')
        if private:
            require(info.st_mode & 0o077 == 0 and info.st_uid == os.getuid(), 'private_file_permissions')
        return os.fdopen(fd, 'rb'), info
    except BaseException:
        os.close(fd)
        raise


def read_small(path, limit, private=True):
    stream, before = regular_open(path, limit, private)
    with stream:
        raw = stream.read(limit + 1)
        require(len(raw) <= limit and len(raw) == before.st_size, 'small_file_length')
        require(identity(os.fstat(stream.fileno())) == identity(before), 'local_file_drift')
    return raw


def digest_value(value):
    require(isinstance(value, str) and HEX.fullmatch(value), 'invalid_digest')
    return value


def timestamp(value):
    require(isinstance(value, str), 'invalid_timestamp')
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, 'timestamp_timezone')
    return parsed


def load_scope(root):
    area = root / 'hardware/current-host-recovery'
    raw = read_small(area / 'capture-scope.json', 512 * 1024, False)
    scope = strict_json(raw)
    require(scope['schema'] == SCOPE_SCHEMA, 'scope_schema')
    require(scope['boundary']['real_profile'] == 'UNBOUND', 'scope_profile')
    require(all(scope['boundary'][k] is False for k in ('target_write', 'start_stop_services',
        'slot_change', 'panda_reset_flash_or_active_io', 'can_tx')), 'scope_permissions')
    parts, metadata = scope['partition_objects'], scope['gpt_metadata_objects']
    require(len(parts) == 42 and len(metadata) == 12, 'scope_count')
    require(sum(x['expected_bytes'] for x in parts) == 25463713792 and
            sum(x['expected_bytes'] for x in metadata) == 270336, 'scope_total')
    objects = parts + metadata
    ids = [x['id'] for x in objects]
    require(len(set(ids)) == 54 and all(SAFE_ID.fullmatch(x) for x in ids), 'scope_ids')
    require(len(scope['excluded_partition_objects']) == 28, 'scope_excluded_count')
    for obj in objects:
        require(re.fullmatch(r'/dev/sd[a-f](?:[1-9][0-9]?)?', obj['node']), 'scope_node')
        require(type(obj['expected_bytes']) is int and obj['expected_bytes'] > 0, 'scope_length')
        require(type(obj['source_offset_bytes']) is int and obj['source_offset_bytes'] >= 0, 'scope_offset')
        require(obj['sector_bytes'] == 4096, 'scope_sector')
        require((obj['last_lba'] - obj['first_lba'] + 1) * 4096 == obj['expected_bytes'], 'scope_range')
    source = root / scope['source']['path']
    require(source.resolve().is_relative_to(root.resolve()), 'scope_source_outside_project')
    source_raw = read_small(source, 1024 * 1024, False)
    require(sha(source_raw) == scope['source']['sha256'], 'scope_source_hash')
    matrix = strict_json(source_raw)['object_coverage']
    matrix_parts = {x['id']: x for x in matrix['partitions']}
    matrix_luns = {x['id'][4:]: x for x in matrix['luns']}
    selected_ids = {x['id'] for x in matrix['partitions'] if
                    not x['contains_potentially_private_or_unique_state'] and not x['name'].startswith('ALIGN_TO_')}
    require({x['id'] for x in parts} == selected_ids and
            {x['id'] for x in scope['excluded_partition_objects']} == set(matrix_parts) - selected_ids,
            'scope_partition_selection')
    for obj in parts:
        original = matrix_parts[obj['id']]
        require(all(obj[key] == original[key] for key in ('name', 'lun', 'partition_number', 'first_lba', 'last_lba')) and
                obj['node'] == original['device_at_observation'] and obj['expected_bytes'] == original['bytes'] and
                obj['source_offset_bytes'] == 0 and obj['lun_bytes'] == matrix_luns[obj['lun']]['bytes'], 'scope_partition_mapping')
    require({x['id'] for x in metadata} == {'gpt_' + lun + '_' + side for lun in matrix_luns for side in ('primary', 'secondary')}, 'scope_gpt_ids')
    for obj in metadata:
        size = matrix_luns[obj['lun']]['bytes']
        primary = obj['id'].endswith('_primary')
        require(obj['node'] == '/dev/' + obj['lun'] and obj['lun_bytes'] == size and
                obj['source_offset_bytes'] == (0 if primary else size - 5 * 4096) and
                obj['expected_bytes'] == (6 if primary else 5) * 4096, 'scope_gpt_mapping')
    frozen = {'entry': read_small(root / 'tools/capture_current_host.py', 256 * 1024, False),
              'block': read_small(area / 'block_stream_collector.py', 256 * 1024, False),
              'startup': read_small(area / 'startup_collector.py', 256 * 1024, False)}
    allowlist = None
    for node in ast.parse(frozen['startup']).body:
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id == 'SOURCE_PATHS' for x in node.targets):
            allowlist = ast.literal_eval(node.value)
    require(isinstance(allowlist, tuple) and all(isinstance(x, str) for x in allowlist), 'startup_allowlist')
    return scope, sha(raw), frozen, allowlist


def common_receipt(directory, phase, frozen):
    private_directory(directory)
    raw = read_small(directory / 'receipt.json', 512 * 1024)
    receipt = strict_json(raw)
    require(receipt['schema'] == RECEIPT_SCHEMA and receipt['phase'] == phase, 'receipt_schema_phase')
    require(receipt['private_archive_directory'] == str(directory), 'private_reference_mismatch')
    require(receipt['profile'] == 'UNBOUND' and all(receipt[k] is False for k in FALSE_PERMISSIONS), 'receipt_permissions')
    require(receipt['exit_code'] == 0 and receipt['automatic_retries'] == 0, 'receipt_execution_status')
    require(timestamp(receipt['started_utc']) <= timestamp(receipt['finished_utc']), 'host_time_order')
    for key, filename in (('collector', 'collector.py'), ('entry', 'entry.py')):
        contents = read_small(directory / filename, 256 * 1024)
        digest = sha(contents)
        require(digest == receipt[key + '_sha256'], 'snapshot_receipt_hash')
        require(contents == frozen[phase if key == 'collector' else 'entry'], 'snapshot_frozen_source_mismatch')
    return receipt, sha(raw)


def archive_check(path, expected_size, expected_sha, raw_size, hash_archives):
    stream, before = regular_open(path)
    with stream:
        require(before.st_size == expected_size and expected_size >= 18, 'archive_size')
        require(stream.read(3) == b'\x1f\x8b\x08', 'gzip_prefix')
        stream.seek(-8, os.SEEK_END)
        footer = stream.read(8)
        require(len(footer) == 8 and struct.unpack('<II', footer)[1] == raw_size % (2**32), 'gzip_footer_isize')
        on_disk_sha = None
        if hash_archives:
            stream.seek(0)
            digest = hashlib.sha256()
            count = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
                count += len(chunk)
                require(count <= expected_size, 'archive_hash_length')
            require(count == expected_size, 'archive_hash_length')
            on_disk_sha = digest.hexdigest()
            require(on_disk_sha == expected_sha, 'archive_hash_mismatch')
        require(identity(before) == identity(os.fstat(stream.fileno())), 'local_file_drift')
    return {'gzip_prefix_and_footer_size_checked': True,
            'gzip_footer_crc_recomputed': False,
            'full_decompression_repeated': False,
            'compressed_archive_sha256_recomputed': on_disk_sha}


def verify_block(root, expected, scope_sha, frozen, hash_archives):
    directory = root / expected['id']
    receipt, receipt_sha = common_receipt(directory, 'block', frozen)
    require(receipt['object_id'] == expected['id'] and receipt['status'] == 'VERIFIED_OBJECT', 'object_not_verified')
    require(receipt['scope_sha256'] == scope_sha and receipt['expected_object'] == expected, 'receipt_scope_mismatch')
    require(receipt['artifact'] == 'data.img.gz', 'archive_artifact')
    require(not (directory / 'data.img.gz.partial').exists(), 'partial_present')
    validation = receipt['validation']
    begin, end, decoded = validation['begin'], validation['end'], validation['local_decompression']
    diagnostic = read_small(directory / 'stderr.jsonl', 65536)
    events = [strict_json(line) for line in diagnostic.splitlines() if line.strip()]
    require(len(events) >= 2 and events[0] == begin and events[-1] == end, 'original_event_mismatch')
    for position, event in enumerate(events):
        require(event['schema'] == BLOCK_SCHEMA and event['object_id'] == expected['id'], 'event_identity')
        require(event['event'] == ('begin' if position == 0 else 'end' if position == len(events)-1 else 'progress'), 'event_order')
    require(begin['target_writes'] is False and begin['atomic_snapshot'] is False, 'begin_permissions')
    require(begin['expected_bytes'] == expected['expected_bytes'], 'begin_length')
    source = begin['source']
    require(source['node'] == Path(expected['node']).name and source['offset'] == expected['source_offset_bytes'] and
            source['bytes'] == expected['expected_bytes'] and source['lun'] == expected['lun'], 'begin_source_range')
    geometry = begin['before']['geometry']
    require(geometry['sector_bytes'] == 4096 and geometry['parent_bytes'] == expected['lun_bytes'], 'geometry_parent')
    if expected['capture_kind'] == 'whole_partition':
        require(source['kind'] == 'partition' and source['start_lba'] == expected['first_lba'] and
                source['partition_number'] == expected['partition_number'] and source['name'] == expected['name'], 'partition_source')
        require(geometry['node_bytes'] == expected['expected_bytes'] and
                geometry['start_512_sectors'] == expected['first_lba'] * 8 and
                geometry['partition_number'] == expected['partition_number'], 'partition_geometry')
    else:
        require(source['kind'] == 'gpt_metadata' and geometry['node_bytes'] == expected['lun_bytes'], 'gpt_geometry')
    require(begin['before'] == end['after'] and end['metadata_stable'] is True, 'metadata_drift')
    require(end['content_stability_proven'] is False, 'overclaimed_content_stability')
    require(end['status'] == 'COMPLETE' and end['actual_bytes'] == expected['expected_bytes'], 'end_length_status')
    require(timestamp(begin['utc']) <= timestamp(end['utc']), 'remote_time_order')
    raw_sha, gzip_sha = digest_value(end['raw_sha256']), digest_value(end['gzip_sha256'])
    require(decoded['full_decompression_verified'] is True and decoded['source_second_read_performed'] is False and
            decoded['bytes'] == end['actual_bytes'] and decoded['sha256'] == raw_sha, 'retained_decode_evidence')
    require(receipt['sha256'] == gzip_sha and receipt['bytes'] == end['gzip_bytes'], 'transport_digest')
    require(validation['cross_object_atomic'] is False and validation['consistency'] == 'BEST_EFFORT_LIVE_READ' and
            begin['consistency'] == 'BEST_EFFORT_LIVE_READ', 'consistency_claim')
    current = archive_check(directory / 'data.img.gz', end['gzip_bytes'], gzip_sha, end['actual_bytes'], hash_archives)
    return {'id': expected['id'], 'status': 'ARCHIVE_HASH_VERIFIED' if hash_archives else 'METADATA_VERIFIED',
            'node': expected['node'], 'offset': expected['source_offset_bytes'],
            'expected_bytes': expected['expected_bytes'], 'actual_bytes': end['actual_bytes'],
            'raw_sha256': raw_sha, 'gzip_sha256': gzip_sha, 'gzip_bytes': end['gzip_bytes'],
            'started_utc': receipt['started_utc'], 'finished_utc': receipt['finished_utc'],
            'source_started_utc': begin['utc'], 'source_finished_utc': end['utc'],
            'receipt_sha256': receipt_sha, 'collector_sha256': receipt['collector_sha256'],
            'entry_sha256': receipt['entry_sha256'], 'scope_sha256': scope_sha,
            'private_reference': expected['id'] + '/data.img.gz',
            'consistency': 'BEST_EFFORT_LIVE_READ', 'cross_object_atomic': False,
            'full_decompression_verified_at_capture': True, 'current_archive_check': current}


def verify_startup(root, frozen, allowlist):
    directory = root / 'startup'
    receipt, receipt_sha = common_receipt(directory, 'startup', frozen)
    require(receipt['status'] == 'VERIFIED_SELECTED_FILES' and receipt['object_id'] is None, 'startup_not_verified')
    require(receipt['artifact'] == 'startup.json' and receipt['scope_sha256'] is None, 'startup_artifact')
    require(not (directory / 'startup.json.partial').exists(), 'startup_partial_present')
    raw = read_small(directory / 'startup.json', 8 * 1024 * 1024)
    require(sha(raw) == receipt['sha256'] and len(raw) == receipt['bytes'], 'startup_payload_hash')
    payload = strict_json(raw)
    require(payload['schema'] == STARTUP_SCHEMA, 'startup_schema')
    require(payload['boundary']['profile'] == 'UNBOUND' and
            all(payload['boundary'][k] is False for k in STARTUP_FALSE), 'startup_permissions')
    require(set(payload['files']) == set(allowlist), 'startup_files_allowlist')
    records = receipt['validation']['files']
    require(len(records) == len(allowlist) and len({x['source_path'] for x in records}) == len(records), 'startup_records')
    by_path = {x['source_path']: x for x in records}
    verified, unavailable, total = [], [], 0
    for index, (name, value) in enumerate(sorted(payload['files'].items())):
        record = by_path[name]
        if value['status'] != 'collected':
            require(record['status'] == value['status'] and record.get('error') == value.get('error'), 'startup_missing_record')
            unavailable.append({'source_path': name, 'status': 'UNAVAILABLE_AT_CAPTURE'})
            continue
        contents = base64.b64decode(value['content_base64'], validate=True)
        require(len(contents) <= 256 * 1024 and len(contents) == value['metadata']['bytes'], 'startup_source_length')
        digest = sha(contents)
        require(digest == value['sha256'] and value['stable_during_read'] is True and value['executed'] is False, 'startup_source_hash')
        expected_artifact = 'source-%02d.bin' % index
        require(record['status'] == 'FILE_BYTES_VERIFIED' and record['artifact'] == expected_artifact and
                record['bytes'] == len(contents) and record['sha256'] == digest and record['metadata'] == value['metadata'], 'startup_file_receipt')
        require(read_small(directory / expected_artifact, 256 * 1024) == contents, 'startup_saved_file_mismatch')
        total += len(contents)
        verified.append({'source_path': name, 'bytes': len(contents), 'sha256': digest,
                         'private_reference': 'startup/' + expected_artifact})
    require(verified and total <= 4 * 1024 * 1024, 'startup_total')
    stripped = {key: ({k: v for k, v in value.items() if not k.endswith('_base64')}
                     if isinstance(value, dict) else value) for key, value in payload['systemd_loaded'].items()}
    require(stripped == receipt['validation']['systemd_loaded'], 'startup_systemd_record')
    for position in ('before', 'after'):
        sample = payload['systemd_loaded'][position]
        if sample['status'] != 'collected':
            continue
        for channel in ('stdout', 'stderr'):
            value = base64.b64decode(sample[channel + '_base64'], validate=True)
            require(len(value) == sample[channel + '_bytes'] and sha(value) == sample[channel + '_sha256'], 'systemd_payload_hash')
    for key in ('dynamic_paths', 'gaps', 'consistency'):
        require(payload[key] == receipt['validation'][key], 'startup_metadata_record')
    require(payload['consistency']['atomic_across_objects'] is False, 'startup_atomic_claim')
    return {'status': 'SELECTED_FILES_VERIFIED', 'file_count': len(verified), 'file_bytes': total,
            'files': verified, 'unavailable_files': unavailable,
            'startup_payload_sha256': sha(raw), 'startup_payload_bytes': len(raw),
            'receipt_sha256': receipt_sha, 'collector_sha256': receipt['collector_sha256'],
            'entry_sha256': receipt['entry_sha256'], 'started_utc': receipt['started_utc'],
            'finished_utc': receipt['finished_utc'], 'private_reference': 'startup/startup.json',
            'cross_object_atomic': False, 'full_application_archive': False}


def failure_code(error):
    return str(error) if isinstance(error, Invalid) else type(error).__name__


def verify(archive_root=None, hash_archives=False, project_root=ROOT):
    scope, scope_sha, frozen, allowlist = load_scope(project_root)
    output = {'schema': SCHEMA, 'checked_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'PREVIEW_NO_ARCHIVE_READ', 'profile': 'UNBOUND',
              'scope_sha256': scope_sha, 'expected_object_count': 54,
              'verified_object_count': 0, 'expected_raw_bytes': 25463984128,
              'archive_hashing_requested': hash_archives, 'target_connections': 0,
              'target_actions': [], 'cross_object_atomic': False,
              'source_second_read_performed': False, 'decompression_repeated': False,
              'objects': [], 'startup': None, 'gaps': []}
    if archive_root is None:
        require(not hash_archives, 'hashing_requires_archive_root')
        return output
    private_directory(archive_root)
    require(not archive_root.resolve().is_relative_to(project_root.resolve()), 'archive_root_inside_project')
    output['private_archive_set_reference_sha256'] = sha(str(archive_root).encode())
    try:
        output['startup'] = verify_startup(archive_root, frozen, allowlist)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
        output['gaps'].append({'id': 'startup', 'code': failure_code(error)})
    objects = scope['partition_objects'] + scope['gpt_metadata_objects']
    for expected in objects:
        try:
            output['objects'].append(verify_block(archive_root, expected, scope_sha, frozen, hash_archives))
        except (OSError, ValueError, KeyError, TypeError) as error:
            output['gaps'].append({'id': expected['id'], 'code': failure_code(error)})
    output['verified_object_count'] = len(output['objects'])
    output['verified_raw_bytes'] = sum(x['actual_bytes'] for x in output['objects'])
    output['verified_gzip_bytes'] = sum(x['gzip_bytes'] for x in output['objects'])
    output['status'] = ('INCOMPLETE_OR_INVALID' if output['gaps'] else
                        'ARCHIVE_HASH_VERIFIED' if hash_archives else 'METADATA_VERIFIED')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive-root', type=Path)
    parser.add_argument('--hash-archives', action='store_true')
    args = parser.parse_args()
    try:
        result = verify(args.archive_root, args.hash_archives)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
        result = {'schema': SCHEMA, 'status': 'INCOMPLETE_OR_INVALID',
                  'gaps': [{'id': 'capture_set', 'code': failure_code(error)}]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 2 if result['status'] == 'INCOMPLETE_OR_INVALID' else 0


if __name__ == '__main__':
    raise SystemExit(main())
