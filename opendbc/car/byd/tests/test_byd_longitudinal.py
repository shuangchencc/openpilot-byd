"""The Song Plus when openpilot does the longitudinal control (alpha_long).

What is checked here is the CarController's side of SongPlusLongitudinal (values.py): which frame it
builds and when. And, by running it next to the real safety code fed the way the firmware feeds it,
that with the safety code in this tree at level 0 - which is the firmware in the car before the
longitudinal rule is flashed - nothing of it reaches the car. That the rule takes every frame the
controller builds is in test_byd_longitudinal_panda.py; the rule itself is tested in
opendbc/safety/tests/test_byd.py.
"""
import unittest
from unittest.mock import patch

from opendbc.can.packer import CANPacker
from opendbc.car import Bus, gen_empty_fingerprint, structs
from opendbc.car.can_definitions import CanData
from opendbc.car.car_helpers import interfaces
from opendbc.car.byd.values import CAR, DBC, SongPlusLongitudinal as Long
from opendbc.safety.tests.libsafety import libsafety_py

SONG_PLUS = CAR.BYD_SONG_PLUS_DMI_23
ACC_HUD_ADAS, ACC_CMD, ACC_AEB = 0x32D, 0x32E, 0x32F
MAIN_BUS, CAM_BUS = 0, 2

# Bytes 0-5 of a stock ACC_CMD as recorded with the ACC controlling: request 0 (raw 100),
# comfort band 102/103, jerk limits 12/67, bit 31 set, bit 39 clear, byte 5 0xD8.
STOCK = bytes([100, 0x66, 0x67, 0x8C, 0x43, 0xD8])
# More of them, as recorded (migration-audit/longitudinal-l0-20261114/out/a10-single-frame-content.txt):
# other requests, band 102/102, jerk limits 13/66, 14/65, 15/64, 16/63, 20/59, 21/58, 22/57.
RECORDED = ["6166668d42d8", "6966668c43d8", "6866678c43d8", "6e66678c43d8", "5b66678c43d8", "5c66668d42d8", "6466668e41d8",
            "6a66668f40d8", "636666903fd8", "6f6666943bd8", "6f6666943cd8", "616666953ad8", "6166669639d8"]


def checksum(dat):
  return (0xFF - sum(dat[:7])) & 0xFF


def get_params(platform=SONG_PLUS, alpha_long=True):
  return interfaces[platform].get_params(platform, gen_empty_fingerprint(), [], alpha_long, False, False)


