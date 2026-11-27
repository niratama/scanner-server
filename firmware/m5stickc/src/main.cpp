#include <Arduino.h>
#include <ArduinoJson.h>
#include <M5Unified.h>
#include <vector>

namespace {

constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t HELLO_INTERVAL_MS = 1500;
constexpr uint32_t HEARTBEAT_TIMEOUT_MS = 15000;
constexpr uint32_t DIM_TIMEOUT_MS = 60000;
constexpr uint32_t PRESET_CONFIRM_HOLD_MS = 700;
constexpr uint32_t POWEROFF_HOLD_MS = 2000;
constexpr uint32_t NOTICE_DURATION_MS = 1800;
constexpr uint32_t VBUS_LOST_POWEROFF_MS = 2000;
constexpr uint8_t BRIGHTNESS_ACTIVE = 110;
constexpr uint8_t BRIGHTNESS_DIM = 0;

enum class DevicePhase {
    Booting,
    Ready,
    ShuttingDown,
};

enum class UiMode {
    Qr = 0,
    Preset = 1,
    Power = 2,
};

struct PresetItem {
    String id;
    String name;
};

DevicePhase phase = DevicePhase::Booting;
UiMode uiMode = UiMode::Qr;

String serverStatus = "idle";
String qrUrl = "";
String serverIp = "";
String activePresetId = "";
String activePresetName = "";
String shutdownReason = "poweroff";
std::vector<PresetItem> presets;
int selectedPresetIdx = 0;

String noticeText = "";
bool noticeOk = true;
uint32_t noticeUntilMs = 0;

uint32_t lastHelloMs = 0;
uint32_t lastRxMs = 0;
uint32_t lastActivityMs = 0;
uint32_t lastAnimMs = 0;
uint8_t animDots = 0;
bool isDimmed = false;
bool needsRedraw = true;

bool btnAHoldTriggered = false;
bool swallowBtnAUntilRelease = false;
uint32_t btnAPressStartMs = 0;
int lastPowerBarPx = -1;

uint32_t vbusLostSinceMs = 0;
String rxBuffer = "";
bool bootStaticDrawn = false;
bool shutdownStaticDrawn = false;
uint32_t rxLineCount = 0;
uint32_t rxParseErrors = 0;

void wakeScreen() {
    lastActivityMs = millis();
    if (isDimmed) {
        M5.Display.setBrightness(BRIGHTNESS_ACTIVE);
        isDimmed = false;
    }
}

void sendCommand(const char* cmd, const char* key = nullptr, const String& value = "") {
    JsonDocument doc;
    doc["cmd"] = cmd;
    if (key != nullptr) {
        doc[key] = value;
    }
    serializeJson(doc, Serial);
    Serial.println();
    Serial.flush();
}

void showNotice(const String& msg, bool ok) {
    noticeText = msg;
    noticeOk = ok;
    noticeUntilMs = millis() + NOTICE_DURATION_MS;
    needsRedraw = true;
}

void syncSelectedPresetIndex() {
    if (presets.empty()) {
        selectedPresetIdx = 0;
        return;
    }
    for (size_t i = 0; i < presets.size(); ++i) {
        if (presets[i].id == activePresetId) {
            selectedPresetIdx = static_cast<int>(i);
            return;
        }
    }
    if (selectedPresetIdx >= static_cast<int>(presets.size())) {
        selectedPresetIdx = 0;
    }
}

void drawCenteredText(const String& text, int y, uint16_t color, uint16_t bg) {
    M5.Display.setTextColor(color, bg);
    M5.Display.setTextDatum(top_center);
    M5.Display.drawString(text, M5.Display.width() / 2, y);
    M5.Display.setTextDatum(top_left);
}

// 現在のフォントで maxWidthPx に収まるよう UTF-8 文字境界で安全に切り詰める
String fitTextToWidth(const String& text, int maxWidthPx) {
    if (M5.Display.textWidth(text) <= maxWidthPx) {
        return text;
    }
    const String ellipsis = "..";
    const int ellipsisW = M5.Display.textWidth(ellipsis);
    String out = "";
    const size_t len = text.length();
    size_t i = 0;
    while (i < len) {
        const uint8_t c = static_cast<uint8_t>(text[i]);
        size_t charLen = 1;
        if ((c & 0xE0) == 0xC0) {
            charLen = 2;
        } else if ((c & 0xF0) == 0xE0) {
            charLen = 3;
        } else if ((c & 0xF8) == 0xF0) {
            charLen = 4;
        }
        if (i + charLen > len) {
            break;
        }
        String candidate = out + text.substring(i, i + charLen);
        if (M5.Display.textWidth(candidate) + ellipsisW > maxWidthPx) {
            break;
        }
        out = candidate;
        i += charLen;
    }
    return out + ellipsis;
}

void drawBootingScreen() {
    const uint16_t bg = M5.Display.color565(14, 24, 46);
    M5.Display.setFont(&fonts::efontJA_12);

    if (!bootStaticDrawn) {
        M5.Display.fillScreen(bg);
        M5.Display.fillRect(0, 0, 80, 20, M5.Display.color565(26, 115, 232));
        drawCenteredText("DR-C125", 4, TFT_WHITE, M5.Display.color565(26, 115, 232));
        drawCenteredText("サーバー", 86, M5.Display.color565(170, 195, 230), bg);
        drawCenteredText("接続待ち", 102, M5.Display.color565(170, 195, 230), bg);
        bootStaticDrawn = true;
    }

    String dots = "";
    for (uint8_t i = 0; i < animDots; ++i) dots += ".";
    M5.Display.fillRect(0, 52, 80, 20, bg);
    drawCenteredText("起動中" + dots, 56, TFT_WHITE, bg);

    if (rxParseErrors > 0) {
        M5.Display.setFont(&fonts::Font0);
        M5.Display.fillRect(0, 144, 80, 14, bg);
        M5.Display.setTextColor(TFT_YELLOW, bg);
        M5.Display.setCursor(4, 146);
        M5.Display.printf("RX:%lu E:%lu", static_cast<unsigned long>(rxLineCount), static_cast<unsigned long>(rxParseErrors));
    }
}

void drawShuttingDownScreen() {
    const uint16_t bg = M5.Display.color565(56, 12, 12);
    M5.Display.setFont(&fonts::efontJA_12);

    if (!shutdownStaticDrawn) {
        M5.Display.fillScreen(bg);
        M5.Display.fillRect(0, 0, 80, 20, M5.Display.color565(197, 34, 31));
        drawCenteredText("POWER", 4, TFT_WHITE, M5.Display.color565(197, 34, 31));
        drawCenteredText("シャット", 46, TFT_WHITE, bg);
        drawCenteredText("電源が切れる", 96, M5.Display.color565(255, 205, 205), bg);
        drawCenteredText("まで待機", 112, M5.Display.color565(255, 205, 205), bg);
        shutdownStaticDrawn = true;
    }

    String dots = "";
    for (uint8_t i = 0; i < animDots; ++i) dots += ".";
    M5.Display.fillRect(0, 60, 80, 18, bg);
    drawCenteredText("ダウン中" + dots, 62, TFT_WHITE, bg);
}

void drawNoticeBannerIfNeeded() {
    if (noticeText.isEmpty() || millis() >= noticeUntilMs) {
        return;
    }
    const uint16_t bg = noticeOk ? M5.Display.color565(19, 115, 51) : M5.Display.color565(197, 34, 31);
    M5.Display.fillRect(0, 140, 80, 20, bg);
    M5.Display.setFont(&fonts::efontJA_10);
    drawCenteredText(fitTextToWidth(noticeText, 76), 145, TFT_WHITE, bg);
}

void drawQrScreen() {
    M5.Display.fillScreen(TFT_BLACK);

    // 上部 80x80: 白背景 + クワイエットゾーン付き QR コード
    M5.Display.fillRect(0, 0, 80, 80, TFT_WHITE);
    if (!qrUrl.isEmpty()) {
        // version=0 で URL 長に応じた最小バージョンを自動選択
        M5.Display.qrcode(qrUrl.c_str(), 4, 4, 72, 0);
    } else {
        M5.Display.setFont(&fonts::efontJA_12);
        drawCenteredText("QR生成中", 32, TFT_BLACK, TFT_WHITE);
    }

    // 下部 80x80: ステータス・プロファイル・IP・操作ヒント
    const bool scanning = (serverStatus == "scanning");
    const bool offline = (serverStatus == "offline");
    uint16_t badgeBg = M5.Display.color565(19, 115, 51);
    const char* badgeText = "待機中";
    if (scanning) {
        badgeBg = M5.Display.color565(26, 115, 232);
        badgeText = "スキャン中";
    } else if (offline) {
        badgeBg = M5.Display.color565(180, 96, 0);
        badgeText = "スキャナOFF";
    }
    M5.Display.fillRoundRect(4, 84, 72, 18, 4, badgeBg);
    M5.Display.setFont(&fonts::efontJA_10);
    drawCenteredText(badgeText, 88, TFT_WHITE, badgeBg);

    M5.Display.setFont(&fonts::efontJA_10);
    const String presetLabel = fitTextToWidth(
        activePresetName.isEmpty() ? "-" : activePresetName,
        76
    );
    drawCenteredText(presetLabel, 108, M5.Display.color565(180, 210, 255), TFT_BLACK);

    if (!serverIp.isEmpty()) {
        M5.Display.setFont(&fonts::TomThumb);
        drawCenteredText(
            fitTextToWidth(serverIp, 76),
            126,
            M5.Display.color565(175, 175, 175),
            TFT_BLACK
        );
    }

    M5.Display.setFont(&fonts::efontJA_10);
    M5.Display.fillRect(0, 142, 80, 18, M5.Display.color565(32, 33, 36));
    drawCenteredText("A:更新 B:切替", 146, M5.Display.color565(220, 220, 220), M5.Display.color565(32, 33, 36));
    drawNoticeBannerIfNeeded();
}

void drawPresetScreen() {
    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setFont(&fonts::efontJA_12);

    const uint16_t headerBg = M5.Display.color565(26, 115, 232);
    M5.Display.fillRect(0, 0, 80, 20, headerBg);
    drawCenteredText("プロファイル", 4, TFT_WHITE, headerBg);

    if (presets.empty()) {
        M5.Display.setFont(&fonts::efontJA_10);
        drawCenteredText("読込中...", 68, TFT_LIGHTGREY, TFT_BLACK);
        return;
    }

    // 候補を最大 5 件ウィンドウ表示
    const int total = static_cast<int>(presets.size());
    const int visible = min(total, 5);
    int start = selectedPresetIdx - 2;
    if (start < 0) start = 0;
    if (start + visible > total) start = max(0, total - visible);

    M5.Display.setFont(&fonts::efontJA_10);
    for (int row = 0; row < visible; ++row) {
        const int idx = start + row;
        const int y = 24 + row * 20;
        const bool isSel = (idx == selectedPresetIdx);
        const bool isActive = (presets[idx].id == activePresetId);

        uint16_t rowBg = TFT_BLACK;
        uint16_t rowFg = M5.Display.color565(200, 200, 200);
        if (isSel) {
            rowBg = M5.Display.color565(35, 78, 145);
            rowFg = TFT_WHITE;
            M5.Display.fillRoundRect(2, y, 76, 18, 3, rowBg);
        }

        M5.Display.setTextColor(rowFg, rowBg);
        M5.Display.setCursor(4, y + 4);
        String prefix = isActive ? "*" : " ";
        M5.Display.print(fitTextToWidth(prefix + presets[idx].name, 72));
    }

    M5.Display.setFont(&fonts::TomThumb);
    char pageBuf[40];
    snprintf(pageBuf, sizeof(pageBuf), "%d/%d (*:ACTIVE)", selectedPresetIdx + 1, total);
    drawCenteredText(String(pageBuf), 129, M5.Display.color565(175, 175, 175), TFT_BLACK);

    M5.Display.setFont(&fonts::efontJA_10);
    M5.Display.fillRect(0, 142, 80, 18, M5.Display.color565(32, 33, 36));
    drawCenteredText("A:次/長押確定", 146, M5.Display.color565(220, 220, 220), M5.Display.color565(32, 33, 36));
    drawNoticeBannerIfNeeded();
}

void drawPowerProgressBar(int fillWidth) {
    M5.Display.drawRect(6, 108, 68, 14, TFT_WHITE);
    M5.Display.fillRect(8, 110, 64, 10, TFT_BLACK);
    if (fillWidth > 0) {
        M5.Display.fillRect(8, 110, min(64, fillWidth), 10, M5.Display.color565(220, 50, 47));
    }
}

void drawPowerScreen() {
    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setFont(&fonts::efontJA_12);

    const uint16_t headerBg = M5.Display.color565(197, 34, 31);
    M5.Display.fillRect(0, 0, 80, 20, headerBg);
    drawCenteredText("電源オフ", 4, TFT_WHITE, headerBg);

    M5.Display.setFont(&fonts::efontJA_10);
    if (serverStatus == "scanning") {
        drawCenteredText("スキャン中", 52, TFT_YELLOW, TFT_BLACK);
        drawCenteredText("終了後に", 72, TFT_LIGHTGREY, TFT_BLACK);
        drawCenteredText("実行できます", 88, TFT_LIGHTGREY, TFT_BLACK);
    } else {
        drawCenteredText("DG-CANPC", 38, TFT_WHITE, TFT_BLACK);
        drawCenteredText("シャットダウン", 54, TFT_WHITE, TFT_BLACK);
        drawCenteredText("BtnA 2秒長押し", 84, M5.Display.color565(255, 180, 180), TFT_BLACK);
        drawPowerProgressBar(0);
    }

    M5.Display.fillRect(0, 142, 80, 18, M5.Display.color565(32, 33, 36));
    drawCenteredText("B:戻る", 146, M5.Display.color565(220, 220, 220), M5.Display.color565(32, 33, 36));
    drawNoticeBannerIfNeeded();
}

void renderScreen() {
    switch (phase) {
        case DevicePhase::Booting:
            shutdownStaticDrawn = false;
            drawBootingScreen();
            break;
        case DevicePhase::ShuttingDown:
            bootStaticDrawn = false;
            drawShuttingDownScreen();
            break;
        case DevicePhase::Ready:
            bootStaticDrawn = false;
            shutdownStaticDrawn = false;
            if (uiMode == UiMode::Qr) {
                drawQrScreen();
            } else if (uiMode == UiMode::Preset) {
                drawPresetScreen();
            } else {
                drawPowerScreen();
            }
            break;
    }
    needsRedraw = false;
}

void handleIncomingJson(const String& line) {
    rxLineCount++;
    JsonDocument doc;
    if (deserializeJson(doc, line)) {
        rxParseErrors++;
        if (phase == DevicePhase::Booting) {
            needsRedraw = true;
        }
        return;
    }

    const char* type = doc["t"] | "";
    lastRxMs = millis();

    if (strcmp(type, "shutdown") == 0) {
        shutdownReason = String(doc["reason"] | "poweroff");
        phase = DevicePhase::ShuttingDown;
        vbusLostSinceMs = 0;
        wakeScreen();
        needsRedraw = true;
        return;
    }

    if (strcmp(type, "notice") == 0) {
        const bool ok = doc["ok"] | true;
        const String msg = String(doc["msg"] | "");
        if (!msg.isEmpty()) {
            showNotice(msg, ok);
        }
        return;
    }

    if (strcmp(type, "state") == 0) {
        const String newStatus = String(doc["st"] | "idle");
        const String newUrl = String(doc["url"] | "");
        const String newIp = String(doc["ip"] | "");
        const String newPresetId = String(doc["p_id"] | "");
        const String newPresetName = String(doc["p_name"] | "");

        if (newStatus == "powering_off") {
            shutdownReason = "poweroff";
            phase = DevicePhase::ShuttingDown;
            vbusLostSinceMs = 0;
            wakeScreen();
            needsRedraw = true;
            return;
        }

        // シャットダウン中だった場合、poweroff ではなく restart なら通常画面へ復帰
        if (phase == DevicePhase::Booting ||
            (phase == DevicePhase::ShuttingDown && shutdownReason != "poweroff")) {
            phase = DevicePhase::Ready;
            uiMode = UiMode::Qr;
            wakeScreen();
            needsRedraw = true;
        }

        if (newStatus != serverStatus || newUrl != qrUrl || newIp != serverIp ||
            newPresetId != activePresetId || newPresetName != activePresetName) {
            serverStatus = newStatus;
            qrUrl = newUrl;
            serverIp = newIp;
            activePresetId = newPresetId;
            activePresetName = newPresetName;
            needsRedraw = true;
        }

        if (doc["presets"].is<JsonArray>()) {
            std::vector<PresetItem> nextPresets;
            for (JsonObject item : doc["presets"].as<JsonArray>()) {
                PresetItem p;
                p.id = String(item["id"] | "");
                p.name = String(item["name"] | "");
                if (!p.id.isEmpty()) {
                    nextPresets.push_back(p);
                }
            }
            bool changed = (nextPresets.size() != presets.size());
            if (!changed) {
                for (size_t i = 0; i < nextPresets.size(); ++i) {
                    if (nextPresets[i].id != presets[i].id ||
                        nextPresets[i].name != presets[i].name) {
                        changed = true;
                        break;
                    }
                }
            }
            if (changed) {
                presets = std::move(nextPresets);
                syncSelectedPresetIndex();
                needsRedraw = true;
            }
        }
    }
}

void pollSerial() {
    while (Serial.available() > 0) {
        const char ch = static_cast<char>(Serial.read());
        if (ch == '\r') continue;
        if (ch == '\n') {
            if (!rxBuffer.isEmpty()) {
                handleIncomingJson(rxBuffer);
                rxBuffer = "";
            }
        } else if (rxBuffer.length() < 2048) {
            rxBuffer += ch;
        } else {
            rxBuffer = "";
        }
    }
}

void handleButtons() {
    if (phase != DevicePhase::Ready) {
        if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) {
            wakeScreen();
        }
        return;
    }

