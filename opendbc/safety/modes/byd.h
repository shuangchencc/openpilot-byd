#pragma once

#include "opendbc/safety/declarations.h"

// Set via the safety param (BYD_PARAM_SONG_PLUS) for the BYD Song Plus DM-i 2023.
// Selects that car's wheel-speed scale and field, its brake, gas and cruise-engaged
// sources, and its steering geometry - see byd_tx_hook.
// Unset (default) keeps the BYD Atto 3 behaviour.
static bool byd_song_plus = false;

// Song Plus longitudinal: openpilot may lower the stock ACC's acceleration request, and
// nothing else. Off unless the safety param carries a level (BYD_PARAM_LONG_LEVEL).
//
// The ADAS controller keeps sending its three frames on bus 2: ACC_HUD_ADAS (0x32D),
// ACC_CMD (0x32E, the request) and 0x32F. The panda forwards all of them to the car as
// it always has, and openpilot cannot transmit 0x32D or 0x32F at all. For ACC_CMD
// openpilot may send ONE thing: a copy of the stock frame that has just arrived, with the
// next counter and with a request that is lower than the stock one. When such a frame
// has been sent, the next stock ACC_CMD is not forwarded (the car already has that
// frame, numbered as the stock one would have been); if openpilot sends nothing, every
// stock frame goes through. So openpilot dying, stalling or misbehaving leaves the car
// with the stock ACC from the very next frame, with no timeout to wait for.
//
// Why "the next frame" and not "this frame": the firmware decides whether to forward a
// frame before it parses it, and the forward hook is told only the bus and the address.
// A frame can therefore only be withheld on the strength of what arrived before it.
// What this costs: what the withheld frame holds reaches the car only through openpilot's
// next frame, which is a copy of it; once openpilot stops, the last frame it stood in
// for is lost to the car. In the recordings, each of the 9 times a frame that may be
// copied was followed by a change in the flag bits, the new value lasted three frames
// or more - and such a frame may not be copied, so the change reaches the car one frame
// (20 ms) late. The band and jerk-limit values do flicker by one count for a single
// frame (33 times in 48,014 frames); such a flicker is late by a frame, or skipped when
// openpilot stops right there. And "sent" means handed to the transmit queue: if that
// frame then fails to go out, the car misses one frame.
//
// The three hooks share the state below without locking. That is sound only because
// they cannot interrupt each other: the TX hook runs in the USB / SPI interrupt, the
// forward and RX hooks in the CAN interrupt, and the firmware leaves every interrupt at
// the same priority (no NVIC_SetPriority anywhere in panda/board). Check this again
// when the panda firmware is updated.
//
// The request is byte 0, (raw - 100) * 0.05 m/s^2: on this car the stock controller's own
// requests against wheel-speed acceleration gave a slope of 0.89 at r 0.96.
// Bytes 1-5 hold the comfort band, jerk limits and flag bits; they must be the stock
// frame's own, so whatever the stock controller signals in them still reaches the car.
// While the stock ACC is controlling, bit 39 is 0 and byte 5 is 0xD8 in 99.8% of frames
// (48,014 of 48,101 recorded); the rest are the frames around a change of state. Any
// other pattern, any state other than "controlling", or one of the warning bits of
// ACC_HUD_ADAS (16, 17, 23) within the last two seconds, and nothing may be replaced.
// Details: migration-audit/longitudinal-l0-20261114 and longitudinal-l1-20261114.
//
// Raising (safety param bit 3, BYD_PARAM_LONG_RAISE, which means something only together with a
// level): openpilot may also ask for a little MORE than the stock ACC, in the same kind of frame,
// but only where the stock ACC's own view says the road ahead is free (byd_acc_cmd_raise_ok):
// the stock ACC is not braking (its request is 0 or more); its radar has shown nothing ahead for
// two seconds, neither "lead" (bit 9 of ACC_HUD_ADAS, which it sets for standing objects as
// well) nor a distance bar (bits 13-15); and the car is moving, at 3 m/s or more, and below the
// set speed. Then the request may be up to 0.5 m/s^2 above the stock one and +1.2 m/s^2 at most,
// and no more than 0.05 m/s^2 above the request the car got in the slot just past: 2.5 m/s^3,
// the stock ACC's own fastest change. Once a request may be higher, the stock ACC is no longer
// the upper bound, and these conditions are what is left of it. openpilot adds one the panda
// cannot check, that its own camera sees nothing ahead either. Nothing is raised while the stock
// ACC brakes, at standstill or when starting, at or above the set speed, or within two seconds
// of the stock radar showing something. Details: migration-audit/longitudinal-l6-20261114.
#define BYD_ACC_CMD_MAX_AGE_US 25000U      // a replacement must come from a stock frame no older than this (they arrive every 20 ms)
#define BYD_ACC_SUPPLIED_MAX_AGE_US 100000U  // a replacement withholds the stock frame that follows it, not one that arrives this much later
#define BYD_ACC_WARNING_HOLD_US 2000000U   // nothing is replaced for this long after a warning bit was last seen
#define BYD_ACC_STATE_CONTROLLING 3U
#define BYD_ACC_LEAD_HOLD_US 2000000U      // nothing is raised for this long after the stock radar last showed something ahead
#define BYD_ACC_REQUEST_ZERO 100U          // the raw request for 0 m/s^2
#define BYD_RAISE_MAX_ABOVE_STOCK 10U      // a raised request is no more than this many counts above the stock one (+0.5 m/s^2),
#define BYD_RAISE_MAX_REQUEST 124U         // nor above this (+1.2 m/s^2, the most the stock ACC has been seen to ask for),
#define BYD_RAISE_MAX_STEP 1U              // nor more than this many counts above the request the car got in the slot just past
#define BYD_RAISE_MIN_SPEED 3000           // mm/s (vehicle_speed is m/s * VEHICLE_SPEED_FACTOR): nothing is raised below 3 m/s

