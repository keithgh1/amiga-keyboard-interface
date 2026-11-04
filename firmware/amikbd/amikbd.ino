// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Keith Monahan
// amikbd: Amiga 500 keyboard emulator for ATmega32U4 (Pro Micro, 5 V / 16 MHz).
// The host sends line commands over USB CDC; the firmware drives the A500 keyboard header (CN13).
//
// Wiring (open-drain, 100 R series): D8 = KCLK (CN13-1), D9 = KDAT (CN13-2), D10 = /KBRESET (CN13-3)
// LED sense (10k series, 10k to GND):  A0 = power LED (CN13-7), A1 = drive LED (CN13-8)
//
// Protocol: see PROTOCOL.md. Every command gets exactly one "OK ..." or "ERR ..." line.
// Asynchronous event lines start with "# ".
//
// Written from the Amiga Hardware Reference Manual, Appendix G. No code was taken from other projects;
// these techniques follow open-source keyboard adapters that do the same job:
//   open-drain drive and power-up sync ($FD/$FE): Jartza/PS2toAmiga
//   attention pulse before each byte: jtsiomb/a500kbd, Jartza/PS2toAmiga
//   reset by /KBRESET and KCLK low together: borb/amigahid-pico, gianlucarenzi/stalker

#include <avr/pgmspace.h>
#include <util/delay.h>
#include <string.h>
#include <ctype.h>
#include <stdlib.h>

#define FW_VERSION "amikbd 0.3.1"

// ---- Pins (port B) -------------------------------------------------------------------------
#define KCLK_BIT _BV(PB4)   // D8
#define KDAT_BIT _BV(PB5)   // D9, PCINT5
#define KRST_BIT _BV(PB6)   // D10

#define LINE_LOW(b)  (DDRB |= (b))     // PORTB bit is 0, so output = pull low
#define LINE_REL(b)  (DDRB &= ~(b))    // input, Amiga pull-up takes the line high
#define LINE_IS_HIGH(b) (PINB & (b))

#define PIN_LED_POWER A0
#define PIN_LED_DRIVE A1

// ---- Amiga keycodes ------------------------------------------------------------------------
enum : uint8_t {
  AK_SPACE = 0x40, AK_BS = 0x41, AK_TAB = 0x42, AK_KPENTER = 0x43, AK_RETURN = 0x44,
  AK_ESC = 0x45, AK_DEL = 0x46,
  AK_LSHIFT = 0x60, AK_RSHIFT = 0x61, AK_CAPS = 0x62, AK_CTRL = 0x63,
  AK_LALT = 0x64, AK_RALT = 0x65, AK_LAMIGA = 0x66, AK_RAMIGA = 0x67,
  AK_UPFLAG = 0x80,
  AK_LOSTSYNC = 0xF9, AK_INITPOWER = 0xFD, AK_TERMPOWER = 0xFE,
  AK_NONE = 0xFF
};

static const uint16_t ACK_TIMEOUT_MS = 143;   // HRM handshake timeout
static const uint8_t  RESYNC_MAX_BITS = 9;    // one frame plus one, before falling back to power-up sync

// ---- State ---------------------------------------------------------------------------------
enum SendResult : uint8_t { SEND_OK, SEND_NOSYNC, SEND_FAIL };

static bool     synced = false;
static bool     syncWaiting = false;
static uint32_t syncLastClock = 0;
static uint16_t syncClocks = 0;

static uint8_t  held[16];          // bitmap of keys currently down on the Amiga side
static bool     capsOn = false;
static bool     mouseOwnsAmiga = false;   // Left Amiga pressed by a mouse button-down, not by the host

static uint16_t cfgGapMs = 15;     // pause between key events
static uint16_t cfgHoldMs = 25;    // down→up time for a tap
static uint16_t cfgResetMs = 500;  // /KBRESET + KCLK low time
static bool     cfgAttn = true;    // 20 µs KDAT pulse before each frame
static uint8_t  cfgRawBitUs = 60;  // RAWHEX bit time; 60 = HRM keyboard timing
static bool     cfgRawAttn = true; // RAWHEX attention pulse
static uint8_t  txBitUs = 60;      // timing used by clockFrame; RAWHEX swaps in the cfgRaw values
static bool     txAttn = true;
static uint8_t  cfgMFast = 0;      // plain steps covered by one Shift step (0 = never use Shift)
static uint8_t  cfgMHome = 40;     // Shift steps per axis for HOME

static struct { uint32_t tx, ack, nak, resync, resets; } stats;

struct LedSense {
  uint8_t  pin;
  const char *name;
  bool     on;
  uint8_t  votes;
  uint16_t mv;
  uint32_t changedAt;
};
static LedSense ledPower = { PIN_LED_POWER, "power", false, 0, 0, 0 };
static LedSense ledDrive = { PIN_LED_DRIVE, "drive", false, 0, 0, 0 };
static uint32_t ledLastSample = 0;

// ---- Small helpers -------------------------------------------------------------------------
static void event(const __FlashStringHelper *msg) { Serial.print(F("# ")); Serial.println(msg); }

