# scripts/termux — DEPRECATED for normal use

These Python scripts (`phone_daemon.py`, `phone_planner.py`, `phone_goal_keeper.py`,
`phone_vision.py`, `phone_voice.py`, `phone_wire.py`, `phone_intent.py`) were the
original phone-brain stack that ran inside Termux on the Pixel 6. They have
been **ported to a single native Android app** at `android_companion/`
(package `dev.robot.companion`).

## Use the native app instead

```
cd android_companion
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n dev.robot.companion/.MainActivity
```

The native app:
- Does BLE → robot bridging (was `phone_wire.py`).
- Runs the DeepInfra planner + GoalKeeper in-process (was `phone_planner.py`
  + `phone_goal_keeper.py`).
- Captures camera frames via CameraX (was `termux-camera-photo` in
  `phone_vision.py`).
- Uses Android SpeechRecognizer for voice (was `whisper-cli` in
  `phone_voice.py`).
- Uses Android TextToSpeech for TTS (was `termux-tts-speak`).
- Needs no Termux install.

## When you might still want these scripts

- Ad-hoc bench testing of a single subsystem from an adb shell.
- Debugging planner prompt changes quickly without waiting for a Gradle
  build.
- Running the daemon on a second phone that can't or shouldn't install
  the native APK.

They still work — they just are no longer the recommended runtime.

## Smoke tests (still valid)

```
python3 phone_planner.py 'jump once and say hi'
python3 phone_goal_keeper.py          # hermetic stub test
python3 phone_vision.py --query 'a person' 'a laptop'
```
