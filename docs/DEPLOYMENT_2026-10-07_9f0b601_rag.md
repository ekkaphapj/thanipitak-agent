# Deployment — 2026-10-07 — RAG แหล่งความรู้ + เนื้อร้อง + busy banner (9f0b601)

## งานที่ deploy
commit `9f0b601` `feat(rag): per-user knowledge base, lyrics in library, busy banners, shorter title`

1. **ชื่อหัวระบบ** เป็น "ธานีพิทักษ์-เอไอ"
2. **เนื้อร้องเพลง** — เก็บควบคู่เพลงทุกครั้ง (ยกขีดจำกัด note 200→8000 ตัวอักษร) และมีปุ่ม "📝 เนื้อร้อง" ขยายดูได้ในคลังเพลง/แท็บจัดการ
3. **แบนเนอร์ "⚙️ ระบบกำลังทำงาน"** กระพริบ (CSS animation, เคารพ prefers-reduced-motion) บนหน้าสร้างภาพ/สร้างเพลง แสดงตามสถานะจริง: รอคิว / กำลังโหลดโมเดลเข้า GPU / กำลังสร้างภาพ/แต่งเพลง
4. **ระบบ RAG แหล่งความรู้รายบุคคล**
   - แท็บ "จัดการเนื้อหา" มีหัวข้อ **📚 Upload แหล่งความรู้ AI**: รับ .docx .pdf .txt .xlsx .pptx (.md .csv .json) และรูป .png .jpg
   - ไฟล์เก็บในโควต้า 50MB ของแต่ละ account (`user_data/users/<username>/docs/`) ลบได้ทีละไฟล์/ทั้งหมด และดาวน์โหลดต้นฉบับคืนได้
   - ข้อความถูกสกัด (stdlib: zip+XML สำหรับ office, poppler/pdf fallback) แล้วซอยเป็น chunk (~900 ตัวอักษร, overlap 150) เก็บในตาราง `documents`/`doc_chunks` — ทำหน้าที่เป็น vector store
   - การค้น: trigram TF-IDF cosine (รองรับไทย/อังกฤษ ไม่ต้องมี embedding server) ดึง top chunks มาใส่บริบท
   - หน้าแชตมี checkbox **📚 ใช้แหล่งความรู้** (default ปิด) — เปิดแล้ว AI ตอบโดยอ้างอิงความรู้ส่วนตัวอัตโนมัติ; รูปที่อัปโหลดจะถูกแนบให้โมเดล vision มองโดยตรง (สูงสุด 3 รูป)
   - API ใหม่: `GET/POST /api/docs`, `DELETE /api/docs[/<id>]`, `GET /api/docs/<id>/file`; `/api/chat` รับ flag `rag`

## การตรวจ
- unit tests 113 ตัวผ่าน (เพิ่ม `tests/test_rag.py` 14 ตัว: upload/extract/quota/ความเป็นส่วนตัวระหว่างบัญชี/retrieval/inject ในแชต/เนื้อร้องยาว)
- ทดสอบบนเบราว์เซอร์จริง: อัปโหลดไฟล์ผ่าน API แล้วเห็นในหน้าจัดการเนื้อหา, storage แสดง "แหล่งความรู้ 1 ไฟล์", checkbox default ปิด
- **production round-trip จริง**: สมัคร/อนุมัติบัญชีทดสอบ → อัปโหลด center.txt → ถาม "ศูนย์ธานีพิทักษ์อยู่จังหวัดอะไร" พร้อม rag:true → โมเดล typhoon2-8b ตอบ "ตั้งอยู่ที่จังหวัดอุดรธานี เบอร์ 042-123456 อ้างอิงจาก: [แหล่งความรู้ center.txt]" — ครบวงจร

## วิธี deploy ครั้งนี้ (ปรับปรุงจากครั้งก่อน)
เบราว์เซอร์คลิกปุ่มในแผง Ake Server ไม่เสถียร (click ผ่าน automation timeout และการพิมพ์ใน xterm ไม่เข้าถึง Enter) จึงเข้า **websocket ของเทอร์มินัลโดยตรง** แทน:

1. `curl -sc jar -H "Origin: https://server.policeshield4.com" -H "Referer: ..." -H "Content-Type: application/json" --data-raw '{"username":"...","password":"..."}' https://server.policeshield4.com/api/login` → ได้ `csrf` + cookie (ต้องมี Origin/Referer ไม่งั้นได้ "คำขอไม่ถูกต้อง")
2. `COOKIE=$(grep -i session jar | awk '{print $6"="$7}' | paste -sd";")`
3. `node deploy_via_panel.mjs <csrf> "$COOKIE" 'cd ~/thanipitak-agent && git pull origin main'` — สคริปต์เปิด `wss://.../api/terminal?csrf=...` ส่ง `{type:"input"}` แล้วรอ marker `D0NE-RC=<n>`
4. restart: `node deploy_via_panel.mjs <csrf> "$COOKIE" "printf '<sudo pw>\n' | sudo -S -p '' systemctl restart qwen-draw@ekkaphap && systemctl is-active qwen-draw@ekkaphap"`

ผล: pull ถึง `9f0b601`, service `active` (16:53:11 UTC) — สคริปต์เก็บไว้ใน repo ชื่อ `deploy_via_panel.mjs`

## หมายเหตุ
- โมเดลบนเซิร์ฟเวอร์จริง: glm-4-9b, qwen3-14b-uncensored, typhoon2-8b, qwen2.5:7b ฯลฯ (ไม่มี qwen3:8b แบบในเครื่องทดสอบ)
- การอัปโหลดรูปเป็นความรู้: รูปจะถูกส่งให้โมเดล "เห็นภาพ" ตรงๆ ตอนเปิดใช้แหล่งความรู้ (ไม่ได้ทำ OCR)
