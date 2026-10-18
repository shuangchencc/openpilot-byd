#pragma once

#include <cstdint>
#include <vector>

namespace openpilot::pandad::compatibility {

// Availability is separate from numeric wire values. In particular an absent
// current sensor is not a measured zero current and an absent SPI monitor is
// not evidence of an error-free SPI link.
struct HealthFieldAvailability {
  bool voltage = false;
  bool current = false;
  bool temperature = false;
  bool sound_output = false;
  bool spi_error_count = false;
};
inline constexpr HealthFieldAvailability kH7HealthFields{true, true, true, true, true};
inline constexpr HealthFieldAvailability kF4HealthFields{true, false, true, false, false};
inline constexpr std::uint8_t kF4CapabilityRequest = 0xd5;
inline constexpr std::size_t kF4CapabilityLength = 16;

// P5 F4C1 is an exact reference-candidate contract, not a negotiable minimum.
// Unknown versions/bits, downgraded fields and legacy/short replies quarantine
// this connection. The canonical health and CAN protocol hashes stay unchanged.
inline bool valid_f4_capabilities(const std::vector<std::uint8_t> &bytes) {
  static const std::vector<std::uint8_t> expected{
    'F', '4', 'C', '1', 1, 0, 16, 0, 0x1f, 0, 0, 0, 7, 0, 0, 0,
  };
  return bytes == expected;
}

}  // namespace openpilot::pandad::compatibility
