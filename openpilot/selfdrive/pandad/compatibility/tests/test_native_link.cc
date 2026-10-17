#include "usb_backend.h"
#ifdef __linux__
#include "linux_spi_io.h"
#endif
#include <iostream>
#include <stdexcept>
using namespace openpilot::pandad::compatibility;
int main() {
  // Construction/destruction and rejected default opening must not initialize
  // libusb, enumerate, claim or access a device. Tests run with /dev denied.
  UsbBackend usb;
  if (usb.is_open() || usb.open_bound({}) != Status::Invalid) {
    throw std::runtime_error("native USB default must remain inert");
  }
#ifdef __linux__
  LinuxSpiIo spi;
  if (spi.is_open()) throw std::runtime_error("native SPI constructor opened hardware");
#endif
  std::cout << "{\"suite\":\"native_link\",\"passed\":1,\"failed\":0,\"device_open\":false}\n";
}
