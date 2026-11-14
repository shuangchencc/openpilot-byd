"""card.step() must tell pandad to leave ELM327 in passive (dashcam) mode too.

Runs the real Car.step() with its collaborators replaced, so it checks the wiring that failed in
the car on 2026-11-12 (panda stuck in elm327 because ControlsReady was never set), not a copy of
the condition. Plain unittest: the device venv has no pytest.
"""
import itertools
import unittest
from types import SimpleNamespace

from opendbc.car import gen_empty_fingerprint, structs
from opendbc.car.byd.values import CAR
from opendbc.car.car_helpers import interfaces
from openpilot.selfdrive.car import card as card_module
from openpilot.selfdrive.car.card import Car, songplus_alpha_long
from openpilot.selfdrive.car.car_events import CarEvents

EventName = card_module.EventName
EVENT_NAME = {number: name for name, number in EventName.schema.enumerants.items()}


class FakeParams:
  def __init__(self):
    self.puts = []

  def put_bool(self, key, value, **kwargs):
    self.puts.append((key, value))

  def put_bool_nonblocking(self, key, value):
    self.puts.append((key, value))


class FakeSubMaster(dict):
  def __init__(self, events, seen=True):
    super().__init__(onroadEvents=events, carControl=SimpleNamespace())
    self.seen = {'onroadEvents': seen}

  def all_alive(self, services):
    return False  # keeps controls_update() from reaching CI.apply()


class FakeCarInterface:
  def __init__(self):
    self.init_calls = 0

  def init(self, CP, *callbacks):
    self.init_calls += 1


def make_car(passive, events, seen=True):
  car = Car.__new__(Car)  # no CAN sockets, no fingerprinting
  car.CP = SimpleNamespace(passive=passive)
  car.CI = FakeCarInterface()
  car.params = FakeParams()
  car.sm = FakeSubMaster(events, seen)
  car.can_callbacks = ()
  car.initialized_prev = False
  car.CS_prev = None
  car.state_update = lambda: (SimpleNamespace(canValid=True), None)
  car.state_publish = lambda CS, RD: None
  return car


INITIALIZING = [SimpleNamespace(name=EventName.selfdriveInitializing)]
READY = [SimpleNamespace(name=EventName.startupNoControl)]


class TestPassiveControlsReady(unittest.TestCase):
  def test_passive_sets_controls_ready_once_initialized(self):
    car = make_car(passive=True, events=READY)
    car.step()
    self.assertEqual(car.params.puts, [("ControlsReady", True)])
    self.assertEqual(car.CI.init_calls, 0, "a passive car must not run CI.init(): it may transmit")

  def test_passive_sets_it_only_once(self):
    car = make_car(passive=True, events=READY)
    for _ in range(5):
      car.step()
    self.assertEqual(car.params.puts, [("ControlsReady", True)])

  def test_passive_waits_for_selfdrived_to_initialize(self):
    car = make_car(passive=True, events=INITIALIZING)
    car.step()
    car.step()
    self.assertEqual(car.params.puts, [])
    car.sm['onroadEvents'] = READY
    car.step()
    self.assertEqual(car.params.puts, [("ControlsReady", True)])

  def test_passive_waits_until_onroad_events_were_seen(self):
    car = make_car(passive=True, events=[], seen=False)
    car.step()
    self.assertEqual(car.params.puts, [])

  def test_active_path_is_unchanged(self):
    car = make_car(passive=False, events=READY)
    for _ in range(3):
      car.step()
    self.assertEqual(car.params.puts, [("ControlsReady", True)])
    self.assertEqual(car.CI.init_calls, 1)

  def test_active_car_does_nothing_before_initialization(self):
    car = make_car(passive=False, events=INITIALIZING)
    car.step()
    self.assertEqual(car.params.puts, [])
    self.assertEqual(car.CI.init_calls, 0)


