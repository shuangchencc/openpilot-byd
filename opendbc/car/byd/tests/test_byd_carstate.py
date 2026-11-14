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
    self.CI = CarInterface(CarInterface.get_non_essential_params(platform))
    self.CS = self.CI.CS
    self.pt, self.cam = FakeParser(), FakeParser()
    self.frame = 0
    self.sent = []

  def eps(self, state):
    self.pt.vl["STEERING_TORQUE"]["LKA_STATE"] = state

  def driver_torque(self, torque):
    self.pt.vl["STEERING_TORQUE"]["DRIVER_TORQUE"] = torque

  def wheel(self, angle_deg):
    self.pt.vl["STEER_MODULE_2"]["STEER_ANGLE_2"] = angle_deg

  def speed(self, kph):
    for wheel in ("WHEELSPEED_FL", "WHEELSPEED_FR", "WHEELSPEED_BL", "WHEELSPEED_BR"):
      self.pt.vl["WHEELSPEED_CLEAN"][wheel] = kph

  def drive(self, lat_active, steps=1, enabled=None, target_deg=0.0):
    """CarState then CarController, as the interface runs them, `steps` times at 100 Hz.
    `enabled` is openpilot being engaged; unless given, it is engaged exactly while it asks to steer.
    Every frame the controller builds is appended to self.sent as (address, data)."""
    ret = None
    for _ in range(steps):
      ret = self.step()
      self.CS.out = ret
      CC = structs.CarControl()
      CC.latActive = lat_active
      CC.enabled = lat_active if enabled is None else enabled
      CC.actuators.steeringAngleDeg = target_deg
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
    assert 17.0 < first < 20.0, first          # one limiter step from the wheel, not a jump to 0
    asked = [angle for _, angle, _ in frames[back:]]
    assert all(b <= a for a, b in zip(asked, asked[1:], strict=False))          # then steadily towards the target
    assert max(a - b for a, b in zip(asked, asked[1:], strict=False)) <= 20.0 - first + 1e-6

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


if __name__ == "__main__":
  unittest.main()
