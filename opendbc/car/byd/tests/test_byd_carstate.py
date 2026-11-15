import random
import unittest
from collections import defaultdict
from unittest import mock

import numpy as np

from opendbc.can.packer import CANPacker
from opendbc.car import Bus, structs
from opendbc.car.byd import bydcan
from opendbc.car.can_definitions import CanData
from opendbc.car.car_helpers import interfaces
from opendbc.car.byd.carcontroller import get_safety_CP
from opendbc.car.byd.values import CAR, DBC, CarControllerParams
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.lateral import get_max_angle_delta_vm, get_max_angle_vm
from opendbc.car.vehicle_model import VehicleModel

SONG_PLUS = CAR.BYD_SONG_PLUS_DMI_23
ButtonType = structs.CarState.ButtonEvent.Type

# ACC_HUD_ADAS.ACC_STATE_CODE, as measured on the Song Plus
UNAVAILABLE, STANDBY, ACTIVE, EXITING, GAS_OVERRIDE, STANDSTILL_HOLD = 1, 2, 3, 4, 5, 7


class FakeParser:
  """Stands in for CANParser: every signal reads 0 unless set."""
  def __init__(self):
    self.vl = defaultdict(lambda: defaultdict(float))
    self.vl_all = defaultdict(lambda: defaultdict(list))
    self.can_valid = True


class Car:
  def __init__(self, platform=SONG_PLUS):
    CarInterface = interfaces[platform]
    self.CI = CarInterface(CarInterface.get_non_essential_params(platform))
    self.CS = self.CI.CS
    self.pt, self.cam, self.rejected = FakeParser(), FakeParser(), FakeParser()
    self.frame = 0
    self.sent = []

  def eps(self, state):
    self.pt.vl["STEERING_TORQUE"]["LKA_STATE"] = state

  def driver_torque(self, torque):
    self.pt.vl["STEERING_TORQUE"]["DRIVER_TORQUE"] = torque

  def wheel(self, angle_deg):
    self.pt.vl["STEER_MODULE_2"]["STEER_ANGLE_2"] = angle_deg

  def speed(self, kph):
    for wheel in ("WHEELSPEED_FL", "WHEELSPEED_FR", "WHEELSPEED_BL", "WHEELSPEED_BR", "WHEELSPEED_CLEAN"):   # the last is the Atto 3's
      self.pt.vl["WHEELSPEED_CLEAN"][wheel] = kph

  def drive(self, lat_active, steps=1, enabled=None, target_deg=0.0, lane_change=None):
    """CarState then CarController, as the interface runs them, `steps` times at 100 Hz.
    `enabled` is openpilot being engaged; unless given, it is engaged exactly while it asks to steer.
    `lane_change` ("left" / "right") is openpilot showing a lane change, as controlsd does in
    CC.leftBlinker / CC.rightBlinker whenever its lane change state is not off.
    Every frame the controller builds is appended to self.sent as (address, data)."""
    ret = None
    for _ in range(steps):
      ret = self.step()
      self.CS.out = ret
      CC = structs.CarControl()
      CC.latActive = lat_active
      CC.enabled = lat_active if enabled is None else enabled
      CC.actuators.steeringAngleDeg = target_deg
      CC.leftBlinker = lane_change == "left"
      CC.rightBlinker = lane_change == "right"
      _, can_sends = self.CI.CC.update(CC.as_reader(), self.CS, self.frame * 10_000_000)
      self.sent += [(msg[0], bytes(msg[1])) for msg in can_sends]
      self.frame += 1
    return ret

  def steering_frames(self, since=0):
    """(STEER_REQ, STEER_ANGLE in deg, ANGLE_RATE_LIMIT_UPPER) of every 0x1E2 built since self.sent[since]."""
    out = []
    for addr, dat in self.sent[since:]:
      if addr == 0x1E2:
        angle = int.from_bytes(dat[3:5], "little", signed=True) * 0.1
        limit = int.from_bytes(dat[:2], "little") & 0x3FF
        out.append(((dat[2] >> 5) & 1, angle, limit))
    return out

  def acc(self, distance_bars, state):
    # the follow-distance setting is bits 10-12 (SET_DISTANCE); the DBC's ACC_ENGAGED_STATE
    # is bits 10-11 of the same frame, so it reads the setting's two low bits
    self.cam.vl["ACC_HUD_ADAS"]["SET_DISTANCE"] = distance_bars
    self.cam.vl["ACC_HUD_ADAS"]["ACC_ENGAGED_STATE"] = distance_bars & 3
    self.cam.vl["ACC_HUD_ADAS"]["ACC_STATE_CODE"] = state

  def key(self, pressed):
    self.pt.vl["PCM_BUTTONS"]["ACC_ON_BTN"] = int(pressed)

  def reject(self, *steer_reqs, counters=None):
    """Steering frames panda refused, as pandad hands them back for the next update: their STEER_REQ
    bits, and their counters (by default ones no frame of ours has)."""
    self.rejected.vl_all["STEERING_MODULE_ADAS"]["STEER_REQ"] = list(steer_reqs)
    self.rejected.vl_all["STEERING_MODULE_ADAS"]["COUNTER"] = list(counters) if counters is not None else [99] * len(steer_reqs)

  def step(self):
    ret = self.CS.update({Bus.pt: self.pt, Bus.cam: self.cam, Bus.loopback: self.rejected})
    for signal in ("STEER_REQ", "COUNTER"):                          # CANParser starts every update afresh
      self.rejected.vl_all["STEERING_MODULE_ADAS"][signal] = []
    return ret


def safety_limits(kph, platform=SONG_PLUS):
  """(the angle the lateral acceleration limit allows, the change per steering frame the limits allow) at this
  speedometer speed, with the geometry panda checks and the controller limits with (carcontroller.get_safety_CP)."""
  CP = interfaces[platform].get_non_essential_params(platform)
  v = max(kph * CV.KPH_TO_MS * CP.wheelSpeedFactor, 1.0)
  VM = VehicleModel(get_safety_CP(platform))
  return (get_max_angle_vm(v, VM, CarControllerParams),
          min(get_max_angle_delta_vm(v, VM, CarControllerParams), CarControllerParams.ANGLE_LIMITS.MAX_ANGLE_RATE))


class TestSongPlusSafetyGeometry(unittest.TestCase):
  """The geometry panda checks Song PLUS angles with turns less than the car at every speed it was measured at,
  so the angle the limit allows never stands for more than the limit's lateral acceleration (values.py)."""

  # apparent steering ratio (wheel angle over road wheel angle) by road speed, from the gyroscope on 2026-11-15
  MEASURED = {20: 17.84, 40: 20.21, 60: 25.45, 80: 27.54}

  def test_it_turns_less_than_the_car(self):
    VM = VehicleModel(get_safety_CP(SONG_PLUS))
    for kph, measured in self.MEASURED.items():
      v = kph * CV.KPH_TO_MS
      modelled = VM.sR / VM.curvature_factor(v) / VM.l          # the wheel angle per unit curvature, over the wheelbase
      assert modelled < measured, (kph, modelled, measured)
      assert modelled > 0.75 * measured, (kph, modelled, measured)   # and not needlessly far off

  def test_the_atto3_keeps_its_own(self):
    from opendbc.car.byd.interface import CarInterface
    assert get_safety_CP(CAR.BYD_ATTO_3).steerRatio == CarInterface.get_non_essential_params(CAR.BYD_ATTO_3).steerRatio


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
      (3, ACTIVE, True), (4, ACTIVE, True),                # 4 is 0b100: bits 10-11 alone read 0
      (4, GAS_OVERRIDE, True), (4, STANDSTILL_HOLD, True),
      (5, ACTIVE, True), (6, ACTIVE, True), (7, ACTIVE, True),
      (4, EXITING, False), (4, STANDBY, False), (4, UNAVAILABLE, False),
    ):
      car.acc(bars, state)
      assert car.step().cruiseState.enabled == expected, (bars, state)

  def test_the_distance_key_pressed_while_engaged(self):
    # 2026-11-15, route 00000033, 12:55:11-12:55:20: controlling at setting 2, the distance
    # key twice (3 for 1.15 s, then 4), the ACC still controlling until the driver turned it off
    car = Car()
    seq = [(2, ACTIVE)] * 100 + [(3, ACTIVE)] * 115 + [(4, ACTIVE)] * 859 + [(0, UNAVAILABLE)] * 10
    enabled = []
    for bars, state in seq:
      car.acc(bars, state)
      enabled.append(car.step().cruiseState.enabled)
    assert enabled == [True] * 1074 + [False] * 10

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