static inline bool isHeld(uint8_t k)  { return held[k >> 3] & (1 << (k & 7)); }
static inline void setHeld(uint8_t k, bool on) {
  if (on) held[k >> 3] |= (1 << (k & 7)); else held[k >> 3] &= ~(1 << (k & 7));
}
static uint8_t heldCount() {
  uint8_t n = 0;
  for (uint8_t k = 0; k < 0x80; k++) if (isHeld(k)) n++;
  return n;
}

// Transmission order is 6-5-4-3-2-1-0-7: rotate left so bit 7 (up flag) goes last.
static inline uint8_t rotl(uint8_t b) { return (b << 1) | (b >> 7); }

static inline void ackFlagClear() { PCIFR = _BV(PCIF0); }
static inline bool ackSeen() { return (PCIFR & _BV(PCIF0)) || !LINE_IS_HIGH(KDAT_BIT); }

// ---- LED sense -----------------------------------------------------------------------------
static void ledSample(LedSense &l) {
  l.mv = (uint16_t)(analogRead(l.pin) * 5000UL / 1023UL);
  bool raw = l.on ? (l.mv > 800) : (l.mv > 1200);   // hysteresis
  if (raw == l.on) { l.votes = 0; return; }
  if (++l.votes < 3) return;                         // 3 consecutive samples = 30 ms debounce
  l.votes = 0;
  l.on = raw;
  l.changedAt = millis();
  Serial.print(F("# LED ")); Serial.print(l.name); Serial.print('='); Serial.println(l.on ? 1 : 0);
}

static void ledService() {
  if (millis() - ledLastSample < 10) return;
  ledLastSample = millis();
  ledSample(ledPower);
  ledSample(ledDrive);
}

// Delay that keeps LED tracking alive.
static void pause(uint16_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) ledService();
}

// ---- Line level ----------------------------------------------------------------------------
// Both lines idle high means the Amiga is powered and not mid-handshake.
static bool waitIdle(uint16_t ms) {
  uint32_t t0 = millis();
  while (!(LINE_IS_HIGH(KDAT_BIT) && LINE_IS_HIGH(KCLK_BIT))) {
    if (millis() - t0 >= ms) return false;
  }
  return true;
}

// Waits for the handshake (Amiga pulls KDAT low). The pin-change flag latches pulses that
// are shorter than the polling interval.
static bool waitAck(uint16_t timeoutMs) {
  uint32_t t0 = millis();
  while (!ackSeen()) {
    if (millis() - t0 >= timeoutMs) return false;
  }
  t0 = millis();
  while (!LINE_IS_HIGH(KDAT_BIT) && millis() - t0 < 10) {}
  return true;
}

// One 8-bit frame, MSB first, KDAT low = 1. txBitUs per bit (60 µs as in HRM App. G), split into
// data setup / KCLK low / KCLK high thirds. _delay_loop_2 runs 4 cycles = 0.25 µs per count at 16 MHz.
static void clockFrame(uint8_t tx) {
  uint16_t third = (uint16_t)txBitUs * 4 / 3;
  uint8_t sreg = SREG;
  cli();
  if (txAttn) {
    LINE_LOW(KDAT_BIT); _delay_us(20); LINE_REL(KDAT_BIT); _delay_us(100);
  }
  for (uint8_t i = 0; i < 8; i++) {
    if (tx & 0x80) LINE_LOW(KDAT_BIT); else LINE_REL(KDAT_BIT);
    tx <<= 1;
    _delay_loop_2(third);
    LINE_LOW(KCLK_BIT);
    _delay_loop_2(third);
    LINE_REL(KCLK_BIT);
    _delay_loop_2(third);
  }
  LINE_REL(KDAT_BIT);
  _delay_us(5);          // let KDAT rise before arming the handshake latch
  ackFlagClear();
  SREG = sreg;
}

// Single 1 bit, used to (re)gain sync.
static void clockOne() {
  uint8_t sreg = SREG;
  cli();
  LINE_LOW(KDAT_BIT);
  _delay_us(20);
  LINE_LOW(KCLK_BIT);
  _delay_us(20);
  LINE_REL(KCLK_BIT);
  _delay_us(20);
  LINE_REL(KDAT_BIT);
  _delay_us(5);
  ackFlagClear();
  SREG = sreg;
}

static bool sendFrame(uint8_t code) {
  if (!waitIdle(20)) return false;
  clockFrame(rotl(code));
  stats.tx++;
  if (waitAck(ACK_TIMEOUT_MS)) { stats.ack++; return true; }
  stats.nak++;
  return false;
}

static bool resyncBlocking() {
  for (uint8_t i = 0; i < RESYNC_MAX_BITS; i++) {
    if (!waitIdle(20)) return false;
    clockOne();
    if (waitAck(ACK_TIMEOUT_MS)) return true;
  }
  return false;
}

static void dropSync() {
  synced = false;
  syncWaiting = false;
  syncClocks = 0;
}

