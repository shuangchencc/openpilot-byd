#!/usr/bin/env python3
"""Validate and summarize normalized CAN data frames without any device interface.

Only Python's standard library is used. This module reads local files and writes
JSON to stdout. It does not decode physical signals, construct CarState, load
plugins, execute recorded content, or provide any CAN transmission operation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable


MANIFEST_SCHEMA = "can-jsonl-manifest/v1"
RESULT_SCHEMA = "can-jsonl-report/v1"
MAX_TIMESTAMP_NS = (1 << 63) - 1
VALID_PAYLOAD_LENGTHS = frozenset((*range(9), 12, 16, 20, 24, 32, 48, 64))
RECORD_FIELDS = frozenset(("timestamp_ns", "bus", "address", "data"))
MANIFEST_FIELDS = frozenset((
    "schema", "data_kind", "log_file", "sha256", "timestamp_unit",
    "timestamp_clock", "description",
))
HEX_DATA = re.compile(r"(?:[0-9a-fA-F]{2})*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_LINE_BYTES = 65536
MAX_MANIFEST_BYTES = 65536


class ValidationError(ValueError):
    """Input does not satisfy the declared local data contract."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValidationError(f"non-finite JSON constant is not allowed: {value}")


def parse_json(text: str, context: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as error:
        raise ValidationError(f"{context}: {error}") from error


def _check_fields(value: Any, expected: frozenset[str], context: str) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{context}: expected a JSON object")
    missing, unknown = expected - value.keys(), value.keys() - expected
    if missing or unknown:
        raise ValidationError(
            f"{context}: missing fields={sorted(missing)}; unknown fields={sorted(unknown)}")


def _integer(value: Any, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"{name}: expected an integer in [{minimum}, {maximum}]")
    return value


@dataclass(frozen=True)
class Frame:
    timestamp_ns: int
    bus: int
    address: int
    data: bytes


def parse_frame(value: Any, line_number: int) -> Frame:
    context = f"line {line_number}"
    _check_fields(value, RECORD_FIELDS, context)
    timestamp = _integer(value["timestamp_ns"], 0, MAX_TIMESTAMP_NS, f"{context}.timestamp_ns")
    bus = _integer(value["bus"], 0, 255, f"{context}.bus")
    address = _integer(value["address"], 0, 0x1FFFFFFF, f"{context}.address")
    data = value["data"]
    if not isinstance(data, str) or HEX_DATA.fullmatch(data) is None:
        raise ValidationError(f"{context}.data: expected contiguous, even-length ASCII hex without a prefix")
    length = len(data) // 2
    if length not in VALID_PAYLOAD_LENGTHS:
        raise ValidationError(f"{context}.data: illegal CAN/CAN-FD payload length {length}")
    return Frame(timestamp, bus, address, bytes.fromhex(data))


def read_manifest(path: Path) -> tuple[dict[str, Any], Path]:
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValidationError(f"cannot resolve local manifest path: {error}") from error
    if not path.is_file():
        raise ValidationError("manifest must be a regular local file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValidationError("manifest exceeds 65536 bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("manifest must be UTF-8") from error
    manifest = parse_json(text, "manifest")
    _check_fields(manifest, MANIFEST_FIELDS, "manifest")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise ValidationError(f"manifest.schema must be {MANIFEST_SCHEMA}")
    if manifest["data_kind"] not in ("synthetic", "recorded"):
        raise ValidationError("manifest.data_kind must be synthetic or recorded")
    if manifest["timestamp_unit"] != "ns" or manifest["timestamp_clock"] != "monotonic":
        raise ValidationError("manifest requires timestamp_unit=ns and timestamp_clock=monotonic")
    if not isinstance(manifest["description"], str) or not manifest["description"].strip():
        raise ValidationError("manifest.description must be non-empty text")
    if not isinstance(manifest["sha256"], str) or SHA256.fullmatch(manifest["sha256"]) is None:
        raise ValidationError("manifest.sha256 must be 64 lowercase hex characters")
    name = manifest["log_file"]
    if (not isinstance(name, str) or not name or "\\" in name
            or any(ord(character) < 32 for character in name)):
        raise ValidationError("manifest.log_file must be a normalized relative POSIX path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name or name == ".":
        raise ValidationError("manifest.log_file must stay inside the manifest directory")
    try:
        log_path = (path.parent / name).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValidationError(f"cannot resolve local log path: {error}") from error
    if not log_path.is_relative_to(path.parent) or not log_path.is_file():
        raise ValidationError("manifest.log_file must name a regular file inside the manifest directory")
    if log_path == path:
        raise ValidationError("manifest cannot also be its own log file")
    return manifest, log_path


@dataclass
class ByteStats:
    values: set[int] = field(default_factory=set)
    observations: int = 0
    comparisons: int = 0
    changed: int = 0
    flip_counts: list[int] = field(default_factory=lambda: [0] * 8)

    def add(self, value: int, previous: int | None) -> None:
        self.values.add(value)
        self.observations += 1
        if previous is not None:
            self.comparisons += 1
            difference = previous ^ value
            self.changed += int(difference != 0)
            for bit in range(8):
                self.flip_counts[bit] += (difference >> bit) & 1

    def report(self, offset: int, frames: int) -> dict[str, Any]:
        return {
            "offset": offset,
            "observations": self.observations,
            "frames_without_byte": frames - self.observations,
            "distinct_values": sorted(self.values),
            "min_raw_value": min(self.values),
            "max_raw_value": max(self.values),
            "adjacent_frame_comparisons": self.comparisons,
            "changed_comparisons": self.changed,
            "bit_flip_counts_lsb_first": self.flip_counts,
        }


@dataclass
class MessageStats:
    count: int = 0
    first_ns: int = 0
    last_ns: int = 0
    minimum_interval: int | None = None
    maximum_interval: int | None = None
    minimum_positive_interval: int | None = None
    zero_intervals: int = 0
    lengths: Counter[int] = field(default_factory=Counter)
    byte_stats: list[ByteStats] = field(default_factory=list)
    previous_data: bytes = b""

    def add(self, frame: Frame, collect_bytes: bool) -> None:
        if self.count:
            interval = frame.timestamp_ns - self.last_ns
            self.minimum_interval = interval if self.minimum_interval is None else min(self.minimum_interval, interval)
            self.maximum_interval = interval if self.maximum_interval is None else max(self.maximum_interval, interval)
            self.zero_intervals += int(interval == 0)
            if interval > 0:
                self.minimum_positive_interval = (interval if self.minimum_positive_interval is None
                                                  else min(self.minimum_positive_interval, interval))
        else:
            self.first_ns = frame.timestamp_ns
        if collect_bytes:
            while len(self.byte_stats) < len(frame.data):
                self.byte_stats.append(ByteStats())
            for offset, value in enumerate(frame.data):
                previous = self.previous_data[offset] if self.count and offset < len(self.previous_data) else None
                self.byte_stats[offset].add(value, previous)
            self.previous_data = frame.data
        self.last_ns = frame.timestamp_ns
        self.count += 1
        self.lengths[len(frame.data)] += 1

    def report(self, bus: int, address: int) -> dict[str, Any]:
        intervals = self.count - 1
        duration = self.last_ns - self.first_ns
        if not intervals:
            rate_status = "insufficient_frames"
        elif duration == 0:
            rate_status = "zero_time_span"
        elif self.zero_intervals:
            rate_status = "computed_with_duplicate_timestamps"
        else:
            rate_status = "computed"
        rate = intervals * 1_000_000_000 / duration if intervals and duration > 0 else None
        return {
            "bus": bus, "address": address, "address_hex": f"0x{address:X}",
            "requires_29bit_identifier": address > 0x7FF,
            "frame_count": self.count, "first_timestamp_ns": self.first_ns, "last_timestamp_ns": self.last_ns,
            "length_counts": {str(length): count for length, count in sorted(self.lengths.items())},
            "contains_payload_requiring_can_fd": any(length > 8 for length in self.lengths),
            "observed_interval_rate_hz": rate, "rate_status": rate_status,
            "interval_ns": {
                "count": intervals, "zero_count": self.zero_intervals,
                "positive_count": intervals - self.zero_intervals,
                "min": self.minimum_interval, "max": self.maximum_interval,
                "min_positive": self.minimum_positive_interval,
                "mean": duration / intervals if intervals else None,
            },
            "raw_byte_candidates": [stats.report(offset, self.count) for offset, stats in enumerate(self.byte_stats)],
        }


def _scan_recording(manifest_path: Path | str, on_frame: Callable[[Frame], None] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Internal scanner: callbacks only collect local state, never emit outputs."""
    manifest, log_path = read_manifest(Path(manifest_path))
    digest = hashlib.sha256()
    bus_counts: Counter[int] = Counter()
    previous_ns: int | None = None
    first_ns: int | None = None
    frame_count, line_number = 0, 0
    with log_path.open("rb") as stream:
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            line_number += 1
            if len(raw) > MAX_LINE_BYTES:
                raise ValidationError(f"line {line_number}: exceeds {MAX_LINE_BYTES} bytes")
            digest.update(raw)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValidationError(f"line {line_number}: expected UTF-8") from error
            if not text.strip():
                raise ValidationError(f"line {line_number}: blank records are not allowed")
            frame = parse_frame(parse_json(text, f"line {line_number}"), line_number)
            if previous_ns is not None and frame.timestamp_ns < previous_ns:
                raise ValidationError(f"line {line_number}: timestamp_ns moved backwards in the global log")
            if first_ns is None:
                first_ns = frame.timestamp_ns
            previous_ns = frame.timestamp_ns
            frame_count += 1
            bus_counts[frame.bus] += 1
            if on_frame is not None:
                on_frame(frame)
    if not frame_count:
        raise ValidationError("log contains no frames")
    actual_digest = digest.hexdigest()
    if actual_digest != manifest["sha256"]:
        raise ValidationError("log SHA256 differs from manifest; no report was accepted")
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA, "validation": "passed",
        "source": {
            "manifest_declared_data_kind": manifest["data_kind"],
            "description": manifest["description"], "log_file": manifest["log_file"],
            "verified_sha256": actual_digest, "timestamp_clock": "monotonic", "timestamp_unit": "ns",
            "provenance_note": "A hash binds the bytes to this manifest; it does not prove a recorded origin or a vehicle identity.",
        },
        "frame_count": frame_count, "first_timestamp_ns": first_ns, "last_timestamp_ns": previous_ns,
        "bus_frame_counts": {str(bus): count for bus, count in sorted(bus_counts.items())},
    }
    return manifest, result


def load_recording(manifest_path: Path | str) -> tuple[dict[str, Any], tuple[Frame, ...]]:
    """Return manifest and immutable frames only after full validation succeeds.

    This helper materializes all frames for offline decoding. Large statistics-only
    workloads should use analyze(), which retains aggregate state instead.
    """
    frames: list[Frame] = []
    try:
        manifest, _ = _scan_recording(manifest_path, frames.append)
    except OSError as error:
        raise ValidationError(f"local input access failed: {error}") from error
    return manifest, tuple(frames)


def analyze(manifest_path: Path | str, *, summarize: bool = True) -> dict[str, Any]:
    messages: dict[tuple[int, int], MessageStats] = {}

    def add_frame(frame: Frame) -> None:
        key = (frame.bus, frame.address)
        messages.setdefault(key, MessageStats()).add(frame, collect_bytes=True)

    _, result = _scan_recording(manifest_path, add_frame if summarize else None)
    result["operation"] = "summarize" if summarize else "validate"
    if summarize:
        result["interpretation"] = {
            "frequency": "(frame_count - 1) / observed time span, per bus and numeric ID; duplicates are retained and flagged. This is a capture statistic, not a verified transmitter schedule.",
            "raw_bytes": "Byte observations and adjacent-frame bit changes only; no signedness, endianness, scaling, units, physical meaning or safety behavior is inferred.",
            "frame_format": "Input represents normalized data-frame payloads only. Numeric IDs do not preserve IDE/RTR/error flags; payloads <=8 bytes do not distinguish classic CAN from CAN-FD.",
        }
        result["messages"] = [stats.report(bus, address) for (bus, address), stats in sorted(messages.items())]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "summarize"):
        sub = subcommands.add_parser(command)
        sub.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = analyze(args.manifest, summarize=args.command == "summarize")
    except (ValidationError, OSError) as error:
        print(json.dumps({"schema": RESULT_SCHEMA, "validation": "failed", "error": str(error)},
                         ensure_ascii=False, allow_nan=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
