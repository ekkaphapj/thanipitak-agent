# ผลศึกษาโค้ดอัปเดต Thanipitak Agent

ศึกษาวันที่ 29 กันยายน 2026 จาก `main` commit `196f7e6bf673d32386ca424c94dfd88d150ee3cb` ซึ่งตรงกับ remote `main` ขณะตรวจ เปรียบเทียบกับ snapshot ก่อนหน้า `483e78574d9e20207e8b4a4e8298eb63aabf0e1b`

รายงานนี้เป็นการอ่านโค้ดและทดสอบในเครื่องพัฒนา ข้อค้นพบด้านล่างยังไม่ได้แก้ใน application และไม่ได้ยืนยันสถานะ deployment หรือความเร็วบน GPU จริงในการศึกษาครั้งนี้

## 1. สิ่งที่เพิ่มมา

| Commit | การเปลี่ยนแปลง |
|---|---|
| `950ddf5` | เพิ่มแท็บสนทนา AI ผ่าน Ollama พร้อมแนบรูปและเอกสาร |
| `f643322` | ส่ง heartbeat ระหว่างรอโหลดโมเดลหรือรอโทเคนแรก |
| `d40b61c` | ขอ ComfyUI คืน VRAM และโหลดโมเดลแชตก่อนตอบ |
| `2accbd3` | เพิ่มปุ่มคัดลอกคำตอบและ code block |
| `196f7e6` | เพิ่มวิธีคัดลอกสำรองด้วย `execCommand` เมื่อ Clipboard API ใช้ไม่ได้ |

ไฟล์ที่เปลี่ยน: `draw_server.py`, `index.html`, `tests/test_draw_server.py`, `README.md` รวม 786 บรรทัดเพิ่มและ 7 บรรทัดลบ ส่วน graph สร้างภาพและค่า profile ไม่ได้เปลี่ยนในช่วง commit นี้

## 2. โครงสร้างการทำงานปัจจุบัน

- หน้าเว็บ HTML/JavaScript มีสองแท็บ: สร้างภาพและสนทนา AI
- Python `ThreadingHTTPServer` ให้บริการทั้งสองแท็บ โดยยังใช้ standard library
- งานภาพ: browser ส่งงาน → Python แปล prompt ไทยเมื่อเปิด enhancer → ส่ง graph ไป ComfyUI → browser ตรวจสถานะและรับ PNG
- งานแชต: browser ส่งประวัติและไฟล์ → Python สกัดข้อความเอกสาร → ตรวจโมเดลและความสามารถ vision → ขอคืน VRAM จาก ComfyUI → warm up Ollama → ส่งคำตอบเป็น NDJSON
- ประวัติสนทนาเก็บในตัวแปร JavaScript ของหน้าเว็บ รีเฟรชแล้วหาย เอกสารที่สกัดเป็นข้อความถูกเก็บในบริบทของบทสนทนาต่อไป

### API ใหม่

| Endpoint | หน้าที่ |
|---|---|
| `GET /api/chat/models` | อ่านโมเดลจาก Ollama และระบุว่าโมเดลรับรูปได้หรือไม่ |
| `POST /api/chat` | รับ `{model, messages}` แล้วส่ง NDJSON: `user_content`, `notes`, `stage`, `beat`, `delta`, `done`, `error` |

ค่าเริ่มต้น `OLLAMA_URL=http://127.0.0.1:11434`, `CHAT_KEEP_ALIVE=30m` เป็น environment variables ส่วนขีดจำกัดในโค้ดคือ body 40 MiB, เอกสารไฟล์ละ 10 MiB, รูปสูงสุด 4 รูป, เอกสารสูงสุด 4 ไฟล์, ประวัติสูงสุด 80 messages, heartbeat ทุก 15 วินาที, warm up timeout 300 วินาที และ chat socket timeout 600 วินาที

หน้าเว็บย่อรูปเป็น JPEG ด้านยาวไม่เกิน 1536 px และรับไฟล์รูปต้นฉบับไม่เกิน 15 MiB ข้อความเอกสารที่ส่งเข้าโมเดลถูกตัดเหลือ 60,000 ตัวอักษรต่อไฟล์

## 3. จุดอ่านโค้ดสำหรับแก้ไขต่อ

| ส่วน | ตำแหน่งเริ่มต้น |
|---|---|
| อ่านโมเดลและ vision | `draw_server.py:218` — `ollama_tags`, `chat_models` |
| สกัด DOCX/XLSX/PDF | `draw_server.py:235` — `_xml_text` และ extractor ต่าง ๆ |
| คืน VRAM / warm up | `draw_server.py:307` — `free_comfy_vram`, `warm_chat_model` |
| ตรวจ history / แนบเอกสาร | `draw_server.py:333` — `build_chat_messages` |
| สตรีมคำตอบ / heartbeat | `draw_server.py:542` — `Handler._stream_chat` |
| รับคำขอแชต | `draw_server.py:726` — `Handler._handle_chat` |
| แท็บ / โมเดล / ไฟล์แนบ | `index.html:298` เป็นต้นไป |
| แสดง Markdown แบบพื้นฐาน | `index.html:357` — `mdLite` |
| ส่งแชต / คัดลอก / หยุด | `index.html:379` เป็นต้นไป |

## 4. ข้อค้นพบที่ควรแก้ก่อน

### 4.1 XLSX อ่านข้อมูลไม่ครบ และบางไฟล์ทำให้คำขอล้มเหลว

`extract_xlsx` อ่านเฉพาะ `xl/sharedStrings.xml` จึงไม่ได้อ่านตัวเลข ค่าเซลล์ ลำดับแถว/คอลัมน์ สูตร หรือข้อความแบบ inline จาก worksheet ถ้าไฟล์ไม่มี shared strings จะคืนข้อความว่าง จากนั้น `extract_attachment` คืน `(None, None)` และ `build_chat_messages` ทำ `text[:60000]` จนเกิด `TypeError`

