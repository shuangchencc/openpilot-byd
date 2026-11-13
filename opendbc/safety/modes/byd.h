#pragma once

#include "opendbc/safety/declarations.h"

// Set via the safety param (BYD_PARAM_SONG_PLUS) for the BYD Song Plus DM-i 2023.
// Selects that car's wheel-speed scale and field, its brake, gas and cruise-engaged
// sources. Steering geometry is deliberately NOT selected by it - see byd_tx_hook.
// Unset (default) keeps the BYD Atto 3 behaviour.
static bool byd_song_plus = false;

static uint8_t byd_get_counter(const CANPacket_t *msg) {
  // Speed (0x1F0) and the Song Plus pedal message (0x342) use the high nibble;
  // cruise status and IPB use the low nibble. Measured on the Song Plus over
  // 349,703 frames of 0x342: the high nibble counts up by one every frame.
  bool high_nibble = (msg->addr == 0x1F0U) || (msg->addr == 0x342U);
  return high_nibble ? (msg->data[6] >> 4) : (msg->data[6] & 0xFU);
}

static uint32_t byd_get_checksum(const CANPacket_t *msg) {
  return msg->data[7];
}

static uint32_t byd_compute_checksum(const CANPacket_t *msg) {
  uint8_t sum = 0;
  for (int i = 0; i < 7; i++) {
    sum += msg->data[i];
  }
  return 0xFFU - sum;
}

static void byd_rx_hook(const CANPacket_t *msg) {
  if (msg->bus == 0U) {
    // Steering angle: 0.1 deg/LSB, signed
    if (msg->addr == 0x11FU) {
      int angle_meas_new = to_signed((msg->data[1] << 8) | msg->data[0], 16);  // STEER_ANGLE_2
      update_sample(&angle_meas, angle_meas_new);
    }

    // Vehicle speed. Atto 3: 0.1 kph/LSB. Song Plus: WHEELSPEED_FL is 0.0725
    // kph/LSB (verified against vEgo, slope 1.0000), and the ~5.8% regulatory
    // speedometer bias is removed so the panda speed equals the controller's
    // vEgo (raw * 0.0725 * wheelSpeedFactor 0.9453 = raw * 0.06853425). Keeping
    // the raw car scale would make the panda stricter than the controller above
    // ~62 km/h (see lateral.h fudged_speed) and reject legal commands.
    if (msg->addr == 0x1F0U) {
      // Atto 3: WHEELSPEED_CLEAN is 16 bits. Song Plus: WHEELSPEED_FL is the low 12
      // bits only; bits 12-15 were 0 in every recorded frame, but they are not part
      // of the signal.
      uint8_t speed_msb = byd_song_plus ? (msg->data[1] & 0xFU) : msg->data[1];
      int speed = (speed_msb << 8) | msg->data[0];
      vehicle_moving = speed > 0;
      UPDATE_VEHICLE_SPEED(speed * (byd_song_plus ? 0.06853425 : 0.1) * KPH_TO_MS);
    }

    // Atto 3: gas and brake both come from DRIVE_STATE.
    if (!byd_song_plus && (msg->addr == 0x242U)) {
      gas_pressed = (msg->data[3] & 0x7FU) > 0U;      // RAW_THROTTLE
      brake_pressed = (msg->data[4] >> 5) & 0x1U;     // BRAKE_PRESSED
    }

    // Gas pressed, Song Plus: PEDAL.GAS_PEDAL > 0.03 (0.01/LSB), the same rule as
    // carstate.py. DRIVE_STATE RAW_THROTTLE is not the pedal on this car: it is also
    // non-zero while the stock ACC accelerates (half the frames of an ACC drive had
    // RAW_THROTTLE > 0 with the pedal at 0), so panda and CarState disagreed.
    if (byd_song_plus && (msg->addr == 0x342U)) {
      gas_pressed = msg->data[0] > 3U;                // PEDAL.GAS_PEDAL
    }

    // Brake pressed, Song Plus: IPB is the reliable source. DRIVE_STATE agreed
    // with carState only 97.0% here and missed real presses; IPB 0x123 matches 99.973%.
    if (byd_song_plus && (msg->addr == 0x123U)) {
      brake_pressed = (msg->data[5] >> 3) & 0x1U;     // IPB.BRAKE_PRESSED (bit 43)
    }
  }

  if (msg->bus == 2U) {
    // Cruise state
    if (msg->addr == 0x32DU) {
      bool acc_on;
      if (byd_song_plus) {
        // ACC_ENGAGED_STATE, bits 10-11: 0 = not engaged, 1 and 2 = engaged, the
        // same rule as carstate.py. The Atto 3 field below (bits 19-21) is constant
        // 0 on this car - 0 in all 21,621 frames of an ACC drive while bits 10-11
        // read 2 in 13,693 of them - so with it the panda never saw cruise engage
        // and never allowed controls.
        acc_on = ((msg->data[1] >> 2) & 0x3U) != 0U;
      } else {
        // ACC_STATE: 0=OFF, 2=ACC_ON, 3=ACC_ACTIVE, 5=FORCE_ACCEL, 7=ERROR
        uint8_t acc_state = (msg->data[2] >> 3) & 0x7U;
        acc_on = (acc_state == 3U) || (acc_state == 5U);
      }
      pcm_cruise_check(acc_on);
    }
  }
}