class Car:
  """The stock ACC's frames in, the CarController's frames out, one 10 ms step at a time."""

  def __init__(self, alpha_long=True, platform=SONG_PLUS):
    self.CP = get_params(platform, alpha_long)
    self.CI = interfaces[platform](self.CP)
    self.packer = CANPacker(DBC[platform][Bus.pt])
    self.nanos = 1_000_000_000
    self.counter = 0            # of the stock ACC_CMD sent last
    self.hud_counter = 0
    self.stock = STOCK
    self.state_code = 3
    self.shown = 2
    self.warning = ()
    self.gas = 0.0
    self.set_speed = 60.0       # km/h, the stock ACC's set speed, on the speedometer
    self.speed = 40.0           # km/h, the wheels as the car reports them (the speedometer's scale)
    self.lead = 0               # the stock radar: HAS_LEAD ...
    self.bars = 0               # ... and LEAD_DISTANCE
    self.lead_visible = False   # openpilot sees a lead (hudControl.leadVisible)
    self.CS = None

  def stock_frames(self):
    """The two ACC frames the ADAS controller sends every 20 ms, as CanData from bus 2."""
    self.counter = (self.counter + 1) % 16
    self.hud_counter = (self.hud_counter + 1) % 16
    hud = {"ACC_ENGAGED_STATE": self.shown, "ACC_STATE_CODE": self.state_code, "SET_SPEED": self.set_speed, "COUNTER": self.hud_counter,
           "HAS_LEAD": self.lead, "LEAD_DISTANCE": self.bars}
    hud.update({f"WARNING_BIT_{bit}": 1 for bit in self.warning})
    cmd = {"ACCEL_CMD": self.stock[0] - 100, "STOCK_BYTES_1_4": int.from_bytes(self.stock[1:5], "little"),
           "STOCK_BYTE_5": self.stock[5], "SET_ME_XF": 0xF, "COUNTER": self.counter}
    out = []
    for name, values in (("ACC_HUD_ADAS", hud), ("ACC_CMD", cmd)):
      addr, dat, _ = self.packer.make_can_msg(name, CAM_BUS, values)
      out.append(CanData(addr, bytes(dat), CAM_BUS))
    return out

  def pedal_frame(self):
    addr, dat, _ = self.packer.make_can_msg("PEDAL", MAIN_BUS, {"GAS_PEDAL": self.gas})
    return CanData(addr, bytes(dat), MAIN_BUS)

  def wheel_frame(self):
    addr, dat, _ = self.packer.make_can_msg("WHEELSPEED_CLEAN", MAIN_BUS, {f"WHEELSPEED_{w}": self.speed for w in ("FL", "FR", "BL", "BR")})
    return CanData(addr, bytes(dat), MAIN_BUS)

  def step(self, accel, new_stock_frame=True, long_active=None, earlier=()):
    """One step: CAN in (with a new pair of stock frames or without), CarState, CarController.
    `earlier` are frames that arrived before this step's own and have not been read yet.

    Returns (the frames that came in, the ACC_CMD frames the controller built as bytes, what it
    reports as the request the car gets)."""
    frames = [*earlier] + (self.stock_frames() if new_stock_frame else []) + [self.pedal_frame(), self.wheel_frame()]
    self.CS = self.CI.update([(self.nanos, frames)])
    CC = structs.CarControl()
    CC.enabled = bool(self.CS.cruiseState.enabled)
    # what controlsd makes of it: no longitudinal while the driver is on the gas
    CC.longActive = (CC.enabled and not self.CS.gasPressed and self.CP.openpilotLongitudinalControl) if long_active is None else long_active
    CC.actuators.accel = accel
    CC.hudControl.leadVisible = self.lead_visible
    out, can_sends = self.CI.apply(CC.as_reader(), self.nanos)
    self.nanos += 10_000_000
    return frames, [bytes(dat) for addr, dat, bus in can_sends if addr == ACC_CMD and bus == MAIN_BUS], out.accel

  def drive(self, accel, steps, **kwargs):
    """`steps` steps with a new stock frame in every second one; returns every ACC_CMD built."""
    sent = []
    for i in range(steps):
      sent += self.step(accel, new_stock_frame=i % 2 == 0, **kwargs)[1]
    return sent


class TestSongPlusLongitudinalParams(unittest.TestCase):
  def test_without_alpha_long_nothing_changes(self):
    CP = get_params(alpha_long=False)
    assert not CP.openpilotLongitudinalControl
    assert CP.safetyConfigs[0].safetyParam == 1
    assert CP.pcmCruise and not CP.alphaLongitudinalAvailable

  def test_with_alpha_long(self):
    CP = get_params(alpha_long=True)
    assert CP.openpilotLongitudinalControl
    # the stock ACC still engages and disengages, and there is still no toggle for this
    assert CP.pcmCruise and not CP.alphaLongitudinalAvailable
    assert CP.dashcamOnly
    assert CP.safetyConfigs[0].safetyParam == 1 | (Long.PANDA_LEVEL << 1) | (Long.PANDA_RAISE_BIT if Long.RAISE else 0)
    assert abs(CP.longitudinalActuatorDelay - 0.3) < 1e-6

  def test_raising_switch_and_its_numbers(self):
    # RAISE is switched on together with a panda firmware that has the rule, on the day it is driven
    assert Long.PANDA_RAISE_BIT == 8
    assert (Long.RAISE_MAX_ABOVE_STOCK, Long.RAISE_MAX_REQUEST, Long.RAISE_MAX_STEP, Long.RAISE_MIN_SPEED) == (10, 124, 1, 3.0)
    assert 0 < Long.RAISE_ABOVE_STOCK_USED <= Long.RAISE_MAX_ABOVE_STOCK
    with patch.object(Long, "RAISE", True):
      assert get_params().safetyConfigs[0].safetyParam == 1 | (Long.PANDA_LEVEL << 1) | 8
      assert get_params(alpha_long=False).safetyConfigs[0].safetyParam == 1

  def test_the_atto3_has_no_longitudinal(self):
    CP = get_params(CAR.BYD_ATTO_3, alpha_long=True)
    assert not CP.openpilotLongitudinalControl
    assert CP.safetyConfigs[0].safetyParam == 0

  def test_accel_limits_are_the_levels(self):
    CI = interfaces[SONG_PLUS]
    low, high = CI.get_pid_accel_limits(get_params(), 20.0, 30.0)
    assert low == (Long.MIN_REQUEST[Long.LEVEL] - 100) * 0.05 and high == 2.0
    assert Long.MIN_REQUEST == {1: 80, 2: 60, 3: 30}
    assert Long.PANDA_LEVEL in (0, Long.LEVEL)


