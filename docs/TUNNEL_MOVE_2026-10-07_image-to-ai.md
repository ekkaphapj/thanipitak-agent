# ย้าย URL ใน Cloudflare Tunnel — image → ai (2026-10-07)

ย้ายหน้าเว็บ Thanipitak Agent (ระบบสร้างรูป/เพลง/แชต, พอร์ต 8190) จาก `https://image.policeshield4.com/` มาที่ **`https://ai.policeshield4.com`**

## ข้อมูล tunnel

- Tunnel ID: `77fac5b3-dc81-4bf4-aa11-b7d4a9e11c2c` (remote-managed — ingress อยู่ฝั่ง Cloudflare ไม่ใช่ config.yml บนเซิร์ฟเวอร์)
- Account: `e66266443b7091003cd44219f39df982` · Zone: `policeshield4.com` = `6bc0244d7bf0e6479755958734b1dfa0`
- แก้ผ่าน API: `PUT /accounts/{acc}/cfd_tunnel/{tun}/configurations` (อ่าน config ปัจจุบัน แก้ 2 จุด แล้วเขียนกลับทั้งก้อน)

## สิ่งที่เปลี่ยน

**Ingress (version 9 → ใหม่):**
- `ai.policeshield4.com` → เปลี่ยนจาก `http://127.0.0.1:3100` เป็น `http://127.0.0.1:8190` (เว็บนี้)
- ลบ rule `image.policeshield4.com` → `http://127.0.0.1:8190` ออก
- rule อื่นคงเดิมทุกข้อ: `server`→3210, `thanipitak-ai`→3100, `files`→3923, `ssh`→22, `draw`→8188, catch-all→404, warp-routing ปิด

**DNS (zone policeshield4.com):**
- ลบ CNAME `image` → `77fac5b3-….cfargotunnel.com` (record id `837194cfa1c939f151eecbe322ecce93`)
- CNAME `ai` มีอยู่แล้วและคงเดิม

## ผลกระทบที่ต้องรู้

- **แอป Node บนพอร์ต 3100 (`thanipitak-ai.service`) ไม่เสียทางเข้า** เพราะมี hostname สองตัวมาแต่แรก — ตอนนี้ยังใช้งานได้ที่ `https://thanipitak-ai.policeshield4.com` (ตรวจแล้ว 302 → /ai.html ปกติ) แต่ URL เดิม `ai.policeshield4.com` จะพามาที่เว็บสร้างรูป/เพลง/แชตแทน
- `image.policeshield4.com` เลิก resolve แล้ว (DNS + ingress ถูกถอดทั้งคู่) — bookmark เก่าต้องเปลี่ยน

## ผลตรวจหลังย้าย

- `https://ai.policeshield4.com/` → 200 พร้อมหน้า login (ฟอร์ม username + แท็บจัดการเนื้อหา), `/approve` → 200, `POST /api/register` ว่าง → 400
- `https://thanipitak-ai.policeshield4.com/` → 302 → `/ai.html` ปกติ
- `https://image.policeshield4.com/` → ไม่ resolve แล้ว

## ถ้าต้องการย้อนกลับ

1. เพิ่ม rule `{"hostname": "image.policeshield4.com", "service": "http://127.0.0.1:8190"}` กลับใน ingress และเปลี่ยน `ai` กลับเป็น `http://127.0.0.1:3100`
2. สร้าง CNAME ใหม่: `image` → `77fac5b3-dc81-4bf4-aa11-b7d4a9e11c2c.cfargotunnel.com` (Proxy on)
