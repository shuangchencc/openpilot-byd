#!/usr/bin/env python3
# simple pandad wrapper that updates the panda first
import os
import signal
import subprocess
import time

import usb1

from panda import Panda, PandaDFU
from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

# Why this file diverges from upstream on this device
# --------------------------------------------------
# This vehicle's setup has TWO pandas: an internal F4 "dos" panda built into the C3XL, and
# a red panda (H7). Our panda submodule (a591951) is H7-only:
#   board/obj/              only panda_h7.bin.signed and bootstub.panda_h7.bin
#   constants.py            class McuType has a single member, H7
#   __init__.py:152-153     H7_DEVICES = [RED_PANDA 0x07, TRES 0x09, CUATRO 0x0a, BODY 0xb1]
#                           SUPPORTED_DEVICES = H7_DEVICES          (no dos, no F4)
#   __init__.py:509         up_to_date() compares against McuType.H7.config.app_fn
#   __init__.py:643         get_dfu_serial() hardcodes McuType.H7
#
# Upstream's flow assumes the only panda worth talking to is the internal one, and reaches
# for GPIO to recover it:
#   HARDWARE.reset_internal_panda()     RST high, BOOT0 low,  RST low   -> plain reset
#   HARDWARE.recover_internal_panda()   RST high, BOOT0 HIGH, RST low   -> ST bootloader
# with GPIO.STM_RST_N = 124 and GPIO.STM_BOOT0 = 134 (common/hardware/comma/pins.py), i.e.
# the comma three's internal panda pins. On this device those pins go to the *dos* panda,
# and neither call does a type check first.
#
# What that actually causes, checked in the source rather than assumed:
#   - recover_internal_panda() leaves the dos panda sitting in the ST bootloader, so it
#     stops being a panda and CAN goes away until something resets it.
#   - It does NOT brick it. PandaDFU(None) -> usb.py:44-46 identifies the MCU by DFU sector
#     count, `assert sector_count in mcu_by_sector_count`, and H7's sector_count is 8 while
#     an STM32F4 reports 04*016Kg,01*064Kg,07*128Kg = 12. The assert fires before any write.
#     The SPI path is equally safe: spi.py:297 MCU_TYPE_BY_IDCODE[get_chip_id()] KeyErrors.
#     (This corrects an earlier reading that had the H7 bootstub being written into the F4.)
#   - The AssertionError propagates out of flash_panda() into main()'s except, so the loop
#     repeats: reset the dos panda, push it into DFU, fail, reset it again, forever, and
#     ./pandad never starts.
#   - panda_ready() used Panda(cli=False) with no serial, which connects to whichever panda
#     Panda.list() happens to yield first. Connecting to the dos panda makes up_to_date()
#     or the SUPPORTED_DEVICES assert raise, which panda_ready() catches and turns into
#     False, which calls flash_panda(). So the bad path is not a rare race; it is what
#     happens whenever the dos panda is enumerated first.
#
# So: select the panda by type, address it by serial everywhere, and drop the GPIO calls.
# Dropping them costs nothing for a red panda - those pins are not wired to it, so
# recover_internal_panda() could never have recovered it anyway. A red panda that will not
# answer has to be recovered over USB DFU by hand.
#
# Flashing a red panda is done entirely in software: reset(enter_bootloader=True) asserts
# SUPPORTED_DEVICES itself (__init__.py:355), so it cannot be aimed at the dos panda even by
# mistake, and PandaDFU is given that panda's own DFU serial so it cannot pick up a
# different device that happens to be in DFU.

# Set this to pin a specific panda when more than one supported device is present.
SERIAL_ENV = "PANDA_SERIAL"


def _usb_hw_type(serial: str) -> bytes | None:
  """Read a USB panda's hw type without going through Panda(), i.e. without writing to it.

  Panda.connect() is not a passive open: __init__.py:233-247 sends set_heartbeat_disabled,
  set_power_save(0), can_reset_communications, and then set_canfd_auto(bus, False) and
  set_can_speed_kbps(bus, 500) for every bus. Setting every bus to 500 kbps is the one that
  would matter on a device wired to a car. Since the whole point here is to decide which
  devices we are allowed to touch, the decision itself must not touch them: get_type() is
  vendor request 0xc1, a control READ, so it can be issued directly.

  Returns None if this serial is not on USB (e.g. SPI) or is sitting in the bootstub.
  """
  with usb1.USBContext() as ctx:
    for device in ctx.getDeviceList(skip_on_error=True):
      if device.getVendorID() not in Panda.USB_VIDS or device.getProductID() not in Panda.USB_PIDS:
        continue
      try:
        if device.getSerialNumber() != serial:
          continue
      except Exception:
        continue
      # usb_connect() calls this the bootstub marker (__init__.py:294)
      if (device.getProductID() & 0xF0) == 0xe0:
        return None
      handle = device.open()
      try:
        # libusb1's controlRead defaults to timeout=0, i.e. wait forever. A device
        # that does not answer would hang pandad before it ever starts.
        return bytes(handle.controlRead(Panda.REQUEST_IN, 0xc1, 0, 0, 0x40, timeout=1000))
      finally:
        handle.close()
  return None


