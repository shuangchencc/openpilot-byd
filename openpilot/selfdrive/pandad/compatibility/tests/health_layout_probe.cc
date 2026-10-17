// Host-only ABI oracle. Uses the locked headers via explicit compiler -I paths.
// It does not build MCU firmware or exercise hardware. Output is one JSON value.
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <limits>

#include "health.h"
#include "sys/sys.h"
#include "opendbc/safety/can.h"
#include "opendbc/safety/declarations.h"

static_assert(sizeof(health_t) == 48);
static_assert(sizeof(can_health_t) == 64);
static_assert(sizeof(CANPacket_t) == 72);
static_assert(alignof(CANPacket_t) == 4);
static_assert(offsetof(CANPacket_t, checksum) == 5);
static_assert(offsetof(CANPacket_t, data) == 6);
static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559);

void hex(const void *data, size_t size) {
  const auto *bytes = static_cast<const uint8_t *>(data);
  std::printf("\"");
  for (size_t i = 0; i < size; ++i) std::printf("%02x", bytes[i]);
  std::printf("\"");
}

#define FIELD(type, field, name) std::printf("\"%s\":%zu,", name, offsetof(type, field))

int main() {
  const uint16_t endian = 1;
  if (*reinterpret_cast<const uint8_t *>(&endian) != 1) return 2;
  std::printf("{\"host_abi_only\":true,\"sizes\":{\"health\":%zu,\"can_health\":%zu,\"can_packet\":%zu,\"can_alignment\":%zu},",
              sizeof(health_t), sizeof(can_health_t), sizeof(CANPacket_t), alignof(CANPacket_t));
  std::printf("\"offsets\":{\"health\":{");
  FIELD(health_t, uptime_pkt, "uptime");
  FIELD(health_t, voltage_pkt, "voltage");
  FIELD(health_t, current_pkt, "current");
  FIELD(health_t, safety_tx_blocked_pkt, "safety_tx_blocked");
  FIELD(health_t, safety_rx_invalid_pkt, "safety_rx_invalid");
  FIELD(health_t, tx_buffer_overflow_pkt, "tx_buffer_overflow");
  FIELD(health_t, rx_buffer_overflow_pkt, "rx_buffer_overflow");
  FIELD(health_t, faults_pkt, "faults");
  FIELD(health_t, flags_pkt, "flags");
  FIELD(health_t, car_harness_status_pkt, "car_harness_status");
  FIELD(health_t, safety_mode_pkt, "safety_mode");
  FIELD(health_t, safety_param_pkt, "safety_param");
  FIELD(health_t, fault_status_pkt, "fault_status");
  FIELD(health_t, alternative_experience_pkt, "alternative_experience");
  FIELD(health_t, interrupt_load_pkt, "interrupt_load");
  FIELD(health_t, fan_power, "fan_power");
  FIELD(health_t, spi_error_count_pkt, "spi_error_count");
  FIELD(health_t, sbu1_voltage_mV, "sbu1_voltage_mV");
  FIELD(health_t, sbu2_voltage_mV, "sbu2_voltage_mV");
  FIELD(health_t, sound_output_level_pkt, "sound_output_level");
  std::printf("\"temperature\":%zu},\"can_health\":{", offsetof(health_t, temperature_pkt));
  FIELD(can_health_t, bus_off, "bus_off");
  FIELD(can_health_t, bus_off_cnt, "bus_off_cnt");
  FIELD(can_health_t, error_warning, "error_warning");
  FIELD(can_health_t, error_passive, "error_passive");
  FIELD(can_health_t, last_error, "last_error");
  FIELD(can_health_t, last_stored_error, "last_stored_error");
  FIELD(can_health_t, last_data_error, "last_data_error");
  FIELD(can_health_t, last_data_stored_error, "last_data_stored_error");
  FIELD(can_health_t, receive_error_cnt, "receive_error_cnt");
  FIELD(can_health_t, transmit_error_cnt, "transmit_error_cnt");
  FIELD(can_health_t, total_error_cnt, "total_error_cnt");
  FIELD(can_health_t, total_tx_lost_cnt, "total_tx_lost_cnt");
  FIELD(can_health_t, total_rx_lost_cnt, "total_rx_lost_cnt");
  FIELD(can_health_t, total_tx_cnt, "total_tx_cnt");
  FIELD(can_health_t, total_rx_cnt, "total_rx_cnt");
  FIELD(can_health_t, total_fwd_cnt, "total_fwd_cnt");
  FIELD(can_health_t, total_tx_checksum_error_cnt, "total_tx_checksum_error_cnt");
  FIELD(can_health_t, can_speed, "can_speed");
  FIELD(can_health_t, can_data_speed, "can_data_speed");
  FIELD(can_health_t, canfd_enabled, "canfd_enabled");
  FIELD(can_health_t, brs_enabled, "brs_enabled");
  FIELD(can_health_t, canfd_non_iso, "canfd_non_iso");
  FIELD(can_health_t, irq0_call_rate, "irq0_call_rate");
  FIELD(can_health_t, irq1_call_rate, "irq1_call_rate");
  FIELD(can_health_t, irq2_call_rate, "irq2_call_rate");
  std::printf("\"can_core_reset_cnt\":%zu},\"checksum\":%zu,\"data\":%zu},",
              offsetof(can_health_t, can_core_reset_cnt), offsetof(CANPacket_t, checksum), offsetof(CANPacket_t, data));

  health_t health{};
  health.uptime_pkt = 0x01020304;
  health.voltage_pkt = 12345;
  health.current_pkt = 678;
  health.safety_tx_blocked_pkt = 0x11121314;
  health.safety_rx_invalid_pkt = 0x21222324;
  health.tx_buffer_overflow_pkt = 0x31323334;
  health.rx_buffer_overflow_pkt = 0x41424344;
  health.faults_pkt = FAULT_RELAY_MALFUNCTION | FAULT_INTERRUPT_RATE_USB;
  health.flags_pkt = HEALTH_FLAG_IGNITION_LINE | HEALTH_FLAG_HEARTBEAT_LOST | HEALTH_FLAG_HARDFAULT_RESET;
  health.car_harness_status_pkt = 2;
  health.safety_mode_pkt = SAFETY_NOOUTPUT;
  health.safety_param_pkt = 0x5152;
  health.fault_status_pkt = FAULT_STATUS_TEMPORARY;
  health.alternative_experience_pkt = ALT_EXP_RAISE_LONGITUDINAL_LIMITS_TO_ISO_MAX;
  health.interrupt_load_pkt = 128;
  health.fan_power = 75;
  health.spi_error_count_pkt = 0x6162;
  health.sbu1_voltage_mV = 3300;
  health.sbu2_voltage_mV = 1200;
  health.sound_output_level_pkt = 0x7172;
  health.temperature_pkt = 65;
  std::printf("\"current_health_hex\":");
  hex(&health, sizeof(health));

  can_health_t can_health{};
  can_health.bus_off = 1;
  can_health.bus_off_cnt = 0x01020304;
  can_health.error_warning = 1;
  can_health.error_passive = 0;
  can_health.last_error = 3;
  can_health.last_stored_error = 7;
  can_health.last_data_error = 2;
  can_health.last_data_stored_error = 6;
  can_health.receive_error_cnt = 127;
  can_health.transmit_error_cnt = 255;
  can_health.total_error_cnt = 0x11121314;
  can_health.total_tx_lost_cnt = 0x21222324;
  can_health.total_rx_lost_cnt = 0x31323334;
  can_health.total_tx_cnt = 0x41424344;
  can_health.total_rx_cnt = 0x51525354;
  can_health.total_fwd_cnt = 0x61626364;
  can_health.total_tx_checksum_error_cnt = 0x71727374;
  can_health.can_speed = 500;
  can_health.can_data_speed = 2000;
  can_health.canfd_enabled = 1;
  can_health.brs_enabled = 1;
  can_health.canfd_non_iso = 0;
  can_health.irq0_call_rate = 0x81828384;
  can_health.irq1_call_rate = 0x91929394;
  can_health.irq2_call_rate = 0xa1a2a3a4;
  can_health.can_core_reset_cnt = 0xb1b2b3b4;
  std::printf(",\"can_health_hex\":");
  hex(&can_health, sizeof(can_health));

  CANPacket_t frame{};
  frame.fd = 0;
  frame.bus = 1;
  frame.data_len_code = 3;
  frame.addr = 0x123;
  frame.data[0] = 0x11;
  frame.data[1] = 0x22;
  frame.data[2] = 0x33;
  uint8_t checksum = 0;
  for (size_t i = 0; i < CANPACKET_HEAD_SIZE + GET_LEN(&frame); ++i) {
    checksum ^= reinterpret_cast<const uint8_t *>(&frame)[i];
  }
  frame.checksum = checksum;
  std::printf(",\"can_frame_hex\":");
  hex(&frame, CANPACKET_HEAD_SIZE + GET_LEN(&frame));

  frame = CANPacket_t{};
  frame.fd = 1;
  frame.bus = 2;
  frame.data_len_code = 9;
  frame.addr = 0x18da10f1;
  frame.extended = 1;
  frame.returned = 1;
  for (size_t i = 0; i < 12; ++i) frame.data[i] = static_cast<uint8_t>(i);
  checksum = 0;
  for (size_t i = 0; i < CANPACKET_HEAD_SIZE + GET_LEN(&frame); ++i) {
    checksum ^= reinterpret_cast<const uint8_t *>(&frame)[i];
  }
  frame.checksum = checksum;
  std::printf(",\"can_fd_frame_hex\":");
  hex(&frame, CANPACKET_HEAD_SIZE + GET_LEN(&frame));

  const uint32_t fault_mask = FAULT_RELAY_MALFUNCTION | FAULT_UNUSED_INTERRUPT_HANDLED | FAULT_INTERRUPT_RATE_CAN_1 |
      FAULT_INTERRUPT_RATE_CAN_2 | FAULT_INTERRUPT_RATE_CAN_3 | FAULT_INTERRUPT_RATE_TACH | FAULT_INTERRUPT_RATE_INTERRUPTS |
      FAULT_INTERRUPT_RATE_SPI_DMA | FAULT_INTERRUPT_RATE_USB | FAULT_REGISTER_DIVERGENT | FAULT_INTERRUPT_RATE_CLOCK_SOURCE |
      FAULT_INTERRUPT_RATE_TICK | FAULT_INTERRUPT_RATE_EXTI | FAULT_INTERRUPT_RATE_SPI | FAULT_SIREN_MALFUNCTION |
      FAULT_HEARTBEAT_LOOP_WATCHDOG | FAULT_INTERRUPT_RATE_SOUND_DMA;
  std::printf(",\"known_fault_mask\":%u,\"known_alternative_experience_mask\":%u,\"current_byd_safety\":%u,",
              fault_mask, ALT_EXP_DISABLE_STOCK_AEB | ALT_EXP_RAISE_LONGITUDINAL_LIMITS_TO_ISO_MAX | ALT_EXP_ALLOW_AEB, SAFETY_BYD);
  // These expressions mirror the pinned get_health_pkt float32 operations.
  // They validate host rounding, not target sensor inputs or MCU execution.
  const float loads[] = {-1.0F, 0.0F, 0.0019607844296842813F, 0.5F, 1.0F, 2.0F};
  const float temperatures[] = {-100.0F, -40.0F, -39.5F, 25.5F, 214.5F, 300.0F};
  std::printf("\"quantization_vectors\":[");
  for (size_t i = 0; i < 6; ++i) {
    const float scaled_load = (std::clamp(loads[i], 0.0F, 1.0F) * 255.0F) + 0.5F;
    const float encoded_temperature = (std::clamp(temperatures[i], -40.0F, 214.5F) + 40.0F) + 0.5F;
    std::printf("%s{\"load\":%.17g,\"temperature\":%.17g,\"encoded_load\":%u,\"encoded_temperature\":%u}",
                i == 0 ? "" : ",", static_cast<double>(loads[i]), static_cast<double>(temperatures[i]),
                static_cast<unsigned>(static_cast<uint8_t>(scaled_load)), static_cast<unsigned>(static_cast<uint8_t>(encoded_temperature)));
  }
  std::printf("]}\n");
}
