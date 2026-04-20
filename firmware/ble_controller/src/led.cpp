#include "led.h"
#include <Arduino.h>
#include <Adafruit_NeoPixel.h>

namespace robot {

namespace {

inline uint32_t packRGB(uint8_t r, uint8_t g, uint8_t b) {
  return (uint32_t(r) << 16) | (uint32_t(g) << 8) | uint32_t(b);
}

// Sine-style breathing envelope in [0..1] from a phase in [0..1].
inline float breathe(float phase01) {
  // Simple 2*(1 - |2x-1|) triangle wave for a 0..1..0 ramp — avoids sin/cos.
  float t = phase01 - float(int(phase01));
  return 1.0f - fabsf(2.0f * t - 1.0f);
}

} // namespace

void Led::begin(uint8_t pin, uint8_t brightness) {
  pin_ = pin;
  bright_ = brightness;
  auto* s = new Adafruit_NeoPixel(1, pin_, NEO_GRB + NEO_KHZ800);
  strip_ = s;
  s->begin();
  s->setBrightness(bright_);
  s->clear();
  s->show();
  begun_ = true;
  stateEnteredMs_ = millis();
  setState(LedState::BOOT);
}

void Led::setState(LedState s) {
  if (s == state_) return;
  state_ = s;
  stateEnteredMs_ = millis();
  // Force the next render frame to paint, even if it's the same color.
  lastRendered_ = LedState::BOOT;
}

void Led::setAppColor(uint8_t r, uint8_t g, uint8_t b) {
  appR_ = r; appG_ = g; appB_ = b;
  appDirty_ = true;
  setState(LedState::APP);
}

void Led::tick(uint32_t now_ms) {
  if (!begun_) return;
  // Cap render rate to ~30 Hz — plenty for breathing/blink effects.
  if (now_ms - lastFrameMs_ < 33) return;
  lastFrameMs_ = now_ms;
  render(now_ms);
}

void Led::showRGB(uint8_t r, uint8_t g, uint8_t b) {
  auto* s = static_cast<Adafruit_NeoPixel*>(strip_);
  s->setPixelColor(0, packRGB(r, g, b));
  s->show();
}

void Led::render(uint32_t now_ms) {
  const uint32_t dt = now_ms - stateEnteredMs_;

  switch (state_) {
    case LedState::BOOT: {
      // Dim warm white for ~400 ms, then auto-switch to IDLE so callers don't
      // have to manage the boot -> idle handoff.
      if (dt < 400) {
        showRGB(30, 25, 18);
      } else {
        setState(LedState::IDLE);
      }
      return;
    }
    case LedState::IDLE: {
      // Slow warm-white breathing (period ~4 s).
      float b = breathe(float(dt) / 4000.0f);          // 0..1..0
      uint8_t v = uint8_t(16 + 24.0f * b);             // 16..40
      showRGB(v, uint8_t(v * 0.85f), uint8_t(v * 0.65f));
      return;
    }
    case LedState::CONNECTED: {
      // Green breathing, slightly brighter (the "I'm alive + linked" vibe).
      float b = breathe(float(dt) / 3000.0f);
      uint8_t v = uint8_t(10 + 60.0f * b);
      showRGB(uint8_t(v * 0.15f), v, uint8_t(v * 0.25f));
      return;
    }
    case LedState::DISCONNECTED: {
      // Solid dim red, no animation (clearly "not right").
      showRGB(60, 0, 0);
      return;
    }
    case LedState::LISTENING: {
      // Fast blue pulse (600 ms cycle).
      float b = breathe(float(dt) / 600.0f);
      uint8_t v = uint8_t(25 + 100.0f * b);
      showRGB(uint8_t(v * 0.2f), uint8_t(v * 0.4f), v);
      return;
    }
    case LedState::SPEAKING: {
      // Cyan mouth-movement-ish pulse (400 ms cycle).
      float b = breathe(float(dt) / 400.0f);
      uint8_t v = uint8_t(40 + 90.0f * b);
      showRGB(0, v, v);
      return;
    }
    case LedState::WALKING: {
      // Green breathing faster than CONNECTED (~1 s cycle).
      float b = breathe(float(dt) / 1000.0f);
      uint8_t v = uint8_t(20 + 100.0f * b);
      showRGB(0, v, uint8_t(v * 0.15f));
      return;
    }
    case LedState::TRIPPED: {
      // Fast red flash — 4 Hz.
      bool on = ((dt / 125) & 1) == 0;
      if (on) showRGB(180, 0, 0); else showRGB(0, 0, 0);
      return;
    }
    case LedState::LOW_BATT: {
      // Amber blink, 1 Hz.
      bool on = ((dt / 500) & 1) == 0;
      if (on) showRGB(120, 60, 0); else showRGB(25, 12, 0);
      return;
    }
    case LedState::APP: {
      if (appDirty_ || lastRendered_ != LedState::APP) {
        showRGB(appR_, appG_, appB_);
        appDirty_ = false;
      }
      return;
    }
  }
  lastRendered_ = state_;
}

} // namespace robot
