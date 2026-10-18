// P6 executes the pinned safety hooks and P5 common CAN/firmware liveness C.
// Only registers, peripheral calls, scheduler and transport endpoints are fake.
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define MIN(a,b) ((a) < (b) ? (a) : (b))
#define ENTER_CRITICAL() do {} while (0)
#define EXIT_CRITICAL() do {} while (0)
#define MAX_CAN_MSGS_PER_USB_BULK_TRANSFER 5U
#define MAX_CAN_MSGS_PER_SPI_BULK_TRANSFER 5U
#include "board/drivers/drivers.h"
#if P6_TARGET_F4
#define STM32F4
#else
#define STM32H7
#endif
static uint32_t now_us;
uint32_t microsecond_timer_get(void) { return now_us; }
#include "opendbc/safety/safety.h"
static unsigned processed[3], initialized;
bool can_init(uint8_t num) { (void)num; ++initialized; return true; }
void process_can(uint8_t num) { ++processed[num]; }
void print(const char *s) { (void)s; }
void puth(unsigned int i) { (void)i; }
void can_tx_comms_resume_usb(void) {}
void can_tx_comms_resume_spi(void) {}
#if defined(__APPLE__) && !P6_TARGET_F4
// Native Mach-O requires a segment name. Preserve each H7 queue section while
// adapting its host linker spelling; this does not model MCU RAM placement.
#define section(name) section("__DATA," name)
#endif
#include "board/drivers/can_common.h"
#if defined(__APPLE__) && !P6_TARGET_F4
#undef section
#endif
#include "board/can_comms.h"
#include "board/sys/faults.h"

static unsigned assertions;
#define CHECK(x) do { ++assertions; if (!(x)) { fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x); exit(1); } } while (0)
enum { HARNESS_STATUS_NC = 0, HARNESS_STATUS_FLIPPED = 2, CAN_MODE_NORMAL = 0,
       CAN_MODE_OBD_CAN2 = 1, LED_GREEN = 1, LED_BLUE = 2 };
#undef CANIF_FROM_CAN_NUM
#define CANIF_FROM_CAN_NUM(x) (x)
static struct { uint8_t status; } harness;
static struct { uint32_t SR; } fake_timer;
#define TICK_TIMER (&fake_timer)
static uint32_t heartbeat_counter, heartbeat_tick_counter, uptime_cnt;
static bool heartbeat_lost, heartbeat_disabled, siren_enabled, ignition_line, som_gpio;
bool power_save_enabled;
static unsigned relay_calls, mode_calls, fan_calls, ir_calls, host_cleanup_calls;
static uint32_t board_voltage = 12000U;
static uint32_t voltage(void) { return board_voltage; }
static void siren(bool value) { (void)value; }
static void can_mode(unsigned mode) { (void)mode; ++mode_calls; }
static void ir(uint32_t value) { CHECK(value == 0U); ++ir_calls; }
static bool som(void) { return som_gpio; }
static struct {
  uint32_t (*read_voltage_mV)(void);
  void (*set_siren)(bool);
  void (*set_can_mode)(unsigned);
  void (*set_ir_power)(uint32_t);
  bool (*read_som_gpio)(void);
} board = {voltage, siren, can_mode, ir, som};
#define current_board (&board)
static void assert_fatal(bool condition, const char *message) { (void)message; CHECK(condition); }
static void set_intercept_relay(bool intercept, bool ignition) { (void)intercept; (void)ignition; ++relay_calls; }
static void can_clear_send(unsigned interface, unsigned bus) { (void)interface; (void)bus; }
void set_power_save_state(bool enabled) { power_save_enabled = enabled; }
void fan_tick(void) {}
static void harness_tick(void) {}
static void sound_tick(void) {}
static void led_set(unsigned led, bool enabled) { (void)led; (void)enabled; }
static bool harness_check_ignition(void) { return ignition_line; }
void bootkick_tick(bool started, bool recent) { (void)started; (void)recent; }
void fan_set_power(uint8_t power) { CHECK(power == 0U || power == 30U); ++fan_calls; }
void check_registers(void) {}
struct fake_request { uint8_t request; uint16_t param1; };
#include "firmware_source.inc"

