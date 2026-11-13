import unittest

from opendbc.car import structs
from opendbc.car.car_helpers import interfaces
from opendbc.car.byd.values import CAR, BydFlags

STEERING_MODULE_ADAS = 0x1E2
LKAS_HUD_ADAS = 0x316
PCM_BUTTONS = 0x3B0


def run_controller(platform, steps=100, cancel=False, lat_active=False):
  """Drive the real CarController for `steps` 100 Hz frames; return every frame it asks to send."""
  CarInterface = interfaces[platform]
  CI = CarInterface(CarInterface.get_non_essential_params(platform))
  CI.update([])   # one pass with no CAN so CS.out exists
  sent = []
  for i in range(steps):
    CC = structs.CarControl()
    CC.enabled = lat_active
    CC.latActive = lat_active
    CC.cruiseControl.cancel = cancel
    _, can_sends = CI.apply(CC.as_reader(), i * 10_000_000)
    sent += [(msg[0], msg[2], bytes(msg[1])) for msg in can_sends]
  return sent


def count(sent, addr):
  return sum(1 for a, _, _ in sent if a == addr)


class TestBydCancelButton(unittest.TestCase):
  """controlsd asks for cruiseControl.cancel whenever the stock cruise is engaged and openpilot
  is not. What each BYD does with that request."""

  def test_song_plus_never_sends_the_button(self):
    # cruise on, openpilot not engaged: the case that asks for a cancel 10 times a second
    sent = run_controller(CAR.BYD_SONG_PLUS_DMI_23, cancel=True, lat_active=False)
    assert count(sent, PCM_BUTTONS) == 0
    # and it does not matter whether openpilot is steering
    sent = run_controller(CAR.BYD_SONG_PLUS_DMI_23, cancel=True, lat_active=True)
    assert count(sent, PCM_BUTTONS) == 0

  def test_song_plus_still_sends_steering_and_hud(self):
    sent = run_controller(CAR.BYD_SONG_PLUS_DMI_23, steps=100, cancel=True)
    assert count(sent, STEERING_MODULE_ADAS) == 50
    assert count(sent, LKAS_HUD_ADAS) == 50
    assert {a for a, _, _ in sent} == {STEERING_MODULE_ADAS, LKAS_HUD_ADAS}

  def test_atto3_still_sends_the_button(self):
    sent = run_controller(CAR.BYD_ATTO_3, steps=100, cancel=True)
    buttons = [(bus, dat) for a, bus, dat in sent if a == PCM_BUTTONS]
    assert len(buttons) == 10                      # every 10th frame
    assert all(bus == 0 for bus, _ in buttons)
    assert all(dat[2] & 0x08 for _, dat in buttons)  # ACC_ON_BTN

  def test_nothing_is_sent_without_a_cancel_request(self):
    for platform in (CAR.BYD_ATTO_3, CAR.BYD_SONG_PLUS_DMI_23):
      assert count(run_controller(platform, cancel=False), PCM_BUTTONS) == 0

  def test_flag_is_on_the_song_plus_only(self):
    flags = {p: interfaces[p].get_non_essential_params(p).flags for p in (CAR.BYD_ATTO_3, CAR.BYD_SONG_PLUS_DMI_23)}
    assert flags[CAR.BYD_SONG_PLUS_DMI_23] & BydFlags.NO_CANCEL_BUTTON
    assert not flags[CAR.BYD_ATTO_3] & BydFlags.NO_CANCEL_BUTTON


if __name__ == "__main__":
  unittest.main()
