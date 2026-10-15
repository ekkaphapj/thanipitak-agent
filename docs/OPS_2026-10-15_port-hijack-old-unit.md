# OPS — 2026-10-15 — unit เก่า (`qwen-draw.service`) ยึดพอร์ต 8190 หลังรีบูต

## อาการ

ผู้ใช้เปิด https://ai.policeshield4.com แล้วเจอหน้า "ระบบสร้างรูปภาพ Local AI" รุ่นแรกสุด (สร้างรูปอย่างเดียว) — ไม่มีหน้า login, ไม่มีแท็บ Webchat/เพลง แม้ production เคยยืนยันว่าเป็นเวอร์ชันใหม่แล้วตั้งแต่ 8 ต.ค.

## การวินิจฉัย (จากเครื่องก่อนและผ่านแผง Ake Server)

- หน้าที่โดเมนเสิร์ฟ **byte-for-byte เท่ากับ `index.html` ของ commit `28f5b2b`** (28 ก.ย. 2026 — "Extract local AI image generator") และชุด route ของ API ตรงกับ draw_server.py ยุคนั้นเป๊ะ: `/api/chat`, `/approve`, `/api/tags` → 404 พร้อม 404 body `{"error": "not found"}`; `/ai.html` → 404 ตัดข้อสันนิษฐานว่าเป็นแอป Node พอร์ต 3100 (มันจะตอบ /ai.html ได้)
- `/api/models` ยังตอบรายการโมเดลใหม่ (Qwen-Image-2.1, FLUX.2 klein) — ไม่ขัดแย้ง เพราะ catalog อ่านจากดิสก์
- บนเซิร์ฟเวอร์: `qwen-draw@ekkaphap` = `activating (auto-restart)` ล่มวนด้วย `OSError: [Errno 98] Address already in use`; `ss -tlnp` ชี้ว่าพอร์ต 8190 ถูก `python` **pid 1482** ยึดอยู่ — cwd `/home/ekkaphap/draw-server` (สำเนาเก่าของโปรเจกต์)
- ต้นเหตุ: มี **unit เก่า `/etc/systemd/system/qwen-draw.service`** (ไม่ใช่ template) `ExecStart=/home/ekkaphap/ComfyUI/venv/bin/python /home/ekkaphap/draw-server/draw_server.py`, `WorkingDirectory=/home/ekkaphap/draw-server`, สถานะ **enabled** — ตกค้างจากการติดตั้งรุ่นแรกปลาย ก.ย.
- เครื่องถูกรีบูตวันที่ 15 ต.ค. 08:27:19 (kernel 7.0.0-34 → 7.0.0-38, รออยู่ตั้งแต่ตอนติดตั้ง EAS เช้านั้น) → ตอนบูต unit เก่าเริ่มก่อนและยึด 8190 เสียก่อน ตัวจริง (`qwen-draw@ekkaphap`, WorkingDirectory `~/thanipitak-agent` ที่ checkout `fde4cf0` ถูกต้อง) เลย bind ไม่ได้ตั้งแต่เช้า
- ไม่ใช่แคช: Cloudflare ตอบ `cf-cache-status: DYNAMIC`; เทียบ HTML กับ git history ทุก commit แล้วตรงกับ `28f5b2b` เท่านั้น

## สิ่งที่แก้

```bash
printf '<sudo pw>\n' | sudo -S -p '' systemctl disable --now qwen-draw.service   # หยุด pid เก่า + ถอดจาก multi-user.target.wants
printf '<sudo pw>\n' | sudo -S -p '' systemctl restart qwen-draw@ekkaphap
```

ไม่มีการแก้โค้ด — repo ที่ `~/thanipitak-agent` อยู่ที่ `fde4cf0` สะอาดและตรง origin อยู่แล้ว ปัญหาอยู่ฝั่ง systemd unit ตกค้างล้วน ๆ

## ผลตรวจหลังแก้

- `qwen-draw@ekkaphap` = **active**; พอร์ต 8190 ถือโดย process ของ unit ตัวจริง
- โดเมนจริง `https://ai.policeshield4.com` → 200, 134,987 bytes (เท่าขนาดไฟล์ `index.html` ปัจจุบันเป๊ะ), title "ระบบเอไอธานีพิทักษ์ - THANIPITAK AI", เจอ `login-gate` และครบ 4 แท็บ (`tab-chat`, `tab-music`, `tab-draw`, `tab-manage`)
- `/approve` → 200; `/api/chat`, `/api/login`, `/api/queue`, `/api/models` → 401 (บังคับ login ตามดีไซน์)
- เทสต์บนเซิร์ฟเวอร์: `python3 -m unittest discover -s tests` → **Ran 127 tests — OK**
- บริการอื่นหลังรีบูต: `comfyui`, `searxng`, `cloudflared` = active ทั้งหมด

## ค้างในใจ

- ไฟล์ unit เก่า `/etc/systemd/system/qwen-draw.service` ยังอยู่แต่ **disabled แล้ว** (จะไม่ขึ้นตอนบูต) — ถ้าจะลบทิ้งให้ยืนยันจากเจ้าของก่อน
- โฟลเดอร์เก่า `~/draw-server` (สำเนายุค 28 ก.ย.) ยังอยู่บนดิสก์ ไม่ได้แตะ — ควรเก็บกวาด/ย้าย archive วันหลัง
- ผู้ใช้ที่เปิดหน้าเว็บค้างไว้ตั้งแต่เช้า อาจต้องกด Ctrl+F5 หนึ่งครั้ง

## บทเรียน

หลังเปลี่ยนมาใช้ unit ใหม่ (template `qwen-draw@.service`) ต้อง **disable unit เก่าทิ้งด้วย** ไม่ใช่แค่ติดตั้งตัวใหม่ทับ — unit เก่าที่ยัง enabled จะกลับมาแย่งพอร์ตทันทีที่เครื่องรีบูต และอาการจะดูเหมือน "เว็บถอยเวอร์ชัน" ทั้งที่ checkout ถูกต้อง
