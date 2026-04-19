#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../android_companion"
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n dev.robot.companion/.MainActivity
