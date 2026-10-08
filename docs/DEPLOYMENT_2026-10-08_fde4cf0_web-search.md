# Deployment — 2026-10-08 — Local AI ค้นเว็บผ่าน SearXNG (fde4cf0)

## งานที่ deploy
commit `fde4cf0` `feat(chat): web search for local AI via SearXNG + DDG fallback`

1. **โหมดค้นเว็บของแชต** — checkbox "🌐 ค้นเว็บ" ข้าง "📚 ใช้แหล่งความรู้" (default ปิด เพราะคำถามจะออกนอกเครื่อง) เมื่อเปิดแล้วถาม: เซิร์ฟเวอร์ค้นคำถาม → อ่านหน้าเว็บ 3 หน้าแรกพร้อมกัน → ฉีดเป็นบล็อก "[เว็บ: ชื่อเรื่อง] (url)" เข้า context แบบเดียวกับ RAG (โมเดลตอบโดยอ้างอิงแหล่ง) + note "🌐 ค้นเว็บแล้ว N แหล่ง (ผ่าน SearXNG)" + marker "[ใช้ค้นเว็บ]" ในประวัติ ระหว่างรอหน้าเว็บแสดง "🌐 กำลังค้นเว็บและอ่านหน้าเว็บ…"
2. **เครื่องยนต์ค้นหาสองชั้น** — SearXNG บน `127.0.0.1:8888` เป็นตัวหลัก (JSON API) หากล่ม/ไม่มีผลลัพธ์ สลับไป scrape DuckDuckGo Lite อัตโนมัติ ทั้งคู่ล่มจึงตอบจากความรู้โมเดลพร้อม note แจ้ง — แชตไม่มีวันติดเพราะเครื่องยนต์ค้นหา
3. **งบบริบท** — 1,500 ตัวอักษร/แหล่ง รวมไม่เกิน 6,000 ตัวอักษร, โหลดหน้าเว็บ timeout 6 วิ จำกัด 400KB/หน้า รับเฉพาะ html/text, fetch 3 หน้าแรกพร้อมกันด้วย ThreadPoolExecutor

## การติดตั้ง SearXNG บนเซิร์ฟเวอร์ (native ไม่ใช้ Docker)
- user ระบบ `searxng`, โค้ดที่ `/usr/local/searxng/src` (git clone --depth 1), venv `/usr/local/searxng/py`, config `/etc/searxng/`
- รันด้วย uwsgi ของระบบ (`/etc/searxng/searxng.ini`): `plugins=python314`, `module=searx.webapp:app`, `http-socket 127.0.0.1:8888`, 2 processes × 4 threads; systemd unit `searxng.service` (User=searxng, Restart=on-failure, enabled ที่บูต)
- **บทเรียนสำคัญ:** uwsgi ini ต้องมี `enable-threads = true` + `lazy-apps = true` ไม่งั้น ThreadPoolExecutor ของ SearXNG ไม่ทำงานและทุกคำขอค้างเป็นตาย (HTTP 000)
- **เครื่องยนต์:** `keep_only: [bing, yahoo, wikipedia]` + `engines: disabled: false` บังคับเปิดทั้งสามตัว — จากการทดสอบจริงบนเครือข่ายนี้: google (access denied), duckduckgo (CAPTCHA), brave (rate limit), qwant (CAPTCHA) ใช้ไม่ได้ ส่วน **bing + yahoo ตอบทั้งอังกฤษและไทย** และ wikipedia เติม infobox
- กับดักที่เจอ: engine ที่ผ่าน keep_only แล้วสถานะ `enabled` เป็น False (aggregate จึงได้ 0 ผลลัพธ์ ทั้งที่ query รายตัวด้วย `engines=` ได้ผล) — แก้ด้วยการ override `disabled: false` ต่อตัวตามกลไก merge ใน `searx/settings_loader.py`; และผลลัพธ์เปล่าที่เคย cache ไว้ต้องรอ TTL หรือ restart
- DDG Lite (ตัวสำรองของ draw_server) ทดสอบจากเซิร์ฟเวอร์แล้ว: HTTP 200 มี result links ครบ — ใช้ได้จริง

## การตรวจ
- Windows: `python -m unittest discover -s tests` — **127 OK** (+9 เคส: parse JSON/DDG Lite, กรองโฆษณา/unwrap redirect, ลำดับ fallback, ตัดแท็ก+จำกัดความยาว, ฉีด context และ note ในแชต)
- เซิร์ฟเวอร์: ชุดเดียวกัน **127 OK**
- SearXNG จริง: `curl /search?q=อุดรธานี&format=json` → 11 ผลลัพธ์ ไม่มี engine ล่ม; "bangkok" → 10 ผลลัพธ์
- **end-to-end บนเซิร์ฟเวอร์**: `web_research('อุดรธานี สถานที่ท่องเที่ยว')` → engine SearXNG, 5 บล็อก, เนื้อหาไทยจริงจาก travel.trueid.net
- โดเมนจริง: `https://ai.policeshield4.com/` → 200, เห็น `#web-toggle` ใน HTML
- บริการหลัง deploy: `searxng` และ `qwen-draw@ekkaphap` active ทั้งคู่

## วิธี deploy ครั้งนี้
เหมือนครั้งก่อน (แผง Ake Server + deploy_via_panel.mjs) เพิ่มเทคนิค: งานนาน (apt, pip) รันผ่าน nohup + log แล้ว poll เพราะตัวช่วยตัดที่ 150 วิ และไฟล์ config ส่งขึ้นแบบ base64 เลี่ยงปัญหา quoting

## หมายเหตุ
- ยังไม่ได้ทดสอบ `/api/chat` แบบ web:true ผ่านโดเมนจริงด้วยบัญชีจริง (ไม่มี token) — การันตีด้วย web_research บนเซิร์ฟเวอร์ + เทสต์ integration แทน
- ถ้าวันหน้า google/duckduckgo เลิกบล็อก IP นี้ สามารถเพิ่มกลับใน keep_only ได้ทันที
- `searxng` service ใช้ RAM ~100-200MB ไม่แตะ GPU
