#!/usr/bin/env python3
# scanmac.py — OCR MAC từ ảnh (Termux, nhanh & 1 thanh tiến trình)
# • Chỉ một thanh ngang duy nhất: xóa dòng & vẽ lại, auto-fit theo bề rộng terminal
# • Không cache; mỗi lần chạy OCR toàn bộ ảnh hợp lệ
# • Mỗi ảnh lấy MAC đầu tiên; mọi “O tròn” → số 0 (MAC không có chữ O)
# • Chỉ ghi /storage/emulated/0/scanmac.txt khi có MAC

import argparse, time, re, sys, shutil, os, subprocess
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

# ---------- Đường dẫn ----------
STORAGE      = Path("/storage/emulated/0").resolve()
DEFAULT_DIR  = STORAGE / "OCR"
DEFAULT_OUT  = STORAGE / "scanmac.txt"

# ---------- Ảnh & hiển thị ----------
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
GREEN = "\033[92m"; RESET = "\033[0m"

def have(cmd:str)->bool: return shutil.which(cmd) is not None
def wake_lock():
    if have("termux-wake-lock"): subprocess.run(["termux-wake-lock"], check=False)
def wake_unlock():
    if have("termux-wake-unlock"): subprocess.run(["termux-wake-unlock"], check=False)

def term_cols(default=80):
    try: return shutil.get_terminal_size().columns
    except Exception: return default

# ---- PROGRESS: 1 thanh, không wrap ----
def progress_bar(done, total, *, force=False):
    # throttle theo thời gian để mượt
    now = time.time()
    last = getattr(progress_bar, "_last", 0.0)
    if not force and (now - last) < 0.05 and done < total:
        return
    progress_bar._last = now

    cols = term_cols()
    # text phải in ngoài thân bar
    right_text = f" {done}/{total} {int((done/total if total else 1)*100):3d}%"
    # khung: "[", "]" + khoảng trắng + right_text
    reserve = len(right_text) + 4
    width = max(10, cols - reserve)  # thân bar
    pct = done/total if total else 1.0
    filled = int(pct * width)
    bar = "█"*filled + " "*(width - filled)
    # XÓA DÒNG & in lại trên CÙNG 1 DÒNG
    sys.stdout.write("\r\033[2K" + GREEN + "[" + bar + "]" + RESET + right_text)
    sys.stdout.flush()

# ---------- Regex MAC & chuẩn hoá ----------
PAT_COLON = re.compile(r"(?:\b|^)([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}(?:\b|$)")
PAT_DOT   = re.compile(r"(?:\b|^)[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}(?:\b|$)")
PAT_RAW12 = re.compile(r"(?:\b|^)[0-9A-Fa-f]{12}(?:\b|$)")
VALID     = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")

CIRCLE_TO_ZERO = str.maketrans({
    "O":"0","o":"0","Ｏ":"0","ｏ":"0","Ο":"0","ο":"0","О":"0","о":"0",
    "○":"0","◯":"0","●":"0","∘":"0","⚫":"0","⚪":"0","•":"0","∙":"0","◦":"0","〇":"0",
})
def circles_to_zero(s:str)->str: return s.translate(CIRCLE_TO_ZERO)
def normalize_mac(s:str)->str:
    s = s.strip().upper().replace("-", ":")
    if "." in s: s = s.replace(".", "")
    if ":" not in s and len(s)==12: s=":".join(s[i:i+2] for i in range(0,12,2))
    return s
def first_mac(text:str)->str|None:
    text = circles_to_zero(text)
    for rx in (PAT_COLON, PAT_DOT, PAT_RAW12):
        m = rx.search(text)
        if m:
            mac = normalize_mac(m.group(0))
            if VALID.fullmatch(mac): return mac
    return None

# ---------- OCR (nhanh trước, chính xác sau) ----------
def tess_cfgs():
    base = ("--oem 3 --dpi 300 "
            "-c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ:.- "
            "-c load_system_dawg=0 -c load_freq_dawg=0")
    return [f"{base} --psm 7", f"{base} --psm 6"]

def fast_pre(img):
    from PIL import ImageOps
    g = ImageOps.grayscale(img)
    try: g = ImageOps.autocontrast(g)
    except Exception: pass
    return g

def accurate_variants(img):
    from PIL import ImageOps, ImageFilter
    g = ImageOps.grayscale(img)
    try: g = ImageOps.autocontrast(g)
    except Exception: pass
    out = [ g.filter(ImageFilter.UnsharpMask(radius=1.0, percent=150, threshold=2)) ]
    w,h = g.size
    if max(w,h) < 1100:
        s = 1100/max(w,h)
        out.append(g.resize((int(w*s), int(h*s))))
    out.append(g.point(lambda x: 255 if x>165 else 0, mode="1"))
    return out

