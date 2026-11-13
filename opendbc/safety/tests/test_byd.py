#!/usr/bin/env python3
import itertools
import unittest
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

  def setUp(self):
    super().setUp()
    self.VM = VehicleModel(get_safety_CP())

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
            msg = self.packer.make_can_msg_safety(name, bus, {signal: initial})
            self.assertTrue(self._rx(msg))
          speed_min = self.safety.get_vehicle_speed_min()
          speed_max = self.safety.get_vehicle_speed_max()
          msg = self.packer.make_can_msg_safety(name, bus, {signal: corrupt})
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
          msg = self.packer.make_can_msg_safety(name, bus, {signal: value})
          self.assertTrue(self._rx(msg))
        self.safety.set_controls_allowed(True)
        # Replayed frames are rejected after the common counter tolerance.
        for i in range(common.MAX_WRONG_COUNTERS + 1):
          should_rx = i < common.MAX_WRONG_COUNTERS - 1
          self.assertEqual(should_rx, self._rx(msg))
          self.assertEqual(should_rx, self.safety.get_controls_allowed())
        # A valid sequence clears the counter faults.
        for _ in range(common.MAX_WRONG_COUNTERS):
          msg = self.packer.make_can_msg_safety(name, bus, {signal: 0})
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
  speed, brake comes from IPB, gas from PEDAL, and cruise-engaged from ACC_ENGAGED_STATE
  (bits 10-11 of ACC_HUD_ADAS). Each was fitted to recordings of the car and each is exercised
  here both through the common suite and by the focused cases at the end.

  Steering geometry is still the Atto 3's for every BYD (see byd_tx_hook), so the angle-limit
  tests run with the same vehicle model; only the speed they are fed is encoded differently.
  """

  DBC = "byd_song_plus"
  SAFETY_PARAM = 1   # BYD_PARAM_SONG_PLUS

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

  def _pcm_status_msg(self, enable):
    # ACC_ENGAGED_STATE: 0 = not engaged, 1 and 2 = engaged
    return self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_ENGAGED_STATE": 2 if enable else 0})

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

  def test_cruise_engaged_from_engaged_state_bits(self):
    for state, engaged in ((0, False), (1, True), (2, True), (3, True), (0, False)):
      self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_ENGAGED_STATE": state}))
      self.assertEqual(engaged, self.safety.get_cruise_engaged_prev(), state)
      self.assertEqual(engaged, self.safety.get_controls_allowed(), state)

  def test_cruise_ignores_the_atto3_acc_state_field(self):
    # bits 19-21 are constant 0 on this car; if they ever were not, they must not engage
    for acc_state in (3, 5):
      self._rx(self.packer.make_can_msg_safety("ACC_HUD_ADAS", self.CAM_BUS, {"ACC_STATE": acc_state, "ACC_ENGAGED_STATE": 0}))
      self.assertFalse(self.safety.get_cruise_engaged_prev())
      self.assertFalse(self.safety.get_controls_allowed())


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
      self.safety.safety_rx_hook(self.packer.make_can_msg_safety("ACC_HUD_ADAS", 2, {"ACC_ENGAGED_STATE": 2, "ACC_STATE": 0}))
    self.assertFalse(self.safety.get_cruise_engaged_prev())
    self.assertFalse(self.safety.get_controls_allowed())


if __name__ == "__main__":
  unittest.main()
