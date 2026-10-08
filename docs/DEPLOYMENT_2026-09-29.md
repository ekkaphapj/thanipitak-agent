# ผลศึกษาโค้ดและตรวจ deployment — 29 กันยายน 2026

ปลายทาง: https://image.policeshield4.com/

เวอร์ชันที่ตรวจ: `main` / `483e78574d9e20207e8b4a4e8298eb63aabf0e1b`

## ผลการซิงก์และสถานะบริการ

- GitHub, checkout บน Windows และ checkout บนเซิร์ฟเวอร์ตรงกันที่ commit ข้างต้น
- รัน `git pull --ff-only origin main` บนเซิร์ฟเวอร์แล้ว ได้ `Already up to date.`
- ปลายทางใช้ checkout `/home/ekkaphap/thanipitak-agent` ผ่าน `qwen-draw@ekkaphap.service` อยู่แล้ว บริการเริ่มทำงานหลัง commit ล่าสุด และการสร้างภาพจริงยืนยันว่าใช้ profile ใหม่
- `qwen-draw@ekkaphap.service`, `comfyui.service` และ `cloudflared.service` เป็น `active`; checkout บนเซิร์ฟเวอร์ไม่มีการแก้ไขค้าง
- ตรวจ SHA-256 ของไฟล์ tracked ทั้งห้าไฟล์ที่เกี่ยวข้องกับโค้ด เอกสาร tests และ service template หลังปรับ line endings เป็น LF แล้วตรงกับเครื่อง Windows
- หน้าเว็บจาก origin ที่ `127.0.0.1:8190` ตรงกับ `index.html`; JavaScript หลักที่ส่งผ่านโดเมนจริงตรงกับ checkout เช่นกัน Cloudflare เพิ่ม analytics script จึงทำให้ hash ของ HTML ทั้งหน้าแตกต่างจาก origin
- เวอร์ชันล่าสุดออนไลน์อยู่ก่อนเริ่มตรวจครั้งนี้ จึงไม่จำเป็นต้อง restart บริการหรือเปลี่ยน Tunnel/DNS เพื่อให้ได้เวอร์ชันนี้

## สิ่งที่เพิ่มจากเวอร์ชัน 28 กันยายน

| Commit | การเปลี่ยนแปลง |
|---|---|
| `a72dce1` | ลด Qwen steps, เพิ่มตัวแปล prompt ไทยผ่าน OpenAI-compatible endpoint, cache ผลแปล และสถานะ translating |
| `a686272` | เปลี่ยน paths ใน systemd template จาก `%h` เป็น `/home/%i` |
| `483e785` | ตรวจจำนวนงานที่รอผ่าน ComfyUI `/queue` และแสดงเวลารวมเมื่อเสร็จ |

### Sampling

- `fast`: 12 steps, CFG 1.0
- `medium`: 20 steps, CFG 1.0
- `quality`: 20 steps, CFG 2.5
- Qwen ยังคงใช้ Euler / simple และ workflow รูปอ้างอิงเดิม
- FLUX.2 [klein] ยังคงใช้ 4 steps

### ตัวแปล prompt

- `enhance_prompt()` ตรวจอักษรไทยก่อนเรียก endpoint; prompt อังกฤษหรือกรณีไม่ตั้งค่า endpoint ผ่านตรง
- แปลใน background worker ก่อนสร้าง graph จึงคืน job id ได้ทันที
- ใช้ system prompt กำหนดให้คงข้อความใน double quotes และรักษาความหมาย; ถ้า endpoint ผิดพลาดหรือผลลัพธ์ไม่ผ่านการตรวจพื้นฐาน จะใช้ prompt เดิม
- ลบ `<think>...</think>` ก่อนใช้ผลแปล และ cache ได้ 256 prompts ในหน่วยความจำ
- `model_catalog()` เพิ่ม `prompt_enhancer`; หน้าเว็บใช้เปิดข้อความแนะนำภาษาไทย
- บริการจริงตั้ง `ENRICHER_URL=http://127.0.0.1:11434/v1/chat/completions`, `ENRICHER_MODEL=typhoon2.5-4b`, `ENRICHER_TIMEOUT=180`
- endpoint ตอบกลับและพบ `typhoon2.5-4b:latest` ที่ติดตั้งแล้ว ค่า model alias และ timeout ของบริการจริงต่างจาก defaults ใน template จึงควรเก็บค่าที่ใช้งานได้เหล่านี้ไว้ในการ deploy ครั้งต่อไป