ยืนยันด้วย ZIP จำลอง worksheet ที่มี inline text และตัวเลข แต่ไม่มี shared strings ได้ `TypeError: 'NoneType' object is not subscriptable` ควรอ่าน worksheet จริงและรองรับผลสกัดว่างก่อนตัดข้อความ

### 4.2 Error จาก Ollama ระหว่างสตรีมถูกกลืน

`_stream_chat` ไม่ตรวจ `chunk.error` เมื่อ upstream ส่ง error object จึงข้ามข้อความนั้นและส่ง `{done:true}` เมื่อ stream จบ ผู้ใช้เห็นเหมือนตอบเสร็จแต่ไม่มีคำตอบ/รายละเอียดข้อผิดพลาด ยืนยันผ่าน HTTP server ในเครื่องโดยจำลอง upstream `{"error":"model failed during inference"}`

Ollama ระบุว่าข้อผิดพลาดระหว่างสตรีมส่งเป็น NDJSON ที่มี property `error` และ HTTP status ยังคงเดิม ดังนั้นควรส่ง error ต่อให้ browser แล้วจบงานด้วยสถานะล้มเหลว ([Ollama Errors](https://docs.ollama.com/api/errors))

นอกจากนี้โค้ดตรวจ `done` ก่อนส่ง `message.content` จึงทิ้งข้อความถ้า chunk สุดท้ายมีทั้ง content และ `done:true` ยืนยันด้วยข้อมูลจำลองแล้ว แม้ชุดทดสอบเดิมใช้ content ว่างใน chunk สุดท้าย

### 4.3 การตรวจ vision ขึ้นอยู่กับข้อมูล capabilities จาก `/api/tags`

ถ้า tag ไม่มี `capabilities` โค้ดจะตั้ง `vision:false` และไม่กรอง embedding ออก ยืนยันด้วยข้อมูลจำลองของโมเดล `vl:4b` ที่ไม่มี field นี้ ส่วนเอกสาร API แสดง `capabilities` ใน `/api/show` จึงควรใช้ endpoint นี้เป็น fallback พร้อม cache ผล เมื่อ `/api/tags` ไม่มีข้อมูลความสามารถ ([List models](https://docs.ollama.com/api/tags), [Show model details](https://docs.ollama.com/api-reference/show-model-details))

### 4.4 การแบ่ง GPU ยังไม่มีการประสานงานทั้งระบบ

`free_comfy_vram` จะไม่ unload ขณะ ComfyUI มีงานกำลังรัน แต่ผู้เรียกไม่ได้ใช้ค่า False เพื่อรอคิวหรือแจ้งผู้ใช้ จึงยังเริ่มโหลด Ollama ได้พร้อมกับงานภาพ ไม่มี lock กลางที่ครอบคลุมสอง backend และทางสร้างภาพยังไม่มีขั้นตอน unload โมเดลแชตที่ตั้งให้ค้าง 30 นาที

สำหรับ RTX 3060 12 GB ควรออกแบบคิวและการคืน VRAM ทั้งสองทิศทางก่อนสรุปว่าการสลับภาพ/แชตเร็วขึ้น การเปลี่ยนนี้ช่วยเตรียม GPU ก่อนแชต แต่ยังไม่มีผล benchmark ในรายงานนี้

## 5. ข้อจำกัดด้านไฟล์และหน้าเว็บ

- PDF เป็น parser เบื้องต้นที่อ่านข้อความ literal `Tj` และ decode Latin-1 ไม่มี ToUnicode, OCR หรือ parser PDF เต็มรูปแบบ จึงยังไม่เหมาะกับ PDF ภาษาไทยทั่วไป
- DOCX อ่านเฉพาะ `word/document.xml` และลบ XML tags ด้วย regex ไม่อ่าน header/footer และยังไม่ decode XML entities
- รูปที่แนบส่งให้โมเดลเฉพาะ turn ปัจจุบัน ประวัติทั้ง frontend/backend เก็บ turn ก่อนหน้าเป็นข้อความ จึงไม่รักษารูปสำหรับถามต่อ
- ข้อผิดพลาดไฟล์แนบในแท็บแชตใช้ `setStatus` ซึ่งชี้ไปยังสถานะในแท็บสร้างภาพ ผู้ใช้อาจไม่เห็นข้อความขณะอยู่แท็บแชต
- `notes` ที่แสดงในคำตอบถูกแทนที่เมื่อ delta ต่อไปเขียน `reply.innerHTML` ทำให้คำเตือนหายระหว่างตอบ
- ปุ่มหยุด abort fetch ใน browser ยังไม่มี API ยกเลิก warm up หรืองาน upstream โดยตรง

## 6. ผลการตรวจ

- `python -m unittest discover -s tests -v`: ผ่าน 26 tests
- ทดสอบเพิ่มเติมในหน่วยความจำ: XLSX ไม่มี shared strings, tag ไม่มี capabilities, error ระหว่างสตรีม และ content ใน done chunk ได้ผลตามข้อค้นพบด้านบน
- Tests ใช้ Ollama/ComfyUI mocks และไฟล์ตัวอย่างขนาดเล็ก ไม่ได้ตรวจความเร็ว GPU จริง, PDF ภาษาไทยจริง, Excel แบบครบโครงสร้าง หรือพฤติกรรม clipboard ใน browser

ลำดับแก้ที่เสนอ: error streaming และ XLSX → capabilities fallback → คิว/VRAM ร่วมภาพกับแชต → การแสดงข้อผิดพลาดและประวัติรูป → ตัวอ่านเอกสารที่รองรับภาษาไทยได้ครบขึ้น
