#!/usr/bin/env python3
import itertools
import random
import unittest
from fractions import Fraction
import numpy as np

from opendbc.car.byd.carcontroller import get_safety_CP
from opendbc.car.byd.interface import CarInterface
from opendbc.car.byd.values import CAR, CarControllerParams
from opendbc.car.lateral import get_max_angle_delta_vm, get_max_angle_vm
from opendbc.car.structs import CarParams
from opendbc.car.vehicle_model import VehicleModel
import opendbc.safety.tests.common as common
from opendbc.safety.tests.common import away_round
from opendbc.safety.tests.libsafety import libsafety_py

STEERING_MODULE_ADAS = 0x1E2
LKAS_HUD_ADAS = 0x316
PCM_BUTTONS = 0x3B0
ACC_HUD_ADAS = 0x32D
ACC_CMD = 0x32E
ACC_AEB = 0x32F


def safety_max_can(max_angle_float, can_offset=0):
  # Matches C: max_angle_can = (int)(max_angle * 10 + 1.) which is floor(max_angle * 10) + 1
  return int(max_angle_float * 10 + 1.) + can_offset


class TestBydSafety(common.CarSafetyTest, common.AngleSteeringSafetyTest):
  DBC = "byd_atto3"
  SAFETY_MODEL = CarParams.SafetyModel.byd

  RELAY_MALFUNCTION_ADDRS = {0: (STEERING_MODULE_ADAS, LKAS_HUD_ADAS)}
  FWD_BLACKLISTED_ADDRS = {2: [STEERING_MODULE_ADAS, LKAS_HUD_ADAS]}
  TX_MSGS = [[STEERING_MODULE_ADAS, 0], [LKAS_HUD_ADAS, 0], [PCM_BUTTONS, 0]]

  MAIN_BUS = 0
  CAM_BUS = 2

  STEER_ANGLE_MAX = 390  # deg, EPS fault limit
  DEG_TO_CAN = 10

  # BYD uses get_max_angle_delta_vm and get_max_angle_vm for lateral accel and jerk limits
  ANGLE_RATE_BP = None
  ANGLE_RATE_UP = None
  ANGLE_RATE_DOWN = None

  # Real time limits
  LATERAL_FREQUENCY = 50  # Hz

  # One CAN count of vehicle speed, in kph, as byd.h scales it into UPDATE_VEHICLE_SPEED.
  SPEED_KPH_PER_LSB = 0.1
  # Messages byd.h checks checksum and counter on:
  # (message, bus, signal, a valid value, the value sent in the corrupted frame)
  RX_CHECKED = (
    ("WHEELSPEED_CLEAN", 0, "WHEELSPEED_CLEAN", 72, 0),
    ("ACC_HUD_ADAS", 2, "ACC_STATE", 0, 3),
  )
  CRUISE_MSG = "ACC_HUD_ADAS"

  def _rx_checked_values(self, name, signal, value):
    return {signal: value}

  def _quantized_speed(self, speed):
    # match both the CAN encoding and VEHICLE_SPEED_FACTOR=1000 rounding in UPDATE_VEHICLE_SPEED
    sent = speed + 1
    sent_can = away_round(sent / self.SPEED_KPH_PER_LSB * 3.6) * self.SPEED_KPH_PER_LSB / 3.6
    # lateral.h clamps the fudged speed at 1 m/s; a scale that does not land on a round
    # number (the Song Plus's) would otherwise leave this a hair below it
    return max(round(sent_can * 1000) / 1000 - 1, 1)

  @staticmethod
  def _fix_checksum(msg):
    msg[0].data[7] = (0xFF - (sum(msg[0].data[i] for i in range(7)) & 0xFF)) & 0xFF
    return msg

  def _get_steer_cmd_angle_max(self, speed):
    return get_max_angle_vm(max(speed, 1), self.VM, CarControllerParams)

  SAFETY_VM = CAR.BYD_ATTO_3   # whose geometry byd.h checks this param's angles with

  def setUp(self):
    super().setUp()
    self.VM = VehicleModel(get_safety_CP(self.SAFETY_VM))

  def _angle_cmd_msg(self, angle: float, enabled: bool, increment_timer: bool = True):
    values = {"STEER_ANGLE": angle, "STEER_REQ": 1 if enabled else 0, "STEER_REQ_ACTIVE_LOW": 0 if enabled else 1}
    if increment_timer:
      self.safety.set_timer(self.__class__.cnt_angle_cmd * int(1e6 / self.LATERAL_FREQUENCY))
      self.__class__.cnt_angle_cmd += 1
    return self.packer.make_can_msg_safety("STEERING_MODULE_ADAS", self.MAIN_BUS, values)

  cnt_angle_cmd = 0

  def _angle_meas_msg(self, angle: float):
    values = {"STEER_ANGLE_2": angle}
    return self.packer.make_can_msg_safety("STEER_MODULE_2", self.MAIN_BUS, values)

  def _pcm_status_msg(self, enable):
    # ACC_STATE: 0=OFF, 3=ACC_ACTIVE
    values = {"ACC_STATE": 3 if enable else 0}
    return self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, values)

  def _speed_msg(self, speed):
    values = {"WHEELSPEED_CLEAN": speed * 3.6}
    return self.packer.make_can_msg_safety("WHEELSPEED_CLEAN", self.MAIN_BUS, values)

  def _user_brake_msg(self, brake):
    values = {"BRAKE_PRESSED": 1 if brake else 0}
    return self.packer.make_can_msg_safety("DRIVE_STATE", self.MAIN_BUS, values)

  def _user_gas_msg(self, gas):
    values = {"RAW_THROTTLE": int(gas * 100)}
    return self.packer.make_can_msg_safety("DRIVE_STATE", self.MAIN_BUS, values)

  def test_acc_state_values(self):
    # ACC_STATE: 0=OFF, 2=ACC_ON, 3=ACC_ACTIVE, 5=FORCE_ACCEL, 7=ERROR. Engaged is 3 or 5.
    if self.SAFETY_PARAM != 0:
      raise unittest.SkipTest("the Song Plus does not use ACC_STATE")
    for state in (0, 1, 2, 3, 0, 4, 5, 0, 6, 7):
      self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_STATE": state}))
      self.assertEqual(state in (3, 5), self.safety.get_cruise_engaged_prev(), state)

  def test_cruise_buttons(self):
    buttons = ("SET_BTN", "RES_BTN", "LKAS_ON_BTN", "DEC_DISTANCE_BTN", "INC_DISTANCE_BTN", "ACC_ON_BTN")
    for cruise_engaged, controls_allowed in itertools.product((False, True), repeat=2):
      self.assertTrue(self._rx(self._pcm_status_msg(cruise_engaged)))
      self.safety.set_controls_allowed(controls_allowed)
      for pressed in itertools.product((False, True), repeat=len(buttons)):
        values = dict(zip(buttons, pressed, strict=True))
        values.update(SET_ME_1_1=1, SET_ME_1_2=1)
        with self.subTest(cruise_engaged=cruise_engaged, controls_allowed=controls_allowed, buttons=pressed):
          msg = self.packer.make_can_msg_safety("PCM_BUTTONS", self.MAIN_BUS, values)
          should_tx = not any(pressed[:-1]) and (not pressed[-1] or cruise_engaged)
          self.assertEqual(should_tx, self._tx(msg))

  def test_rx_checksums(self):
    for name, bus, signal, initial, corrupt in self.RX_CHECKED:
      for byte in range(8):
        with self.subTest(message=name, byte=byte):
          self.safety.set_safety_hooks(CarParams.SafetyModel.byd, self.SAFETY_PARAM)
          self.safety.init_tests()
          for _ in range(16):
            msg = self.packer.make_can_msg_safety(name, bus, self._rx_checked_values(name, signal, initial))
            self.assertTrue(self._rx(msg))
          speed_min = self.safety.get_vehicle_speed_min()
          speed_max = self.safety.get_vehicle_speed_max()
          msg = self.packer.make_can_msg_safety(name, bus, self._rx_checked_values(name, signal, corrupt))
          msg[0].data[byte] ^= 0xFF
          # the corrupted cruise frame says "engaged": it must not be able to enable controls.
          # For the others, start with controls allowed and check a bad frame takes them away.
          self.safety.set_controls_allowed(name != self.CRUISE_MSG)
          self.assertFalse(self._rx(msg))
          self.assertFalse(self.safety.get_controls_allowed())
          self.assertEqual(speed_min, self.safety.get_vehicle_speed_min())
          self.assertEqual(speed_max, self.safety.get_vehicle_speed_max())

  def test_rx_counters(self):
    for name, bus, signal, initial, corrupt in self.RX_CHECKED:
      value = max(initial, corrupt)
      with self.subTest(message=name):
        # Check both counter locations and rollover with valid checksums.
        for _ in range(32):
          msg = self.packer.make_can_msg_safety(name, bus, self._rx_checked_values(name, signal, value))
          self.assertTrue(self._rx(msg))
        self.safety.set_controls_allowed(True)
        # Replayed frames are rejected after the common counter tolerance.
        for i in range(common.MAX_WRONG_COUNTERS + 1):
          should_rx = i < common.MAX_WRONG_COUNTERS - 1
          self.assertEqual(should_rx, self._rx(msg))
          self.assertEqual(should_rx, self.safety.get_controls_allowed())
        # A valid sequence clears the counter faults.
        for _ in range(common.MAX_WRONG_COUNTERS):
          msg = self.packer.make_can_msg_safety(name, bus, self._rx_checked_values(name, signal, 0))
          self.assertTrue(self._rx(msg))

  def test_car_messages_are_not_read_from_the_camera_bus(self):
    # With the relay open the car's own messages show up on bus 2 as well. Only ACC_HUD_ADAS
    # is read there; speed, brake and gas must come from bus 0.
    for msg in (self._speed_msg(20), self._user_brake_msg(1), self._user_gas_msg(1)):
      msg[0].bus = self.CAM_BUS
      self._rx(msg)
    self.assertEqual(0, self.safety.get_vehicle_speed_max())
    self.assertFalse(self.safety.get_vehicle_moving())
    self.assertFalse(self.safety.get_brake_pressed_prev())
    self.assertFalse(self.safety.get_gas_pressed_prev())

  def test_angle_cmd_when_enabled(self):
    # We properly test lateral acceleration and jerk below
    pass

  def test_lateral_accel_limit(self):
    for speed in np.linspace(0, 40, 100):
      speed = self._quantized_speed(max(speed, 1))
      for sign in (-1, 1):
        self.safety.set_controls_allowed(True)
        self._reset_speed_measurement(speed + 1)  # safety fudges the speed

        max_angle_float = get_max_angle_vm(speed, self.VM, CarControllerParams)

        # at limit (safety tolerance adds 1 CAN unit)
        max_angle_can = safety_max_can(max_angle_float)
        max_angle_can = min(max_angle_can, self.STEER_ANGLE_MAX * self.DEG_TO_CAN)
        max_angle = sign * max_angle_can / self.DEG_TO_CAN
        self.safety.set_desired_angle_last(sign * max_angle_can)

        self.assertTrue(self._tx(self._angle_cmd_msg(max_angle, True)))

        # 1 unit above limit
        over_can = safety_max_can(max_angle_float, 1)
        over_can_clipped = min(over_can, self.STEER_ANGLE_MAX * self.DEG_TO_CAN)
        over_angle = sign * over_can_clipped / self.DEG_TO_CAN
        self._tx(self._angle_cmd_msg(over_angle, True))

        # at low speeds max angle is above STEER_ANGLE_MAX, so adding 1 has no effect
        should_tx = over_can >= self.STEER_ANGLE_MAX * self.DEG_TO_CAN
        self.assertEqual(should_tx, self._tx(self._angle_cmd_msg(over_angle, True)))

  def test_lateral_jerk_limit(self):
    for speed in np.linspace(0, 40, 100):
      speed = self._quantized_speed(max(speed, 1))
      for sign in (-1, 1):
        self.safety.set_controls_allowed(True)
        self._reset_speed_measurement(speed + 1)  # safety fudges the speed
        self._tx(self._angle_cmd_msg(0, True))

        max_delta_float = get_max_angle_delta_vm(speed, self.VM, CarControllerParams)

        # Stay within limits
        # Up
        max_delta_can = safety_max_can(max_delta_float)
        max_angle_delta = sign * max_delta_can / self.DEG_TO_CAN
        self.assertTrue(self._tx(self._angle_cmd_msg(max_angle_delta, True)))

        # Don't change
        self.assertTrue(self._tx(self._angle_cmd_msg(max_angle_delta, True)))

        # Down
        self.assertTrue(self._tx(self._angle_cmd_msg(0, True)))

        # Inject too high rates
        # Up
        over_delta_can = safety_max_can(max_delta_float, 1)
        max_angle_delta = sign * over_delta_can / self.DEG_TO_CAN
        self.assertFalse(self._tx(self._angle_cmd_msg(max_angle_delta, True)))

        # Don't change
        self.safety.set_desired_angle_last(sign * over_delta_can)
        self.assertTrue(self._tx(self._angle_cmd_msg(max_angle_delta, True)))

        # Down
        self.assertFalse(self._tx(self._angle_cmd_msg(0, True)))

        # Recover
        self.assertTrue(self._tx(self._angle_cmd_msg(0, True)))


