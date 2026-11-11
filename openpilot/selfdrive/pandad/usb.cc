// USB transport for pandad, restored for this device.
//
// Upstream removed USB from the C++ pandad once every supported device had an SPI-attached
// panda (comma 3X, comma four). This device - a comma three-class C3XL - reaches the car
// through a red panda (H7) that is on USB only, so without this file pandad cannot talk to it
// at all. Measured 2026-11-12: ./pandad spun on "SPI: timed out" against the red panda's
// serial while the panda sat on USB. The panda firmware and the python library still speak
// USB, and so did upstream's C++ pandad until the removal: vendor control requests on
// endpoint 0, CAN on bulk endpoints 0x81 (in) and 3 (out). This is that transport. The CAN
// stream format is shared with SPI and lives in panda.cc, unchanged.

#include <algorithm>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

#include "common/swaglog.h"
#include "selfdrive/pandad/panda_comms.h"

namespace {

// comma's VID, and the pre-2021 one the python library still accepts
const uint16_t PANDA_VIDS[] = {0x3801, 0xbbaa};
// application firmware; the bootstub (0xddee) is handled by pandad.py, never by ./pandad
const uint16_t PANDA_PID_APP = 0xddcc;

bool is_panda_app(const libusb_device_descriptor &desc) {
  return desc.idProduct == PANDA_PID_APP &&
         std::find(std::begin(PANDA_VIDS), std::end(PANDA_VIDS), desc.idVendor) != std::end(PANDA_VIDS);
}

libusb_context *init_usb_ctx() {
  libusb_context *context = nullptr;
  int err = libusb_init(&context);
  if (err != 0) {
    LOGE("libusb initialization error %d", err);
    return nullptr;
  }
#if LIBUSB_API_VERSION >= 0x01000106
  libusb_set_option(context, LIBUSB_OPTION_LOG_LEVEL, LIBUSB_LOG_LEVEL_INFO);
#endif
  return context;
}

std::string read_serial(libusb_device_handle *h, const libusb_device_descriptor &desc) {
  unsigned char buf[64] = {0};
  int ret = libusb_get_string_descriptor_ascii(h, desc.iSerialNumber, buf, sizeof(buf));
  return (ret > 0) ? std::string((char *)buf, ret) : std::string();
}

}  // namespace


PandaUsbHandle::PandaUsbHandle(std::string serial) {
  ctx = init_usb_ctx();
  if (!ctx) {
    throw std::runtime_error("Error connecting to panda: libusb init failed");
  }

  libusb_device **dev_list = nullptr;
  ssize_t num_devices = libusb_get_device_list(ctx, &dev_list);
  for (ssize_t i = 0; i < num_devices && dev_handle == nullptr; ++i) {
    libusb_device_descriptor desc;
    if (libusb_get_device_descriptor(dev_list[i], &desc) != 0 || !is_panda_app(desc)) {
      continue;
    }
    libusb_device_handle *h = nullptr;
    if (libusb_open(dev_list[i], &h) != 0 || h == nullptr) {
      continue;
    }
    std::string s = read_serial(h, desc);
    if (!s.empty() && (serial.empty() || s == serial)) {
      dev_handle = h;
      hw_serial = s;
    } else {
      libusb_close(h);
    }
  }
  if (dev_list != nullptr) {
    libusb_free_device_list(dev_list, 1);
  }

  if (dev_handle == nullptr) {
    cleanup();
    throw std::runtime_error("Error connecting to panda: no USB panda with serial '" + serial + "'");
  }

  if (libusb_kernel_driver_active(dev_handle, 0) == 1) {
    libusb_detach_kernel_driver(dev_handle, 0);
  }

  // Only set the configuration if it is not already 1: on an already-configured device
  // libusb's set_configuration acts as a lightweight reset, which there is no reason to do.
  int config = -1;
  int err = libusb_get_configuration(dev_handle, &config);
  if (err == 0 && config != 1) {
    err = libusb_set_configuration(dev_handle, 1);
  }
  if (err == 0) {
    err = libusb_claim_interface(dev_handle, 0);
  }
  if (err != 0) {
    LOGE("USB panda %s: configure/claim failed: %d (%s)", hw_serial.c_str(), err, libusb_strerror((enum libusb_error)err));
    cleanup();
    throw std::runtime_error("Error connecting to panda: USB claim failed");
  }
}

PandaUsbHandle::~PandaUsbHandle() {
  std::lock_guard lk(hw_lock);
  cleanup();
  connected = false;
}

