// PhoneWalker BLE controller firmware
// ------------------------------------
// Board:  Waveshare ESP32-S3 Zero
// Servos: 4x Feetech STS3032 on half-duplex UART bus (default IDs 1..4)
// IMU:    MPU-6050 on I2C
//
// Wire protocol (BLE GATT, Nordic UART Service compatible):
//   RX char (write, no-response or with-response): newline-delimited JSON
//     {"c":"ping"}                                 -> ack
//     {"c":"pose","n":"neutral|lean_left|lean_right|bow_front","d":<ms>}
//     {"c":"walk","on":true,"stride":150,"step":400}
//     {"c":"stop"}
//     {"c":"jump"}
//     {"cfg":{"tx":N,"rx":N,"dir":N,"sda":N,"scl":N,"ids":[1,2,3,4]}}  (persists to NVS)
//   TX char (notify): newline-delimited JSON at 10 Hz
//     {"t":"state","p":[p0,p1,p2,p3],"v":<voltage*10>,"tmp":<temp_c>,"ms":<uptime>,
//      "imu":[ax,ay,az,gx,gy,gz]}
//     {"t":"ack","c":"<cmd>","ok":true|false}
//     {"t":"err","msg":"..."}
//
// Safety:
//   - Power-on pose: neutral. No autonomous motion.
//   - BLE disconnect  -> immediate stop (fail-safe).
//   - 10 s command timeout -> hold last pose, no autonomous motion.
//   - NVS-backed pin config via {"cfg":{...}} so a bad default can be fixed
//     over the air without re-flashing.
//
// See firmware/ble_controller/README.md for restore instructions if this
// firmware misbehaves on your board.

#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>
#include <NimBLEDevice.h>
#include <ArduinoJson.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <SCServo.h>

// ------------------------------------------------------------------
// Default pin map (OVERRIDABLE via NVS and BLE cfg characteristic).
// These are a best-guess for the Waveshare ESP32-S3 Zero + generic
// Feetech STS servo driver board. Verify against your schematic.
// If the servos don't move, send a cfg JSON over BLE to correct the pins
// (see README.md) — no re-flash needed.
// ------------------------------------------------------------------
static constexpr uint8_t DEFAULT_SERVO_TX  = 43;  // ESP32 -> bus TX (half-duplex)
static constexpr uint8_t DEFAULT_SERVO_RX  = 44;  // bus -> ESP32 RX
static constexpr uint8_t DEFAULT_SERVO_DIR = 42;  // DE/RE direction pin (if used)
static constexpr uint8_t DEFAULT_I2C_SDA   =  8;
static constexpr uint8_t DEFAULT_I2C_SCL   =  9;
static constexpr uint8_t DEFAULT_ADC_VBAT  =  3;  // divider on GPIO3 (guess; adjust in cfg)

static constexpr uint8_t  N_SERVOS          = 4;
static constexpr uint32_t STATE_RATE_HZ     = 10;
static constexpr uint32_t CMD_TIMEOUT_MS    = 10 * 1000;
static constexpr int      NEUTRAL_POS       = 2048;    // STS3032 mid-range (12-bit)
static constexpr uint16_t SERVO_BAUD        = 1000000; // 1 Mbit half-duplex default

// ------------------------------------------------------------------
// BLE GATT: Nordic UART Service (NUS) + a config characteristic.
// ------------------------------------------------------------------
static const char* BLE_NAME       = "PhoneWalker-BLE";
static const char* SVC_NUS        = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* CHR_NUS_RX     = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"; // write
static const char* CHR_NUS_TX     = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"; // notify
static const char* CHR_CFG        = "6E400004-B5A3-F393-E0A9-E50E24DCCA9E"; // write/read

// ------------------------------------------------------------------
// Persistent config (NVS-backed, overridable by BLE cfg writes).
// ------------------------------------------------------------------
struct Config {
  uint8_t servo_tx;
  uint8_t servo_rx;
  uint8_t servo_dir;
  uint8_t i2c_sda;
  uint8_t i2c_scl;
  uint8_t adc_vbat;
  uint8_t ids[N_SERVOS];
};

static Config g_cfg = {
  DEFAULT_SERVO_TX, DEFAULT_SERVO_RX, DEFAULT_SERVO_DIR,
  DEFAULT_I2C_SDA,  DEFAULT_I2C_SCL,  DEFAULT_ADC_VBAT,
  {1, 2, 3, 4}
};