// Sends a key code (with up flag) or special code. On a missed handshake, follows the HRM:
// clock 1s until handshake, send $F9, retransmit. If that fails, background power-up sync takes over.
static SendResult sendCode(uint8_t code) {
  if (!synced) return SEND_NOSYNC;
  if (sendFrame(code)) return SEND_OK;
  stats.resync++;
  if (resyncBlocking() && sendFrame(AK_LOSTSYNC) && sendFrame(code)) {
    event(F("RESYNC ok"));
    return SEND_OK;
  }
  dropSync();
  event(F("SYNC lost"));
  return SEND_FAIL;
}

// Background power-up sync: clock a 1 every 143 ms until the Amiga handshakes, then send the
// power-up key stream ($FD, no keys, $FE). Same as the real keyboard after power-on.
static void syncService() {
  if (synced) return;
  if (syncWaiting) {
    if (ackSeen()) {
      uint32_t t0 = millis();
      while (!LINE_IS_HIGH(KDAT_BIT) && millis() - t0 < 10) {}
      syncWaiting = false;
      memset(held, 0, sizeof(held));
  mouseOwnsAmiga = false;
      capsOn = false;
      if (sendFrame(AK_INITPOWER) && sendFrame(AK_TERMPOWER)) {
        synced = true;
        Serial.print(F("# SYNC ok clocks=")); Serial.println(syncClocks);
      }
      syncClocks = 0;
      return;
    }
    if (millis() - syncLastClock < ACK_TIMEOUT_MS) return;
    syncWaiting = false;
  }
  // Lines low = Amiga off (its pull-ups unpowered) or busy; try again later.
  if (!(LINE_IS_HIGH(KDAT_BIT) && LINE_IS_HIGH(KCLK_BIT))) return;
  clockOne();
  syncLastClock = millis();
  syncWaiting = true;
  if (syncClocks < 0xFFFF) syncClocks++;
}

static void amigaReset(uint16_t ms) {
  // A500 resets via /KBRESET; holding KCLK low as well covers the A1000/A2000 style reset.
  LINE_LOW(KRST_BIT);
  LINE_LOW(KCLK_BIT);
  pause(ms);
  LINE_REL(KCLK_BIT);
  LINE_REL(KRST_BIT);
  memset(held, 0, sizeof(held));
  mouseOwnsAmiga = false;
  capsOn = false;
  dropSync();
  stats.resets++;
  event(F("RESET"));
}

// ---- Key events ----------------------------------------------------------------------------
static SendResult keyEvent(uint8_t key, bool down) {
  key &= 0x7F;
  if (key == AK_CAPS) {
    // Caps Lock only sends on press; the up/down bit carries the new LED state (down = on).
    if (!down) return SEND_OK;
    bool next = !capsOn;
    SendResult r = sendCode(AK_CAPS | (next ? 0 : AK_UPFLAG));
    if (r == SEND_OK) capsOn = next;
    return r;
  }
  if (down) {
    setHeld(key, true);
    if (isHeld(AK_CTRL) && isHeld(AK_LAMIGA) && isHeld(AK_RAMIGA)) {
      // The A500 keyboard turns Ctrl + Amiga + Amiga into a hardware reset.
      amigaReset(cfgResetMs);
      return SEND_OK;
    }
  }
  SendResult r = sendCode(key | (down ? 0 : AK_UPFLAG));
  if (down && r != SEND_OK) setHeld(key, false);
  if (!down && r == SEND_OK) setHeld(key, false);
  return r;
}

static SendResult releaseAll() {
  SendResult worst = SEND_OK;
  for (uint8_t k = 0; k < 0x80; k++) {
    if (!isHeld(k)) continue;
    if (!synced) { setHeld(k, false); continue; }
    SendResult r = keyEvent(k, false);
    if (r != SEND_OK) { worst = r; setHeld(k, false); }
    pause(cfgGapMs);
  }
  mouseOwnsAmiga = false;
  return worst;
}

// ---- Keyboard mouse (Intuition) ------------------------------------------------------------
// Amiga + cursor key moves the pointer one step per key-down; Shift makes the step larger.
// Holding a cursor key accelerates the pointer, so moves are made of separate taps.
// Amiga + Left Alt = left button, Amiga + Right Alt = right button.
enum : uint8_t { AK_UP = 0x4C, AK_DOWN = 0x4D, AK_RIGHT = 0x4E, AK_LEFT = 0x4F };

static SendResult pressIfUp(uint8_t key, bool *pressed) {
  *pressed = false;
  if (isHeld(key)) return SEND_OK;
  SendResult r = keyEvent(key, true);
  if (r == SEND_OK) { *pressed = true; pause(cfgGapMs); }
  return r;
}

static SendResult mouseSteps(uint8_t dirKey, uint16_t n, bool fast) {
  if (n == 0) return SEND_OK;
  bool ownAmiga, ownShift = false;
  SendResult r = pressIfUp(AK_LAMIGA, &ownAmiga);
  if (r == SEND_OK && fast) r = pressIfUp(AK_LSHIFT, &ownShift);
  for (uint16_t i = 0; i < n && r == SEND_OK; i++) {
    r = keyEvent(dirKey, true);
    pause(cfgHoldMs);
    if (r == SEND_OK) r = keyEvent(dirKey, false);
    pause(cfgGapMs);
  }
  if (ownShift) { keyEvent(AK_LSHIFT, false); pause(cfgGapMs); }
  if (ownAmiga) { keyEvent(AK_LAMIGA, false); pause(cfgGapMs); }
  return r;
}

