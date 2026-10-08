# Commit / push / deploy — d9dce73

ดำเนินการวันที่ 4 ตุลาคม 2026 สำหรับ https://image.policeshield4.com/

## เวอร์ชันและการติดตั้ง

- Commit `d9dce73f`: feat(auth): gate the app behind a shared password login
- Commit โค้ด 4 ไฟล์: backend, หน้าเว็บ, tests และ README; push ไป GitHub `origin/main` สำเร็จ (`695feff..d9dce73`)
- เซิร์ฟเวอร์ `ake-server` (เชื่อมต่อผ่าน IPv6 `2405:9800:ba90:4bc8:d6d6:dfff:fe57:982e`; ไม่มี SSH alias ใน `~/.ssh/config`) checkout `/home/ekkaphap/thanipitak-agent` สะอาด แล้ว pull fast-forward สำเร็จ
- Tests ผ่าน 58 รายการทั้งบน Windows และบนเซิร์ฟเวอร์ Linux (รวม `LoginFlowTest` ใหม่)

## เหตุการณ์ที่พบระหว่าง deploy: บริการ crash-loop มาก่อน

- ก่อน restart พบว่า `qwen-draw@ekkaphap.service` เป็น `activating (auto-restart)` แล้ว ไม่ใช่ `active` — กระบวนการของบริการ bind พอร์ต 8190 ไม่ได้ (`OSError: Address already in use`) และ restart สะสมไปแล้ว 683–702 รอบ
- ตัวยึดพอร์ตคือ process `1426`: `/home/ekkaphap/ComfyUI/venv/bin/python /home/ekkaphap/draw-server/draw_server.py` — สำเนาเก่าจากโฟลเดอร์ `/home/ekkaphap/draw-server` (commit `28f5b2b` วันที่ 28 กันยายน) ที่ถูกสั่งรันแยกเมื่อ 13:33:34 UTC ของวัน deploy นี้ และแย่งพอร์ตของบริการหลักตั้งแต่นั้น เว็บจึงให้บริการโค้ดเก่า (ไม่มี login) ผ่าน process นี้ระหว่างที่บริการ systemd พัง
- จัดการโดย `kill 1426` (คิว ComfyUI ว่าง ไม่มีงานค้าง) แล้ว systemd `Restart=on-failure` ดึงบริการกลับมา `active` เองภายในไม่กี่วินาที — ไม่ต้องใช้ sudo
- ควรระวังในอนาคต: อย่าสั่งรัน `draw_server.py` จากโฟลเดอร์อื่นด้วยมือ จะชนกับบริการหลิก; หากจะทดสอบ ให้ตั้ง `DRAW_PORT` เป็นพอร์ตอื่น และควรลบ/เลิกใช้โฟลเดอร์ `/home/ekkaphap/draw-server` ที่ล้าสมัยแล้ว

## สิ่งที่เปลี่ยนในเวอร์ชันนี้

- หน้าเว็บมีหน้า "เข้าสู่ระบบ" ครอบก่อนใช้งาน กรอกรหัสผ่านร่วม (default `thanipitak1` ปรับด้วย `DRAW_PASSWORD`) แล้วได้ bearer token เก็บใน localStorage 30 วัน
- ทุก endpoint ใต้ `/api/*` ต้องแนบ `Authorization: Bearer <token>` ยกเว้น `POST /api/login` และ `GET /api/session`; รหัสผิดถูกหน่วง 0.8 วินาที
- รูปที่สร้างเสร็จโหลดผ่าน blob fetch พร้อม token แทน `<img src>` ตรง ๆ; เซสชันหมดอายุหรือรีสตาร์ทเซิร์ฟเวอร์แล้วหน้าเว็บเด้งกลับหน้า login เอง

## ผลตรวจผ่านโดเมนจริง (User-Agent แบบ browser)

- `GET /` → HTTP 200 พร้อม markup หน้า login (`login-gate`)
- `GET /api/models` ไม่มี token → HTTP 401
- `POST /api/login` รหัสถูก → token ความยาว 43 อักษร
- `GET /api/models` พร้อม token → HTTP 200; `/api/health` รายงาน Qwen และ FLUX พร้อมใช้งานทั้งคู่
- `qwen-draw@ekkaphap` (active ตั้งแต่ 14:11:37 UTC), `comfyui` และ `cloudflared` เป็น `active` ทั้งหมด

การตรวจรอบนี้ยืนยันการ deploy และระบบ login ผ่าน Tunnel จริง ไม่ได้ทดสอบการสร้างภาพหรือแชตรอบนี้เพราะ graph สร้างภาพและ chat pipeline ไม่ได้เปลี่ยนแปลงใน commit นี้
