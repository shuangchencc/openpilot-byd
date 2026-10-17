#!/usr/bin/env python3
"""Build and test the pinned official stack on this macOS host, offline.

Run tools/bootstrap_host.sh explicitly before first use. This command never
downloads dependencies, starts openpilot services, or accesses/flashes devices.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "upstream-openpilot"
OUTPUT = ROOT / "results/upstream-current"
PYTHON = UPSTREAM / ".venv/bin/python"
HOST_TARGETS = ["openpilot/common", "openpilot/cereal", "msgq_repo",
                "openpilot/selfdrive/controls/lib/longitudinal_mpc_lib", "openpilot/selfdrive/locationd", "panda"]
TEST_GROUPS = {
    "opendbc_tests": ["opendbc.safety.tests.test_byd", "opendbc.can.tests.test_packer_parser", "opendbc.can.tests.test_dbc_parser"],
    "openpilot_tests": [
        "openpilot.common.tests.test_file_helpers", "openpilot.common.tests.test_markdown",
        "openpilot.common.tests.test_params", "openpilot.common.tests.test_qrcode", "openpilot.common.tests.test_simple_kalman",
        "openpilot.selfdrive.controls.tests.test_longcontrol", "openpilot.selfdrive.controls.tests.test_latcontrol",
        "openpilot.selfdrive.controls.tests.test_latcontrol_torque_buffer", "openpilot.selfdrive.controls.tests.test_torqued_lat_accel_offset",
        "openpilot.selfdrive.car.tests.test_cruise_speed", "openpilot.selfdrive.selfdrived.tests.test_state_machine",
        "openpilot.selfdrive.selfdrived.tests.test_alertmanager", "openpilot.selfdrive.selfdrived.tests.test_alerts",
    ],
    "mpc_tests": ["openpilot.selfdrive.controls.tests.test_following_distance"],
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-only", action="store_true", help="Only build core host libraries and H7 firmware; omit model compilation")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 16:
        parser.error("--jobs must be between 1 and 16")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []
    env = dict(os.environ)
    env.update({"PATH": str(PYTHON.parent) + os.pathsep + env.get("PATH", ""), "UV_OFFLINE": "1",
                "UV_PYTHON_INSTALL_DIR": str(ROOT / ".bootstrap/python")})
    # Never use an inherited development PYTHONPATH to silently select older code.
    env.pop("PYTHONPATH", None)
    sandbox = ["/usr/bin/sandbox-exec", "-f", str(ROOT / "tools/offline-network.sb")]

    def run(name: str, command: list[str], *, cwd: Path = UPSTREAM, tests: bool = False,
            timeout: int = 900, use_sandbox: bool = True) -> dict:
        wrapped = (sandbox if use_sandbox else []) + list(map(str, command))
        started = time.monotonic()
        item = {"name": name, "command": list(map(str, command)), "cwd": str(cwd.relative_to(ROOT)),
                "remote_network_denied": use_sandbox}
        stdout, stderr = "", ""
        try:
            proc = subprocess.Popen(wrapped, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
                item["timeout_seconds"] = timeout
            item.update({"exit_code": proc.returncode, "status": "PASS" if proc.returncode == 0 else "FAIL"})
            if tests:
                count = re.search(r"Ran (\d+) tests? in", stderr)
                skipped = re.search(r"skipped=(\d+)", stderr)
                item["tests_run"] = int(count[1]) if count else 0
                item["tests_skipped"] = int(skipped[1]) if skipped else 0
                item["tests_passed"] = item["tests_run"] - item["tests_skipped"] if proc.returncode == 0 else None
                item["skip_details"] = [line for line in stderr.splitlines() if " ... skipped " in line]
                if not item["tests_run"]:
                    item["status"] = "FAIL"
        except OSError as exc:
            item.update({"status": "UNAVAILABLE", "error": str(exc)})
        for suffix, content in (("stdout", stdout), ("stderr", stderr)):
            path = OUTPUT / f"{name}.{suffix}.log"
            path.write_text(content)
            item[suffix + "_log"] = str(path.relative_to(ROOT))
            item[suffix + "_sha256"] = sha256(path)
        item["elapsed_seconds"] = round(time.monotonic() - started, 3)
        checks.append(item)
        print(f"{item['status']} {name}", flush=True)
        return item

    def finish() -> int:
        statuses = {check["status"] for check in checks}
        status = "FAIL" if "FAIL" in statuses else "UNAVAILABLE" if "UNAVAILABLE" in statuses else "PASS"
        report = {
            "schema": "songplus-upstream-host-validation/v1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": status, "scope": "core_host_build_and_selected_official_tests" if args.core_only else "official_minimal_macos_build_and_selected_official_tests",
            "host": {"system": platform.system(), "machine": platform.machine(), "platform": platform.platform()},
            "upstream_lock_sha256": sha256(ROOT / "upstream-lock.json") if (ROOT / "upstream-lock.json").exists() else None,
            "checks": checks, "tests_run": sum(c.get("tests_run", 0) for c in checks),
            "tests_skipped": sum(c.get("tests_skipped", 0) for c in checks),
            "tests_passed": sum(c.get("tests_passed") or 0 for c in checks),
            "network": "Remote outbound network denied by macOS sandbox; local IPC/localhost allowed. Bootstrap is a separate online action.",
            "panda_build_configuration": "Official default development build: debug signature and ALLOW_DEBUG; no production/release or installed-board compatibility claim",
            "not_validated": ["C3XL/AGNOS kernel, camera, ISP, QCOM GPU and realtime performance",
                              "Linux/AGNOS camerad and pandad binaries (excluded by official Darwin build), desktop extras omitted by --minimal",
                              "Song PLUS protocol, actuation, vehicle controller or vehicle-specific safety",
                              "Installed Panda MCU/board identity and firmware compatibility (upstream builds H7 only)",
                              "Firmware flashing, bootstub replacement, system installation or device recovery",
                              "Complete upstream test suite, hardware-in-loop tests or on-road validation"],
            "actuation_allowed": False, "device_accessed": False, "services_started": False,
        }
        (OUTPUT / "validation-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"{status}: {report['tests_passed']} passed, {report['tests_skipped']} skipped; {OUTPUT / 'validation-result.json'}")
        return 0 if status == "PASS" else 1

    if Path("/AGNOS").exists() or platform.system() != "Darwin" or platform.machine() != "arm64":
        checks.append({"name": "host_guard", "status": "UNAVAILABLE", "reason": "This validated wrapper requires macOS arm64, never a comma device"})
        return finish()
    if not PYTHON.exists() or not (ROOT / "upstream-lock.json").exists():
        checks.append({"name": "bootstrap", "status": "UNAVAILABLE", "reason": "Pinned checkout and explicit host bootstrap required"})
        return finish()
    if run("source_lock_before", [str(PYTHON), str(ROOT / "tools/lock_upstream.py"), "--verify"], cwd=ROOT)["status"] != "PASS":
        return finish()
    uv = ROOT / ".bootstrap/bin/uv"
    uv_version_code = "import subprocess; v=subprocess.check_output([" + repr(str(uv)) + ", '--version'],text=True); print(v,end=''); assert v.split()[1]=='0.13.1', v"
    if run("uv_version", [str(PYTHON), "-c", uv_version_code])["status"] != "PASS":
        return finish()
    if run("frozen_environment_check", [str(uv), "sync", "--project", str(UPSTREAM), "--frozen", "--all-extras",
                                         "--check", "--offline", "--python", "3.12.13"])["status"] != "PASS":
        return finish()
    runtime_code = '''import importlib.metadata as m, json, pathlib, platform, sys
assert platform.python_version() == pathlib.Path('.python-version').read_text().strip()
print(json.dumps({"python": sys.version, "executable": sys.executable,
 "packages": sorted([{"name": d.metadata["Name"], "version": d.version} for d in m.distributions()], key=lambda d:d["name"].lower())}, indent=2))'''
    if run("python_environment", [str(PYTHON), "-c", runtime_code])["status"] != "PASS":
        return finish()
    compiler_code = '''import json, subprocess
commands = [['clang', '--version'], ['xcodebuild', '-version'], ['xcrun', '--show-sdk-path'], ['arm-none-eabi-gcc', '--version']]
print(json.dumps([{ "command": c, "output": subprocess.check_output(c, text=True).strip() } for c in commands], indent=2))'''
    if run("host_compilers", [str(PYTHON), "-c", compiler_code])["status"] != "PASS":
        return finish()
    # An immediate EPERM on a TEST-NET address proves the wrapper is active.
    probe = "import errno,socket; s=socket.socket(); s.settimeout(1); code=s.connect_ex(('203.0.113.1',443)); s.close(); assert code==errno.EPERM, code; print('remote network denied')"
    if run("offline_network_guard", [str(PYTHON), "-c", probe])["status"] != "PASS":
        return finish()

    if not args.core_only:
        toolchain_lock = ROOT / "host-toolchain-lock.json"
        try:
            toolchain = json.loads(toolchain_lock.read_text())
            for file in toolchain["files"]:
                path = ROOT / file["path"]
                if sha256(path) != file["sha256"]:
                    raise ValueError(f"Host toolchain hash mismatch: {path}")
            env["LLVM_PATH"] = str(ROOT / toolchain["llvm_library"])
            checks.append({"name": "host_toolchain", "status": "PASS", "manifest_sha256": sha256(toolchain_lock)})
        except (OSError, ValueError, KeyError) as exc:
            checks.append({"name": "host_toolchain", "status": "UNAVAILABLE", "reason": str(exc)})
            return finish()

    build = [str(PYTHON.parent / "scons"), f"-j{args.jobs}", "--minimal", "--site-dir=" + str(ROOT / "tools/scons_site")]
    if args.core_only:
        build.extend(HOST_TARGETS)
    built = run("host_build", build)
    if built["status"] == "PASS":
        firmware = ["panda/board/obj/panda_h7/main.elf", "panda/board/obj/panda_h7/bootstub.elf",
                    "panda/board/obj/body_h7/main.elf", "panda/board/obj/body_h7/bootstub.elf"]
        run("panda_h7_elf_size", [str(PYTHON.parent / "arm-none-eabi-size"), *firmware])
        artifacts = firmware + ["panda/board/obj/panda_h7.bin.signed", "panda/board/obj/body_h7.bin.signed"]
        if not args.core_only:
            artifacts += [str(path.relative_to(UPSTREAM)) for path in
                          sorted((UPSTREAM / "openpilot/selfdrive/modeld/models").glob("*_tinygrad.pkl*"))
                          if path.is_file() and not path.name.startswith("big_")]
        checks.append({"name": "local_build_outputs", "status": "PASS", "meaning": "Local development artifacts only; never installation or device-compatibility evidence",
                       "files": [{"path": str((UPSTREAM / name).relative_to(ROOT)), "size": (UPSTREAM / name).stat().st_size,
                                  "sha256": sha256(UPSTREAM / name)} for name in artifacts]})
    for name, modules in TEST_GROUPS.items():
        if name == "opendbc_tests" or built["status"] == "PASS":
            run(name, [str(PYTHON), "-m", "unittest", "-v", *modules], tests=True, timeout=240)
        else:
            checks.append({"name": name, "status": "UNAVAILABLE", "reason": "Host build did not pass"})
    run("source_lock_after", [str(PYTHON), str(ROOT / "tools/lock_upstream.py"), "--verify"], cwd=ROOT)
    return finish()


if __name__ == "__main__":
    raise SystemExit(main())
