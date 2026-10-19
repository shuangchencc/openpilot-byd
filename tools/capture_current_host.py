#!/usr/bin/env python3
"""Explicit read-only captures to private host storage; preview by default.

No target files installed, service control, slot/Panda operation, or CAN TX.
Raw files and images are never placed in the engineering repository. No retry.
"""
import argparse
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import selectors
import subprocess
import time
import zlib

ROOT = Path(__file__).resolve().parents[1]
AREA = ROOT / 'hardware/current-host-recovery'
SCHEMAS = {'startup': 'songplus-current-host-startup-passive/v1',
           'block': 'songplus-current-host-block-stream/v1'}


class ReceiveError(ValueError):
    def __init__(self, reason, partial):
        super().__init__(reason)
        self.partial = partial


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def reject(value):
        raise ValueError('nonfinite_json')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def private_write(path, data):
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write(data)


def ssh_command(connection, phase, object_id):
    require(not connection.is_symlink() and 0 < connection.stat().st_size <= 16384, 'connection_file')
    config = strict_json(connection.read_bytes())
    ipaddress.ip_address(config['host'])
    require(config.get('user') == 'comma' and type(config.get('port')) is int and 1 <= config['port'] <= 65535, 'endpoint')
    identity = Path(config['identity_file'])
    require(identity.is_absolute() and identity.is_file(), 'existing_identity_required')
    remote = '/usr/bin/python3 -I -S -B -'
    if phase == 'block':
        # IDs come only from the fixed scope, never from remote output or a path.
        require(object_id and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' for c in object_id), 'object_id')
        remote += ' ' + object_id
    return ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-p', str(config['port']), '-i', str(identity),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UpdateHostKeys=no', '-o', 'ClearAllForwardings=yes', '-o', 'ControlMaster=no',
            '-o', 'ControlPath=none', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10',
            '-o', 'ServerAliveCountMax=2', 'comma@' + config['host'], remote]


def receive(command, source, output, maximum, timeout, object_id):
    """Bounded local receiver. Timeout terminates only this diagnostic SSH child."""
    digest = hashlib.sha256()
    error = bytearray()
    count = 0
    selector = selectors.DefaultSelector()
    process = None
    started = time.monotonic()
    last_progress = started
    try:
        with output.open('xb') as stream:
            os.chmod(output, 0o600)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, shell=False, close_fds=True)
            # The isolated interpreter consumes the complete script before running.
            process.stdin.write(source)
            process.stdin.close()
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                require(time.monotonic() - started < timeout, 'local_ssh_timeout')
                for key, unused in selector.select(0.2):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == 'stdout':
                        require(count + len(chunk) <= maximum, 'stdout_budget')
                        stream.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                    else:
                        require(len(error) + len(chunk) <= 65536, 'stderr_budget')
                        error.extend(chunk)
                if time.monotonic() - last_progress >= 30:
                    print(json.dumps({'status': 'RECEIVING', 'object_id': object_id,
                                      'received_bytes': count, 'elapsed_seconds': round(time.monotonic() - started, 1)}), flush=True)
                    last_progress = time.monotonic()
            remaining = timeout - (time.monotonic() - started)
            require(remaining > 0, 'local_ssh_timeout')
            code = process.wait(timeout=remaining)
            stream.flush()
            os.fsync(stream.fileno())
        return {'exit_code': code, 'bytes': count, 'sha256': digest.hexdigest(),
                'stderr': bytes(error), 'elapsed_seconds': time.monotonic() - started}
    except (OSError, ValueError, subprocess.SubprocessError) as problem:
        allowed = {'local_ssh_timeout', 'stdout_budget', 'stderr_budget'}
        reason = str(problem) if isinstance(problem, ValueError) and str(problem) in allowed else type(problem).__name__
        raise ReceiveError(reason, {'exit_code': process.poll() if process is not None else None,
                                   'bytes': count, 'sha256': digest.hexdigest(), 'stderr': bytes(error),
                                   'elapsed_seconds': time.monotonic() - started,
                                   'receive_failure': reason}) from problem
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()