class TestBydSongPlusSafety(TestBydSafety):
  """The whole BYD suite again under BYD_PARAM_SONG_PLUS, built from this car's messages.

  What the param changes in byd.h: vehicle speed is WHEELSPEED_FL (12 bits) scaled to road
  speed, brake comes from IPB, gas from PEDAL, and cruise-engaged from the follow-distance
  setting (SET_DISTANCE, bits 10-12 of ACC_HUD_ADAS) together with ACC_STATE_CODE (the ACC's
  own state), the same rule as carstate.py. Each was fitted to recordings of the car and each is exercised here
  both through the common suite and by the focused cases at the end.

  The param also selects the Song PLUS's own steering geometry (see byd_tx_hook), so the angle-limit
  tests run with its vehicle model, the one the controller limits with (carcontroller.get_safety_CP).
  """

  DBC = "byd_song_plus"
  SAFETY_PARAM = 1   # BYD_PARAM_SONG_PLUS
  SAFETY_VM = CAR.BYD_SONG_PLUS_DMI_23

  # Must match byd.h's Song Plus speed constant: DBC wheel scale * wheelSpeedFactor.
  DBC_WHEEL_SCALE = 0.0725  # WHEELSPEED_FL kph/LSB in byd_song_plus.dbc
  WHEEL_SPEED_FACTOR = CarInterface.get_non_essential_params(CAR.BYD_SONG_PLUS_DMI_23).wheelSpeedFactor
  SPEED_KPH_PER_LSB = DBC_WHEEL_SCALE * WHEEL_SPEED_FACTOR

  RX_CHECKED = (
    ("WHEELSPEED_CLEAN", 0, "WHEELSPEED_FL", 72, 0),
    ("ACC_HUD_ADAS", 2, "ACC_ENGAGED_STATE", 0, 2),
    ("IPB", 0, "BRAKE_PRESSED", 0, 1),
    ("PEDAL", 0, "GAS_PEDAL", 0, 1.0),
  )

  def _rx_checked_values(self, name, signal, value):
    values = {signal: value}
    if name == "ACC_HUD_ADAS":
      # a frame that says "engaged" has to say so in both fields
      values["ACC_STATE_CODE"] = 3 if value else 2
    return values

  def _pcm_status_msg(self, enable):
    # engaged: the ACC is on (ACC_ENGAGED_STATE 1 or 2) and controlling (ACC_STATE_CODE 3);
    # not engaged: standby, as the car sends it (0 and 2)
    values = {"ACC_ENGAGED_STATE": 2, "ACC_STATE_CODE": 3} if enable else {"ACC_ENGAGED_STATE": 0, "ACC_STATE_CODE": 2}
    return self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, values)

  def _speed_msg(self, speed):
    # speed is the road speed the panda should end up with; the car reports it 1/factor higher
    values = {"WHEELSPEED_FL": speed * 3.6 / self.WHEEL_SPEED_FACTOR}
    return self.packer.make_can_msg_safety("WHEELSPEED_CLEAN", self.MAIN_BUS, values)

  def _user_brake_msg(self, brake):
    return self.packer.make_can_msg_safety("IPB", self.MAIN_BUS, {"BRAKE_PRESSED": 1 if brake else 0})

  def _user_gas_msg(self, gas):
    return self.packer.make_can_msg_safety("PEDAL", self.MAIN_BUS, {"GAS_PEDAL": min(gas, 2.55)})

  def test_vehicle_speed_measurements(self):
    # WHEELSPEED_FL is 12 bits: 4095 counts is 77.9 m/s of road speed, so stop short of the
    # 80 m/s the common test uses.
    self._common_measurement_test(self._speed_msg, 0, 77, 1, self.safety.get_vehicle_speed_min, self.safety.get_vehicle_speed_max)

  # ---- focused cases for what the param selects

  def _wheelspeed_counts(self, fl_counts):
    # Set WHEELSPEED_FL so the raw CAN value equals fl_counts (physical = counts * scale).
    return self.packer.make_can_msg_safety("WHEELSPEED_CLEAN", self.MAIN_BUS,
                                           {"WHEELSPEED_FL": fl_counts * self.DBC_WHEEL_SCALE})

  def test_speed_uses_road_scale(self):
    # Panda must report road speed = raw * (DBC scale * wheelSpeedFactor), matching
    # the controller's vEgo, not the raw car (speedometer) speed. Compare against the
    # Atto 3 path (0.1 kph/LSB) so VEHICLE_SPEED_FACTOR and KPH_TO_MS cancel; the
    # ratio pins byd.h's constant to DBC scale * wheelSpeedFactor and breaks if either
    # the DBC factor or interface.py's wheelSpeedFactor is retuned without byd.h.
    counts = 1000
    self.safety.set_safety_hooks(CarParams.SafetyModel.byd, 0)  # Atto 3: 0.1 kph/LSB
    self.safety.init_tests()
    for _ in range(common.MAX_SAMPLE_VALS):  # flush the rolling speed window
      self._rx(self._wheelspeed_counts(counts))
    s_atto3 = self.safety.get_vehicle_speed_min()

    self.safety.set_safety_hooks(CarParams.SafetyModel.byd, self.SAFETY_PARAM)
    self.safety.init_tests()
    for _ in range(common.MAX_SAMPLE_VALS):  # flush the rolling speed window
      self._rx(self._wheelspeed_counts(counts))
    s_song_plus = self.safety.get_vehicle_speed_min()

    expected_ratio = (self.DBC_WHEEL_SCALE * self.WHEEL_SPEED_FACTOR) / 0.1
    self.assertAlmostEqual(s_song_plus / s_atto3, expected_ratio, places=4)

  def test_speed_reads_only_the_12_bit_wheel_field(self):
    # bits 12-15 of 0x1F0 are not part of WHEELSPEED_FL; a 16-bit read would add 4096 counts per bit
    for _ in range(common.MAX_SAMPLE_VALS):
      self.assertTrue(self._rx(self._wheelspeed_counts(1000)))
    expected = (self.safety.get_vehicle_speed_min(), self.safety.get_vehicle_speed_max())
    for _ in range(common.MAX_SAMPLE_VALS):
      msg = self._wheelspeed_counts(1000)
      msg[0].data[1] |= 0xF0
      self.assertTrue(self._rx(self._fix_checksum(msg)))
    self.assertEqual(expected, (self.safety.get_vehicle_speed_min(), self.safety.get_vehicle_speed_max()))

  def test_brake_ignores_drive_state(self):
    # On the Song Plus, DRIVE_STATE (0x242) brake is unreliable and must be ignored.
    self._rx(self._user_brake_msg(0))
    self.assertFalse(self.safety.get_brake_pressed_prev())
    self._rx(self.packer.make_can_msg_safety("DRIVE_STATE", self.MAIN_BUS, {"BRAKE_PRESSED": 1}))
    self.assertFalse(self.safety.get_brake_pressed_prev())

  def test_gas_ignores_drive_state_throttle(self):
    # RAW_THROTTLE is also non-zero while the stock ACC accelerates, with the pedal untouched
    self._rx(self._user_gas_msg(0))
    self._rx(self.packer.make_can_msg_safety("DRIVE_STATE", self.MAIN_BUS, {"RAW_THROTTLE": 50}))
    self._rx(self._user_gas_msg(0))
    self.assertFalse(self.safety.get_gas_pressed_prev())

  def test_gas_threshold_matches_carstate(self):
    # carstate.py: PEDAL.GAS_PEDAL > 0.03, i.e. more than 3 counts
    for counts, pressed in ((0, False), (3, False), (4, True), (255, True), (3, False)):
      self._rx(self.packer.make_can_msg_safety("PEDAL", self.MAIN_BUS, {"GAS_PEDAL": counts * 0.01}))
      self.assertEqual(pressed, self.safety.get_gas_pressed_prev(), counts)

  def test_cruise_engaged_needs_the_acc_on_and_its_state(self):
    # carstate.py's rule: the follow-distance setting (SET_DISTANCE, bits 10-12) non-zero
    # and ACC_STATE_CODE 3 (controlling), 5 (driver on the gas) or 7 (holding at
    # standstill). Every combination, each one entered from "not engaged" so that engaging
    # is seen as well.
    for distance, code in itertools.product(range(8), range(16)):
      with self.subTest(set_distance=distance, acc_state_code=code):
        self._rx(self._pcm_status_msg(False))
        self.assertFalse(self.safety.get_controls_allowed())
        self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"SET_DISTANCE": distance, "ACC_STATE_CODE": code}))
        engaged = distance != 0 and code in (3, 5, 7)
        self.assertEqual(engaged, self.safety.get_cruise_engaged_prev())
        self.assertEqual(engaged, self.safety.get_controls_allowed())

  def test_cruise_stays_engaged_through_the_distance_key(self):
    # 2026-11-15, route 00000033: controlling, the distance key twice, 2 -> 3 -> 4. At 4
    # (0b100) bits 10-11 read 0; the ACC went on controlling and so must controls.
    self._rx(self._pcm_status_msg(True))
    self.assertTrue(self.safety.get_controls_allowed())
    for distance in (2, 3, 4, 4):
      self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"SET_DISTANCE": distance, "ACC_STATE_CODE": 3}))
      self.assertTrue(self.safety.get_cruise_engaged_prev(), distance)
      self.assertTrue(self.safety.get_controls_allowed(), distance)
    # then the driver turned the ACC off
    self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"SET_DISTANCE": 0, "ACC_STATE_CODE": 1}))
    self.assertFalse(self.safety.get_cruise_engaged_prev())
    self.assertFalse(self.safety.get_controls_allowed())

  def test_cruise_ends_when_the_acc_starts_exiting(self):
    # State 4: the ACC is on its way out and still shows the follow distance. Seen in the
    # car for 2.5 s; openpilot has let go by then, and so must the panda.
    self._rx(self._pcm_status_msg(True))
    self.assertTrue(self.safety.get_controls_allowed())
    self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_ENGAGED_STATE": 2, "ACC_STATE_CODE": 4}))
    self.assertFalse(self.safety.get_cruise_engaged_prev())
    self.assertFalse(self.safety.get_controls_allowed())

  def test_cruise_ignores_the_atto3_acc_state_field(self):
    # bits 19-21 are constant 0 on this car; if they ever were not, they must not engage
    for acc_state in (3, 5):
      for values in ({"ACC_ENGAGED_STATE": 0, "ACC_STATE_CODE": 3}, {"ACC_ENGAGED_STATE": 2, "ACC_STATE_CODE": 2}):
        self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_STATE": acc_state, **values}))
        self.assertFalse(self.safety.get_cruise_engaged_prev())
        self.assertFalse(self.safety.get_controls_allowed())

  def test_acc_cmd_is_not_openpilots_to_send_without_a_level(self):
    # Without a longitudinal level ACC_CMD is not a TX message at all: not even a frame
    # that would pass every check of TestBydSongPlusLongSafety gets out, and the stock
    # frame is always forwarded.
    if (self.SAFETY_PARAM >> 1) & 3:
      raise unittest.SkipTest("this class has a longitudinal level")
    self._rx(self._pcm_status_msg(True))
    self.assertTrue(self.safety.get_controls_allowed())
    stock = {"ACCEL_CMD": 0, "STOCK_BYTES_1_4": 0x438C6766, "STOCK_BYTE_5": 0xD8, "SET_ME_XF": 0xF}
    self._rx(self.packer.make_can_msg_safety("ACC_CMD", self.CAM_BUS, {**stock, "COUNTER": 4}))
    self.assertFalse(self._tx(self.packer.make_can_msg_safety("ACC_CMD", self.MAIN_BUS, {**stock, "ACCEL_CMD": -10, "COUNTER": 5})))
    for _ in range(3):
      self.assertEqual(0, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))


