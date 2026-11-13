"""card.step() must tell pandad to leave ELM327 in passive (dashcam) mode too.

Runs the real Car.step() with its collaborators replaced, so it checks the wiring that failed in
the car on 2026-11-12 (panda stuck in elm327 because ControlsReady was never set), not a copy of
the condition. Plain unittest: the device venv has no pytest.
"""
import unittest
from types import SimpleNamespace

from openpilot.selfdrive.car import card as card_module
from openpilot.selfdrive.car.card import Car

EventName = card_module.EventName


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


if __name__ == "__main__":
  unittest.main()
