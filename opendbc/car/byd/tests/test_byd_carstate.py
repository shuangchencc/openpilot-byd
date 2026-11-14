import unittest
from collections import defaultdict

from opendbc.car import Bus, structs
from opendbc.car.car_helpers import interfaces
from opendbc.car.byd.values import CAR

SONG_PLUS = CAR.BYD_SONG_PLUS_DMI_23
ButtonType = structs.CarState.ButtonEvent.Type

# ACC_HUD_ADAS.ACC_STATE_CODE, as measured on the Song Plus
UNAVAILABLE, STANDBY, ACTIVE, EXITING, GAS_OVERRIDE, STANDSTILL_HOLD = 1, 2, 3, 4, 5, 7


class FakeParser:
  """Stands in for CANParser: every signal reads 0 unless set."""
  def __init__(self):
    self.vl = defaultdict(lambda: defaultdict(float))
    self.can_valid = True


class Car:
  def __init__(self, platform=SONG_PLUS):
    CarInterface = interfaces[platform]
    self.CS = CarInterface(CarInterface.get_non_essential_params(platform)).CS
    self.pt, self.cam = FakeParser(), FakeParser()

  def acc(self, distance_bars, state):
    self.cam.vl["ACC_HUD_ADAS"]["ACC_ENGAGED_STATE"] = distance_bars
    self.cam.vl["ACC_HUD_ADAS"]["ACC_STATE_CODE"] = state

  def key(self, pressed):
    self.pt.vl["PCM_BUTTONS"]["ACC_ON_BTN"] = int(pressed)

  def step(self):
    return self.CS.update({Bus.pt: self.pt, Bus.cam: self.cam})


class TestSongPlusCruiseEnabled(unittest.TestCase):
  """cruiseState.enabled follows the stock ACC's own state, not the distance display."""

  def test_states(self):
    car = Car()
    for bars, state, expected in (
      (0, STANDBY, False), (0, UNAVAILABLE, False),
      (1, ACTIVE, True), (2, ACTIVE, True),
      (1, GAS_OVERRIDE, True), (2, GAS_OVERRIDE, True),    # pressing the gas does not end the ACC
      (1, STANDSTILL_HOLD, True),                          # stopped and held by the ACC
      (1, 6, False), (1, 8, False), (1, 15, False),        # codes never seen read as not engaged
      (1, EXITING, False), (2, EXITING, False),            # on its way out: distance still shown
      (1, UNAVAILABLE, False), (1, STANDBY, False),
      (0, ACTIVE, False),                                  # never seen; needs both to count
    ):
      car.acc(bars, state)
      assert car.step().cruiseState.enabled == expected, (bars, state)

  def test_the_exit_recorded_in_the_car(self):
    # 2026-11-14, 06:04:45: on, braking; the ACC key four times; EXITING for 2.5 s; brake
    car = Car()
    seq = [(0, STANDBY)] * 5 + [(1, ACTIVE)] * 400 + [(1, EXITING)] * 250 + [(0, UNAVAILABLE)] * 10 + [(0, STANDBY)] * 5
    enabled = []
    for bars, state in seq:
      car.acc(bars, state)
      enabled.append(car.step().cruiseState.enabled)
    assert enabled == [False] * 5 + [True] * 400 + [False] * 265

  def test_available_while_engaged_and_overridden(self):
    car = Car()
    car.acc(1, GAS_OVERRIDE)
    ret = car.step()
    assert ret.cruiseState.enabled and ret.cruiseState.available

  def test_atto3_still_reads_acc_state(self):
    car = Car(CAR.BYD_ATTO_3)
    for acc_state, expected in ((0, False), (2, False), (3, True), (5, True)):
      car.cam.vl["ACC_HUD_ADAS"]["ACC_STATE"] = acc_state
      car.cam.vl["ACC_HUD_ADAS"]["ACC_STATE_CODE"] = EXITING      # must be ignored there
      assert car.step().cruiseState.enabled == expected, acc_state


