#!/usr/bin/env bash
# ขั้นต่อจาก setup_eas_android.sh: ติดตั้ง SDK packages + NDK + ตั้ง env + ตรวจสอบ
# (licenses ยอมรับแล้วบนเครื่อง)  รันแบบ detached:
#   setsid nohup bash contin_eas.sh >/tmp/eas_contin.log 2>&1 </dev/null &
set -uo pipefail
STATUS=/tmp/eas_contin.status
echo RUNNING > "$STATUS"
log() { echo "=== [$(date '+%F %T')] $* ==="; }
fail() { echo "FAILED:$*" | tee -a "$STATUS" >&2; exit 1; }

SDK="$HOME/Android/Sdk"
export ANDROID_HOME="$SDK"
export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$PATH"
SDKM="$SDK/cmdline-tools/latest/bin/sdkmanager"

log "ยืนยัน licenses ซ้ำ (ถ้าเคยผ่านจะเร็ว)"
yes | "$SDKM" --licenses >/tmp/eas_lic.log 2>&1 || log "WARNING: licenses มีข้อผิดพลาด (ดู /tmp/eas_lic.log) แต่เดินต่อ"

log "ติดตั้ง platform-tools + platforms 35/36 + build-tools 35/36"
"$SDKM" "platform-tools" "platforms;android-35" "platforms;android-36" \
  "build-tools;35.0.0" "build-tools;36.0.0" >/tmp/eas_sdk.log 2>&1 || fail "sdk_packages ดู /tmp/eas_sdk.log"

log "ติดตั้ง NDK 27.1.12297006 (ไฟล์ใหญ่ ใช้เวลานานสุด)"
"$SDKM" "ndk;27.1.12297006" >/tmp/eas_ndk.log 2>&1 || log "WARNING: NDK ไม่สำเร็จ (gradle จะดาวน์โหลดเวอร์ชันที่โปรเจกต์ต้องการเองตอน build แรก) ดู /tmp/eas_ndk.log"

BASHRC="$HOME/.bashrc"
if ! grep -q "ANDROID_HOME" "$BASHRC"; then
  JAVA_HOME_DETECTED="$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")"
  cat >> "$BASHRC" <<EOF

# Android SDK / EAS local build (เพิ่มโดย setup_eas_android.sh)
export ANDROID_HOME="\$HOME/Android/Sdk"
export JAVA_HOME="$JAVA_HOME_DETECTED"
export PATH="\$ANDROID_HOME/cmdline-tools/latest/bin:\$ANDROID_HOME/platform-tools:\$PATH"
EOF
fi

log "ตรวจสอบเวอร์ชัน"
java -version 2>&1 | head -1
node -v
eas --version 2>/dev/null || fail "eas_version"
"$SDKM" --list_installed 2>/dev/null | tail -30
log "เสร็จสมบูรณ์"
echo DONE > "$STATUS"
