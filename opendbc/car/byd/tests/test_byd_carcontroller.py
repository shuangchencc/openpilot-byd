import unittest

from opendbc.can.packer import CANPacker
from opendbc.car import Bus, structs
from opendbc.car.byd import bydcan
from opendbc.car.car_helpers import interfaces
from opendbc.car.byd.values import CAR, DBC, BydFlags

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


def hud_frame(platform, lat_active, counter=5, stock=None, lanes_visible=False, left_depart=False, right_depart=False):
  """LKAS_HUD_ADAS as bydcan builds it for `platform`, as hex."""
  hud = structs.CarControl.HUDControl()
  hud.leftLaneVisible = hud.rightLaneVisible = lanes_visible
  hud.leftLaneDepart, hud.rightLaneDepart = left_depart, right_depart
  song_plus = bool(interfaces[platform].get_non_essential_params(platform).flags & BydFlags.LKAS_HUD_ALT)
  msg = bydcan.create_lkas_hud(CANPacker(DBC[platform][Bus.pt]), lat_active, counter, dict(stock or {}), hud, song_plus)
  assert msg[0] == LKAS_HUD_ADAS and msg[2] == 0
  return bytes(msg[1]).hex()


class TestSongPlusHud(unittest.TestCase):
  """What the instrument cluster is sent in LKAS_HUD_ADAS. The Song Plus gets what the stack
  that steered this car through the 116 min recording sent it."""

  SONG_PLUS = CAR.BYD_SONG_PLUS_DMI_23
  STOCK = {"HMA_ON_OFF": 1}       # the stock camera's frame on this car: this bit and nothing else

  def test_steering_frame_is_the_recorded_one(self):
    # recorded at 21:18:15.79, counter 14: both lane states 2, LKAS_STATE 3
    assert hud_frame(self.SONG_PLUS, True, counter=14, stock=self.STOCK) == "208000003800e047"

  def test_idle_frame_is_the_stock_cameras(self):
    assert hud_frame(self.SONG_PLUS, False, counter=2, stock=self.STOCK) == "008000000000205f"

  def test_nothing_else_is_signalled(self):
    # lane visibility and lane departure change nothing, steering or not
    for lat_active in (False, True):
      plain = hud_frame(self.SONG_PLUS, lat_active, stock=self.STOCK)
      for lanes_visible in (False, True):
        for left_depart, right_depart in ((False, False), (True, False), (False, True)):
          assert hud_frame(self.SONG_PLUS, lat_active, stock=self.STOCK, lanes_visible=lanes_visible,
                           left_depart=left_depart, right_depart=right_depart) == plain

  def test_the_rest_of_the_stock_frame_passes_through(self):
    stock = {"HMA_ON_OFF": 1, "LKS_MODE": 3, "TJA_ICA_STATE": 2, "SPEED_LIMIT_VALUE": 60, "AUTO_LIGHT": 1, "SLA_STATE": 2,
             "LEFT_LANE_STATE": 1, "RIGHT_LANE_STATE": 1, "LKAS_STATE": 1}
    idle = bytes.fromhex(hud_frame(self.SONG_PLUS, False, stock=stock))
    steering = bytes.fromhex(hud_frame(self.SONG_PLUS, True, stock=stock))
    assert idle[0] >> 6 == 3 and steering[0] >> 6 == 3                      # LKS_MODE is the camera's, never ours
    assert (idle[0] >> 4) & 3 == 1 and (steering[0] >> 4) & 3 == 2          # LEFT_LANE_STATE
    assert (idle[4] >> 2) & 3 == 1 and (steering[4] >> 2) & 3 == 2          # RIGHT_LANE_STATE
    assert idle[4] >> 4 == 1 and steering[4] >> 4 == 3                      # LKAS_STATE
    # and nothing else differs between the two
    differing = [i for i in range(6) if idle[i] != steering[i]]
    assert differing == [0, 4]
    assert idle[5] == steering[5] == 13                                     # SPEED_LIMIT_VALUE 60 km/h

  def test_hands_on_wheel_request_is_never_passed_on(self):
    for lat_active in (False, True):
      with_req = hud_frame(self.SONG_PLUS, lat_active, stock={**self.STOCK, "HANDS_ON_WHEEL_REQ": 1})
      assert with_req == hud_frame(self.SONG_PLUS, lat_active, stock=self.STOCK)

  def test_atto3_frames_are_what_they_were(self):
    # (steering, lanes visible, left departure, right departure): frame before this flag existed
    before = {
      (0, 0, 0, 0): "00000000000050af", (0, 0, 0, 1): "00000000080050a7", (0, 0, 1, 0): "200000000000508f",
      (0, 1, 0, 0): "00000000000050af", (0, 1, 0, 1): "00000000080050a7", (0, 1, 1, 0): "200000000000508f",
      (1, 0, 0, 0): "800000002000500f", (1, 0, 0, 1): "8000000028005007", (1, 0, 1, 0): "a0000000200050ef",
      (1, 1, 0, 0): "90000000240050fb", (1, 1, 0, 1): "90000000280050f7", (1, 1, 1, 0): "a0000000240050eb",
    }
    for (lat_active, lanes_visible, left_depart, right_depart), frame in before.items():
      assert hud_frame(CAR.BYD_ATTO_3, bool(lat_active), lanes_visible=bool(lanes_visible),
                       left_depart=bool(left_depart), right_depart=bool(right_depart)) == frame

  def test_the_controller_sends_each_car_its_own(self):
    # no CAN has been parsed, so the "stock" frame is all zero; bytes 0 and 4 hold what we set
    def payloads(platform, lat_active):
      return {(dat[0], dat[4]) for a, _, dat in run_controller(platform, lat_active=lat_active) if a == LKAS_HUD_ADAS}
    assert payloads(self.SONG_PLUS, True) == {(0x20, 0x38)}
    assert payloads(self.SONG_PLUS, False) == {(0x00, 0x00)}
    assert payloads(CAR.BYD_ATTO_3, True) == {(0x80, 0x20)}
    assert payloads(CAR.BYD_ATTO_3, False) == {(0x00, 0x00)}

  def test_flag_is_on_the_song_plus_only(self):
    flags = {p: interfaces[p].get_non_essential_params(p).flags for p in (CAR.BYD_ATTO_3, self.SONG_PLUS)}
    assert flags[self.SONG_PLUS] & BydFlags.LKAS_HUD_ALT
    assert not flags[CAR.BYD_ATTO_3] & BydFlags.LKAS_HUD_ALT


if __name__ == "__main__":
  unittest.main()
