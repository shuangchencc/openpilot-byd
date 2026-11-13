import unittest

from opendbc.car.can_definitions import CanData
from opendbc.car.car_helpers import can_fingerprint
from opendbc.car.byd.fingerprints import FINGERPRINTS
from opendbc.car.byd.values import CAR

SONG_PLUS = CAR.BYD_SONG_PLUS_DMI_23

# What the car's own tester puts on bus 0 during the first ~100 s after power-up
# (measured 2026-11-13, routes 00000011 and 00000012), and the one event-driven extra.
POWER_UP_SCAN = (0x701, 0x704, 0x70C, 0x760, 0x761, 0x770, 0x771, 0x7C2, 0x7D4, 0x7F2)
EVENT_DRIVEN = (0x431,)


def fingerprint_from(addresses: dict[int, int]) -> str | None:
  can = [CanData(address=a, dat=b'\x00' * n, src=0) for a, n in addresses.items()]
  packets = iter([can])
  car, _ = can_fingerprint(lambda **kwargs: [next(packets, [])])
  return car


class TestBydSongPlusFingerprint(unittest.TestCase):
  def setUp(self):
    self.table = FINGERPRINTS[SONG_PLUS][0]
    self.normal = {a: n for a, n in self.table.items() if a not in POWER_UP_SCAN + EVENT_DRIVEN}

  def test_recognized_with_the_car_already_awake(self):
    assert fingerprint_from(self.normal) == SONG_PLUS

  def test_recognized_while_the_car_runs_its_power_up_scan(self):
    # the two real boots that came out unrecognized before these addresses were added
    for seen in ((0x761, 0x7C2), (0x761,)):
      traffic = self.normal | {a: 8 for a in seen}
      assert fingerprint_from(traffic) == SONG_PLUS, [hex(a) for a in seen]

  def test_recognized_with_every_scan_address_present(self):
    assert fingerprint_from(self.normal | {a: 8 for a in POWER_UP_SCAN + EVENT_DRIVEN}) == SONG_PLUS

  def test_an_address_outside_the_table_still_eliminates(self):
    # documents why the launcher pins FINGERPRINT: the table is a union of what was seen,
    # and one unseen address is enough to lose the car
    assert 0x7A0 not in self.table
    assert fingerprint_from(self.normal | {0x7A0: 8}) is None

  def test_table_has_no_extended_ids(self):
    assert all(a < 0x800 for a in self.table)


if __name__ == "__main__":
  unittest.main()
