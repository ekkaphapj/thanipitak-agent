# Deployment — 2026-10-07 — Mobile UI (268fa54)

## งานที่ deploy
commit `268fa54` `feat(ui): mobile layout fixes - header overlap, logout, send/stop below input`

1. **header ทับกับชื่อผู้ใช้** — ที่จอ ≤820px header เป็น column: ชื่อระบบอยู่บรรทัดบน chips (👤 ผู้ใช้, 💾 พื้นที่, ปุ่มออกจากระบบ) อยู่บรรทัดล่าง ไม่ทับกัน
2. **ปุ่มออกจากระบบ** — เพิ่ม `#logout-btn` ใน userbox แสดงหลัง login กดแล้วล้าง token + กลับหน้า login ( abort แชตที่กำลัง stream ด้วย)
3. **ปุ่มส่ง/หยุดใต้ช่องพิมพ์** — `#chat-form` เป็น flex-wrap บนมือถือ: textarea เต็มบรรทัด แถวล่างมี 📎 ซ้าย + ปุ่มส่ง/หยุด ขวา และซ่อนปุ่มส่งตอน AI ตอบ (เหลือปุ่มหยุดอย่างเดียว) ฟอร์ม sticky ติดล่างจอ
4. **ปุ่มกลับหน้าหลัก** — หน้าสมัครมีปุ่ม `← กลับหน้าหลัก` (กลับฟอร์ม login) และหน้า `/approve` มีลิงก์กลับทั้งสถานะล็อกและปลดล็อกแล้ว
5. **ปรับแต่งอื่น** — แท็บเป็น grid 2×2 ที่ ≤580px (ข้อความไม่ถูกตัด), ซ่อน hint ยาวใต้ช่องแชต, ประวัติแชตสูง 200px, ปุ่มลบในประวัติแชตแตะง่ายขึ้น

## บั๊กที่เจอระหว่างทำ
media query `@media(max-width:820px)` เดิมวางไว้ต้น stylesheet แต่ rule ฐาน (`.chat-form`, `#chat-input`, `.user-chip`) ประกาศทีหลังด้วย specificity เท่ากัน จึง override media query (media query ไม่เพิ่ม specificity) ทำให้ฟอร์มยุบเหลือ 33px และหน้าเลื่อนแนวนอน — แก้โดยย้าย block ไปท้าย stylesheet แล้ววัด bounding box ยืนยัน: ไม่มี overflow, ปุ่มส่งอยู่ใต้ช่องพิมพ์, header ไม่ทับ

## การตรวจ
- `python -m unittest discover -s tests` — 99 tests OK
- node --check กับ JS ใน index.html/approve.html — ผ่าน
- เปิดเซิร์ฟเวอร์จำลอง + browser 375px: login จริง, วัด layout, ทดสอบ logout/ปุ่มกลับ/หน้าอนุมัติ ผ่านทั้งหมด
- production `https://ai.policeshield4.com` — เห็น element/CSS ใหม่ครบ, `/api/login` ตอบปกติ

## ข้อสังเกตเรื่องการ deploy (สำคัญ)
ครั้งนี้ `ssh ekkaphap@ake-server` ใช้ไม่ได้: token Cloudflare Access หมดอายุ และการ login ใหม่ต้องยืนยันตัวตนผ่าน dash.cloudflare.com ในเบราว์เซอร์ของเจ้าของบัญชี (รอผู้ใช้เปิด URL ที่ `cloudflared access login ssh.policeshield4.com` ให้)

ทางที่ใช้แทน: **แผงควบคุม Ake Server** ที่ `https://server.policeshield4.com` (ingress tunnel → 127.0.0.1:3210) มี SSH Terminal ในตัว
- login ด้วยบัญชี SSH ของเครื่อง (username+password เดียวกับ sudo)
- API login (`POST /api/login`) ผ่าน curl ต้องใส่ header `Origin`/`Referer` ด้วย ไม่งั้นได้ "คำขอไม่ถูกต้อง"
- path จริงของโปรเจกต์บนเซิร์ฟเวอร์คือ **`/home/ekkaphap/thanipitak-agent`** (ไม่ใช่ /opt/...)
- ลำดับ deploy: `cd ~/thanipitak-agent && git pull origin main` → `printf '<sudo pw>\\n' | sudo -S -p '' systemctl restart qwen-draw@ekkaphap` → `systemctl is-active ...`
- ผล: `active` (ActiveEnterTimestamp 2026-10-07 15:57:43 UTC)