static Preferences g_prefs;

static void loadCfg() {
  if (!g_prefs.begin("bleconf", true)) return;
  g_cfg.servo_tx  = g_prefs.getUChar("tx",  g_cfg.servo_tx);
  g_cfg.servo_rx  = g_prefs.getUChar("rx",  g_cfg.servo_rx);
  g_cfg.servo_dir = g_prefs.getUChar("dir", g_cfg.servo_dir);
  g_cfg.i2c_sda   = g_prefs.getUChar("sda", g_cfg.i2c_sda);
  g_cfg.i2c_scl   = g_prefs.getUChar("scl", g_cfg.i2c_scl);
  g_cfg.adc_vbat  = g_prefs.getUChar("vbat",g_cfg.adc_vbat);
  uint8_t ids_buf[N_SERVOS] = {1, 2, 3, 4};
  g_prefs.getBytes("ids", ids_buf, N_SERVOS);
  for (uint8_t i = 0; i < N_SERVOS; ++i) g_cfg.ids[i] = ids_buf[i];
  g_prefs.end();
}

static void saveCfg() {
  if (!g_prefs.begin("bleconf", false)) return;
  g_prefs.putUChar("tx",  g_cfg.servo_tx);
  g_prefs.putUChar("rx",  g_cfg.servo_rx);
  g_prefs.putUChar("dir", g_cfg.servo_dir);
  g_prefs.putUChar("sda", g_cfg.i2c_sda);
  g_prefs.putUChar("scl", g_cfg.i2c_scl);
  g_prefs.putUChar("vbat",g_cfg.adc_vbat);
  g_prefs.putBytes("ids", g_cfg.ids, N_SERVOS);
  g_prefs.end();
}

// ------------------------------------------------------------------
// Servo subsystem (Feetech STS3032, half-duplex, via Serial1).
// ------------------------------------------------------------------
static SMS_STS g_sms;
static int16_t g_last_pos[N_SERVOS] = {NEUTRAL_POS, NEUTRAL_POS, NEUTRAL_POS, NEUTRAL_POS};

static void servosBegin() {
  // Feetech library needs the HardwareSerial started before use.
  Serial1.begin(SERVO_BAUD, SERIAL_8N1, g_cfg.servo_rx, g_cfg.servo_tx);
  g_sms.pSerial = &Serial1;
  // Some driver boards have an explicit DE/RE pin for direction. Hold it
  // high for TX-enable; the SCServo lib handles timing, but if your board
  // needs per-byte flipping you'll see garbled replies — reconfigure dir.
  if (g_cfg.servo_dir != 255) {
    pinMode(g_cfg.servo_dir, OUTPUT);
    digitalWrite(g_cfg.servo_dir, HIGH);
  }
  delay(20);
}

static void servosNeutral(uint16_t dur_ms = 800) {
  uint16_t speed = 0;  // 0 = use accel/default; good enough for a "go home".
  uint8_t  acc   = 50;
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    g_sms.WritePosEx(g_cfg.ids[i], NEUTRAL_POS, speed, acc);
    g_last_pos[i] = NEUTRAL_POS;
  }
  (void)dur_ms;
}

static void servosPose(const char* name, uint16_t dur_ms) {
  // Positions derived from the live wire protocol log in
  // docs/FIRMWARE_IMU_AUDIT.md §"Verified command surface".
  int16_t targets[N_SERVOS];
  if      (!strcmp(name, "neutral"))    { for (uint8_t i=0;i<N_SERVOS;++i) targets[i] = 2048; }
  else if (!strcmp(name, "lean_left"))  { for (uint8_t i=0;i<N_SERVOS;++i) targets[i] = 1700; }
  else if (!strcmp(name, "lean_right")) { for (uint8_t i=0;i<N_SERVOS;++i) targets[i] = 2400; }
  else if (!strcmp(name, "bow_front"))  { targets[0]=1501; targets[1]=2501; targets[2]=2597; targets[3]=1595; }
  else { return; }
  // Map duration -> speed on STS3032: speed is in steps/s. Full travel ~4096
  // steps; target roughly (|delta| / dur_ms) * 1000. Bounded.
  uint16_t speed = dur_ms > 0 ? (uint16_t)constrain(4096000UL / (uint32_t)dur_ms, 100UL, 3400UL) : 1000;
  uint8_t  acc   = 50;
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    g_sms.WritePosEx(g_cfg.ids[i], targets[i], speed, acc);
    g_last_pos[i] = targets[i];
  }
}