static bool byd_tx_hook(const CANPacket_t *msg) {
  const AngleSteeringLimits BYD_STEERING_LIMITS = {
    .max_angle = 3900,  // 390 deg
    .angle_deg_to_can = 10,
    .frequency = 50U,
  };

  // Vehicle geometry for the angle check: the Atto 3 numbers, for every BYD.
  // The allowed angle scales with steer_ratio, so this is the tighter side, and
  // openpilot's controller limits with the same geometry (SAFETY_VM_PLATFORM in
  // values.py), which is what keeps the two in step.
  //
  // The Song PLUS is a different car, but there is no confirmed steer_ratio to put
  // here. paramsd's in-model estimate on it converged to 20.6, while a model-free
  // kinematic fit against raw GPS yaw brackets the geometric ratio at 13.2-17.6
  // on strong low-speed turns. Those are not the same quantity, so the second does
  // not confirm the first; the cost of staying on Atto 3 geometry is capability,
  // not safety, which is the right way to be wrong about an unresolved number.
  const AngleSteeringParams BYD_STEERING_PARAMS = {
    .slip_factor = -0.0006166479109059387,  // calc_slip_factor(VM), unchanged literal
    .steer_ratio = 14.8,
    .wheelbase = 2.72,
  };

  bool tx = true;

  // Steering control: 0.1 deg/LSB, signed
  if (msg->addr == 0x1E2U) {
    int desired_angle = to_signed((msg->data[4] << 8) | msg->data[3], 16);  // STEER_ANGLE
    bool steer_req = ((msg->data[2] >> 5) & 0x1U) != 0U;                    // STEER_REQ

    if (steer_angle_cmd_checks_vm(desired_angle, steer_req, BYD_STEERING_LIMITS, BYD_STEERING_PARAMS)) {
      tx = false;
    }
  }

  // Only allow cancel (ACC_ON_BTN) while stock cruise is engaged, or button release.
  if (msg->addr == 0x3B0U) {
    bool other_buttons = ((msg->data[0] & 0x58U) != 0U) ||  // SET, RES, LKAS_ON
                         ((msg->data[1] & 0x80U) != 0U) ||  // DEC_DISTANCE
                         ((msg->data[2] & 0x1U) != 0U);     // INC_DISTANCE
    bool cancel = (msg->data[2] & 0x8U) != 0U;             // ACC_ON_BTN
    tx = !other_buttons && (!cancel || cruise_engaged_prev);
  }

  return tx;
}

static safety_config byd_init(uint16_t param) {
  const uint16_t BYD_PARAM_SONG_PLUS = 1U;  // set for the BYD Song Plus DM-i platform
  byd_song_plus = GET_FLAG(param, BYD_PARAM_SONG_PLUS);

  static const CanMsg BYD_TX_MSGS[] = {
    {0x1E2, 0, 8, .check_relay = true},   // STEERING_MODULE_ADAS (lateral steering command)
    {0x316, 0, 8, .check_relay = true},   // LKAS_HUD_ADAS (dash HUD)
    {0x3B0, 0, 8, .check_relay = false},  // PCM_BUTTONS (cruise cancel button spoof)
  };

  // Atto 3: brake comes from DRIVE_STATE (0x242); its DBC has no IPB (0x123).
  static RxCheck byd_atto3_rx_checks[] = {
    {.msg = {{0x11F, 0, 5, 100U, .ignore_checksum = true, .ignore_counter = true, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // STEER_MODULE_2 (steering angle)
    {.msg = {{0x1F0, 0, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // WHEELSPEED_CLEAN (vehicle speed)
    {.msg = {{0x242, 0, 8,  50U, .ignore_checksum = true, .ignore_counter = true, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // DRIVE_STATE (gas and brake pressed)
    {.msg = {{0x32D, 2, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // ACC_HUD_ADAS (cruise state)
  };

  // Song Plus: brake comes from IPB (0x123, 100 Hz) and gas from PEDAL (0x342,
  // 50 Hz), both with valid checksum and counter on every recorded frame.
  // DRIVE_STATE (0x242) is not read on this platform.
  static RxCheck byd_song_plus_rx_checks[] = {
    {.msg = {{0x11F, 0, 5, 100U, .ignore_checksum = true, .ignore_counter = true, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // STEER_MODULE_2 (steering angle)
    {.msg = {{0x1F0, 0, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // WHEELSPEED_CLEAN (vehicle speed)
    {.msg = {{0x123, 0, 8, 100U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // IPB (brake pressed)
    {.msg = {{0x342, 0, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // PEDAL (gas pressed)
    {.msg = {{0x32D, 2, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // ACC_HUD_ADAS (cruise state)
  };

  safety_config ret;
  if (byd_song_plus) {
    ret = BUILD_SAFETY_CFG(byd_song_plus_rx_checks, BYD_TX_MSGS);
  } else {
    ret = BUILD_SAFETY_CFG(byd_atto3_rx_checks, BYD_TX_MSGS);
  }
  return ret;
}

const safety_hooks byd_hooks = {
  .init = byd_init,
  .rx = byd_rx_hook,
  .tx = byd_tx_hook,
  .get_counter = byd_get_counter,
  .get_checksum = byd_get_checksum,
  .compute_checksum = byd_compute_checksum,
};