static uint8_t byd_long_level = 0U;            // 0 = openpilot cannot send ACC_CMD; 1-3 select the lowest request it may make
static uint8_t byd_acc_cmd[6] = {0};           // bytes 0-5 of the latest valid stock ACC_CMD; all zero is no frame that may be copied
static uint8_t byd_acc_cmd_counter = 0U;       // and its counter
static uint32_t byd_acc_cmd_ts = 0U;           // and when it arrived
static uint32_t byd_acc_cmd_arrivals = 0U;     // stock ACC_CMD frames that have reached the forward decision, valid or not
static uint32_t byd_acc_cmd_saved_arrival = 0U;  // the count when the frame in byd_acc_cmd arrived
static bool byd_acc_cmd_supplied = false;      // openpilot has sent the frame that stands in for the next stock ACC_CMD
static uint32_t byd_acc_cmd_supplied_ts = 0U;  // and when
static uint8_t byd_acc_state_code = 0U;        // the stock ACC's own state, from ACC_HUD_ADAS
static bool byd_acc_warning = false;           // a warning bit was seen less than BYD_ACC_WARNING_HOLD_US ago
static uint32_t byd_acc_warning_ts = 0U;
static bool byd_long_raise = false;            // openpilot may raise the request as well (BYD_PARAM_LONG_RAISE)
static bool byd_acc_lead = true;               // the stock radar showed something ahead less than BYD_ACC_LEAD_HOLD_US ago
static uint32_t byd_acc_lead_ts = 0U;
static uint8_t byd_acc_set_speed = 0U;         // the stock ACC's set speed, 0.5 km/h per count, on the speedometer's scale
static bool byd_acc_cmd_forwarded = false;     // the forward decision let the newest stock ACC_CMD through
// The request the car got in one slot, the slot of stock frame number byd_acc_ref_arrival (counted
// like byd_acc_cmd_arrivals): openpilot's, when the TX hook took a frame of ours for that slot, or
// the stock frame's own, when the forward decision let it through. A raised request is held against
// it, and only when it is the slot just past.
static bool byd_acc_ref_valid = false;
static uint8_t byd_acc_ref = 0U;
static uint32_t byd_acc_ref_arrival = 0U;

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
      uint32_t speed = GET_BYTES_LE(msg, 0, 2) & (byd_song_plus ? 0xFFFU : 0xFFFFU);
      vehicle_moving = speed > 0U;
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
        // The same rule as carstate.py: the ACC is on (the follow-distance setting,
        // SET_DISTANCE, bits 10-12, non-zero) and its own state (ACC_STATE_CODE, high
        // nibble of byte 5) is 3 controlling, 5 overridden with the gas pedal or 7
        // holding at standstill. The setting alone stays set while the ACC is on its
        // way out (state 4), which is when openpilot has already let go. All three
        // bits: the fourth setting is 0b100, and with bits 10-11 alone the ACC looked
        // off at it while it went on controlling (route 00000033, 2026-11-15). The
        // Atto 3 field below (bits 19-21) is constant 0 on this car - 0 in all 21,621
        // frames of an ACC drive while bits 10-11 read 2 in 13,693 of them - so with
        // it the panda never saw cruise engage and never allowed controls.
        uint8_t state_code = msg->data[5] >> 4;
        bool acc_shown = ((msg->data[1] >> 2) & 0x7U) != 0U;
        acc_on = acc_shown && ((state_code == 3U) || (state_code == 5U) || (state_code == 7U));

        // for byd_acc_cmd_replacement_ok
        uint32_t ts = microsecond_timer_get();
        byd_acc_state_code = state_code;
        if ((msg->data[2] & 0x83U) != 0U) {   // bits 16, 17 and 23
          byd_acc_warning = true;
          byd_acc_warning_ts = ts;
        } else if (byd_acc_warning && (safety_get_ts_elapsed(ts, byd_acc_warning_ts) >= BYD_ACC_WARNING_HOLD_US)) {
          byd_acc_warning = false;
        } else {
          // no warning, or one that is still being held
        }

        // for byd_acc_cmd_raise_ok: whether the stock radar shows anything ahead, "lead" (bit 9) or a
        // distance bar (bits 13-15), held the same way; and the set speed (bits 0-7)
        if ((msg->data[1] & 0xE2U) != 0U) {
          byd_acc_lead = true;
          byd_acc_lead_ts = ts;
        } else if (byd_acc_lead && (safety_get_ts_elapsed(ts, byd_acc_lead_ts) >= BYD_ACC_LEAD_HOLD_US)) {
          byd_acc_lead = false;
        } else {
          // nothing ahead, or something that is still being held
        }
        byd_acc_set_speed = msg->data[0];
      } else {
        // ACC_STATE: 0=OFF, 2=ACC_ON, 3=ACC_ACTIVE, 5=FORCE_ACCEL, 7=ERROR
        uint8_t acc_state = (msg->data[2] >> 3) & 0x7U;
        acc_on = (acc_state == 3U) || (acc_state == 5U);
      }
      pcm_cruise_check(acc_on);
    }

    // The stock request, kept to check openpilot's replacement against. Only reaches
    // here on a Song Plus with a longitudinal level: ACC_CMD is an RX-checked message
    // in that configuration alone, and the RX hook sees no others.
    if (msg->addr == 0x32EU) {
      for (int i = 0; i < 6; i++) {
        byd_acc_cmd[i] = msg->data[i];
      }
      byd_acc_cmd_counter = msg->data[6] & 0xFU;
      byd_acc_cmd_ts = microsecond_timer_get();
      byd_acc_cmd_saved_arrival = byd_acc_cmd_arrivals;

      // if it was let through, its request is what the car got in this slot. The forward decision
      // for this same frame was made just before, in the same interrupt.
      if (byd_acc_cmd_forwarded) {
        byd_acc_ref = msg->data[0];
        byd_acc_ref_arrival = byd_acc_cmd_arrivals;
        byd_acc_ref_valid = true;
      }
    }
  }
}

