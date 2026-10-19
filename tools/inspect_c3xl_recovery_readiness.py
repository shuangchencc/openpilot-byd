#!/usr/bin/env python3
"""Explicit passive recovery/supervisor metadata capture; preview by default.

No sudo, target software launch, service control or USB/SPI control I/O. The
gpt-header phase reads fixed sde metadata with O_RDONLY. Existing SSH credentials
are used without reading their contents; no remote file is installed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = {
    'supervisor': ('hardware/recovery-readiness/supervisor_collector.py', 'songplus-recovery-supervisor-passive/v1'),
    'gpt-header': ('hardware/recovery-readiness/gpt_header_collector.py', 'songplus-recovery-gpt-header-passive/v1'),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate JSON key')
            out[key] = value
        return out
    def reject(value):
        raise ValueError('nonfinite JSON value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=COLLECTORS, nargs='?', default='supervisor')
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--connection', type=Path, default=ROOT / 'local/c3xl-connection.json')
    args = parser.parse_args()
    collector, schema = COLLECTORS[args.phase]
    source = (ROOT / collector).read_bytes()
    if not args.connect:
        print(json.dumps({'mode': 'preview_no_connection', 'collector': collector,
                          'collector_sha256': sha(source), 'config_read': False}))
        return 0
    if not 0 < args.connection.stat().st_size <= 16384:
        raise ValueError('bounded connection configuration required')
    config = strict_json(args.connection.read_bytes())
    ipaddress.ip_address(config['host'])
    if config.get('user') != 'comma' or type(config.get('port')) is not int or not 1 <= config['port'] <= 65535:
        raise ValueError('unexpected SSH endpoint')
    identity = Path(config['identity_file'])
    if not identity.is_absolute() or not identity.is_file():
        raise ValueError('existing absolute identity path required')
    command = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-p', str(config['port']), '-i', str(identity),
               '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UpdateHostKeys=no', '-o', 'ClearAllForwardings=yes', '-o', 'ControlMaster=no',
               '-o', 'ControlPath=none', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10',
               '-o', 'ServerAliveCountMax=2', 'comma@' + config['host'], '/usr/bin/python3 -I -S -B -']
    out = ROOT / 'results' / ('recovery-readiness-' + args.phase + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    if any(p.is_symlink() for p in (out, *out.parents)):
        raise ValueError('symlink output forbidden')
    out.mkdir(mode=0o700)
    entry = Path(__file__).read_bytes()
    (out / 'collector.py').write_bytes(source)
    (out / 'entry.py').write_bytes(entry)
    receipt = {'schema': 'songplus-recovery-readiness-receipt/v1',
               'phase': args.phase, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
               'collector': collector, 'collector_sha256': sha(source), 'entry_sha256': sha(entry),
               'host_key_check': 'strict against preexisting known_hosts',
               'privilege_escalated': False, 'target_writes_requested': False,
               'services_controlled': False, 'usb_spi_control_io': False, 'can_tx': False,
               'target_stack_started': False, 'profile_binding_changed': False,
               'note': 'Existing services keep running; SSH may log. Non-atomic metadata observation only.'}
    try:
        process = subprocess.run(command, input=source, capture_output=True, timeout=45)
        raw, error = process.stdout, process.stderr
        receipt['exit_code'] = process.returncode
        valid = False
        try:
            if len(raw) > 1024 * 1024:
                raise ValueError('output bound')
            data = strict_json(raw)
            valid = (process.returncode == 0 and isinstance(data, dict) and data.get('schema') == schema
                     and isinstance(data.get('collected_utc'), str))
        except (ValueError, UnicodeError, TypeError):
            pass
        receipt.update(status='COLLECTED_PASSIVE_ONLY' if valid else 'FAILED', valid_payload=valid)
    except subprocess.TimeoutExpired as exc:
        raw, error = exc.stdout or b'', exc.stderr or b''
        receipt.update(status='TIMEOUT', valid_payload=False, exit_code=None)
    except OSError as exc:
        raw, error = b'', type(exc).__name__.encode()
        receipt.update(status='SSH_UNAVAILABLE', valid_payload=False, exit_code=None)
    (out / 'stdout.json').write_bytes(raw)
    (out / 'stderr.log').write_bytes(error)
    receipt.update(completed_at_utc=datetime.now(timezone.utc).isoformat(),
                   stdout_sha256=sha(raw), stderr_sha256=sha(error),
                   source_unchanged=sha((ROOT / collector).read_bytes()) == sha(source))
    if not receipt['source_unchanged']:
        receipt.update(status='SOURCE_CHANGED', valid_payload=False)
    (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'directory': str(out), 'status': receipt['status']}))
    return 0 if receipt['valid_payload'] else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'UNAVAILABLE', 'error_type': type(exc).__name__}))
        raise SystemExit(2)