### คิวและเวลา

- `comfy_jobs_ahead()` อ่าน `/queue` แล้วนับ running jobs และตำแหน่งใน pending queue
- status API เพิ่ม `queue_ahead` เมื่อค้นหาตำแหน่งงานได้
- งานเก็บ `started` เมื่อ ComfyUI รับ prompt และ `finished` เมื่อพบ output
- หน้าเว็บแสดงเวลาจาก `finished - ts` ซึ่งรวมการแปล การรอคิว และการสร้างภาพ
- `started` ยังไม่ได้หมายถึงเวลาที่ GPU เริ่มประมวลผลจริง และสถานะงานยังอยู่ในหน่วยความจำเหมือนเดิม

## ผลทดสอบ

- `python -m unittest discover -s tests -v` ผ่านครบ 14 tests ทั้งเครื่อง Windows (Python 3.14.3) และเซิร์ฟเวอร์ (Python 3.14.4)
- `GET /`, `/api/health`, `/api/models` ผ่านโดเมนจริงได้ HTTP 200
- catalog ระบุ Qwen และ FLUX พร้อมใช้งาน และ `prompt_enhancer: true`
- ใช้ User-Agent แบบ browser ใน HTTP verification; คำขอ Python แบบ default ก่อนหน้านี้ได้รับ HTTP 403

### ทดสอบสร้างภาพจริงผ่าน Tunnel

- Model: `qwen-image-2.1`
- Profile / resolution: `fast` / 512×512
- Seed: `29092026`
- Job id: `a7e5f3059d1f`
- ComfyUI prompt id: `6734bea2-7b1e-49ab-ad65-52e11c90f251`
- การแปลถูกเรียกใช้จริง; งานผ่านสถานะ `translating` → `sampling` → `done`
- ComfyUI history เป็น `success`, `completed: true` และยืนยัน KSampler 12 steps / CFG 1.0
- เวลาฝั่งเซิร์ฟเวอร์ 42.27 วินาที รวมการแปลและรอผล; ตัวเลขนี้เป็นผลทดสอบงานเดียวที่ 512px ไม่ใช่ benchmark ที่ 1024px
- Image API ตอบ HTTP 200, Content-Type `image/png`; ตรวจ PNG signature และ dimensions ได้ 512×512, ขนาด 365,459 bytes
- SHA-256 ของ PNG: `3e794a11bea40d97a0b5f3971040539ed3715acd0cdf1dcaf119bf6deb5678e3`

## ข้อสังเกตที่ยังต้องแก้เพื่อเพิ่มความตรงของ prompt

prompt ไทยที่ทดสอบ:

> แมวสีส้มหนึ่งตัวนั่งอ่านหนังสือบนโต๊ะไม้ในห้องสมุด แสงอบอุ่น ไม่มีตัวหนังสือในภาพ

ผลแปลเก็บ subject และฉากได้ แต่ประโยคสุดท้ายเป็น `No actual books are visible in the image.` ซึ่งแปลคำว่า “ตัวหนังสือ” ผิดเป็น “หนังสือ” และขัดกับคำสั่งให้แมวอ่านหนังสือ

ระบบแปลและสร้างภาพทำงานครบ แต่ตัวแปลยังไม่ได้รับประกัน semantic accuracy หรือการคงข้อความใน quotes ด้วย validation ฝั่งโค้ด ควรเพิ่มชุดตัวอย่างภาษาไทยสำหรับคำกำกวม จำนวน สี ตำแหน่ง และข้อความในภาพ ก่อนปรับ system prompt หรือเปลี่ยนตัวแปล ไม่ได้แก้ logic การแปลในงานตรวจ deployment ครั้งนี้

ผลตรวจนี้ยืนยันการทำงานของ API และไฟล์ภาพ ไม่ได้ประเมินคุณภาพภาพด้วยสายตา หรือ benchmark โหมด medium/quality และรูปอ้างอิง
