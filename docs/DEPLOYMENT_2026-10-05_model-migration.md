# ย้ายโมเดล AI ไปลง drive 2 — 5 ตุลาคม 2026

ดำเนินการบนเซิร์ฟเวอร์ `ake-server` หลังพบว่า NVMe (`/`, 232GB) ถูกใช้ 81% ขณะที่ SSD ตัวที่สอง (`/srv/thanipitak-files`, 477GB SATA) ว่าง 429GB

## สิ่งที่ย้าย

| ของเดิม | ขนาด | ที่ใหม่ | วิธีเชื่อมกลับ |
|---|---|---|---|
| `~/ComfyUI/models` | 33GB | `/srv/thanipitak-files/ai-models/comfy-models` | symlink ที่ `~/ComfyUI/models` |
| `~/models` (GGUF เก่า 4 ตัว) | 28GB | `/srv/thanipitak-files/ai-models/user-models` | symlink ที่ `~/models` |
| Docker volume `ollama` → `/root/.ollama` | 71GB | `/srv/thanipitak-files/ai-models/ollama` | recreate container `ollama` ด้วย bind mount |

## สิ่งที่เปลี่ยนในระบบ

- **ComfyUI**: หยุด process ที่สตาร์ทมือ (PID 13452 จากเหตุการณ์ 4 ต.ค.) แล้วเปิด `comfyui.service` (unit เดิม ExecStart ตรงกับ flag ของ process มือทุกตัว: `--listen 0.0.0.0 --port 8188 --lowvram --use-sage-attention`) ตอนนี้ `enabled` + `active` — แก้ข้อควรระวังจาก deployment ก่อนหน้า (รีบูตแล้ว ComfyUI จะไม่กลับมาเอง)
- **Ollama**: ค้นพบว่ารันเป็น Docker container (ชื่อ `ollama`, image `ollama/ollama`, `--gpus all`, `--restart unless-stopped`, port 11434) ไม่ใช่ bare process — โมเดลเคยอยู่ใน named volume บน `/var/lib/docker/volumes/ollama` ย้ายข้อมูลด้วย rsync (ตรวจ byte-for-byte: 75,424,077,760 ทั้งสองฝั่ง) แล้ว recreate container ด้วย `-v /srv/thanipitak-files/ai-models/ollama:/root/.ollama` จากนั้นลบ volume เก่าคืนพื้นที่
- ไม่มีการแก้โค้ด draw_server หรือ index.html — ทุก path เดิมยังใช้ได้ผ่าน symlink

## ผลลัพธ์พื้นที่

| จุด | ก่อน | หลัง |
|---|---|---|
| `/` (NVMe 232GB) | 178G ใช้ / เหลือ 43G (**81%**) | 47G ใช้ / เหลือ 174G (**22%**) |
| `/srv/thanipitak-files` | 17G ใช้ / เหลือ 429G | 147G ใช้ / เหลือ 298G (34%) |

## การทดสอบหลังย้าย (ทั้งหมดผ่าน)

- แชต cold-load `qwen3.8-heretic:9b` จาก drive 2 ผ่าน draw_server: โหลด ~105 วิ (heartbeat 7 ครั้ง) ตอบสำเร็จ
- แชตผ่านโดเมนจริง `image.policeshield4.com`: ตอบสำเร็จ
- สร้างรูปจริง qwen-image-2.1 512px fast: done ใน ~48 วิ (โหลด text encoder 21GB จาก drive 2 ครั้งแรก)
- `/api/models`: qwen-image-2.1 และ flux.2-klein-4b `available: true`, `music_available: true`
- `/api/tags` ของ Ollama ใหม่: ครบ 10 โมเดลเท่าเดิม (รวม qwen3-embedding ที่แท็บแชตกรองออก)
- service: `qwen-draw@ekkaphap`, `comfyui`, `docker`, `cloudflared` = active ทั้งหมด, `comfyui` และ `docker` = enabled (ollama container เป็น restart=unless-stopped จึงกลับมาเองหลังบูตผ่าน docker.service)

## ข้อสังเกต

- เวลาโหลดโมเดลครั้งแรกจาก SATA SSD ช้ากว่า NVMe เล็กน้อย (cold load heretic 9B ~105 วิ vs ~60-90 วิเดิม) — ตอน infer รันจาก VRAM ไม่ต่าง
- ไฟล์โมเดลใน `/srv/thanipitak-files/ai-models/ollama` เป็น root-owned (container รัน root) — อย่าแก้ permission เป็น user ธรรมดาโดยไม่จำเป็น
- ห้ามลบ symlink ที่ `~/ComfyUI/models` และ `~/models` — ต้องลบที่โฟลเดอร์จริงบน drive 2 เท่านั้น
