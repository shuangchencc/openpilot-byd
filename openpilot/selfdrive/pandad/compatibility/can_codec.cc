#include "can_codec.h"

#include <algorithm>
#include <utility>

namespace openpilot::pandad::compatibility {
namespace {

bool valid_options(CanCapability capability, FrameDirection direction) {
  return (capability == CanCapability::ClassicOnly || capability == CanCapability::CanFd) &&
         (direction == FrameDirection::Rx || direction == FrameDirection::Tx);
}

CodecError validate(const CanFrame &frame, CanCapability capability, FrameDirection direction) {
  if (!valid_options(capability, direction)) return CodecError::InvalidArgument;
  if (frame.address > (frame.extended ? 0x1fffffffU : 0x7ffU)) return CodecError::InvalidAddress;
  if (frame.local_bus > 2) return CodecError::InvalidBus;
  if ((frame.returned && frame.rejected) ||
      (direction == FrameDirection::Tx && (frame.returned || frame.rejected))) return CodecError::InvalidFlags;
  if (frame.fd && capability == CanCapability::ClassicOnly) return CodecError::UnsupportedFd;
  if (std::find(kCanDlcLengths.begin(), kCanDlcLengths.end(), frame.payload.size()) == kCanDlcLengths.end() ||
      (!frame.fd && frame.payload.size() > 8)) return CodecError::InvalidLength;
  return CodecError::None;
}

std::uint8_t checksum(const std::uint8_t *data, std::size_t size) {
  std::uint8_t result = 0;
  for (std::size_t i = 0; i < size; ++i) result ^= data[i];
  return result;
}

}  // namespace

const char *codec_error_name(CodecError error) {
  switch (error) {
    case CodecError::None: return "none";
    case CodecError::InvalidArgument: return "invalid_argument";
    case CodecError::InvalidAddress: return "invalid_address";
    case CodecError::InvalidBus: return "invalid_bus";
    case CodecError::InvalidSlot: return "invalid_slot";
    case CodecError::InvalidLength: return "invalid_length";
    case CodecError::UnsupportedFd: return "unsupported_fd";
    case CodecError::InvalidFlags: return "invalid_flags";
    case CodecError::BadChecksum: return "bad_checksum";
    case CodecError::InputTooLarge: return "input_too_large";
    case CodecError::Poisoned: return "poisoned";
  }
  return "unknown_error";
}

CodecResult<std::vector<std::uint8_t>> encode_can_frame(
    const CanFrame &frame, CanCapability capability, FrameDirection direction) {
  const auto error = validate(frame, capability, direction);
  if (error != CodecError::None) return {{}, error};
  const auto dlc = static_cast<std::uint8_t>(std::distance(
    kCanDlcLengths.begin(), std::find(kCanDlcLengths.begin(), kCanDlcLengths.end(), frame.payload.size())));
  std::vector<std::uint8_t> bytes(kCanHeaderSize + frame.payload.size(), 0);
  bytes[0] = static_cast<std::uint8_t>((dlc << 4U) | (frame.local_bus << 1U) | frame.fd);
  const std::uint32_t word = (static_cast<std::uint32_t>(frame.address) << 3U) | (static_cast<std::uint32_t>(frame.extended) << 2U) |
                            (static_cast<std::uint32_t>(frame.returned) << 1U) | frame.rejected;
  for (std::size_t i = 0; i < 4; ++i) bytes[i + 1] = static_cast<std::uint8_t>(word >> (i * 8U));
  std::copy(frame.payload.begin(), frame.payload.end(), bytes.begin() + kCanHeaderSize);
  bytes[5] = checksum(bytes.data(), bytes.size());
  return {std::move(bytes), CodecError::None};
}

CodecResult<CanFrame> decode_can_frame(
    const std::uint8_t *data, std::size_t size, CanCapability capability, FrameDirection direction) {
  if (!valid_options(capability, direction) || (data == nullptr && size != 0)) return {{}, CodecError::InvalidArgument};
  // Check before reading byte0 or subtracting header size (upstream's unsigned
  // size - sizeof(header) loop is unsafe for every 0..5-byte prefix).
  if (size < kCanHeaderSize || size > kCanMaxFrameSize) return {{}, CodecError::InvalidLength};
  const auto payload_size = kCanDlcLengths[data[0] >> 4U];
  if (size != kCanHeaderSize + payload_size) return {{}, CodecError::InvalidLength};
  if (checksum(data, size) != 0) return {{}, CodecError::BadChecksum};
  std::uint32_t word = 0;
  for (std::size_t i = 0; i < 4; ++i) word |= static_cast<std::uint32_t>(data[i + 1]) << (i * 8U);
  CanFrame frame;
  frame.address = word >> 3U;
  frame.local_bus = (data[0] >> 1U) & 7U;
  frame.extended = (word & 4U) != 0;
  frame.fd = (data[0] & 1U) != 0;
  frame.returned = (word & 2U) != 0;
  frame.rejected = (word & 1U) != 0;
  frame.payload.assign(data + kCanHeaderSize, data + size);
  const auto error = validate(frame, capability, direction);
  if (error != CodecError::None) return {{}, error};
  return {std::move(frame), CodecError::None};
}

CodecResult<std::uint8_t> host_source(
    std::uint32_t slot, std::uint32_t local_bus, bool returned, bool rejected, FrameDirection direction) {
  if (direction != FrameDirection::Rx && direction != FrameDirection::Tx) return {{}, CodecError::InvalidArgument};
  if (slot > 1) return {{}, CodecError::InvalidSlot};
  if (local_bus > 2) return {{}, CodecError::InvalidBus};
  if ((returned && rejected) || (direction == FrameDirection::Tx && (returned || rejected))) return {{}, CodecError::InvalidFlags};
  const auto source = 4U * slot + local_bus + (returned ? 0x80U : (rejected ? 0xc0U : 0U));
  return {static_cast<std::uint8_t>(source), CodecError::None};
}

CodecResult<HostRoute> parse_host_source(std::uint32_t source, FrameDirection direction) {
  if (direction != FrameDirection::Rx && direction != FrameDirection::Tx) return {{}, CodecError::InvalidArgument};
  HostRoute route;
  std::uint32_t base = source;
  if (source >= 0xc0U && source <= 0xc6U) {
    base -= 0xc0U;
    route.rejected = true;
  } else if (source >= 0x80U && source <= 0x86U) {
    base -= 0x80U;
    route.returned = true;
  }
  if (base > 6 || base == 3) return {{}, CodecError::InvalidBus};
  if (direction == FrameDirection::Tx && (route.returned || route.rejected)) return {{}, CodecError::InvalidFlags};
  route.slot = static_cast<std::uint8_t>(base / 4U);
  route.local_bus = static_cast<std::uint8_t>(base % 4U);
  return {route, CodecError::None};
}

CanStreamParser::CanStreamParser(CanCapability capability, FrameDirection direction)
    : capability_(capability), direction_(direction) {}

CodecResult<std::vector<CanFrame>> CanStreamParser::fail(CodecError error) {
  pending_.fill(0);
  pending_size_ = 0;
  poisoned_ = true;
  return {{}, error};
}

CodecResult<std::vector<CanFrame>> CanStreamParser::feed(const std::uint8_t *data, std::size_t size) {
  if (poisoned_) return {{}, CodecError::Poisoned};
  if (!valid_options(capability_, direction_) || (data == nullptr && size != 0)) return fail(CodecError::InvalidArgument);
  if (size > kCanMaxInputSize) return fail(CodecError::InputTooLarge);
  // Bound allocation before copying. The only persistent storage is an
  // incomplete frame (<70 bytes), never an arbitrary stream backlog.
  std::vector<std::uint8_t> bytes;
  bytes.reserve(pending_size_ + size);
  bytes.insert(bytes.end(), pending_.begin(), pending_.begin() + pending_size_);
  if (size != 0) bytes.insert(bytes.end(), data, data + size);
  std::vector<CanFrame> frames;
  std::size_t offset = 0;
  while (bytes.size() - offset >= kCanHeaderSize) {
    const auto frame_size = kCanHeaderSize + kCanDlcLengths[bytes[offset] >> 4U];
    if (bytes.size() - offset < frame_size) break;
    auto decoded = decode_can_frame(bytes.data() + offset, frame_size, capability_, direction_);
    if (!decoded) return fail(decoded.error);
    frames.push_back(std::move(decoded.value));
    offset += frame_size;
  }
  pending_size_ = bytes.size() - offset;
  std::copy(bytes.begin() + offset, bytes.end(), pending_.begin());
  return {std::move(frames), CodecError::None};
}

void CanStreamParser::clear() {
  pending_.fill(0);
  pending_size_ = 0;
  poisoned_ = false;
}

}  // namespace openpilot::pandad::compatibility
