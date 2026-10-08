# ผลศึกษาโค้ดอัปเดต — de58fb1

หลังจัดทำรายงานนี้ ได้แก้เส้นทาง PDF และ error พร้อมจุดเกี่ยวข้องใน workspace แล้ว ดู [บันทึกการแก้ไข](FIXES_2026-09-29.md) รายงานด้านล่างเก็บผลของ snapshot `de58fb1` ก่อนแก้

ศึกษาวันที่ 29 กันยายน 2026 จาก `main` commit `de58fb1d520e4e9883b06417b58b26957a32d727` ตรงกับ remote `main` ขณะตรวจ เปรียบเทียบกับ [รายงานรุ่น 196f7e6](SYSTEM_UPDATE_2026-09-29.md)

ตรวจโค้ดและทดสอบในเครื่องพัฒนา ข้อค้นพบยังไม่ได้แก้ใน application และรายงานนี้ไม่ได้ยืนยัน deployment หรือ benchmark บน RTX 3060 จริง

## 1. สิ่งที่เปลี่ยน

มี 8 commits ใหม่ เปลี่ยน `draw_server.py`, `index.html`, `tests/test_draw_server.py` รวมเพิ่ม 433 บรรทัดและลบ 35 บรรทัด README และ systemd template ยังเหมือนรุ่นก่อน

| Commit | การเปลี่ยนแปลง |
|---|---|
| `f1e93ca` | วาง screenshot จาก clipboard ในแท็บแชตด้วย Ctrl+V |
| `90d870c` | ขยาย context แชตและแจ้งเตือนคำตอบที่ถูกตัด |
| `84d68eb` | ส่ง `num_ctx` และ `num_predict` ภายใน `options` |
| `081fe31` | แสดงจำนวน token และพื้นที่บริบทที่เหลือ |
| `daef7fa` | ตอบ HTML ด้วย `Cache-Control: no-cache` |
| `272e0a6` | อ่าน PDF ผ่าน Poppler และเก็บคำเตือนไฟล์แนบระหว่างตอบ |
| `49e9625` | อ่าน worksheet Excel เป็นตารางแทนการอ่านเฉพาะ shared strings |
| `de58fb1` | ข้ามการคืน VRAM และ warm up เมื่อโมเดลแชตผ่านเงื่อนไข resident |

Graph และ profile สร้างภาพ Qwen/FLUX รวมถึงตัวแปล prompt ไทยไม่ได้เปลี่ยนในช่วงนี้

## 2. การทำงานใหม่

### แชตและ GPU

- `CHAT_NUM_CTX` ค่าเริ่มต้น 32768 ใช้ทั้ง warm up และ chat; chat ตั้ง `options.num_predict=-1`
- `chat_model_ready` อ่าน Ollama `/api/ps` แล้วตรวจชื่อโมเดลกับ `size_vram >= 0.5 * size` ถ้าผ่านจะข้าม `free_comfy_vram` และ `warm_chat_model`
- เมื่อจบคำตอบ backend ส่ง `usage` ที่มี context ที่ตั้งไว้และจำนวน prompt/output tokens ถ้า upstream ส่งมา พร้อม `truncated` เมื่อ reason เป็น `length`
- หน้าเว็บแสดง prompt + output tokens เทียบกับ context ที่ตั้งไว้ และเปลี่ยนสีเมื่อเหลือต่ำกว่า 15% ตัวเลขอัปเดตหลังคำตอบเสร็จ ไม่ได้วัดระหว่างตอบหรือยืนยัน context ที่ runner ใช้จริง
- `streamNotes` ทำให้คำเตือนยังอยู่เมื่อแสดง delta และเมื่อคำตอบจบ จุดคำเตือนหายที่พบรุ่นก่อนดีขึ้นแล้ว

