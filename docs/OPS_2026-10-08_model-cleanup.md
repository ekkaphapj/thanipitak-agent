# Ops — 2026-10-08 — ล้างโมเดลแชต + ติดตั้ง Qwen3.5-9B

ไม่มีการเปลี่ยนโค้ดใน repo (แก้เฉพาะฝั่งเซิร์ฟเวอร์: โมเดล Ollama + systemd override)

## ลบออก (ตามที่ผู้ใช้แนบภาพระบุ) — คืนพื้นที่ ~35 GB
- `hf.co/bartowski/glm-4-9b-chat-1m-GGUF:Q6_K` (8.3GB)
- `qwen3-14b-uncensored:latest` (9.0GB)
- `typhoon2-8b:latest` (6.6GB)
- `qwen2.5:7b` (4.7GB)
- `qwen3.8-heretic:9b` (6.3GB)

ผ่าน `DELETE /api/delete` ของ Ollama (POST ตอบ 405 — endpoint นี้ต้องใช้ HTTP DELETE จริง) ทุกตัวตอบ 200

## ติดตั้งใหม่
- `qwen3.5:9b` (6.6GB) ผ่าน `POST /api/pull` แบบ nohup+poll
- capabilities: **completion, vision, tools, thinking** — เป็นโมเดลมัลติโมดัลตัวที่สองของระบบ (ข้าง qwen3-vl:4b) และเป็นตัวที่แรงที่สุดที่มี vision
- smoke test: ถามไทยตอบถูก 0.4 วิ (โมเดล warm); หมายเหตุ: เรียกโดยไม่ส่ง `think:false` คำตอบจะว่างเปล่าเพราะโทเคนถูกใช้ใน thinking phase — เว็บของเราส่ง `think:false` ทุกคำขออยู่แล้ว จึงไม่กระทบ

## โมเดลที่เหลือบนเซิร์ฟเวอร์ (7 ตัว)
| โมเดล | ขนาด | บทบาท |
|---|---|---|
| qwen3.5:9b | 6.6GB | แชตหลัก + เห็นภาพ (ใหม่) |
| ministral3-14b-heresy | 8.7GB | แชต (ctx 16k ผ่าน override) |
| gemma4:12b | 7.6GB | แชต |
| qwen3:8b-q6 | 6.7GB | แชต |
| qwen3-vl:4b | 3.3GB | เห็นภาพ (ตัวเล็ก) |
| typhoon2.5-4b | 2.5GB | ENRICHER แปล prompt รูปไทย→อังกฤษ |
| qwen3-embedding:0.6b | 0.6GB | ไม่ถูกใช้ (RAG ใช้ trigram) — เก็บไว้ตามที่ผู้ใช้ไม่ได้แนบลบ |

ดิสก์หลังล้าง: `/` ใช้ 52G/232G (24%) ว่าง 170G

## แก้ systemd ควบคู่
`/etc/systemd/system/qwen-draw@ekkaphap.service.d/override.conf` — ถอด `"qwen3-14b-uncensored":12288` ออกจาก `CHAT_NUM_CTX_OVERRIDES` (โมเดลถูกลบแล้ว) เหลือ `{"ministral3-14b-heresy":16384}` + `daemon-reload` + restart `qwen-draw@ekkaphap` → active, เว็บ 200

## ข้อสังเกต
- qwen3.5:9b ยังไม่มี ctx override — ใช้ default 32768 เหมือนเพื่อนรุ่นเดียวกัน (qwen3:8b-q6 ที่ 6.7GB รัน 32k ได้บนการ์ด 12GB) ถ้าพบว่าช้า/KV ล้นค่อยเพิ่ม override 16384 ใน override.conf
- dropdown โมเดลในเว็บดึงจาก `/api/tags` แบบเรียลไทม์ + cache 5 นาที — โมเดลใหม่จะขึ้นเองหลัง cache หมดอายุหรือรีเฟรชหน้า (เรา restart บริการไปแล้ว cache เริ่มใหม่)
