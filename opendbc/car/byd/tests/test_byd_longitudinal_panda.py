"""The CarController and byd.h's longitudinal rule, side by side (needs the rule in byd.h).

Every frame the controller builds is taken by the safety code at each level, one frame per slot
reaches the car, and a frame that comes too late is refused while the stock frame goes instead.
The controller's own behaviour is in test_byd_longitudinal.py, the rule's in
opendbc/safety/tests/test_byd.py.
"""
import unittest
from unittest.mock import patch

from opendbc.car.byd.tests.test_byd_longitudinal import RAISE_DRIVE_SPELLS, Car, Panda, checksum, run_with_panda
from opendbc.car.byd.values import SongPlusLongitudinal as Long


class TestSongPlusLongitudinalWithPanda(unittest.TestCase):
  def setUp(self):
    # lowering unless a test switches raising on
    patcher = patch.object(Long, "RAISE", False)
    patcher.start()
    self.addCleanup(patcher.stop)

  def test_panda_takes_every_frame_the_controller_builds(self):
    for level in sorted(Long.MIN_REQUEST):
      if Long.MIN_REQUEST[level] > Long.MIN_REQUEST[Long.LEVEL]:
        continue                                     # a panda level stricter than the controller's is not a configuration
      panda, stock_sent, built = run_with_panda(1 | (level << 1))
      assert len(built) > 400, len(built)
      assert panda.refused == [], (level, len(panda.refused), panda.refused[:1])
      # one frame per slot reached the car, numbered without a gap or a repeat. (If the drive
      # ended on a frame of ours, that one stands for a stock frame the drive did not get to.)
      if len(panda.on_bus) == len(stock_sent) + 1:
        assert panda.on_bus[-1][0] == "openpilot"
        stock_sent.append(None)
      assert len(panda.on_bus) == len(stock_sent)
      counters = [dat[6] & 0xF for _, dat in panda.on_bus]
      first = counters[0]
      assert counters == [(first + n) % 16 for n in range(len(counters))]
      # and each of ours is the stock frame before it with a lower request, down to the floor at most
      ours = 0
      for n, (source, dat) in enumerate(panda.on_bus):
        assert dat[7] == checksum(dat)
        if source == "openpilot":
          ours += 1
          before = stock_sent[n - 1]
          assert dat[1:6] == before[1:6] and Long.MIN_REQUEST[Long.LEVEL] <= dat[0] < before[0], (n, dat.hex(), before.hex())
        else:
          assert dat == stock_sent[n]
      assert ours == len(built)

  def test_panda_takes_every_raised_frame_as_well(self):
    # RAISE on, and a panda that may raise: still nothing refused, through spells of something
    # ahead, of walking speed and of the set speed below the car's speed
    with patch.object(Long, "RAISE", True):
      panda, stock_sent, built = run_with_panda(1 | (Long.LEVEL << 1) | Long.PANDA_RAISE_BIT, steps=4000, raise_drive=True)
    assert panda.refused == [], (len(panda.refused), panda.refused[:1])
    if len(panda.on_bus) == len(stock_sent) + 1:
      stock_sent.append(None)
    assert len(panda.on_bus) == len(stock_sent)
    counters = [dat[6] & 0xF for _, dat in panda.on_bus]
    assert counters == [(counters[0] + n) % 16 for n in range(len(counters))]
    raised = 0
    for n, (source, dat) in enumerate(panda.on_bus):
      before = stock_sent[n - 1]
      if source == "openpilot" and dat[0] > before[0]:
        raised += 1
        t = (panda.on_bus_us[n] - 1_000_000) / 1e6
        # what the car got: no more than 10 counts above the stock request it replaced, never above 124,
        # one count above the frame before at most; and nothing near a spell in which the road was not free
        assert before[0] >= 100 and dat[0] <= before[0] + 10 and dat[0] <= 124, (n, dat.hex(), before.hex())
        assert dat[0] <= panda.on_bus[n - 1][1][0] + 1, (n, dat.hex(), panda.on_bus[n - 1][1].hex())
        for what, (t0, t1) in RAISE_DRIVE_SPELLS.items():
          assert not (t0 <= t < t1 + (2.1 if what in ("lead", "bars", "lead_visible") else 0.0)), (what, t)
    assert raised > 100, raised

  def test_a_panda_that_cannot_raise_refuses_about_one_frame_a_second(self):
    # RAISE on by mistake with a panda that has no raise bit: every raised frame is refused, the stock
    # frame goes instead, and the controller tries again only a second later
    with patch.object(Long, "RAISE", True):
      panda, stock_sent, built = run_with_panda(1 | (Long.LEVEL << 1), steps=4000, raise_drive=True)
    # (lowered frames it takes, see test_panda_takes_every_frame_the_controller_builds: these are the raised ones)
    assert 1 <= len(panda.refused) <= 40, len(panda.refused)          # 40 s of driving
    for n, (source, dat) in enumerate(panda.on_bus):
      if source == "openpilot":
        assert dat[0] < stock_sent[n - 1][0]        # every one that went is a lowered one

  def test_a_late_frame_is_refused_and_the_stock_frame_has_gone_instead(self):
    # openpilot's frame comes 21 ms after the stock frame it copies, 1 ms after the next one
    car, panda = Car(), Panda(1 | (Long.LEVEL << 1))
    pending = None
    for i in range(200):
      now_us = 1_000_000 + i * 10000
      frames, sent, _ = car.step(-0.4 if i >= 20 else 0.0, new_stock_frame=i % 2 == 0)
      for f in frames:
        panda.rx(f, now_us)
      if pending is not None and i % 2 == 0:
        panda.tx(pending, now_us + 1000)
        pending = None
      if sent:
        pending = sent[0]
    assert len(panda.refused) > 80
    assert all(source == "stock" for source, _ in panda.on_bus)
    counters = [dat[6] & 0xF for _, dat in panda.on_bus]
    assert counters == [(counters[0] + n) % 16 for n in range(len(counters))]


if __name__ == "__main__":
  unittest.main()
