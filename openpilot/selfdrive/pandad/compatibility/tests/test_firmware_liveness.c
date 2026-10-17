// Host oracle for the actual pinned firmware C. Source extraction is performed
// by run_firmware_liveness.py; no copied candidate liveness model is used here.
// HAL calls below only record intent. In particular set_safety_hooks accepts
// and records a mode, without pretending to exercise MCU CAN safety hooks.
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "firmware_liveness_constants.h"

static unsigned assertions;
#define CHECK(x) do { ++assertions; if (!(x)) { \
  fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #x); exit(1); } } while (0)

enum { HARNESS_STATUS_NC = 0, HARNESS_STATUS_FLIPPED = 2, CAN_MODE_NORMAL = 0,
       CAN_MODE_OBD_CAN2 = 1, LED_GREEN = 1, LED_BLUE = 2 };
#define CANIF_FROM_CAN_NUM(x) (x)
struct fake_timer { uint32_t SR; };
static struct fake_timer fake_timer;
#define TICK_TIMER (&fake_timer)
struct fake_request { uint8_t request; uint16_t param1; };
static struct { uint8_t status; } harness;

static uint32_t heartbeat_counter, heartbeat_tick_counter, heartbeat_engaged_mismatches;
static bool heartbeat_lost, heartbeat_disabled, heartbeat_engaged;
static bool controls_allowed, siren_enabled, relay_malfunction, power_save_enabled;
static bool ignition_can, can_silent;
static uint32_t uptime_cnt, safety_mode_cnt, ignition_can_cnt;
static uint16_t current_safety_mode, current_safety_param;
static uint32_t safety_tx_blocked, safety_rx_invalid;
static uint32_t now_us;
static bool ignition_line, som_gpio;
static unsigned safety_calls, power_calls, ir_calls, fan_calls, can_init_calls, safety_tick_calls;
static uint16_t last_mode, last_param;
static uint32_t last_ir, last_fan;

static void print(const char *text) { (void)text; }
static void puth(uint32_t value) { (void)value; }
static uint32_t microsecond_timer_get(void) { return now_us; }
static uint32_t voltage(void) { return 12000U; }
static void siren(bool enabled) { (void)enabled; }
static void can_mode(unsigned mode) { (void)mode; }
static void ir(uint32_t power) { ++ir_calls; last_ir = power; }
static bool som(void) { return som_gpio; }
static struct {
  uint32_t (*read_voltage_mV)(void);
  void (*set_siren)(bool);
  void (*set_can_mode)(unsigned);
  void (*set_ir_power)(uint32_t);
  bool (*read_som_gpio)(void);
} board = {voltage, siren, can_mode, ir, som};
#define current_board (&board)

static int set_safety_hooks(uint16_t mode, uint16_t param) {
  ++safety_calls; last_mode = mode; last_param = param;
  current_safety_mode = mode; current_safety_param = param;
  return 0;
}
static void assert_fatal(bool condition, const char *message) { (void)message; CHECK(condition); }
static void set_intercept_relay(bool intercept, bool ignition) { (void)intercept; (void)ignition; }
static void can_clear_send(unsigned interface, unsigned bus) { (void)interface; (void)bus; }
static void can_init_all(void) { ++can_init_calls; }
static void set_power_save_state(bool enabled) { ++power_calls; power_save_enabled = enabled; }
static void fan_tick(void) {}
static void harness_tick(void) {}
static void sound_tick(void) {}
static void can_set_orientation(bool flipped) { (void)flipped; }
static void led_set(unsigned led, bool enabled) { (void)led; (void)enabled; }
static bool harness_check_ignition(void) { return ignition_line; }
static void bootkick_tick(bool started, bool recent_heartbeat) { (void)started; (void)recent_heartbeat; }
static void fan_set_power(uint32_t power) { ++fan_calls; last_fan = power; }
static void check_registers(void) {}
static void safety_tick(void) { ++safety_tick_calls; }

#include "firmware_liveness_source.h"

static void init(bool started) {
  current_safety_mode = SAFETY_TOYOTA;  // Arbitrary pinned car-mode predicate fixture, not a vehicle claim.
  current_safety_param = 123U;
  ignition_line = started;
  heartbeat_engaged = true;
  initialize_heartbeat_watchdog();
  CHECK(wd_state.threshold == 375000U);
  CHECK(wd_state.fault == FAULT_HEARTBEAT_LOOP_WATCHDOG);
  CHECK(HEARTBEAT_IGNITION_CNT_ON == 5U);
  CHECK(HEARTBEAT_IGNITION_CNT_OFF == 2U);
}

static void control(uint8_t request, uint16_t param) {
  const struct fake_request req = {request, param};
  heartbeat_control(&req);
}

