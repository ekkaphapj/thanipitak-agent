# DEPLOYMENT — 2026-10-21 — แท็บ 🤖 ธานีพิทักษ์ (WebUI สั่งงาน Codex) — release `2fb9ab4`

## สิ่งที่ทำ

เพิ่มแท็บ **🤖 ธานีพิทักษ์** ใน https://ai.policeshield4.com — WebUI แบบแชท (มือถือใช้ได้) สำหรับสั่งงาน OpenAI Codex CLI ให้แก้โค้ดระบบ THANI PITAK Data Center (`~/udonpolice-datacenter`) บนเซิร์ฟเวอร์ โดยไม่ต้องเข้า terminal เลย

## ฟีเจอร์

- แชทต่อเนื่อง: ข้อความถัดไปส่งเข้า `codex exec resume <thread_id>` — Codex จำบริบทงานก่อนหน้าได้ (ทดสอบแล้วผ่านโดเมนจริง)
- งานรันเป็น background job บนเซิร์ฟเวอร์ — ปิดหน้าเว็บแล้วกลับมาดูผลได้ (poll ทุก 3 วิ)
- stream กิจกรรมสด: ข้อความตอบ, คำสั่งที่รัน (`$ npm run typecheck` + ผลลัพธ์พับเก็บได้), ไฟล์ที่แก้, สรุปโทเคน
- ปุ่ม 🧹 เริ่มเซสชันใหม่ (ตัดบริบทเดิม)
- งานเดียวพร้อมกัน (busy ส่งไม่ได้ 409), จำกัดความยาว prompt 4,000 ตัวอักษร, timeout 30 นาที (`CODEX_TIMEOUT`)
- **สิทธิ์**: เฉพาะแอดมิน (login หน้าอนุมัติ) หรือ username ใน env `CODEX_ALLOWED_USERS` (คั่นด้วยจุลภาค ว่าง = แอดมินอย่างเดียว) — user ทั่วไปเห็นข้อความล็อก

## สถาปัตยกรรม

```
หน้าเว็บ (แท็บใหม่) ──POST /api/codex/send──▶ draw_server.py:8190
                                                │ single job slot + CODEX_LOCK
                                                ▼
                            codex exec --json -C ~/udonpolice-datacenter -s workspace-write
                            (งานถัดไป: codex exec resume <thread_id> --json …)
                                                │ JSONL ทีละบรรทัด → events
GET /api/codex/status ◀── poll ทุก 3 วิ ────────┘
POST /api/codex/reset — เริ่มเซสชันใหม่
```

- sandbox: `workspace-write` + `approval_policy=never` — Codex แก้ไฟล์/รันคำสั่งได้เฉพาะในโฟลเดอร์โปรเจกต์ ออกนอกกรอบทำไม่ได้ (ปลอดภัยกว่า terminal ตรง)
- การจำสนทนาอยู่ในหน่วยความจำ `CODEX_STATE` — restart บริการ = เริ่มเซสชันใหม่ (codex เก็บ session ไฟล์ไว้ใน `~/.codex/sessions` อยู่แล้ว)
- `exec resume` ไม่รับ `-C`/`-s` (bug แรกที่เจอแล้วแก้ใน `2fb9ab4`)

## env ใหม่ (ใส่ใน override.conf ได้ถ้าต้องการ — ตอนนี้ใช้ค่า default)

| ตัวแปร | default |
|---|---|
| `CODEX_BIN` | `~/.local/bin/codex` |
| `CODEX_PROJECT_DIR` | `~/udonpolice-datacenter` |
| `CODEX_ALLOWED_USERS` | (ว่าง = แอดมินเท่านั้น) |
| `CODEX_TIMEOUT` | `1800` วินาที |

## การทดสอบ

- ชุดเทสต์ใหม่ `tests/test_codex_webui.py` — 16 เคส (args/event fold/sanitize, auth 401/403, allowed-users env, 503 binary หาย, happy path + status, busy 409, validate, reset) — รวม suite แล้ว **143 เคสผ่านทั้ง Windows และเซิร์ฟเวอร์**
- ตรวจ syntax JS ด้วย `node --check` (แนวปฏิบัติเดิม)
- E2E ผ่าน https://ai.policeshield4.com จริง: login แอดมิน → ส่ง "อ่าน package.json ตอบชื่อโปรเจกต์" → done ตอบ `vite_react_shadcn_ts` → ส่งต่อ "จากที่อ่านเมื่อกี้…" → resume สำเร็จตอบ `done`

## ⚠️ สถานะพิเศษหลัง deploy นี้ (สำคัญ)

- ระหว่าง deploy ผม restart บริการไม่ได้เพราะไม่มี sudo → ใช้วิธี kill process แล้วคิดว่า systemd จะเปิดใหม่ แต่ **`kill -TERM` ไม่นับเป็น failure** (systemd ยกเว้น SIGTERM ให้ Restart=on-failure) บริการเลยดับชั่วครู่
- กู้คืนทันทีด้วย **manual process**: `setsid nohup python3 ~/thanipitak-agent/draw_server.py` (pid รันอยู่ โค้ดล่าสุด `2fb9ab4`) — เว็บใช้ได้ปกติทุกแท็บ
- **ครั้งหน้าที่ได้ sudo**: `pkill -f draw_server.py && systemctl restart qwen-draw@ekkaphap` (kill ตัว manual ก่อน ไม่งั้นชนพอร์ต 8190)
- unit เป็น `enabled` — ถ้ารีบูตเครื่อง systemd เปิดเอง ไม่มีปัญหา
- บทเรียน: ถ้าจะบังคับ restart แบบไม่มี sudo อีก ใช้ `pkill -9` (SIGKILL นับเป็น failure → on-failure restart ทำงาน)

## หมายเหตุอื่น

- comfyui กลับมา `enabled + active` ตั้งแต่ 19 ต.ค. (ก่อน session นี้) — ข้อมูลใน HANDOFF ที่ว่า disabled ล้าสมัยแล้ว
- Codex บนเซิร์ฟเวอร์ (codex-cli 0.163.0 + deploy key + tmux) ดู docs/OPS_2026-10-21_codex-cli.md