// Relative move in plain-step units; uses Shift steps for the bulk once MFAST is calibrated.
static SendResult mouseAxis(int16_t d, uint8_t negKey, uint8_t posKey) {
  uint8_t key = d < 0 ? negKey : posKey;
  uint16_t n = d < 0 ? -d : d;
  SendResult r = SEND_OK;
  if (cfgMFast > 1 && n >= cfgMFast) {
    r = mouseSteps(key, n / cfgMFast, true);
    n %= cfgMFast;
  }
  if (r == SEND_OK) r = mouseSteps(key, n, false);
  return r;
}

static SendResult mouseButton(char which, bool down) {
  uint8_t alt = which == 'R' ? AK_RALT : AK_LALT;
  if (down) {
    bool own;
    SendResult r = pressIfUp(AK_LAMIGA, &own);
    if (own) mouseOwnsAmiga = true;
    if (r == SEND_OK) r = keyEvent(alt, true);
    return r;
  }
  SendResult r = keyEvent(alt, false);
  pause(cfgGapMs);
  if (mouseOwnsAmiga && !isHeld(AK_LALT) && !isHeld(AK_RALT)) {
    SendResult x = keyEvent(AK_LAMIGA, false);
    mouseOwnsAmiga = false;
    if (r == SEND_OK) r = x;
  }
  return r;
}

static SendResult mouseHome() {
  SendResult r = mouseSteps(AK_LEFT, cfgMHome, true);
  if (r == SEND_OK) r = mouseSteps(AK_UP, cfgMHome, true);
  return r;
}

// ---- Key name / ASCII mapping --------------------------------------------------------------
static const uint8_t LETTERS[26] PROGMEM = {
  0x20, 0x35, 0x33, 0x22, 0x12, 0x23, 0x24, 0x25, 0x17, 0x26, 0x27, 0x28, 0x37,  // a-m
  0x36, 0x18, 0x19, 0x10, 0x13, 0x21, 0x14, 0x16, 0x34, 0x11, 0x32, 0x15, 0x31   // n-z
};

// US layout. Returns keycode, with bit 7 set if Shift is needed; AK_NONE if unmapped.
static uint8_t asciiToAmiga(char c) {
  if (c >= 'a' && c <= 'z') return pgm_read_byte(&LETTERS[c - 'a']);
  if (c >= 'A' && c <= 'Z') return pgm_read_byte(&LETTERS[c - 'A']) | 0x80;
  if (c >= '1' && c <= '9') return c - '0';
  switch (c) {
    case '0': return 0x0A;  case ' ': return AK_SPACE;
    case '`': return 0x00;  case '~': return 0x80;
    case '!': return 0x81;  case '@': return 0x82;  case '#': return 0x83;  case '$': return 0x84;
    case '%': return 0x85;  case '^': return 0x86;  case '&': return 0x87;  case '*': return 0x88;
    case '(': return 0x89;  case ')': return 0x8A;
    case '-': return 0x0B;  case '_': return 0x8B;  case '=': return 0x0C;  case '+': return 0x8C;
    case '\\': return 0x0D; case '|': return 0x8D;
    case '[': return 0x1A;  case '{': return 0x9A;  case ']': return 0x1B;  case '}': return 0x9B;
    case ';': return 0x29;  case ':': return 0xA9;  case '\'': return 0x2A; case '"': return 0xAA;
    case ',': return 0x38;  case '<': return 0xB8;  case '.': return 0x39;  case '>': return 0xB9;
    case '/': return 0x3A;  case '?': return 0xBA;
  }
  return AK_NONE;
}

struct KeyName { char name[10]; uint8_t code; };
static const KeyName KEY_NAMES[] PROGMEM = {
  {"LAMIGA", AK_LAMIGA}, {"LA", AK_LAMIGA}, {"RAMIGA", AK_RAMIGA}, {"RA", AK_RAMIGA},
  {"LSHIFT", AK_LSHIFT}, {"SHIFT", AK_LSHIFT}, {"RSHIFT", AK_RSHIFT},
  {"CTRL", AK_CTRL}, {"CONTROL", AK_CTRL},
  {"LALT", AK_LALT}, {"ALT", AK_LALT}, {"RALT", AK_RALT},
  {"CAPS", AK_CAPS}, {"CAPSLOCK", AK_CAPS},
  {"SPACE", AK_SPACE}, {"BS", AK_BS}, {"BACKSPACE", AK_BS}, {"TAB", AK_TAB},
  {"RETURN", AK_RETURN}, {"RET", AK_RETURN}, {"ENTER", AK_RETURN}, {"KPENTER", AK_KPENTER},
  {"ESC", AK_ESC}, {"DEL", AK_DEL}, {"HELP", 0x5F},
  {"UP", 0x4C}, {"DOWN", 0x4D}, {"RIGHT", 0x4E}, {"LEFT", 0x4F},
  {"F1", 0x50}, {"F2", 0x51}, {"F3", 0x52}, {"F4", 0x53}, {"F5", 0x54},
  {"F6", 0x55}, {"F7", 0x56}, {"F8", 0x57}, {"F9", 0x58}, {"F10", 0x59},
  {"KP0", 0x0F}, {"KP1", 0x1D}, {"KP2", 0x1E}, {"KP3", 0x1F}, {"KP4", 0x2D},
  {"KP5", 0x2E}, {"KP6", 0x2F}, {"KP7", 0x3D}, {"KP8", 0x3E}, {"KP9", 0x3F},
  {"KPDOT", 0x3C}, {"KPMINUS", 0x4A}, {"KPPLUS", 0x5E}, {"KPSTAR", 0x5D}, {"KPSLASH", 0x5C},
  {"KPLPAREN", 0x5A}, {"KPRPAREN", 0x5B},
};

