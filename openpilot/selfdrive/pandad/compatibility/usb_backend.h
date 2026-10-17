#pragma once

#include <memory>
#include <mutex>
#include <string>

#include "libusb_api.h"
#include "transport.h"

namespace openpilot::pandad::compatibility {

struct UsbBinding {
  std::string serial;
  // Descriptor bcdDevice high byte: DOS=6, RED_PANDA=7. This software identity
  // is not proof of the physical MCU, firmware ABI, or permission to send CAN.
  std::uint16_t board_type = 0;
  // Separate caller-supplied activation evidence/assumption, not a fact inferred
  // from alt0 descriptors. The locked firmware lacks GET_INTERFACE. No fallback
  // SET_INTERFACE is allowed: an unverified real session must remain unopened.
  bool bulk_alt0_verified = false;
};

class UsbBackend final : public Backend {
public:
  explicit UsbBackend(std::unique_ptr<LibusbApi> api = make_native_libusb_api());
  ~UsbBackend() override;
  UsbBackend(const UsbBackend &) = delete;
  UsbBackend &operator=(const UsbBackend &) = delete;

  // Deliberate future activation boundary, not used by offline acceptance.
  // Empty/unbound identities and the default false permission do no USB I/O.
  // Opening enumerates/read-descriptors/claims only, never resets or configures.
  // OS open/claim latency is outside the bounded transfer contract. The caller
  // must serialize lifecycle operations and keep this object alive during use.
  Status open_bound(const UsbBinding &binding, bool allow_device_io = false);
  void close();
  bool is_open() const;
  Result transact(const Request &request, std::uint32_t timeout_ms) override;

private:
  void cleanup_locked();
  std::unique_ptr<LibusbApi> api_;
  mutable std::mutex mutex_;
  UsbHandle handle_ = 0;
  bool initialized_ = false;
  bool claimed_ = false;
};

}  // namespace openpilot::pandad::compatibility
