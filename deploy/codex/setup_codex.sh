#!/usr/bin/env bash
# ติดตั้ง OpenAI Codex CLI บน ake-server (รันในชื่อ user ปกติ เช่น ekkaphap)
# รันซ้ำได้ (idempotent) ใช้สำหรับติดตั้งครั้งแรกและอัปเดตเวอร์ชัน
#
# ติดตั้งแบบไม่ใช้ sudo: ลงที่ npm prefix ~/.local ของ user เอง
# (npm global ของเครื่องอยู่ที่ /usr ต้อง sudo — หลีกเลี่ยง sudo npm)
# ใช้: bash setup_codex.sh
set -euo pipefail
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
NPM_PREFIX="$HOME/.local"

log(){ printf '[codex-setup] %s\n' "$*"; }

node --version >/dev/null 2>&1 || { log "ERROR: ไม่พบ node — ต้องมี Node.js 22+"; exit 1; }
log "node $(node --version)"

# 1) ติดตั้ง/อัปเดต codex ที่ ~/.local (ไม่แตะ npm config ถาวรของ user)
log "ติดตั้ง/อัปเดต @openai/codex ที่ $NPM_PREFIX (โหลด ~1-2 นาที)..."
npm install -g --prefix "$NPM_PREFIX" @openai/codex

# 2) ให้เชลล์เจอ ~/.local/bin (เดิม PATH ของเครื่องไม่มี)
export PATH="$NPM_PREFIX/bin:$PATH"
if ! grep -qs '\.local/bin' "$HOME/.bashrc"; then
  printf '\n# Codex CLI (npm user-prefix)\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.bashrc"
  log "เพิ่ม ~/.local/bin เข้า PATH ใน ~/.bashrc แล้ว (มีผลกับเชลล์ใหม่)"
fi

hash -r 2>/dev/null || true
log "ใช้งานได้: $(command -v codex) — $(codex --version)"

# 3) config เริ่มต้น (ไม่ทับของเดิมถ้ามีอยู่แล้ว)
mkdir -p "$CODEX_HOME"
if [ ! -f "$CODEX_HOME/config.toml" ]; then
  cat > "$CODEX_HOME/config.toml" <<'EOF'
# Codex CLI — ake-server (สร้างโดย deploy/codex/setup_codex.sh)
# ถามก่อนทุกครั้งที่จะรันคำสั้ง/แก้ไฟล์ที่ sandbox ไม่อนุญาตเอง
approval_policy = "on-request"
sandbox_mode    = "workspace-write"
# เก็บ token เป็นไฟล์ธรรมดา (~/.codex/auth.json) — เครื่อง headless ไม่มี keyring
cli_auth_credentials_store = "file"
EOF
  log "เขียน $CODEX_HOME/config.toml แล้ว"
else
  log "พบ config.toml อยู่แล้ว — ไม่แตะ"
fi

log "เสร็จแล้ว — ขั้นถัดไป: codex login --device-auth"