def verify_gzip(path, expected_bytes, expected_sha):
    digest, count = hashlib.sha256(), 0
    with gzip.open(path, 'rb') as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            require(count <= expected_bytes, 'expanded_size_exceeded')
            digest.update(chunk)
    require(count == expected_bytes and digest.hexdigest() == expected_sha, 'expanded_stream_mismatch')
    return {'bytes': count, 'sha256': digest.hexdigest(), 'full_decompression_verified': True,
            'source_second_read_performed': False}


def validate_block(result, expected, path):
    require(result['exit_code'] == 0, 'ssh_or_collector_failed')
    events = [strict_json(line) for line in result['stderr'].splitlines() if line.strip()]
    require(len(events) >= 2 and events[0]['event'] == 'begin' and events[-1]['event'] == 'end', 'stream_receipts')
    for event in events:
        require(event['schema'] == SCHEMAS['block'] and event['object_id'] == expected['id'], 'stream_identity')
    require(all(e['event'] == 'progress' for e in events[1:-1]), 'unexpected_event')
    begin, end = events[0], events[-1]
    require(begin['expected_bytes'] == expected['expected_bytes'] and begin['target_writes'] is False, 'source_scope')
    require(begin['source']['node'] == Path(expected['node']).name and begin['source']['offset'] == expected['source_offset_bytes'], 'source_range')
    require(end['status'] == 'COMPLETE' and end['actual_bytes'] == expected['expected_bytes'], 'incomplete_object')
    require(end['metadata_stable'] is True and begin['before'] == end['after'], 'source_metadata_drift')
    require(end['gzip_bytes'] == result['bytes'] and end['gzip_sha256'] == result['sha256'], 'gzip_stream_mismatch')
    checked = verify_gzip(path, end['actual_bytes'], end['raw_sha256'])
    return {'begin': begin, 'end': end, 'local_decompression': checked,
            'consistency': 'BEST_EFFORT_LIVE_READ', 'cross_object_atomic': False}


