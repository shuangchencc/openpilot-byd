"""Execute the real wrapper's pre-import dispatch without importing Panda."""
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
WRAPPER = ROOT / 'openpilot/selfdrive/pandad/pandad.py'


def prefix_code():
  tree = ast.parse(WRAPPER.read_text(), filename=str(WRAPPER))
  stop = next(i for i, node in enumerate(tree.body)
              if isinstance(node, ast.ImportFrom) and node.module == 'panda')
  return compile(ast.Module(body=tree.body[:stop], type_ignores=[]), str(WRAPPER), 'exec')


class TestEntryGuard(unittest.TestCase):
  def execute(self, args, environ, execv=None):
    with patch('sys.argv', [str(WRAPPER), *args]), patch.dict('os.environ', environ, clear=True), patch('os.execv', execv):
      exec(prefix_code(), {'__name__': '__main__', '__file__': str(WRAPPER)})

  def test_supplied_profile_always_stops_before_panda_import(self):
    for profile in ('', '{}', '{malformed', 'null'):
      with self.subTest(profile=profile), self.assertRaises(SystemExit):
        self.execute([], {'SONGPLUS_PANDA_PROFILE_JSON': profile})

  def test_invalid_compat_arguments_do_not_fall_back(self):
    for args in (
      ['--compat-unknown'],
      ['--compat-offline', 'extra'],
      ['fixture:serial', '--compat-offline'],
      ['fixture:serial', '--compat-unknown'],
      ['fixture:serial', 'extra', '--compat-unknown'],
      ['--compat-offline', '--compat-unknown'],
      ['', '--compat-offline'],
      ['fixture:serial', '--compat-offline=1'],
    ):
      with self.subTest(args=args), self.assertRaises(SystemExit):
        self.execute(args, {})
    self.execute([], {})  # Exact default prefix reaches the unchanged imports.

  def test_explicit_offline_dispatch_before_all_device_imports(self):
    calls = []
    class Dispatched(Exception):
      pass
    def dispatch(path, args):
      calls.append((path,args))
      raise Dispatched
    with self.assertRaises(Dispatched):
      self.execute(['--compat-offline'], {'SONGPLUS_PANDA_PROFILE_JSON':'{}'}, dispatch)
    self.assertEqual(calls, [(str(WRAPPER.with_name('pandad')), [str(WRAPPER.with_name('pandad')), '--compat-offline'])])
    main = (WRAPPER.parent/'main.cc').read_text()
    self.assertLess(main.index('pandad_compat_offline_main()'), main.index('LOGW("starting pandad")'))
    self.assertLess(main.index('SONGPLUS_PANDA_PROFILE_JSON'), main.index('Hardware::PC()'))
    # The native CLI must scan every argument, before any startup side effect;
    # Linux acceptance also executes the corresponding multi-argument matrix.
    scan = main.index('for (int i = 1; i < argc; ++i)')
    check = main.index('std::strncmp(argv[i], "--compat-", 9)')
    self.assertLess(main.index('pandad_compat_offline_main()'), scan)
    self.assertLess(scan, check)
    for startup in ('LOGW("starting pandad")', 'Hardware::PC()', 'pandad_main_thread(serial)'):
      self.assertLess(check, main.index(startup))
