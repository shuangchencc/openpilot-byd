#!/usr/bin/env python3
"""Read actual native pandad cereal bytes with pinned pycapnp and consumer policy."""
import json
from pathlib import Path
import sys

from test_message_policy import (LOG, FakeSubMaster, MessagePolicy, PANDA_MAX_AGE_NS,
                                 load_profile, synthetic_dict)


def main():
  native = json.loads(Path(sys.argv[1]).read_text())
  messages = [json.loads(line.removeprefix('CEREAL ')) for line in native['traces'] if line.startswith('CEREAL ')]
  assert len(messages) == 11, len(messages)
  counts = {'pandaStates': 0, 'can': 0}
  routes = []
  checks = 0
  for record in messages:
    with LOG.Event.from_bytes(bytes.fromhex(record['hex'])) as event:
      assert event.which() == record['topic']
      assert event.valid == record['expected_valid']
      now = record['now_ns']
      policy = MessagePolicy(load_profile(json.dumps(synthetic_dict(record['dos_slot']))))
      if event.which() == 'pandaStates':
        assert len(event.pandaStates) == 2
        sm = FakeSubMaster(pandaStates=event.as_builder())
        sm.recv_time['pandaStates'] = now / 1e9
        assert policy.group_ready(sm, now) == event.valid
        if event.valid:
          observed = policy.hardware(sm, now)
          assert observed.valid and observed.ignition and observed.in_car
          assert observed.voltage == 12001 and observed.current is None
          f4, h7 = event.pandaStates[record["dos_slot"]], event.pandaStates[1-record["dos_slot"]]
          assert f4.current == f4.spiErrorCount == f4.soundOutputLevel == 0
          assert (h7.current, h7.spiErrorCount, h7.soundOutputLevel) == (654, 77, 1234)
          assert not policy.group_ready(sm, event.logMonoTime + PANDA_MAX_AGE_NS)
          assert not policy.enabled(sm, now)
          checks += 6
        else:
          assert not policy.hardware(sm, now).valid
          checks += 1
        checks += 2
      else:
        assert policy.accept_can(event, now)
        assert len(event.can) == 1 and event.can[0].dat == b'\x01\x02\x03'
        routes.append(event.can[0].src)
        checks += 2
      counts[event.which()] += 1
      checks += 2
  assert counts == {'pandaStates': 5, 'can': 6}
  assert routes == [0, 1, 2, 4, 5, 6]
  print(json.dumps({'status': 'PASS', 'native_cereal_messages': len(messages), 'topics': counts,
                    'cross_language_checks': checks, 'routes': routes, 'assertions_enabled': __debug__,
                    'actual_consumer_policy': True, 'device_access': False, 'real_can_tx': False}))


if __name__ == '__main__':
  main()