def validate_startup(result, path, directory):
    require(result['exit_code'] == 0, 'ssh_or_collector_failed')
    data = strict_json(path.read_bytes())
    require(data['schema'] == SCHEMAS['startup'], 'startup_schema')
    require(data['boundary']['profile'] == 'UNBOUND', 'profile')
    for key in ('services_changed', 'target_files_written', 'device_data_read',
                'target_programs_imported_or_executed', 'slot_changed', 'panda_reset_or_flash', 'can_tx'):
        require(data['boundary'][key] is False, 'startup_boundary')
    manifest = []
    for index, (name, item) in enumerate(sorted(data['files'].items())):
        if item.get('status') != 'collected':
            manifest.append({'source_path': name, 'status': item.get('status'), 'error': item.get('error')})
            continue
        raw = base64.b64decode(item['content_base64'], validate=True)
        require(len(raw) == item['metadata']['bytes'] and sha(raw) == item['sha256'], 'source_file_mismatch')
        filename = 'source-%02d.bin' % index
        private_write(directory / filename, raw)
        manifest.append({'source_path': name, 'status': 'FILE_BYTES_VERIFIED', 'artifact': filename,
                         'bytes': len(raw), 'sha256': sha(raw), 'metadata': item['metadata']})
    return {'files': manifest, 'systemd_loaded': {key: ({k: v for k, v in value.items() if not k.endswith('_base64')}
                     if isinstance(value, dict) else value) for key, value in data['systemd_loaded'].items()},
            'dynamic_paths': data['dynamic_paths'], 'gaps': data['gaps'], 'consistency': data['consistency']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=SCHEMAS)
    parser.add_argument('--object')
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--connection', type=Path, default=ROOT / 'local/c3xl-connection.json')
    args = parser.parse_args()
    collector = AREA / ('startup_collector.py' if args.phase == 'startup' else 'block_stream_collector.py')
    source = collector.read_bytes()
    expected = None
    scope_raw = None
    if args.phase == 'block':
        scope_raw = (AREA / 'capture-scope.json').read_bytes()
        scope = strict_json(scope_raw)
        objects = scope['partition_objects'] + scope['gpt_metadata_objects']
        found = [obj for obj in objects if obj['id'] == args.object]
        require(len(found) == 1, 'fixed_object_required')
        expected = found[0]
    if not args.connect:
        print(json.dumps({'mode': 'PREVIEW_NO_CONNECTION', 'phase': args.phase, 'object_id': args.object,
                          'collector_sha256': sha(source), 'config_read': False,
                          'expected_bytes': expected['expected_bytes'] if expected else None}))
        return 0
    require(args.destination is not None and args.destination.is_absolute(), 'private_destination_required')
    destination = args.destination
    require(not any(p.is_symlink() for p in (destination, *destination.parents)), 'symlink_destination')
    require(destination.is_dir() and not destination.resolve().is_relative_to(ROOT), 'destination_must_be_external')
    require(destination.stat().st_mode & 0o077 == 0, 'private_directory_mode')
    os.umask(0o077)
    free = os.statvfs(destination)
    require(free.f_bavail * free.f_frsize >= (expected['expected_bytes'] if expected else 8*1024*1024) + 4*1024**3, 'destination_capacity')
    command = ssh_command(args.connection, args.phase, args.object)
    directory = destination / (args.object if expected else 'startup')
    directory.mkdir(mode=0o700)
    private_write(directory / 'collector.py', source)
    entry = Path(__file__).read_bytes()
    private_write(directory / 'entry.py', entry)
    partial = directory / ('data.img.gz.partial' if expected else 'startup.json.partial')
    receipt = {'schema': 'songplus-current-host-capture-receipt/v1', 'phase': args.phase,
               'object_id': args.object, 'started_utc': utc(), 'collector_sha256': sha(source),
               'entry_sha256': sha(entry), 'private_archive_directory': str(directory),
               'scope_sha256': sha(scope_raw) if scope_raw else None,
               'expected_object': expected,
               'destination_available_bytes_at_start': free.f_bavail * free.f_frsize,
               'target_writes': False, 'service_control': False, 'slot_change': False,
               'panda_active_io_reset_flash': False, 'can_tx': False, 'profile': 'UNBOUND',
               'automatic_retries': 0, 'remote_atime_or_service_logging_possible': True}
    try:
        maximum = expected['expected_bytes'] * 102 // 100 + 1024*1024 if expected else 8*1024*1024
        timeout = 1830 if expected and expected['expected_bytes'] > 1024**3 else 210 if expected else 45
        receipt.update(local_ssh_timeout_seconds=timeout, max_received_bytes=maximum)
        result = receive(command, source, partial, maximum, timeout, args.object)
        private_write(directory / 'stderr.jsonl', result['stderr'])
        receipt.update({k: v for k, v in result.items() if k != 'stderr'})
        receipt['validation'] = validate_block(result, expected, partial) if expected else validate_startup(result, partial, directory)
        require(collector.read_bytes() == source and Path(__file__).read_bytes() == entry, 'source_changed')
        final = directory / ('data.img.gz' if expected else 'startup.json')
        partial.rename(final)
        receipt.update(status='VERIFIED_OBJECT' if expected else 'VERIFIED_SELECTED_FILES', artifact=final.name)
    except (OSError, ValueError, KeyError, TypeError, EOFError, zlib.error, subprocess.SubprocessError) as error:
        if isinstance(error, ReceiveError):
            private_write(directory / 'stderr.jsonl', error.partial['stderr'])
            receipt.update({k: v for k, v in error.partial.items() if k != 'stderr'})
        receipt.update(status='FAILED_PARTIAL_RETAINED', error_type=type(error).__name__)
    receipt['finished_utc'] = utc()
    private_write(directory / 'receipt.json', (json.dumps(receipt, indent=2)+'\n').encode())
    print(json.dumps({'status': receipt['status'], 'object_id': args.object, 'receipt': str(directory / 'receipt.json')}), flush=True)
    return 0 if receipt['status'].startswith('VERIFIED_') else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'UNAVAILABLE', 'error_type': type(error).__name__}))
        raise SystemExit(2)