def supported_panda_serials() -> list[str]:
  """Serials of the pandas our firmware can actually drive, as reported by get_type().

  Unsupported devices are never reset, pushed into DFU, or flashed. Identification is done
  with a control read where possible (see _usb_hw_type) so that deciding to leave a device
  alone does not itself write to it. A device that cannot be identified is also skipped -
  an old or mismatched protocol raising here must not be read as "needs flashing".
  """
  supported: list[str] = []
  for serial in sorted(Panda.list()):
    try:
      hw_type = _usb_hw_type(serial)
      how = "control-read"
      if hw_type is None:
        # Not on USB, or in the bootstub. Fall back to a full open, which does write the
        # settings listed in _usb_hw_type. Acceptable here: a device in the bootstub is not
        # driving a bus, and an SPI panda on this device is the one we intend to use anyway.
        how = "full-open"
        with Panda(serial, cli=False) as p:
          hw_type = p.get_type()
      if hw_type in Panda.SUPPORTED_DEVICES:
        supported.append(serial)
        cloudlog.event("pandad.device_supported", serial=serial, hw_type=hw_type.hex(), how=how)
      else:
        cloudlog.warning(f"skipping unsupported panda {serial}, hw_type {hw_type.hex()} "
                         f"({how}): our firmware is H7-only and this device must not be "
                         "reset, pushed into DFU, or flashed")
    except Exception:
      cloudlog.exception(f"pandad.could_not_identify_panda serial={serial}")
  return supported


def panda_ready(serial: str) -> bool:
  """Is this specific panda up and running the firmware we ship?"""
  try:
    if not Panda.wait_for_panda(serial, timeout=1):
      cloudlog.warning(f"panda {serial} not found")
      return False
    with Panda(serial, cli=False) as p:
      if p.bootstub:
        cloudlog.warning(f"panda stuck in bootstub, version {p.get_version()}")
        return False
      if not p.up_to_date():
        cloudlog.warning(f"panda firmware out of date, version {p.get_version()}")
        return False
      return True
  except Exception:
    # e.g. SPI protocol mismatch with old firmware
    cloudlog.exception("pandad.panda_check_failed")
    return False


def flash_panda(serial: str) -> None:
  """Reflash bootstub + app on one specific panda, over USB DFU, without touching GPIO.

  The bootstub is replaced and not just the app: the bootstub on a panda that has been
  running someone else's build may be signed with a different key than the app we ship,
  and a signature mismatch leaves it stuck in the bootstub.
  """
  cloudlog.warning(f"flashing panda {serial}")

  with Panda(serial, cli=False) as p:
    # Re-check on this same connection before touching anything: the caller's "not ready"
    # may have been a panda that was merely slow to enumerate.
    if not p.bootstub and p.up_to_date():
      cloudlog.warning(f"panda {serial} is up to date after all; not flashing")
      return
    # Derived from this panda's own UID, so PandaDFU below cannot land on another device
    # that happens to be sitting in DFU.
    dfu_serial = p.get_dfu_serial()
    # Two steps, as upstream's Panda.recover() does (python/__init__.py:464-469). A RELEASE
    # app ignores a direct "enter bootloader" request - main_comms.h:164-175 compiles it only
    # under ALLOW_DEBUG - but always honours "enter bootstub", and the bootstub honours
    # "enter bootloader" unconditionally (flasher.h:67-74). Measured on this car's red panda
    # 2026-11-12: its vendor DEV-719e0dd0-RELEASE app ignored the one-step request, so DFU was
    # never reached and pandad looped. reset() asserts get_type() in SUPPORTED_DEVICES before
    # each request, and the bootstub answers get_type() (flasher.h:45), so neither request can
    # be aimed at an unsupported panda.
    p.reset(enter_bootstub=True)
    p.reset(enter_bootloader=True)

  assert Panda.wait_for_dfu(dfu_serial, timeout=10), f"panda {serial} didn't enter DFU"
  with PandaDFU(dfu_serial) as dfu:
    dfu.recover()

  assert Panda.wait_for_panda(serial, timeout=10), f"panda {serial} didn't boot into bootstub"
  with Panda(serial, cli=False) as p:
    p.flash()
  cloudlog.warning(f"done flashing panda {serial}")


