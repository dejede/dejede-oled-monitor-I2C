# 🚀 dejede-oled-monitor

[![Dejede Badge](https://img.shields.io/badge/DEJEDE_%7C_%2B6285236578999-5C765D?style=flat&logo=whatsapp&logoColor=white&labelColor=3F4F40)](https://wa.me/6285236578999)
![OpenWrt](https://img.shields.io/badge/OpenWrt-23.x%20--%2025.x-1b4b34?logo=openwrt&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.x-3776ab?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-blue)
![Status](https://img.shields.io/badge/status-stable-brightgreen)

> Utilitas monitoring koneksi internet seluler *real-time* dengan animasi visual dinamis untuk router **OpenWrt** Anda, menggunakan layar **LCD OLED 0.96" I2C (128x64)** dan integrasi modem **Dell DW5821e (ModemManager)**.

---

## 📌 Fitur Utama
* **Top Bar (Informasi Seluler):** Menampilkan indikator bar sinyal dinamis, persentase kualitas sinyal akurat, dan nama operator seluler asli (diambil langsung via `mmcli`).
* **Middle Section (Trafik Jaringan):** Menampilkan kecepatan *Upload* (UP) dan *Download* (DWN) secara *real-time* lengkap dengan panah animasi naik-turun.
* **Footer Section (Marquee Text):** Teks berjalan yang dapat dikustomisasi untuk menampilkan informasi personal atau layanan.
* **Anti-Freeze & Error Transparency:** Dilengkapi proteksi waktu tunggu (*timeout*) 0.4 detik dan penanganan error transparan (seperti `TIMEOUT`, `MMCLI ERROR`, atau `NO SERVICE`) tanpa data palsu.

---

## 🛠️ Kebutuhan Hardware & Wiring

* **Router OpenWrt**
* **Modem Dell DW5821e (Snapdragon X20 LTE)**
* **LCD OLED 0.96" I2C** (Resolusi 128x64, Controller SSD1306)
* Kabel Jumper (Female-to-Female)

### Skema Wiring Pin (I2C)
| Pin OLED | Pin Router / Board |
| :--- | :--- |
| **VCC** | 3.3V |
| **GND** | Ground (GND) |
| **SDA** | I2C SDA Pin |
| **SCL** | I2C SCL Pin |

---

## 📖 Panduan Instalasi (Step-by-Step)

### 1. Update & Install Paket Sistem
Masuk ke terminal OpenWrt Anda melalui SSH, lalu jalankan perintah berikut:
```bash
opkg update
opkg install kmod-i2c-core kmod-i2c-gpio python3 python3-light python3-pip

2. Install Library Python (luma.oled)
Instal dependensi dan library grafis OLED menggunakan pip3:

Bash
opkg install python3-codecs python3-openssl python3-pillow python3-smbus i2c-tools
pip3 install luma.oled
3. Deploy Script Monitoring
Buat file script baru di direktori /root/:

Bash
nano /root/oled_monitor.py
(Salin kode program oled_monitor.py ke dalam editor, lalu simpan dengan menekan Ctrl+O, Enter, lalu keluar menggunakan Ctrl+X).

4. Uji Coba Manual
Pastikan script berjalan dengan baik tanpa error di terminal:

Bash
python3 /root/oled_monitor.py
(Tekan Ctrl+C untuk keluar dari mode uji coba).

5. Konfigurasi Auto-Start (Daemon init.d)
Agar script berjalan otomatis di latar belakang saat router melakukan booting:

Buat file service init.d:

Bash
nano /etc/init.d/oled_monitor
Masukkan konfigurasi layanan berikut:

Bash
#!/bin/sh /etc/rc.common

START=99
STOP=10

start() {
    python3 /root/oled_monitor.py &
}

stop() {
    killall python3
}
Berikan izin eksekusi dan aktifkan layanan:

Bash
chmod +x /etc/init.d/oled_monitor
/etc/init.d/oled_monitor enable
/etc/init.d/oled_monitor start

📜 Lisensi
Proyek ini bersifat open-source dan bebas untuk dikembangkan kembali sesuai kebutuhan Anda.

Modified by DEJEDE | Photo & Video Services (+62 852-3657-8999)
