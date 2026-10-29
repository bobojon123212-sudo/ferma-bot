#!/bin/bash
# Проверка: набирается ли текст с клавиатуры в приложении (настоящие касания и нажатия клавиш).
# $1 = apk, $2 = пакет, $3 = метка, $4 = сколько ждать загрузки (сек)
APK=$1; PKG=$2; L=$3; WAIT=${4:-15}
OUT=tests/out; mkdir -p $OUT
log() { echo "$*" | tee -a $OUT/keyboard-report.txt; }
adb install -r "$APK" >/dev/null
adb shell am force-stop $PKG
adb shell pm clear $PKG >/dev/null
adb shell am start -W -n $PKG/.MainActivity >/dev/null
sleep $WAIT
dump() { adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1; adb shell cat /sdcard/ui.xml; }
adb exec-out screencap -p > $OUT/$L-1-start.png
X=$(dump)
# первое поле ввода (EditText внутри WebView)
B=$(echo "$X" | grep -o 'class="android.widget.EditText"[^>]*bounds="\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]"' | head -1 | grep -o 'bounds="[^"]*"')
if [ -z "$B" ]; then
  # на главном экране офлайн-приложения поля нет — открываем «Добавить корову»
  BTN=$(echo "$X" | grep -oE '(text|content-desc)="[^"]*(Добавить первую корову|Войти по Telegram ID)[^"]*"[^>]*bounds="[^"]*"' | grep -o 'bounds="[^"]*"' | head -1)
  if [ -n "$BTN" ]; then
    N=($(echo $BTN | grep -o '[0-9]\+')); adb shell input tap $(( (N[0]+N[2])/2 )) $(( (N[1]+N[3])/2 )); sleep 3
    X=$(dump)
    B=$(echo "$X" | grep -o 'class="android.widget.EditText"[^>]*bounds="\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]"' | head -1 | grep -o 'bounds="[^"]*"')
  fi
fi
if [ -z "$B" ]; then log "❌ $L: поле ввода не найдено на экране"; echo "$X" > $OUT/$L-ui.xml; exit 0; fi
N=($(echo $B | grep -o '[0-9]\+'))
adb shell input tap $(( (N[0]+N[2])/2 )) $(( (N[1]+N[3])/2 ))
sleep 3
adb exec-out screencap -p > $OUT/$L-2-focused.png
IME=$(adb shell dumpsys input_method | grep -o 'mInputShown=[a-z]*' | head -1)
adb shell input text 6985425925
sleep 2
adb exec-out screencap -p > $OUT/$L-3-typed.png
X=$(dump); echo "$X" > $OUT/$L-ui.xml
if echo "$X" | grep -q 'text="[^"]*6985425925'; then log "✅ $L: текст набирается ($IME)"; else log "❌ $L: текст НЕ набрался ($IME)"; fi
FOCUS=$(echo "$X" | grep -o 'class="android.widget.EditText"[^>]*focused="[a-z]*"' | grep -o 'focused="[a-z]*"' | head -1)
log "   $L: фокус поля: $FOCUS"
