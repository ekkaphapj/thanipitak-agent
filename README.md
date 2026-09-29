# Thanipitak Agent — Local AI image generation + webchat

เว็บสองโหมด: สร้างภาพจากข้อความภาษาไทยหรืออังกฤษผ่าน ComfyUI (Qwen-Image-2.1 และ FLUX.2 [klein]) และสนทนาแบบ webchat กับโมเดล local ผ่าน Ollama พร้อมแนบเอกสาร (PDF, DOCX, XLSX, TXT) และรูปภาพ (PNG, JPG) มาถามได้ โครงการนี้แยกจากระบบผู้ช่วยสนทนาธานีพิทักษ์และใช้ Python standard library สำหรับเว็บเซิร์ฟเวอร์

## เริ่มต้น

ต้องมี Python 3.10 ขึ้นไป, ComfyUI ที่เปิด API อยู่ และติดตั้ง custom nodes/โมเดลที่ต้องใช้ครบก่อน จากโฟลเดอร์ ComfyUI ให้ตั้งค่าตำแหน่งแล้วเปิดเว็บเซิร์ฟเวอร์:

```powershell
$env:COMFY_HOME = 'C:\AI\ComfyUI'
$env:COMFY_URL = 'http://127.0.0.1:8188'
$env:DRAW_HOST = '127.0.0.1'
$env:DRAW_PORT = '8190'
python .\draw_server.py
```

เปิด `http://127.0.0.1:8190` การตั้งค่าเริ่มต้นของเซิร์ฟเวอร์ใช้ `~/ComfyUI` และพอร์ต `8190`; ปรับได้ด้วย `COMFY_HOME`, `COMFY_URL`, `COMFY_OUTPUT_DIR`, `COMFY_INPUT_DIR`, `COMFY_MODEL_ROOT`, `DRAW_HOST` และ `DRAW_PORT`.

### แปลคำบรรยายภาษาไทยอัตโนมัติ

Qwen-Image เข้าใจคำบรรยายภาษาอังกฤษได้ดีกว่าภาษาไทยอย่างชัดเจน ถ้าตั้งค่า `ENRICHER_URL` เป็น endpoint แบบ OpenAI-compatible (เช่น Ollama `http://127.0.0.1:11434/v1/chat/completions`) เซิร์ฟเวอร์จะตรวจหาอักษรไทยใน prompt แล้วเรียก LLM แปลเป็นภาษาอังกฤษแบบละเอียดก่อนสร้างภาพ โดยคงข้อความในเครื่องหมายคำพูด `" "` ไว้ตรงตามเดิมเพื่อให้ปรากฏในภาพ ถ้าไม่ตั้งค่าหรือ LLM ล่ม ระบบจะส่ง prompt เดิมต่อทันที งานไม่มีวันค้าง:

- `ENRICHER_URL` — endpoint แปล prompt (ว่าง = ปิดการแปล)
- `ENRICHER_MODEL` — ชื่อโมเดลที่ endpoint ต้องการ (แนะนำโมเดลไทยขนาดเล็ก เช่น Typhoon 4B)
- `ENRICHER_TIMEOUT` — วินาทีที่รอ (ปกติ 30)

### โปรไฟล์ความเร็ว

ทุกโปรไฟล์ใช้ sampler `euler` + scheduler `simple` ตามสูตรทางการของ Qwen-Image: `fast` 12 steps CFG 1.0 · `medium` 20 steps CFG 1.0 · `quality` 20 steps CFG 2.5 (negative prompt มีผลเฉพาะโปรไฟล์นี้)

เว็บเซิร์ฟเวอร์ควรรับการเชื่อมต่อจาก loopback เท่านั้นเมื่อยังไม่มีระบบยืนยันตัวตน หากต้องเปิดผ่านอินเทอร์เน็ต ให้วาง Cloudflare Access หรือระบบยืนยันตัวตนและ reverse proxy ไว้ด้านหน้า

## โมเดล

| รหัส | โมเดล | โปรไฟล์ | รูปอ้างอิง | Negative prompt |
|---|---|---|---|---|
| `qwen-image-2.1` | Qwen-Image-2.1 GGUF Q4 | fast, medium, quality | รองรับ | รองรับ |
| `flux.2-klein-4b` | FLUX.2 [klein] 4B FP8 | standard | ไม่รองรับ | ไม่รองรับ |

ไฟล์โมเดลต้องติดตั้งไว้ใน ComfyUI `models/` ตามหมวด diffusion model, text encoder และ VAE ที่ประกาศใน `MODELS` ของ `draw_server.py` โครงการนี้ไม่รวมไฟล์น้ำหนักโมเดลหรือ ComfyUI custom nodes

## API

- `GET /api/health` และ `GET /api/models`
- `POST /api/generate` รับ `prompt`, `model`, `profile`, `resolution` และตัวเลือกที่โมเดลรองรับ แล้วคืนรหัสงานทันที
- `GET /api/status/<id>` ตรวจสถานะงาน
- `GET /api/image/<id>` ดาวน์โหลด PNG เมื่อเสร็จ

ตัวอย่าง body:

```json
{"prompt":"แมวส้มอ่านหนังสือในห้องสมุด แสงอบอุ่น","model":"qwen-image-2.1","profile":"medium","resolution":768}
```

งานสร้างภาพทำงานเบื้องหลังและให้หน้าเว็บตรวจสถานะเป็นระยะ จึงไม่ต้องเปิดคำขอ HTTP ค้างไว้ระหว่าง ComfyUI สร้างภาพ

## Webchat (Ollama)

แท็บ "สนทนา AI" คุยกับโมเดล local ผ่าน Ollama (ค่าเริ่มต้น `http://127.0.0.1:11434` ปรับด้วย `OLLAMA_URL`):

- `GET /api/chat/models` — โมเดลที่คุยได้ (กรอง embedding ออก) พร้อมป้ายว่ารองรับรูปภาพหรือไม่
- `POST /api/chat` — ส่งประวัติบทสนทนา `{model, messages}` รับคำตอบแบบสตรรีม NDJSON (บรรทัดละ JSON: `user_content`, `notes`, `delta`, `done`, `error`)

ข้อความล่าสุดแนบไฟล์ได้: `images` (base64, สูงสุด 4 รูป — ต้องใช้โมเดลที่รองรับภาพ) และ `docs` (base64 ของ pdf/docx/xlsx/txt/csv/json/md สูงสุด 4 ไฟล์ ไฟล์ละ 10MB) — เซิร์ฟเวอร์สกัดข้อความจากไฟล์ด้วย stdlib (PDF เป็นแบบ best-effort: ฟอนต์ฝังแบบพิเศษเช่นไทยจำนวนมากอ่านไม่ออก ระบบจะแจ้งให้แนบเป็น .txt แทน) แล้วใส่ไว้ในบริบทของบทสนทนาต่อไป

## ติดตั้งเป็น systemd service

ตัวอย่างไฟล์ template อยู่ที่ `deploy/systemd/qwen-draw@.service` สำหรับบัญชี Linux ที่ติดตั้ง ComfyUI ไว้ใน `~/ComfyUI` และ checkout ไว้ที่ `~/thanipitak-agent` หากใช้ path อื่น ให้แก้ WorkingDirectory และ ExecStart ก่อนเปิด service:

```bash
sudo cp deploy/systemd/qwen-draw@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qwen-draw@ekkaphap
```

## ไฟล์ที่ไม่ควร commit

ห้ามเพิ่มไฟล์โมเดล, รูปที่สร้าง, รูปอ้างอิงที่อัปโหลด, token, `.env`, log หรือข้อมูลผู้ใช้เข้า Git
