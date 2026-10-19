# KonversiKu — Web Konverter Dokumen

Konversi **PDF → Word**, **Word → PDF**, **Excel → PDF**, **PowerPoint → PDF**, **PDF → Gambar**, **Gambar → PDF**, dan **Gabung PDF**.
Satu container Docker berisi backend (FastAPI) + frontend (HTML statis).

```
Browser ──upload──► FastAPI (/api/convert)
                       │  validasi: ekstensi, magic bytes, ukuran, jumlah halaman, rate limit
                       ├─ PDF → Word ........ pdf2docx
                       ├─ Office → PDF ...... LibreOffice headless
                       ├─ PDF → Gambar ...... PyMuPDF
                       ├─ Gambar → PDF ...... Pillow
                       └─ Gabung PDF ........ pypdf
                     folder temp per-request → hasil dikirim → folder dihapus
```

## Struktur

```
konverter/
├── app.py            # backend + semua logika konversi
├── static/index.html # frontend (tanpa build step)
├── requirements.txt
├── Dockerfile
├── render.yaml       # blueprint 1-klik untuk Render
└── README.md
```

## ⚠️ Beda dengan website statis

Website yang isinya hanya HTML/JS (mis. di GitHub Pages / Netlify / Vercel static) **tidak bisa** menjalankan web ini,
karena konversi butuh **server** yang terpasang LibreOffice dan library Python. Platform serverless biasanya juga
membatasi ukuran upload & tidak bisa memasang LibreOffice. Jadi deploy-nya ke platform yang mendukung **Docker**.

## Jalankan di komputer sendiri

**Dengan Docker (disarankan):**
```bash
docker build -t konversiku .
docker run -p 8000:8000 konversiku
# buka http://localhost:8000
```

**Tanpa Docker** (perlu LibreOffice terpasang & `soffice` ada di PATH):
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```
Windows: set `SOFFICE_BIN="C:\Program Files\LibreOffice\program\soffice.exe"` bila `soffice` tidak ditemukan.

## Deploy (pilih salah satu)

### A. Render — paling mudah
1. Push folder ini ke repo GitHub.
2. Render → **New → Blueprint** → pilih repo (otomatis membaca `render.yaml`).
3. Tunggu build (±5–10 menit, karena memasang LibreOffice).
Catatan: paket gratis biasanya "tidur" saat tidak dipakai (akses pertama lambat) dan RAM-nya kecil — cek paket terbaru di situs Render.

### B. Hugging Face Spaces (Docker) — gratis, RAM lebih lega
1. Buat Space baru → SDK **Docker** → Blank.
2. Upload semua file. Tambahkan header ini di **paling atas** `README.md` Space:
   ```yaml
   ---
   title: KonversiKu
   sdk: docker
   app_port: 8000
   ---
   ```

### C. Railway / Fly.io
Keduanya otomatis mendeteksi `Dockerfile`. Platform mengisi env `PORT` sendiri; app sudah membacanya.

### D. VPS sendiri (kontrol penuh)
```bash
docker build -t konversiku . && docker run -d --restart unless-stopped -p 127.0.0.1:8000:8000 --name konversiku konversiku
```
Lalu pasang reverse proxy dengan HTTPS, contoh **Caddy** (`/etc/caddy/Caddyfile`):
```
konversi.domainanda.com {
    request_body { max_size 100MB }
    reverse_proxy 127.0.0.1:8000
}
```

## Konfigurasi (environment variable)

| Variabel | Default | Fungsi |
|---|---|---|
| `MAX_FILE_MB` | 20 | Batas ukuran per file |
| `MAX_FILES` | 20 | Maks file per permintaan (gabung/gambar) |
| `MAX_PDF_PAGES` | 100 | Maks halaman PDF |
| `MAX_CONCURRENT` | 2 | Konversi paralel (turunkan ke 1 bila RAM ≤ 512 MB) |
| `CONVERT_TIMEOUT` | 180 | Detik sebelum konversi dibatalkan |
| `RATE_LIMIT_PER_MIN` | 10 | Permintaan per IP per menit |
| `TRUST_PROXY` | true | Baca IP asli dari `X-Forwarded-For` (set `false` bila tanpa proxy) |

## Monitoring

- `GET /api/health` → status, uptime, jumlah konversi sukses/gagal, LibreOffice tersedia atau tidak.
- Setiap konversi menulis **1 baris log JSON** ke stdout:
  ```json
  {"event":"convert","status":"ok","tool":"pdf-to-word","files":1,"in_bytes":13054,"out_bytes":36992,"ms":936}
  ```
  Bisa diolah jadi dashboard: tool terpopuler, rata-rata waktu proses, tingkat gagal per tool, ukuran file tipikal.

## Keamanan yang sudah diterapkan
- Validasi ekstensi **dan** isi file (magic bytes) — file `.exe` yang di-rename jadi `.pdf` ditolak.
- Batas ukuran dibaca bertahap (upload raksasa dihentikan di tengah jalan), batas halaman, batas jumlah file.
- PDF terkunci password & gambar "decompression bomb" ditolak.
- Folder temp unik per request & dihapus setelah selesai — tidak ada penyimpanan permanen.
- Rate limit per IP, batas konversi paralel, timeout LibreOffice (proses di-kill bila macet).
- Container berjalan sebagai user non-root; header keamanan (CSP, X-Frame-Options, nosniff).
- Pesan error ke user tidak membocorkan detail internal (detail hanya di log, dengan kode request).

## Batasan yang perlu diketahui
- **PDF hasil scan** (isinya foto) → Word hanya berisi gambar; perlu OCR (mis. `ocrmypdf`) untuk teks yang bisa diedit.
- PDF → Word tidak selalu 100% identik untuk layout rumit (tabel kompleks, kolom ganda).
- Rate limit disimpan di memori — reset saat restart & tidak dibagi antar-instance. Untuk skala besar pakai Redis.
- Tidak ada login/antrian; untuk traffic tinggi pertimbangkan antrian job (Celery/RQ + Redis).
