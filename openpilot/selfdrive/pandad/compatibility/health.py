"""Pure, pinned Panda health codecs. No discovery, I/O, or control authorization.

The layouts are pinned to panda a5919516 + opendbc b3bd79a6. SP098 health16
is an explicitly selected offline format, not a negotiation fallback. Raw wire
values and capability-aware physical values are deliberately separate objects.
"""
from dataclasses import dataclass
from enum import Enum
import math
import struct
from types import MappingProxyType
from collections.abc import Mapping


class HealthCodecError(ValueError):
  pass


class ProtocolProfile(Enum):
  CURRENT = "panda-a5919516"
  SP098_OFFLINE = "sp098-health16-can4-canhealth5-offline"


CURRENT_VERSION_BYTES = bytes.fromhex("f512bcff76f2ab75")
LEGACY_VERSION_BYTES = bytes.fromhex("100405")
CURRENT_HEALTH_FORMAT = "<IHHIIIIIHBBHBHBBHHHHB"
LEGACY_HEALTH_FORMAT = "<IIIIIIIIBBBBBHBBBHfBBHBHHB"
CAN_HEALTH_FORMAT = "<BIBBBBBBBBIIIIIIIHHBBBIIII"
CURRENT_HEALTH_FIELDS = (
  "uptime", "voltage", "current", "safety_tx_blocked", "safety_rx_invalid",
  "tx_buffer_overflow", "rx_buffer_overflow", "faults", "flags", "car_harness_status",
  "safety_mode", "safety_param", "fault_status", "alternative_experience", "interrupt_load",
  "fan_power", "spi_error_count", "sbu1_voltage_mV", "sbu2_voltage_mV", "sound_output_level", "temperature",
)
LEGACY_HEALTH_FIELDS = (
  "uptime", "voltage", "current", "safety_tx_blocked", "safety_rx_invalid",
  "tx_buffer_overflow", "rx_buffer_overflow", "faults", "ignition_line", "ignition_can",
  "controls_allowed", "car_harness_status", "safety_mode", "safety_param", "fault_status",
  "power_save_enabled", "heartbeat_lost", "alternative_experience", "interrupt_load", "fan_power",
  "safety_rx_checks_invalid", "spi_checksum_error_count", "fan_stall_count", "sbu1_voltage_mV",
  "sbu2_voltage_mV", "som_reset_triggered",
)
CAN_HEALTH_FIELDS = (
  "bus_off", "bus_off_cnt", "error_warning", "error_passive", "last_error", "last_stored_error",
  "last_data_error", "last_data_stored_error", "receive_error_cnt", "transmit_error_cnt",
  "total_error_cnt", "total_tx_lost_cnt", "total_rx_lost_cnt", "total_tx_cnt", "total_rx_cnt",
  "total_fwd_cnt", "total_tx_checksum_error_cnt", "can_speed", "can_data_speed", "canfd_enabled",
  "brs_enabled", "canfd_non_iso", "irq0_call_rate", "irq1_call_rate", "irq2_call_rate", "can_core_reset_cnt",
)
HEALTH_FLAG_NAMES = (
  "ignition_line", "ignition_can", "controls_allowed", "power_save_enabled", "heartbeat_lost",
  "safety_rx_checks_invalid", "som_reset_triggered", "nmi_reset", "hardfault_reset",
)
KNOWN_HEALTH_FLAGS = (1 << len(HEALTH_FLAG_NAMES)) - 1
# Current board/sys/sys.h deliberately has gaps; a reserved bit is not a known fault.
KNOWN_FAULT_BITS = frozenset((0, 1, 2, 3, 4, 5, 7, 8, 15, 18, 20, 21, 22, 23, 25, 26, 27))
KNOWN_FAULT_MASK = sum(1 << bit for bit in KNOWN_FAULT_BITS)
# Knowing an enum value does not authorize that safety mode (notably ELM327/ALLOUTPUT).
CURRENT_SAFETY_MODES = frozenset((0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 15, 17, 18,
                                 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 30, 31, 33, 34, 35, 38))
LEGACY_SAFETY_MODES = frozenset((0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 15, 17, 18,
                                19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 32))
CURRENT_ALTERNATIVE_EXPERIENCE_MASK = 2 | 8 | 16


@dataclass(frozen=True)
class ProtocolVersion:
  profile: ProtocolProfile
  health: int
  can: int
  can_health: int | None

  @property
  def control_protocol_supported(self) -> bool:
    """Protocol identity only. This never grants permission to transmit."""
    return (type(self) is ProtocolVersion and type(self.profile) is ProtocolProfile
            and self.profile is ProtocolProfile.CURRENT and type(self.health) is int
            and type(self.can) is int and self.can_health is None and self == CURRENT_VERSION)