class AccCmdRule:
  """byd.h's rule for ACC_CMD written out a second time, for test_against_the_rule_written_out.

  It keeps only what byd.h keeps for this rule. Whether controls are allowed, the gas pedal,
  the relay state and the vehicle speed are the common safety code's and are handed in.
  Raising is written the way DESIGN.txt states it (longitudinal-l6-20261114), in exact numbers.
  """
  MIN_REQUEST = {1: 80, 2: 60, 3: 30}
  MAX_AGE_US = 25000
  SUPPLIED_MAX_AGE_US = 100000
  WARNING_HOLD_US = 2000000
  LEAD_HOLD_US = 2000000
  RAISE_ABOVE_STOCK = 10      # counts, +0.5 m/s^2
  RAISE_MAX = 124             # +1.2 m/s^2
  RAISE_STEP = 1              # counts over what the car got in the slot just past

  def __init__(self, level, may_raise=False, now=0):
    self.level = level
    self.may_raise = may_raise
    self.stock = (bytes(6), 0, 0)   # (bytes 0-5, counter, time of arrival); all zero until a frame has come
    self.supplied = None         # when openpilot's frame for the next slot was sent
    self.arrivals = 0            # stock frames that reached the forward decision
    self.saved_arrival = 0       # ... when the frame in self.stock arrived
    self.state_code = 0
    self.warning_ts = None
    self.lead_ts = now           # when the stock radar last showed something ahead; the mode starts as if just then
    self.set_speed = 0           # counts of 0.5 km/h on the speedometer
    self.forwarded = False       # the newest stock frame went to the car
    self.ref = None              # (the request the car got in a slot, that slot's arrival count)

  @staticmethod
  def _elapsed(now, then):
    return (now - then) & 0xFFFFFFFF   # the panda's timer is 32 bits of microseconds

  def stock_acc_cmd(self, payload, counter, now):
    """A stock frame passed the RX checks (after forward())."""
    self.stock = (bytes(payload), counter, now)
    self.saved_arrival = self.arrivals
    if self.forwarded:
      self.ref = (payload[0], self.arrivals)

  def stock_acc_hud(self, state_code, warning, now, lead=False, set_speed=0):
    self.state_code = state_code
    if warning:
      self.warning_ts = now
    elif self.warning_ts is not None and self._elapsed(now, self.warning_ts) >= self.WARNING_HOLD_US:
      self.warning_ts = None
    if lead:
      self.lead_ts = now
    elif self.lead_ts is not None and self._elapsed(now, self.lead_ts) >= self.LEAD_HOLD_US:
      self.lead_ts = None
    self.set_speed = set_speed

  def forward(self, now):
    """A stock ACC_CMD arrives: is it withheld?"""
    withheld = self.supplied is not None and self._elapsed(now, self.supplied) <= self.SUPPLIED_MAX_AGE_US
    self.supplied = None
    self.arrivals += 1
    self.forwarded = not withheld
    return withheld

  def raise_ok(self, request, stock_request, speed_min_mm, speed_max_mm):
    """Raising: the stock ACC not braking, nothing ahead for two seconds, moving faster than 3 m/s and,
    on the speedometer's scale (road speed / 0.9453), slower than the set speed; at most 10 counts
    above the stock request and 124, and one above what the car got in the slot just past."""
    speedometer_kph = Fraction(speed_max_mm, 1000) * Fraction(36, 10) / Fraction(9453, 10000)
    return (self.may_raise and stock_request >= 100 and self.lead_ts is None and
            speed_min_mm > 3000 and speedometer_kph < Fraction(self.set_speed, 2) and
            request <= stock_request + self.RAISE_ABOVE_STOCK and request <= self.RAISE_MAX and
            self.ref is not None and self.ref[1] == self.arrivals and request <= self.ref[0] + self.RAISE_STEP)

  def may_send(self, dat, now, longitudinal_allowed, relay_malfunction, speed_min_mm=0, speed_max_mm=0):
    ok = longitudinal_allowed and not relay_malfunction and self.supplied is None and self.saved_arrival == self.arrivals
    if ok:
      payload, counter, ts = self.stock
      ok = self._elapsed(now, ts) <= self.MAX_AGE_US
      ok = ok and self.state_code == 3 and self.warning_ts is None
      ok = ok and not payload[4] & 0x80 and payload[5] == 0xD8
      ok = ok and dat[6] == 0xF0 | ((counter + 1) & 0xF)
      ok = ok and dat[7] == (0xFF - sum(dat[:7])) & 0xFF
      ok = ok and bytes(dat[1:6]) == payload[1:6]
      lower = self.MIN_REQUEST[self.level] <= dat[0] < payload[0]
      higher = dat[0] > payload[0] and self.raise_ok(dat[0], payload[0], speed_min_mm, speed_max_mm)
      ok = ok and (lower or higher)
    if ok:
      self.supplied = now
      self.ref = (dat[0], self.arrivals + 1)
    return ok


