// led.h — status LED (single WS2812 on the Waveshare ESP32-S3 Zero, GPIO 21).
//
// Exposes named states so other modules don't touch RGB values directly.
// The runtime effect is non-blocking (breathing / blink driven from tick()).
//
// State colors (restored from the stock firmware behavior):
//   BOOT       dim white
//   IDLE       slow breathing warm white
//   CONNECTED  slow breathing green
//   DISCONNECT solid dim red
//   LISTENING  fast pulsing blue
//   SPEAKING   pulsing cyan
//   WALKING    green chase
//   TRIPPED    fast red flash
//   LOW_BATT   amber blink
//   APP       color supplied over BLE {"c":"led","r":..,"g":..,"b":..}

#pragma once

#include <stdint.h>

namespace robot {

enum class LedState : uint8_t {
  BOOT,
  IDLE,
  CONNECTED,
  DISCONNECTED,
  LISTENING,
  SPEAKING,
  WALKING,
  TRIPPED,
  LOW_BATT,
  APP,        // set via setAppColor() + setState(APP)
};

class Led {
 public:
  // Board default: Waveshare ESP32-S3 Zero on-board WS2812 is on GPIO 21.
  // If your board uses a different pin (DevKitC-1 = 48), call begin(pin).
  void begin(uint8_t pin = 21, uint8_t brightness = 48);

  // Switch to a named state. Idempotent; does nothing if already in that state.
  void setState(LedState s);

  // App-supplied RGB override. Switches state to APP.
  void setAppColor(uint8_t r, uint8_t g, uint8_t b);

  // Drive the animation — call from the main loop. Cheap (~100 us max).
  void tick(uint32_t now_ms);

 private:
  void showRGB(uint8_t r, uint8_t g, uint8_t b);
  void render(uint32_t now_ms);

  void* strip_ = nullptr;      // Adafruit_NeoPixel* (void* to keep header lean)
  uint8_t pin_ = 21;
  uint8_t bright_ = 48;
  LedState state_ = LedState::BOOT;
  LedState lastRendered_ = LedState::BOOT;
  uint32_t stateEnteredMs_ = 0;
  uint32_t lastFrameMs_ = 0;
  uint8_t  appR_ = 0, appG_ = 0, appB_ = 0;
  bool     appDirty_ = false;
  bool     begun_ = false;
};

} // namespace robot
