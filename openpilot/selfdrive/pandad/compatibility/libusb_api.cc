#include "libusb_api.h"

#include <array>
#include <utility>

#include <libusb.h>

namespace openpilot::pandad::compatibility {
namespace {
static_assert(LIBUSB_ERROR_TIMEOUT == kUsbTimeout && LIBUSB_ERROR_PIPE == kUsbPipe);
static_assert(LIBUSB_ERROR_NO_DEVICE == kUsbNoDevice && LIBUSB_ERROR_INTERRUPTED == kUsbInterrupted);

class NativeLibusbApi final : public LibusbApi {
public:
  ~NativeLibusbApi() override { shutdown(); }
  int initialize() override {
    if (context_) return kUsbBusy;
    return libusb_init(&context_);
  }
  int enumerate(std::vector<UsbDeviceInfo> &devices) override {
    if (!context_) return kUsbInvalid;
    devices.clear();
    libusb_device **list = nullptr;
    const auto count = libusb_get_device_list(context_, &list);
    if (count < 0) return static_cast<int>(count);
    int result = kUsbSuccess;
    for (decltype(count + 0) i = 0; i < count; ++i) {
      libusb_device_descriptor desc{};
      const int read_result = libusb_get_device_descriptor(list[i], &desc);
      if (read_result != 0) { result = read_result; break; }
      // Retain only normal-firmware Panda candidates. Bootstub and DFU are not
      // opened, and unconfigured devices are reported without usable endpoints.
      if ((desc.idVendor != 0xbbaa && desc.idVendor != 0x3801) || desc.idProduct != 0xddcc) continue;
      UsbDeviceInfo info;
      info.id = reinterpret_cast<UsbDeviceId>(libusb_ref_device(list[i]));
      retained_.push_back(reinterpret_cast<libusb_device *>(info.id));
      info.vendor = desc.idVendor;
      info.product = desc.idProduct;
      info.bcd_device = desc.bcdDevice;
      info.serial_index = desc.iSerialNumber;
      libusb_config_descriptor *config = nullptr;
      if (libusb_get_active_config_descriptor(list[i], &config) == 0) {
        info.active_configuration_known = true;
        for (int n = 0; n < config->bNumInterfaces; ++n) {
          const auto &interface = config->interface[n];
          for (int a = 0; a < interface.num_altsetting; ++a) {
            const auto &alt = interface.altsetting[a];
            UsbAlternate item{alt.bInterfaceNumber, alt.bAlternateSetting, {}};
            for (int e = 0; e < alt.bNumEndpoints; ++e) {
              const auto &endpoint = alt.endpoint[e];
              item.endpoints.push_back({endpoint.bEndpointAddress, endpoint.bmAttributes, endpoint.wMaxPacketSize});
            }
            info.alternates.push_back(std::move(item));
          }
        }
        libusb_free_config_descriptor(config);
      }
      devices.push_back(std::move(info));
    }
    libusb_free_device_list(list, 1);
    return result;
  }
  int open(UsbDeviceId device, UsbHandle &handle) override {
    handle = 0;
    libusb_device_handle *opened = nullptr;
    const int result = libusb_open(reinterpret_cast<libusb_device *>(device), &opened);
    if (result == 0) handle = reinterpret_cast<UsbHandle>(opened);
    return result;
  }
  int serial(UsbHandle handle, std::uint8_t index, std::string &value,
             std::uint32_t timeout_ms) override {
    value.clear();
    if (index == 0) return kUsbInvalid;
    // libusb_get_string_descriptor_ascii uses an internal 1000ms timeout.
    // Read LANGID and UTF-16LE descriptors explicitly with the requested slice.
    std::array<unsigned char, 255> buffer{};
    auto *native = reinterpret_cast<libusb_device_handle *>(handle);
    int count = libusb_control_transfer(native, 0x80, LIBUSB_REQUEST_GET_DESCRIPTOR,
                                       LIBUSB_DT_STRING << 8, 0, buffer.data(), buffer.size(), timeout_ms);
    if (count < 0) return count;
    if (count < 4 || buffer[1] != LIBUSB_DT_STRING || buffer[0] < 4 || buffer[0] != count || (buffer[0] & 1)) return kUsbIo;
    const auto language = static_cast<std::uint16_t>(buffer[2] | (buffer[3] << 8));
    count = libusb_control_transfer(native, 0x80, LIBUSB_REQUEST_GET_DESCRIPTOR,
                                   static_cast<std::uint16_t>((LIBUSB_DT_STRING << 8) | index),
                                   language, buffer.data(), buffer.size(), timeout_ms);
    if (count < 0) return count;
    if (count < 2 || buffer[1] != LIBUSB_DT_STRING || buffer[0] < 2 || buffer[0] != count || (buffer[0] & 1)) return kUsbIo;
    for (int n = 2; n < buffer[0]; n += 2) {
      if (buffer[n + 1] != 0 || buffer[n] < 33 || buffer[n] > 126) return kUsbIo;
      value.push_back(static_cast<char>(buffer[n]));
    }
    return value.empty() ? kUsbIo : kUsbSuccess;
  }
  int claim(UsbHandle handle, int interface_number) override {
    return libusb_claim_interface(reinterpret_cast<libusb_device_handle *>(handle), interface_number);
  }
  int control(UsbHandle handle, std::uint8_t type, std::uint8_t request,
              std::uint16_t value, std::uint16_t index, std::uint8_t *data,
              std::uint16_t length, std::uint32_t timeout_ms) override {
    return libusb_control_transfer(reinterpret_cast<libusb_device_handle *>(handle), type,
                                    request, value, index, data, length, timeout_ms);
  }
  int bulk(UsbHandle handle, std::uint8_t endpoint, std::uint8_t *data,
           int length, int &actual, std::uint32_t timeout_ms) override {
    actual = 0;
    return libusb_bulk_transfer(reinterpret_cast<libusb_device_handle *>(handle), endpoint,
                                 data, length, &actual, timeout_ms);
  }
  void release(UsbHandle handle, int interface_number) override {
    libusb_release_interface(reinterpret_cast<libusb_device_handle *>(handle), interface_number);
  }
  void close(UsbHandle handle) override {
    libusb_close(reinterpret_cast<libusb_device_handle *>(handle));
  }
  void shutdown() override {
    for (auto *device : retained_) libusb_unref_device(device);
    retained_.clear();
    if (context_) libusb_exit(std::exchange(context_, nullptr));
  }
private:
  libusb_context *context_ = nullptr;
  std::vector<libusb_device *> retained_;
};
}  // namespace
std::unique_ptr<LibusbApi> make_native_libusb_api() {
  return std::make_unique<NativeLibusbApi>();
}
}  // namespace openpilot::pandad::compatibility