    // 画面スリープ（暗転・非表示）中の最初のボタン押下は画面復帰のみ行い、操作は反映しない
    if (isDimmed) {
        if (M5.BtnA.wasPressed()) {
            wakeScreen();
            swallowBtnAUntilRelease = true;
            return;
        }
        if (M5.BtnB.wasPressed()) {
            wakeScreen();
            return;
        }
    }

    if (swallowBtnAUntilRelease) {
        if (M5.BtnA.wasReleased() || !M5.BtnA.isPressed()) {
            swallowBtnAUntilRelease = false;
        }
        if (M5.BtnB.wasPressed()) {
            wakeScreen();
        }
        return;
    }

    // BtnB: 画面モード切替
    if (M5.BtnB.wasPressed()) {
        wakeScreen();
        if (uiMode == UiMode::Qr) {
            uiMode = UiMode::Preset;
            syncSelectedPresetIndex();
        } else if (uiMode == UiMode::Preset) {
            uiMode = UiMode::Power;
            lastPowerBarPx = -1;
        } else {
            uiMode = UiMode::Qr;
        }
        btnAHoldTriggered = false;
        needsRedraw = true;
        return;
    }

    // BtnA: モードごとのアクション
    if (M5.BtnA.wasPressed()) {
        wakeScreen();
        btnAPressStartMs = millis();
        btnAHoldTriggered = false;
        lastPowerBarPx = -1;
    }

