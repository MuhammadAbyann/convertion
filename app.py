"""
KonversiKu — web konverter dokumen (PDF <-> Word, Office -> PDF, Gambar <-> PDF, Gabung PDF)

Stack : FastAPI (API + static frontend)
Engine: pdf2docx (PDF->Word), LibreOffice headless (Office->PDF),
        PyMuPDF (PDF->Gambar), Pillow (Gambar->PDF), pypdf (Gabung PDF)

Prinsip:
- File TIDAK disimpan permanen: tiap request punya folder temp sendiri, dihapus setelah respons terkirim.
- Validasi berlapis: ekstensi + magic bytes + batas ukuran + batas jumlah file + batas halaman.
- Batas beban: semaphore (konversi paralel maksimum) + rate limit per IP + timeout per konversi.
- Log terstruktur (JSON per baris) ke stdout -> siap dibaca platform hosting / dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# ----------------------------------------------------------------------------
# Konfigurasi (bisa diubah lewat environment variable di platform hosting)
# ----------------------------------------------------------------------------
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "20"))
MAX_FILES = int(os.getenv("MAX_FILES", "20"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "100"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
CONVERT_TIMEOUT = int(os.getenv("CONVERT_TIMEOUT", "180"))
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))
TRUST_PROXY = os.getenv("TRUST_PROXY", "true").lower() == "true"
SOFFICE = os.getenv("SOFFICE_BIN") or shutil.which("soffice") or shutil.which("libreoffice") or "soffice"

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("konversiku")


def log_event(**fields):
    fields.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    log.info(json.dumps(fields, ensure_ascii=False))


# ----------------------------------------------------------------------------
# Definisi tool
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    id: str
    label: str
    desc: str
    accept: tuple[str, ...]
    multiple: bool
    output: str


TOOLS: dict[str, Tool] = {
    t.id: t
    for t in [
        Tool("pdf-to-word", "PDF ke Word", "Ubah PDF jadi dokumen Word (.docx) yang bisa diedit.", ("pdf",), False, "docx"),
        Tool("word-to-pdf", "Word ke PDF", "Ubah dokumen Word/ODT/RTF/TXT jadi PDF.", ("doc", "docx", "odt", "rtf", "txt"), False, "pdf"),
        Tool("excel-to-pdf", "Excel ke PDF", "Ubah spreadsheet Excel/ODS/CSV jadi PDF.", ("xls", "xlsx", "ods", "csv"), False, "pdf"),
        Tool("ppt-to-pdf", "PowerPoint ke PDF", "Ubah presentasi PowerPoint/ODP jadi PDF.", ("ppt", "pptx", "odp"), False, "pdf"),
        Tool("pdf-to-image", "PDF ke Gambar", "Setiap halaman PDF jadi gambar JPG (dikemas ZIP bila lebih dari 1).", ("pdf",), False, "jpg/zip"),
        Tool("image-to-pdf", "Gambar ke PDF", "Gabungkan satu atau beberapa gambar jadi satu PDF.", ("jpg", "jpeg", "png", "webp"), True, "pdf"),
        Tool("merge-pdf", "Gabung PDF", "Gabungkan beberapa PDF jadi satu file sesuai urutan.", ("pdf",), True, "pdf"),
    ]
}

# Magic bytes: memastikan isi file sesuai ekstensinya (cegah file .exe yang di-rename jadi .pdf, dll.)
ZIP = b"PK\x03\x04"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAGIC = {
    "pdf": [b"%PDF-"],
    "docx": [ZIP], "xlsx": [ZIP], "pptx": [ZIP], "odt": [ZIP], "ods": [ZIP], "odp": [ZIP],
    "doc": [OLE], "xls": [OLE], "ppt": [OLE],
    "rtf": [b"{\\rtf"],
    "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "webp": [b"RIFF"],
}
TEXT_TYPES = {"txt", "csv"}


class ConvertError(Exception):
    """Error yang pesannya aman ditampilkan ke pengguna."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
app = FastAPI(title="KonversiKu", docs_url=None, redoc_url=None)
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_hits: dict[str, deque] = defaultdict(deque)
STATS = {"started": time.time(), "ok": 0, "fail": 0}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    return resp


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(ip: str):
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise ConvertError("Terlalu banyak permintaan. Coba lagi dalam 1 menit.", 429)
    q.append(now)


def safe_stem(name: str) -> str:
    stem = Path(name or "file").stem
    stem = re.sub(r"[^\w\-. ]+", "_", stem, flags=re.UNICODE).strip(" ._")
    return (stem or "file")[:80]