// Accepts a key name, a single character (its unshifted key), or $NN / 0xNN raw code.
static uint8_t parseKey(const char *tok) {
  size_t n = strlen(tok);
  if (n == 0) return AK_NONE;
  if (tok[0] == '$' || (n > 2 && tok[0] == '0' && (tok[1] == 'x' || tok[1] == 'X'))) {
    char *end;
    long v = strtol(tok + (tok[0] == '$' ? 1 : 2), &end, 16);
    return (*end == 0 && v >= 0 && v < 0x80) ? (uint8_t)v : (uint8_t)AK_NONE;
  }
  for (uint8_t i = 0; i < sizeof(KEY_NAMES) / sizeof(KEY_NAMES[0]); i++) {
    KeyName e;
    memcpy_P(&e, &KEY_NAMES[i], sizeof(e));
    if (strcasecmp(tok, e.name) == 0) return e.code;
  }
  if (n == 1) {
    uint8_t m = asciiToAmiga(tok[0]);
    return m == AK_NONE ? AK_NONE : (m & 0x7F);
  }
  return AK_NONE;
}

// Splits "A+B+C" into keycodes. Returns count, or -1 with *bad pointing at the bad token.
static int8_t parseChord(char *s, uint8_t *keys, uint8_t maxKeys, const char **bad) {
  uint8_t n = 0;
  char *save;
  for (char *tok = strtok_r(s, "+", &save); tok; tok = strtok_r(NULL, "+", &save)) {
    while (*tok == ' ') tok++;
    uint8_t k = parseKey(tok);
    if (k == AK_NONE || n >= maxKeys) { *bad = tok; return -1; }
    keys[n++] = k;
  }
  if (n == 0) { *bad = ""; return -1; }
  return n;
}

// ---- Responses -----------------------------------------------------------------------------
static void replyResult(SendResult r) {
  if (r == SEND_OK) Serial.println(F("OK"));
  else if (r == SEND_NOSYNC) Serial.println(F("ERR NOSYNC"));
  else Serial.println(F("ERR NOACK"));
}

static void printStatus() {
  Serial.print(F("OK sync="));    Serial.print(synced ? 1 : 0);
  Serial.print(F(" caps="));      Serial.print(capsOn ? 1 : 0);
  Serial.print(F(" kclk="));      Serial.print(LINE_IS_HIGH(KCLK_BIT) ? 1 : 0);
  Serial.print(F(" kdat="));      Serial.print(LINE_IS_HIGH(KDAT_BIT) ? 1 : 0);
  Serial.print(F(" rst="));       Serial.print(LINE_IS_HIGH(KRST_BIT) ? 1 : 0);
  Serial.print(F(" power="));     Serial.print(ledPower.on ? 1 : 0);
  Serial.print(F(" drive="));     Serial.print(ledDrive.on ? 1 : 0);
  Serial.print(F(" power_mv="));  Serial.print(ledPower.mv);
  Serial.print(F(" drive_mv="));  Serial.print(ledDrive.mv);
  Serial.print(F(" held="));      Serial.print(heldCount());
  Serial.print(F(" tx="));        Serial.print(stats.tx);
  Serial.print(F(" ack="));       Serial.print(stats.ack);
  Serial.print(F(" nak="));       Serial.print(stats.nak);
  Serial.print(F(" resync="));    Serial.print(stats.resync);
  Serial.print(F(" resets="));    Serial.println(stats.resets);
}

static void printConfig() {
  Serial.print(F("OK gap="));   Serial.print(cfgGapMs);
  Serial.print(F(" hold="));    Serial.print(cfgHoldMs);
  Serial.print(F(" reset="));   Serial.print(cfgResetMs);
  Serial.print(F(" attn="));    Serial.print(cfgAttn ? 1 : 0);
  Serial.print(F(" rawbit="));  Serial.print(cfgRawBitUs);
  Serial.print(F(" rawattn=")); Serial.print(cfgRawAttn ? 1 : 0);
  Serial.print(F(" mfast="));   Serial.print(cfgMFast);
  Serial.print(F(" mhome="));   Serial.println(cfgMHome);
}