    if (M5.BtnA.isPressed()) {
        const uint32_t heldMs = millis() - btnAPressStartMs;
        if (uiMode == UiMode::Preset && !btnAHoldTriggered && heldMs >= PRESET_CONFIRM_HOLD_MS) {
            btnAHoldTriggered = true;
            if (!presets.empty()) {
                sendCommand("select_preset", "id", presets[selectedPresetIdx].id);
                showNotice("切替要求", true);
            }
        } else if (uiMode == UiMode::Power && serverStatus != "scanning") {
            const int barPx = static_cast<int>((min(heldMs, POWEROFF_HOLD_MS) * 64UL) / POWEROFF_HOLD_MS);
            if (barPx != lastPowerBarPx) {
                lastPowerBarPx = barPx;
                drawPowerProgressBar(barPx);
            }
            if (!btnAHoldTriggered && heldMs >= POWEROFF_HOLD_MS) {
                btnAHoldTriggered = true;
                shutdownReason = "poweroff";
                phase = DevicePhase::ShuttingDown;
                vbusLostSinceMs = 0;
                sendCommand("poweroff");
                needsRedraw = true;
            }
        }
    }

    if (M5.BtnA.wasReleased()) {
        if (!btnAHoldTriggered) {
            if (uiMode == UiMode::Qr) {
                sendCommand("refresh_qr");
                showNotice("QR更新", true);
            } else if (uiMode == UiMode::Preset && !presets.empty()) {
                selectedPresetIdx = (selectedPresetIdx + 1) % static_cast<int>(presets.size());
                needsRedraw = true;
            } else if (uiMode == UiMode::Power && lastPowerBarPx > 0) {
                lastPowerBarPx = 0;
                drawPowerProgressBar(0);
            }
        }
        btnAHoldTriggered = false;
    }
}

