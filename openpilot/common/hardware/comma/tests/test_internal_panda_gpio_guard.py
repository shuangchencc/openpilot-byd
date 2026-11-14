#!/usr/bin/env python3
"""On tici the two methods that drive the internal panda's reset and boot lines must refuse.

Hardware-free: gpio_init and gpio_set are replaced, so no GPIO is touched on any device.
"""
import unittest
from unittest import mock

from openpilot.common.hardware.comma import hardware
from openpilot.common.hardware.comma.hardware import HardwareComma


class TestInternalPandaGpioGuard(unittest.TestCase):
  def _call(self, device_type: str, method: str):
    hw = HardwareComma()
    with mock.patch.object(HardwareComma, "get_device_type", return_value=device_type), \
         mock.patch.object(hardware, "gpio_init") as gpio_init, \
         mock.patch.object(hardware, "gpio_set") as gpio_set, \
         mock.patch.object(hardware.time, "sleep"):
      try:
        getattr(hw, method)()
        raised = None
      except RuntimeError as e:
        raised = e
      return raised, gpio_init.call_count + gpio_set.call_count

  def test_tici_refuses_before_touching_a_gpio(self):
    for method in ("reset_internal_panda", "recover_internal_panda"):
      with self.subTest(method=method):
        raised, gpio_calls = self._call("tici", method)
        self.assertIsNotNone(raised)
        self.assertIn(method, str(raised))
        self.assertEqual(gpio_calls, 0)

  def test_other_devices_are_unchanged(self):
    # 2 gpio_init, then 3 gpio_set for a reset and 4 for the bootloader sequence
    for device_type in ("tizi", "mici"):
      for method, expected in (("reset_internal_panda", 5), ("recover_internal_panda", 6)):
        with self.subTest(device_type=device_type, method=method):
          raised, gpio_calls = self._call(device_type, method)
          self.assertIsNone(raised)
          self.assertEqual(gpio_calls, expected)


if __name__ == "__main__":
  unittest.main()