class TestSongPlusCustomKey(unittest.TestCase):
  """The custom key's short press is published as the lkas button (BydFlags.CUSTOM_KEY)."""

  PRESS = {"KEY_PAGE": 1, "KEY_CODE": 0x25, "KEY_BYTE_2": 0x80, "KEY_BYTES_3_7": 0}

  def update(self, car, frames=()):
    """one CarState update with these STEERING_WHEEL_KEYS frames (dicts of signal values) in it"""
    for sig in self.PRESS:
      car.pt.vl_all["STEERING_WHEEL_KEYS"][sig] = [f.get(sig, 0) for f in frames]
    ret = car.step()
    for sig in self.PRESS:
      car.pt.vl_all["STEERING_WHEEL_KEYS"][sig] = []
    return [(e.type, e.pressed) for e in ret.buttonEvents]

  def press(self, car, **kw):
    return self.update(car, [{**self.PRESS, **kw}])

  def test_short_press(self):
    car = Car()
    assert self.update(car) == []
    assert self.press(car) == [(ButtonType.lkas, True)]
    for _ in range(9):
      assert self.update(car) == []                     # 100 ms to the next frame
    assert self.press(car) == []                        # held: no repeat
    assert self.update(car, [{**self.PRESS, "KEY_CODE": 0}]) == [(ButtonType.lkas, False)]
    assert self.update(car) == []

  def test_press_in_any_acc_state(self):
    # what the press does is openpilot's to decide; the car only reports it
    for state in (UNAVAILABLE, STANDBY, ACTIVE, EXITING):
      car = Car()
      car.acc(1 if state == ACTIVE else 0, state)
      car.step()
      assert self.press(car) == [(ButtonType.lkas, True)], state

  def test_only_the_exact_frame(self):
    for kw in ({"KEY_CODE": 0x26}, {"KEY_CODE": 0x15}, {"KEY_CODE": 0x0C}, {"KEY_PAGE": 2}, {"KEY_BYTE_2": 0},
               {"KEY_BYTES_3_7": 1}):
      car = Car()
      assert self.press(car, **kw) == [], kw

  def test_holding_into_the_long_press_is_released_when_the_code_changes(self):
    car = Car()
    assert self.press(car) == [(ButtonType.lkas, True)]
    assert self.press(car, KEY_CODE=0x26) == [(ButtonType.lkas, False)]

  def test_lost_release_frame(self):
    car = Car()
    self.press(car)
    events = []
    for _ in range(40):
      events += self.update(car)
    assert events == [(ButtonType.lkas, False)]
    # released after 300 ms without a frame, so a new press is one again
    assert self.press(car) == [(ButtonType.lkas, True)]

  def test_last_frame_of_an_update_decides(self):
    car = Car()
    assert self.update(car, [self.PRESS, {**self.PRESS, "KEY_CODE": 0}]) == []
    assert self.update(car, [{**self.PRESS, "KEY_CODE": 0}, self.PRESS]) == [(ButtonType.lkas, True)]

  def test_with_the_acc_key_in_the_same_update(self):
    car = Car()
    car.acc(1, ACTIVE)
    car.step()
    car.key(True)
    assert sorted(self.press(car)) == sorted([(ButtonType.cancel, True), (ButtonType.lkas, True)])

  def test_atto3_reads_no_key(self):
    car = Car(CAR.BYD_ATTO_3)
    assert self.press(car) == []


class TestSongPlusCustomKeyParser(unittest.TestCase):
  def test_key_frames_never_make_can_invalid(self):
    # the real parser: an event frame that has not been seen is not a missing message
    CP = interfaces[SONG_PLUS].get_non_essential_params(SONG_PLUS)
    pt = interfaces[SONG_PLUS].CarState.get_can_parsers(CP)[Bus.pt]
    state = pt.message_states[0x4A8]
    assert state.ignore_alive

  def test_atto3_parser_has_no_key(self):
    CP = interfaces[CAR.BYD_ATTO_3].get_non_essential_params(CAR.BYD_ATTO_3)
    pt = interfaces[CAR.BYD_ATTO_3].CarState.get_can_parsers(CP)[Bus.pt]
    assert 0x4A8 not in pt.message_states

# STEERING_TORQUE.LKA_STATE
EPS_OFF, EPS_READY, EPS_ACTIVE, EPS_ABORTED = 0, 1, 2, 3