void checkAutoPowerOffOnVbusLost() {
    if (phase != DevicePhase::ShuttingDown || shutdownReason != "poweroff") {
        vbusLostSinceMs = 0;
        return;
    }
    // 初代 M5StickC (AXP192): DG-CANPC の USB 給電が切れたらバッテリー消費を防ぐため自動電源断
    if (M5.Power.getType() == m5::Power_Class::pmic_axp192) {
        const float vbus = M5.Power.Axp192.getVBUSVoltage();
        if (vbus < 3.8f) {
            if (vbusLostSinceMs == 0) {
                vbusLostSinceMs = millis();
            } else if (millis() - vbusLostSinceMs >= VBUS_LOST_POWEROFF_MS) {
                M5.Power.powerOff();
            }
        } else {
            vbusLostSinceMs = 0;
        }
    }
}

}  // namespace

void setup() {
    // サーバーからの state JSON (400〜1500B) を取りこぼさないよう、
    // M5.begin() / Serial.begin() より前に RX バッファを 2048B へ拡張する。
    Serial.setRxBufferSize(2048);

    auto cfg = M5.config();
    cfg.serial_baudrate = SERIAL_BAUD;
    M5.begin(cfg);

    M5.Display.setRotation(0);  // 80x160 縦向き
    M5.Display.setBrightness(BRIGHTNESS_ACTIVE);
    M5.Display.setTextWrap(false);

    lastActivityMs = millis();
    lastHelloMs = 0;
    phase = DevicePhase::Booting;
    needsRedraw = true;
    renderScreen();
}

