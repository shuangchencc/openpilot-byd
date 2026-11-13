"""When pandad.py may flash the red panda, and which route it takes.

No hardware: Panda and PandaDFU are replaced by a small model of one red panda (which firmware
and bootstub it carries, which mode it is in), and every action that would touch the device is
recorded. The real ensure_panda() / flash_panda() / panda_state() run against it.
Plain unittest: the device venv has no pytest.

  python -m unittest openpilot.selfdrive.pandad.tests.test_pandad_flash_policy
"""
import ast
import inspect
import unittest
from unittest import mock

from openpilot.selfdrive.pandad import pandad

SERIAL = "1d001b000c51303436323838"
DFU_SERIAL = "385332533430"

OURS, VENDOR = "ours", "vendor"
VERSIONS = {OURS: "DEV-unknown-DEBUG", VENDOR: "DEV-719e0dd0-RELEAS"}


class RedPanda:
  """State of one panda and a log of everything done to it."""

  def __init__(self, app=OURS, bootstub=OURS, mode="app", present=True, app_current=True):
    self.app = app                  # whose app is in flash (None: nothing valid)
    self.bootstub = bootstub        # whose bootstub is in flash
    self.mode = mode                # "app" | "bootstub" | "dfu"
    self.present = present
    self.app_current = app_current  # is the app in flash the build we ship?
    self.connect_error = None
    self.dfu_reachable = True
    self.actions = []

  def boot(self):
    """What the bootstub does after a reset: start the app if it accepts its signature."""
    self.mode = "app" if self.app is not None and self.app == self.bootstub else "bootstub"


def make_fakes(world: RedPanda):
  class FakePanda:
    def __init__(self, serial=None, cli=True):
      assert serial == SERIAL, "pandad.py must address the panda by serial"
      if world.connect_error is not None:
        raise world.connect_error
      if not world.present or world.mode == "dfu":
        raise Exception("failed to connect to panda")

    def __enter__(self):
      return self

    def __exit__(self, *args):
      pass

    @property
    def bootstub(self):
      return world.mode == "bootstub"

    def get_version(self):
      return VERSIONS[world.bootstub if world.mode == "bootstub" else world.app]

    def up_to_date(self):
      return world.app == OURS and world.app_current

    def get_dfu_serial(self):
      return DFU_SERIAL

    def flash(self):
      if self.up_to_date():      # the library returns early here, also in the bootstub
        return
      world.actions.append("flash_app")
      world.app, world.app_current = OURS, True
      world.boot()

    def reset(self, enter_bootstub=False, enter_bootloader=False):
      if enter_bootloader:
        world.actions.append("enter_dfu")
        # a RELEASE app ignores this request; a bootstub and a DEBUG app honour it
        ignored = world.mode == "app" and world.app == VENDOR
        if not ignored and world.dfu_reachable:
          world.mode = "dfu"
      elif enter_bootstub:
        world.actions.append("enter_bootstub")
        world.mode = "bootstub"
      else:
        world.actions.append("reset")
        world.boot()

    @staticmethod
    def wait_for_panda(serial, timeout):
      return world.present and world.mode != "dfu"

    @staticmethod
    def wait_for_dfu(dfu_serial, timeout=None):
      assert dfu_serial == DFU_SERIAL
      return world.mode == "dfu"

  class FakePandaDFU:
    def __init__(self, dfu_serial):
      assert dfu_serial == DFU_SERIAL, "PandaDFU must be given this panda's own DFU serial"
      assert world.mode == "dfu"

    def __enter__(self):
      return self

    def __exit__(self, *args):
      pass

    def recover(self):
      world.actions.append("dfu_write_bootstub")
      world.bootstub = OURS
      world.boot()

  return FakePanda, FakePandaDFU