static CANPacket_t packet(unsigned addr, unsigned bus, unsigned dlc) {
  CANPacket_t p = {0}; p.addr = addr; p.bus = bus; p.data_len_code = dlc;
  can_set_checksum(&p); return p;
}
static void reset_mode(uint16_t mode, uint16_t param) {
  set_safety_mode(mode, param);
  memset(processed, 0, sizeof(processed));
  can_clear(&can_rx_q); comms_can_reset();
}
static unsigned queued(void) {
  unsigned n = 0U;
  for (unsigned i = 0; i < 3U; ++i) n += can_queues[i]->fifo_size - 1U - can_slots_empty(can_queues[i]);
  return n;
}
static void send(CANPacket_t p) {
  can_set_checksum(&p);
  comms_can_write((const uint8_t *)&p, CANPACKET_HEAD_SIZE + GET_LEN(&p));
}
static void trace_state(const char *event) {
  printf("TRACE {\"event\":\"%s\",\"mode\":%u,\"param\":%u,\"controls\":%s,\"queued\":%u,\"processed\":[%u,%u,%u],\"blocked\":%u,\"heartbeat\":%u,\"host_cleanup_calls\":%u}\n",
         event, current_safety_mode, current_safety_param, controls_allowed ? "true" : "false", queued(),
         processed[0], processed[1], processed[2], safety_tx_blocked, heartbeat_counter, host_cleanup_calls);
}
static void deny_modes_all_local_and_host_buses(void) {
  const uint16_t modes[] = {SAFETY_SILENT, SAFETY_NOOUTPUT};
  for (unsigned m = 0; m < 2U; ++m) {
    for (unsigned bus = 0; bus < 8U; ++bus) {
      for (unsigned dlc = 0; dlc < 16U; ++dlc) {
        reset_mode(modes[m], 0U); controls_allowed = true;
        CANPacket_t p = packet(0x1E2U, bus, dlc); send(p);
        CHECK(queued() == 0U && processed[0] == 0U && processed[1] == 0U && processed[2] == 0U);
        CHECK(safety_tx_blocked == 1U);
        CHECK(safety_fwd_hook(bus, p.addr) == -1);
      }
    }
  }
  trace_state("deny_all_buses_and_lengths");
}
static void legacy_enum32_param33_falls_back_silent(void) {
  CHECK(SAFETY_BYD == 35U && SAFETY_RIVIAN == 33U);
  CHECK(set_safety_hooks(32U, 33U) == -1);
  reset_mode(32U, 33U);
  CHECK(current_safety_mode == SAFETY_SILENT && current_safety_param == 0U && can_silent);
  CHECK(!controls_allowed); send(packet(0x1E2U, 0U, 8U)); CHECK(queued() == 0U);
  trace_state("legacy32_param33_no_translation");
}
static void checksum_and_response_flags_cannot_bypass(void) {
  reset_mode(SAFETY_ELM327, 0U);
  for (unsigned i = 0; i < 14U; ++i) {
    CANPacket_t p = packet(0x700U, 0U, 8U);
    ((uint8_t *)&p)[i] ^= i == 0U ? 1U : 0x80U;
    comms_can_write((uint8_t *)&p, 14U);
    CHECK(queued() == 0U);
  }
  CHECK(safety_tx_blocked == 14U);
  for (unsigned variant = 0; variant < 4U; ++variant) {
    CANPacket_t p = packet(0x700U, 0U, 8U);
    if (variant == 0U) p.returned = 1U;
    if (variant == 1U) p.rejected = 1U;
    if (variant == 2U) p.bus = 7U;
    if (variant == 3U) p.addr = 0x800U;
    send(p); CHECK(queued() == 0U);
  }
  CHECK(safety_tx_blocked == 18U);
  trace_state("malformed_host_packet_before_safety");
}
static void rejected_frame_retains_local_bus(void) {
  for (unsigned bus = 0; bus < 3U; ++bus) {
    reset_mode(SAFETY_NOOUTPUT, 0U);
    CANPacket_t p = packet(0x321U, bus, 8U); send(p);
    CANPacket_t rejected; CHECK(can_pop(&can_rx_q, &rejected));
    CHECK(rejected.bus == bus && rejected.addr == 0x321U && rejected.rejected && !rejected.returned);
    CHECK(can_check_checksum(&rejected)); CHECK(!can_pop(&can_rx_q, &p));
    CHECK(queued() == 0U && processed[0] + processed[1] + processed[2] == 0U);
    printf("TRACE {\"event\":\"rejected_packet\",\"local_bus\":%u,\"wire_hex\":\"", bus);
    for (unsigned i = 0; i < 14U; ++i) printf("%02x", ((uint8_t *)&rejected)[i]);
    puts("\",\"alternate_local_sends\":0}");
  }
}
static void partial_packet_reset_does_not_send(void) {
  reset_mode(SAFETY_ELM327, 0U);
  CANPacket_t p = packet(0x700U, 0U, 8U);
  comms_can_write((uint8_t *)&p, 7U); CHECK(queued() == 0U);
  comms_can_reset(); CHECK(can_write_buffer.ptr == 0U && can_read_buffer.ptr == 0U);
  send(packet(0x700U, 0U, 8U)); CHECK(queued() == 1U && processed[0] == 1U);
  trace_state("fresh_packet_only_after_stream_reset");
}
static void fd_capability_differs_without_policy_drift(void) {
  reset_mode(SAFETY_ELM327, 0U);
  CANPacket_t p = packet(0x700U, 0U, 8U); p.fd = 1U; send(p);
  CHECK(queued() == (P6_TARGET_F4 ? 0U : 1U));
  CHECK(safety_tx_blocked == (P6_TARGET_F4 ? 1U : 0U));
  // This case is intentionally excluded from classic-frame parity comparison.
  trace_state("f4_fd_denied_h7_existing_fd_header_semantics");
}
static void second(void) {
  for (unsigned i = 0; i < 8U; ++i) { now_us += 125000U; TICK_TIMER->SR = 1U; tick_handler(); CHECK(TICK_TIMER->SR == 0U); }
}
static void start_liveness(bool ignition) {
  reset_mode(SAFETY_TOYOTA, 0U); ignition_line = ignition;
  controls_allowed = true;  // Inject pre-existing engagement, not spoofed CAN RX.
  const struct fake_request req = {0xf3U, 1U}; heartbeat_control(&req);
  initialize_heartbeat_watchdog(); CHECK(wd_state.threshold == 375000U);
}
static void abrupt_host_loss(bool ignition, bool usb_power_loss) {
  start_liveness(ignition);
  // Model commands accepted before the host disappeared. HAL completion is
  // deliberately paused so the real SILENT transition must clear all queues.
  for (unsigned bus = 0; bus < 3U; ++bus) {
    CANPacket_t pending = packet(0x700U, bus, 8U);
    CHECK(can_push(can_queues[bus], &pending));
  }
  CHECK(queued() == 3U);
  // Host process receives no cleanup callback and sends no further commands.
  // A separately powered MCU keeps executing its real 8 Hz timer path.
  if (usb_power_loss) board_voltage = 5000U;
  const uint32_t limit = ignition ? 5U : 2U;
  for (uint32_t n = 1U; n <= limit; ++n) {
    second(); CHECK(heartbeat_counter == n); CHECK(host_cleanup_calls == 0U);
    CHECK(current_safety_mode == (n < limit ? SAFETY_TOYOTA : SAFETY_SILENT));
    CHECK(queued() == (n < limit ? 3U : 0U));
  }
  CHECK(!controls_allowed && !heartbeat_engaged && heartbeat_lost && can_silent && power_save_enabled);
  CHECK(queued() == 0U && fan_calls != 0U && ir_calls != 0U);
  trace_state(usb_power_loss ? "usb_power_lost_mcu_remains_powered" : "abrupt_process_death_no_host_cleanup");
}
static void whole_mcu_power_loss_has_no_software_action(void) {
  start_liveness(true); const unsigned before_modes = mode_calls;
  now_us = 6000000U;  // Time in the external model advances; MCU executes nothing.
  CHECK(heartbeat_counter == 0U && current_safety_mode == SAFETY_TOYOTA);
  CHECK(mode_calls == before_modes && faults == 0U && host_cleanup_calls == 0U);
  trace_state("whole_mcu_power_loss_no_execution_or_claimed_cleanup");
}
static void stopped_loop_watchdog_only_on_resume(void) {
  start_liveness(true); now_us = 375001U;
  CHECK(faults == 0U && heartbeat_counter == 0U);
  simple_watchdog_kick(); CHECK(faults == FAULT_HEARTBEAT_LOOP_WATCHDOG);
  CHECK(current_safety_mode == SAFETY_TOYOTA && heartbeat_counter == 0U);
  trace_state("software_watchdog_fault_on_resume_is_not_silent");
}
static void watchdog_threshold_and_wrap(void) {
  start_liveness(true);
  for (unsigned wrap = 0; wrap < 2U; ++wrap) {
    for (unsigned delta = 374999U; delta <= 375001U; ++delta) {
      faults = 0U; now_us = wrap ? UINT32_MAX - 100U : 100U;
      initialize_heartbeat_watchdog(); now_us += delta; simple_watchdog_kick();
      CHECK((faults != 0U) == (delta > 375000U)); CHECK(current_safety_mode == SAFETY_TOYOTA);
    }
  }
  trace_state("watchdog_strict_375000_and_one_wrap");
}

