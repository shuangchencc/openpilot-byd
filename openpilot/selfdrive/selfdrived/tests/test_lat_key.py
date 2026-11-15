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
from openpilot.selfdrive.selfdrived.lat_key import LatKey, lat_key_enabled, songplus_lat_key, BYD_SAFETY_LAT_KEY
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

  def test_the_release_is_nothing(self):
    d = Drive()
    assert not d.step(release=True)
    assert d.press()
    assert d.step(release=True)

  def test_second_press_ends_it(self):
    d = Drive()
    assert d.press()
    assert not d.press()
    assert d.press()

  def test_brake_ends_it(self):
    d = Drive()
    d.press()
    d.brake = True
    assert not d.step()
    d.brake = False
    assert not d.step(n=100)
    assert not d.lat_key.lat_only

  def test_no_entry_with_the_brake_held(self):
    d = Drive()
    d.brake = True
    d.step()
    assert not d.press()
    assert not d.lat_key.lat_only

  def test_no_entry_with_the_brake_held_at_standstill(self):
    # stopped with the brake held openpilot would otherwise pre-enable, which panda does not follow
    d = Drive()
    d.stopped = True
    d.brake = True
    d.step()
    assert not d.press()
    assert d.sm.state == log.SelfdriveState.OpenpilotState.disabled

  def test_usual_no_entry_still_applies(self):
    d = Drive()
    d.gear = GearShifter.park
    assert not d.step(press=True)
    assert not d.lat_key.lat_only            # refused: not even for the step it was refused in
    assert not d.step(n=10)
    d.gear = GearShifter.drive
    assert d.press()

  def test_acc_engaging_makes_it_an_ordinary_engagement(self):
    d = Drive()
    d.press()
    d.acc_enabled = True
    assert d.step(n=10)
    assert not d.lat_key.lat_only
    d.acc_enabled = False
    d.acc_available = True
    assert not d.step()

  def test_press_while_the_acc_is_engaged_does_nothing(self):
    d = Drive()
    d.step()
    d.acc_enabled = True
    assert d.step()
    assert d.press()
    assert not d.lat_key.lat_only
    d.acc_enabled = False
    assert not d.step()
    # nor does it engage openpilot while the ACC is engaged without it
    d = Drive()
    d.acc_enabled = True
    d.step()
    d.sm.state = log.SelfdriveState.OpenpilotState.disabled
    d.enabled = False
    assert not d.press()

  def test_without_the_panda_bit_the_key_is_nothing(self):
    d = Drive(song_plus_CP(lat_key=False))
    assert not d.lat_key.on
    assert not d.press()

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