def select_panda(timeout: float = 30.0) -> str | None:
  """Pick the one panda to drive, waiting for it to show up. None if none appeared.

  Deliberately does NOT raise when nothing is found. USB enumeration after boot, and
  re-enumeration after a flash, both take time, and an exception here would have manager
  restarting pandad.py in a tight loop with an unreadable log. The caller stays in its own
  loop instead.
  """
  forced = os.environ.get(SERIAL_ENV, "").strip()
  deadline = time.monotonic() + timeout
  serials: list[str] = []
  while True:
    serials = supported_panda_serials()
    if forced:
      if forced in serials:
        cloudlog.event("pandad.panda_selected", serial=forced, reason="env", candidates=serials)
        return forced
    elif serials:
      if len(serials) > 1:
        cloudlog.warning(f"more than one supported panda present: {serials}. Using {serials[0]}. "
                         f"Set {SERIAL_ENV} to pin a specific one.")
      cloudlog.event("pandad.panda_selected", serial=serials[0], reason="only_supported",
                     candidates=serials)
      return serials[0]
    if time.monotonic() >= deadline:
      break
    time.sleep(1.0)

  if forced:
    cloudlog.error(f"{SERIAL_ENV}={forced} never appeared among supported pandas; "
                   f"found {serials} after {timeout:.0f}s")
  else:
    cloudlog.error("no supported panda found after %.0fs. Our firmware is H7-only and an "
                   "internal F4 dos panda is deliberately skipped. Run identify-pandas.py.",
                   timeout)
  return None


def main() -> None:
  # signal pandad to close the relay and exit
  def signal_handler(signum, frame):
    cloudlog.info(f"Caught signal {signum}, exiting")
    nonlocal do_exit
    do_exit = True
    if process is not None:
      process.send_signal(signal.SIGINT)

  process = None
  do_exit = False
  signal.signal(signal.SIGINT, signal_handler)

  serial = None
  checked_health = False

  count = 0
  while not do_exit:
    try:
      if serial is None:
        serial = select_panda()
        if serial is None:
          # Nothing to do but keep looking. No exception: see select_panda's docstring.
          continue

      if not checked_health:
        checked_health = True
        # check health for lost heartbeat, on the selected device only
        try:
          with Panda(serial, cli=False) as p:
            health = p.health()
            if p.is_internal() and health["heartbeat_lost"]:
              Params().put_bool("PandaHeartbeatLost", True, block=True)
              cloudlog.event("heartbeat lost", deviceState=health)
        except Exception:
          cloudlog.exception("pandad.uncaught_exception")

      cloudlog.event("pandad.flash_and_connect", count=count, serial=serial)
      count += 1

      # No HARDWARE.reset_internal_panda() here: those GPIOs are the internal panda's, which
      # on this device is the F4 dos panda we must not touch. See the note at the top.
      if not panda_ready(serial):
        # panda_ready() is also False for "not found within 1 s" and for any exception, e.g.
        # while the panda re-enumerates after a USB drop, and flash_panda() rewrites the
        # bootstub over DFU. The red panda has no button, so losing power during that
        # 1-2 s write is unrecoverable without opening the case. On this device automatic
        # flashing is therefore OFF: retry instead, and flash only when someone sets
        # PANDAD_ALLOW_FLASH=1 deliberately. (Found by openpilot-1e's review, 2026-11-12.)
        if os.environ.get("PANDAD_ALLOW_FLASH") != "1":
          cloudlog.error(f"panda {serial} not ready; automatic flashing is disabled "
                         "(PANDAD_ALLOW_FLASH=1 to flash). Retrying.")
          time.sleep(1.0)
          continue
        flash_panda(serial)
        assert panda_ready(serial), "panda not ready after flashing"

      # run real pandad, told which panda to use (main.cc:19 reads argv[1] as the serial)
      os.environ['MANAGER_DAEMON'] = 'pandad'
      process = subprocess.Popen(["./pandad", serial],
                                 cwd=os.path.join(BASEDIR, "openpilot/selfdrive/pandad"))
      process.wait()
    except Exception:
      cloudlog.exception("pandad.uncaught_exception")


if __name__ == "__main__":
  main()
