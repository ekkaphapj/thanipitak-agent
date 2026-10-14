#!/usr/bin/env bash
# ติดตั้งชุดเครื่องมือ EAS local build (Android) บน ake-server (Ubuntu)
# ติดตั้ง: JDK 17, Node.js + eas-cli, Android SDK (cmdline-tools, platform-tools,
#          platforms 35/36, build-tools 35/36, NDK 27.1.12297006) ลงใน ~/Android/Sdk
#
# วิธีรันบนเซิร์ฟเวอร์ (ผ่านเทอร์มินัลแผง ใช้ nohup เพราะคำสั่งถูกตัดที่ ~150 วิ):
#   SUDOPW='<sudo pw>' nohup bash setup_eas_android.sh >/tmp/eas_setup.log 2>&1 &
# ติดตาม:
#   tail -n 20 /tmp/eas_setup.log
#   cat /tmp/eas_setup.status          # RUNNING | DONE | FAILED:<เหตุผล>
#
# สคริปต์รันซ้ำได้ (ข้ามส่วนที่ติดตั้งแล้ว) และไม่บันทึกรหัสผ่านลงดิสก์
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_INTERACTIVE=0
export UCF_FORCE_CONFOLD=1
APT_OPTS="-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

STATUS=/tmp/eas_setup.status
echo RUNNING > "$STATUS"
log() { echo "=== [$(date '+%F %T')] $* ==="; }
fail() { echo "FAILED:$*" | tee -a "$STATUS" >&2; exit 1; }

# ---- sudo ------------------------------------------------------------------
# หมายเหตุ: ต้องส่งรหัสผ่านทุกครั้ง เพราะตอนรันผ่าน nohup ไม่มี tty
# (sudo credential cache แบบ per-tty ใช้ไม่ได้)
if [ "$(id -u)" -eq 0 ]; then
  sd() { "$@"; }
else
  if [ -z "${SUDOPW:-}" ]; then echo "FAILED:NEED_SUDOPW" > "$STATUS"; exit 1; fi
  # ต้องฉีด env ผ่าน sudo ตรง ๆ เพราะ sudo ทำ env_reset ทิ้ง (env ที่ export ไว้ไม่ผ่าน)
  sd() { printf '%s\n' "$SUDOPW" | sudo -S -p '' env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_INTERACTIVE=0 "$@" || fail "sudo:$*"; }
fi

# ---- Phase A: apt packages -------------------------------------------------
log "เก็บงาน dpkg ค้าง (ถ้ามี)"
sd env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a dpkg --configure -a || fail "dpkg_configure"

log "apt-get update"
sd apt-get update -y || fail "apt_update"

log "apt install base packages"
sd apt-get install -y $APT_OPTS curl unzip zip build-essential || fail "apt_base"

if ! command -v java >/dev/null 2>&1; then
  log "install JDK 17 (fallback: 21)"
  sd apt-get install -y $APT_OPTS openjdk-17-jdk || sd apt-get install -y $APT_OPTS openjdk-21-jdk || fail "apt_jdk"
fi

NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
if [ "${NODE_MAJOR:-0}" -lt 20 ]; then
  log "install Node.js (found major=$NODE_MAJOR)"
  sd apt-get install -y $APT_OPTS nodejs npm || fail "apt_node"
fi

if ! command -v eas >/dev/null 2>&1; then
  log "npm install -g eas-cli (รอสักครู่)"
  sd npm install -g eas-cli --no-audit --no-fund || fail "npm_eas_cli"
fi

# ---- Phase B: Android SDK (user-space ที่ ~/Android/Sdk) --------------------
SDK="$HOME/Android/Sdk"
mkdir -p "$SDK/cmdline-tools"
cd /tmp || fail "cd_tmp"

ZIP=cmdline-tools-linux.zip
if [ ! -s "$ZIP" ]; then
  log "ดาวน์โหลด Android cmdline-tools"
  curl -fL --retry 3 -o "$ZIP" \
    https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip || fail "download_cmdline_tools"
fi
if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
  rm -rf "$SDK/cmdline-tools/.extract"
  mkdir -p "$SDK/cmdline-tools/.extract"
  unzip -q "$ZIP" -d "$SDK/cmdline-tools/.extract" || fail "unzip_cmdline_tools"
  rm -rf "$SDK/cmdline-tools/latest"
  mv "$SDK/cmdline-tools/.extract/cmdline-tools" "$SDK/cmdline-tools/latest" || fail "mv_cmdline_tools"
  rm -rf "$SDK/cmdline-tools/.extract"
fi

export ANDROID_HOME="$SDK"
export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$PATH"
SDKM="$SDK/cmdline-tools/latest/bin/sdkmanager"

log "ยอมรับ Android SDK licenses"
# error ของ sdkmanager ต้องเห็น (เคยเจอ fetch remote repo ล้มชั่วขณะ) — retry 1 ครั้ง
yes | "$SDKM" --licenses >/tmp/eas_lic.log 2>&1 \
  || { sleep 5; yes | "$SDKM" --licenses >>/tmp/eas_lic.log 2>&1; } \
  || log "WARNING: licenses ยังไม่ผ่าน (ดู /tmp/eas_lic.log) — ขั้นต่อไปจะพังถ้า licenses ไม่ครบ"

log "ติดตั้ง SDK packages (platform-tools, platforms 35+36, build-tools 35+36)"
"$SDKM" "platform-tools" "platforms;android-35" "platforms;android-36" \
  "build-tools;35.0.0" "build-tools;36.0.0" >/tmp/eas_sdk.log 2>&1 || fail "sdk_packages ดู /tmp/eas_sdk.log"

log "ติดตั้ง NDK 27.1.12297006 (ใหญ่ ~2GB ใช้เวลานานสุด)"
"$SDKM" "ndk;27.1.12297006" >/tmp/eas_ndk.log 2>&1 || log "WARNING: ติดตั้ง NDK ไม่สำเร็จ (gradle จะดาวน์โหลดเองตอน build แรก) ดู /tmp/eas_ndk.log"

# ---- Phase C: env + ตรวจสอบ -------------------------------------------------
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
"$SDKM" --list_installed 2>/dev/null | grep -v '^ ' | grep -v '^\[' || true

log "เสร็จสมบูรณ์"
echo DONE > "$STATUS"