class TestSongPlusLongitudinalController(unittest.TestCase):
  """Lowering. Raising is off here whatever values.py says; it has its own tests below."""

  def setUp(self):
    patcher = patch.object(Long, "RAISE", False)
    patcher.start()
    self.addCleanup(patcher.stop)

  def test_nothing_without_alpha_long(self):
    car = Car(alpha_long=False)
    assert car.drive(-0.5, 100, long_active=True) == []

  def test_lower_request_is_sent_once_per_stock_frame(self):
    car = Car()
    car.drive(0.0, 10)                                  # the stock ACC engages; openpilot wants what it wants
    seen = []
    for i in range(40):
      new = i % 2 == 0
      _, sent, accel = car.step(-0.5, new_stock_frame=new)
      seen.append(len(sent))
      if new:
        # the stock frame that has just arrived, the request lowered to -0.5 m/s^2, numbered as the next one
        expected = bytearray([90, *STOCK[1:], 0xF0 | ((car.counter + 1) % 16), 0])
        expected[7] = checksum(expected)
        assert sent == [bytes(expected)], (i, sent)
      assert abs(accel - -0.5) < 1e-6
    assert seen == [1, 0] * 20

  def test_two_stock_frames_in_one_step_get_one_answer(self):
    # card was late: two stock frames are waiting. The newer one is answered, once.
    car = Car()
    car.drive(0.0, 10)
    unread = car.stock_frames()
    _, sent, _ = car.step(-0.5, earlier=unread)
    assert len(sent) == 1 and sent[0][6] == 0xF0 | ((car.counter + 1) % 16)

  def test_nothing_when_the_stock_acc_wants_no_more(self):
    car = Car()
    car.drive(0.0, 10)
    for accel, stock_request in ((0.0, 100), (0.3, 100), (-0.5, 90), (-0.5, 80), (2.0, 123), (-0.02, 100)):
      car.stock = bytes([stock_request, *STOCK[1:]])
      _, sent, reported = car.step(accel)
      assert sent == [], (accel, stock_request)
      # what the car is asked for is then the stock ACC's own request
      assert abs(reported - (stock_request - 100) * 0.05) < 1e-6

  def test_request_is_rounded_to_the_nearest_count(self):
    car = Car()
    car.drive(0.0, 10)
    for accel, raw in ((-0.03, 99), (-0.07, 99), (-0.08, 98), (-0.5, 90), (-0.524, 90), (-0.526, 89), (-0.99, 80)):
      _, sent, reported = car.step(accel)
      assert sent and sent[0][0] == raw, (accel, sent)
      assert abs(reported - (raw - 100) * 0.05) < 1e-6
      car.step(accel, new_stock_frame=False)

  def test_never_below_the_levels_floor(self):
    floor = Long.MIN_REQUEST[Long.LEVEL]
    car = Car()
    car.drive(0.0, 10)
    for accel in (Long.ACCEL_MIN, Long.ACCEL_MIN - 0.01, -3.5, -10.0):
      _, sent, reported = car.step(accel)
      assert sent and sent[0][0] == floor, (accel, sent)
      assert abs(reported - (floor - 100) * 0.05) < 1e-6
      car.step(accel, new_stock_frame=False)
    # and when the stock ACC itself asks for that much or more, its frame is the one that goes
    for stock_request in (floor, floor - 1, 40, 0):
      car.stock = bytes([stock_request, *STOCK[1:]])
      assert car.step(-10.0)[1] == []
      car.step(-10.0, new_stock_frame=False)

  def test_nothing_unless_long_active(self):
    car = Car()
    car.drive(0.0, 10)
    assert car.drive(-0.5, 20, long_active=False) == []
    assert len(car.drive(-0.5, 20, long_active=True)) == 10
    # and what is reported as the car's request is then the stock ACC's own
    car.stock = bytes([96, *STOCK[1:]])
    _, sent, reported = car.step(-0.5, long_active=False)
    assert sent == [] and abs(reported - -0.2) < 1e-6

  def test_never_more_than_the_stock_acc_without_raising(self):
    # a free road and openpilot wanting more: without RAISE the stock frames go to the car
    car = Car()
    car.drive(0.0, 300)
    for accel in (0.06, 0.5, 2.0):
      assert car.drive(accel, 40) == [], accel
      _, sent, reported = car.step(accel)
      assert sent == [] and reported == 0.0

  def test_a_request_that_is_not_a_number_sends_nothing(self):
    # controlsd replaces these with 0 before they get here; should one slip through, the
    # stock frame goes to the car and the controller does not fall over
    car = Car()
    car.drive(0.0, 10)
    for accel in (float("nan"), float("inf"), float("-inf")):
      _, sent, reported = car.step(accel)
      assert sent == [] and reported == 0.0, accel
      car.step(accel, new_stock_frame=False)

  def test_nothing_on_the_gas(self):
    car = Car()
    car.drive(0.0, 10)
    car.gas = 0.5
    assert car.drive(-0.5, 20) == []
    car.gas = 0.0
    assert len(car.drive(-0.5, 20)) == 10

  def test_only_while_the_stock_acc_is_controlling(self):
    for state_code in (1, 2, 4, 5, 7):
      car = Car()
      car.drive(0.0, 10)
      car.state_code = state_code
      assert car.drive(-0.5, 20, long_active=True) == [], state_code

  def test_only_a_stock_frame_that_looks_like_controlling(self):
    odd = [bytes([*STOCK[:4], STOCK[4] | 0x80, 0xD8])]                                   # bit 39
    odd += [bytes([*STOCK[:5], byte_5]) for byte_5 in (0xE8, 0xD0, 0x48, 0x20, 0xF0, 0xD9, 0xDA, 0x58)]
    odd += [bytes.fromhex("00666699b7f0")]                                               # holding at standstill, as recorded
    for stock in odd:
      car = Car()
      car.drive(0.0, 10)
      car.stock = stock
      assert car.drive(-5.0, 20, long_active=True) == [], stock.hex()

  def test_not_with_a_warning_bit_nor_for_a_while_after(self):
    for bit in (16, 17, 23):
      car = Car()
      car.drive(0.0, 10)
      car.warning = (bit,)
      assert car.drive(-0.5, 10) == []
      car.warning = ()
      # 2.1 s from the last step that had the bit: longer than panda's 2 s, so that panda refuses nothing
      assert car.drive(-0.5, 209) == [], bit
      assert len(car.drive(-0.5, 20)) == 10, bit

  def test_refused_acc_cmd_frames_are_not_refused_steering_frames(self):
    # With a panda that refuses ACC_CMD (L3: the firmware in the car today), every frame of ours
    # comes back on bus 0xC0, up to 50 times a second. The CarState reads refused steering frames
    # there, and three in half a second are a steering fault: these must not count, nor make that
    # parser invalid.
    car = Car()
    car.drive(0.0, 10)
    refused = []
    returned = 0
    for i in range(200):
      _, sent, _ = car.step(-0.5, new_stock_frame=i % 2 == 0, earlier=refused)
      returned += len(refused)
      refused = [CanData(ACC_CMD, dat, 0xC0) for dat in sent]
      assert car.CI.CS.steer_requests_rejected == 0 and car.CI.CS.steer_counters_rejected == [], i
      assert car.CI.can_parsers[Bus.loopback].can_valid, i
      assert not car.CS.steerFaultTemporary and not car.CS.steerFaultPermanent, i
    assert returned >= 99, returned
    # and the path is live: a refused steering frame that asked the EPS to steer is counted
    addr, dat, _ = car.packer.make_can_msg("STEERING_MODULE_ADAS", 0, {"STEER_REQ": 1, "COUNTER": 3})
    car.step(-0.5, earlier=[CanData(addr, bytes(dat), 0xC0)])
    assert car.CI.CS.steer_requests_rejected == 1 and car.CI.CS.steer_counters_rejected == [3]

  def test_recorded_stock_frames_go_back_out_byte_for_byte(self):
    for recorded in RECORDED:
      stock = bytes.fromhex(recorded)
      car = Car()
      car.stock = stock
      car.drive(0.0, 10)
      for _ in range(18):                               # across the counter's rollover
        _, sent, _ = car.step(-0.6)
        assert len(sent) == 1, recorded
        dat = sent[0]
        assert dat[0] == 88
        assert dat[1:6] == stock[1:6], (recorded, dat.hex())
        assert dat[6] == 0xF0 | ((car.counter + 1) % 16)
        assert dat[7] == checksum(dat)
        car.step(-0.6, new_stock_frame=False)