class TestBydSongPlusLongSafety(TestBydSongPlusSafety):
  """The Song Plus suite once more with a longitudinal level, then what the level adds.

  With a level, openpilot may send ACC_CMD - but only a copy of the stock frame that has just
  arrived on bus 2, numbered as the next one, with a request that is lower than the stock one
  and not below the level's floor, and only while the stock ACC is controlling with nothing
  unusual about it. Sending one withholds exactly one stock frame, the next. Everything else
  about the three ACC frames is as without a level: ACC_HUD_ADAS and 0x32F are always forwarded
  and never openpilot's to send. See the top of byd.h.
  """

  LONG_LEVEL = 1
  SAFETY_PARAM = 1 | (LONG_LEVEL << 1)   # BYD_PARAM_SONG_PLUS and the level in bits 1-2
  MIN_REQUEST = AccCmdRule.MIN_REQUEST[LONG_LEVEL]

  TX_MSGS = [[STEERING_MODULE_ADAS, 0], [LKAS_HUD_ADAS, 0], [PCM_BUTTONS, 0], [ACC_CMD, 0]]
  RELAY_MALFUNCTION_ADDRS = {0: (STEERING_MODULE_ADAS, LKAS_HUD_ADAS, ACC_CMD)}
  # FWD_BLACKLISTED_ADDRS stays as it is: a stock ACC_CMD is forwarded unless it has been replaced.

  RX_CHECKED = TestBydSongPlusSafety.RX_CHECKED + (("ACC_CMD", 2, "ACCEL_CMD", 0, -20),)

  # Bytes 0-5 of a stock frame as recorded with the ACC controlling at 60 km/h or more:
  # request 0, comfort band 102/103, jerk limits 12/67, bit 31 set, bit 39 clear, byte 5 0xD8.
  STOCK = bytes([100, 0x66, 0x67, 0x8C, 0x43, 0xD8])
  T0 = 5000000           # us; when the scenario's first stock frame arrives
  FRAME_US = 20000       # the stock frames are 20 ms apart
  REPLY_US = 12000       # openpilot's frame comes this long after the stock frame it copies

  # ---- building blocks

  def _acc_cmd(self, bus, payload, counter, set_me_xf=0xF):
    # through the DBC's raw-copy signals, the way bydcan.py copies a stock frame
    values = {"ACCEL_CMD": payload[0] - 100, "STOCK_BYTES_1_4": int.from_bytes(payload[1:5], "little"),
              "STOCK_BYTE_5": payload[5], "SET_ME_XF": set_me_xf, "COUNTER": counter % 16}
    return self.packer.make_can_msg_safety("ACC_CMD", bus, values)

  def _acc_hud(self, state_code=3, shown=2, warning_bits=(), set_speed=0, lead=0, bars=0):
    # set_speed in counts of 0.5 km/h; lead and bars: HAS_LEAD (bit 9) and LEAD_DISTANCE (bits 13-15)
    values = {"ACC_ENGAGED_STATE": shown, "ACC_STATE_CODE": state_code, "SET_SPEED": set_speed * 0.5, "HAS_LEAD": lead, "LEAD_DISTANCE": bars}
    values.update({f"WARNING_BIT_{bit}": 1 for bit in warning_bits})
    return self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, values)

  def _stock_frame(self, counter, payload=STOCK):
    """A stock ACC_CMD arrives. As in the firmware: the forward decision first, then the RX hook.

    Returns True if the frame was withheld from the car.
    """
    withheld = self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD) == -1
    self.assertTrue(self._rx(self._acc_cmd(self.CAM_BUS, payload, counter)))
    return withheld

  def _controlling(self, payload=STOCK, counter=3, t0=None):
    """The stock ACC is controlling, controls are allowed, and stock frame `counter` arrives at T0 (or t0)."""
    t0 = self.T0 if t0 is None else t0
    for c in range(counter - 4, counter):      # a run of frames, so that the counter check has settled
      self.safety.set_timer(t0 - (counter - c) * self.FRAME_US)
      self._rx(self._acc_cmd(self.CAM_BUS, payload, c))
    self.safety.set_timer(t0)
    self.assertTrue(self._rx(self._acc_hud()))
    self.assertTrue(self.safety.get_controls_allowed())
    self.assertFalse(self._stock_frame(counter, payload))
    self.safety.set_timer(t0 + self.REPLY_US)

  def _lowered(self, request, payload=STOCK):
    return bytes([request]) + payload[1:]

  def _assert_refused(self, msg, why="", stock=None):
    self.assertFalse(self._tx(msg), why)
    # and so the stock frame that follows, the one with the refused frame's counter, is not withheld
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD), why)
    self._rx(self._acc_cmd(self.CAM_BUS, self.STOCK if stock is None else stock, msg[0].data[6] & 0xF))

  # ---- what is allowed

  def test_lowered_copy_takes_the_place_of_the_next_stock_frame(self):
    self._controlling(counter=3)
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    # nothing else is held back, before or after
    for addr in (ACC_HUD_ADAS, ACC_AEB, STEERING_MODULE_ADAS + 1):
      self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, addr))
    self.assertEqual(self.CAM_BUS, self.safety.safety_fwd_hook(self.MAIN_BUS, ACC_CMD))
    # the next stock ACC_CMD is, and only that one
    self.assertEqual(-1, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_HUD_ADAS))
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_AEB))

  def test_frame_after_frame_and_back_to_stock_at_once(self):
    # 40 frames in a row, across the counter's rollover; then openpilot simply stops
    self._controlling(counter=3)
    t = self.T0
    for k in range(4, 44):
      self.safety.set_timer(t + self.REPLY_US)
      self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(99 - (k % 20)), k)), k)
      t += self.FRAME_US
      self.safety.set_timer(t)
      self.assertTrue(self._rx(self._acc_hud()))
      self.assertTrue(self._stock_frame(k), k)       # withheld: the car has openpilot's frame k
    t += self.FRAME_US
    self.safety.set_timer(t)
    self.assertFalse(self._stock_frame(44))          # nothing sent for it: forwarded
    self.assertFalse(self._stock_frame(45))

  def test_request_between_the_floor_and_the_stock_request(self):
    # every request value, against several stock requests and at every level
    for level, floor in AccCmdRule.MIN_REQUEST.items():
      for stock_request in (0, 29, 30, 59, 60, 79, 80, 81, 100, 101, 123, 255):
        self.safety.set_safety_hooks(CarParams.SafetyModel.byd, 1 | (level << 1) | (self.SAFETY_PARAM & 8))
        self.safety.init_tests()
        stock = self._lowered(stock_request)
        self._controlling(payload=stock, counter=3)
        t, k = self.T0, 3
        for request in range(256):
          with self.subTest(level=level, stock_request=stock_request, request=request):
            self.safety.set_timer(t + self.REPLY_US)
            allowed = floor <= request < stock_request
            self.assertEqual(allowed, self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(request, stock), k + 1)))
            t, k = t + self.FRAME_US, k + 1
            self.safety.set_timer(t)
            self.assertEqual(allowed, self._stock_frame(k, stock))

  def test_plain_copy_is_refused(self):
    # A frame that asks for what the stock ACC asks for would only make the stock frame
    # late: openpilot has nothing to say then, and the stock frame goes through itself.
    self._controlling(counter=3)
    self._assert_refused(self._acc_cmd(self.MAIN_BUS, self.STOCK, 4))
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(99), 5)))   # a copy of stock frame 4, lowered

  def test_withheld_frame_is_still_the_one_to_copy(self):
    # A withheld stock frame goes through the RX hook like any other, so the next
    # replacement has to be a copy of it and not of the last frame the car saw.
    self._controlling(counter=3)
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    newer = bytes([104, 0x66, 0x66, 0x8D, 0x42, 0xD8])         # the stock ACC moved on: request, band and jerk limits
    self.safety.set_timer(self.T0 + self.FRAME_US)
    self.assertTrue(self._rx(self._acc_hud()))
    self.assertTrue(self._stock_frame(4, newer))                # withheld
    self.safety.set_timer(self.T0 + self.FRAME_US + self.REPLY_US)
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 5)))            # frame 3 with a lower request
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(103, newer), 5)))    # frame 4 with a lower request

  def test_mark_does_not_wait_for_ever(self):
    # openpilot's frame went out and then the stock frames stopped. Whatever comes back
    # later is not the frame that was replaced, and passes.
    for pause, withheld in ((8000, True), (20000, True), (100000, True), (100001, False), (5000000, False)):
      with self.subTest(pause_us=pause):
        self._reset_safety_hooks()
        self._controlling(counter=3)
        self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
        self.safety.set_timer(self.T0 + self.REPLY_US + pause)
        self.assertEqual(-1 if withheld else self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
        self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))

  # ---- what is not

  def test_only_one_frame_per_stock_frame(self):
    self._controlling(counter=3)
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 5)))
    self.assertEqual(-1, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))

  def test_needs_controls_allowed(self):
    self._controlling(counter=3)
    self.safety.set_controls_allowed(False)
    self._assert_refused(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4))

  def test_not_while_the_driver_is_on_the_gas(self):
    self._controlling(counter=3)
    self._rx(self._user_gas_msg(1))
    self.assertTrue(self.safety.get_controls_allowed())     # the gas pedal does not end the engagement
    self._assert_refused(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4))
    self._rx(self._user_gas_msg(0))
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 5)))   # the stock frame 4 went through meanwhile

  def test_not_after_the_brake(self):
    self._controlling(counter=3)
    self._rx(self._speed_msg(10))
    self._rx(self._user_brake_msg(1))
    self._assert_refused(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4))

  def test_needs_a_stock_frame(self):
    # controls allowed, but no stock ACC_CMD has been seen since the mode was set
    self._rx(self._acc_hud())
    self.assertTrue(self.safety.get_controls_allowed())
    self._assert_refused(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4))

  def test_stock_frame_must_be_fresh(self):
    for age, allowed in ((0, True), (19999, True), (25000, True), (25001, False), (40000, False), (10000000, False)):
      with self.subTest(age_us=age):
        self._reset_safety_hooks()
        self._controlling(counter=3)
        self.safety.set_timer(self.T0 + age)
        self.assertEqual(allowed, self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))

  def test_late_frame_meets_the_next_stock_frame_and_is_refused(self):
    # openpilot's frame for slot 4 arrives after stock frame 4 has: the counter no longer fits
    self._controlling(counter=3)
    self.safety.set_timer(self.T0 + self.FRAME_US)
    self.assertFalse(self._stock_frame(4))
    self.safety.set_timer(self.T0 + self.FRAME_US + 1000)
    self._assert_refused(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4))

  def test_counter_must_be_the_next_one(self):
    for stock_counter in range(16):
      for counter in range(16):
        with self.subTest(stock_counter=stock_counter, counter=counter):
          self._reset_safety_hooks()
          self._controlling(counter=stock_counter)
          allowed = counter == (stock_counter + 1) % 16
          self.assertEqual(allowed, self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), counter)))

  def test_byte_6_high_nibble_and_checksum(self):
    for nibble in range(15):
      self._reset_safety_hooks()
      self._controlling(counter=3)
      self._assert_refused(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4, set_me_xf=nibble), nibble)
    for wrong in range(1, 256):
      self._reset_safety_hooks()
      self._controlling(counter=3)
      msg = self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)
      msg[0].data[7] ^= wrong
      self._assert_refused(msg, wrong)

  def test_bytes_1_to_5_must_be_the_stock_frames(self):
    # each of the 40 bits on its own, and each byte replaced by each value the vendor's stack sent
    for byte, bit in itertools.product(range(1, 6), range(8)):
      with self.subTest(byte=byte, bit=bit):
        self._reset_safety_hooks()
        self._controlling(counter=3)
        changed = bytearray(self._lowered(90))
        changed[byte] ^= 1 << bit
        self._assert_refused(self._acc_cmd(self.MAIN_BUS, bytes(changed), 4))
    # what the stack this car came with sent while it did the longitudinal control
    # (comfort band 100/101, jerk limits 41/79, byte 5 0x58): not a copy, so refused
    self._reset_safety_hooks()
    self._controlling(counter=3)
    self._assert_refused(self._acc_cmd(self.MAIN_BUS, bytes([90, 0x64, 0x65, 0xA9, 0x4F, 0x58]), 4))

  def test_stock_frame_must_look_like_controlling(self):
    # bit 39, each single bit of byte 5, and byte 5 as recorded in the other ACC states
    odd = [bytes([*self.STOCK[:4], self.STOCK[4] | 0x80, 0xD8])]
    odd += [bytes([*self.STOCK[:5], 0xD8 ^ (1 << bit)]) for bit in range(8)]
    odd += [bytes([*self.STOCK[:5], byte_5]) for byte_5 in (0xE8, 0xD0, 0x48, 0x20, 0xF0, 0x00, 0xFF)]
    odd += [bytes([0, 0x66, 0x66, 0x99, 0xB7, 0xF0])]     # holding at standstill, as recorded
    for stock in odd:
      with self.subTest(stock=stock.hex()):
        self._reset_safety_hooks()
        self._controlling(payload=stock, counter=3)
        self._assert_refused(self._acc_cmd(self.MAIN_BUS, stock, 4), stock=stock)                  # not even a plain copy
        self._reset_safety_hooks()
        self._controlling(payload=stock, counter=3)
        self._assert_refused(self._acc_cmd(self.MAIN_BUS, bytes([max(stock[0] - 5, 0)]) + stock[1:], 4), stock=stock)

  def test_only_while_the_stock_acc_is_controlling(self):
    for state_code in range(16):
      with self.subTest(state_code=state_code):
        self._reset_safety_hooks()
        self._controlling(counter=3)
        self._rx(self._acc_hud(state_code=state_code))
        # 5 and 7 are still "engaged"; for the others, allow controls by hand so that it is
        # the state itself that is seen to refuse the frame
        self.safety.set_controls_allowed(True)
        self.assertEqual(state_code == 3, self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))

  def test_not_with_a_warning_bit_nor_for_two_seconds_after(self):
    for bit in (16, 17, 23):
      with self.subTest(bit=bit):
        self._reset_safety_hooks()
        self._controlling(counter=3)
        k, t = 3, self.T0
        self.safety.set_timer(t)
        self._rx(self._acc_hud(warning_bits=(bit,)))
        self.assertTrue(self.safety.get_controls_allowed())
        self.safety.set_timer(t + self.REPLY_US)
        self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), k + 1)))
        # the bit is gone from the next frame on; frames keep coming every 20 ms, and the
        # first one that arrives two seconds after the warning frame ends the hold
        for n in range(1, 106):
          k, t = k + 1, t + self.FRAME_US
          self.safety.set_timer(t)
          self._rx(self._acc_hud())
          self.assertFalse(self._stock_frame(k))
          self.safety.set_timer(t + self.REPLY_US)
          allowed = n * self.FRAME_US >= 2000000
          self.assertEqual(allowed, self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), k + 1)), n)
          if allowed:
            break
        self.assertEqual(100, n)

  def test_warning_hold_ends_at_two_seconds_exactly(self):
    # the hold ends with the first ACC_HUD_ADAS frame that arrives 2,000,000 us or more after the last warning frame
    for after_us, held in ((1999999, True), (2000000, False), (2000001, False)):
      with self.subTest(after_us=after_us):
        self._reset_safety_hooks()
        self._controlling(counter=3)
        self.safety.set_timer(self.T0)
        self._rx(self._acc_hud(warning_bits=(17,)))
        t = self.T0 + after_us
        self.safety.set_timer(t)
        self._rx(self._acc_hud())
        self.assertFalse(self._stock_frame(4))
        self.safety.set_timer(t + self.REPLY_US)
        self.assertEqual(not held, self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 5)))

  def test_setting_the_mode_again_forgets_everything(self):
    # the stock frame: controls are allowed again and the ACC is controlling, but there is no frame to copy yet
    self._controlling(counter=3)
    self._reset_safety_hooks()
    self.safety.set_timer(self.T0 + self.REPLY_US)
    self._rx(self._acc_hud())
    self.assertTrue(self.safety.get_controls_allowed())
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    # ... not even with the timer and the counter where a just-set mode has them
    self._reset_safety_hooks()
    self._controlling(counter=3)
    self._reset_safety_hooks()
    self.safety.set_timer(self.REPLY_US)
    self._rx(self._acc_hud())
    self.assertTrue(self.safety.get_controls_allowed())
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 1)))

    # the ACC's state: a stock frame arrives after the reset, but nothing has said "controlling" since
    self._reset_safety_hooks()
    self._controlling(counter=3)
    self._reset_safety_hooks()
    for c in range(8, 13):
      self._rx(self._acc_cmd(self.CAM_BUS, self.STOCK, c))
    self.safety.set_controls_allowed(True)
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 13)))

    # a warning: it was showing before the reset, and is not held against the frames after it,
    # whatever the timer reads then (one second later: still inside the hold, had it been kept)
    for t0 in (self.T0 + 1000000, 200000):
      self._reset_safety_hooks()
      self._controlling(counter=3)
      self._rx(self._acc_hud(warning_bits=(16,)))
      self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
      self._reset_safety_hooks()
      self._controlling(counter=3, t0=t0)
      self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)), t0)

  def test_warning_hold_restarts_with_every_warning_frame(self):
    self._controlling(counter=3)
    k, t = 3, self.T0
    for n in range(1, 251):                       # 5 s of frames; the warning shows for the first 3 s
      k, t = k + 1, t + self.FRAME_US
      self.safety.set_timer(t)
      self._rx(self._acc_hud(warning_bits=(17,) if n <= 150 else ()))
      self._stock_frame(k)
      self.safety.set_timer(t + self.REPLY_US)
      allowed = n >= 250
      self.assertEqual(allowed, self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), k + 1)), n)

  def test_mark_is_cleared_by_a_frame_the_rx_checks_reject(self):
    # the withheld frame's place was taken; whatever arrives next must pass again, valid or not
    self._controlling(counter=3)
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    self.assertEqual(-1, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
    bad = self._acc_cmd(self.CAM_BUS, self.STOCK, 4)
    bad[0].data[7] ^= 0xFF
    self.assertFalse(self._rx(bad))
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
    # and a rejected stock frame is not one to copy: byd.h never saw it
    self.safety.set_controls_allowed(True)
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 5)))
    # nor may the frame before it be copied again: that copy would carry counter 4, which the car has
    # had already (openpilot's first frame), still inside the 25 ms of stock frame 3 (a0's review)
    self.safety.set_timer(self.T0 + self.REPLY_US + 2000)
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    # the next valid stock frame makes copying possible again
    self.safety.set_timer(self.T0 + 2 * self.FRAME_US)
    self.assertFalse(self._stock_frame(5))
    self.safety.set_timer(self.T0 + 2 * self.FRAME_US + self.REPLY_US)
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 6)))

  def test_a_frame_the_rx_checks_never_see_stops_copying_until_the_next(self):
    # A 0x32E on bus 2 that is not the stock frame's shape (here 7 bytes long) reaches the forward
    # decision but no RX check, so it changes neither the saved stock frame nor whether controls
    # are allowed. The frame saved before it may no longer be copied: the newest to arrive is not it.
    self._controlling(counter=3)
    self.safety.set_timer(self.T0 + 10000)
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
    self._rx(common.make_msg(self.CAM_BUS, ACC_CMD, 7))
    self.assertTrue(self.safety.get_controls_allowed())
    self.safety.set_timer(self.T0 + 12000)
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    # the stock frame 4 arrives and may be copied again
    self.safety.set_timer(self.T0 + self.FRAME_US)
    self.assertFalse(self._stock_frame(4))
    self.safety.set_timer(self.T0 + self.FRAME_US + self.REPLY_US)
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 5)))

  def test_a_stock_frame_with_a_bad_checksum_ends_the_engagement(self):
    # not this rule: any RX-checked frame that fails its checks clears controls_allowed (safety.h
    # is_msg_valid), so nothing is replaced until the stock ACC engages again
    self._controlling(counter=3)
    self.safety.set_timer(self.T0 + self.FRAME_US)
    bad = self._acc_cmd(self.CAM_BUS, self.STOCK, 4)
    bad[0].data[7] ^= 0x01
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
    self.assertFalse(self._rx(bad))
    self.assertFalse(self.safety.get_controls_allowed())
    self.safety.set_timer(self.T0 + self.FRAME_US + 1000)
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 5)))

  def test_no_other_acc_frame_is_openpilots_to_send(self):
    self._controlling(counter=3)
    for addr in (ACC_HUD_ADAS, ACC_AEB):
      for bus in range(3):
        self.assertFalse(self._tx(common.make_msg(bus, addr, 8)))
    for bus in (1, 2):
      msg = self._acc_cmd(bus, self._lowered(90), 4)
      self.assertFalse(self._tx(msg))

  def test_stock_acc_cmd_on_the_cars_bus_is_a_relay_malfunction(self):
    self._controlling(counter=3)
    self._rx(self._acc_cmd(self.MAIN_BUS, self.STOCK, 4))
    self.assertTrue(self.safety.get_relay_malfunction())
    self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(90), 4)))
    self.assertEqual(-1, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))

  def test_against_the_rule_written_out(self):
    """Random traffic and random frames from openpilot, every decision compared with AccCmdRule."""
    rng = random.Random(20261114)
    raise_bit = self.SAFETY_PARAM & 8
    for level in (1, 2, 3):
      now = 0xFFFFFFFF - 3000000          # the 32-bit timer rolls over three seconds in
      self.safety.set_timer(now)          # the mode is set at `now`, which raising starts from
      self.safety.set_safety_hooks(CarParams.SafetyModel.byd, 1 | (level << 1) | raise_bit)
      self.safety.init_tests()
      rule = AccCmdRule(level, bool(raise_bit), now)
      counter, hud_counter = 0, 0
      stock = self.STOCK
      sent = allowed_n = raised_n = raised_allowed_n = 0
      # the stock request, the set speed and the car's speed each stay put for a while and then jump,
      # so that the conditions for raising hold for stretches, and fail on either side of each edge
      stock_request, set_speed, speed_counts = 100, 120, 525
      for step in range(40000):
        # mostly a few ms between events, so that openpilot's frames are usually in time;
        # now and then long enough for a stock frame to go stale or a hold to end
        now = (now + rng.choice((500, 1000, 2000, 3000, 5000, 8000, 12000, 20000, 26000) if rng.random() < 0.995 else (60000, 2500000))) & 0xFFFFFFFF
        self.safety.set_timer(now)
        what = rng.random()
        if what < 0.30:
          # a stock ACC_CMD: mostly the usual frame with a wandering request, sometimes something else
          counter = (counter + rng.choice((1,) * 30 + (2, 0))) % 16
          stock = bytearray(self.STOCK)
          if rng.random() < 0.1:
            stock_request = rng.choice((100, 100, 100, 96, 104, 110, 85, 70, 40, 20, 0, 130, 99, 101, 114, 118, 124))
          stock[0] = stock_request
          if rng.random() < 0.05:
            stock[rng.randrange(1, 6)] ^= 1 << rng.randrange(8)
          msg = self._acc_cmd(self.CAM_BUS, bytes(stock), counter)
          if rng.random() < 0.01:
            msg[0].data[7] ^= 0x10
          self.assertEqual(rule.forward(now), self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD) == -1, step)
          if self._rx(msg):
            rule.stock_acc_cmd(stock, counter, now)
        elif what < 0.45:
          state_code = rng.choice((3,) * 40 + (5, 7, 2, 1, 4))
          shown = rng.choice((2,) * 20 + (1, 0))
          bits = tuple(b for b in (16, 17, 23) if rng.random() < 0.0015)
          # now and then the stock radar shows something; the set speed around the speeds below
          lead, bars = rng.choice(((1, 0), (0, 1), (1, 3), (0, 4))) if rng.random() < 0.003 else (0, 0)
          if rng.random() < 0.02:
            set_speed = rng.choice((0, 20, 58, 120, 120, 120, 120, 203, 255))
          values = {"ACC_ENGAGED_STATE": shown, "ACC_STATE_CODE": state_code, "COUNTER": hud_counter,
                    "SET_SPEED": set_speed * 0.5, "HAS_LEAD": lead, "LEAD_DISTANCE": bars}
          values.update({f"WARNING_BIT_{b}": 1 for b in bits})
          hud_counter = (hud_counter + 1) % 16
          if self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, values)):
            rule.stock_acc_hud(state_code, bool(bits), now, bool(lead or bars), set_speed)
        elif what < 0.47:
          self._rx(self._user_gas_msg(rng.choice((0, 0, 0, 0, 1))))
        elif what < 0.48:
          # rarer than the others: with the car moving, every press ends the engagement
          self._rx(self._user_brake_msg(rng.random() < 0.05))
        elif what < 0.49:
          self.safety.set_controls_allowed(rng.random() < 0.9)
        elif what < 0.55:
          # the wheel speed, on both sides of 3 m/s and of the set speeds above
          if rng.random() < 0.03:
            speed_counts = rng.choice((0, 100, 157, 158, 399, 400, 525, 525, 525, 525, 525, 827, 828, 1400, 1401, 2000))
          self._rx(self._wheelspeed_counts(speed_counts))
        else:
          # openpilot offers a frame: usually the right one, often with one thing wrong; its request
          # around the stock one, or around one step above what the car got last
          dat = bytearray(stock) + bytes([0xF0 | ((counter + 1) % 16), 0])
          if rule.ref is not None and rng.random() < 0.5:
            dat[0] = max(0, min(255, rule.ref[0] + rng.choice((1, 1, 1, 1, 2, 0, -1, 5))))
          else:
            dat[0] = max(0, min(255, stock[0] - rng.choice((1, 1, 1, 3, 10, 25, 50, 0, -1, -5, -10, -11))))
          wrong = rng.random()
          if wrong < 0.06:
            dat[rng.randrange(1, 6)] ^= 1 << rng.randrange(8)
          elif wrong < 0.10:
            dat[6] = 0xF0 | rng.randrange(16)
          elif wrong < 0.12:
            dat[6] ^= 1 << rng.randrange(4, 8)
          dat[7] = (0xFF - sum(dat[:7])) & 0xFF
          if 0.12 <= wrong < 0.14:
            dat[7] ^= 1 << rng.randrange(8)
          raised = dat[0] > rule.stock[0][0]
          speeds = (round(self.safety.get_vehicle_speed_min() * 1000), round(self.safety.get_vehicle_speed_max() * 1000))
          expected = rule.may_send(dat, now, self.safety.get_controls_allowed() and not self.safety.get_gas_pressed_prev(),
                                   self.safety.get_relay_malfunction(), *speeds)
          self.assertEqual(expected, self._tx(common.make_msg(self.MAIN_BUS, ACC_CMD, 8, bytes(dat))), (level, step, bytes(dat).hex()))
          sent += 1
          allowed_n += expected
          raised_n += raised
          raised_allowed_n += raised and expected
      # the comparison means something only if each answer came up often
      self.assertGreater(allowed_n, 800, (level, sent))
      self.assertGreater(sent - allowed_n, 5000, (level, sent))
      self.assertGreater(raised_n - raised_allowed_n, 1000, (level, raised_n))
      if raise_bit:
        self.assertGreater(raised_allowed_n, 100, (level, raised_n))
      else:
        self.assertEqual(raised_allowed_n, 0, level)