static void printHelp() {
  Serial.println(F("# T <text>            type text (US layout; escapes \\n \\t \\e \\b \\\\)"));
  Serial.println(F("# K <k1+k2+...>       tap chord, e.g. K LAMIGA+M, K RAMIGA+Q, K CTRL+C"));
  Serial.println(F("# D <chord> / U <chord>  hold / release keys"));
  Serial.println(F("# X                   release all held keys"));
  Serial.println(F("# R [ms]              reset the Amiga (/KBRESET low)"));
  Serial.println(F("# SYNC                drop sync and redo the power-up handshake"));
  Serial.println(F("# WAITDISK [quiet_ms] [timeout_ms]  wait for the drive LED to stay off"));
  Serial.println(F("# RAW <hex>           send one raw 8-bit code (before rotation)"));
  Serial.println(F("# M <dx> <dy> | MOVE <UP|DOWN|LEFT|RIGHT> <n> [FAST] | HOME | MABS <x> <y>"));
  Serial.println(F("# CLICK [L|R] [count] | MDOWN [L|R] | MUP [L|R]   (Left Amiga + Alt)"));
  Serial.println(F("# S | L | V | CFG [GAP|HOLD|RESET|ATTN|RAWBIT|RAWATTN|MFAST|MHOME <n>]"));
  Serial.println(F("# key names: LAMIGA RAMIGA LSHIFT RSHIFT CTRL LALT RALT CAPS SPACE BS TAB RETURN"));
  Serial.println(F("#   KPENTER ESC DEL HELP UP DOWN LEFT RIGHT F1-F10 KP0-KP9 KPDOT KPMINUS KPPLUS"));
  Serial.println(F("#   KPSTAR KPSLASH KPLPAREN KPRPAREN, any single char, or $NN"));
  Serial.println(F("OK"));
}

// ---- Commands ------------------------------------------------------------------------------
// Raw transport for LinkAgent. The whole hex string is checked before clocking.
// OK acknowledges keyboard handshakes only; the host must also check the screen ACK.
static void cmdRawHex(const char *arg) {
  uint8_t codes[96];
  size_t n = strlen(arg);
  if (!n || (n & 1) || n > sizeof(codes) * 2) {
    Serial.println(F("ERR BADARG")); return;
  }
  for (size_t i = 0; i < n; i++) {
    char c = toupper(arg[i]);
    if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F'))) {
      Serial.println(F("ERR BADARG")); return;
    }
    uint8_t v = c <= '9' ? c - '0' : c - 'A' + 10;
    if (!(i & 1)) codes[i / 2] = v << 4;
    else codes[i / 2] |= v;
  }
  if (!synced) { Serial.println(F("ERR NOSYNC")); return; }
  txBitUs = cfgRawBitUs;
  txAttn = cfgRawAttn;
  SendResult r = SEND_OK;
  for (size_t i = 0; i < n / 2 && r == SEND_OK; i++) r = sendCode(codes[i]);
  txBitUs = 60;
  txAttn = cfgAttn;
  if (r != SEND_OK) replyResult(r);
  else Serial.println(F("OK"));
}

static void cmdType(const char *text) {
  // Decode and validate everything before sending anything.
  static uint8_t codes[200];
  uint8_t n = 0;
  for (uint16_t i = 0; text[i]; i++) {
    char c = text[i];
    uint8_t m;
    if (c == '\\' && text[i + 1]) {
      char e = text[++i];
      if (e == 'n' || e == 'r') m = AK_RETURN;
      else if (e == 't') m = AK_TAB;
      else if (e == 'e') m = AK_ESC;
      else if (e == 'b') m = AK_BS;
      else if (e == '\\') m = 0x0D;
      else { Serial.print(F("ERR BADESC ")); Serial.println(i); return; }
    } else {
      m = asciiToAmiga(c);
      if (m == AK_NONE) { Serial.print(F("ERR BADCHAR ")); Serial.println(i); return; }
    }
    if (n >= sizeof(codes)) { Serial.println(F("ERR TOOLONG")); return; }
    codes[n++] = m;
  }
  if (!synced) { Serial.println(F("ERR NOSYNC")); return; }

  for (uint8_t i = 0; i < n; i++) {
    bool shift = codes[i] & 0x80;
    uint8_t key = codes[i] & 0x7F;
    SendResult r = SEND_OK;
    if (shift && !isHeld(AK_LSHIFT)) { r = keyEvent(AK_LSHIFT, true); pause(cfgGapMs); }
    if (r == SEND_OK) { r = keyEvent(key, true); pause(cfgHoldMs); }
    if (r == SEND_OK) { r = keyEvent(key, false); pause(cfgGapMs); }
    if (shift && r == SEND_OK) { r = keyEvent(AK_LSHIFT, false); pause(cfgGapMs); }
    if (r != SEND_OK) {
      releaseAll();
      Serial.print(r == SEND_NOSYNC ? F("ERR NOSYNC at ") : F("ERR NOACK at "));
      Serial.println(i);
      return;
    }
  }
  Serial.print(F("OK ")); Serial.println(n);
}