void loop() {
    M5.update();
    pollSerial();
    handleButtons();

    const uint32_t now = millis();

    // 起動中・シャットダウン中のドットアニメーション & hello 送信
    if (phase == DevicePhase::Booting) {
        if (now - lastHelloMs >= HELLO_INTERVAL_MS) {
            lastHelloMs = now;
            sendCommand("hello");
        }
        if (now - lastAnimMs >= 500) {
            lastAnimMs = now;
            animDots = (animDots + 1) % 4;
            needsRedraw = true;
        }
    } else if (phase == DevicePhase::ShuttingDown) {
        if (now - lastAnimMs >= 500) {
            lastAnimMs = now;
            animDots = (animDots + 1) % 4;
            needsRedraw = true;
        }
        checkAutoPowerOffOnVbusLost();
    } else if (phase == DevicePhase::Ready) {
        // サーバーからのハートビートが一定時間途絶えたら接続待ちへ戻す
        if (lastRxMs > 0 && (now - lastRxMs) >= HEARTBEAT_TIMEOUT_MS) {
            phase = DevicePhase::Booting;
            lastHelloMs = 0;
            needsRedraw = true;
        }
        // トースト表示の期限切れで再描画
        if (!noticeText.isEmpty() && now >= noticeUntilMs) {
            noticeText = "";
            needsRedraw = true;
        }
        // 焼き付き防止の輝度低下
        if (!isDimmed && (now - lastActivityMs) >= DIM_TIMEOUT_MS) {
            M5.Display.setBrightness(BRIGHTNESS_DIM);
            isDimmed = true;
        }
    }

    if (needsRedraw) {
        renderScreen();
        pollSerial();
    }

    delay(5);
}
