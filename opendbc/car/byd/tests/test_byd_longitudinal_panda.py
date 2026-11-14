"""The CarController and byd.h's longitudinal rule, side by side (needs the rule in byd.h).

Every frame the controller builds is taken by the safety code at each level, one frame per slot
reaches the car, and a frame that comes too late is refused while the stock frame goes instead.
The controller's own behaviour is in test_byd_longitudinal.py, the rule's in
opendbc/safety/tests/test_byd.py.
"""
import unittest

from opendbc.car.byd.tests.test_byd_longitudinal import Car, Panda, checksum, run_with_panda
from opendbc.car.byd.values import SongPlusLongitudinal as Long


class TestSongPlusLongitudinalWithPanda(unittest.TestCase):
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