CURRENT_VERSION = ProtocolVersion(ProtocolProfile.CURRENT, 0xFFBC12F5, 0x75ABF276, None)
LEGACY_VERSION = ProtocolVersion(ProtocolProfile.SP098_OFFLINE, 16, 4, 5)


def _profile(profile: ProtocolProfile) -> ProtocolProfile:
  if type(profile) is not ProtocolProfile:
    raise HealthCodecError("an explicit ProtocolProfile enum is required")
  return profile


def _version(version: ProtocolVersion) -> ProtocolVersion:
  if type(version) is not ProtocolVersion or type(version.health) is not int or type(version.can) is not int:
    raise HealthCodecError("invalid protocol version object")
  _profile(version.profile)
  expected = CURRENT_VERSION if version.profile is ProtocolProfile.CURRENT else LEGACY_VERSION
  if version != expected or (version.can_health is not None and type(version.can_health) is not int):
    raise HealthCodecError("unknown protocol version; no layout fallback")
  return version


def _bytes(data: bytes, size: int) -> bytes:
  if type(data) is not bytes or len(data) != size:
    raise HealthCodecError(f"expected exactly {size} immutable bytes")
  return data


def decode_versions(data: bytes, *, profile: ProtocolProfile = ProtocolProfile.CURRENT) -> ProtocolVersion:
  profile = _profile(profile)
  expected = CURRENT_VERSION_BYTES if profile is ProtocolProfile.CURRENT else LEGACY_VERSION_BYTES
  if _bytes(data, len(expected)) != expected:
    raise HealthCodecError("version mismatch; legacy health17 and unknown hashes are unsupported")
  return CURRENT_VERSION if profile is ProtocolProfile.CURRENT else LEGACY_VERSION


def encode_versions(version: ProtocolVersion) -> bytes:
  version = _version(version)
  return CURRENT_VERSION_BYTES if version.profile is ProtocolProfile.CURRENT else LEGACY_VERSION_BYTES


@dataclass(frozen=True)
class WireHealth:
  version: ProtocolVersion
  values: Mapping[str, int | float]

  def __post_init__(self):
    object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class WireCanHealth:
  version: ProtocolVersion
  values: Mapping[str, int]

  def __post_init__(self):
    object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


def _finite(value: int | float, field: str) -> float:
  if type(value) not in (int, float):
    raise HealthCodecError(f"{field}: expected a finite number, not bool/coerced text")
  try:
    finite = math.isfinite(value)
  except (OverflowError, ValueError):
    finite = False
  if not finite:
    raise HealthCodecError(f"{field}: non-finite or unrepresentable number")
  return float(value)


def _uint(value: int, bits: int, field: str) -> None:
  if type(value) is not int or not 0 <= value < (1 << bits):
    raise HealthCodecError(f"{field}: expected uint{bits}, no wrap/truncation/coercion")


def _fields(values: Mapping, names: tuple, fmt: str) -> None:
  if not isinstance(values, Mapping) or set(values) != set(names):
    raise HealthCodecError("packet fields must exactly match the selected layout")
  for field, code in zip(names, fmt[1:], strict=True):
    if code == "f":
      _finite(values[field], field)
    else:
      _uint(values[field], {"B": 8, "H": 16, "I": 32}[code], field)


def _enum(value: int, allowed, name: str) -> None:
  if value not in allowed:
    raise HealthCodecError(f"unknown {name}: {value}")


def _layout(version: ProtocolVersion) -> tuple[str, tuple[str, ...]]:
  if _version(version).profile is ProtocolProfile.CURRENT:
    return CURRENT_HEALTH_FORMAT, CURRENT_HEALTH_FIELDS
  return LEGACY_HEALTH_FORMAT, LEGACY_HEALTH_FIELDS