def ext_of(name: str) -> str:
    return Path(name or "").suffix.lower().lstrip(".")


async def save_upload(upload: UploadFile, dest: Path, allowed: tuple[str, ...]) -> str:
    """Simpan upload ke disk sambil cek ukuran & magic bytes. Return ekstensi."""
    ext = ext_of(upload.filename)
    if ext not in allowed:
        raise ConvertError(f"Format .{ext or '?'} tidak didukung untuk tool ini. Gunakan: {', '.join('.' + a for a in allowed)}.")

    limit = MAX_FILE_MB * 1024 * 1024
    size = 0
    head = b""
    with dest.open("wb") as f:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ConvertError(f"File '{upload.filename}' melebihi batas {MAX_FILE_MB} MB.", 413)
            if len(head) < 16:
                head += chunk[: 16 - len(head)]
            f.write(chunk)

    if size == 0:
        raise ConvertError(f"File '{upload.filename}' kosong.")
    if ext in TEXT_TYPES:
        if b"\x00" in head:
            raise ConvertError(f"File '{upload.filename}' bukan file teks yang valid.")
    elif not any(head.startswith(sig) for sig in MAGIC.get(ext, [])):
        raise ConvertError(f"Isi file '{upload.filename}' tidak cocok dengan ekstensi .{ext} (file rusak atau bukan {ext.upper()}).")
    if ext == "webp" and head[8:12] != b"WEBP":
        raise ConvertError(f"File '{upload.filename}' bukan WEBP yang valid.")
    return ext


def check_pdf(path: Path, name: str):
    import pymupdf as fitz

    try:
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise ConvertError(f"PDF '{name}' terkunci password. Buka kuncinya dulu.")
            if doc.page_count > MAX_PDF_PAGES:
                raise ConvertError(f"PDF '{name}' punya {doc.page_count} halaman (maks {MAX_PDF_PAGES}).")
            return doc.page_count
    except ConvertError:
        raise
    except Exception:
        raise ConvertError(f"PDF '{name}' rusak atau tidak bisa dibaca.")


# ----------------------------------------------------------------------------
# Engine konversi
# ----------------------------------------------------------------------------
def pdf_to_word(src: Path, out: Path):
    from pdf2docx import Converter

    cv = Converter(str(src))
    try:
        cv.convert(str(out))
    finally:
        cv.close()


async def office_to_pdf(src: Path, workdir: Path) -> Path:
    """LibreOffice headless. Profil per-request supaya aman dijalankan paralel."""
    profile = workdir / "lo_profile"
    outdir = workdir / "out"
    outdir.mkdir(exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        SOFFICE, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nofirststartwizard",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to", "pdf", "--outdir", str(outdir), str(src),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=CONVERT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ConvertError("Konversi terlalu lama (timeout). Coba file yang lebih kecil.", 504)
    result = outdir / (src.stem + ".pdf")
    if proc.returncode != 0 or not result.exists():
        log_event(event="soffice_error", rc=proc.returncode, stderr=err.decode(errors="ignore")[-500:])
        raise ConvertError("Gagal mengonversi dokumen. File mungkin rusak atau terproteksi.", 422)
    return result


def pdf_to_images(src: Path, outdir: Path, stem: str, dpi: int = 150) -> Path:
    import pymupdf as fitz

    outdir.mkdir(exist_ok=True)
    files = []
    with fitz.open(src) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi)
            p = outdir / f"{stem}_hal{i:03d}.jpg"
            pix.save(str(p), jpg_quality=85)
            files.append(p)
    if len(files) == 1:
        return files[0]
    zpath = outdir.parent / f"{stem}_gambar.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, arcname=p.name)
    return zpath


def images_to_pdf(srcs: list[Path], out: Path):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 80_000_000  # cegah "decompression bomb"
    pages = []
    for s in srcs:
        im = Image.open(s)
        im = ImageOps.exif_transpose(im)  # ikuti rotasi foto HP
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        pages.append(im)
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=150)


def merge_pdfs(srcs: list[Path], out: Path):
    from pypdf import PdfWriter

    w = PdfWriter()
    for s in srcs:
        w.append(str(s))
    with out.open("wb") as f:
        w.write(f)


