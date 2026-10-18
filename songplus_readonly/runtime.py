"""Reject an accidental switch to the historical research opendbc checkout."""
from functools import lru_cache
import importlib.util
from pathlib import Path
import subprocess

LAB = Path(__file__).resolve().parents[1]
TARGET = LAB / "upstream-openpilot/opendbc_repo"
TARGET_COMMIT = "b3bd79a659dae961af169a3baac723961f720133"


@lru_cache(maxsize=1)
def verify_runtime() -> dict:
  spec = importlib.util.find_spec("opendbc")
  expected = TARGET / "opendbc/__init__.py"
  if spec is None or spec.origin is None or Path(spec.origin).resolve() != expected.resolve():
    raise RuntimeError("Use upstream-openpilot/.venv/bin/python with the pinned target opendbc, not upstream-opendbc")
  head = subprocess.check_output(
    ["git", "--no-optional-locks", "-C", str(TARGET), "rev-parse", "HEAD"], text=True).strip()
  if head != TARGET_COMMIT:
    raise RuntimeError(f"Target opendbc pin changed: {head}")
  subprocess.run(["git", "--no-optional-locks", "-C", str(TARGET), "diff", "--quiet", "HEAD", "--"], check=True)
  return {"opendbc_path": str(TARGET), "opendbc_commit": head, "tracked_source_clean": True}