// May openpilot's request be above the stock one? See "Raising" at the top of the file. Asked only
// about a frame with a request above the stock one; every other check is byd_acc_cmd_replacement_ok's.
static bool byd_acc_cmd_raise_ok(uint8_t request) {
  // raising is on, and the stock ACC is not braking: above a negative stock request, a higher one
  // would take away from its braking (a0's review)
  bool ok = byd_long_raise && (byd_acc_cmd[0] >= BYD_ACC_REQUEST_ZERO);

  // its radar has shown nothing ahead for two seconds
  ok = ok && !byd_acc_lead;

  // the car is moving, and slower than the set speed. Of the panda's last few speeds the lowest is
  // held against 3 m/s and the highest against the set speed. The set speed is on the speedometer's
  // scale, which reads 1 / 0.9453 of the road speed the panda keeps (see the wheel speed in
  // byd_rx_hook): speed [mm/s] * 3.6 / 1000 / 0.9453 < count * 0.5 [km/h], or in whole numbers
  // speed * 72 < count * 9453.
  ok = ok && (vehicle_speed.min > BYD_RAISE_MIN_SPEED);
  ok = ok && ((vehicle_speed.max * 72) < ((int)byd_acc_set_speed * 9453));

  // no more than 0.5 m/s^2 above the stock request, nor more than +1.2 m/s^2 ...
  ok = ok && (request <= (byd_acc_cmd[0] + BYD_RAISE_MAX_ABOVE_STOCK)) && (request <= BYD_RAISE_MAX_REQUEST);
  // ... nor more than 0.05 m/s^2 above what the car got in the slot just past (a0's review)
  ok = ok && byd_acc_ref_valid && (byd_acc_ref_arrival == byd_acc_cmd_arrivals);
  ok = ok && (request <= (byd_acc_ref + BYD_RAISE_MAX_STEP));

  return ok;
}