class TestBydSongPlusLongLevel2Safety(TestBydSongPlusLongSafety):
  LONG_LEVEL = 2
  SAFETY_PARAM = 1 | (LONG_LEVEL << 1)
  MIN_REQUEST = AccCmdRule.MIN_REQUEST[LONG_LEVEL]


class TestBydSongPlusLongLevel3Safety(TestBydSongPlusLongSafety):
  LONG_LEVEL = 3
  SAFETY_PARAM = 1 | (LONG_LEVEL << 1)
  MIN_REQUEST = AccCmdRule.MIN_REQUEST[LONG_LEVEL]


class TestBydSongPlusLongRaiseSafety(TestBydSongPlusLongSafety):
  """The Song Plus suites again with raising allowed (safety param bit 3), then what raising adds.

  Everything in the longitudinal suite above still holds with the bit: none of its scenarios is a
  free road (the car is not moving in them), and off a free road the rule is the same with or
  without it. Here: a request above the stock one, and only on a free road - the stock ACC not
  braking, its radar showing nothing ahead for two seconds, the car moving and below the set speed -
  no more than 10 counts above the stock request nor above 124, and no more than one count above
  what the car got in the slot just past. See "Raising" at the top of byd.h.
  """

  SAFETY_PARAM = 1 | (1 << 1) | 8
  SET_SPEED = 120            # counts of 0.5 km/h: 60 km/h on the speedometer
  SPEED_COUNTS = 525         # WHEELSPEED_FL: 10.0 m/s of road speed, 38 km/h on the speedometer

  def setUp(self):
    super().setUp()
    # set the mode again with the timer at 0: what the mode starts with ("something was just
    # ahead") then dates from a known time, whatever the test before left the timer at
    self._new_mode()

  def _new_mode(self, param=None, t=0):
    self.safety.set_timer(t)
    self.safety.set_safety_hooks(CarParams.SafetyModel.byd, self.SAFETY_PARAM if param is None else param)
    self.safety.init_tests()      # as setUp does (it also puts the timer back to 0)
    self.safety.set_timer(t)

  def _road(self, counts=None, set_speed=None, lead=0, bars=0, hud=None):
    """The frames of a 20 ms slot besides the stock ACC_CMD: the wheel speed and ACC_HUD_ADAS."""
    self.assertTrue(self._rx(self._wheelspeed_counts(self.SPEED_COUNTS if counts is None else counts)))
    if hud is None:
      hud = self._acc_hud(set_speed=self.SET_SPEED if set_speed is None else set_speed, lead=lead, bars=bars)
    self.assertTrue(self._rx(hud))

  def _free_road(self, request=100, counter=3, t0=None):
    """2.2 s of a free road, the stock ACC controlling and asking for `request`, nothing from openpilot.
    Ends like _controlling: stock frame `counter` arrived at t0 (default T0) and went to the car, and
    the timer is REPLY_US past it."""
    self.t = self.T0 if t0 is None else t0
    self.k = counter
    self.stock = self._lowered(request)
    for n in range(110, -1, -1):
      self.safety.set_timer(self.t - n * self.FRAME_US)
      self._road()
      self.assertFalse(self._stock_frame(counter - n, self.stock))
    self.assertTrue(self.safety.get_controls_allowed())
    self.safety.set_timer(self.t + self.REPLY_US)

  def _slot(self, request=None, stock_request=None, **road):
    """openpilot sends `request` for the next slot, REPLY_US after the newest stock frame (None: nothing),
    then that slot's frames arrive, 20 ms after the stock frame before; the stock ACC_CMD asks for
    `stock_request` from then on (default: as before). Returns whether openpilot's frame was taken
    (None when there was none); a frame that was taken stands in for that slot's stock frame."""
    taken = None
    if request is not None:
      self.safety.set_timer(self.t + self.REPLY_US)
      taken = self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(request, self.stock), self.k + 1))
    self.t += self.FRAME_US
    self.k += 1
    if stock_request is not None:
      self.stock = self._lowered(stock_request, self.stock)
    self.safety.set_timer(self.t)
    self._road(**road)
    self.assertEqual(bool(taken), self._stock_frame(self.k, self.stock))
    return taken

  def _hud_byte_1(self, bits):
    """ACC_HUD_ADAS of a free road with `bits` set in byte 1 as well (bit 8 of the frame is bit 0 there)."""
    msg = self._acc_hud(set_speed=self.SET_SPEED)
    msg[0].data[1] |= bits
    return self._fix_checksum(msg)

  # ---- what is allowed

  def test_raised_copy_takes_the_place_of_the_next_stock_frame(self):
    self._free_road(request=100)
    self.assertTrue(self._slot(101))
    self.assertIsNone(self._slot())                 # and the next stock frame goes to the car again
    self.assertTrue(self._slot(101))
    # a raised frame is the stock frame with another request, like a lowered one
    self.safety.set_timer(self.t + self.REPLY_US)
    sent = self._acc_cmd(self.MAIN_BUS, self._lowered(102, self.stock), self.k + 1)
    self.assertEqual(bytes([102]) + self.STOCK[1:], bytes(sent[0].data[i] for i in range(6)))
    self.assertTrue(self._tx(sent))

  def test_climbs_one_count_per_frame_to_ten_above_the_stock_request(self):
    self._free_road(request=104)
    for request in range(105, 115):
      self.assertTrue(self._slot(request), request)
    self.assertFalse(self._slot(115))               # 11 above the stock request
    self.assertFalse(self._slot(114))               # the stock frame went instead: the car got 104 last
    self.assertTrue(self._slot(105))
    self.assertTrue(self._slot(104 + 2))

  def test_at_most_one_count_above_what_the_car_got_last(self):
    self._free_road(request=100)
    self.assertFalse(self._slot(102))               # the car got 100
    self.assertTrue(self._slot(101))                # it still got 100 (the stock frame took the refused frame's slot)
    self.assertTrue(self._slot(102))
    self.assertTrue(self._slot(102))                # the same again
    self.assertFalse(self._slot(104))
    self.assertTrue(self._slot(101))                # the car got 100, the stock frame
    self.assertTrue(self._slot(102))
    self.assertTrue(self._slot(101))                # down by any amount, while still above the stock request
    self.assertTrue(self._slot(99))                 # or below it: the rule for a lower request
    self.assertFalse(self._slot(101))               # the car got 99: 101 is two above it
    self.assertTrue(self._slot(100 - 2))
    self.assertFalse(self._slot(100))               # the stock request itself is never sent
    self.assertTrue(self._slot(101))                # the car got 100 last

  def test_every_request_against_the_stock_one_and_the_last(self):
    # the car got `last` in the slot just past (climbing from the stock request), the stock frame
    # to copy asks for `stock_request`; every request from 0 to 255 in turn, each from the same start
    for stock_request, last, climb_from in ((100, 100, 100), (100, 110, 100), (104, 110, 104), (110, 114, 104),
                                            (114, 124, 114), (118, 124, 114), (99, 105, 100), (124, 124, 124), (130, 124, 114)):
      for request in [*range(stock_request - 3, 132), 0, 79, 80, 255]:
        with self.subTest(stock_request=stock_request, last=last, request=request):
          self._new_mode()
          self._free_road(request=climb_from)
          for step in range(climb_from + 1, last):
            self.assertTrue(self._slot(step))
          # the last step of the climb, in the slot whose stock frame asks for stock_request
          self.assertEqual(last != climb_from, bool(self._slot(last if last != climb_from else None, stock_request=stock_request)))
          lower = 80 <= request < stock_request
          higher = (stock_request >= 100 and stock_request < request <= min(stock_request + 10, 124, last + 1))
          self.assertEqual(lower or higher, self._slot(request))

  # ---- what is not

  def test_not_while_the_stock_acc_brakes(self):
    for stock_request in (0, 50, 80, 98, 99, 100, 101):
      with self.subTest(stock_request=stock_request):
        self._new_mode()
        self._free_road(request=stock_request)
        self.assertEqual(stock_request >= 100, self._slot(stock_request + 1))

  def test_a_raise_ends_the_moment_the_stock_acc_brakes(self):
    self._free_road(request=100)
    for request in range(101, 106):
      self.assertTrue(self._slot(request))
    self.assertTrue(self._slot(105, stock_request=99))   # the stock frame ours stands in for asks for -0.05 m/s^2
    self.assertFalse(self._slot(100))
    self.assertTrue(self._slot(98))                       # lower is lower
    self.assertFalse(self._slot(99 + 1, stock_request=100))
    self.assertTrue(self._slot(101))                      # the stock ACC is back at 0, and the car got that

  def test_the_cap_follows_the_stock_request_down(self):
    for first in (111, 112):
      with self.subTest(first=first):
        self._new_mode()
        self._free_road(request=104)
        for request in range(105, 115):
          self.assertTrue(self._slot(request))
        self.assertTrue(self._slot(114, stock_request=101))   # the frame ours stands in for asks for 101
        self.assertEqual(first == 111, self._slot(first))     # 101 + 10 at most
        if first == 112:
          self.assertFalse(self._slot(111))                   # the stock frame went instead: the car got 101 last
          self.assertTrue(self._slot(102))

  def test_never_above_124(self):
    for stock_request in (114, 118, 123, 124, 125, 140):
      with self.subTest(stock_request=stock_request):
        self._new_mode()
        self._free_road(request=stock_request)
        for request in range(stock_request + 1, 125):
          self.assertTrue(self._slot(request))
        self.assertFalse(self._slot(max(125, stock_request + 1)))

  def test_each_lead_field_holds_raising_for_two_seconds(self):
    for lead, bars in ((1, 0), (0, 1), (0, 2), (0, 4), (1, 3)):
      with self.subTest(lead=lead, bars=bars):
        self._new_mode()
        self._free_road(request=100)
        self.assertTrue(self._slot(101))
        self.assertIsNone(self._slot(lead=lead, bars=bars))
        # the radar shows nothing again from the next frame on; frames keep coming every 20 ms,
        # and the first one two seconds after the one that showed something ends the hold
        for n in range(1, 110):
          self.assertEqual(n >= 101, self._slot(101), n)

  def test_each_bit_of_the_lead_fields_on_its_own(self):
    for bit in (9, 13, 14, 15):
      with self.subTest(bit=bit):
        self._new_mode()
        self._free_road(request=100)
        self.assertIsNone(self._slot(hud=self._hud_byte_1(1 << (bit - 8))))
        self.assertFalse(self._slot(101))

  def test_the_other_bits_of_byte_1_are_no_lead(self):
    # bit 8 (above the set speed's 8 bits), 10-11 (the ACC shown, its follow distance), 12
    for bits in (0x01, 0x04, 0x08, 0x0C, 0x10, 0x1D):
      with self.subTest(bits=hex(bits)):
        self._new_mode()
        self._free_road(request=100)
        self.assertIsNone(self._slot(hud=self._hud_byte_1(bits)))
        self.assertTrue(self._slot(101))

  def test_lead_hold_ends_at_two_seconds_exactly(self):
    # the hold ends with the first ACC_HUD_ADAS frame that arrives 2,000,000 us or more after the last one with a lead
    for after_us, held in ((1999999, True), (2000000, False), (2000001, False)):
      with self.subTest(after_us=after_us):
        self._new_mode()
        self._free_road(request=100)
        lead_t = self.t + 5000
        self.safety.set_timer(lead_t)
        self.assertTrue(self._rx(self._acc_hud(set_speed=self.SET_SPEED, lead=1)))
        self.t = lead_t + after_us - self.FRAME_US
        self.assertIsNone(self._slot())
        self.assertEqual(not held, self._slot(101))

  def test_lead_hold_restarts_with_every_lead_frame(self):
    self._free_road(request=100)
    # the radar shows something in the frames of the first 150 slots (3 s); each frame of ours is
    # sent before its slot's frames arrive, so the first one still goes, and then none until two
    # seconds after the last frame that showed something
    for n in range(1, 260):
      self.assertEqual(n == 1 or n >= 251, self._slot(101, lead=1 if n <= 150 else 0), n)

  def test_not_below_three_metres_per_second(self):
    # the lowest of the panda's last six speeds; 157 counts are 2.989 m/s, 158 are 3.008
    for counts, allowed in ((0, False), (100, False), (157, False), (158, True), (159, True)):
      with self.subTest(counts=counts):
        self._new_mode()
        self._free_road(request=100)
        for _ in range(6):
          self.assertIsNone(self._slot(counts=counts))
        self.assertEqual(allowed, self._slot(101, counts=counts))
    # one slow frame among fast ones is enough, for as long as it is one of the last six
    self._new_mode()
    self._free_road(request=100)
    self.assertIsNone(self._slot(counts=157))
    for _ in range(6):
      self.assertFalse(self._slot(101))
    self.assertTrue(self._slot(101))

  def test_only_below_the_set_speed(self):
    # road speed / 0.9453 < set speed, against the highest of the panda's last six speeds. The pairs
    # put the wheel speed one count on each side of the line; for 58 and 203 the line falls within a
    # tenth of a mm/s of a count, which pins the numbers down
    cases = [(58, 399), (58, 400), (203, 1400), (203, 1401), (120, 827), (120, 828), (40, 200), (40, 300),
             (255, 1730), (255, 1740), (20, 158), (0, 158)]
    outcomes = set()
    for set_speed, counts in cases:
      with self.subTest(set_speed=set_speed, counts=counts):
        self._new_mode()
        self._free_road(request=100)
        for _ in range(6):
          self.assertIsNone(self._slot(counts=counts, set_speed=set_speed))
        speed_mm = round(self.safety.get_vehicle_speed_max() * 1000)
        allowed = Fraction(speed_mm, 1000) * Fraction(36, 10) / Fraction(9453, 10000) < Fraction(set_speed, 2)
        self.assertEqual(allowed, self._slot(101, counts=counts, set_speed=set_speed))
        outcomes.add((set_speed, allowed))
    self.assertTrue({(58, True), (58, False), (203, True), (203, False), (120, True), (120, False)} <= outcomes)
    # one fast frame among slow ones is enough, for as long as it is one of the last six
    self._new_mode()
    self._free_road(request=100)
    self.assertIsNone(self._slot(counts=828))
    for _ in range(6):
      self.assertFalse(self._slot(101, counts=827))
    self.assertTrue(self._slot(101, counts=827))

  def test_the_set_speed_is_bits_0_to_7(self):
    # 10 km/h: below the car's 38. Bit 8 next to it does not make it 138
    self._free_road(request=100)
    self.assertIsNone(self._slot(hud=self._fix_checksum(self._with_bit_8(self._acc_hud(set_speed=20)))))
    self.assertFalse(self._slot(101))

  @staticmethod
  def _with_bit_8(msg):
    msg[0].data[1] |= 0x01
    return msg

  def test_a_raised_frame_needs_everything_a_lowered_one_needs(self):
    spoils = {
      "wrong counter": lambda dat: dat.__setitem__(6, 0xF0 | ((dat[6] + 1) & 0xF)),
      "other band": lambda dat: dat.__setitem__(1, dat[1] ^ 0x01),
      "bad checksum": lambda dat: dat.__setitem__(7, dat[7] ^ 0x01),
    }
    for name, spoil in spoils.items():
      with self.subTest(spoil=name):
        self._new_mode()
        self._free_road(request=100)
        msg = self._acc_cmd(self.MAIN_BUS, self._lowered(101, self.stock), self.k + 1)
        dat = bytearray(msg[0].data[i] for i in range(8))
        spoil(dat)
        if name != "bad checksum":
          dat[7] = (0xFF - sum(dat[:7])) & 0xFF
        self.assertFalse(self._tx(common.make_msg(self.MAIN_BUS, ACC_CMD, 8, bytes(dat))))
    for case in ("gas", "controls", "state 5", "warning", "late"):
      with self.subTest(case=case):
        self._new_mode()
        self._free_road(request=100)
        if case == "gas":
          self._rx(self._user_gas_msg(1))
        elif case == "controls":
          self.safety.set_controls_allowed(False)
        elif case == "state 5":
          self._rx(self._acc_hud(state_code=5, set_speed=self.SET_SPEED))
          self.assertTrue(self.safety.get_controls_allowed())
        elif case == "warning":
          self._rx(self._acc_hud(warning_bits=(17,), set_speed=self.SET_SPEED))
        else:
          self.safety.set_timer(self.t + 25001)
        self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(101, self.stock), self.k + 1)))

  def test_the_withheld_stock_frame_is_not_what_the_car_got(self):
    # ours took the slot; the stock frame for it asks for 104, but what the car got there is ours
    self._free_road(request=100)
    self.assertTrue(self._slot(101, stock_request=104))
    self.assertFalse(self._slot(105))              # one above the withheld frame's 104, but four above the 101 the car got
    self.assertTrue(self._slot(105))               # that refused frame's slot went to the stock frame: the car got 104 last

  def test_a_forwarded_stock_frame_is_what_the_car_got(self):
    # ours went out, the stock frames paused, and the next one came after the mark had run out, so it
    # went to the car as well: the car got that one last
    self._free_road(request=100)
    for request in range(101, 106):
      self.assertTrue(self._slot(request))
    self.safety.set_timer(self.t + self.REPLY_US)
    self.assertTrue(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(106, self.stock), self.k + 1)))
    self.t += self.REPLY_US + 100001
    self.k += 1
    self.safety.set_timer(self.t)
    self._road()
    self.assertFalse(self._stock_frame(self.k, self.stock))
    self.assertFalse(self._slot(107))              # one above ours, two..seven above what the car got last
    self.assertFalse(self._slot(102))
    self.assertTrue(self._slot(101))

  def test_after_a_frame_the_rx_checks_never_see(self):
    # a 0x32E on bus 2 of another length: nothing may be copied until the next stock frame, which is
    # then what the car got last, whatever ours had been
    self._free_road(request=100)
    for request in range(101, 104):
      self.assertTrue(self._slot(request))
    self.safety.set_timer(self.t + 10000)
    self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
    self._rx(common.make_msg(self.CAM_BUS, ACC_CMD, 7))
    self.assertFalse(self._slot(104))
    self.assertFalse(self._slot(102))
    self.assertTrue(self._slot(101))

  def test_nothing_is_raised_for_two_seconds_after_the_mode_is_set(self):
    # the mode is set again on a free road; the stock ACC is engaged again at once, and lowering works
    # again with the first stock frame, but raising waits for two seconds of frames with nothing ahead
    self._free_road(request=100)
    self.assertTrue(self._slot(101))
    self._new_mode(t=self.t + 5000)
    for n in range(1, 110):
      self.assertEqual(n >= 102, self._slot(101), n)
    self._new_mode(t=self.t + 5000)
    self.assertIsNone(self._slot())
    self.assertTrue(self._slot(99))

  def test_without_the_raise_bit_a_free_road_changes_nothing(self):
    for param in (self.SAFETY_PARAM & ~8, 1 | (2 << 1), 1 | (3 << 1)):
      with self.subTest(param=param):
        self._new_mode(param)
        self._free_road(request=100)
        self.assertFalse(self._slot(101))
        self.assertTrue(self._slot(99))

  def test_the_raise_bit_needs_a_level(self):
    # without a level ACC_CMD is no TX message at all, raise bit or not, Song Plus or not
    for param in (1 | 8, 8):
      with self.subTest(param=param):
        self._new_mode(param)
        self.safety.set_controls_allowed(True)
        for c in range(4):
          self.assertEqual(self.MAIN_BUS, self.safety.safety_fwd_hook(self.CAM_BUS, ACC_CMD))
          self._rx(self._acc_cmd(self.CAM_BUS, self.STOCK, c))
        self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(101), 4)))
        self.assertFalse(self._tx(self._acc_cmd(self.MAIN_BUS, self._lowered(99), 4)))


