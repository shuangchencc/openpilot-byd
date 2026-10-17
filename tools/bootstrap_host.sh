#!/usr/bin/env bash
# Explicit NETWORK preparation. No device access, system update or firmware flash.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
REFERENCE_ONLY=0
if [[ "${1:-}" == "--reference-only" ]]; then
  REFERENCE_ONLY=1
elif [[ $# -ne 0 ]]; then
  echo 'Usage: tools/bootstrap_host.sh [--reference-only]' >&2
  exit 2
fi

python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required for preparation"'
python3 - "$REFERENCE_ONLY" <<'PY'
import pathlib, platform, sys
assert platform.system() == 'Darwin' and platform.machine() == 'arm64' and not pathlib.Path('/AGNOS').exists(), 'This bootstrap is for a macOS arm64 development host only'
if sys.argv[1] != '1':
    assert tuple(map(int, platform.mac_ver()[0].split('.')[:2])) >= (26, 0), 'Pinned full-host LLVM requires macOS 26.0+'
PY
if [[ ! -x .bootstrap/bin/python ]]; then
  python3 -m venv .bootstrap
fi
.bootstrap/bin/python -m pip install --require-hashes -r requirements-bootstrap.txt
export UV_PYTHON_INSTALL_DIR="$PROJECT_ROOT/.bootstrap/python"
export UV_CACHE_DIR="$PROJECT_ROOT/.cache/uv"
export GIT_TERMINAL_PROMPT=0

# These checkouts contain source/LFS pointers only until the explicit fetch below.
git -c filter.lfs.smudge= -c filter.lfs.process= -c filter.lfs.required=false \
  submodule update --init --recursive
python3 tools/lock_upstream.py --verify --allow-lfs-pointers

.bootstrap/bin/uv python install --no-bin 3.12.7
.bootstrap/bin/uv sync --project upstream-opendbc --frozen --extra testing \
  --no-default-groups --python 3.12.7
if [[ "$REFERENCE_ONLY" == 1 ]]; then
  echo 'Reference environment prepared. Official model assets/current host build were not prepared.'
  exit 0
fi

.bootstrap/bin/uv python install --no-bin 3.12.13
.bootstrap/bin/uv sync --project upstream-openpilot --frozen --all-extras --python 3.12.13
python3 tools/bootstrap_llvm.py
export PATH="$PROJECT_ROOT/upstream-openpilot/.venv/bin:$PATH"
git -C upstream-openpilot lfs install --local --force
# Local absolute paths also let subsequent ordinary Git checks run without a
# global git-lfs executable or a modified shell PATH. Refresh after relocation.
for LFS_ACTION in clean smudge process; do
  LFS_ARGUMENT="$LFS_ACTION"
  if [[ "$LFS_ACTION" == process ]]; then LFS_ARGUMENT=filter-process; fi
  if [[ "$LFS_ACTION" != process ]]; then LFS_ARGUMENT="$LFS_ARGUMENT -- %f"; fi
  git -C upstream-openpilot config --local "filter.lfs.$LFS_ACTION" \
    "\"$PROJECT_ROOT/upstream-openpilot/.venv/bin/git-lfs\" $LFS_ARGUMENT"
done
git -C upstream-openpilot lfs pull
python3 tools/lock_upstream.py --verify
echo 'Pinned sources, Python dependencies and LFS assets prepared. No AGNOS partition image was fetched.'
echo 'Host toolchain requirements and validation commands are documented in README.md.'