def ocr_one_image(path:str, lang:str)->str|None:
    import pytesseract
    from PIL import Image
    p = Path(path)
    try: img = Image.open(p)
    except Exception: return None
    g = fast_pre(img)
    for cfg in tess_cfgs():
        mac = first_mac(pytesseract.image_to_string(g, lang=lang, config=cfg) or "")
        if mac: return mac
    for v in accurate_variants(img):
        for cfg in tess_cfgs():
            mac = first_mac(pytesseract.image_to_string(v, lang=lang, config=cfg) or "")
            if mac: return mac
    return None

# ---------- Ảnh hợp lệ ----------
def list_images(folder:Path, min_bytes:int=1024, min_px:int=80):
    from PIL import Image
    valid=[]; total=0
    for p in sorted(folder.iterdir()):
        total+=1
        if p.suffix.lower() not in IMG_EXT: continue
        try: sz=p.stat().st_size
        except Exception: continue
        if sz<min_bytes: continue
        try:
            with Image.open(p) as im: w,h=im.size
        except Exception: continue
        if w<min_px or h<min_px: continue
        valid.append(str(p))
    return valid, total

# ---------- Phụ thuộc ----------
def require_deps():
    try:
        import pytesseract  # noqa
        from PIL import Image  # noqa
    except Exception:
        sys.exit("Thiếu thư viện. Cài:\n  pkg install tesseract\n  pip install pillow pytesseract")

# ---------- Main ----------
def main():
    require_deps()
    cpu = os.cpu_count() or 2
    ap = argparse.ArgumentParser(description="OCR MAC từ ảnh (1 thanh tiến trình, không cache).")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="Thư mục ảnh (mặc định: /storage/emulated/0/OCR)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="File kết quả (mặc định: /storage/emulated/0/scanmac.txt)")
    ap.add_argument("--lang", default="eng", help="Ngôn ngữ OCR")
    ap.add_argument("--workers", type=int, help="Số tiến trình 1–8 (nếu bỏ qua sẽ hỏi)")
    args = ap.parse_args()

    if args.workers is None:
        try:
            w = input(f"Số nhân (1–8, Enter={min(8, max(1, cpu))}): ").strip()
            args.workers = int(w) if w else min(8, max(1, cpu))
        except Exception:
            args.workers = min(8, max(1, cpu))
    args.workers = max(1, min(8, args.workers))

    folder  = Path(args.dir).resolve()
    outpath = Path(args.out).resolve()
    if not folder.is_dir(): sys.exit("❌ Không tìm thấy thư mục ảnh.")
    if str(outpath).startswith(str(folder) + os.sep):
        outpath = STORAGE / "scanmac.txt"

    imgs, _ = list_images(folder)
    n_valid = len(imgs)
    if n_valid == 0:
        print("✅ Không có ảnh hợp lệ → bỏ qua.")
        print("Ảnh hợp lệ : 0")
        return

    wake_lock()
    start = time.time()
    progress_bar(0, n_valid, force=True)

    macs = set()
    processed = 0
    interrupted = False
    try:
        if args.workers > 1:
            with ProcessPoolExecutor(max_workers=args.workers) as ex:
                fut = {ex.submit(ocr_one_image, p, args.lang): p for p in imgs}
                for f in as_completed(fut):
                    try:
                        m = f.result()
                        if m: macs.add(m)
                    except Exception:
                        pass
                    processed += 1
                    progress_bar(processed, n_valid)
        else:
            for p in imgs:
                m = ocr_one_image(p, args.lang)
                if m: macs.add(m)
                processed += 1
                progress_bar(processed, n_valid)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        progress_bar(n_valid, n_valid, force=True); print()
        wake_unlock()

        saved = 0
        if macs:
            outpath.parent.mkdir(parents=True, exist_ok=True)
            with outpath.open("w", encoding="utf-8") as f:
                for m in sorted(macs):
                    f.write(m + "\n")
            saved = len(macs)
        elif outpath.exists():
            try: outpath.unlink()
            except Exception: pass

        spent = f"{round(time.time()-start, 2):.2f}s"
        print("⛔ Dừng (Ctrl+C), đã xử lý an toàn." if interrupted else "✅ Hoàn tất")
        wlab = max(len(x) for x in ["Ảnh hợp lệ","Đã xử lý","MAC hợp lệ","Mới lưu","Thời gian","Workers"])
        print(f"{'Ảnh hợp lệ':<{wlab}} : {n_valid}")
        print(f"{'Đã xử lý':<{wlab}} : {processed}")
        print(f"{'MAC hợp lệ':<{wlab}} : {len(macs)}")
        print(f"{'Mới lưu':<{wlab}} : {saved}")
        print(f"{'Thời gian':<{wlab}} : {spent}")
        print(f"{'Workers':<{wlab}} : {args.workers}")

if __name__ == "__main__":
    main()
