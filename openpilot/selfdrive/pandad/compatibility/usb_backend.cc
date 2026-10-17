#include "usb_backend.h"

#include <algorithm>
#include <set>
#include <utility>

namespace openpilot::pandad::compatibility {
namespace {
Status map_error(int error) {
  switch (error) {
    case kUsbTimeout: return Status::Timeout;
    case kUsbNoDevice: return Status::Disconnected;
    case kUsbPipe: return Status::Stall;
    case kUsbBusy: return Status::Busy;
    case kUsbInterrupted: return Status::Cancelled;
    case kUsbInvalid: return Status::Invalid;
    default: return Status::IoError;
  }
}
bool valid_binding(const UsbBinding &binding) {
  return binding.bulk_alt0_verified && !binding.serial.empty() && binding.serial.size() <= 128 &&
    std::all_of(binding.serial.begin(), binding.serial.end(), [](unsigned char c) { return c > 32 && c < 127; }) &&
    (binding.board_type == 6 || binding.board_type == 7);
}
bool normal_panda(const UsbDeviceInfo &device) {
  return (device.vendor == 0xbbaa || device.vendor == 0x3801) && device.product == 0xddcc;
}
bool valid_descriptor(const UsbDeviceInfo &device, const UsbBinding &binding) {
  if (!device.active_configuration_known || device.serial_index == 0 ||
      device.bcd_device != (binding.board_type << 8)) return false;
  int matches = 0;
  for (const auto &alt : device.alternates) {
    if (alt.interface_number != 0 || alt.alternate_setting != 0) continue;
    ++matches;
    std::set<unsigned int> seen;
    bool rx = false, tx = false;
    for (const auto &ep : alt.endpoints) {
      if (!seen.insert(ep.address).second) return false;
      if (ep.address == 0x81 || ep.address == 0x03) {
        if ((ep.attributes & 3) != 2 || ep.max_packet_size != 64) return false;
        if (ep.address == 0x81) rx = true;
        else tx = true;
      }
    }
    if (!rx || !tx) return false;
  }
  return matches == 1;
}
}  // namespace

UsbBackend::UsbBackend(std::unique_ptr<LibusbApi> api) : api_(std::move(api)) {}
UsbBackend::~UsbBackend() { close(); }
void UsbBackend::cleanup_locked() {
  if (handle_) {
    if (claimed_) api_->release(handle_, 0);
    api_->close(handle_);
  }
  handle_ = 0;
  claimed_ = false;
  if (initialized_) api_->shutdown();
  initialized_ = false;
}
void UsbBackend::close() {
  std::lock_guard<std::mutex> lock(mutex_);
  cleanup_locked();
}
bool UsbBackend::is_open() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return handle_ != 0 && claimed_;
}
Status UsbBackend::open_bound(const UsbBinding &binding, bool allow_device_io) {
  std::unique_lock<std::mutex> lock(mutex_, std::try_to_lock);
  if (!lock.owns_lock()) return Status::Busy;
  if (!allow_device_io || !api_ || !valid_binding(binding)) return Status::Invalid;
  if (initialized_ || handle_) return Status::Busy;
  int result = api_->initialize();
  if (result != 0) return map_error(result);
  initialized_ = true;
  auto fail = [&](Status status) { cleanup_locked(); return status; };
  std::vector<UsbDeviceInfo> devices;
  result = api_->enumerate(devices);
  if (result != 0) return fail(map_error(result));
  for (const auto &device : devices) {
    if (!normal_panda(device)) continue;
    UsbHandle candidate = 0;
    result = api_->open(device.id, candidate);
    if (result != 0 || candidate == 0) {
      if (candidate) api_->close(candidate);
      return fail(result == 0 ? Status::IoError : map_error(result));
    }
    std::string serial;
    result = api_->serial(candidate, device.serial_index, serial, kBackendSliceMs);
    if (result != 0) {
      api_->close(candidate);
      return fail(map_error(result));  // cannot exclude an inaccessible duplicate
    }
    if (serial != binding.serial) { api_->close(candidate); continue; }
    if (handle_ || !valid_descriptor(device, binding)) {
      api_->close(candidate);
      return fail(Status::Poisoned);  // duplicate or replacement identity
    }
    handle_ = candidate;
  }
  if (!handle_) return fail(Status::Disconnected);
  result = api_->claim(handle_, 0);
  if (result != 0) return fail(map_error(result));
  claimed_ = true;
  return Status::Ok;
}
Result UsbBackend::transact(const Request &request, std::uint32_t timeout_ms) {
  std::unique_lock<std::mutex> lock(mutex_, std::try_to_lock);
  if (!lock.owns_lock()) return {Status::Busy, 0, {}};
  if (!valid_request(request) || timeout_ms == 0 || timeout_ms > kBackendSliceMs) return {Status::Invalid, 0, {}};
  if ((request.kind == Kind::BulkRead && request.endpoint != 0x81) ||
      (request.kind == Kind::BulkWrite && request.endpoint != 0x03)) return {Status::Invalid, 0, {}};
  if (!handle_ || !claimed_) return {Status::Disconnected, 0, {}};
  const bool read = is_read(request.kind);
  const std::size_t length = read ? request.read_length : request.payload.size();
  std::vector<std::uint8_t> bytes = read ? std::vector<std::uint8_t>(length) : request.payload;
  int actual = 0;
  int result = 0;
  if (request.kind == Kind::ControlRead || request.kind == Kind::ControlWrite) {
    result = api_->control(handle_, read ? 0xc0 : 0x40, static_cast<std::uint8_t>(request.request),
      static_cast<std::uint16_t>(request.value), static_cast<std::uint16_t>(request.index),
      bytes.data(), static_cast<std::uint16_t>(length), timeout_ms);
    if (result < 0) return {map_error(result), 0, {}, false, !read};
    actual = result;
    result = kUsbSuccess;
  } else {
    result = api_->bulk(handle_, static_cast<std::uint8_t>(request.endpoint), bytes.data(),
                        static_cast<int>(length), actual, timeout_ms);
  }
  // libusb OVERFLOW does not guarantee actual_length or buffer contents,
  // including when the reported count happens to be within the requested size.
  if (result == kUsbOverflow) return {Status::Poisoned, 0, {}, false, !read};
  if (actual < 0 || static_cast<std::size_t>(actual) > length) return {Status::Poisoned, 0, {}, false, !read};
  const auto size = static_cast<std::size_t>(actual);
  Status status = result == 0 ? (size == length ? Status::Ok : Status::Short) : map_error(result);
  if (read) bytes.resize(size);
  else bytes.clear();
  return {status, size, std::move(bytes), true, !read && status != Status::Ok};
}
}  // namespace openpilot::pandad::compatibility