// mode: 'K' tap, 'D' hold, 'U' release
static void cmdChord(char mode, char *arg) {
  uint8_t keys[8];
  const char *bad;
  int8_t n = parseChord(arg, keys, sizeof(keys), &bad);
  if (n < 0) { Serial.print(F("ERR BADKEY ")); Serial.println(bad); return; }
  if (!synced) { Serial.println(F("ERR NOSYNC")); return; }

  uint32_t resetsBefore = stats.resets;
  SendResult r = SEND_OK;
  if (mode == 'K' || mode == 'D') {
    for (int8_t i = 0; i < n && r == SEND_OK; i++) {
      r = keyEvent(keys[i], true);
      pause(i == n - 1 && mode == 'K' ? cfgHoldMs : cfgGapMs);
      if (stats.resets != resetsBefore) { Serial.println(F("OK RESET")); return; }
    }
  }
  if (mode == 'K' || mode == 'U') {
    SendResult rr = SEND_OK;
    for (int8_t i = n - 1; i >= 0; i--) {
      if (mode == 'K' && !isHeld(keys[i]) && keys[i] != AK_CAPS) continue;
      SendResult x = keyEvent(keys[i], false);
      if (x != SEND_OK) rr = x;
      pause(cfgGapMs);
    }
    if (r == SEND_OK) r = rr;
  }
  if (r != SEND_OK && mode != 'D') releaseAll();
  replyResult(r);
}

static void cmdWaitDisk(char *arg) {
  uint32_t quiet = 1500, timeout = 60000;
  char *save;
  char *a = strtok_r(arg, " ", &save);
  if (a) quiet = strtoul(a, NULL, 10);
  a = strtok_r(NULL, " ", &save);
  if (a) timeout = strtoul(a, NULL, 10);
  uint32_t start = millis();
  while (true) {
    ledService();
    uint32_t since = ledDrive.changedAt > start ? ledDrive.changedAt : start;
    if (!ledDrive.on && millis() - since >= quiet) { Serial.println(F("OK idle")); return; }
    if (millis() - start >= timeout) { Serial.println(F("ERR TIMEOUT")); return; }
  }
}

static void cmdConfig(char *arg) {
  char *save;
  char *key = strtok_r(arg, " ", &save);
  char *val = strtok_r(NULL, " ", &save);
  if (!key) { printConfig(); return; }
  if (!val) { Serial.println(F("ERR BADARG")); return; }
  uint16_t v = (uint16_t)strtoul(val, NULL, 10);
  if (!strcasecmp(key, "GAP")) cfgGapMs = v;
  else if (!strcasecmp(key, "HOLD")) cfgHoldMs = v;
  else if (!strcasecmp(key, "RESET")) cfgResetMs = v < 100 ? 100 : v;
  else if (!strcasecmp(key, "ATTN")) txAttn = cfgAttn = v != 0;
  else if (!strcasecmp(key, "RAWBIT")) cfgRawBitUs = v < 3 ? 3 : v > 60 ? 60 : v;
  else if (!strcasecmp(key, "RAWATTN")) cfgRawAttn = v != 0;
  else if (!strcasecmp(key, "MFAST")) cfgMFast = v > 255 ? 255 : v;
  else if (!strcasecmp(key, "MHOME")) cfgMHome = v > 255 ? 255 : v;
  else { Serial.println(F("ERR BADARG")); return; }
  printConfig();
}

static bool parseInt(const char *s, long *out) {
  if (!s) return false;
  char *end;
  *out = strtol(s, &end, 10);
  return end != s && *end == 0;
}

// M <dx> <dy>          relative move in plain steps (+x right, +y down)
// MOVE <dir> <n> [FAST] n taps in one direction
// MABS <x> <y>         HOME, then relative move
static void cmdMouseMove(const char *cmd, char *arg) {
  char *save;
  char *a = strtok_r(arg, " ", &save);
  char *b = strtok_r(NULL, " ", &save);
  char *c = strtok_r(NULL, " ", &save);
  SendResult r;
  if (!strcmp(cmd, "MOVE")) {
    long n;
    uint8_t key = parseKey(a ? a : "");
    if ((key != AK_UP && key != AK_DOWN && key != AK_LEFT && key != AK_RIGHT) ||
        !parseInt(b, &n) || n < 0 || n > 2000) { Serial.println(F("ERR BADARG")); return; }
    if (!synced) { Serial.println(F("ERR NOSYNC")); return; }
    r = mouseSteps(key, (uint16_t)n, c && !strcasecmp(c, "FAST"));
  } else {
    long x, y;
    bool abs = !strcmp(cmd, "MABS");
    if (!parseInt(a, &x) || !parseInt(b, &y) || labs(x) > 2000 || labs(y) > 2000 ||
        (abs && (x < 0 || y < 0))) {
      Serial.println(F("ERR BADARG")); return;
    }
    if (!synced) { Serial.println(F("ERR NOSYNC")); return; }
    r = abs ? mouseHome() : SEND_OK;
    if (r == SEND_OK) r = mouseAxis((int16_t)x, AK_LEFT, AK_RIGHT);
    if (r == SEND_OK) r = mouseAxis((int16_t)y, AK_UP, AK_DOWN);
  }
  if (r != SEND_OK) releaseAll();
  replyResult(r);
}