// May this ACC_CMD from openpilot stand in for the next stock one? See the top of the file.
static bool byd_acc_cmd_replacement_ok(const CANPacket_t *msg) {
  // The lowest raw request per level: -1.0, -2.0 and -3.5 m/s^2. Without a level
  // ACC_CMD is not a TX message and this is never asked; no request could pass it.
  static const uint8_t BYD_LONG_MIN_REQUEST[4] = {255U, 80U, 60U, 30U};

  uint32_t ts = microsecond_timer_get();

  // A level and no relay malfunction: wherever this is reached both already hold (without
  // a level ACC_CMD is no TX message, and during a relay malfunction the common code sends
  // and forwards nothing). They are kept here on purpose, so that this function does not
  // depend on the TX list or on the common code staying as it is.
  // And controls allowed with the gas pedal released.
  bool ok = (byd_long_level != 0U) && !relay_malfunction && get_longitudinal_allowed();

  // the stock frame it must be a copy of is the one that has just arrived: the newest
  // ACC_CMD to reach the forward decision, not only the newest that passed the RX checks.
  // After one that did not (a frame of another length never reaches them; one that fails
  // them also ends the engagement) a copy of the frame before could carry a counter the car
  // has had already (a0's review). And nothing has been sent in place of the frame after it yet.
  ok = ok && (byd_acc_cmd_saved_arrival == byd_acc_cmd_arrivals);
  ok = ok && !byd_acc_cmd_supplied && (safety_get_ts_elapsed(ts, byd_acc_cmd_ts) <= BYD_ACC_CMD_MAX_AGE_US);

  // the stock ACC is controlling, shows no warning and has not shown one lately ...
  ok = ok && (byd_acc_state_code == BYD_ACC_STATE_CONTROLLING) && !byd_acc_warning;
  // ... and its frame looks the way it does while controlling
  ok = ok && ((byd_acc_cmd[4] & 0x80U) == 0U) && (byd_acc_cmd[5] == 0xD8U);

  // the next counter, a valid checksum, and bytes 1-5 exactly as the stock frame has them
  ok = ok && ((msg->data[6] & 0xFU) == ((byd_acc_cmd_counter + 1U) & 0xFU)) && ((msg->data[6] >> 4) == 0xFU);
  ok = ok && (byd_compute_checksum(msg) == byd_get_checksum(msg));
  for (int i = 1; i < 6; i++) {
    ok = ok && (msg->data[i] == byd_acc_cmd[i]);
  }

  // a request that is lower than the stock one, and not below this level's floor; or one that is
  // higher, where raising allows it. Never the stock request itself: that frame goes by itself.
  bool lower = (msg->data[0] < byd_acc_cmd[0]) && (msg->data[0] >= BYD_LONG_MIN_REQUEST[byd_long_level]);
  bool higher = (msg->data[0] > byd_acc_cmd[0]) && byd_acc_cmd_raise_ok(msg->data[0]);
  ok = ok && (lower || higher);

  return ok;
}