class TestSongPlusLongitudinalRaise(unittest.TestCase):
  """With RAISE on: more than the stock ACC asks for, only on a free road, by no more than panda takes.

  A free road here: the stock ACC controlling at a request of 0, the car at 40 km/h (speedometer) with
  60 set, nothing ahead for the stock radar nor for openpilot."""

  def setUp(self):
    # raising on, by as much as panda allows, whatever values.py says (the first drive uses less: see
    # test_a_smaller_step_above_the_stock_request_for_the_first_drive)
    for name, value in (("RAISE", True), ("RAISE_ABOVE_STOCK_USED", Long.RAISE_MAX_ABOVE_STOCK)):
      patcher = patch.object(Long, name, value)
      patcher.start()
      self.addCleanup(patcher.stop)

  @staticmethod
  def free_road(**kwargs):
    car = Car(**kwargs)
    car.drive(0.0, 230)     # the start counts as "something was just ahead": 2.1 s
    return car

  @staticmethod
  def answers(car, accel, slots, **kwargs):
    """The request of the frame sent for each of the next `slots` stock frames (None: no frame)."""
    out = []
    for _ in range(slots):
      _, sent, reported = car.step(accel, **kwargs)
      out.append(sent[0][0] if sent else None)
      assert abs(reported - ((out[-1] if sent else car.stock[0]) - 100) * 0.05) < 1e-6
      car.step(accel, new_stock_frame=False, **kwargs)
    return out

  def test_not_in_the_first_seconds(self):
    # the start counts as "something was just ahead", as in panda when its mode is set
    car = Car()
    quiet = 0
    while car.step(1.0, new_stock_frame=quiet % 2 == 0)[1] == []:
      quiet += 1
    assert quiet == 210, quiet

  def test_climbs_one_count_per_stock_frame_to_ten_above_it(self):
    car = self.free_road()
    assert self.answers(car, 1.0, 30) == [*range(101, 111), *[110] * 20]
    # with the stock request moving, it stays within 10 of it, one count a frame
    car.stock = bytes([104, *STOCK[1:]])
    assert self.answers(car, 1.0, 6) == [111, 112, 113, 114, 114, 114]
    car.stock = bytes([102, *STOCK[1:]])
    assert self.answers(car, 1.0, 2) == [112, 112]

  def test_a_smaller_step_above_the_stock_request_for_the_first_drive(self):
    with patch.object(Long, "RAISE_ABOVE_STOCK_USED", 4):
      car = self.free_road()
      assert self.answers(car, 1.0, 8) == [101, 102, 103, 104, 104, 104, 104, 104]

  def test_never_above_124(self):
    car = self.free_road()
    car.stock = bytes([118, *STOCK[1:]])
    assert self.answers(car, 2.0, 10) == [*range(119, 125), *[124] * 4]
    car.stock = bytes([124, *STOCK[1:]])
    assert self.answers(car, 2.0, 3) == [None] * 3
    car.stock = bytes([130, *STOCK[1:]])
    assert self.answers(car, 2.0, 3) == [None] * 3      # the stock ACC asks for more than that by itself

  def test_as_much_as_openpilot_wants_and_no_more(self):
    car = self.free_road()
    assert self.answers(car, 0.12, 5) == [101, 102, 102, 102, 102]   # 0.12 m/s^2 is 2.4 counts: 102
    assert self.answers(car, 0.25, 6) == [103, 104, 105, 105, 105, 105]   # on from the 102 the car got last

  def test_not_while_the_stock_acc_brakes(self):
    for stock_request, raises in ((90, False), (99, False), (100, True), (101, True)):
      car = self.free_road()
      car.stock = bytes([stock_request, *STOCK[1:]])
      assert (self.answers(car, 1.0, 3)[0] is not None) == raises, stock_request

  def test_nothing_ahead_for_a_while(self):
    for what in ("lead", "bars", "lead_visible"):
      car = self.free_road()
      assert self.answers(car, 1.0, 3) == [101, 102, 103]
      setattr(car, what, 3 if what == "bars" else True)
      assert self.answers(car, 1.0, 5) == [None] * 5, what
      setattr(car, what, 0 if what == "bars" else False)
      # 2.1 s after the last step that showed something; the stock frames come every second step
      quiet = 0
      while car.step(1.0)[1] == []:
        car.step(1.0, new_stock_frame=False)
        quiet += 1
      assert 2.08 <= quiet * 0.02 <= 2.12, (what, quiet)

  def test_only_below_the_set_speed(self):
    # 1 km/h of margin, on the speedometer's scale; the wheels read 40.02 km/h (552 counts)
    for set_speed, raises in ((60.0, True), (41.5, True), (41.0, False), (40.0, False), (30.0, False)):
      car = self.free_road()
      car.set_speed = set_speed
      assert (self.answers(car, 1.0, 3)[0] is not None) == raises, set_speed

  def test_the_wheel_speed_is_the_speedometers(self):
    # The controller holds wheelSpeeds.fl against the set speed, both on the speedometer's scale, as
    # panda does with the raw wheel (a0's review). That holds because this tree's parse_wheel_speeds
    # puts wheelSpeedFactor into vEgoRaw only. Should it ever scale wheelSpeeds as well (newer opendbc
    # does), the controller would hold road speed against the speedometer's set speed, which is no
    # longer the safe side: it must then read WHEELSPEED_FL itself.
    car = self.free_road()
    car.step(1.0)
    fl_kph = round(40.0 / 0.0725) * 0.0725            # what the frame carries: 552 counts
    assert abs(car.CS.wheelSpeeds.fl * 3.6 - fl_kph) < 1e-6
    assert abs(car.CS.vEgoRaw * 3.6 - fl_kph * car.CP.wheelSpeedFactor) < 1e-6

  def test_not_at_walking_speed(self):
    # 3 m/s of road speed in panda, and 0.5 m/s more here: 13.33 km/h on the speedometer
    for speed, raises in ((0.0, False), (10.0, False), (13.2, False), (13.5, True), (20.0, True)):
      car = self.free_road()
      car.speed = speed
      assert (self.answers(car, 1.0, 3)[0] is not None) == raises, speed

  def test_after_lowering_the_climb_starts_from_the_stock_request(self):
    car = self.free_road()
    assert self.answers(car, -0.5, 2) == [90, 90]
    # the car got 90 last: no raised request can be one count above that, so the stock frame goes first
    assert self.answers(car, 1.0, 3) == [None, 101, 102]

  def test_a_refused_frame_stops_raising_for_a_second(self):
    for after in ("raise", "lower"):
      car = self.free_road()
      assert self.answers(car, 1.0, 2) == [101, 102]
      _, sent, _ = car.step(1.0)
      assert sent and sent[0][0] == 103
      # panda refused it: it comes back on bus 0xC0 a step later, and the car got the stock frame there
      car.step(1.0, new_stock_frame=False, earlier=[CanData(ACC_CMD, sent[0], 0xC0)])
      if after == "lower":
        # lowering goes on as before
        assert self.answers(car, -0.5, 3) == [90, 90, 90]
        continue
      quiet = 0
      while not (sent := car.step(1.0)[1]):
        car.step(1.0, new_stock_frame=False)
        quiet += 1
      assert quiet == 50, quiet
      assert sent[0][0] == 101          # and the climb starts over from the stock request


