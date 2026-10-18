#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace openpilot::pandad::compatibility {

// Thin injectable seam. All return codes follow libusb's documented values;
// constructing the native implementation does not create a context or list USB.
inline constexpr int kUsbSuccess = 0;
inline constexpr int kUsbIo = -1;
inline constexpr int kUsbInvalid = -2;
inline constexpr int kUsbAccess = -3;
inline constexpr int kUsbNoDevice = -4;
inline constexpr int kUsbNotFound = -5;
inline constexpr int kUsbBusy = -6;
inline constexpr int kUsbTimeout = -7;
inline constexpr int kUsbOverflow = -8;
inline constexpr int kUsbPipe = -9;
inline constexpr int kUsbInterrupted = -10;
inline constexpr std::size_t kUsbMaxInventoryDevices = 64;
inline constexpr std::size_t kUsbMaxAlternates = 32;
inline constexpr std::size_t kUsbMaxEndpoints = 16;

using UsbDeviceId = std::uintptr_t;
using UsbHandle = std::uintptr_t;
struct UsbEndpoint {
  std::uint8_t address = 0;
  std::uint8_t attributes = 0;
  std::uint16_t max_packet_size = 0;
};
struct UsbAlternate {
  std::uint8_t interface_number = 0;
  std::uint8_t alternate_setting = 0;
  std::vector<UsbEndpoint> endpoints;
};
struct UsbDeviceInfo {
  UsbDeviceId id = 0;
  std::uint16_t vendor = 0;
  std::uint16_t product = 0;
  std::uint16_t bcd_device = 0;
  std::uint8_t serial_index = 0;
  bool active_configuration_known = false;
  std::vector<UsbAlternate> alternates;
};

// One API instance belongs to one backend. Device IDs live until shutdown().
// No detach/reset/clear-halt/configuration/alternate-setting mutation is exposed.
class LibusbApi {
public:
  virtual ~LibusbApi() = default;
  virtual int initialize() = 0;
  virtual int enumerate(std::vector<UsbDeviceInfo> &devices) = 0;
  virtual int open(UsbDeviceId device, UsbHandle &handle) = 0;
  virtual int serial(UsbHandle handle, std::uint8_t index, std::string &value,
                     std::uint32_t timeout_ms) = 0;
  virtual int claim(UsbHandle handle, int interface_number) = 0;
  virtual int control(UsbHandle handle, std::uint8_t type, std::uint8_t request,
                      std::uint16_t value, std::uint16_t index, std::uint8_t *data,
                      std::uint16_t length, std::uint32_t timeout_ms) = 0;
  virtual int bulk(UsbHandle handle, std::uint8_t endpoint, std::uint8_t *data,
                   int length, int &actual, std::uint32_t timeout_ms) = 0;
  virtual void release(UsbHandle handle, int interface_number) = 0;
  virtual void close(UsbHandle handle) = 0;
  virtual void shutdown() = 0;
};

std::unique_ptr<LibusbApi> make_native_libusb_api();

}  // namespace openpilot::pandad::compatibility