class TestBydAtto3IgnoresSongPlusFields(unittest.TestCase):
  """Without the Song Plus param, none of that car's sources may leak into the Atto 3 path."""

  def setUp(self):
    self.safety = libsafety_py.libsafety
    self.packer = common.CANPackerSafety("byd_song_plus")   # only to build that car's frames
    self.safety.set_safety_hooks(CarParams.SafetyModel.byd, 0)
    self.safety.init_tests()

  def test_pedal_message_does_not_set_gas(self):
    self.safety.safety_rx_hook(self.packer.make_can_msg_safety("PEDAL", 0, {"GAS_PEDAL": 1.0}))
    self.assertFalse(self.safety.get_gas_pressed_prev())

  def test_ipb_does_not_set_brake(self):
    self.safety.safety_rx_hook(self.packer.make_can_msg_safety("IPB", 0, {"BRAKE_PRESSED": 1}))
    self.assertFalse(self.safety.get_brake_pressed_prev())

  def test_engaged_state_bits_do_not_engage(self):
    for _ in range(4):
      self.safety.safety_rx_hook(self.packer.make_can_msg_safety("ACC_HUD_ADAS", 2, {"ACC_ENGAGED_STATE": 2, "ACC_STATE_CODE": 3, "ACC_STATE": 0}))
    self.assertFalse(self.safety.get_cruise_engaged_prev())
    self.assertFalse(self.safety.get_controls_allowed())

  def test_longitudinal_level_needs_the_song_plus(self):
    # the level bits mean nothing on their own: ACC_CMD stays out of the TX messages
    for level in (1, 2, 3):
      self.safety.set_safety_hooks(CarParams.SafetyModel.byd, level << 1)
      self.safety.init_tests()
      self.safety.set_controls_allowed(True)
      stock = {"ACCEL_CMD": 0, "STOCK_BYTES_1_4": 0x438C6766, "STOCK_BYTE_5": 0xD8, "SET_ME_XF": 0xF}
      self.safety.safety_rx_hook(self.packer.make_can_msg_safety("ACC_CMD", 2, {**stock, "COUNTER": 4}))
      self.assertFalse(self.safety.safety_tx_hook(self.packer.make_can_msg_safety("ACC_CMD", 0, {**stock, "ACCEL_CMD": -10, "COUNTER": 5})))
      self.assertEqual(0, self.safety.safety_fwd_hook(2, ACC_CMD))
      # nor is it a message whose presence on the car's bus means anything
      self.safety.safety_rx_hook(self.packer.make_can_msg_safety("ACC_CMD", 0, {**stock, "COUNTER": 6}))
      self.assertFalse(self.safety.get_relay_malfunction())


