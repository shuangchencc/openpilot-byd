#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

// Pure protocol helpers. This header deliberately has no Panda, transport,
// cereal or MCU dependencies and cannot authorize or transmit a frame.
namespace openpilot::pandad::compatibility {

inline constexpr std::array<std::size_t, 16> kCanDlcLengths = {
  0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64,
};
inline constexpr std::size_t kCanHeaderSize = 6;
inline constexpr std::size_t kCanMaxFrameSize = 70;
inline constexpr std::size_t kCanMaxInputSize = 16384;

enum class CanCapability { ClassicOnly, CanFd };
enum class FrameDirection { Rx, Tx };
enum class CodecError {
  None, InvalidArgument, InvalidAddress, InvalidBus, InvalidSlot,
  InvalidLength, UnsupportedFd, InvalidFlags, BadChecksum,
  InputTooLarge, Poisoned,
};

const char *codec_error_name(CodecError error);

template <typename T>
struct CodecResult {
  T value{};
  CodecError error = CodecError::None;
  explicit operator bool() const { return error == CodecError::None; }
};

struct CanFrame {
  // Wide ingress types preserve out-of-range values until validate() rejects
  // them; narrowing before validation could turn bus256/address2^32 into zero.
  std::uint64_t address = 0;
  std::uint32_t local_bus = 0;
  bool extended = false;
  bool fd = false;
  bool returned = false;
  bool rejected = false;
  std::vector<std::uint8_t> payload;
};

// DLC length is exact: unsupported lengths are rejected, never padded/truncated.
// A standard identifier must fit 11 bits; an extended one must fit 29 bits.
// FD is explicit even for <=8 bytes. ClassicOnly rejects every FD frame.
CodecResult<std::vector<std::uint8_t>> encode_can_frame(
  const CanFrame &frame, CanCapability capability, FrameDirection direction);
CodecResult<CanFrame> decode_can_frame(
  const std::uint8_t *data, std::size_t size,
  CanCapability capability, FrameDirection direction);

struct HostRoute {
  std::uint8_t slot = 0;
  std::uint8_t local_bus = 0;
  bool returned = false;
  bool rejected = false;
};

// Host src is a separate routing namespace: stride4; local0..2; 3/7 reserved.
// returned adds 0x80, rejected adds 0xc0; both together are invalid, not wrapped.
// Inputs stay wide until validation, so overflow cannot become a valid route.
CodecResult<std::uint8_t> host_source(
  std::uint32_t slot, std::uint32_t local_bus, bool returned, bool rejected,
  FrameDirection direction);
CodecResult<HostRoute> parse_host_source(std::uint32_t source, FrameDirection direction);

class CanStreamParser {
public:
  explicit CanStreamParser(CanCapability capability, FrameDirection direction = FrameDirection::Rx);

  // Each feed accepts <=16KiB. Its complete-frame output is atomic: any error
  // discards ALL output from that call, clears residue and poisons this parser.
  // Previous successful feeds remain valid. A partial tail is not an error.
  // A poisoned parser cannot silently resynchronize. clear() only resets local
  // memory; the caller must decide when a new session is allowed.
  CodecResult<std::vector<CanFrame>> feed(const std::uint8_t *data, std::size_t size);
  void clear();
  bool poisoned() const { return poisoned_; }
  std::size_t buffered_size() const { return pending_size_; }

private:
  CodecResult<std::vector<CanFrame>> fail(CodecError error);
  CanCapability capability_;
  FrameDirection direction_;
  std::array<std::uint8_t, kCanMaxFrameSize - 1> pending_{};
  std::size_t pending_size_ = 0;
  bool poisoned_ = false;
};

}  // namespace openpilot::pandad::compatibility