Context มากขึ้นต้องใช้หน่วยความจำเพิ่ม จึงยังสรุปไม่ได้ว่า 32K เร็วขึ้นบน VRAM 12 GB ควรตรวจ context และการ offload ของ runner จริงก่อนเลือกค่าใช้งาน ([Ollama Context length](https://docs.ollama.com/context-length))

### PDF

- โมเดล vision: ใช้ `pdftoppm` แปลงหน้า PDF เป็น JPEG 110 DPI แล้วส่งเป็นรูป ค่า `CHAT_PDF_PAGES` เริ่มต้น 6 หน้า
- โมเดลข้อความ หรือแปลงรูปไม่ได้: ใช้ `pdftotext -layout` แล้วแนบข้อความสูงสุด 60,000 ตัวอักษร
- เพิ่ม heuristic แก้ mojibake ไทยเมื่อมี U+0E00 อย่างน้อย 3 ตัว โดยลองแปลง ISO-8859-11 กลับเป็น UTF-8
- ถ้าไม่มี Poppler หรือสกัดไม่ได้ ยังใช้ parser PDF แบบเดิม เป็น fallback
- การใช้งานส่วนใหม่ขึ้นอยู่กับ executable `pdftoppm` และ `pdftotext` ใน PATH ของ service แต่ README/systemd ยังไม่อธิบาย dependency และค่าตั้งใหม่

### Excel

`extract_xlsx` อ่าน workbook relationships เพื่อหาชื่อและไฟล์ชีต อ่านค่าเซลล์ทั้ง shared/inline strings, ตัวเลข, Boolean และ cached string/error แล้วจัดคอลัมน์เป็นข้อความคั่นด้วย tab รองรับ `.xlsx` และ `.xlsm` ใน backend จำกัด 400 แถวที่มี cell ต่อชีตด้วย `CHAT_SHEET_ROWS`

ข้อจำกัด: ยังไม่ใช้ styles เพื่อแปลงวันที่หรือรูปแบบตัวเลข, ไม่คำนวณสูตร, ไม่แสดงสูตรที่ไม่มี cached value และไม่เก็บแถวว่างตามเลขแถว ช่องเลือกไฟล์หน้าเว็บยังไม่ได้เพิ่ม `.xlsm`

## 3. ข้อค้นพบที่ยืนยันเพิ่มเติม

### 3.1 PDF หลายหน้าติดขีดจำกัดรูปที่ไม่ตรงกัน

`preprocess_pdf_docs` เตรียมรูปได้ถึง 8 รูปรวม และค่าเริ่มต้นแปลง PDF 6 หน้า แต่ `build_chat_messages` ยังคงจำกัด `CHAT_MAX_IMAGES=4` หลัง preprocessing จึงปฏิเสธเอกสารที่แปลงได้เกิน 4 หน้า

ยืนยันผ่าน HTTP server ในเครื่องด้วยโมเดล vision และ renderer จำลองคืน 6 หน้า: ได้ HTTP 400 `too many images (max 4)` ควรใช้ขีดจำกัดร่วมกัน พร้อมแจ้งจำนวนหน้าที่ตัดออก

### 3.2 PDF ที่ต้องใช้ Poppler และไม่มีข้อความ prompt ถูกปฏิเสธก่อนอ่านไฟล์

`_handle_chat` เรียก `build_chat_messages` ก่อน `preprocess_pdf_docs` หาก parser เดิมอ่านไฟล์ไม่ได้ และผู้ใช้ส่ง PDF อย่างเดียวโดย content ว่าง จะเกิด `message is empty` ก่อนลอง Poppler ทั้งที่หน้าเว็บอนุญาตส่งไฟล์แนบโดยไม่พิมพ์ข้อความ

ยืนยันผ่าน HTTP ได้ 400 และ mock renderer ไม่ถูกเรียก ควรตรวจโครงสร้างคำขอก่อน แล้ว preprocessing ก่อนตัดสินว่าไม่มีเนื้อหาที่ใช้ได้

### 3.3 Warm up ล้มเหลวหลุดออกจากการจัดการ error ของสตรีม

commit ล่าสุดนำ try/except รอบขั้นเตรียม GPU ออก ขณะนี้ `warm_chat_model` อยู่ก่อน try ที่ครอบคลุม upstream chat หาก timeout/HTTP error เกิดขึ้น จะหลุดออกจาก `_stream_chat` หลังเริ่ม HTTP 200 โดยไม่ส่ง `error` หรือ `done` และไม่ได้ตั้ง state ของ heartbeat ว่างานจบ

ยืนยันด้วย handler จำลองและ warm up ที่ raise `OSError`: ส่งเพียง `user_content`, `stage:freeing`, `stage:loading` แล้ว exception หลุดออกมา ควรจัดการ prep และ chat ในเส้นทางที่ปิดสตรีม/heartbeat ได้เสมอ

### 3.4 Error ระหว่างสตรีมจาก Ollama ยังถูกกลืน

ข้อค้นพบรุ่นก่อนยังอยู่: backend ไม่ตรวจ `chunk.error` และส่ง `done:true` เมื่อ upstream จบ ยืนยัน HTTP อีกครั้งด้วยโมเดล resident และ error object ได้เพียง `user_content` กับ `done:true` ไม่มีรายละเอียด error

ตำแหน่งตรวจ `done` ยังอยู่ก่อนส่ง content เช่นเดิม จึงยังทิ้งข้อความถ้า chunk สุดท้ายมีทั้ง content และ `done:true`

## 4. จุดที่ยังต้องพิจารณา

- `chat_model_ready` ตรวจสัดส่วน VRAM แบบ heuristic ไม่ได้ตรวจ `context_length` ว่าตรงกับ `CHAT_NUM_CTX` หรือไม่ ทั้งที่ `/api/ps` มีข้อมูลนี้ โมเดลที่ resident จึงอาจยังต้องโหลด runner ใหม่เมื่อใช้ options ต่างกัน ([Ollama List running models](https://docs.ollama.com/api/ps))
- ยังไม่มีคิว GPU กลางระหว่างภาพและแชต และทางสร้างภาพยังไม่ได้ unload โมเดลแชตที่ค้างไว้
- การตรวจ vision ยังขึ้นกับ `capabilities` ใน `/api/tags`; ไม่มี fallback `/api/show`
- รูปจากไฟล์แนบและหน้าของ PDF ไม่ถูกเก็บใน history สำหรับ turn ต่อไป ส่วน PDF ที่สกัดเป็นข้อความเก็บบริบทไว้ได้
- ไฟล์ Excel ที่ไม่มี workbook/relationships ที่อ่านได้ยังคืนผลว่าง จากนั้น `extract_attachment` คืน `(None, None)` ซึ่งเส้นทาง `text[:60000]` ยังจัดการไม่ได้ ควรคืนคำอธิบายไฟล์เสียหาย/ไม่มีข้อมูล
- ข้อผิดพลาดเลือกไฟล์และวาง screenshot ยังเรียกสถานะของแท็บสร้างภาพ ผู้ใช้ในแท็บแชตอาจไม่เห็น

## 5. แผนที่โค้ดรุ่นนี้

| ส่วน | ตำแหน่ง |
|---|---|
| ตั้ง context / จำนวนหน้า / แถว | `draw_server.py:55` |
| อ่าน Excel | `draw_server.py:254` |
| render PDF / สกัดข้อความ | `draw_server.py:397`, `draw_server.py:419` |
| preprocess PDF | `draw_server.py:436` |
| ตรวจ resident / warm up | `draw_server.py:511`, `draw_server.py:525` |
| ตรวจ messages และไฟล์ | `draw_server.py:538` |
| สตรีม / usage / truncated | `draw_server.py:755` |
| HTTP chat และลำดับ preprocessing | `draw_server.py:949` |
| เพิ่มไฟล์และ paste | `index.html:341` |
| context meter | `index.html:398` |
| แสดง notes / usage ระหว่างสตรีม | `index.html:464` |

## 6. ผลตรวจและขอบเขต

- `python -m unittest discover -s tests -v`: ผ่าน 33 tests ใน 5.178 วินาที
- ทดสอบเพิ่มเติมด้วย mocks ผ่าน HTTP: PDF 6 หน้า, PDF อย่างเดียวที่ parser เดิมอ่านไม่ได้, upstream stream error
- ทดสอบ handler แยก: exception ระหว่าง warm up
- Tests PDF ใหม่ตรวจฟังก์ชัน preprocessing แยก จึงยังไม่ครอบคลุมลำดับและขีดจำกัดของ HTTP route ที่พบด้านบน
- ไม่ได้ทดสอบอ่าน PDF ภาษาไทยด้วย executable จริง, browser clipboard, หรือเวลาโหลดโมเดล/VRAM บน server จริงในรอบนี้

ลำดับแก้ที่เสนอ: ปิดสตรีมเมื่อ prep/error ล้มเหลว → จัดลำดับอ่าน PDF และขีดจำกัดรูป → ตรวจ context/VRAM runner จริง → เส้นทางไฟล์เสียหายและสถานะแชต