class TestBydSongPlusLatKeySafety(TestBydSongPlusSafety):
  """The Song Plus suite once more with the lateral key, then what the key adds (top of byd.h).

  The owner's rule: the custom key (STEERING_WHEEL_KEYS, one exact frame) is an on/off switch for
  steering and the pedals are the driver's alone. A press allows controls whatever the ACC and
  the brake are doing; while the key allowed them, neither the ACC nor the brake ends them. RX
  checks and openpilot's heartbeat still do, and that is how the second press ends them.
  """

  SAFETY_PARAM = 1 | 8   # BYD_PARAM_SONG_PLUS, BYD_PARAM_LAT_KEY
  KEY_FRAME_US = 100000  # the key frames come every 100 ms while a key is down

  def setUp(self):
    super().setUp()
    self.t = 10_000_000

  def _key(self, code=0x25, page=1, byte2=0x80, rest=0, after_us=None):
    self.t += self.KEY_FRAME_US if after_us is None else after_us
    self.safety.set_timer(self.t)
    values = {"KEY_PAGE": page, "KEY_CODE": code, "KEY_BYTE_2": byte2, "KEY_BYTES_3_7": rest}
    return self._rx(self.packer.make_can_msg_safety("STEERING_WHEEL_KEYS", self.MAIN_BUS, values))

  def _press(self, **kw):
    """a short press: the key frame, then the release frame"""
    self._key(**kw)
    self._key(code=0)

  def _acc(self, shown, code):
    self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_ENGAGED_STATE": shown, "ACC_STATE_CODE": code}))

  def _brake(self, pressed):
    self._rx(self._user_brake_msg(pressed))

  def _start(self, moving=True):
    self._reset_safety_hooks()
    self.t = 10_000_000
    self._rx(self._speed_msg(15 if moving else 0))
    self._acc(0, 2)      # stock ACC in standby
    self._brake(False)
    self.assertFalse(self.safety.get_controls_allowed())

  def test_press_allows_controls_and_acc_off_does_not_end_them(self):
    for standby_code in (1, 2):    # unavailable, standby
      self._start()
      self._key()
      self.assertTrue(self.safety.get_controls_allowed())
      self._key(code=0)
      for _ in range(100):
        self._acc(0, standby_code)
      self.assertTrue(self.safety.get_controls_allowed())
      self.assertTrue(self._tx(self._angle_cmd_msg(0, True)))

  def test_no_press_no_controls(self):
    self._start()
    self._acc(0, 2)
    self.assertFalse(self._tx(self._angle_cmd_msg(0, True)))

  def test_only_the_exact_frame_is_a_press(self):
    for kw in ({"code": 0x26}, {"code": 0x15}, {"code": 0x0C}, {"code": 0x24}, {"page": 2}, {"byte2": 0x00},
               {"rest": 1}, {"rest": 1 << 39}):
      with self.subTest(**kw):
        self._start()
        self._press(**kw)
        self.assertFalse(self.safety.get_controls_allowed())

  def test_brake_does_not_end_it(self):
    for moving in (True, False):
      with self.subTest(moving=moving):
        self._start(moving)
        self._press()
        for _ in range(3):
          self._brake(True)
          for _ in range(20):
            self._brake(True)
            self._acc(0, 2)
          self._brake(False)
        self.assertTrue(self.safety.get_controls_allowed())
        self.assertTrue(self._tx(self._angle_cmd_msg(0, True)))

  def test_press_with_the_brake_held(self):
    self._start()
    self._brake(True)
    self._press()
    self.assertTrue(self.safety.get_controls_allowed())
    self._brake(True)
    self.assertTrue(self.safety.get_controls_allowed())

  def test_brake_still_ends_an_engagement_by_the_acc(self):
    self._start()
    self._acc(1, 3)
    self.assertTrue(self.safety.get_controls_allowed())
    self._brake(True)
    self.assertFalse(self.safety.get_controls_allowed())

  def test_ended_by_the_heartbeat_then_the_brake_rule_is_back(self):
    # openpilot disengaged (second press): its heartbeat ends the controls here ...
    self._start()
    self._press()
    self.safety.set_controls_allowed(False)
    self._acc(0, 2)
    self.assertFalse(self.safety.get_controls_allowed())
    # ... and the key's engagement with them: controls allowed again by the ACC end with the brake
    self._acc(1, 3)
    self.assertTrue(self.safety.get_controls_allowed())
    self._brake(True)
    self.assertFalse(self.safety.get_controls_allowed())

  def test_held_key_is_one_press(self):
    self._start()
    self._key()
    self.assertTrue(self.safety.get_controls_allowed())
    self.safety.set_controls_allowed(False)     # e.g. openpilot was not engaged (heartbeat)
    for _ in range(30):                          # still held, 3 s
      self._key()
      self._acc(0, 2)
    self.assertFalse(self.safety.get_controls_allowed())

  def test_lost_release_frame_and_a_new_press_later(self):
    self._start()
    self._key()
    self.safety.set_controls_allowed(False)
    self._key(after_us=300_000)                  # 300 ms later: still the same press
    self.assertFalse(self.safety.get_controls_allowed())
    self._key(after_us=301_000)                  # more than 300 ms later: a new one
    self.assertTrue(self.safety.get_controls_allowed())

  def test_press_while_the_acc_is_engaged_keeps_steering_after_the_acc(self):
    self._start()
    self._acc(1, 3)
    self.assertTrue(self.safety.get_controls_allowed())
    self._press()
    self._acc(1, 4)       # the ACC on its way out, e.g. after the brake
    self._brake(True)
    for _ in range(10):
      self._acc(0, 2)
    self.assertTrue(self.safety.get_controls_allowed())

  def test_press_with_the_acc_engaged_and_controls_ended(self):
    self._start()
    self._acc(1, 3)
    self.safety.set_controls_allowed(False)
    self._press()
    self.assertTrue(self.safety.get_controls_allowed())

  def test_the_acc_coming_and_going_does_not_end_it(self):
    self._start()
    self._press()
    for code in (3, 5, 3, 4, 2, 1, 3, 4, 2):
      for _ in range(5):
        self._acc(0 if code in (1, 2) else 1, code)
      self.assertTrue(self.safety.get_controls_allowed(), code)

  def test_the_key_never_ends_anything(self):
    self._start()
    self._press()
    for _ in range(3):
      self._press()
    self.assertTrue(self.safety.get_controls_allowed())

  def test_absent_key_frames_are_no_rx_fault(self):
    self._start()
    self._acc(1, 3)
    self.assertTrue(self.safety.get_controls_allowed())
    # 5 s with every periodic message fresh and not one key frame
    for ms in range(0, 5000, 10):
      self.safety.set_timer(self.t + ms * 1000)
      self._rx(self._speed_msg(10))
      self._rx(self._user_brake_msg(False))
      self._rx(self._user_gas_msg(0))
      self._rx(self._angle_meas_msg(0))
      if ms % 20 == 0:
        self._acc(1, 3)
      if ms % 1000 == 0:
        self.safety.safety_tick()
    self.assertTrue(self.safety.get_controls_allowed())

  def test_rx_fault_still_ends_it(self):
    self._start()
    self._press()
    self.safety.set_timer(self.t + 3_000_000)    # nothing for 3 s
    self.safety.safety_tick()
    self.assertFalse(self.safety.get_controls_allowed())

  def test_exemption_is_cleared_by_any_mode_change(self):
    for model in (CarParams.SafetyModel.toyota, CarParams.SafetyModel.hyundai, CarParams.SafetyModel.noOutput,
                  CarParams.SafetyModel.byd):
      with self.subTest(model=model):
        self.safety.set_safety_hooks(self.SAFETY_MODEL, self.SAFETY_PARAM)   # back from the last one
        self._start()
        self._press()
        self._brake(True)
        self.assertTrue(self.safety.get_brake_keeps_controls())
        self.safety.set_safety_hooks(model, 0)
        self.assertFalse(self.safety.get_brake_keeps_controls())

  def test_brake_rule_is_back_after_a_mode_change(self):
    self._start()
    self._press()
    self._brake(True)            # leaves the common flag set
    self._reset_safety_hooks()
    self._rx(self._speed_msg(15))
    self._brake(False)
    self.safety.set_controls_allowed(True)
    self._brake(True)
    self.assertFalse(self.safety.get_controls_allowed())

  def test_needs_its_param_bit_and_the_song_plus(self):
    for param in (1, 8, 1 | 2):
      with self.subTest(param=param):
        self.safety.set_safety_hooks(CarParams.SafetyModel.byd, param)
        self.safety.init_tests()
        self._acc(0, 2)
        self._press()
        self.assertFalse(self.safety.get_controls_allowed())


