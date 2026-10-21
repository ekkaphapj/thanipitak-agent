# AGENTS.md — คู่มือสำหรับ Codex (และ agent อื่น) ที่ทำงานใน repo นี้

เว็บ AI ภาษาไทย https://ai.policeshield4.com — แชต / สร้างเพลง (YuE2) / สร้างรูป (Qwen-Image, FLUX.2)
โค้ดใน repo นี้: `index.html` (หน้าเว็บทั้งหมด) + `draw_server.py` (เซิร์ฟเวอร์ Python **stdlib เท่านั้น** ไม่มี pip dependency) — รันบนเซิร์ฟเวอร์ `ake-server` ที่ `~/thanipitak-agent`

อ่านก่อนเริ่มงานเสมอ: `docs/HANDOFF_*.md` (ไฟล์ล่าสุด) และ `docs/SYSTEM_OVERVIEW.md`

## กฎเหล็ก

- **ห้าม stop/restart บริการอื่นบนเซิร์ฟเวอร์** ยกเว้น `qwen-draw@ekkaphap` เมื่อ deploy งานจาก repo นี้ — อย่าแตะ `comfyui`, `searxng`, `cloudflared`, Ollama
- **ห้ามบันทึกรหัสผ่าน/token/secret ลง repo หรือ log** (ธรรมเนียมเดิม: อ้างเป็น `<sudo pw>` เฉย ๆ)
- ไม่เพิ่ม dependency ภายนอกในส่วนหลักของ `draw_server.py` — ใช้ Python stdlib เท่านั้น
- เอกสาร/commit message เขียนภาษาไทยตามแบบ `docs/` เดิม

## ก่อนส่งงาน

1. `python3 -m unittest discover -s tests` ต้องผ่านทั้งหมด
2. ถ้าแก้ JS ใน `index.html`: แกะ `<script>` ออกมาตรวจ syntax ด้วย `new Function()` (แนวปฏิบัติเดิมใน tests)

## การ deploy (สรุปย่อ)

commit → push `origin main` → บนเซิร์ฟเวอร์: `cd ~/thanipitak-agent && git pull` → รันเทสต์ → `systemctl restart qwen-draw@ekkaphap` → เช็ค `is-active` + `curl 127.0.0.1:8190` + โดเมน
รายละเอียดเต็มและกับดักของแผง/เซิร์ฟเวอร์: ดู `docs/HANDOFF_*.md` ล่าสุด
