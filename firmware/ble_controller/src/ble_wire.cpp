#include "ble_wire.h"

#include <NimBLEDevice.h>
#include <ArduinoJson.h>
#include <string.h>

#include "poses.h"
#include "robot_profile.h"

namespace robot {

// NUS UUIDs — DO NOT CHANGE (see ble_wire.h header comment).
static const char* kSvcNUS    = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* kChrNusRx  = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* kChrNusTx  = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* kChrCfg    = "6E400004-B5A3-F393-E0A9-E50E24DCCA9E";

static NimBLECharacteristic* g_tx_chr  = nullptr;
static NimBLECharacteristic* g_rx_chr  = nullptr;
static NimBLECharacteristic* g_cfg_chr = nullptr;
static NimBLEServer*         g_server  = nullptr;
static volatile bool         g_connected = false;
static WireContext           g_ctx = {};

// --- notify helpers -----------------------------------------------------
static void notifyLine(const String& line) {
  if (!g_connected || !g_tx_chr) return;
  String payload = line;
  if (!payload.endsWith("\n")) payload += "\n";
  g_tx_chr->setValue((uint8_t*)payload.c_str(), payload.length());
  g_tx_chr->notify();
}

static void ack(const char* cmd, bool ok) {
  StaticJsonDocument<96> d;
  d["t"] = "ack"; d["c"] = cmd; d["ok"] = ok;
  String s; serializeJson(d, s); notifyLine(s);
}

static void err(const char* msg) {
  StaticJsonDocument<96> d;
  d["t"] = "err"; d["msg"] = msg;
  String s; serializeJson(d, s); notifyLine(s);
}

// --- dispatch -----------------------------------------------------------
static void dispatchCommand(JsonDocument& doc) {
  // Pin/ID reconfig: {"cfg":{...}}
  if (doc.containsKey("cfg")) {
    JsonObject cfgObj = doc["cfg"].as<JsonObject>();
    CfgPatch patch_;
    if (cfgObj.containsKey("tx"))  { patch_.has_tx  = true; patch_.tx  = cfgObj["tx"].as<uint8_t>(); }
    if (cfgObj.containsKey("rx"))  { patch_.has_rx  = true; patch_.rx  = cfgObj["rx"].as<uint8_t>(); }
    if (cfgObj.containsKey("dir")) { patch_.has_dir = true; patch_.dir = cfgObj["dir"].as<uint8_t>(); }
    if (cfgObj.containsKey("sda")) { patch_.has_sda = true; patch_.sda = cfgObj["sda"].as<uint8_t>(); }
    if (cfgObj.containsKey("scl")) { patch_.has_scl = true; patch_.scl = cfgObj["scl"].as<uint8_t>(); }
    if (cfgObj.containsKey("vbat")){ patch_.has_vbat= true; patch_.vbat= cfgObj["vbat"].as<uint8_t>(); }
    if (cfgObj.containsKey("ids")) {
      patch_.has_ids = true;
      // Seed with the current IDs so a partial array doesn't zero the rest.
      if (g_ctx.cfg) {
        for (uint8_t i = 0; i < N_SERVOS; ++i) patch_.ids[i] = g_ctx.cfg->ids[i];
      }
      JsonArray ids = cfgObj["ids"].as<JsonArray>();
      for (uint8_t i = 0; i < N_SERVOS && i < ids.size(); ++i) {
        patch_.ids[i] = ids[i].as<uint8_t>();
      }
    }
    if (g_ctx.cfg) {
      patch(*g_ctx.cfg, patch_);
      saveToNVS(*g_ctx.cfg);
    }
    ack("cfg", true);
    notifyLine("{\"t\":\"info\",\"msg\":\"cfg saved; reboot to apply pins\"}");
    return;
  }

  const char* c = doc["c"] | "";
  if (!c || !*c) { err("no cmd"); return; }
  if (g_ctx.watchdog) g_ctx.watchdog->touch(millis());

  if (!strcmp(c, "ping")) {
    ack("ping", true);
  } else if (!strcmp(c, "pose")) {
    const char* n = doc["n"] | "";
    uint16_t   d  = doc["d"] | 800;
    if (!*n) { ack("pose", false); return; }
    if (g_ctx.gait) g_ctx.gait->stop();
    bool ok = false;
    if (g_ctx.bus && g_ctx.cfg) ok = applyPose(n, d, *g_ctx.bus, *g_ctx.cfg);
    ack("pose", ok);
  } else if (!strcmp(c, "walk")) {
    bool on = doc["on"] | false;
    uint16_t stride  = doc["stride"] | kDefaultGait.stride;
    uint16_t step_ms = doc["step"]   | kDefaultGait.step_ms;
    if (g_ctx.gait) {
      if (on) g_ctx.gait->start(stride, step_ms);
      else    g_ctx.gait->stop();
    }
    ack("walk", true);
  } else if (!strcmp(c, "stop")) {
    if (g_ctx.gait) g_ctx.gait->stop();
    if (g_ctx.bus && g_ctx.cfg) g_ctx.bus->stopAll(g_ctx.cfg->ids);
    ack("stop", true);
  } else if (!strcmp(c, "jump")) {
    if (g_ctx.gait) g_ctx.gait->stop();
    if (g_ctx.jump && g_ctx.bus && g_ctx.cfg) g_ctx.jump->trigger(*g_ctx.bus, *g_ctx.cfg);
    ack("jump", true);
  } else {
    err("unknown cmd");
  }
}

static void handleCommandLine(const char* line, size_t len) {
  StaticJsonDocument<512> doc;
  DeserializationError e = deserializeJson(doc, line, len);
  if (e) { err("bad json"); return; }
  dispatchCommand(doc);
}

class RxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* chr) override {
    std::string val = chr->getValue();
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
    g_connected = true;
    if (g_ctx.watchdog) g_ctx.watchdog->arm(millis());
  }
  void onDisconnect(NimBLEServer* s) override {
    g_connected = false;
    if (g_ctx.watchdog && g_ctx.gait && g_ctx.bus && g_ctx.cfg) {
      g_ctx.watchdog->trip(*g_ctx.gait, *g_ctx.bus, *g_ctx.cfg);
    }
    NimBLEDevice::startAdvertising();
  }
};