static void servosStop() {
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    // Writing current position with zero speed latches the servo in place.
    g_sms.WritePosEx(g_cfg.ids[i], g_last_pos[i], 0, 50);
  }
}

static void servosReadPositions(int16_t out[N_SERVOS]) {
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    int pos = g_sms.ReadPos(g_cfg.ids[i]);
    out[i] = (pos >= 0) ? (int16_t)pos : g_last_pos[i];
  }
}

// ------------------------------------------------------------------
// IMU (MPU-6050 on I2C).
// ------------------------------------------------------------------
static Adafruit_MPU6050 g_mpu;
static bool g_imu_ok = false;

static void imuBegin() {
  Wire.begin(g_cfg.i2c_sda, g_cfg.i2c_scl);
  Wire.setClock(400000);
  g_imu_ok = g_mpu.begin();
  if (g_imu_ok) {
    g_mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
    g_mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    g_mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
  }
}

static void imuRead(float& ax, float& ay, float& az,
                    float& gx, float& gy, float& gz) {
  if (!g_imu_ok) { ax=ay=az=gx=gy=gz=0.0f; return; }
  sensors_event_t a, g, t;
  if (!g_mpu.getEvent(&a, &g, &t)) { ax=ay=az=gx=gy=gz=0.0f; return; }
  // Scale accel m/s^2 -> g so daemon sees ~0.98 at rest (matches live fw).
  ax = a.acceleration.x / 9.80665f;
  ay = a.acceleration.y / 9.80665f;
  az = a.acceleration.z / 9.80665f;
  // Gyro: rad/s -> deg/s.
  gx = g.gyro.x * 57.2957795f;
  gy = g.gyro.y * 57.2957795f;
  gz = g.gyro.z * 57.2957795f;
}

// ------------------------------------------------------------------
// Battery voltage (ADC) — rough read through a 2:1 divider on adc_vbat.
// ------------------------------------------------------------------
static uint16_t readVoltageTimes10() {
  if (g_cfg.adc_vbat == 255) return 0;
  uint32_t raw = analogReadMilliVolts(g_cfg.adc_vbat);
  // Divider ratio 2:1 by default; user can recalibrate externally.
  uint32_t mv_battery = raw * 2;
  return (uint16_t)(mv_battery / 100);  // centi-volts: 7.2 V -> 72
}

static int16_t readTempCelsius() {
  // MPU-6050 has an internal temp sensor (in the sensors_event_t path).
  if (!g_imu_ok) return 0;
  sensors_event_t a, g, t;
  if (!g_mpu.getEvent(&a, &g, &t)) return 0;
  return (int16_t)t.temperature;
}

// ------------------------------------------------------------------
// Walking state machine (very light stub — iterate on real hardware).
// ------------------------------------------------------------------
static bool     g_walking      = false;
static uint16_t g_walk_stride  = 150;
static uint16_t g_walk_step_ms = 400;
static uint32_t g_last_step_ms = 0;
static uint8_t  g_walk_phase   = 0;

static void walkTick(uint32_t now_ms) {
  if (!g_walking) return;
  if ((now_ms - g_last_step_ms) < g_walk_step_ms) return;
  g_last_step_ms = now_ms;
  // Alternate left/right half-step. This is a placeholder gait; the real
  // per-leg phase angles need to be ported from the original firmware or
  // tuned on the bench.
  int16_t off = (int16_t)g_walk_stride;
  if ((g_walk_phase & 1) == 0) {
    g_sms.WritePosEx(g_cfg.ids[0], NEUTRAL_POS - off, 2000, 100);
    g_sms.WritePosEx(g_cfg.ids[2], NEUTRAL_POS + off, 2000, 100);
    g_sms.WritePosEx(g_cfg.ids[1], NEUTRAL_POS,       2000, 100);
    g_sms.WritePosEx(g_cfg.ids[3], NEUTRAL_POS,       2000, 100);
  } else {
    g_sms.WritePosEx(g_cfg.ids[0], NEUTRAL_POS,       2000, 100);
    g_sms.WritePosEx(g_cfg.ids[2], NEUTRAL_POS,       2000, 100);
    g_sms.WritePosEx(g_cfg.ids[1], NEUTRAL_POS + off, 2000, 100);
    g_sms.WritePosEx(g_cfg.ids[3], NEUTRAL_POS - off, 2000, 100);
  }
  g_walk_phase++;
}