#ifdef ALLOW_DEBUG
static CANPacket_t byd_rx(unsigned addr, unsigned counter, bool engaged) {
  CANPacket_t p = packet(addr, addr == 0x32DU ? 2U : 0U, addr == 0x11FU ? 5U : 8U);
  if (addr == 0x32DU) { p.data[2] = engaged ? (3U << 3) : 0U; p.data[6] = counter & 15U; }
  if (addr == 0x1F0U) p.data[6] = (counter & 15U) << 4;
  uint8_t sum = 0U; for (unsigned i = 0; i < 7U; ++i) sum += p.data[i];
  p.data[7] = 0xFFU - sum; can_set_checksum(&p); return p;
}
static void feed_byd(unsigned counter, bool engaged) {
  const unsigned addresses[] = {0x11FU, 0x1F0U, 0x242U, 0x32DU};
  for (unsigned i = 0; i < 4U; ++i) { CANPacket_t p = byd_rx(addresses[i], counter, engaged); CHECK(safety_rx_hook(&p)); }
}
static void fresh_byd(void) {
  reset_mode(SAFETY_BYD, 0U);
  for (unsigned i = 0; i < 16U; ++i) { now_us = 1000U + i * 20000U; feed_byd(i, true); }
  CHECK(controls_allowed); safety_tick(); CHECK(controls_allowed && !safety_rx_checks_invalid);
}
static void assert_disengaged_steer_denied(void) {
  CANPacket_t steer = packet(0x1E2U, 0U, 8U);
  steer.data[2] = 1U << 5; steer.data[3] = 100U; send(steer);
  CHECK(queued() == 0U && safety_tx_blocked == 1U);
}
static void byd_debug_reference_only(void) {
  reset_mode(SAFETY_BYD, 33U);
  CHECK(current_safety_mode == 35U && current_safety_param == 33U && !controls_allowed);
  CHECK(current_safety_config.rx_checks_len == 4 && current_safety_config.tx_msgs_len == 3);
  CHECK(current_safety_config.tx_msgs[0].addr == 0x1E2 && current_safety_config.tx_msgs[0].bus == 0U);
  // Current byd_init ignores param. Acceptance is ATTO3 reference semantics;
  // param 33 cannot recreate the private legacy enum32 behavior.
  trace_state("byd35_debug_atto3_param_ignored_not_songplus");
}
static void byd_wrong_bus_and_dlc(void) {
  for (unsigned bus = 0; bus < 8U; ++bus) {
    for (unsigned dlc = 0; dlc < 16U; ++dlc) {
      reset_mode(SAFETY_BYD, 0U); send(packet(0x316U, bus, dlc));
      CHECK(queued() == ((bus == 0U && dlc == 8U) ? 1U : 0U));
    }
  }
  trace_state("byd_tx_whitelist_exact_bus_length");
}
static void byd_wrong_rx_bus_length_cannot_enable(void) {
  for (unsigned bus = 0; bus < 3U; ++bus) {
    for (unsigned dlc = 7U; dlc <= 9U; ++dlc) {
      if (bus == 2U && dlc == 8U) continue;
      reset_mode(SAFETY_BYD, 0U);
      CANPacket_t p = byd_rx(0x32DU, 1U, true); p.bus = bus; p.data_len_code = dlc;
      (void)safety_rx_hook(&p); CHECK(!controls_allowed);
      CHECK(!current_safety_config.rx_checks[3].status.msg_seen);
    }
  }
  trace_state("wrong_rx_bus_length_not_whitelisted");
}
static void byd_payload_checksum_and_counter(void) {
  fresh_byd(); CANPacket_t p = byd_rx(0x32DU, 0U, true); p.data[7] ^= 1U;
  CHECK(!safety_rx_hook(&p)); CHECK(!controls_allowed);
  CHECK(!current_safety_config.rx_checks[3].status.valid_checksum);
  assert_disengaged_steer_denied();
  fresh_byd();
  for (unsigned i = 1U; i <= 5U; ++i) {
    p = byd_rx(0x32DU, 15U, true); const bool valid = safety_rx_hook(&p);
    CHECK(valid == (i < 5U)); CHECK(controls_allowed == (i < 5U));
    CHECK(current_safety_config.rx_checks[3].status.wrong_counters == (int)i);
  }
  assert_disengaged_steer_denied();
  trace_state("bad_payload_checksum_or_fifth_wrong_counter_exits");
}
static void byd_freshness_boundary_and_wrap(void) {
  for (unsigned wrap = 0; wrap < 2U; ++wrap) {
    fresh_byd(); now_us = wrap ? UINT32_MAX - 100U : 1000U;
    feed_byd(0U, true); const uint32_t base = now_us;
    now_us = base + 1000000U; safety_tick(); CHECK(controls_allowed && !safety_rx_checks_invalid);
    now_us = base + 1000001U; safety_tick(); CHECK(!controls_allowed && safety_rx_checks_invalid);
    assert_disengaged_steer_denied();
  }
  trace_state("rx_lag_strict_1000000_and_one_wrap");
}
static void byd_relay_and_forwarding_block(void) {
  fresh_byd(); CHECK(safety_fwd_hook(2, 0x1E2) == -1);
  CHECK(safety_fwd_hook(2, 0x316) == -1); CHECK(safety_fwd_hook(1, 0x123) == -1);
  CHECK(safety_fwd_hook(0, 0x123) == 2); CHECK(safety_fwd_hook(2, 0x123) == 0);
  safety_mode_cnt = 2U; CANPacket_t stock = packet(0x1E2U, 0U, 8U);
  CHECK(safety_rx_hook(&stock)); CHECK(relay_malfunction);
  for (unsigned bus = 0; bus < 3U; ++bus) {
    CHECK(safety_fwd_hook(bus, 0x123) == -1); send(packet(0x316U, bus, 8U)); CHECK(queued() == 0U);
  }
  CHECK(safety_tx_blocked == 3U); CHECK(processed[0] + processed[1] + processed[2] == 0U);
  trace_state("relay_fault_tx_and_forwarding_denied_all_local_buses");
}
static void byd_disengaged_steering_rejected(void) {
  reset_mode(SAFETY_BYD, 0U); CANPacket_t steer = packet(0x1E2U, 0U, 8U);
  steer.data[2] = 1U << 5; steer.data[3] = 100U; send(steer);
  CHECK(queued() == 0U && safety_tx_blocked == 1U && current_safety_mode == SAFETY_BYD);
  trace_state("disengaged_steering_denied_without_mode_change");
}
#else
static void release_byd35_unavailable(void) {
  CHECK(set_safety_hooks(SAFETY_BYD, 0U) == -1);
  reset_mode(SAFETY_BYD, 33U); CHECK(current_safety_mode == SAFETY_SILENT && current_safety_param == 0U);
  for (unsigned bus = 0; bus < 3U; ++bus) { send(packet(0x1E2U, bus, 8U)); CHECK(queued() == 0U); }
  trace_state("release_byd35_unavailable_falls_back_silent");
}
#endif

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  const char *name = argv[1];
#define CASE(fn) if (!strcmp(name, #fn)) fn()
  CASE(deny_modes_all_local_and_host_buses);
  else CASE(legacy_enum32_param33_falls_back_silent);
  else CASE(checksum_and_response_flags_cannot_bypass);
  else CASE(rejected_frame_retains_local_bus);
  else CASE(partial_packet_reset_does_not_send);
  else CASE(fd_capability_differs_without_policy_drift);
  else if (!strcmp(name, "abrupt_process_death_ignition_on")) abrupt_host_loss(true, false);
  else if (!strcmp(name, "abrupt_process_death_ignition_off")) abrupt_host_loss(false, false);
  else if (!strcmp(name, "usb_power_loss_mcu_alive")) abrupt_host_loss(true, true);
  else CASE(whole_mcu_power_loss_has_no_software_action);
  else CASE(stopped_loop_watchdog_only_on_resume);
  else CASE(watchdog_threshold_and_wrap);
#ifdef ALLOW_DEBUG
  else CASE(byd_debug_reference_only);
  else CASE(byd_wrong_bus_and_dlc);
  else CASE(byd_wrong_rx_bus_length_cannot_enable);
  else CASE(byd_payload_checksum_and_counter);
  else CASE(byd_freshness_boundary_and_wrap);
  else CASE(byd_relay_and_forwarding_block);
  else CASE(byd_disengaged_steering_rejected);
#else
  else CASE(release_byd35_unavailable);
#endif
  else return 3;
  printf("{\"case\":\"%s\",\"assertions\":%u,\"failed\":0,\"skipped\":0}\n", name, assertions);
  return 0;
}
