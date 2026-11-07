#!/usr/bin/env python3
"""Check a bounded host-evidence packet; never discover tools or activate agents."""
import argparse
import json
from pathlib import Path
import sys

MAX_BYTES = 16384
REVIEW = ('native_binding', 'loaded_permissions', 'scratch_access', 'direct_assignment')
POLL = ('owned_handle_inspection', 'timer_completion', 'exact_root_status',
        'exact_root_push', 'durable_continuation', 'single_controller')


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return value


def identifier(value, name):
    if not isinstance(value, str) or not value or len(value.encode()) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError(f'{name} must be a bounded nonempty identifier')
    return value


def check(packet):
    if not isinstance(packet, dict) or packet.get('schema_version') != 1:
        raise ValueError('Expected schema_version 1 object')
    root = identifier(packet.get('root'), 'root')
    helper = identifier(packet.get('helper'), 'helper')
    parent = identifier(packet.get('observed_parent'), 'observed_parent')
    if parent != root or helper == root:
        raise ValueError('Helper must be a distinct child of this exact root')
    context = packet['context']
    used = integer(context['observed_bytes'], 'observed_bytes')
    limit = integer(context['limit_bytes'], 'limit_bytes', 1)
    batches = integer(context['batches'], 'batches')
    batch_limit = integer(context['batch_limit'], 'batch_limit', 1)
    allowance = packet['allowance']
    total = integer(allowance['total'], 'total')
    consumed = integer(allowance['consumed'], 'consumed')
    if consumed > total:
        raise ValueError('Consumed allowance exceeds total; reconcile without resetting')
    if type(packet['effects_reconciled']) is not bool:
        raise ValueError('effects_reconciled must be boolean')
    capabilities = packet['capabilities']
    if not isinstance(capabilities, dict) or set(capabilities) - set(REVIEW + POLL):
        raise ValueError('Unknown capability')
    verified = set()
    for name, item in capabilities.items():
        if not isinstance(item, dict) or type(item.get('available')) is not bool:
            raise ValueError(f'{name}: available must be boolean')
        if item['available']:
            if item.get('verified_by') not in ('parent', 'helper'):
                raise ValueError(f'{name}: identify parent or helper verifier')
            identifier(item.get('evidence'), f'{name}.evidence')
            verified.add(name)
    blockers = []
    if used >= limit or batches >= batch_limit:
        blockers.append('context_limit')
    if not packet['effects_reconciled']:
        blockers.append('unreconciled_effects')
    review_missing = sorted(set(REVIEW) - verified)
    poll_missing = sorted(set(POLL) - verified)
    review_ready = not blockers and not review_missing
    poll_ready = review_ready and not poll_missing and consumed < total
    if consumed == total:
        poll_missing.append('return_allowance_exhausted')
    return {'status': 'READY' if poll_ready else 'REVIEW_ONLY' if review_ready else 'NOT_READY',
            'review_ready': review_ready, 'poll_ready': poll_ready,
            'blockers': blockers, 'review_missing': review_missing, 'poll_missing': poll_missing,
            'returns_remaining': total - consumed,
            'context_measurement': 'supplied byte proxy; not total model context',
            'live_delivery_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with args.evidence.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('Evidence exceeds 16 KiB; supply selected facts, not tool catalogs')
        result = check(json.loads(data))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'INVALID', 'error': str(error)[:400]}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0 if result['poll_ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
