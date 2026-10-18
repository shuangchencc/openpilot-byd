// Compile the real locked SPI state machine; only hardware DMA and application
// endpoint dependencies are replaced. No copy of its cache/CS logic lives here.
#include "spi_freshness_oracle/board/drivers/drivers.h"
#ifndef P10_PANDA_SPI_HEADER
#error P10_PANDA_SPI_HEADER must name the locked Panda board/drivers/spi.h
#endif
#include P10_PANDA_SPI_HEADER

uint8_t oracle_uid[12] = {1,2,3,4,5,6,7,8,9,10,11,12};
uint8_t hw_type = 7;
static uint8_t queued[4096];
static uint16_t queued_size, received_size;
static unsigned int controls[256], writes, reads;
static uint8_t signature[128];
uint32_t crc32(const uint8_t *data, unsigned int size) {
  uint32_t crc = 0xffffffffU;
  for (unsigned int i = 0; i < size; ++i) {
    crc ^= data[i];
    for (unsigned int bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ ((crc & 1U) ? 0xedb88320U : 0U);
  }
  return crc ^ 0xffffffffU;
}
void crc32_init(void) {}
void llspi_init(void) {}
void llspi_queue(const uint8_t *data, uint16_t size) { memcpy(queued, data, size); queued_size = size; }
uint16_t llspi_rx_count(void) { return received_size; }
int comms_control_handler(ControlPacket_t *control, uint8_t *out) {
  ++controls[control->request];
  const uint8_t protocol[8] = {0xf5,0x12,0xbc,0xff,0x76,0xf2,0xab,0x75};
  const uint8_t caps[16] = {'F','4','C','1',1,0,16,0,0x1f,0,0,0,7,0,0,0};
  switch (control->request) {
    case 0xc3: memcpy(out, oracle_uid, 12); return 12;
    case 0xc1: out[0] = hw_type; return 1;
    case 0xdd: memcpy(out, protocol, 8); return 8;
    case 0xd3: memcpy(out, signature, 64); return 64;
    case 0xd4: memcpy(out, signature + 64, 64); return 64;
    case 0xd5: memcpy(out, caps, 16); return 16;
    case 0xd2: memset(out, 5, 48); return 48;
    case 0xc2: memset(out, 6, 64); return 64;
    case 0xb2: memset(out, 1, 2); return 2;
    default: ++writes; return 0;
  }
}
uint16_t comms_can_read(uint8_t *out, uint16_t size) { ++reads; memset(out, 42, size); return size; }
void comms_endpoint2_write(const uint8_t *data, uint16_t size) { (void)data; (void)size; ++writes; }
void comms_can_write(const uint8_t *data, uint16_t size) { (void)data; (void)size; ++writes; }
void p10_oracle_exchange(const uint8_t *tx, uint8_t *rx, unsigned int size) {
  // Model one full-duplex CS assertion and its final rising edge. A transfer
  // beyond the queued response sees underrun zeros. Partial CS reads are legal
  // here and exercise the actual response_queued state in the locked driver.
  memset(rx, 0, size);
  memcpy(rx, queued, MIN(size, queued_size));
  memcpy(spi_buf_rx, tx, size);
  received_size = (uint16_t)size;
  spi_cs_rising();
}
static void put32(uint8_t *out, uint32_t value) { for (unsigned int i = 0; i < 4; ++i) out[i] = (uint8_t)(value >> (8 * i)); }
void p10_oracle_reset(void) {
  uint8_t filler[4096], reply[4096]; memset(filler, 0xff, sizeof(filler));
  p10_oracle_exchange(filler, reply, sizeof(filler));
  p10_oracle_exchange(filler, reply, sizeof(filler));
  spi_init();
  // Expel the previous fixture's cache using a valid endpoint-info id=0 frame.
  // This does not modify the firmware state machine or its function statics.
  uint8_t request[13] = {0xf0,0,0,0,0,0,0,25,0}; put32(request + 9, crc32(request, 9));
  p10_oracle_exchange(request, reply, sizeof(request));
  p10_oracle_exchange(filler, reply, 25);
  for (unsigned int i = 0; i < 12; ++i) oracle_uid[i] = (uint8_t)(i + 1);
  for (unsigned int i = 0; i < 128; ++i) signature[i] = (uint8_t)(i + 17);
  hw_type = 7; memset(controls, 0, sizeof(controls)); writes = 0; reads = 0;
}
unsigned int p10_oracle_controls(unsigned int command) { return controls[command]; }
unsigned int p10_oracle_writes(void) { return writes; }
unsigned int p10_oracle_reads(void) { return reads; }
void p10_oracle_set_uid_byte(unsigned int index, uint8_t value) { oracle_uid[index] = value; }
void p10_oracle_set_board(uint8_t board) { hw_type = board; }