class TestBydSongPlusLongLatKeySafety(TestBydSongPlusLongSafety):
  """The longitudinal suite with the lateral key bit as well; and no ACC_CMD while engaged by the key."""

  SAFETY_PARAM = TestBydSongPlusLongSafety.SAFETY_PARAM | 8

  def test_no_acc_cmd_while_engaged_by_the_key(self):
    self._reset_safety_hooks()
    self.safety.set_timer(self.T0)
    self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_ENGAGED_STATE": 0, "ACC_STATE_CODE": 2}))
    self.safety.set_timer(self.T0 + 1000)
    self._rx(self.packer.make_can_msg_safety("STEERING_WHEEL_KEYS", self.MAIN_BUS, {"KEY_PAGE": 1, "KEY_CODE": 0x25, "KEY_BYTE_2": 0x80}))
    self.assertTrue(self.safety.get_controls_allowed())
    # an ACC frame claiming "controlling" without the distance shown leaves acc_on false
    self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_ENGAGED_STATE": 0, "ACC_STATE_CODE": 3}))
    self.assertTrue(self.safety.get_controls_allowed())
    self.safety.set_timer(self.T0 + 2000)
    self._rx(self._acc_cmd(2, self.STOCK, 4))
    self.safety.set_timer(self.T0 + 2000 + self.REPLY_US)
    lowered = bytes([self.STOCK[0] - 5]) + self.STOCK[1:]
    self.assertFalse(self._tx(self._acc_cmd(0, lowered, 5)))


if __name__ == "__main__":
  unittest.main()