class Panda:
  """byd.h, fed the way the firmware feeds it: for a frame from the bus the forward decision first
  and then the RX hook; a frame from openpilot through the TX hook."""

  def __init__(self, param):
    self.safety = libsafety_py.libsafety
    assert self.safety.set_safety_hooks(structs.CarParams.SafetyModel.byd, param) == 0
    self.safety.init_tests()
    self.on_bus = []          # ACC_CMD frames that reached the car's bus, in order: (source, data)
    self.on_bus_us = []       # ... and when
    self.refused = []

  def rx(self, frame: CanData, now_us):
    self.safety.set_timer(now_us)
    forwarded = self.safety.safety_fwd_hook(frame.src, frame.address) == 0 if frame.src == CAM_BUS else None
    self.safety.safety_rx_hook(libsafety_py.make_CANPacket(frame.address, frame.src, frame.dat))
    if frame.address == ACC_CMD and forwarded:
      self.on_bus.append(("stock", frame.dat))
      self.on_bus_us.append(now_us)
    return forwarded

  def tx(self, dat, now_us):
    self.safety.set_timer(now_us)
    ok = bool(self.safety.safety_tx_hook(libsafety_py.make_CANPacket(ACC_CMD, MAIN_BUS, dat)))
    (self.on_bus if ok else self.refused).append(("openpilot", dat))
    if ok:
      self.on_bus_us.append(now_us)
    return ok