// --- public API ---------------------------------------------------------
void bleBegin(const char* device_name, WireContext ctx) {
  g_ctx = ctx;

  NimBLEDevice::init(device_name);
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  NimBLEDevice::setMTU(185);

  g_server = NimBLEDevice::createServer();
  g_server->setCallbacks(new ServerCallbacks());

  NimBLEService* svc = g_server->createService(kSvcNUS);

  g_rx_chr = svc->createCharacteristic(
      kChrNusRx,
      NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  g_rx_chr->setCallbacks(new RxCallbacks());

  g_tx_chr = svc->createCharacteristic(
      kChrNusTx,
      NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ);

  g_cfg_chr = svc->createCharacteristic(
      kChrCfg,
      NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::READ);
  g_cfg_chr->setCallbacks(new RxCallbacks());  // accepts same JSON

  svc->start();

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(kSvcNUS);
  adv->setScanResponse(true);
  adv->setMinInterval(32);   // 20 ms
  adv->setMaxInterval(64);   // 40 ms
  bool adv_ok = adv->start();

  NimBLEAddress mac = NimBLEDevice::getAddress();
  Serial.printf("[ble_controller] BLE MAC: %s\n", mac.toString().c_str());
  Serial.printf("[ble_controller] adv.start() -> %s\n", adv_ok ? "OK" : "FAIL");
  Serial.printf("[ble_controller] advertising as %s\n", device_name);
}

bool bleConnected() { return g_connected; }

void bleEmitState(uint32_t now_ms) {
  if (!g_connected) return;
  if (!g_ctx.bus || !g_ctx.cfg) return;

  StaticJsonDocument<256> d;
  d["t"] = "state";
  JsonArray p = d.createNestedArray("p");
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    ServoRead r = g_ctx.bus->readPosition(g_ctx.cfg->ids[i]);
    p.add(r.pos);
  }

  ImuSample imu = {};
  if (g_ctx.imu) imu = g_ctx.imu->read6();
  d["v"]   = g_ctx.battery ? g_ctx.battery->readCentiVolts() : 0;
  d["tmp"] = (int16_t)imu.temp_c;
  d["ms"]  = now_ms;
  JsonArray ia = d.createNestedArray("imu");
  ia.add(imu.ax); ia.add(imu.ay); ia.add(imu.az);
  ia.add(imu.gx); ia.add(imu.gy); ia.add(imu.gz);

  String s; serializeJson(d, s);
  notifyLine(s);
}

void bleNotifyInfo(const char* msg) {
  StaticJsonDocument<96> d;
  d["t"] = "info"; d["msg"] = msg;
  String s; serializeJson(d, s); notifyLine(s);
}

} // namespace robot