class TestSongPlusAccKey(unittest.TestCase):
  """The ACC key toggles the stock ACC. Pressed with the ACC engaged, it is a cancel for openpilot."""

  def events(self, car):
    return [(e.type, e.pressed) for e in car.step().buttonEvents]

  def test_press_with_acc_engaged_is_a_cancel_in_the_same_step(self):
    car = Car()
    car.acc(1, ACTIVE)
    assert self.events(car) == []
    car.key(True)
    assert self.events(car) == [(ButtonType.cancel, True)]
    assert self.events(car) == []                       # held: no repeat
    car.key(False)
    assert self.events(car) == [(ButtonType.cancel, False)]
    assert self.events(car) == []

  def test_press_counts_in_every_engaged_state(self):
    for state in (ACTIVE, GAS_OVERRIDE, STANDSTILL_HOLD):
      car = Car()
      car.acc(1, state)
      car.step()
      car.key(True)
      assert self.events(car) == [(ButtonType.cancel, True)], state

  def test_press_while_not_engaged_is_not_published(self):
    # distance bars set in every case: the bars alone must not make a press a cancel
    for state in (UNAVAILABLE, STANDBY, EXITING, 6, 15):
      car = Car()
      car.acc(1, state)
      car.step()
      car.key(True)
      assert self.events(car) == [], state
      car.key(False)
      assert self.events(car) == [], state

  def test_press_from_standby_turns_the_acc_on_and_is_not_a_cancel(self):
    car = Car()
    car.acc(0, STANDBY)
    car.step()
    car.key(True)
    assert self.events(car) == []
    # the ACC comes on about 0.2 s later while the key may still be held ...
    car.acc(1, ACTIVE)
    assert self.events(car) == []
    assert car.step().cruiseState.enabled
    # ... and its release must not read as a cancel, or openpilot would drop out again
    car.key(False)
    assert self.events(car) == []

  def test_long_press_from_standby_never_publishes_anything(self):
    # 400 ms press at 100 Hz; the ACC answers 0.2 s into it. The distance bars are tried
    # both ways: 0 in standby as recorded, and already set in standby, which was never seen.
    for bars_in_standby in (0, 1, 2):
      car = Car()
      car.acc(bars_in_standby, STANDBY)
      events = self.events(car)
      enabled = []
      for i in range(60):
        car.key(i < 40)
        car.acc(*((bars_in_standby, STANDBY) if i < 20 else (1, ACTIVE)))
        ret = car.step()
        events += [(e.type, e.pressed) for e in ret.buttonEvents]
        enabled.append(ret.cruiseState.enabled)
      assert events == [], bars_in_standby
      assert enabled == [False] * 20 + [True] * 40, bars_in_standby

  def test_second_press_after_turning_on_is_a_cancel(self):
    car = Car()
    car.acc(0, STANDBY)
    car.step()
    car.key(True)
    car.step()
    car.key(False)
    car.acc(1, ACTIVE)
    car.step()
    car.key(True)
    assert self.events(car) == [(ButtonType.cancel, True)]

  def test_the_other_keys_are_not_a_cancel(self):
    car = Car()
    car.acc(1, ACTIVE)
    car.step()
    for name in ("LKAS_ON_BTN", "SET_BTN", "RES_BTN", "INC_DISTANCE_BTN", "DEC_DISTANCE_BTN"):
      car.pt.vl["PCM_BUTTONS"][name] = 1
      assert self.events(car) == [], name
      car.pt.vl["PCM_BUTTONS"][name] = 0
      assert self.events(car) == [], name

  def test_atto3_publishes_no_button(self):
    car = Car(CAR.BYD_ATTO_3)
    car.cam.vl["ACC_HUD_ADAS"]["ACC_STATE"] = 3
    car.step()
    car.key(True)
    assert self.events(car) == []


if __name__ == "__main__":
  unittest.main()
