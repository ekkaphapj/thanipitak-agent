# OPS — 2026-10-15 — ติดตั้ง EAS local build (Android) บน ake-server

## สิ่งที่ทำ

ติดตั้งชุดเครื่องมือสำหรับ `eas build --local --platform android` (Expo Application Services local build) บนเซิร์ฟเวอร์ `ake-server` เพื่อใช้ build แอป Android ได้บนเครื่องเราเอง ไม่ต้องเสีย quota build บนคลาวด์ของ EAS

## สิ่งที่ติดตั้ง (ผู้ใช้ ekkaphap)

| รายการ | เวอร์ชัน | ที่อยู่ |
|---|---|---|
| OpenJDK | 17.0.20.1 (apt) | `/usr/lib/jvm/java-17-openjdk-amd64` |
| Node.js | v24.21.0 (มีอยู่แล้ว) | — |
| eas-cli | 24.13.0 (npm global) | `/usr/local/bin/eas` (หรือเทียบเท่า) |
| Android cmdline-tools | 15859902 | `~/Android/Sdk/cmdline-tools/latest` |
| platform-tools | 37.0.1 | `~/Android/Sdk/platform-tools` |
| platforms | android-35, android-36 | `~/Android/Sdk/platforms` |
| build-tools | 35.0.0, 36.0.0 | `~/Android/Sdk/build-tools` |
| NDK | 27.1.12297006 | `~/Android/Sdk/ndk/27.1.12297006` |

- พื้นที่ใช้ไปรวม ~2.7GB (ดิสก์เหลือ 166GB ณ ตอนติดตั้ง)
- สคริปต์ติดตั้งอยู่ใน repo: `deploy/eas/setup_eas_android.sh` (รวมทุกขั้น รันซ้ำได้) และ `deploy/eas/contin_eas.sh` (เฉพาะ SDK packages + env ใช้ต่อเมื่อ apt ผ่านแล้ว)
- env ผูกไว้ใน `~/.bashrc` แล้ว: `ANDROID_HOME`, `JAVA_HOME`, PATH เพิ่ม `cmdline-tools/latest/bin` + `platform-tools`

## วิธีตรวจว่าพร้อมใช้

```bash
bash -lc 'eas --version && java -version && sdkmanager --list_installed | tail -8'
```

## วิธีใช้ build (ครั้งแรก)

1. ในโฟลเดอร์โปรเจกต์ Expo บนเซิร์ฟเวอร์:
   - `eas login` (บัญชี Expo) หรือตั้ง `EXPO_TOKEN` จาก https://expo.dev/accounts/[account]/settings/access-tokens
2. มี `eas.json` อย่างน้อย:
   ```json
   { "build": { "preview": { "android": { "buildType": "apk" } }, "production": { "android": { "buildType": "app-bundle" } } } }
   ```
3. build: `eas build --local --platform android -p android --profile preview` (รอบแรก gradle จะโหลด dependencies + อาจโหลด NDK เวอร์ชันที่โปรเจกต์ pin ไว้เพิ่ม ใช้เวลานาน)

## กับดักที่เจอตอนติดตั้ง (อย่าให้เจอซ้ำ)

- **sudo ทำ env_reset** — `DEBIAN_FRONTEND=noninteractive` ที่ export ไว้**ไม่ผ่าน** `sudo apt-get` ต้องฉีดตรง: `printf 'pw\n' | sudo -S -p '' env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get ...` ไม่งั้น needrestart เด้ง dialog ncurses "Pending kernel upgrade" ค้าง (เจอจริง 2 รอบ)
- **เทอร์มินัลแผงปิด = SIGHUP ฆ่าลูกที่ sudo spawn** — nohup เพียงอย่างเดียวไม่พอ ต้อง `setsid nohup bash script >/log 2>&1 </dev/null &` (เจอจริง: npm install eas-cli โดน Hangup กลางทาง)
- **pty ของแผงรับบรรทัดได้ไม่เกิน ~4096 ตัวอักษร** — อัปโหลดไฟล์เป็น base64 แบ่งบล็อก ~1000 ตัว/บรรทัด แล้ว `echo >> ` ทีละบล็อก (ดู deploy_via_panel.mjs)
- **sdkmanager --licenses ล้มได้ชั่วขณะ** (fetch remote repository ผิดพลาด) — รันซ้ำแล้วผ่าน; สคริปต์ใน repo ทำ retry ให้แล้ว
- **sdkmanager ถูก deprecate** — Google แนะนำ CLI ใหม่ชื่อ `android` (อยู่ใน cmdline-tools เดียวกัน) แต่ sdkmanager ยังใช้ได้ปกติ

## หมายเหตุอื่น

- เครื่องมือนี้ไม่ได้เป็น systemd service — ใช้เมื่อไหร่รันเมื่อนั้น ไม่กระทบ RAM/GPU ตอน idle
- NDK ที่ติดตั้ง (27.1) คือเวอร์ชันที่ React Native/Expo SDK รุ่น 2024–2025 pin ไว้ ถ้าโปรเจกต์อนาคต pin เวอร์ชันอื่น Gradle จะดาวน์โหลดเองอัตโนมัติ (license ยอมรับครบแล้ว) หรือรัน `sdkmanager "ndk;<version>"` เอง
- แผงรายงาน "*** System restart required ***" (kernel 7.0.0-38 รออยู่ ตอนติดตั้งรันบน 7.0.0-34) — ยังไม่ได้รีบูต เลื่อนไปทำตอมืดวันหลังได้ ไม่เร่งด่วน
- ตรวจสถานะบริการเดิมหลังงานเสร็จ: qwen-draw@ekkaphap / comfyui / searxng / cloudflared = active ทั้งหมด