def _validate_health(packet: WireHealth) -> None:
  fmt, names = _layout(packet.version)
  values = packet.values
  _fields(values, names, fmt)
  _enum(values["car_harness_status"], (0, 1, 2), "harness status")
  _enum(values["fault_status"], (0, 1, 2), "fault status")
  if values["fan_power"] > 100:
    raise HealthCodecError("fan power exceeds 100 percent")
  if packet.version.profile is ProtocolProfile.CURRENT:
    _enum(values["safety_mode"], CURRENT_SAFETY_MODES, "current safety mode")
    if values["flags"] & ~KNOWN_HEALTH_FLAGS:
      raise HealthCodecError("unknown mandatory health flag bits")
    if values["faults"] & ~KNOWN_FAULT_MASK:
      raise HealthCodecError("unknown mandatory current fault bits")
    if values["alternative_experience"] & ~CURRENT_ALTERNATIVE_EXPERIENCE_MASK:
      raise HealthCodecError("unknown current alternative-experience bits")
  else:
    _enum(values["safety_mode"], LEGACY_SAFETY_MODES, "legacy safety mode")
    for name in HEALTH_FLAG_NAMES[:7]:
      _enum(values[name], (0, 1), name)
    if not 0.0 <= values["interrupt_load"] <= 1.0:
      raise HealthCodecError("legacy interrupt load must be finite and within [0,1]")
    # Old MCU fault/alternative definitions were not recovered. Preserve their
    # raw bits only in this offline profile; never infer current safety semantics.


def decode_health(data: bytes, version: ProtocolVersion) -> WireHealth:
  fmt, names = _layout(version)
  packet = WireHealth(version, dict(zip(names, struct.unpack(fmt, _bytes(data, struct.calcsize(fmt))), strict=True)))
  _validate_health(packet)
  return packet


def encode_health(packet: WireHealth) -> bytes:
  if type(packet) is not WireHealth:
    raise HealthCodecError("expected WireHealth")
  _validate_health(packet)
  fmt, names = _layout(packet.version)
  return struct.pack(fmt, *(packet.values[name] for name in names))


@dataclass(frozen=True)
class HealthCapabilities:
  """None means unverified, False means absent; neither exposes fake zeroes."""
  voltage: bool | None = None
  current: bool | None = None
  harness: bool | None = None
  fan: bool | None = None
  sound: bool | None = None
  temperature: bool | None = None
  spi_error_count: bool | None = None

  def __post_init__(self):
    for name in self.__dataclass_fields__:
      if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
        raise HealthCodecError(f"{name}: capability must be bool or None")


def decode_f4_capabilities(data: bytes) -> HealthCapabilities:
  """Decode the exact P5 reference contract; connection binding lives in C++.

  This pure codec alone grants no validity, role or control authorization.
  """
  expected = bytes.fromhex("46344331010010001f00000007000000")
  if _bytes(data, len(expected)) != expected:
    raise HealthCodecError("unknown F4 capability contract; no fallback")
  return HealthCapabilities(voltage=True, current=False, harness=True, fan=True,
                            sound=False, temperature=True, spi_error_count=False)


@dataclass(frozen=True)
class HealthView:
  raw: WireHealth
  values: Mapping[str, int | float | bool | None]
  capabilities: HealthCapabilities

  def __post_init__(self):
    object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

  @property
  def control_protocol_supported(self) -> bool:
    return self.raw.version.control_protocol_supported


def normalize_health(packet: WireHealth, capabilities: HealthCapabilities) -> HealthView:
  if type(packet) is not WireHealth or type(capabilities) is not HealthCapabilities:
    raise HealthCodecError("WireHealth and explicit HealthCapabilities required")
  _validate_health(packet)
  raw = packet.values
  current = packet.version.profile is ProtocolProfile.CURRENT
  values = {name: raw[name] for name in ("uptime", "safety_tx_blocked", "safety_rx_invalid", "tx_buffer_overflow",
                                       "rx_buffer_overflow", "faults", "safety_mode", "safety_param", "fault_status",
                                       "alternative_experience")}
  values.update({name: bool(raw["flags"] & (1 << bit)) if current else (bool(raw[name]) if name in raw else None)
                 for bit, name in enumerate(HEALTH_FLAG_NAMES)})
  values.update({
    "voltage_v": raw["voltage"] / 1000.0 if capabilities.voltage is True else None,
    "current_a": raw["current"] / 1000.0 if capabilities.current is True else None,
    "car_harness_status": raw["car_harness_status"] if capabilities.harness is True else None,
    "sbu1_voltage_v": raw["sbu1_voltage_mV"] / 1000.0 if capabilities.harness is True else None,
    "sbu2_voltage_v": raw["sbu2_voltage_mV"] / 1000.0 if capabilities.harness is True else None,
    "interrupt_load": raw["interrupt_load"] / 255.0 if current else raw["interrupt_load"],
    "fan_power_percent": raw["fan_power"] if capabilities.fan is True else None,
    "fan_stall_count": raw["fan_stall_count"] if not current and capabilities.fan is True else None,
    "spi_error_count": raw["spi_error_count"] if current and capabilities.spi_error_count is True else None,
    "spi_checksum_error_count": raw["spi_checksum_error_count"] if not current else None,
    # Firmware amplitude units, not dB or a proof of audible output.
    "sound_output_level": raw["sound_output_level"] if current and capabilities.sound is True else None,
    "temperature_c": raw["temperature"] - 40.0 if current and capabilities.temperature is True else None,
  })
  if capabilities.harness is not True:
    values["ignition_line"] = None
  return HealthView(packet, values, capabilities)