class TestSongPlusAlphaLongSwitch(unittest.TestCase):
  """openpilot longitudinal on the Song PLUS exists for one launch at a time and only on top of
  the switch that allows control at all."""

  def test_needs_both_switches(self):
    for control, long in itertools.product((None, "0", "1"), repeat=2):
      environ = {k: v for k, v in (("SONGPLUS_ALLOW_CONTROL", control), ("SONGPLUS_ALPHA_LONG", long)) if v is not None}
      self.assertEqual(control == "1" and long == "1", songplus_alpha_long(environ), environ)

  def test_only_the_exact_value_counts(self):
    for value in ("", "true", "True", "yes", "on", "2", " 1", "1 "):
      self.assertFalse(songplus_alpha_long({"SONGPLUS_ALLOW_CONTROL": "1", "SONGPLUS_ALPHA_LONG": value}), repr(value))
      self.assertFalse(songplus_alpha_long({"SONGPLUS_ALLOW_CONTROL": value, "SONGPLUS_ALPHA_LONG": "1"}), repr(value))

  def test_the_param_is_not_read(self):
    # A value left in AlphaLongitudinalEnabled must not be able to turn it on: card.py does not
    # look at it any more. (The only thing in its place is the function tested above.)
    import inspect
    source = inspect.getsource(card_module)
    self.assertNotIn("AlphaLongitudinalEnabled", source.replace(inspect.getsource(songplus_alpha_long), ""))
    self.assertIn("alpha_long_allowed = songplus_alpha_long(os.environ)", source)

  def test_what_the_switch_turns_on(self):
    CarInterface = interfaces[CAR.BYD_SONG_PLUS_DMI_23]
    for alpha_long in (False, True):
      CP = CarInterface.get_params(CAR.BYD_SONG_PLUS_DMI_23, gen_empty_fingerprint(), [], alpha_long, False, False)
      self.assertEqual(alpha_long, CP.openpilotLongitudinalControl)
      # either way: the stock ACC engages and disengages, no toggle in the settings, and the
      # tree is still passive unless control is allowed for the launch
      self.assertTrue(CP.pcmCruise)
      self.assertFalse(CP.alphaLongitudinalAvailable)
      self.assertTrue(CP.dashcamOnly)


class TestSongPlusAlphaLongEngagement(unittest.TestCase):
  """With openpilot longitudinal the stock ACC still decides when openpilot is engaged: every event
  the car code raises is the same with and without it, step by step, over everything a driver
  does to engage and disengage."""

  @staticmethod
  def car_state(**kwargs):
    CS = structs.CarState()
    CS.gearShifter = structs.CarState.GearShifter.drive
    CS.cruiseState.available = True
    CS.cruiseState.speed = 20.0
    CS.vEgo = 15.0
    CS.canValid = True
    for k, v in kwargs.items():
      if k in ("enabled", "standstill"):
        setattr(CS.cruiseState, k, v)
      else:
        setattr(CS, k, v)
    return CS

  def test_events_are_the_same(self):
    ButtonEvent = structs.CarState.ButtonEvent
    cancel = lambda pressed: [ButtonEvent(pressed=pressed, type=ButtonEvent.Type.cancel)]   # noqa: E731
    steps = [
      {}, {"enabled": True}, {"enabled": True}, {"enabled": True, "gasPressed": True}, {"enabled": True},
      {"enabled": True, "brakePressed": True}, {"brakePressed": True}, {},
      {"enabled": True}, {"enabled": True, "buttonEvents": cancel(True)}, {"enabled": True, "buttonEvents": cancel(False)}, {},
      {"enabled": True}, {"enabled": True, "steeringPressed": True}, {"enabled": True, "steerFaultTemporary": True},
      {"enabled": True, "steerFaultPermanent": True}, {},
      {"enabled": True}, {"enabled": True, "doorOpen": True}, {"enabled": True, "seatbeltUnlatched": True},
      {"enabled": True, "vEgo": 0.0, "standstill": True}, {"enabled": True, "vEgo": 0.0, "standstill": True, "brakePressed": True},
      {"enabled": True, "gearShifter": structs.CarState.GearShifter.reverse}, {"enabled": False},
      {"enabled": True, "canValid": False}, {"enabled": True, "espDisabled": True}, {"enabled": True, "accFaulted": True}, {},
    ]
    CarInterface = interfaces[CAR.BYD_SONG_PLUS_DMI_23]
    seen = {}
    for alpha_long in (False, True):
      CP = CarInterface.get_params(CAR.BYD_SONG_PLUS_DMI_23, gen_empty_fingerprint(), [], alpha_long, False, False)
      events = CarEvents(CP)
      CS_prev = self.car_state()
      names = []
      for step in steps:
        CS = self.car_state(**step)
        CC = structs.CarControl()
        CC.enabled = CS.cruiseState.enabled
        CC.actuators.accel = -0.5 if alpha_long else 0.0
        names.append(sorted(EVENT_NAME[e] for e in events.update(CS.as_reader(), CS_prev.as_reader(), CC.as_reader()).names))
        CS_prev = CS
      seen[alpha_long] = names
    self.assertEqual(seen[False], seen[True])
    # and the sequence did engage and disengage, so that this compares something
    flat = {name for names in seen[True] for name in names}
    for name in ("pcmEnable", "pcmDisable", "buttonCancel", "doorOpen", "wrongGear"):
      self.assertIn(name, flat)


if __name__ == "__main__":
  unittest.main()