static void second(void) {
  // The untouched handler decimates eight 125 ms fake ticks to one counter step.
  for (unsigned i = 0; i < 8U; ++i) {
    now_us += 125000U;
    TICK_TIMER->SR = 1U;
    tick_handler();
    CHECK(TICK_TIMER->SR == 0U);
  }
}

static void heartbeat_write_exact_semantics(void) {
  init(true);
  const uint16_t params[] = {0U, 1U, 2U, UINT16_MAX};
  for (unsigned i = 0; i < sizeof(params) / sizeof(params[0]); ++i) {
    heartbeat_counter = UINT32_MAX; heartbeat_tick_counter = UINT32_MAX;
    heartbeat_lost = true; heartbeat_disabled = true; controls_allowed = true;
    control(0xf3U, params[i]);
    CHECK(heartbeat_counter == 0U && heartbeat_tick_counter == 0U);
    CHECK(!heartbeat_lost && !heartbeat_disabled);
    CHECK(heartbeat_engaged == (params[i] == 1U));
    CHECK(controls_allowed && safety_calls == 0U && fan_calls == 0U && ir_calls == 0U);
  }
  puts("TRACE heartbeat 0xf3 resets counters/lost/disabled; engaged iff param1==1; does not itself set SILENT or clear controls_allowed");
}

static void boundary(bool started) {
  init(started);
  const uint32_t limit = started ? 5U : 2U;
  heartbeat_counter = limit - 2U;
  second(); CHECK(heartbeat_counter == limit - 1U); CHECK(safety_calls == 0U);
  second(); CHECK(heartbeat_counter == limit); CHECK(safety_calls == 1U);
  CHECK(last_mode == SAFETY_SILENT && last_param == 0U && can_silent);
  CHECK(heartbeat_lost && !heartbeat_engaged && power_save_enabled);
  CHECK(can_init_calls == 1U && safety_tick_calls == 2U);
  second(); CHECK(heartbeat_counter == limit + 1U); CHECK(safety_calls == 1U);
  printf("TRACE ignition=%s counter=%u/%u/%u SILENT_calls=0/1/1\n", started ? "on" : "off", limit - 1U, limit, limit + 1U);
}

static void heartbeat_saturates_and_repeated_timeout(void) {
  init(true); heartbeat_counter = UINT32_MAX;
  second(); CHECK(heartbeat_counter == UINT32_MAX && safety_calls == 1U);
  second(); CHECK(heartbeat_counter == UINT32_MAX && safety_calls == 1U);
  CHECK(heartbeat_lost && power_calls == 1U && ir_calls == 2U && fan_calls == 2U);
}

static void engaged_false_third_mismatch(void) {
  init(true); controls_allowed = true;
  for (unsigned i = 1; i <= 3; ++i) {
    control(0xf3U, 0U);
    CHECK(controls_allowed);  // Receiving engaged=false is not an immediate exit.
    second();
    CHECK(heartbeat_engaged_mismatches == i);
    CHECK(controls_allowed == (i < 3U));
    CHECK(safety_calls == 0U && current_safety_mode == SAFETY_TOYOTA);
  }
  puts("TRACE heartbeat engaged=false mismatch_count=1/2/3 controls_allowed=true/true/false; safety mode unchanged");
}

static void engaged_mismatch_reset(void) {
  init(true); controls_allowed = true;
  for (unsigned i = 0; i < 2; ++i) { control(0xf3U, 0U); second(); }
  CHECK(heartbeat_engaged_mismatches == 2U && controls_allowed);
  control(0xf3U, 1U); second(); CHECK(heartbeat_engaged_mismatches == 0U && controls_allowed);
  control(0xf3U, 0U); second(); CHECK(heartbeat_engaged_mismatches == 1U && controls_allowed);
  controls_allowed = false; second(); CHECK(heartbeat_engaged_mismatches == 0U);
}

static void car_mode_cannot_disable_heartbeat(void) {
  init(true); control(0xf8U, 0U); CHECK(!heartbeat_disabled);
  heartbeat_disabled = true; heartbeat_counter = 4U;
  second(); CHECK(!heartbeat_disabled && current_safety_mode == SAFETY_SILENT && heartbeat_lost);
}

static void nooutput_disabled_and_lost_flag(void) {
  init(false); current_safety_mode = SAFETY_NOOUTPUT;
  control(0xf8U, 0U); CHECK(heartbeat_disabled);
  heartbeat_counter = 2U; second();
  CHECK(current_safety_mode == SAFETY_NOOUTPUT && !heartbeat_lost && safety_calls == 0U);
  control(0xf3U, 0U); CHECK(!heartbeat_disabled && heartbeat_counter == 0U);
  second(); CHECK(safety_calls == 0U);
  second(); CHECK(safety_calls == 1U && current_safety_mode == SAFETY_SILENT && !heartbeat_lost);
  puts("TRACE noOutput expiry still enters SILENT, but heartbeat_lost only records a lost car-safety session");
}

