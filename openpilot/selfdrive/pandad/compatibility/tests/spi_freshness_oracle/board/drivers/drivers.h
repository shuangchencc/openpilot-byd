#pragma once
// Host-only dependencies of the unmodified locked board/drivers/spi.h oracle.
// The runner supplies its exact, hash-verified Panda header with P10_PANDA_SPI_HEADER.
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define SPI_BUF_SIZE 4096U
#define MIN(a, b) ((a) < (b) ? (a) : (b))
typedef struct { uint8_t request; uint16_t value, index, length; } __attribute__((packed)) ControlPacket_t;
extern uint8_t oracle_uid[12];
extern uint8_t hw_type;
#define UID_BASE oracle_uid
#define USB_PID 0xddccU
// Keep the host oracle dependency symbols separate from zlib crc32, whose
// signature and ABI differ. The real driver's cache/CS logic is unchanged.
#define crc32 p10_firmware_crc32
#define crc32_init p10_firmware_crc32_init
uint32_t crc32(const uint8_t *, unsigned int);
void crc32_init(void);
int comms_control_handler(ControlPacket_t *, uint8_t *);
uint16_t comms_can_read(uint8_t *, uint16_t);
void comms_endpoint2_write(const uint8_t *, uint16_t);
void comms_can_write(const uint8_t *, uint16_t);