class TestSongPlusSteeringFault(unittest.TestCase):
  """The EPS's own state is this car's only steering fault indication."""

  def test_aborted_is_a_permanent_fault_at_once(self):
    car = Car()
    for state, expected in ((EPS_OFF, False), (EPS_READY, False), (EPS_ACTIVE, False), (EPS_ABORTED, True), (EPS_READY, False)):
      car.eps(state)
      assert car.step().steerFaultPermanent == expected, state

  def test_normal_engagement_is_not_a_fault(self):
    # the EPS answers a request within 46 ms; when the request is dropped it took up to
    # 258 ms to report ready again
    car = Car()
    car.eps(EPS_READY)
    assert not car.drive(False, 100).steerFaultTemporary
    faults = [car.drive(True).steerFaultTemporary for _ in range(5)]      # asked, not answered yet
    car.eps(EPS_ACTIVE)
    faults += [car.drive(True).steerFaultTemporary for _ in range(500)]
    faults += [car.drive(False, enabled=True).steerFaultTemporary for _ in range(26)]   # still active after we stopped asking
    car.eps(EPS_READY)
    faults += [car.drive(False, enabled=True).steerFaultTemporary for _ in range(100)]
    assert not any(faults)

  def test_slow_answer_just_inside_the_limit_is_not_a_fault(self):
    car = Car()
    car.eps(EPS_READY)
    faults = [car.drive(True).steerFaultTemporary for _ in range(50)]
    car.eps(EPS_ACTIVE)
    faults += [car.drive(True).steerFaultTemporary for _ in range(300)]
    assert not any(faults)

  def test_dropping_and_repeating_the_request_is_not_a_fault(self):
    # openpilot stays engaged and stops asking for a while, e.g. while the driver steers
    for answer_after in (0, 2, 4, 6, 30, 50):
      car = Car()
      car.eps(EPS_READY)
      faults = []
      for _ in range(12):
        faults += [car.drive(True).steerFaultTemporary for _ in range(answer_after)]
        car.eps(EPS_ACTIVE)
        faults += [car.drive(True).steerFaultTemporary for _ in range(100)]
        faults += [car.drive(False, enabled=True).steerFaultTemporary for _ in range(26)]
        car.eps(EPS_READY)
        faults += [car.drive(False, enabled=True).steerFaultTemporary for _ in range(30)]
      assert not any(faults), answer_after

  def test_request_ignored_becomes_a_fault_after_half_a_second(self):
    for state in (EPS_READY, EPS_OFF):
      car = Car()
      car.eps(state)
      faults = [car.drive(True).steerFaultTemporary for _ in range(70)]
      first = faults.index(True)
      assert 50 <= first <= 53, (state, first)        # 0.5 s, plus the step it takes to reach CarState
      assert all(faults[first:])

  def test_fault_stays_until_openpilot_has_disengaged(self):
    # whatever happens in between: we stop asking (openpilot does, see below), or the EPS
    # starts steering after all
    for then in ("stop asking", "keep asking", "eps answers late"):
      car = Car()
      car.eps(EPS_READY)
      assert car.drive(True, 53).steerFaultTemporary
      if then == "eps answers late":
        car.eps(EPS_ACTIVE)
      rets = [car.drive(then != "stop asking", enabled=True) for _ in range(500)]
      assert all(r.steerFaultTemporary for r in rets), then
      # still engaged 3.5 s later: no soft disable took place, so it is made permanent
      permanent = [r.steerFaultPermanent for r in rets]
      assert 345 <= permanent.index(True) <= 352 and all(permanent[permanent.index(True):]), then
      car.eps(EPS_READY)
      rets = [car.drive(False, enabled=False) for _ in range(5)]
      assert not any(r.steerFaultTemporary or r.steerFaultPermanent for r in rets[1:]), then   # gone one step after openpilot let go
      # and the next engagement starts from nothing
      car.eps(EPS_ACTIVE)
      ret = car.drive(True, 600)
      assert not ret.steerFaultTemporary and not ret.steerFaultPermanent, then

  def test_with_openpilot_in_the_loop_it_asks_once_and_then_disengages(self):
    # controlsd: latActive = active and not steerFaultTemporary and not steerFaultPermanent.
    # selfdrived: a temporary steering fault is a soft disable, which ends the engagement 3 s later.
    car = Car()
    car.eps(EPS_READY)
    fault, soft_disable, trace = False, 300, []
    for _ in range(600):
      enabled = soft_disable > 0
      asking = enabled and not fault
      ret = car.drive(asking, enabled=enabled)
      fault = ret.steerFaultTemporary
      soft_disable -= fault
      trace.append((asking, fault, enabled, ret.steerFaultPermanent))
    asked = "".join("A" if a else "-" for a, _, _, _ in trace)
    reported = "".join("F" if f else "-" for _, f, _, _ in trace)
    first = reported.index("F")
    assert 50 <= first <= 53
    assert asked == "A" * len(asked.rstrip("-")) + "-" * (600 - len(asked.rstrip("-")))    # one stretch of asking, never again
    assert first <= len(asked.rstrip("-")) <= first + 1
    assert reported[first:first + 300] == "F" * 300                                         # reported for the whole soft disable
    assert "F" not in reported[first + 302:]                                                # and gone once disengaged
    assert not any(p for _, _, _, p in trace)                                               # never permanent on this path

  def test_with_openpilot_in_the_loop_and_no_soft_disable_it_ends_the_engagement_itself(self):
    # selfdrived: after an override a temporary steering fault is only a quiet warning, so
    # openpilot stays engaged; a permanent one disengages at once.
    car = Car()
    car.eps(EPS_READY)
    temporary = permanent = False
    enabled, trace = True, []
    for _ in range(700):
      enabled = enabled and not permanent
      asking = enabled and not temporary
      ret = car.drive(asking, enabled=enabled)
      temporary, permanent = ret.steerFaultTemporary, ret.steerFaultPermanent
      trace.append((asking, temporary, permanent, enabled))
    first = [t for _, t, _, _ in trace].index(True)
    gave_up = [p for _, _, p, _ in trace].index(True)
    assert 50 <= first <= 53
    assert 350 <= gave_up - first <= 352                                 # 3.5 s of the quiet warning, then permanent
    assert not trace[gave_up + 1][3]                                     # which ends the engagement in the next step
    assert not any(t or p for _, t, p, _ in trace[gave_up + 3:])         # and both are gone once it has
    assert sum(a for a, _, _, _ in trace) <= 53                          # asked once, never again

  def test_the_abort_recorded_in_the_car(self):
    # 2026-11-14 06:28:24.67: active -> aborted while we kept asking for 6.6 s more
    car = Car()
    car.eps(EPS_ACTIVE)
    assert not car.drive(True, 300).steerFaultPermanent
    car.eps(EPS_ABORTED)
    ret = car.drive(True)
    assert ret.steerFaultPermanent and not ret.steerFaultTemporary
    # openpilot disengages at once on a permanent fault, so nothing else builds up
    ret = car.drive(False, 300)
    assert ret.steerFaultPermanent and not ret.steerFaultTemporary

  def test_not_asking_is_never_a_fault(self):
    for state in (EPS_OFF, EPS_READY, EPS_ACTIVE):
      for enabled in (False, True):
        car = Car()
        car.eps(state)
        ret = car.drive(False, 300, enabled=enabled)
        assert not ret.steerFaultTemporary and not ret.steerFaultPermanent, (state, enabled)

  def test_atto3_is_untouched(self):
    car = Car(CAR.BYD_ATTO_3)
    car.eps(EPS_ABORTED)
    ret = car.drive(True, 200)
    assert not ret.steerFaultPermanent and not ret.steerFaultTemporary
    car.cam.vl["LKAS_HUD_ADAS"]["LKAS_STATE"] = 4       # the Atto 3's own fault read still works
    assert car.drive(True).steerFaultTemporary