static bool byd_tx_hook(const CANPacket_t *msg) {
  const AngleSteeringLimits BYD_STEERING_LIMITS = {
    .max_angle = 3900,  // 390 deg
    .angle_deg_to_can = 10,
    .frequency = 50U,
  };

  // Vehicle geometry for the angle check, the same as openpilot's controller limits with (carcontroller.py,
  // get_safety_CP), which is what keeps the two in step. The Atto 3: its own CarSpecs. The Song PLUS: its geometry
  // as measured with the device's gyroscope on 2026-11-15 - an apparent steering ratio of 17.84 at 10-30 km/h,
  // rising with understeer to 27.54 at 70-90 km/h - taken a little short of the car at every measured speed
  // (ratio 17.2, slip factor -0.0008), so that the angle allowed never stands for more lateral acceleration than
  // the limit's. Until then it used the Atto 3's, which allowed it 19-27% less steering. Measurements and margins:
  // values.py, SONG_PLUS_SAFETY_STEER_RATIO.
  const AngleSteeringParams BYD_STEERING_PARAMS = {
    .slip_factor = -0.0006166479109059387,  // calc_slip_factor(VM), unchanged literal
    .steer_ratio = 14.8,
    .wheelbase = 2.72,
  };
  const AngleSteeringParams BYD_SONG_PLUS_STEERING_PARAMS = {
    .slip_factor = -0.0008000000648791277,  // calc_slip_factor(VehicleModel(get_safety_CP(BYD_SONG_PLUS_DMI_23)))
    .steer_ratio = 17.2,
    .wheelbase = 2.765,
  };

  bool tx = true;

  // Steering control: 0.1 deg/LSB, signed
  if (msg->addr == 0x1E2U) {
    int desired_angle = to_signed((msg->data[4] << 8) | msg->data[3], 16);  // STEER_ANGLE
    bool steer_req = ((msg->data[2] >> 5) & 0x1U) != 0U;                    // STEER_REQ

    const AngleSteeringParams params = byd_song_plus ? BYD_SONG_PLUS_STEERING_PARAMS : BYD_STEERING_PARAMS;
    if (steer_angle_cmd_checks_vm(desired_angle, steer_req, BYD_STEERING_LIMITS, params)) {
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

  // The stock ACC request with a lower value (or a higher one, see "Raising"), one per stock frame
  if (msg->addr == 0x32EU) {
    tx = byd_acc_cmd_replacement_ok(msg);
    if (tx) {
      byd_acc_cmd_supplied = true;
      byd_acc_cmd_supplied_ts = microsecond_timer_get();
      // the car gets this request in the slot of the next stock frame
      byd_acc_ref = msg->data[0];
      byd_acc_ref_arrival = byd_acc_cmd_arrivals + 1U;
      byd_acc_ref_valid = true;
    }
  }

  return tx;
}

static bool byd_fwd_hook(int bus_num, int addr) {
  bool block = false;

  // A stock ACC_CMD whose place openpilot's frame has already taken. The mark is cleared
  // here and not in the RX hook, so that it is cleared by every frame that arrives,
  // including one the RX checks reject: it must never outlive the one frame it is for.
  // Nor does it wait for ever: if the stock frames stop and start again later, the
  // first one back is not the frame that was replaced.
  if ((bus_num == 2) && (addr == 0x32E)) {
    block = byd_acc_cmd_supplied && (safety_get_ts_elapsed(microsecond_timer_get(), byd_acc_cmd_supplied_ts) <= BYD_ACC_SUPPLIED_MAX_AGE_US);
    byd_acc_cmd_supplied = false;
    byd_acc_cmd_arrivals += 1U;
    byd_acc_cmd_forwarded = !block;
  }

  return block;
}

static safety_config byd_init(uint16_t param) {
  const uint16_t BYD_PARAM_SONG_PLUS = 1U;  // set for the BYD Song Plus DM-i platform
  // Bits 1-2, the longitudinal level. It has a meaning on the Song Plus only (the
  // configurations are chosen below): 0 = openpilot does not touch the stock ACC's
  // request; 1, 2, 3 = it may lower it, down to -1.0, -2.0 or -3.5 m/s^2.
  const uint16_t BYD_PARAM_LONG_LEVEL_SHIFT = 1U;
  const uint16_t BYD_PARAM_LONG_LEVEL_MASK = 3U;
  // Bit 3: it may raise the request as well, see "Raising" at the top. Nothing without a level.
  const uint16_t BYD_PARAM_LONG_RAISE = 8U;
  byd_song_plus = GET_FLAG(param, BYD_PARAM_SONG_PLUS);
  byd_long_level = (uint8_t)((param >> BYD_PARAM_LONG_LEVEL_SHIFT) & BYD_PARAM_LONG_LEVEL_MASK);
  byd_long_raise = GET_FLAG(param, BYD_PARAM_LONG_RAISE);

  for (int i = 0; i < 6; i++) {
    byd_acc_cmd[i] = 0U;
  }
  byd_acc_cmd_counter = 0U;
  byd_acc_cmd_ts = 0U;
  byd_acc_cmd_arrivals = 0U;
  byd_acc_cmd_saved_arrival = 0U;
  byd_acc_cmd_supplied = false;
  byd_acc_cmd_supplied_ts = 0U;
  byd_acc_state_code = 0U;
  byd_acc_warning = false;
  byd_acc_warning_ts = 0U;
  // nothing is raised before the stock radar has been seen to show nothing ahead for two seconds,
  // nor before ACC_HUD_ADAS has given a set speed, nor before the car has got a request in some slot
  byd_acc_lead = true;
  byd_acc_lead_ts = microsecond_timer_get();
  byd_acc_set_speed = 0U;
  byd_acc_cmd_forwarded = false;
  byd_acc_ref_valid = false;
  byd_acc_ref = 0U;
  byd_acc_ref_arrival = 0U;

  static const CanMsg BYD_TX_MSGS[] = {
    {0x1E2, 0, 8, .check_relay = true},   // STEERING_MODULE_ADAS (lateral steering command)
    {0x316, 0, 8, .check_relay = true},   // LKAS_HUD_ADAS (dash HUD)
    {0x3B0, 0, 8, .check_relay = false},  // PCM_BUTTONS (cruise cancel button spoof)
  };

  // With a longitudinal level: ACC_CMD as well. Seeing it on bus 0 from anyone else is a
  // relay malfunction like the others, but it is not blocked statically: the stock frame
  // is forwarded unless byd_fwd_hook says its place has been taken.
  static const CanMsg BYD_LONG_TX_MSGS[] = {
    {0x1E2, 0, 8, .check_relay = true},   // STEERING_MODULE_ADAS (lateral steering command)
    {0x316, 0, 8, .check_relay = true},   // LKAS_HUD_ADAS (dash HUD)
    {0x3B0, 0, 8, .check_relay = false},  // PCM_BUTTONS (cruise cancel button spoof)
    {0x32E, 0, 8, .check_relay = true, .disable_static_blocking = true},  // ACC_CMD (stock request, lowered or raised)
  };

  // Each set of RX checks is declared in the branch that uses it (MISRA: the smallest scope).
  safety_config ret;
  if (byd_song_plus && (byd_long_level != 0U)) {
    // The Song Plus's below, and the stock ACC_CMD (50 Hz, next to ACC_HUD_ADAS): openpilot's
    // replacement is checked against it, so it has to be alive and valid as well.
    static RxCheck byd_song_plus_long_rx_checks[] = {
      {.msg = {{0x11F, 0, 5, 100U, .ignore_checksum = true, .ignore_counter = true, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // STEER_MODULE_2 (steering angle)
      {.msg = {{0x1F0, 0, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // WHEELSPEED_CLEAN (vehicle speed)
      {.msg = {{0x123, 0, 8, 100U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // IPB (brake pressed)
      {.msg = {{0x342, 0, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // PEDAL (gas pressed)
      {.msg = {{0x32D, 2, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // ACC_HUD_ADAS (cruise state)
      {.msg = {{0x32E, 2, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // ACC_CMD (the stock request)
    };
    ret = BUILD_SAFETY_CFG(byd_song_plus_long_rx_checks, BYD_LONG_TX_MSGS);
  } else if (byd_song_plus) {
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
    ret = BUILD_SAFETY_CFG(byd_song_plus_rx_checks, BYD_TX_MSGS);
  } else {
    // Atto 3: brake comes from DRIVE_STATE (0x242); its DBC has no IPB (0x123).
    static RxCheck byd_atto3_rx_checks[] = {
      {.msg = {{0x11F, 0, 5, 100U, .ignore_checksum = true, .ignore_counter = true, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // STEER_MODULE_2 (steering angle)
      {.msg = {{0x1F0, 0, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // WHEELSPEED_CLEAN (vehicle speed)
      {.msg = {{0x242, 0, 8,  50U, .ignore_checksum = true, .ignore_counter = true, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // DRIVE_STATE (gas and brake pressed)
      {.msg = {{0x32D, 2, 8,  50U, .max_counter = 15U, .ignore_quality_flag = true}, { 0 }, { 0 }}},  // ACC_HUD_ADAS (cruise state)
    };
    ret = BUILD_SAFETY_CFG(byd_atto3_rx_checks, BYD_TX_MSGS);
  }
  return ret;
}

const safety_hooks byd_hooks = {
  .init = byd_init,
  .rx = byd_rx_hook,
  .tx = byd_tx_hook,
  .fwd = byd_fwd_hook,
  .get_counter = byd_get_counter,
  .get_checksum = byd_get_checksum,
  .compute_checksum = byd_compute_checksum,
};
