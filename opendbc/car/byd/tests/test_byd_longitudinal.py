"""The Song Plus when openpilot does the longitudinal control (alpha_long).

What is checked here is the CarController's side of SongPlusLongitudinal (values.py): which frame it
builds and when. And, by running it next to the real safety code fed the way the firmware feeds it,
that with the safety code in this tree at level 0 - which is the firmware in the car before the
longitudinal rule is flashed - nothing of it reaches the car. That the rule takes every frame the
controller builds is in test_byd_longitudinal_panda.py; the rule itself is tested in
opendbc/safety/tests/test_byd.py.
"""
import unittest

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
    self.CS = None

  def stock_frames(self):
    """The two ACC frames the ADAS controller sends every 20 ms, as CanData from bus 2."""
    self.counter = (self.counter + 1) % 16
    self.hud_counter = (self.hud_counter + 1) % 16
    hud = {"ACC_ENGAGED_STATE": self.shown, "ACC_STATE_CODE": self.state_code, "SET_SPEED": 60, "COUNTER": self.hud_counter}
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

  def step(self, accel, new_stock_frame=True, long_active=None, earlier=()):
    """One step: CAN in (with a new pair of stock frames or without), CarState, CarController.
    `earlier` are frames that arrived before this step's own and have not been read yet.

    Returns (the frames that came in, the ACC_CMD frames the controller built as bytes, what it
    reports as the request the car gets)."""
    frames = [*earlier] + (self.stock_frames() if new_stock_frame else []) + [self.pedal_frame()]
    self.CS = self.CI.update([(self.nanos, frames)])
    CC = structs.CarControl()
    CC.enabled = bool(self.CS.cruiseState.enabled)
    # what controlsd makes of it: no longitudinal while the driver is on the gas
    CC.longActive = (CC.enabled and not self.CS.gasPressed and self.CP.openpilotLongitudinalControl) if long_active is None else long_active
    CC.actuators.accel = accel
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
    assert CP.safetyConfigs[0].safetyParam == 1 | (Long.PANDA_LEVEL << 1)
    assert abs(CP.longitudinalActuatorDelay - 0.3) < 1e-6

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


class Panda:
  """byd.h, fed the way the firmware feeds it: for a frame from the bus the forward decision first
  and then the RX hook; a frame from openpilot through the TX hook."""

  def __init__(self, param):
    self.safety = libsafety_py.libsafety
    assert self.safety.set_safety_hooks(structs.CarParams.SafetyModel.byd, param) == 0
    self.safety.init_tests()
    self.on_bus = []          # ACC_CMD frames that reached the car's bus, in order: (source, data)
    self.refused = []

  def rx(self, frame: CanData, now_us):
    self.safety.set_timer(now_us)
    forwarded = self.safety.safety_fwd_hook(frame.src, frame.address) == 0 if frame.src == CAM_BUS else None
    self.safety.safety_rx_hook(libsafety_py.make_CANPacket(frame.address, frame.src, frame.dat))
    if frame.address == ACC_CMD and forwarded:
      self.on_bus.append(("stock", frame.dat))
    return forwarded

  def tx(self, dat, now_us):
    self.safety.set_timer(now_us)
    ok = bool(self.safety.safety_tx_hook(libsafety_py.make_CANPacket(ACC_CMD, MAIN_BUS, dat)))
    (self.on_bus if ok else self.refused).append(("openpilot", dat))
    return ok


def run_with_panda(param, steps=3000, reply_us=12000):
  """A drive in which everything that matters to the rule changes, the CarController and byd.h
  side by side. Returns (the panda, the stock ACC_CMD frames sent, the frames the controller built)."""
  car, panda = Car(), Panda(param)
  stock_sent, built = [], []
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
    # openpilot: wants less than the stock ACC for a while, then more, in turns; sometimes much less
    accel = (-0.4, 0.1, -0.15, 0.5, -3.0, 0.0)[(i // 170) % 6]

    new = i % 2 == 0
    now_us = 1_000_000 + i * 10000
    frames, sent, _ = car.step(accel, new_stock_frame=new)
    for f in frames:
      panda.rx(f, now_us)
      if f.address == ACC_CMD:
        stock_sent.append(f.dat)
    for dat in sent:
      built.append(dat)
      panda.tx(dat, now_us + reply_us)
  return panda, stock_sent, built


class TestSongPlusLongitudinalWithTodaysPanda(unittest.TestCase):
  """The safety code as it is in this tree, which may be the firmware in the car (no level):
  whatever the CarController builds, nothing of it reaches the car."""

  def test_a_panda_without_the_level_refuses_them_all(self):
    # PANDA_LEVEL 0, and equally the firmware that is in the car today: nothing of ours gets out,
    # every stock frame goes to the car
    panda, stock_sent, built = run_with_panda(1)
    assert len(built) > 400
    assert len(panda.refused) == len(built)
    assert [dat for _, dat in panda.on_bus] == stock_sent


if __name__ == "__main__":
  unittest.main()