class TestSongPlusYieldsToTheDriver(unittest.TestCase):
  """While the driver is turning the wheel the EPS is not asked to steer, and for half a second after."""

  PRESSING = 30.0      # driver torque above the steeringPressed threshold (24)

  def engaged(self):
    car = Car()
    car.speed(30)
    car.eps(EPS_ACTIVE)
    car.drive(True, 100)
    assert [req for req, _, _ in car.steering_frames()][-5:] == [1] * 5
    return car

  def requests(self, car, since):
    return [req for req, _, _ in car.steering_frames(since)]

  def test_request_is_withdrawn_within_a_tenth_of_a_second(self):
    car = self.engaged()
    mark = len(car.sent)
    car.driver_torque(self.PRESSING)
    car.drive(True, 100)
    reqs = self.requests(car, mark)
    assert 0 in reqs[:5]                      # 0x1E2 is every other step: within 100 ms
    assert set(reqs[reqs.index(0):]) == {0}   # and not asked again while he keeps turning it

  def test_request_comes_back_half_a_second_after_he_stops(self):
    car = self.engaged()
    car.driver_torque(self.PRESSING)
    car.drive(True, 100)
    car.driver_torque(0)
    mark = len(car.sent)
    car.drive(True, 100)
    reqs = self.requests(car, mark)
    back = reqs.index(1)
    assert 27 <= back <= 29, back             # 50 ms for steeringPressed to drop, then 500 ms
    assert set(reqs[back:]) == {1}

  def test_torque_hovering_at_the_threshold_is_one_withdrawal(self):
    car = self.engaged()
    car.driver_torque(self.PRESSING)
    car.drive(True, 20)
    mark = len(car.sent)
    for i in range(40):                        # 4 s of 26 / 22 / 26 / 22 ..., 100 ms each
      car.driver_torque(26 if i % 2 == 0 else 22)
      car.drive(True, 10)
    assert set(self.requests(car, mark)) == {0}

  def test_a_touch_too_light_or_too_short_changes_nothing(self):
    for torque, steps in ((20.0, 300), (self.PRESSING, 4)):
      car = self.engaged()
      mark = len(car.sent)
      car.driver_torque(torque)
      car.drive(True, steps)
      car.driver_torque(0)
      car.drive(True, 100)
      assert set(self.requests(car, mark)) == {1}, (torque, steps)

  def test_the_push_that_starts_a_lane_change_is_not_a_withdrawal(self):
    # openpilot starts a lane change on steeringPressed toward the blinker; the push is not the driver
    # taking the wheel (positive torque is the wheel turned left, as DesireHelper reads it)
    # the owner's 16 on 2026-11-15 peaked at 26.2-32.5 and were above 24 for 0.10-0.18 s
    for side, sign in (("left", 1.0), ("right", -1.0)):
      for torque in (24.5, 26.6, 28.0, 32.5, 34.9):
        car = self.engaged()
        mark = len(car.sent)
        car.driver_torque(sign * torque)
        ret = car.drive(True, 30, lane_change=side)       # 0.3 s
        assert ret.steeringPressed, (side, torque)        # still what starts the change
        car.driver_torque(0)
        car.drive(True, 100, lane_change=side)
        assert set(self.requests(car, mark)) == {1}, (side, torque)

  def test_letting_go_after_the_push_is_not_a_withdrawal(self):
    # steeringPressed outlasts the push by a few frames, and a released wheel reads about -1.1: against a
    # change to the left, but nowhere near the driver taking the wheel
    for side, sign in (("left", 1.0), ("right", -1.0)):
      car = self.engaged()
      mark = len(car.sent)
      car.driver_torque(sign * 26.0)
      car.drive(True, 30, lane_change=side)
      for released in (-1.1, -3.0, 1.1, 3.0):
        car.driver_torque(released)
        car.drive(True, 3, lane_change=side)
      car.drive(True, 100, lane_change=side)
      assert set(self.requests(car, mark)) == {1}, side

  def test_a_firm_push_during_a_lane_change_is_a_withdrawal(self):
    for side, sign in (("left", 1.0), ("right", -1.0)):
      car = self.engaged()
      mark = len(car.sent)
      car.driver_torque(sign * 35.0)
      car.drive(True, 100, lane_change=side)
      reqs = self.requests(car, mark)
      assert 0 in reqs[:5], side
      assert set(reqs[reqs.index(0):]) == {0}, side

  def test_a_push_held_toward_the_lane_change_is_a_withdrawal_after_half_a_second(self):
    # held, it is the driver steering: withdrawn from 0.5 s after steeringPressed, which comes 50 ms in
    for side, sign in (("left", 1.0), ("right", -1.0)):
      car = self.engaged()
      mark = len(car.sent)
      car.driver_torque(sign * 30.0)
      car.drive(True, 100, lane_change=side)
      reqs = self.requests(car, mark)
      first = reqs.index(0)
      assert 26 <= first <= 29, (side, first)
      assert set(reqs[first:]) == {0}, side

  def test_a_light_push_may_last_while_the_lane_change_shows(self):
    # route 00000033: a push of 29-31 easing into 23-28 toward the change, steeringPressed for 0.6 s and more
    for side, sign in (("left", 1.0), ("right", -1.0)):
      car = self.engaged()
      mark = len(car.sent)
      car.driver_torque(sign * 30.0)
      car.drive(True, 20, lane_change=side)                # firm for 0.2 s
      car.driver_torque(sign * 26.0)
      car.drive(True, 200, lane_change=side)               # then light for 2 s
      car.driver_torque(-1.1)
      car.drive(True, 60, lane_change=side)
      assert set(self.requests(car, mark)) == {1}, side

  def test_firm_frames_add_up_within_one_push(self):
    # firm for 0.3 s, light, firm again for 0.3 s: 60 firm frames in one push, past the half second
    car = self.engaged()
    mark = len(car.sent)
    for torque, steps in ((30.0, 30), (26.0, 50), (30.0, 30)):
      car.driver_torque(torque)
      car.drive(True, steps, lane_change="left")
    reqs = self.requests(car, mark)
    assert 0 in reqs[40:], reqs
    assert set(reqs[:40]) == {1}

  def test_a_push_against_the_lane_change_is_a_withdrawal(self):
    for side, sign in (("left", -1.0), ("right", 1.0)):
      car = self.engaged()
      mark = len(car.sent)
      car.driver_torque(sign * 25.0)
      car.drive(True, 100, lane_change=side)
      reqs = self.requests(car, mark)
      assert 0 in reqs[:5], side
      assert set(reqs[reqs.index(0):]) == {0}, side

  def test_the_cars_own_blinker_is_not_enough(self):
    # below 20 mph openpilot shows no lane change with the blinker on: the push is the driver steering
    car = self.engaged()
    car.pt.vl["STALKS"]["LEFT_BLINKER"] = 1
    mark = len(car.sent)
    car.driver_torque(25.0)
    car.drive(True, 100)
    reqs = self.requests(car, mark)
    assert 0 in reqs[:5]
    assert set(reqs[reqs.index(0):]) == {0}

  def test_easing_off_below_the_push_limit_during_a_lane_change(self):
    # a firm push is a withdrawal; once it eases below the limit (still above steeringPressed's 24, for
    # less than the half second a push may last) the request comes back half a second after the firm
    # part, gently, as after any other withdrawal
    car = self.engaged()
    car.driver_torque(36.0)
    car.drive(True, 30, lane_change="left")
    mark = len(car.sent)
    car.driver_torque(25.0)
    car.drive(True, 40, lane_change="left")
    car.driver_torque(-1.1)
    car.drive(True, 60, lane_change="left")
    reqs = self.requests(car, mark)
    back = reqs.index(1)
    assert 24 <= back <= 26, back             # 50 frames of hold at 100 Hz: 25 steering frames
    assert set(reqs[back:]) == {1}

  def test_the_push_stays_light_while_openpilot_shows_the_lane_change(self):
    # with the blinker left on after a change, DesireHelper goes back to preLaneChange and openpilot keeps
    # showing it: a light push toward it then starts the next change, and is not a withdrawal either
    car = self.engaged()
    mark = len(car.sent)
    for _ in range(3):
      car.driver_torque(26.0)
      car.drive(True, 40, lane_change="left")
      car.driver_torque(0)
      car.drive(True, 200, lane_change="left")
    assert set(self.requests(car, mark)) == {1}

  def test_commanded_angle_follows_the_wheel_and_restarts_from_it(self):
    car = self.engaged()                       # asking for 0 deg, wheel at 0
    car.driver_torque(self.PRESSING)
    mark = len(car.sent)
    for i in range(100):                       # he turns it to 20 deg in 1 s
      car.wheel(0.2 * (i + 1))
      car.drive(True)
    withdrawn = [(angle, i) for i, (req, angle, _) in enumerate(car.steering_frames(mark)) if req == 0]
    assert len(withdrawn) >= 45
    for angle, i in withdrawn:                 # frame i is built on step 2 i + 1 (or 2 i + 2) of the turn
      assert 0.2 * (2 * i) - 0.05 <= angle <= 0.2 * (2 * i + 3) + 0.05, (i, angle)
    car.driver_torque(0)
    mark = len(car.sent)
    car.drive(True, 100)                       # openpilot still wants 0 deg; the wheel stays at 20
    frames = car.steering_frames(mark)
    back = [req for req, _, _ in frames].index(1)
    assert all(abs(angle - 20.0) < 0.05 for _, angle, _ in frames[:back])
    first = frames[back][1]
    assert 19.8 < first <= 20.0, first         # from the wheel, not a jump towards 0
    asked = [angle for _, angle, _ in frames[back:]]
    assert all(b <= a for a, b in zip(asked, asked[1:], strict=False))          # then steadily towards the target
    assert asked[-1] < 15.0

  def test_rate_limit_field_ramps_down_and_up_around_it(self):
    car = self.engaged()
    car.driver_torque(self.PRESSING)
    mark = len(car.sent)
    car.drive(True, 100)
    frames = car.steering_frames(mark)
    first = [req for req, _, _ in frames].index(0)
    assert [limit for _, _, limit in frames[first:first + 14]] == list(range(484, 300, -16)) + [300, 300]
    car.driver_torque(0)
    mark = len(car.sent)
    car.drive(True, 100)
    frames = car.steering_frames(mark)
    back = [req for req, _, _ in frames].index(1)
    assert [limit for _, _, limit in frames[back:back + 14]] == list(range(316, 500, 16)) + [500, 500]

  def test_cluster_frame_follows_the_request(self):
    car = self.engaged()
    hud = lambda since: [(dat[0], dat[4]) for addr, dat in car.sent[since:] if addr == 0x316]  # noqa: E731
    assert hud(0)[-1] == (0x20, 0x38)          # steering: lane states 2, LKAS_STATE 3
    car.driver_torque(self.PRESSING)
    car.drive(True, 20)
    mark = len(car.sent)
    car.drive(True, 50)
    assert set(hud(mark)) == {(0x00, 0x00)}

  def test_withdrawing_is_not_an_unanswered_request(self):
    car = self.engaged()
    car.driver_torque(self.PRESSING)
    faults = [car.drive(True).steerFaultTemporary for _ in range(10)]
    car.eps(EPS_READY)                         # the EPS lets go
    faults += [car.drive(True).steerFaultTemporary for _ in range(400)]
    car.driver_torque(0)
    faults += [car.drive(True).steerFaultTemporary for _ in range(60)]   # asked again at the end of this
    car.eps(EPS_ACTIVE)
    ret = car.drive(True, 300)
    assert not any(faults) and not ret.steerFaultTemporary and not ret.steerFaultPermanent

  def test_an_eps_that_does_not_come_back_is_still_a_fault(self):
    car = self.engaged()
    car.driver_torque(self.PRESSING)
    car.drive(True, 100)
    car.eps(EPS_READY)
    car.driver_torque(0)
    faults = [car.drive(True).steerFaultTemporary for _ in range(120)]
    first = faults.index(True)
    assert 105 <= first <= 112, first          # 0.05 s + 0.5 s until asked again, then the 0.5 s limit

  def test_pressing_before_engaging_delays_the_first_request(self):
    car = Car()
    car.speed(30)
    car.eps(EPS_READY)
    car.driver_torque(self.PRESSING)
    car.drive(False, 50)
    mark = len(car.sent)
    car.drive(True, 50)
    assert set(self.requests(car, mark)) == {0}

  def test_atto3_keeps_asking(self):
    car = Car(CAR.BYD_ATTO_3)
    car.pt.vl["STEER_MODULE_2"]["DRIVER_EPS_TORQUE"] = 100
    car.drive(True, 50)
    assert car.CS.out.steeringPressed
    mark = len(car.sent)
    car.drive(True, 50)
    assert set(self.requests(car, mark)) == {1}


