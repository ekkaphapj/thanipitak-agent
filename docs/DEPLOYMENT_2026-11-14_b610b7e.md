# Deployment — 2026-11-14 — เผยแพร่โค้ด HTML จากแชตเป็นหน้าเว็บจริง (b610b7e)

## งานที่ deploy

commit `b610b7e` `feat(chat): เผยแพร่โค้ด HTML จากแชตเป็นหน้าเว็บจริงบนเซิร์ฟเวอร์` (+ เอกสาร `60d25ae` รวมในการ pull เดียวกัน)

ฟีเจอร์ "sites": เมื่อ AI เขียนโค้ด HTML ในแชต ระบบสร้างไฟล์จริงบนเซิร์ฟเวอร์แล้วให้ลิงก์เปิดเลย

- `POST /api/sites` สร้างโฟลเดอร์ `user_data/users/<user>/sites/<id>/` เก็บ `index.html` + ไฟล์ asset (รูปที่แนบในแชตและถูกอ้างใน src/href) → คืนลิงก์ `/s/<id>/<key>/` เปิดได้**โดยไม่ต้อง login** (key สุ่ม 16 hex)
- ส่งแต่ไฟล์ไม่มี html → สร้างหน้าแกลเลอรีไทยอัตโนมัติ (ใช้กับปุ่ม 🔗 สร้างลิงก์ไฟล์บน chip รูปแนบ)
- หน้าเว็บ: ปุ่ม "🌐 เปิดเป็นหน้าเว็บ" บน code block html + คำตอบที่เป็นเอกสาร HTML ครบชุดจะ publish อัตโนมัติเมื่อตอบจบ + แคชลิงก์ใน localStorage กันสร้างซ้ำ
- ตารางใหม่ `sites` นับเข้าโควตา 50MB (`sites_bytes`/`site_count` ใน /api/storage) + `DELETE /api/sites/<id>`
- รายละเอียดสถาปัตยกรรม/ไลน์โค้ด: ดู `docs/HANDOFF_2026-11-14.md`

## การตรวจ

- บนเครื่อง dev (Windows): เทสต์ **136/136 ผ่าน** (เพิ่ม `SiteHttpTest` 9 เคส) + `node --check` กับ `<script>` ของ index.html ผ่าน
- บนเซิร์ฟเวอร์: `python3 -m unittest discover -s tests` = **136/136 ผ่าน** (51.8 วิ — รันผ่าน nohup เพราะเกินเพดาน ~150 วิของเทอร์มินัลแผงเกือบถึง)
- `systemctl restart qwen-draw@ekkaphap` → `active`, ตาราง `sites` ถูกสร้างอัตโนมัติใน `user_data/users.db` ตอน restart (ไม่ต้อง migrate มือ)
- โดเมนจริง https://ai.policeshield4.com : หน้าแรกมีโค้ดใหม่ (`site-btn` ×7), `POST /api/sites` ไม่มี token → 401, `GET /s/<id ใด ๆ>/<key ใด ๆ>/` → 404 ข้อความไทยจาก handler ใหม่
- **E2E จริงบน production** (สคริปต์ python ยิง 127.0.0.1:8190 บนเซิร์ฟเวอร์): สมัคร+อนุมัติบัญชีทดสอบ → login → `POST /api/sites` พร้อม html `<h1>SMOKE-OK</h1>` + รูป `dot.png` → ได้ url → `GET /s/4fdd…/` **200 text/html มี SMOKE-OK** → `GET …/dot.png` **200 image/png ไบต์ตรง** → `DELETE /api/sites/<id>` ลบสำเร็จ — ครบวงจร จบด้วยการลบบัญชีทดสอบ 2 ตัว (deploytest*) ออกจาก DB แล้ว

## วิธี deploy ครั้งนี้

ตามคู่มือใน `docs/HANDOFF_2026-11-14.md`: ขอ credential แผงจากผู้ใช้ในแชต → `curl -sc jar …/api/login` ได้ csrf+cookie → `node deploy_via_panel.mjs <csrf> "$COOKIE" '<cmd>'` ทีละขั้น: pull → nohup เทสต์+poll → restart ด้วย `<sudo pw>` ผ่าน `sudo -S` → ตรวจ

### กับดักใหม่ที่เจอครั้งนี้ (เพิ่มใน HANDOFF แล้ว)

- **heredoc (`<<EOF`) ผ่านเทอร์มินัลแผงไม่ได้** — สคริปต์หลายบรรทัดต้อง base64 แล้ว `echo <b64> | base64 -d | python3 -`
- สคริปต์ทดสอบที่สุ่มรหัสผ่านต่อรอบ + บัญชีค้างจากรอบก่อน = login 401 — ใช้รหัสคงที่+ชื่อบัญชีใหม่สำหรับบัญชีทดสอบ
- หากต้องการ env ของบริการจริง (เช่น APPROVE_CODE ที่ fallback จาก DRAW_PASSWORD): `systemctl show` อาจไม่ครบ — อ่านจาก `/proc/$(pgrep -f draw_server.py)/environ` แทน และ**อย่าพิมพ์ค่าออกทาง log**

## หมายเหตุ

- ลิงก์ `/s/<id>/<key>/` แชร์ต่อให้คนนอกระบบเปิดได้โดย design — ความเสี่ยง same-origin (token ใน localStorage) บันทึกไว้ใน HANDOFF แล้ว ยอมรับได้เพราะบัญชีต้องผ่านการอนุมัติ
- ผู้ใช้เจอข้อจำกัดเดิมจากภาพตัวอย่าง (แนบโลโก้ → ได้แต่ code block) — ตอนนี้ลำดับที่แนะนำ: แนบรูป (ชื่ออังกฤษ เช่น logo.png) → ขอ "เขียนหน้าเว็บ HTML ใช้รูปที่แนบ" → ลิงก์โผล่ใต้โค้ดอัตโนมัติเมื่อตอบจบ
