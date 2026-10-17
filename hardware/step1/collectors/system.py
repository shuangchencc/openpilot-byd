"""Read existing OS/sysfs state only; standard library, no device-control imports.

Run via python3 -I -S -B over stdin. No files are written on the target.
USB descriptors are kernel-cached attributes; no usbfs/control transfers.
Never collect cmdline/environment wholesale, device serials, keys or GPS.
"""
import datetime
import json
import pathlib
import re
import struct
import subprocess

P = pathlib.Path


def read(path, limit=65536):
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace").rstrip("\x00\n")
    except (OSError, ValueError) as e:
        return {"error": type(e).__name__}


def fields(path, names):
    return {name: read(path / name) for name in names if (path / name).exists()}


def run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=8)
        return {"returncode": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}
    except Exception as e:
        return {"error": type(e).__name__}


out = {"collected_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "method": "passive OS/file reads; no hardware control libraries"}
out["system"] = {"uname": run(["/bin/uname", "-srmo"]), "version": read("/VERSION"), "uptime": read("/proc/uptime"), "os_release": read("/etc/os-release"), "python": run(["/usr/bin/python3", "-I", "-S", "-B", "--version"])}
cmdline = read("/proc/cmdline")
out["boot_parameters"] = [s for s in cmdline.split() if s.startswith(("androidboot.slot_suffix=", "androidboot.hardware=", "androidboot.bootdevice=", "root=", "ro", "rw", "androidboot.verifiedbootstate="))] if isinstance(cmdline, str) else cmdline
out["soc"] = fields(P("/sys/devices/soc0"), ["soc_id", "family", "machine", "revision"])
out["device_tree"] = fields(P("/sys/firmware/devicetree/base"), ["model", "compatible"])
out["memory"] = {s.split(":")[0]: s.split(":", 1)[1].strip() for s in read("/proc/meminfo").splitlines() if s.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"))}
out["usb"] = []
for p in sorted(P("/sys/bus/usb/devices").glob("*")):
    if not (p / "idVendor").exists():
        continue
    d = {"name": p.name, "path": str(p.resolve())}
    d.update(fields(p, ["idVendor", "idProduct", "product", "manufacturer", "speed", "busnum", "devpath", "version", "bMaxPower", "bConfigurationValue", "authorized", "power/runtime_status"]))
    out["usb"].append(d)
out["roles"] = {str(p): fields(p, ["mode", "power_role", "data_role", "supported_modes", "port_type", "preferred_role"]) for base in ["/sys/class/dual_role_usb", "/sys/class/typec", "/sys/class/usb_role"] for p in P(base).glob("*")}
out["udc"] = {p.name: fields(p, ["state", "current_speed", "maximum_speed", "function"]) for p in P("/sys/class/udc").glob("*")}
out["gadget_bindings"] = {str(p): read(p) for p in P("/config/usb_gadget").glob("*/UDC")}
out["usb_dt"] = []
dt = P("/sys/firmware/devicetree/base")
for p in dt.rglob("*"):
    if p.is_dir() and any(a in p.name for a in ["a600000", "a800000"]):
        for node in [p] + [q for q in p.iterdir() if q.is_dir()]:
            d = {"path": str(node), **fields(node, ["compatible", "status", "dr_mode", "maximum-speed", "phy-names"])}
            for name in ["reg", "phys", "phandle"]:
                f = node / name
                if f.exists():
                    try:
                        b = f.read_bytes()
                        d[name + "_u32"] = list(struct.unpack(">" + "I" * (len(b) // 4), b)) if len(b) % 4 == 0 else b.hex()
                    except OSError:
                        pass
            out["usb_dt"].append(d)
out["power_supply_names"] = [p.name for p in P("/sys/class/power_supply").glob("*")]
out["power_cached_metadata"] = {p.name: fields(p, ["type", "usb_type", "scope"]) for p in P("/sys/class/power_supply").glob("*")}
out["thermal"] = {p.name: fields(p, ["type", "temp"]) for p in P("/sys/class/thermal").glob("thermal_zone*")}
out["processes"] = []
for p in P("/proc").glob("[0-9]*"):
    name = read(p / "comm", 200)
    if not isinstance(name, str):
        continue
    if any(s in name for s in ["camera", "model", "panda", "boardd", "manager", "thermald", "hardwared", "loggerd", "ui", "python"]):
        status = read(p / "status", 12000)
        item = {"pid": p.name, "comm": name}
        if isinstance(status, str):
            item["status"] = [s for s in status.splitlines() if s.startswith(("State:", "VmRSS:", "VmSize:", "Threads:"))]
        try:
            item["exe"] = str((p / "exe").resolve())
        except OSError:
            pass
        out["processes"].append(item)
out["params"] = {name: read(P("/data/params/d") / name, 1000) for name in ["DriverCameraHardwareMissing", "IsOnroad", "IsOffroad", "IsDriverViewEnabled", "IsTakingSnapshot"] if (P("/data/params/d") / name).exists()}
out["nodes"] = [str(p) for pattern in ["/dev/video*", "/dev/media*", "/dev/v4l-subdev*", "/dev/spidev*", "/dev/kgsl*"] for p in P("/").glob(pattern[1:])]
out["libraries"] = run(["/usr/bin/dpkg-query", "-W", "-f=${Package}\t${Version}\n", "libc6", "libstdc++6", "python3", "libusb-1.0-0"])
out["current_kernel_camera_usb"] = run(["/bin/dmesg", "--color=never"])
if "stdout" in out["current_kernel_camera_usb"]:
    lines = out["current_kernel_camera_usb"]["stdout"].splitlines()
    lines = [s for s in lines if re.search(r"(SuperSpeed|super-speed|new (high|full|low)-speed|CAM_SENSOR.*(probe|Probe|acquire|Acquire|slot|mismatch|fail)|cam_sensor.*(probe|Probe|fail)|usb .*reset|under.?voltage|over.?current)", s)]
    out["current_kernel_camera_usb"]["stdout"] = "\n".join(s for s in lines[-160:] if not re.search(r"serial|imei|mac address", s, re.I))
print(json.dumps(out, indent=2, ensure_ascii=False))