void PandaUsbHandle::cleanup() {
  if (dev_handle != nullptr) {
    libusb_release_interface(dev_handle, 0);
    libusb_close(dev_handle);
    dev_handle = nullptr;
  }
  if (ctx != nullptr) {
    libusb_exit(ctx);
    ctx = nullptr;
  }
}

std::vector<std::string> PandaUsbHandle::list() {
  std::vector<std::string> serials;
  libusb_context *context = init_usb_ctx();
  if (context == nullptr) {
    return serials;
  }

  libusb_device **dev_list = nullptr;
  ssize_t num_devices = libusb_get_device_list(context, &dev_list);
  for (ssize_t i = 0; i < num_devices; ++i) {
    libusb_device_descriptor desc;
    if (libusb_get_device_descriptor(dev_list[i], &desc) != 0 || !is_panda_app(desc)) {
      continue;
    }
    libusb_device_handle *h = nullptr;
    if (libusb_open(dev_list[i], &h) != 0 || h == nullptr) {
      continue;
    }
    std::string s = read_serial(h, desc);
    libusb_close(h);
    if (!s.empty()) {
      serials.push_back(s);
    }
  }
  if (dev_list != nullptr) {
    libusb_free_device_list(dev_list, 1);
  }
  libusb_exit(context);
  return serials;
}

void PandaUsbHandle::handle_usb_issue(int err, const char func[]) {
  LOGE_100("usb error %d \"%s\" in %s", err, libusb_strerror((enum libusb_error)err), func);
  if (err == LIBUSB_ERROR_NO_DEVICE) {
    LOGE("lost connection");
    connected = false;
  }
}

int PandaUsbHandle::control_write(uint8_t bRequest, uint16_t wValue, uint16_t wIndex, unsigned int timeout) {
  const uint8_t bmRequestType = LIBUSB_ENDPOINT_OUT | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE;
  if (!connected) {
    return LIBUSB_ERROR_NO_DEVICE;
  }

  int err;
  std::lock_guard lk(hw_lock);
  do {
    err = libusb_control_transfer(dev_handle, bmRequestType, bRequest, wValue, wIndex, NULL, 0, timeout);
    if (err < 0) handle_usb_issue(err, __func__);
  } while (err < 0 && connected);
  return err;
}

int PandaUsbHandle::control_read(uint8_t bRequest, uint16_t wValue, uint16_t wIndex, unsigned char *data, uint16_t wLength, unsigned int timeout) {
  const uint8_t bmRequestType = LIBUSB_ENDPOINT_IN | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_DEVICE;
  if (!connected) {
    return LIBUSB_ERROR_NO_DEVICE;
  }

  int err;
  std::lock_guard lk(hw_lock);
  do {
    err = libusb_control_transfer(dev_handle, bmRequestType, bRequest, wValue, wIndex, data, wLength, timeout);
    if (err < 0) handle_usb_issue(err, __func__);
  } while (err < 0 && connected);
  return err;
}

int PandaUsbHandle::bulk_write(unsigned char endpoint, unsigned char* data, int length, unsigned int timeout) {
  if (!connected) {
    return 0;
  }

  int err;
  int transferred = 0;
  std::lock_guard lk(hw_lock);
  do {
    // If the panda's receive buffer is full it NAKs and libusb retries until the timeout.
    // On timeout the messages are dropped, as upstream did.
    err = libusb_bulk_transfer(dev_handle, endpoint, data, length, &transferred, timeout);
    if (err == LIBUSB_ERROR_TIMEOUT) {
      LOGW("Transmit buffer full");
      break;
    } else if (err != 0 || length != transferred) {
      handle_usb_issue(err, __func__);
    }
  } while (err != 0 && connected);
  return transferred;
}

int PandaUsbHandle::bulk_read(unsigned char endpoint, unsigned char* data, int length, unsigned int timeout) {
  if (!connected) {
    return 0;
  }

  int err;
  int transferred = 0;
  std::lock_guard lk(hw_lock);
  do {
    err = libusb_bulk_transfer(dev_handle, endpoint, data, length, &transferred, timeout);
    if (err == LIBUSB_ERROR_TIMEOUT) {
      break;  // a timeout still returns whatever was received
    } else if (err == LIBUSB_ERROR_OVERFLOW) {
      comms_healthy = false;
      LOGE_100("overflow got 0x%x", transferred);
    } else if (err != 0) {
      handle_usb_issue(err, __func__);
    }
  } while (err != 0 && connected);
  return transferred;
}
