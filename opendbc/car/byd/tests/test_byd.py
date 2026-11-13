import unittest
from collections import defaultdict

from opendbc.car import Bus
from opendbc.car.can_definitions import CanData
from opendbc.car.car_helpers import can_fingerprint, interfaces
from opendbc.car.byd.fingerprints import FINGERPRINTS
from opendbc.car.byd.values import CAR, CarControllerParams as CCP

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


class FakeParser:
  """Stands in for CANParser: every signal reads 0 unless set."""
  def __init__(self):
    self.vl = defaultdict(lambda: defaultdict(float))
    self.can_valid = True


def car_state(platform):
  CarInterface = interfaces[platform]
  CI = CarInterface(CarInterface.get_non_essential_params(platform))
  parsers = {Bus.pt: FakeParser(), Bus.cam: FakeParser()}
  return CI.CS, parsers


class TestBydSongPlusSteeringPressed(unittest.TestCase):
  """steeringPressed on the Song Plus is "a hand is on the wheel", around a non-zero sensor zero.

  The torque levels are the ones measured in the labelled capture of 2026-11-13.
  """

  def pressed_after(self, torque, ticks=100):
    CS, parsers = car_state(SONG_PLUS)
    ret = None
    for _ in range(ticks):
      parsers[Bus.pt].vl["STEERING_TORQUE"]["DRIVER_TORQUE"] = torque
      ret = CS.update(parsers)
    return ret

  def test_hands_off_is_not_pressed(self):
    for level in (-1.33, -1.5, -1.2, -1.1):
      assert not self.pressed_after(level).steeringPressed, level

  def test_friction_residual_after_dry_steering_is_not_pressed(self):
    for level in (-2.0, -3.2, -3.4, -0.8):
      assert not self.pressed_after(level).steeringPressed, level

  def test_a_resting_hand_is_pressed(self):
    for level in (2.64, 3.41, 5.8):
      assert self.pressed_after(level).steeringPressed, level

  def test_the_same_hand_weight_is_seen_in_both_directions(self):
    weight = 4.0
    assert self.pressed_after(CCP.DRIVER_TORQUE_ZERO_NM + weight).steeringPressed
    assert self.pressed_after(CCP.DRIVER_TORQUE_ZERO_NM - weight).steeringPressed

  def test_band_edges(self):
    zero, thr = CCP.DRIVER_TORQUE_ZERO_NM, CCP.STEER_DRIVER_OVERRIDE_NM
    assert not self.pressed_after(zero + thr - 0.1).steeringPressed
    assert self.pressed_after(zero + thr + 0.1).steeringPressed
    assert not self.pressed_after(zero - thr + 0.1).steeringPressed
    assert self.pressed_after(zero - thr - 0.1).steeringPressed

  def test_needs_six_consecutive_samples(self):
    assert not self.pressed_after(5.0, ticks=5).steeringPressed
    assert self.pressed_after(5.0, ticks=6).steeringPressed

  def test_steering_torque_is_reported_raw(self):
    assert abs(self.pressed_after(3.41).steeringTorque - 3.41) < 1e-6

  def test_values_are_the_measured_ones(self):
    assert CCP.DRIVER_TORQUE_ZERO_NM == -1.1
    assert CCP.STEER_DRIVER_OVERRIDE_NM == 3.0

  def test_disengage_level_stays_out_of_reach(self):
    # byd.h has no matching check, so this must not become reachable by accident
    for level in (-41.7, 24.0, 41.7):
      assert not self.pressed_after(level).steeringDisengage, level

  def test_atto3_is_untouched(self):
    CS, parsers = car_state(CAR.BYD_ATTO_3)
    ret = None
    for torque, expected in ((CCP.STEER_DRIVER_OVERRIDE, False), (CCP.STEER_DRIVER_OVERRIDE + 1, True)):
      for _ in range(20):
        parsers[Bus.pt].vl["STEER_MODULE_2"]["DRIVER_EPS_TORQUE"] = torque
        parsers[Bus.pt].vl["STEERING_TORQUE"]["DRIVER_TORQUE"] = 20.0   # must be ignored on this platform
        ret = CS.update(parsers)
      assert ret.steeringPressed == expected, torque


if __name__ == "__main__":
  unittest.main()