# For raising: when the road is not free in run_with_panda(raise_drive=True), in s
RAISE_DRIVE_SPELLS = {"lead": (22.0, 22.5), "bars": (25.0, 25.3), "lead_visible": (27.5, 28.0), "slow": (30.0, 32.0), "set_speed": (33.0, 35.0)}


def run_with_panda(param, steps=3000, reply_us=12000, raise_drive=False):
  """A drive in which everything that matters to the rule changes, the CarController and byd.h
  side by side; a frame panda refuses comes back to the controller in the next step, as pandad
  hands it back. With raise_drive, also spells in which the road is not free (RAISE_DRIVE_SPELLS).
  Returns (the panda, the stock ACC_CMD frames sent, the frames the controller built)."""
  car, panda = Car(), Panda(param)
  stock_sent, built = [], []
  refused_back: list[CanData] = []
  for i in range(steps):
    t = i * 0.01
    # the stock ACC: a slowly swinging request, a spell on the gas, a warning, a spell of standby
    car.stock = bytes([100 + round(12 * ((i // 50) % 5 - 2) / 2), *STOCK[1:3], *(STOCK[3:5] if (i // 400) % 2 else bytes([0x8D, 0x42])), 0xD8])
    car.gas = 0.4 if 8.0 <= t < 9.0 else 0.0
    car.warning = (17,) if 14.0 <= t < 14.5 else ()
    car.state_code, car.shown = (2, 0) if 20.0 <= t < 21.0 else ((5, 2) if car.gas else (3, 2))
    if car.state_code == 5:
      car.stock = bytes([*car.stock[:5], 0xE8])
    elif car.state_code == 2:
      car.stock = bytes([100, *STOCK[1:5], 0xD0])
    if raise_drive:
      within = {what: t0 <= t < t1 for what, (t0, t1) in RAISE_DRIVE_SPELLS.items()}
      car.lead, car.bars, car.lead_visible = int(within["lead"]), 2 * within["bars"], within["lead_visible"]
      car.speed = 10.0 if within["slow"] else 40.0
      car.set_speed = 40.0 if within["set_speed"] else 60.0
    # openpilot: wants less than the stock ACC for a while, then more, in turns; sometimes much less
    accel = (-0.4, 0.1, -0.15, 0.5, -3.0, 0.0)[(i // 170) % 6]

    new = i % 2 == 0
    now_us = 1_000_000 + i * 10000
    frames, sent, _ = car.step(accel, new_stock_frame=new, earlier=refused_back)
    refused_back = []
    for f in frames:
      if f.src >= 0x80:
        continue                        # what pandad hands back, not a frame on a bus
      panda.rx(f, now_us)
      if f.address == ACC_CMD:
        stock_sent.append(f.dat)
    for dat in sent:
      built.append(dat)
      if not panda.tx(dat, now_us + reply_us):
        refused_back.append(CanData(ACC_CMD, dat, 0xC0))
  return panda, stock_sent, built


class TestSongPlusLongitudinalWithTodaysPanda(unittest.TestCase):
  """The safety code as it is in this tree, which may be the firmware in the car (no level):
  whatever the CarController builds, nothing of it reaches the car."""

  def setUp(self):
    patcher = patch.object(Long, "RAISE", False)
    patcher.start()
    self.addCleanup(patcher.stop)

  def test_a_panda_without_the_level_refuses_them_all(self):
    # PANDA_LEVEL 0, and equally the firmware that is in the car today: nothing of ours gets out,
    # every stock frame goes to the car
    panda, stock_sent, built = run_with_panda(1)
    assert len(built) > 400
    assert len(panda.refused) == len(built)
    assert [dat for _, dat in panda.on_bus] == stock_sent


if __name__ == "__main__":
  unittest.main()