def _f32(value: float, name: str) -> float:
  try:
    result = struct.unpack("<f", struct.pack("<f", value))[0]
  except (OverflowError, struct.error) as exc:
    raise HealthCodecError(f"{name}: cannot represent finite float32") from exc
  if not math.isfinite(result):
    raise HealthCodecError(f"{name}: cannot represent finite float32")
  return result


def quantize_current_measurements(*, voltage_mv: int, current_ma: int, interrupt_load: float, temperature_c: float) -> Mapping[str, int]:
  """Model main_comms.h saturation/float32 rounding for offline fixture generation.

  This is not lossless conversion of legacy packets or a way to invent sensors.
  Raw encode_health itself rejects out-of-width values instead of saturating them.
  """
  _uint(voltage_mv, 32, "voltage_mv")
  _uint(current_ma, 32, "current_ma")
  load = min(1.0, max(0.0, _f32(_finite(interrupt_load, "interrupt_load"), "interrupt_load")))
  temp = min(214.5, max(-40.0, _f32(_finite(temperature_c, "temperature_c"), "temperature_c")))
  return MappingProxyType({
    "voltage": min(voltage_mv, 65535),
    "current": min(current_ma, 65535),
    "interrupt_load": int(_f32(_f32(load * 255.0, "scaled load") + 0.5, "rounded load")),
    "temperature": int(_f32(_f32(temp + 40.0, "offset temperature") + 0.5, "rounded temperature")),
  })


def _validate_can_health(packet: WireCanHealth) -> None:
  _version(packet.version)
  values = packet.values
  _fields(values, CAN_HEALTH_FIELDS, CAN_HEALTH_FORMAT)
  for field in ("bus_off", "error_warning", "error_passive", "canfd_enabled", "brs_enabled", "canfd_non_iso"):
    _enum(values[field], (0, 1), field)
  for field in ("last_error", "last_stored_error", "last_data_error", "last_data_stored_error"):
    _enum(values[field], range(8), field)
  if values["receive_error_cnt"] > 127:
    raise HealthCodecError("receive error counter outside hardware range 0..127")


def decode_can_health(data: bytes, version: ProtocolVersion) -> WireCanHealth:
  _version(version)
  packet = WireCanHealth(version, dict(zip(CAN_HEALTH_FIELDS,
                         struct.unpack(CAN_HEALTH_FORMAT, _bytes(data, struct.calcsize(CAN_HEALTH_FORMAT))), strict=True)))
  _validate_can_health(packet)
  return packet


def encode_can_health(packet: WireCanHealth) -> bytes:
  if type(packet) is not WireCanHealth:
    raise HealthCodecError("expected WireCanHealth")
  _validate_can_health(packet)
  return struct.pack(CAN_HEALTH_FORMAT, *(packet.values[name] for name in CAN_HEALTH_FIELDS))


def normalize_can_health(packet: WireCanHealth, *, can_fd: bool | None) -> Mapping[str, int | bool | None]:
  """Wire speeds are kbit/s (main_comms.h divides config speed by ten)."""
  if type(packet) is not WireCanHealth or (can_fd is not None and type(can_fd) is not bool):
    raise HealthCodecError("WireCanHealth and explicit bool/None CAN FD capability required")
  _validate_can_health(packet)
  values = dict(packet.values)
  # can_init_all clears canfd_enabled without clearing brs_enabled. Preserve
  # that valid wire snapshot and expose its transient configuration mismatch;
  # consistency here is diagnostic information, never control authorization.
  values["fd_configuration_consistent"] = not bool(values["brs_enabled"] and not values["canfd_enabled"])
  if can_fd is False and any(values[name] for name in ("canfd_enabled", "brs_enabled", "canfd_non_iso")):
    raise HealthCodecError("FD status contradicts declared classic-only capability")
  values["can_speed_bps"] = values.pop("can_speed") * 1000
  values["can_data_speed_bps"] = values.pop("can_data_speed") * 1000 if can_fd is True else None
  for name in ("bus_off", "error_warning", "error_passive"):
    values[name] = bool(values[name])
  for name in ("canfd_enabled", "brs_enabled", "canfd_non_iso"):
    values[name] = bool(values[name]) if can_fd is True else None
  if can_fd is not True:
    values["last_data_error"] = values["last_data_stored_error"] = None
  return MappingProxyType(values)
