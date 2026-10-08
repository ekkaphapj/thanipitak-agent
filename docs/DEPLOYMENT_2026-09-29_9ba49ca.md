# Commit / push / deploy — 9ba49ca

ดำเนินการวันที่ 29 กันยายน 2026 สำหรับ https://image.policeshield4.com/

## เวอร์ชันและการติดตั้ง

- Commit `9ba49cac7cab615ea67025adee6abd35e1da9d9e`: Fix PDF attachments and reliable Ollama chat streaming
- Commit โค้ด 4 ไฟล์: README, backend, หน้าเว็บ และ tests; push ไป GitHub `origin/main` สำเร็จ
- เซิร์ฟเวอร์ `ake-server` checkout `/home/ekkaphap/thanipitak-agent` มี tracked tree สะอาดก่อน deploy และ pull แบบ fast-forward จาก `de58fb1` สำเร็จ
- Tests ผ่าน 43 รายการบน Windows และบนเซิร์ฟเวอร์ Linux
- ตรวจแล้วมี `/usr/bin/pdftoppm` และ `/usr/bin/pdftotext` บนเซิร์ฟเวอร์
- บริการ `qwen-draw@ekkaphap.service` เริ่ม process ใหม่ PID `80781` เวลา `2026-09-29 14:31:17 UTC` หรือ 21:31:17 น. ประเทศไทย
- ก่อนเริ่ม process ใหม่ตรวจว่า thread ของเว็บเหลือ main thread เดียว และ ComfyUI ไม่มีงานกำลังรันหรือรอคิว
- `sudo -n systemctl restart` ไม่ผ่านเพราะต้องยืนยันตัวตน จึงส่ง SIGUSR1 ให้ process เว็บของบัญชีเดียวกัน หลังยืนยันชื่อโปรแกรม, UID และนโยบาย `Restart=on-failure` ของ systemd บริการเริ่มใหม่อัตโนมัติและ health ผ่าน
- หลัง restart บริการเว็บ, ComfyUI และ cloudflared เป็น active ทั้งหมด

## ผลตรวจผ่านโดเมนจริง

- `GET /`, `/api/health`, `/api/chat/models`: HTTP 200
- HTML มี `Cache-Control: no-cache`; Cloudflare cache status เป็น `DYNAMIC`
- JavaScript หลักที่เว็บเสิร์ฟตรงกับ checkout บนเครื่องพัฒนา
- SHA-256 ของ backend, HTML, README และ tests หลัง normalize line endings ตรงระหว่างเครื่องพัฒนากับเซิร์ฟเวอร์
- ส่ง Excel ที่ไม่มี workbook ให้ chat API: ได้ HTTP 400 พร้อมชื่อไฟล์และคำอธิบาย แทน connection หลุด

## ทดสอบ PDF และ Ollama จริง

- ส่ง PDF จำลองที่ถูกต้อง 6 หน้า แต่ละหน้ามีข้อความ CHECK-PAGE-1 ถึง CHECK-PAGE-6 ผ่าน HTTPS ไป `/api/chat`
- Model `qwen3-vl:4b`; content ว่าง เพื่อทดสอบการส่ง PDF อย่างเดียว
- HTTP 200, Content-Type `application/x-ndjson; charset=utf-8`
- Backend แจ้งว่าแปลงภาพครบ 6 หน้า จากนั้นส่ง freeing/loading, heartbeat, deltas, usage และ done
- Context 32768, prompt 6179 tokens, output 736 tokens, คำตอบ 881 ตัวอักษร
- จบสำเร็จใน 29.55 วินาที รวมเตรียมโมเดลและตอบ เป็น smoke test ครั้งเดียวของเอกสารจำลองขนาดเล็ก ไม่ใช่ benchmark PDF ทั่วไป

การตรวจรอบนี้ยืนยัน deployment, การสตรีมผ่าน Tunnel, Poppler และ vision chat จริง ไม่ได้ทดสอบ clipboard ด้วย browser หรือสร้างภาพ Qwen ใหม่ เพราะ graph สร้างภาพไม่ได้เปลี่ยนใน commit นี้