static void jumpOnce() {
  // "Crouch then extend" quick two-step. Tune on hardware.
  for (uint8_t i = 0; i < N_SERVOS; ++i) g_sms.WritePosEx(g_cfg.ids[i], 1700, 3500, 150);
  delay(150);
  for (uint8_t i = 0; i < N_SERVOS; ++i) g_sms.WritePosEx(g_cfg.ids[i], 2400, 3500, 150);
  delay(150);
  for (uint8_t i = 0; i < N_SERVOS; ++i) g_sms.WritePosEx(g_cfg.ids[i], NEUTRAL_POS, 2000, 100);
}

// ------------------------------------------------------------------
// BLE glue.
// ------------------------------------------------------------------
static NimBLECharacteristic* g_tx_chr  = nullptr;
static NimBLECharacteristic* g_rx_chr  = nullptr;
static NimBLECharacteristic* g_cfg_chr = nullptr;
static NimBLEServer*         g_server  = nullptr;
static volatile bool         g_connected = false;
static volatile uint32_t     g_last_cmd_ms = 0;

static void bleNotify(const String& line) {
  if (!g_connected || !g_tx_chr) return;
  String payload = line;
  if (!payload.endsWith("\n")) payload += "\n";
  g_tx_chr->setValue((uint8_t*)payload.c_str(), payload.length());
  g_tx_chr->notify();
}

static void bleAck(const char* cmd, bool ok) {
  StaticJsonDocument<96> d;
  d["t"] = "ack"; d["c"] = cmd; d["ok"] = ok;
  String s; serializeJson(d, s); bleNotify(s);
}

static void bleErr(const char* msg) {
  StaticJsonDocument<96> d;
  d["t"] = "err"; d["msg"] = msg;
  String s; serializeJson(d, s); bleNotify(s);
}

static void handleCommandLine(const char* line, size_t len) {
  StaticJsonDocument<512> doc;
  DeserializationError e = deserializeJson(doc, line, len);
  if (e) { bleErr("bad json"); return; }

  // Pin/ID reconfig: {"cfg":{...}}
  if (doc.containsKey("cfg")) {
    JsonObject cfg = doc["cfg"].as<JsonObject>();
    if (cfg.containsKey("tx"))  g_cfg.servo_tx  = cfg["tx"].as<uint8_t>();
    if (cfg.containsKey("rx"))  g_cfg.servo_rx  = cfg["rx"].as<uint8_t>();
    if (cfg.containsKey("dir")) g_cfg.servo_dir = cfg["dir"].as<uint8_t>();
    if (cfg.containsKey("sda")) g_cfg.i2c_sda   = cfg["sda"].as<uint8_t>();
    if (cfg.containsKey("scl")) g_cfg.i2c_scl   = cfg["scl"].as<uint8_t>();
    if (cfg.containsKey("vbat"))g_cfg.adc_vbat  = cfg["vbat"].as<uint8_t>();
    if (cfg.containsKey("ids")) {
      JsonArray ids = cfg["ids"].as<JsonArray>();
      for (uint8_t i = 0; i < N_SERVOS && i < ids.size(); ++i) g_cfg.ids[i] = ids[i].as<uint8_t>();
    }
    saveCfg();
    bleAck("cfg", true);
    bleNotify("{\"t\":\"info\",\"msg\":\"cfg saved; reboot to apply pins\"}");
    return;
  }

  const char* c = doc["c"] | "";
  if (!c || !*c) { bleErr("no cmd"); return; }
  g_last_cmd_ms = millis();

  if (!strcmp(c, "ping")) {
    bleAck("ping", true);
  } else if (!strcmp(c, "pose")) {
    const char* n = doc["n"] | "";
    uint16_t d = doc["d"] | 800;
    if (!*n) { bleAck("pose", false); return; }
    g_walking = false;
    servosPose(n, d);
    bleAck("pose", true);
  } else if (!strcmp(c, "walk")) {
    bool on = doc["on"] | false;
    g_walk_stride  = doc["stride"] | 150;
    g_walk_step_ms = doc["step"]   | 400;
    g_walking      = on;
    bleAck("walk", true);
  } else if (!strcmp(c, "stop")) {
    g_walking = false;
    servosStop();
    bleAck("stop", true);
  } else if (!strcmp(c, "jump")) {
    g_walking = false;
    jumpOnce();
    bleAck("jump", true);
  } else {
    bleErr("unknown cmd");
  }
}

class RxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* chr) override {
    std::string val = chr->getValue();
    // Accept newline-delimited JSON; split on '\n' in case the client batches.
    size_t start = 0;
    for (size_t i = 0; i <= val.size(); ++i) {
      if (i == val.size() || val[i] == '\n') {
        if (i > start) handleCommandLine(val.data() + start, i - start);
        start = i + 1;
      }
    }
  }
};

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s) override {
    g_connected   = true;
    g_last_cmd_ms = millis();
  }
  void onDisconnect(NimBLEServer* s) override {
    g_connected = false;
    // Fail-safe: stop any autonomous motion the moment the controller drops.
    g_walking = false;
    servosStop();
    NimBLEDevice::startAdvertising();
  }
};

// ------------------------------------------------------------------
// Setup / loop.
// ------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(50);
  Serial.println("[ble_controller] boot");

  loadCfg();
  servosBegin();
  imuBegin();
  servosNeutral();  // Power-on pose.

  NimBLEDevice::init(BLE_NAME);
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  NimBLEDevice::setMTU(185);  // Fits a state packet comfortably.

  g_server = NimBLEDevice::createServer();
  g_server->setCallbacks(new ServerCallbacks());

  NimBLEService* svc = g_server->createService(SVC_NUS);

  g_rx_chr = svc->createCharacteristic(
      CHR_NUS_RX,
      NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  g_rx_chr->setCallbacks(new RxCallbacks());

  g_tx_chr = svc->createCharacteristic(
      CHR_NUS_TX,
      NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ);

  g_cfg_chr = svc->createCharacteristic(
      CHR_CFG,
      NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::READ);
  g_cfg_chr->setCallbacks(new RxCallbacks());  // Accepts the same {"cfg":{...}} JSON.

  svc->start();

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SVC_NUS);
  adv->setScanResponse(true);
  // Keep advertising interval short so scanners catch it within a few hundred ms.
  adv->setMinInterval(32);   // 32 * 0.625 ms = 20 ms
  adv->setMaxInterval(64);   // 40 ms
  bool adv_ok = adv->start();

  // Print the BLE MAC so the operator can grep scan output.
  NimBLEAddress mac = NimBLEDevice::getAddress();
  Serial.printf("[ble_controller] BLE MAC: %s\n", mac.toString().c_str());
  Serial.printf("[ble_controller] adv.start() -> %s\n", adv_ok ? "OK" : "FAIL");
  Serial.println("[ble_controller] advertising as PhoneWalker-BLE");
}

void loop() {
  static uint32_t last_state_ms = 0;
  const uint32_t now = millis();

  // 10 Hz state packet stream, only while a central is connected.
  if (g_connected && (now - last_state_ms) >= (1000 / STATE_RATE_HZ)) {
    last_state_ms = now;

    int16_t pos[N_SERVOS];
    servosReadPositions(pos);
    float ax, ay, az, gx, gy, gz;
    imuRead(ax, ay, az, gx, gy, gz);

    StaticJsonDocument<256> d;
    d["t"]   = "state";
    JsonArray p = d.createNestedArray("p");
    for (uint8_t i = 0; i < N_SERVOS; ++i) p.add(pos[i]);
    d["v"]   = readVoltageTimes10();
    d["tmp"] = readTempCelsius();
    d["ms"]  = now;
    JsonArray imu = d.createNestedArray("imu");
    imu.add(ax); imu.add(ay); imu.add(az);
    imu.add(gx); imu.add(gy); imu.add(gz);
    String s; serializeJson(d, s);
    bleNotify(s);
  }

  // 10 s command timeout -> hold last pose (don't move autonomously).
  if (g_walking && (now - g_last_cmd_ms) > CMD_TIMEOUT_MS) {
    g_walking = false;
    servosStop();
    bleNotify("{\"t\":\"info\",\"msg\":\"cmd timeout; holding pose\"}");
  }

  walkTick(now);
  delay(2);
}
