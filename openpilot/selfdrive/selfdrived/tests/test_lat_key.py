"""Song PLUS lateral key (lat_key.py) between the real CarEvents and the real StateMachine.

Steps the way selfdrived.update_events / step do: car events, the brake rule, the key, the state
machine. Plain unittest, nothing touches hardware.
"""
import unittest

from openpilot.cereal import log
from opendbc.car.structs import car
from opendbc.car.byd.values import CAR
from opendbc.car.car_helpers import interfaces
from openpilot.selfdrive.car.car_events import CarEvents
from openpilot.selfdrive.selfdrived.events import Events
from openpilot.selfdrive.selfdrived.lat_key import LatKey, lat_key_enabled, songplus_lat_key, BYD_SAFETY_LAT_KEY, \
                                                  OVERRIDE_TORQUE, OVERRIDE_TORQUE_BLINKER, OVERRIDE_FRAMES
from openpilot.selfdrive.selfdrived.state import StateMachine

EventName = log.OnroadEvent.EventName
ButtonType = car.CarState.ButtonEvent.Type
GearShifter = car.CarState.GearShifter
SONG_PLUS = CAR.BYD_SONG_PLUS_DMI_23


def song_plus_CP(lat_key=True):
  CP = interfaces[SONG_PLUS].get_non_essential_params(SONG_PLUS)
  if lat_key:
    CP.safetyConfigs[0].safetyParam |= BYD_SAFETY_LAT_KEY
  return CP


class Drive:
  def __init__(self, CP=None):
    self.CP = CP if CP is not None else song_plus_CP()
    self.car_events = CarEvents(self.CP)
    self.lat_key = LatKey(self.CP)
    self.sm = StateMachine()
    self.enabled = False
    self.CS_prev = self.cs()
    self.acc_enabled, self.acc_available, self.brake, self.gear = False, False, False, GearShifter.drive

  def cs(self, press=False, release=False):
    CS = car.CarState.new_message()
    CS.canValid = True
    CS.vEgo = 0. if getattr(self, "stopped", False) else 15.
    CS.standstill = getattr(self, "stopped", False)
    CS.gearShifter = getattr(self, "gear", GearShifter.drive)
    CS.brakePressed = getattr(self, "brake", False)
    CS.cruiseState.enabled = getattr(self, "acc_enabled", False)
    CS.steeringTorque = getattr(self, "torque", 0.)
    CS.leftBlinker = getattr(self, "blinker", False)
    CS.cruiseState.available = getattr(self, "acc_available", False) or CS.cruiseState.enabled
    if press or release:
      CS.buttonEvents = [car.CarState.ButtonEvent(type=ButtonType.lkas, pressed=press)]
    return CS

  def step(self, press=False, n=1, release=False):
    for _ in range(n):
      CS = self.cs(press, release)
      events = Events()
      events.add_from_msg(self.car_events.update(CS, self.CS_prev, car.CarControl.new_message()).to_msg())
      if CS.brakePressed and (not self.CS_prev.brakePressed or not CS.standstill):   # selfdrived's brake rule
        events.add(EventName.pedalPressed)
      self.lat_key.update(events, CS, self.enabled)
      self.enabled, _ = self.sm.update(events)
      self.lat_key.after_state_machine(self.enabled)
      self.CS_prev = CS
      press = release = False
    return self.enabled

  def press(self):
    self.step(press=True)
    return self.step(n=10)


