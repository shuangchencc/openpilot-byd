"""Second passive snapshot: metadata, selected power readings, static runtime files."""
import datetime
import hashlib
import json
import os
import pathlib
import struct

P = pathlib.Path


def read(p):
    try:
        return P(p).read_text(errors="replace").strip().rstrip("\x00")
    except OSError as e:
        return {"error": type(e).__name__}


out = {"collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "glibc": os.confstr("CS_GNU_LIBC_VERSION")}
out["power"] = {}
for name, keys in {
    "usb": ["present", "online", "voltage_now", "current_now", "current_max", "typec_mode", "typec_cc_orientation"],
    "bms": ["voltage_now", "current_now"],
}.items():
    p = P("/sys/class/power_supply") / name
    out["power"][name] = {k: read(p / k) for k in keys if (p / k).exists()}
out["power_reading_caveat"] = "These sysfs getters may read hardware registers; no setters used. Not host-VBUS capacity or external supply rating."
out["hwmon"] = {}
for p in P("/sys/class/hwmon").glob("hwmon*"):
    out["hwmon"][p.name] = {"name": read(p / "name")}
    if (p / "power1_input").exists():
        out["hwmon"][p.name]["power1_input"] = read(p / "power1_input")
out["gpu"] = {k: read(P("/sys/class/kgsl/kgsl-3d0") / k) for k in ["gpu_model", "chipid"] if (P("/sys/class/kgsl/kgsl-3d0") / k).exists()}
out["dt_identifiers"] = {}
for p in [P("/sys/firmware/devicetree/base/qcom,msm-id"), P("/sys/firmware/devicetree/base/qcom,board-id")]:
    if p.exists():
        b = p.read_bytes()
        out["dt_identifiers"][p.name] = list(struct.unpack(">" + "I" * (len(b) // 4), b))
out["memory_reg"] = {}
for p in P("/sys/firmware/devicetree/base").glob("memory*/reg"):
    b = p.read_bytes()
    out["memory_reg"][str(p)] = list(struct.unpack(">" + "I" * (len(b) // 4), b))
out["gadget_bindings"] = {str(p): read(p) for base in ["/sys/kernel/config/usb_gadget", "/config/usb_gadget"] for p in P(base).glob("*/UDC")}
out["param_metadata"] = {}
for name in ["DriverCameraHardwareMissing", "IsOnroad", "IsOffroad"]:
    p = P("/data/params/d") / name
    if p.exists():
        out["param_metadata"][name] = {"value": read(p), "mtime_utc": datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.timezone.utc).isoformat()}
out["packages"] = {}
for base in ["/usr/local/pyenv/versions/3.11.4/lib/python3.11/site-packages", "/data/pythonpath"]:
    for p in P(base).glob("*.dist-info/METADATA"):
        if p.parent.name.lower().startswith(("numpy-", "pycapnp-", "onnxruntime-", "tinygrad-", "pyzmq-")):
            out["packages"][str(p.parent)] = [s for s in p.read_text(errors="replace").splitlines() if s.startswith(("Name: ", "Version: ", "Requires-Python: "))]
out["import_files"] = {}
files = ["/data/openpilot/cereal/__init__.py", "/data/openpilot/cereal/log.capnp", "/data/openpilot/cereal/car.capnp", "/data/openpilot/cereal/custom.capnp", "/data/openpilot/msgq/__init__.py"]
files += [str(p) for p in P("/data/openpilot/msgq").glob("ipc_pyx*.so")]
files += [str(p) for p in P("/usr/local/pyenv/versions/3.11.4/lib/python3.11/site-packages/capnp").glob("__init__.py")]
for f in files:
    p = P(f)
    if p.is_file():
        b = p.read_bytes()
        out["import_files"][f] = {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
out["existing_message_queue_names"] = [p.name for p in P("/dev/shm").glob("*") if any(x in p.name for x in ["deviceState", "pandaStates", "managerState", "CameraState"])]
print(json.dumps(out, indent=2))