static void timeout_peripheral_intents(void) {
  init(true); heartbeat_counter = 4U; som_gpio = true;
  second(); CHECK(ir_calls == 1U && last_ir == 0U && fan_calls == 1U && last_fan == 30U);
  som_gpio = false;
  second(); CHECK(ir_calls == 2U && last_ir == 0U && fan_calls == 2U && last_fan == 0U);
  CHECK(power_save_enabled);
  puts("TRACE timeout upstream HAL intent IR=0; fan=30 iff SOM GPIO high else 0; physical cooling untested");
}

static void watchdog_boundary(bool wrap) {
  init(true);
  const uint32_t base = wrap ? UINT32_MAX - 100U : 100U;
  const uint32_t elapsed[] = {374999U, 375000U, 375001U};
  for (unsigned i = 0; i < 3U; ++i) {
    faults = 0U; fault_status = FAULT_STATUS_NONE;
    now_us = base; initialize_heartbeat_watchdog();
    now_us = base + elapsed[i]; simple_watchdog_kick();
    CHECK(wd_state.last_ts == now_us);
    CHECK((faults & FAULT_HEARTBEAT_LOOP_WATCHDOG) == (i == 2U ? FAULT_HEARTBEAT_LOOP_WATCHDOG : 0U));
    CHECK(fault_status == (i == 2U ? FAULT_STATUS_TEMPORARY : FAULT_STATUS_NONE));
    CHECK(safety_calls == 0U && current_safety_mode == SAFETY_TOYOTA);
    CHECK(heartbeat_engaged && !heartbeat_lost && !power_save_enabled);
    CHECK(fan_calls == 0U && ir_calls == 0U);
  }
  now_us += 125000U; simple_watchdog_kick();
  CHECK((faults & FAULT_HEARTBEAT_LOOP_WATCHDOG) != 0U);  // Fault persists; timely kicks do not recover it.
  printf("TRACE software_watchdog%s elapsed_us=374999/375000/375001 fault=false/false/true SILENT_calls=0\n", wrap ? "_uint32_wrap" : "");
}

static void watchdog_stall_only_observed_on_resume(void) {
  init(true); now_us = 1000000U;
  CHECK(faults == 0U && safety_calls == 0U);  // No interrupt/kick executed while stalled.
  simple_watchdog_kick();
  CHECK((faults & FAULT_HEARTBEAT_LOOP_WATCHDOG) != 0U && safety_calls == 0U);
  CHECK(heartbeat_counter == 0U && !heartbeat_lost);
  puts("TRACE halted tick loop produces no autonomous software-watchdog action; subsequent kick records late-loop fault only");
}

static void watchdog_fault_does_not_prevent_later_heartbeat_timeout(void) {
  init(true); now_us = 375001U; simple_watchdog_kick();
  CHECK(faults == FAULT_HEARTBEAT_LOOP_WATCHDOG && safety_calls == 0U);
  heartbeat_counter = 4U; second();
  CHECK(faults == FAULT_HEARTBEAT_LOOP_WATCHDOG && safety_calls == 1U);
  CHECK(current_safety_mode == SAFETY_SILENT && heartbeat_lost);
  puts("TRACE late kick first records watchdog fault without SILENT; independent later heartbeat counter expiry requests SILENT");
}

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  const char *name = argv[1];
  if (!strcmp(name, "heartbeat_write_exact_semantics")) heartbeat_write_exact_semantics();
  else if (!strcmp(name, "ignition_on_counter_boundary")) boundary(true);
  else if (!strcmp(name, "ignition_off_counter_boundary")) boundary(false);
  else if (!strcmp(name, "heartbeat_saturates_and_repeated_timeout")) heartbeat_saturates_and_repeated_timeout();
  else if (!strcmp(name, "engaged_false_third_mismatch")) engaged_false_third_mismatch();
  else if (!strcmp(name, "engaged_mismatch_reset")) engaged_mismatch_reset();
  else if (!strcmp(name, "car_mode_cannot_disable_heartbeat")) car_mode_cannot_disable_heartbeat();
  else if (!strcmp(name, "nooutput_disabled_and_lost_flag")) nooutput_disabled_and_lost_flag();
  else if (!strcmp(name, "timeout_peripheral_intents")) timeout_peripheral_intents();
  else if (!strcmp(name, "watchdog_strict_threshold")) watchdog_boundary(false);
  else if (!strcmp(name, "watchdog_timer_wrap")) watchdog_boundary(true);
  else if (!strcmp(name, "watchdog_stall_only_observed_on_resume")) watchdog_stall_only_observed_on_resume();
  else if (!strcmp(name, "watchdog_fault_does_not_prevent_later_heartbeat_timeout")) watchdog_fault_does_not_prevent_later_heartbeat_timeout();
  else return 3;
  printf("{\"case\":\"%s\",\"assertions\":%u,\"failed\":0,\"skipped\":0}\n", name, assertions);
  return 0;
}
