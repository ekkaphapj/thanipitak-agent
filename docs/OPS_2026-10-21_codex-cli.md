# OPS — 2026-10-21 — ติดตั้ง OpenAI Codex CLI บน ake-server

## สิ่งที่ทำ

ติดตั้ง OpenAI Codex CLI บนเซิร์ฟเวอร์ `ake-server` และ login ด้วยบัญชี ChatGPT (ChatGPT plan) เพื่อใช้แก้โค้ด/สั่งงานบนเซิร์ฟเวอร์ได้จากทุกที่ผ่านเทอร์มินัลเว็บของแผงหรือ SSH

## สิ่งที่ติดตั้ง (ผู้ใช้ ekkaphap — ไม่ใช้ sudo และไม่ใช่ systemd service)

| รายการ | เวอร์ชัน | ที่อยู่ |
|---|---|---|
| codex-cli (npm `@openai/codex`) | 0.163.0 | `~/.local/bin/codex` (npm user-prefix `~/.local`) |
| config | — | `~/.codex/config.toml` |
| token บัญชี ChatGPT | — | `~/.codex/auth.json` (โหมด 600, ได้จาก device-code login) |
| PATH `~/.local/bin` | — | เพิ่มใน `~/.bashrc` แล้ว |

- ติดตั้งด้วย `npm install -g --prefix ~/.local @openai/codex` — เลี่ยง `sudo npm` (npm global ของเครื่องอยู่ที่ `/usr`) และไม่แต๊ะ npm config ถาวรของ user
- config: `approval_policy = "on-request"` + `sandbox_mode = "workspace-write"` + `cli_auth_credentials_store = "file"` (เครื่อง headless ไม่มี keyring)
- login ด้วย `codex login --device-auth` ใน tmux — เปิด https://auth.openai.com/codex/device ใส่ one-time code จากมือถือ/เครื่องอื่นได้
- ตรวจแล้ว: `codex login status` = "Logged in using ChatGPT", `codex exec` ทดสอบใน repo ตอบสำเร็จ
- สคริปต์ติดตั้ง/อัปเดตรันซ้ำได้: `deploy/codex/setup_codex.sh`

## วิธีใช้ (สั่งงานออนไลน์จากทุกที่)

เข้าเทอร์มินัลเซิร์ฟเวอร์ด้วยวิธีใดวิธีหนึ่ง:
- แผง Ake Server (https://server.policeshield4.com) → Terminal, หรือ
- SSH จากเครื่อง Windows: `ssh -o ProxyCommand="cloudflared access ssh --hostname %h" ekkaphap@ssh.policeshield4.com` (ต้อง login Cloudflare Access ใน browser ก่อน — token อยู่ได้นานหลายวัน)

แล้วใช้ tmux session ถาวรชื่อ `codex` (สร้างไว้ให้แล้ว เปิด TUI รออยู่ที่ `~/thanipitak-agent`):
- เข้าใช้: `tmux attach -t codex`
- ออกแต่ให้งานเดินต่อ: กด `Ctrl+b` แล้ว `d`
- ถ้า reboot/เซสชันตาย สร้างใหม่: `tmux new-session -A -s codex -c ~/thanipitak-agent` แล้วพิมพ์ `codex`

งานยาวที่ไม่ต้องนั่งดู (non-interactive): `cd ~/thanipitak-agent && codex exec "งาน..."` ใน tmux/nohup ก็ได้

## วิธีอัปเดตเวอร์ชัน

```bash
bash deploy/codex/setup_codex.sh   # ใน repo บนเซิร์ฟเวอร์ — npm install ซ้ำ = อัปเดต
```

## การตรวจสอบ

```bash
codex --version && codex login status
```

## กับดักที่เจอตอนติดตั้ง (อย่าให้เจอซ้ำ)

- **ส่งคำสั่งซับซ้อนผ่าน SSH จาก Git Bash บน Windows แล้วเครื่องหมาย `"` ใน argv โดน mangle** (MSYS→ssh.exe→remote shell) — อาการ: bash มองทั้งบรรทัดเป็นชื่อคำสั่งเดียว "No such file or directory" **แก้: ส่งสคริปต์ผ่าน stdin เสมอ** `ssh ... 'bash -s' <<'EOF' ... EOF` (สคริปต์ใน stdin ไม่ผ่าน argv เลย)
- **`codex exec` ต้องรันใน git repo** นอก repo จะขึ้น "Not inside a trusted directory" — ให้ cd เข้า repo ก่อน และปิด stdin ด้วย `</dev/null` ไม่งั้นรออ่าน input จาก pipe
- device-code login เป็น beta — ต้องเปิด "Device code authorization" ที่ ChatGPT → Settings → Security ก่อน (บัญชีนี้เปิดแล้ว)

## หมายเหตุ

- Codex ไม่กิน RAM/GPU ตอน idle (เป็น CLI รันเมื่อใช้) ไม่กระทบ qwen-draw/comfyui/searxng/ollama/cloudflared — ตรวจสถานะบริการหลังติดตั้ง: active ครบ
- ใช้สิทธิ์จากแพ็กเกจ ChatGPT (ไม่ใช่ API key — ไม่มีค่าใช้จ่ายราย token แยก) quota ตามแผน
- โควตา/บัญชี: `codex login status` ดูได้; logout: `codex logout`