class FlashPolicyTest(unittest.TestCase):
  def run_ensure(self, world, allow_flash):
    fake_panda, fake_dfu = make_fakes(world)
    env = {"PANDAD_ALLOW_FLASH": "1"} if allow_flash else {}
    with mock.patch.object(pandad, "Panda", fake_panda), mock.patch.object(pandad, "PandaDFU", fake_dfu), \
         mock.patch.dict("os.environ", env, clear=False):
      if not allow_flash:
        import os
        os.environ.pop("PANDAD_ALLOW_FLASH", None)
      return pandad.ensure_panda(SERIAL)

  # ---- nothing to do

  def test_ready_panda_is_left_alone(self):
    world = RedPanda()
    self.assertTrue(self.run_ensure(world, allow_flash=True))
    self.assertEqual(world.actions, [])

  # ---- never flash what we cannot see

  def test_absent_panda_is_never_flashed(self):
    world = RedPanda(present=False)
    self.assertFalse(self.run_ensure(world, allow_flash=True))
    self.assertEqual(world.actions, [])

  def test_failed_check_is_never_flashed(self):
    world = RedPanda(app=VENDOR, bootstub=VENDOR)
    world.connect_error = Exception("LIBUSB_ERROR_NO_DEVICE")
    self.assertFalse(self.run_ensure(world, allow_flash=True))
    self.assertEqual(world.actions, [])

  def test_panda_that_reappears_up_to_date_is_not_flashed(self):
    # the check said "needs flash", but by the time flash_panda() connects it is fine
    world = RedPanda(app_current=False)
    fake_panda, fake_dfu = make_fakes(world)
    with mock.patch.object(pandad, "Panda", fake_panda), mock.patch.object(pandad, "PandaDFU", fake_dfu):
      self.assertEqual(pandad.panda_state(SERIAL), pandad.NEEDS_FLASH)
      world.app_current = True
      pandad.flash_panda(SERIAL)
    self.assertEqual(world.actions, [])

  # ---- the master switch

  def test_out_of_date_panda_is_not_flashed_without_the_switch(self):
    for world in (RedPanda(app=VENDOR, bootstub=VENDOR), RedPanda(app_current=False),
                  RedPanda(app=None, mode="bootstub")):
      self.assertFalse(self.run_ensure(world, allow_flash=False))
      self.assertEqual(world.actions, [])

  def test_switch_must_be_exactly_one(self):
    world = RedPanda(app_current=False)
    fake_panda, fake_dfu = make_fakes(world)
    for value in ("", "0", "true", "yes"):
      with mock.patch.object(pandad, "Panda", fake_panda), mock.patch.object(pandad, "PandaDFU", fake_dfu), \
           mock.patch.dict("os.environ", {"PANDAD_ALLOW_FLASH": value}):
        self.assertFalse(pandad.ensure_panda(SERIAL))
    self.assertEqual(world.actions, [])

  # ---- which route

  def test_our_firmware_gets_an_app_only_update(self):
    world = RedPanda(app_current=False)                 # our older build, our bootstub
    self.assertTrue(self.run_ensure(world, allow_flash=True))
    self.assertEqual(world.actions, ["flash_app"])
    self.assertEqual((world.mode, world.bootstub), ("app", OURS))

  def test_stuck_in_our_bootstub_gets_an_app_only_update(self):
    world = RedPanda(app=None, mode="bootstub")
    self.assertTrue(self.run_ensure(world, allow_flash=True))
    self.assertEqual(world.actions, ["flash_app"])

  def test_vendor_firmware_goes_through_dfu_first(self):
    world = RedPanda(app=VENDOR, bootstub=VENDOR)
    self.assertTrue(self.run_ensure(world, allow_flash=True))
    # bootstub -> DFU in two steps, bootstub replaced, and only then our app
    self.assertEqual(world.actions, ["enter_bootstub", "enter_dfu", "dfu_write_bootstub", "flash_app"])
    self.assertEqual((world.mode, world.app, world.bootstub), ("app", OURS, OURS))

  def test_stuck_in_vendor_bootstub_goes_through_dfu(self):
    world = RedPanda(app=None, bootstub=VENDOR, mode="bootstub")
    self.assertTrue(self.run_ensure(world, allow_flash=True))
    self.assertEqual(world.actions, ["enter_dfu", "dfu_write_bootstub", "flash_app"])

  def test_app_only_route_falls_back_to_dfu_when_the_app_does_not_start(self):
    # a DEBUG-looking app over a bootstub that will not take ours: the heuristic is wrong here
    world = RedPanda(app=OURS, bootstub=VENDOR, mode="app", app_current=False)
    self.assertTrue(self.run_ensure(world, allow_flash=True))
    self.assertEqual(world.actions, ["flash_app", "enter_dfu", "dfu_write_bootstub"])
    self.assertEqual((world.mode, world.bootstub), ("app", OURS))

  # ---- failure handling

  def test_no_bootstub_write_if_dfu_is_never_reached(self):
    world = RedPanda(app=VENDOR, bootstub=VENDOR)
    world.dfu_reachable = False
    with self.assertRaises(AssertionError):
      self.run_ensure(world, allow_flash=True)
    self.assertNotIn("dfu_write_bootstub", world.actions)
    self.assertNotIn("flash_app", world.actions)

  def test_version_heuristic(self):
    self.assertTrue(pandad._accepts_our_app("DEV-unknown-DEBUG"))
    self.assertTrue(pandad._accepts_our_app("DEV-a5919516-DEBUG\x00"))
    self.assertFalse(pandad._accepts_our_app("DEV-719e0dd0-RELEAS"))
    self.assertFalse(pandad._accepts_our_app("DEV-719e0dd0-RELEASE"))
    self.assertFalse(pandad._accepts_our_app(""))


class DosGuardStaticTest(unittest.TestCase):
  """The internal F4 dos panda is reset through GPIO in upstream's pandad.py. Ours must not."""

  def test_no_gpio_or_hardware_calls(self):
    tree = ast.parse(inspect.getsource(pandad))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {a.name.split(".")[-1] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    self.assertNotIn("HARDWARE", names | imported)
    for forbidden in ("reset_internal_panda", "recover_internal_panda", "gpio_set", "gpio_init"):
      self.assertNotIn(forbidden, names | attrs | imported)

  def test_recover_is_only_called_on_a_serial_bound_dfu_handle(self):
    # Panda.recover() / PandaDFU(None) would pick "whatever is in DFU"; flash_panda must not
    src = inspect.getsource(pandad.flash_panda)
    self.assertIn("PandaDFU(dfu_serial)", src)
    self.assertNotIn("PandaDFU()", src)
    self.assertNotIn("PandaDFU(None)", src)
    self.assertNotIn("p.recover(", src)


if __name__ == "__main__":
  unittest.main()