class TestSongPlusLowSpeedSmoothing(unittest.TestCase):
  """Below 40 km/h openpilot's angle is smoothed (0.1 s) before the limits, less up to 60 km/h, not above."""

  def engaged(self, kph):
    car = Car()
    car.speed(kph)
    car.eps(EPS_ACTIVE)
    car.drive(True, 200, target_deg=10.0)                     # past the gentle start, at the target
    return car

  def asked(self, car, targets):
    mark = len(car.sent)
    for target in targets:
      car.drive(True, 1, target_deg=target)
    return [angle for req, angle, _ in car.steering_frames(mark) if req]

  def test_twenty_hertz_steps_are_smoothed_at_low_speed(self):
    # the model's curvature arrives in 20 Hz steps: here 3 deg up and down every 0.05 s
    steps = [10.0 if (i // 5) % 2 == 0 else 13.0 for i in range(200)]
    seen = steps[-100:][1::2]                                            # what the 50 Hz frames are built from
    for kph in (20, 30, 39):
      asked = self.asked(self.engaged(kph), steps)
      assert np.ptp(asked[-50:]) < 1.0, (kph, np.ptp(asked[-50:]))        # from 3 deg peak to peak
      assert abs(sum(asked[-50:]) / 50 - sum(seen) / len(seen)) < 0.2, kph  # around the same middle

  def test_a_turn_is_followed_with_little_lag_at_low_speed(self):
    asked = self.asked(self.engaged(30), [30.0] * 50)                    # 20 deg more, at once
    assert asked[24] > 29.0, asked[24]                                     # within half a second
    assert all(b >= a for a, b in zip(asked, asked[1:], strict=False))    # without overshoot or wobble
    assert max(asked) <= 30.0

  def test_nothing_changes_at_speed(self):
    # Car.speed is the speedometer's km/h; the road speed the smoothing goes by is 0.9453 of it
    steps = [10.0 if (i // 5) % 2 == 0 else 13.0 for i in range(200)]
    for kph in (64, 80, 110):
      car, ref = self.engaged(kph), self.engaged(kph)
      with mock.patch.object(CarControllerParams, "STEER_SMOOTH_TAU_S", [0.0, 0.0]):
        unsmoothed = self.asked(ref, steps)
      assert self.asked(car, steps) == unsmoothed, kph

  def test_less_smoothing_between_40_and_60(self):
    steps = [10.0 if (i // 5) % 2 == 0 else 13.0 for i in range(200)]
    spread = [np.ptp(self.asked(self.engaged(kph), steps)[-50:]) for kph in (42, 50, 58)]   # 40, 47, 55 km/h on the road
    assert spread[0] < spread[1] < spread[2], spread

  def test_slowing_down_into_the_smoothing_is_seamless(self):
    # steering at speed, where nothing is smoothed, the smoothing takes over from where openpilot's angle is
    car = self.engaged(80)
    car.drive(True, 100, target_deg=15.0)                     # inside the 24 deg the limit allows at 80
    mark = len(car.sent)
    for kph in range(80, 20, -1):
      car.speed(kph)
      car.drive(True, 4, target_deg=15.0)
    asked = [angle for req, angle, _ in car.steering_frames(mark) if req]
    assert max(abs(a - 15.0) for a in asked) < 0.05, max(abs(a - 15.0) for a in asked)

  def test_after_a_yield_the_smoothing_starts_again_from_the_wheel(self):
    car = self.engaged(30)
    car.drive(True, 100, target_deg=30.0)                    # steering at 30 deg
    car.driver_torque(30.0)                                  # the driver takes the wheel to -10
    car.wheel(-10.0)
    car.drive(True, 100, target_deg=-10.0)
    car.driver_torque(0)
    mark = len(car.sent)
    car.drive(True, 150, target_deg=-10.0)
    asked = [angle for req, angle, _ in car.steering_frames(mark) if req]
    assert max(asked) < -9.5, max(asked)                     # not pulled back towards where it was before

  def test_a_request_starts_from_the_wheel(self):
    car = Car()
    car.speed(30)
    car.eps(EPS_READY)
    car.wheel(20.0)
    car.drive(False, 50)
    mark = len(car.sent)
    car.drive(True, 100, target_deg=0.0)
    asked = [angle for req, angle, _ in car.steering_frames(mark) if req]
    assert abs(asked[0] - 20.0) < 0.2, asked[0]                            # not from a stale smoothed angle
    assert asked[-1] < asked[0]


class TestSongPlusRequestStartsGently(unittest.TestCase):
  """For the first second of every request the commanded angle may change less per frame than
  the limits allow: from nothing, growing evenly to the usual amount."""

  def asked_from(self, platform=SONG_PLUS, kph=30, target_deg=130.0, steps=160):
    """Angles asked for, frame by frame, from the first frame of a request with the wheel at 0."""
    car = Car(platform)
    car.speed(kph)
    car.eps(EPS_READY)
    car.drive(False, 50)
    mark = len(car.sent)
    car.drive(True, steps, target_deg=target_deg)
    return [angle for req, angle, _ in car.steering_frames(mark) if req]

  def test_change_per_frame_grows_evenly_to_the_usual_amount(self):
    asked = self.asked_from()
    changes = [b - a for a, b in zip([0.0] + asked, asked, strict=False)]
    usual = sum(changes[52:58]) / 6
    assert abs(usual - safety_limits(30)[1]) < 0.06            # what the limits allow at 30 km/h
    assert all(abs(c - usual) < 0.11 for c in changes[49:60])  # reached after 50 frames (1 s), and kept (0.1 deg steps on the bus)
    for k in range(1, 50):                                     # before that: k fiftieths of it in frame k
      assert abs(changes[k - 1] - usual * k / 50) < 0.11, (k, changes[k - 1])
    assert asked[0] < 0.11                                     # the first angle asked for is the wheel's own
    assert abs(asked[24] - usual * 6.5) < 0.2                  # 21 deg after half a second, where it used to be 71

  def test_at_walking_pace_it_grows_to_the_ports_own_rate_limit(self):
    # below about 20 km/h the jerk limit allows more than MAX_ANGLE_RATE (5 deg per frame), which then is "the usual amount"
    asked = self.asked_from(kph=5, target_deg=300.0)
    changes = [b - a for a, b in zip([0.0] + asked, asked, strict=False)]
    assert all(abs(c - 5.0) < 0.11 for c in changes[49:60])
    for k in (1, 10, 25, 40, 49):
      assert abs(changes[k - 1] - 5.0 * k / 50) < 0.11, (k, changes[k - 1])

  def test_never_more_per_frame_than_before(self):
    gentle = self.asked_from()
    ramp_frames = CarControllerParams.STEER_REQUEST_RAMP_FRAMES
    try:
      CarControllerParams.STEER_REQUEST_RAMP_FRAMES = 1        # the same car without the gentle start
      plain = self.asked_from()
    finally:
      CarControllerParams.STEER_REQUEST_RAMP_FRAMES = ramp_frames
    for angles in (gentle, plain):
      changes = [b - a for a, b in zip([0.0] + angles, angles, strict=False)]
      assert max(changes) < 3.5
    assert all(g <= p + 0.051 for g, p in zip(gentle, plain, strict=True))
    assert plain[24] > 3 * gentle[24]                          # half a second in: 71 deg against 18
    assert abs(plain[-1] - 130.0) < 0.051 and abs(gentle[-1] - 130.0) < 0.051

  def test_a_target_close_by_is_reached_at_once_all_the_same(self):
    # the gentle start does not hold it back; at 30 km/h the low-speed smoothing (0.1 s) does, a little
    asked = self.asked_from(target_deg=0.5)
    assert abs(asked[14] - 0.5) < 0.051                        # within 15 frames, 300 ms

  def test_it_starts_over_with_every_request(self):
    car = Car()
    car.speed(30)
    car.eps(EPS_ACTIVE)
    car.drive(True, 200, target_deg=0.0)
    car.driver_torque(30.0)                                    # the driver takes the wheel to 30 deg
    car.wheel(30.0)
    car.drive(True, 100, target_deg=0.0)
    car.driver_torque(0)
    mark = len(car.sent)
    car.drive(True, 120, target_deg=0.0)
    frames = car.steering_frames(mark)
    back = [req for req, _, _ in frames].index(1)
    asked = [angle for _, angle, _ in frames[back:]]
    changes = [a - b for a, b in zip([30.0] + asked, asked, strict=False)]
    assert changes[0] < 0.11 and changes[1] < 0.21             # gentle again
    usual = safety_limits(30)[1]
    assert abs(changes[9] - usual / 5) < 0.15 and abs(changes[24] - usual / 2) < 0.15   # and growing again: a fifth and a half of the usual

  def test_atto3_starts_at_the_usual_rate(self):
    asked = self.asked_from(CAR.BYD_ATTO_3)
    assert asked[0] > 2.0


class TestSongPlusRequestStartsInsideTheLimit(unittest.TestCase):
  """A request does not start while the wheel is beyond the angle the lateral acceleration limit
  allows at that speed: no angle could be asked for there that panda would let through.
  At the 85 km/h on the road these run at (90 on the speedometer) the limit is 25.3 deg with the Song PLUS's
  own geometry, and a request may start below 0.9 of it, 22.8."""

  LIMIT = safety_limits(90)[0]
  START = CarControllerParams.STEER_REQUEST_START_ANGLE_RATIO * LIMIT

  def car(self, wheel_deg, kph=90):
    car = Car()
    car.speed(kph)
    car.wheel(wheel_deg)
    car.eps(EPS_READY)
    car.drive(False, 50)
    return car

  def frames(self, car, steps, **kw):
    mark = len(car.sent)
    car.drive(True, steps, **kw)
    return car.steering_frames(mark)

  def test_no_request_while_the_wheel_is_outside(self):
    wheel = round(self.LIMIT + 3.0, 1)
    car = self.car(wheel)
    frames = self.frames(car, 300)
    assert {req for req, _, _ in frames} == {0}
    assert all(abs(angle - wheel) < 0.051 for _, angle, _ in frames)    # "not steering": the wheel's own angle
    assert {limit for _, _, limit in frames} == {300}

  def test_it_starts_once_the_wheel_is_back_inside(self):
    start = round(self.LIMIT + 3.0, 1)
    car = self.car(start)
    self.frames(car, 100)
    mark = len(car.sent)
    wheel = []
    for i in range(100):                                                # the wheel comes back at 10 deg/s
      car.wheel(start - 0.1 * (i + 1))
      car.drive(True)
      wheel += [start - 0.1 * (i + 1)] * (len(car.steering_frames(mark)) - len(wheel))
    frames = car.steering_frames(mark)
    reqs = [req for req, _, _ in frames]
    first = reqs.index(1)
    assert set(reqs[:first]) == {0} and set(reqs[first:]) == {1}
    assert self.START - 0.45 < wheel[first] < self.START + 0.05         # as soon as it is under 0.9 of the limit
    # from the last angle sent, one frame old (panda checks the first request against that one)
    assert abs(frames[first][1] - wheel[first - 1]) < 0.11
    assert abs(frames[first][1] - frames[first - 1][1]) < 0.11

  def test_the_margin(self):
    inside, outside = round(self.START - 0.4, 1), round(self.START + 0.5, 1)
    for wheel, starts in ((inside, True), (-inside, True), (outside, False), (-outside, False), (round(self.LIMIT - 0.1, 1), False)):
      reqs = {req for req, _, _ in self.frames(self.car(wheel), 50)}
      assert reqs == ({1} if starts else {0}), wheel

  def test_the_limit_is_the_speeds(self):
    # 28 km/h on the road: 165 deg, so 100 deg is inside; 113 km/h: 17.7 deg, 0.9 of it 15.9
    assert {req for req, _, _ in self.frames(self.car(100.0, kph=30), 50)} == {1}
    start_120 = CarControllerParams.STEER_REQUEST_START_ANGLE_RATIO * safety_limits(120)[0]
    assert {req for req, _, _ in self.frames(self.car(round(start_120 + 0.6, 1), kph=120), 50)} == {0}
    assert {req for req, _, _ in self.frames(self.car(round(start_120 - 0.4, 1), kph=120), 50)} == {1}

  def test_a_running_request_is_not_ended_by_it(self):
    car = self.car(10.0)
    assert {req for req, _, _ in self.frames(car, 100, target_deg=10.0)} == {1}
    car.wheel(round(self.START + 1.0, 1))                               # past 0.9 of the limit, still inside the limit itself
    assert {req for req, _, _ in self.frames(car, 100, target_deg=10.0)} == {1}
    car.speed(120)                                                      # the limit shrinks to 17.7 under the wheel
    assert {req for req, _, _ in self.frames(car, 100, target_deg=10.0)} == {1}

  def test_it_holds_after_the_driver_has_turned_the_wheel_too(self):
    car = self.car(5.0)
    assert {req for req, _, _ in self.frames(car, 100, target_deg=5.0)} == {1}
    car.driver_torque(30.0)                                             # he takes it to 25 deg and lets go
    car.wheel(25.0)
    self.frames(car, 100, target_deg=5.0)
    car.driver_torque(0)
    assert {req for req, _, _ in self.frames(car, 300, target_deg=5.0)} == {0}   # long past the half second
    car.wheel(16.0)
    assert {req for req, _, _ in self.frames(car, 20, target_deg=5.0)} == {1}

  def test_waiting_outside_is_not_a_fault(self):
    car = self.car(25.0)
    rets = [car.drive(True) for _ in range(400)]
    assert not any(r.steerFaultTemporary or r.steerFaultPermanent for r in rets)

  def test_atto3_asks_at_once(self):
    car = Car(CAR.BYD_ATTO_3)
    car.speed(90)
    car.wheel(25.0)
    car.drive(False, 50)
    assert {req for req, _, _ in self.frames(car, 50)} == {1}


class TestSongPlusRefusedSteeringFrames(unittest.TestCase):
  """Steering frames asking to steer that panda refused to send come back from pandad with 0xC0
  added to their bus. Three within half a second are the same fault as an unanswered request."""

  def engaged(self):
    car = Car()
    car.speed(30)
    car.eps(EPS_ACTIVE)
    assert not car.drive(True, 100).steerFaultTemporary
    return car

  def drive_rejecting(self, car, steps, every=2, steer_req=1, enabled=True):
    """Drive `steps` steps; panda refuses one steering frame every `every` steps (a frame is built every other step)."""
    out = []
    for i in range(steps):
      if i % every == 0:
        car.reject(steer_req)
      out.append(car.drive(enabled, enabled=enabled))
    return out

  def test_refusing_every_frame_is_a_fault_after_the_third(self):
    car = self.engaged()
    faults = [r.steerFaultTemporary for r in self.drive_rejecting(car, 20)]
    # refusals seen in steps 0, 2, 4; the CarController latches in step 4, CarState reports from step 5
    assert faults.index(True) == 5
    assert all(faults[5:])

  def test_it_is_latched_like_an_unanswered_request(self):
    car = self.engaged()
    self.drive_rejecting(car, 6)
    rets = [car.drive(True, enabled=True) for _ in range(400)]       # no refusals any more, the EPS steering
    assert all(r.steerFaultTemporary for r in rets)
    permanent = [r.steerFaultPermanent for r in rets]
    assert 340 <= permanent.index(True) <= 352                       # still engaged 3.5 s after it began
    rets = [car.drive(False, enabled=False) for _ in range(3)]
    assert not rets[-1].steerFaultTemporary and not rets[-1].steerFaultPermanent
    assert not car.drive(True, 300).steerFaultTemporary              # the next engagement starts from nothing

  def test_one_or_two_refusals_are_not_a_fault(self):
    # what a brake press costs: panda withdraws permission on the brake frame, before openpilot stops asking
    for n in (1, 2):
      car = self.engaged()
      faults = [r.steerFaultTemporary for r in self.drive_rejecting(car, 2 * n)]
      faults += [car.drive(True).steerFaultTemporary for _ in range(500)]
      assert not any(faults), n

  def test_refusals_further_apart_than_half_a_second_are_not_a_fault(self):
    car = self.engaged()
    faults = [r.steerFaultTemporary for r in self.drive_rejecting(car, 1000, every=26)]   # three take 52 steps
    assert not any(faults)
    car = self.engaged()
    faults = [r.steerFaultTemporary for r in self.drive_rejecting(car, 60, every=24)]     # three take 48 steps
    assert any(faults)

  def test_several_in_one_update_count_each(self):
    car = self.engaged()
    car.reject(1, 1, 1)                     # pandad can hand back a batch after a stall
    car.drive(True)
    assert car.drive(True).steerFaultTemporary
    car = self.engaged()
    car.reject(1, 0, 1)
    car.drive(True)
    assert not car.drive(True, 100).steerFaultTemporary

  def test_refused_frames_that_do_not_ask_do_not_count(self):
    # e.g. before panda is in the car's safety mode it refuses every frame, asking or not
    car = self.engaged()
    faults = [r.steerFaultTemporary for r in self.drive_rejecting(car, 400, every=1, steer_req=0)]
    assert not any(faults)

  def test_refusals_while_not_engaged_count_for_nothing_later(self):
    car = Car()
    car.speed(30)
    car.eps(EPS_READY)
    faults = [r.steerFaultTemporary for r in self.drive_rejecting(car, 10, every=1, enabled=False)]
    car.eps(EPS_ACTIVE)
    faults += [car.drive(True).steerFaultTemporary for _ in range(300)]     # engaged at once, nothing refused since
    assert not any(faults)

  def test_refusals_before_a_disengagement_count_for_nothing_after_it(self):
    car = self.engaged()
    self.drive_rejecting(car, 4)               # two refusals while engaged: a brake press
    car.drive(False, 2, enabled=False)         # openpilot lets go
    car.reject(1)                              # engaged again at once, and one more refusal
    faults = [car.drive(True).steerFaultTemporary for _ in range(100)]
    assert not any(faults)

  def test_with_openpilot_in_the_loop_it_stops_asking_and_disengages(self):
    # From now on panda refuses every frame that asks to steer (permission lost, or a check that
    # keeps failing). controlsd stops asking on the fault, frames that do not ask pass, and the
    # 3 s soft disable ends the engagement.
    car = self.engaged()
    fault, soft_disable, refused, pending, trace = False, 300, 0, [], []
    for step in range(500):
      enabled = soft_disable > 0
      asking = enabled and not fault
      due = [s for s in pending if s == step]
      pending = [s for s in pending if s != step]
      if due:
        car.reject(*[1] * len(due))           # back from pandad two steps after it was built
        refused += len(due)
      mark = len(car.sent)
      ret = car.drive(asking, enabled=enabled)
      pending += [step + 2 for req, _, _ in car.steering_frames(mark) if req]
      fault = ret.steerFaultTemporary
      soft_disable -= fault
      trace.append((asking, fault, enabled))
    first = [f for _, f, _ in trace].index(True)
    assert first <= 8                          # 80 ms
    assert 3 <= refused <= 5
    assert not any(a for a, _, _ in trace[first + 1:])     # asked no more once the fault was reported
    assert [e for _, _, e in trace].index(False) == first + 300
    assert not ret.steerFaultTemporary and not ret.steerFaultPermanent

  def test_atto3_does_not_watch(self):
    car = Car(CAR.BYD_ATTO_3)
    faults = []
    for _ in range(50):
      car.reject(1, 1)
      faults.append(car.drive(True).steerFaultTemporary)
    assert not any(faults)


class TestSongPlusRefusedFrameSentAgain(unittest.TestCase):
  """A steering frame panda refused is sent again under its own counter; the cluster frame skips
  that slot. On the bus both counters run on unbroken and equal."""

  def run_bus(self, car, steps, refuse, delay=1, asking=lambda i: True, enabled=lambda i: True):
    """Drive `steps` steps; refuse(i, addr, steer_req) says whether panda refuses a frame built in
    step i. A refused 0x1E2 comes back `delay` steps later. Returns what reached the bus:
    (step, address, counter, STEER_REQ or None)."""
    bus, pending = [], {}
    for i in range(steps):
      back = pending.pop(i, [])
      if back:
        car.reject(*[req for req, _ in back], counters=[counter for _, counter in back])
      mark = len(car.sent)
      car.drive(asking(i), enabled=enabled(i))
      for addr, dat in car.sent[mark:]:
        counter = dat[6] >> 4
        req = (dat[2] >> 5) & 1 if addr == 0x1E2 else None
        if refuse(i, addr, req):
          if addr == 0x1E2:
            pending.setdefault(i + delay, []).append((req, counter))
          continue
        bus.append((i, addr, counter, req))
    return bus

  @staticmethod
  def counters(bus, addr):
    return [counter for _, a, counter, _ in bus if a == addr]

  @staticmethod
  def unbroken(counters):
    return all((b - a) % 16 == 1 for a, b in zip(counters, counters[1:], strict=False))

  def engaged(self, platform=SONG_PLUS):
    car = Car(platform)
    car.speed(30)
    car.eps(EPS_ACTIVE)
    car.drive(True, 100)
    return car

  def test_without_refusals_the_frames_are_as_before(self):
    car = Car()
    car.speed(30)
    car.eps(EPS_ACTIVE)
    bus = self.run_bus(car, 400, lambda i, a, r: False)
    steer, hud = self.counters(bus, 0x1E2), self.counters(bus, 0x316)
    assert steer == hud == [n % 16 for n in range(200)]         # one per frame from 0, as (frame // 2) % 16 was

  def test_a_refused_frame_is_sent_again_under_its_counter(self):
    car = self.engaged()
    refused_step = 41                           # an odd step of this run: a frame is built in it
    bus = self.run_bus(car, 100, lambda i, a, r: a == 0x1E2 and i == refused_step)
    steer, hud = self.counters(bus, 0x1E2), self.counters(bus, 0x316)
    assert self.unbroken(steer) and self.unbroken(hud)
    slots = sorted({i for i, _, _, _ in bus})
    resend_slot = refused_step + 2
    assert [a for i, a, _, _ in bus if i == resend_slot] == [0x1E2]          # no cluster frame in that slot
    later = [i for i in slots if i > resend_slot]
    for i in later:                                                           # and from the next slot on, in step again
      assert len({c for j, _, c, _ in bus if j == i}) == 1

  def test_two_refused_in_a_row_are_both_sent_again(self):
    # e.g. after a brake press openpilot asks once more before it lets go, and that is refused too
    car = self.engaged()
    bus = self.run_bus(car, 100, lambda i, a, r: a == 0x1E2 and i in (41, 43))
    steer, hud = self.counters(bus, 0x1E2), self.counters(bus, 0x316)
    assert self.unbroken(steer) and self.unbroken(hud)                        # never back, never a gap
    assert [a for i, a, _, _ in bus if i in (43, 45)] == [0x1E2]               # the cluster frame skipped two slots
    assert len(steer) == len([i for i in range(100) if i % 2 == 1]) - 2

  def test_a_refusal_learnt_too_late_changes_nothing(self):
    # the next frame has already gone out with the next counter: no going back
    car = self.engaged()
    bus = self.run_bus(car, 100, lambda i, a, r: a == 0x1E2 and i == 41, delay=3)
    steer, hud = self.counters(bus, 0x1E2), self.counters(bus, 0x316)
    steps = [(b - a) % 16 for a, b in zip(steer, steer[1:], strict=False)]
    assert steps.count(2) == 1 and set(steps) == {1, 2}                       # the one gap, as before this change
    assert self.unbroken(hud) and len(hud) == 50                              # the cluster frame as before

  def test_a_brake_press(self):
    # panda withdraws permission in step 41, openpilot lets go in step 45: the frames that ask
    # in between are refused, the first one that does not ask carries the counter of the first refused
    car = self.engaged()
    bus = self.run_bus(car, 100, lambda i, a, r: a == 0x1E2 and i >= 41 and r == 1,
                       asking=lambda i: i < 45, enabled=lambda i: i < 45)
    steer = [(c, r) for _, a, c, r in bus if a == 0x1E2]
    assert self.unbroken([c for c, _ in steer]) and self.unbroken(self.counters(bus, 0x316))
    last_asking = max(n for n, (_, r) in enumerate(steer) if r == 1)
    assert steer[last_asking + 1][1] == 0                                     # straight to "not asking", nothing missing

  def test_frames_refused_before_panda_lets_anything_through(self):
    # start-up: every frame is refused until panda is in the car's safety mode
    car = Car()
    car.speed(0)
    bus = self.run_bus(car, 100, lambda i, a, r: i < 30, asking=lambda i: False, enabled=lambda i: False)
    steer, hud = self.counters(bus, 0x1E2), self.counters(bus, 0x316)
    assert steer[0] == 0 and self.unbroken(steer)                             # the first to reach the bus is counter 0
    assert self.unbroken(hud) and hud[0] in (0, 1)

  def test_atto3_never_sends_again(self):
    car = self.engaged(CAR.BYD_ATTO_3)
    bus = self.run_bus(car, 100, lambda i, a, r: a == 0x1E2 and i == 41)
    steps = [(b - a) % 16 for a, b in zip(self.counters(bus, 0x1E2), self.counters(bus, 0x1E2)[1:], strict=False)]
    assert steps.count(2) == 1 and len(self.counters(bus, 0x316)) == 50


class TestSongPlusRefusedFramesParser(unittest.TestCase):
  """The real CANParser that reads them: it must count them and never make canValid false."""

  def setUp(self):
    CarInterface = interfaces[SONG_PLUS]
    self.CI = CarInterface(CarInterface.get_non_essential_params(SONG_PLUS))
    self.packer = CANPacker(DBC[SONG_PLUS][Bus.pt])
    self.t = 0

  def frame(self, steer_req, counter, src):
    msg = bydcan.create_steering_control(self.packer, 1.5, steer_req, counter, 500 if steer_req else 300, 0)
    return CanData(msg[0], bytes(msg[1]), src)

  def update(self, frames=()):
    self.t += 10_000_000
    self.CI.update([(self.t, list(frames))])
    return self.CI.CS.steer_requests_rejected

  def test_their_counters_are_read(self):
    self.update([self.frame(True, 3, 0xC0), self.frame(False, 11, 0xC0)])
    assert self.CI.CS.steer_counters_rejected == [3, 11]
    self.update()
    assert self.CI.CS.steer_counters_rejected == []

  def test_only_refused_frames_that_ask_are_counted(self):
    assert self.update([self.frame(True, 3, 0xC0)]) == 1
    assert self.update([self.frame(True, 4, 0xC0), self.frame(True, 9, 0xC0)]) == 2
    assert self.update([self.frame(False, 5, 0xC0)]) == 0
    assert self.update() == 0
    for src in (0, 2, 0x80, 0x82, 0xC2):      # sent and echoed, the camera's, and refused on another bus
      assert self.update([self.frame(True, 6, src)]) == 0, src

  def test_a_frame_with_a_broken_checksum_is_not_counted(self):
    good = self.frame(True, 7, 0xC0)
    bad = CanData(good.address, good.dat[:7] + bytes([good.dat[7] ^ 0xFF]), 0xC0)
    assert self.update([bad]) == 0

  def test_they_never_make_the_parser_invalid(self):
    cp = self.CI.can_parsers[Bus.loopback]
    rng = random.Random(0)
    for i in range(3000):                     # 30 s: mostly nothing, now and then a refused frame with any counter
      frames = [self.frame(rng.random() < 0.5, rng.randrange(16), 0xC0)] if rng.random() < 0.03 else []
      self.update(frames)
      assert cp.can_valid and not cp.bus_timeout, i
    assert all(st.counter_fail == 0 for st in cp.message_states.values())

  def test_the_parser_is_the_song_plus_only(self):
    assert self.CI.can_parsers[Bus.loopback].bus == 0xC0
    CarInterface = interfaces[CAR.BYD_ATTO_3]
    atto3 = CarInterface(CarInterface.get_non_essential_params(CAR.BYD_ATTO_3))
    assert set(atto3.can_parsers) == {Bus.pt, Bus.cam}


if __name__ == "__main__":
  unittest.main()