// CLICK [L|R] [count], MDOWN [L|R], MUP [L|R]
static void cmdMouseButton(const char *cmd, char *arg) {
  char *save;
  char *a = strtok_r(arg, " ", &save);
  char *b = strtok_r(NULL, " ", &save);
  char which = a ? toupper(a[0]) : 'L';
  long n = 1;
  if ((which != 'L' && which != 'R') || (b && (!parseInt(b, &n) || n < 1 || n > 3))) {
    Serial.println(F("ERR BADARG")); return;
  }
  if (!synced) { Serial.println(F("ERR NOSYNC")); return; }
  SendResult r;
  if (!strcmp(cmd, "MDOWN")) r = mouseButton(which, true);
  else if (!strcmp(cmd, "MUP")) r = mouseButton(which, false);
  else {
    r = SEND_OK;
    for (long i = 0; i < n && r == SEND_OK; i++) {
      r = mouseButton(which, true);
      pause(cfgHoldMs);
      if (r == SEND_OK) r = mouseButton(which, false);
      pause(cfgGapMs);
    }
  }
  if (r != SEND_OK) releaseAll();
  replyResult(r);
}

static void handleLine(char *line) {
  while (*line == ' ') line++;
  if (!*line) return;
  char *arg = line;
  while (*arg && *arg != ' ') arg++;
  if (*arg) *arg++ = 0;           // arg keeps everything after the first space verbatim
  for (char *p = line; *p; p++) *p = toupper(*p);

  if (!strcmp(line, "T")) cmdType(arg);
  else if (!strcmp(line, "K") || !strcmp(line, "D") || !strcmp(line, "U")) cmdChord(line[0], arg);
  else if (!strcmp(line, "X")) replyResult(releaseAll());
  else if (!strcmp(line, "R")) {
    uint16_t ms = *arg ? (uint16_t)strtoul(arg, NULL, 10) : cfgResetMs;
    amigaReset(ms < 100 ? 100 : ms);
    Serial.println(F("OK"));
  }
  else if (!strcmp(line, "SYNC")) { dropSync(); Serial.println(F("OK")); }
  else if (!strcmp(line, "WAITDISK")) cmdWaitDisk(arg);
  else if (!strcmp(line, "M") || !strcmp(line, "MOVE") || !strcmp(line, "MABS")) cmdMouseMove(line, arg);
  else if (!strcmp(line, "HOME")) {
    if (!synced) Serial.println(F("ERR NOSYNC"));
    else { SendResult r = mouseHome(); if (r != SEND_OK) releaseAll(); replyResult(r); }
  }
  else if (!strcmp(line, "CLICK") || !strcmp(line, "MDOWN") || !strcmp(line, "MUP")) cmdMouseButton(line, arg);
  else if (!strcmp(line, "RAWHEX")) cmdRawHex(arg);
  else if (!strcmp(line, "RAW")) {
    char *end;
    long v = strtol(arg, &end, 16);
    if (end == arg || v < 0 || v > 0xFF) Serial.println(F("ERR BADARG"));
    else replyResult(sendCode((uint8_t)v));
  }
  else if (!strcmp(line, "S")) printStatus();
  else if (!strcmp(line, "L")) {
    Serial.print(F("OK power=")); Serial.print(ledPower.on ? 1 : 0);
    Serial.print(F(" drive="));   Serial.print(ledDrive.on ? 1 : 0);
    Serial.print(F(" power_mv=")); Serial.print(ledPower.mv);
    Serial.print(F(" drive_mv=")); Serial.println(ledDrive.mv);
  }
  else if (!strcmp(line, "V")) { Serial.print(F("OK ")); Serial.println(F(FW_VERSION)); }
  else if (!strcmp(line, "CFG")) cmdConfig(arg);
  else if (!strcmp(line, "?") || !strcmp(line, "HELP")) printHelp();
  else Serial.println(F("ERR UNKNOWN"));
}

// ---- Main ----------------------------------------------------------------------------------
void setup() {
  // Lines idle released; PORTB bits stay 0 so enabling the output can only pull low.
  PORTB &= ~(KCLK_BIT | KDAT_BIT | KRST_BIT);
  DDRB &= ~(KCLK_BIT | KDAT_BIT | KRST_BIT);
  PCMSK0 = _BV(PCINT5);   // KDAT changes latch PCIF0; the interrupt itself stays disabled
  ackFlagClear();
  Serial.begin(115200);
}

void loop() {
  static char buf[240];
  static uint8_t len = 0;
  static bool overflow = false;

  ledService();
  syncService();

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r' || c == '\n') {
      if (overflow) Serial.println(F("ERR TOOLONG"));
      else if (len) { buf[len] = 0; handleLine(buf); }
      len = 0;
      overflow = false;
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    } else {
      overflow = true;
    }
  }
}