async def run_blocking(fn, *args):
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=CONVERT_TIMEOUT)
    except asyncio.TimeoutError:
        raise ConvertError("Konversi terlalu lama (timeout). Coba file yang lebih kecil.", 504)


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "uptime_s": int(time.time() - STATS["started"]),
        "conversions_ok": STATS["ok"],
        "conversions_fail": STATS["fail"],
        "libreoffice": bool(shutil.which(SOFFICE) or Path(SOFFICE).exists()),
    }


@app.get("/api/tools")
async def tools():
    return {
        "limits": {"max_file_mb": MAX_FILE_MB, "max_files": MAX_FILES, "max_pdf_pages": MAX_PDF_PAGES},
        "tools": [
            {"id": t.id, "label": t.label, "desc": t.desc, "accept": list(t.accept), "multiple": t.multiple, "output": t.output}
            for t in TOOLS.values()
        ],
    }


@app.post("/api/convert")
async def convert(request: Request, tool: str = Form(...), files: list[UploadFile] = File(...)):
    t0 = time.time()
    ip = client_ip(request)
    req_id = uuid.uuid4().hex[:8]
    workdir = Path(tempfile.mkdtemp(prefix=f"kv_{req_id}_"))
    total_bytes = 0

    def cleanup():
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        check_rate_limit(ip)
        spec = TOOLS.get(tool)
        if not spec:
            raise ConvertError("Tool tidak dikenal.")
        if not files:
            raise ConvertError("Belum ada file yang diunggah.")
        if not spec.multiple and len(files) > 1:
            raise ConvertError("Tool ini hanya menerima 1 file.")
        if len(files) > MAX_FILES:
            raise ConvertError(f"Maksimal {MAX_FILES} file sekaligus.")
        if spec.id == "merge-pdf" and len(files) < 2:
            raise ConvertError("Pilih minimal 2 PDF untuk digabung.")

        inputs: list[Path] = []
        for i, up in enumerate(files):
            ext = ext_of(up.filename)
            p = workdir / f"in_{i:03d}.{ext}"
            await save_upload(up, p, spec.accept)
            total_bytes += p.stat().st_size
            if ext == "pdf":
                check_pdf(p, up.filename)
            inputs.append(p)

        stem = safe_stem(files[0].filename)
        async with semaphore:  # batasi konversi paralel agar RAM/CPU server aman
            if spec.id == "pdf-to-word":
                result = workdir / f"{stem}.docx"
                await run_blocking(pdf_to_word, inputs[0], result)
            elif spec.id in ("word-to-pdf", "excel-to-pdf", "ppt-to-pdf"):
                src = workdir / f"{stem}{inputs[0].suffix}"
                inputs[0].rename(src)
                result = await office_to_pdf(src, workdir)
            elif spec.id == "pdf-to-image":
                result = await run_blocking(pdf_to_images, inputs[0], workdir / "img", stem)
            elif spec.id == "image-to-pdf":
                result = workdir / f"{stem}.pdf"
                await run_blocking(images_to_pdf, inputs, result)
            elif spec.id == "merge-pdf":
                result = workdir / f"{stem}_gabungan.pdf"
                await run_blocking(merge_pdfs, inputs, result)
            else:  # pragma: no cover
                raise ConvertError("Tool belum diimplementasikan.", 500)

        if not result.exists() or result.stat().st_size == 0:
            raise ConvertError("Hasil konversi kosong. File sumber mungkin bermasalah.", 422)

        STATS["ok"] += 1
        log_event(event="convert", status="ok", req=req_id, tool=tool, files=len(files),
                  in_bytes=total_bytes, out_bytes=result.stat().st_size, ms=int((time.time() - t0) * 1000))
        return FileResponse(result, filename=result.name, background=BackgroundTask(cleanup))

    except ConvertError as e:
        cleanup()
        STATS["fail"] += 1
        log_event(event="convert", status="fail", req=req_id, tool=tool, files=len(files or []),
                  in_bytes=total_bytes, ms=int((time.time() - t0) * 1000), reason=str(e), code=e.status)
        return JSONResponse({"error": str(e)}, status_code=e.status)
    except Exception as e:  # error tak terduga: jangan bocorkan detail ke user
        cleanup()
        STATS["fail"] += 1
        log_event(event="convert", status="error", req=req_id, tool=tool, ms=int((time.time() - t0) * 1000),
                  reason=f"{type(e).__name__}: {e}"[:300])
        return JSONResponse({"error": f"Terjadi kesalahan di server (kode {req_id}). Coba lagi."}, status_code=500)


# Frontend statis (index.html) — didaftarkan terakhir agar tidak menimpa /api/*
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