class TestLatKey(unittest.TestCase):
  def test_press_engages_with_the_acc_off_and_it_stays(self):
    for available in (False, True):     # ACC unavailable, or in standby
      d = Drive()
      d.acc_available = available
      assert not d.step(n=10)
      assert d.press(), available
      assert d.step(n=500), available
      assert d.lat_key.lat_only

  def test_second_press_ends_it(self):
    d = Drive()
    assert d.press()
    assert not d.press()
    assert not d.lat_key.lat_only
    assert d.press()

  def test_the_release_is_nothing(self):
    d = Drive()
    assert not d.step(release=True)
    assert d.press()
    assert d.step(release=True)

  def test_brake_does_not_end_it(self):
    for stopped in (False, True):
      d = Drive()
      d.press()
      d.stopped = stopped
      for _ in range(3):
        d.brake = True
        assert d.step(n=50), stopped
        d.brake = False
        assert d.step(n=50), stopped
      assert d.lat_key.lat_only

  def test_press_with_the_brake_held(self):
    d = Drive()
    d.brake = True
    d.step()
    assert d.press()
    # stopped with the brake held it waits for the brake, as any engagement does, and then steers
    d = Drive()
    d.stopped = True
    d.brake = True
    d.step()
    assert d.press()
    assert d.sm.state == log.SelfdriveState.OpenpilotState.preEnabled
    d.brake = False
    d.step()
    assert d.sm.state == log.SelfdriveState.OpenpilotState.enabled

  def test_brake_with_the_acc_engaged_after_the_key(self):
    # the brake ends the stock ACC; steering stays
    d = Drive()
    d.press()
    d.acc_enabled = True
    d.step(n=10)
    d.brake = True
    d.acc_enabled = False
    assert d.step(n=20)

  def test_usual_no_entry_still_applies(self):
    d = Drive()
    d.gear = GearShifter.park
    assert not d.step(press=True)
    assert not d.lat_key.lat_only            # refused: not even for the step it was refused in
    assert not d.step(n=10)
    d.gear = GearShifter.drive
    assert d.press()

  def test_the_acc_coming_and_going_does_not_end_it(self):
    d = Drive()
    d.press()
    for enabled in (True, False, True, False):
      d.acc_enabled = enabled
      assert d.step(n=20)
    assert d.lat_key.lat_only

  def test_press_while_engaged_by_the_acc_ends_it_and_the_next_one_engages(self):
    d = Drive()
    d.step()
    d.acc_enabled = True
    assert d.step()
    assert not d.press()          # off; the ACC goes on by itself
    assert d.press()              # on again, now by the key
    assert d.lat_key.lat_only
    d.acc_enabled = False         # and the ACC leaving does not end it
    assert d.step(n=20)

  def test_a_firm_turn_of_the_wheel_ends_it(self):
    for sign in (1, -1):
      d = Drive()
      d.press()
      d.torque = sign * (OVERRIDE_TORQUE + 0.5)
      for _ in range(OVERRIDE_FRAMES - 1):
        assert d.step()
      assert not d.step()
      d.torque = 0.
      assert not d.step(n=50)        # stays off until the key is pressed again
      assert d.press()

  def test_corrections_and_short_peaks_do_not(self):
    d = Drive()
    d.press()
    d.torque = OVERRIDE_TORQUE        # at the limit, not above
    assert d.step(n=100)
    for _ in range(20):               # peaks shorter than OVERRIDE_FRAMES
      d.torque = 45.
      assert d.step(n=OVERRIDE_FRAMES - 1)
      d.torque = 20.
      assert d.step()
    assert d.lat_key.lat_only

  def test_a_lane_change_push_needs_more(self):
    d = Drive()
    d.press()
    d.blinker = True
    d.torque = 35.
    assert d.step(n=50)
    d.torque = OVERRIDE_TORQUE_BLINKER + 0.5
    assert d.step(n=OVERRIDE_FRAMES - 1)
    assert not d.step()

  def test_an_acc_engagement_only_yields(self):
    # the firm turn rule is the key's: an ACC engagement goes on as before (the port withdraws its request)
    d = Drive()
    d.step()
    d.acc_enabled = True
    assert d.step()
    d.torque = 45.
    assert d.step(n=20)

  def test_pressing_while_turning_firmly_ends_it_right_after(self):
    d = Drive()
    d.torque = 45.
    d.step(n=10)
    assert d.step(press=True)         # engaged; the next steps end it, the driver is still turning
    assert not d.step(n=OVERRIDE_FRAMES)

  def test_without_the_panda_bit_the_key_is_nothing(self):
    d = Drive(song_plus_CP(lat_key=False))
    assert not d.lat_key.on
    assert not d.press()
    d.step()
    d.acc_enabled = True
    assert d.step()
    assert d.press()              # the key does not end an ACC engagement either

  def test_acc_without_the_key_is_as_before(self):
    d = Drive()
    d.step()
    d.acc_enabled = True
    assert d.step()
    d.acc_enabled = False
    assert not d.step()


class TestLatKeySwitch(unittest.TestCase):
  def test_launch_switch(self):
    assert songplus_lat_key({"SONGPLUS_ALLOW_CONTROL": "1", "SONGPLUS_LAT_KEY": "1"})
    for env in ({}, {"SONGPLUS_LAT_KEY": "1"}, {"SONGPLUS_ALLOW_CONTROL": "1"},
                {"SONGPLUS_ALLOW_CONTROL": "1", "SONGPLUS_LAT_KEY": "0"}):
      assert not songplus_lat_key(env), env

  def test_enabled_from_car_params(self):
    assert lat_key_enabled(song_plus_CP())
    assert not lat_key_enabled(song_plus_CP(lat_key=False))
    CP = song_plus_CP()
    CP.passive = True
    assert not lat_key_enabled(CP)
    CP = song_plus_CP()
    CP.safetyConfigs[0].safetyParam &= ~1        # not the Song Plus
    assert not lat_key_enabled(CP)
    atto = interfaces[CAR.BYD_ATTO_3].get_non_essential_params(CAR.BYD_ATTO_3)
    atto.safetyConfigs[0].safetyParam |= BYD_SAFETY_LAT_KEY
    assert not lat_key_enabled(atto)


if __name__ == "__main__":
  unittest.main()
