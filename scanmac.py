#!/usr/bin/env python3
# scanmac.py — OCR MAC từ ảnh (Termux, song song + cache + wake-lock + UI đẹp)
# - 1 thanh tiến trình màu xanh (1 dòng): [%] (x/y)
# - Ctrl+C: thông báo rõ ràng, lưu cache & MAC đã quét, thả wake-lock rồi thoát
# - Cache ảnh đã quét (mtime+size) để không OCR lại
# - Căn lề đẹp, thẳng dấu ":", có icon trạng thái

import argparse, time, re, sys, shutil, os, json, subprocess
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

# -------- Định dạng & Regex --------
IMG_EXT   = {".png", ".jpg", ".jpeg", ".webp"}
RE_COLON  = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b")
RE_DOT    = re.compile(r"\b[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\b")
RE_RAW12  = re.compile(r"\b[0-9A-Fa-f]{12}\b")
VALID     = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")

# -------- Màu --------
GREEN = "\033[92m"
RESET = "\033[0m"

# -------- Wake-lock (giữ máy thức) --------
def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def acquire_wakelock():
    # Chỉ thử nếu có termux-wake-lock
    if have("termux-wake-lock"):
        try: subprocess.run(["termux-wake-lock"], check=False)
        except Exception: pass

def release_wakelock():
    if have("termux-wake-unlock"):
        try: subprocess.run(["termux-wake-unlock"], check=False)
        except Exception: pass

# -------- OCR helpers (top-level để pickle) --------
def fix_ocr_typos(s: str) -> str:
    return s.translate(str.maketrans({
        "O":"0","o":"0","I":"1","l":"1","ı":"1","S":"5","s":"5","B":"8",
        "—":"-","–":"-","−":"-","：":":"
    }))

def normalize_mac(s: str) -> str:
    s = s.strip().upper().replace("-", ":")
    if "." in s: s = s.replace(".", "")
    if ":" not in s and len(s) == 12:
        s = ":".join(s[i:i+2] for i in range(0, 12, 2))
    return s

def extract_macs(text: str):
    text = fix_ocr_typos(text)
    found = set()
    for m in RE_COLON.findall(text): found.add(normalize_mac(m))
    for m in RE_DOT.findall(text):   found.add(normalize_mac(m))
    for m in RE_RAW12.findall(text): found.add(normalize_mac(m))
    return {m for m in found if VALID.fullmatch(m)}

def preprocess_image(img):
    from PIL import ImageOps
    img = ImageOps.grayscale(img)
    try: img = ImageOps.autocontrast(img)
    except Exception: pass
    w, h = img.size
    if max(w, h) < 900:
        scale = 900 / max(w, h)
        img = img.resize((int(w*scale), int(h*scale)))
    return img

def ocr_one_image(path: str, lang: str) -> set[str]:
    import pytesseract
    from PIL import Image
    p = Path(path)
    try:
        img = Image.open(p)
    except Exception:
        return set()
    img = preprocess_image(img)
    cfg1 = "--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789ABCDEFabcdef:.-"
    txt1 = pytesseract.image_to_string(img, lang=lang, config=cfg1) or ""
    macs = extract_macs(txt1)
    if macs:
        return macs
    cfg2 = "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789ABCDEFabcdef:.-"
    txt2 = pytesseract.image_to_string(img, lang=lang, config=cfg2) or ""
    return extract_macs(txt2)

# -------- I/O --------
def list_images(folder: Path):
    return [str(p) for p in sorted(folder.iterdir()) if p.suffix.lower() in IMG_EXT]

def load_existing(out_path: Path):
    if not out_path.exists(): return set()
    out=set()
    for line in out_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line=line.strip().upper()
        if VALID.fullmatch(line): out.add(line)
    return out

def save_new(macs, out_path: Path):
    if not macs: return 0
    existed = load_existing(out_path)
    new = [m for m in sorted(macs) if m not in existed]
    if new:
        with out_path.open("a", encoding="utf-8") as f:
            for m in new: f.write(m + "\n")
    return len(new)

# -------- Cache ảnh đã quét --------
def cache_path_for(dir_path: Path, override: str|None) -> Path:
    return Path(override) if override else (dir_path / ".scanmac_cache.json")

def load_cache(path: Path) -> dict:
    if not path.exists(): return {"images": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"images": {}}

def save_cache(path: Path, data: dict):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def file_sig(p: Path) -> tuple[int, int]:
    try:
        st = p.stat()
        return (int(st.st_mtime), int(st.st_size))
    except Exception:
        return (0, 0)

def filter_unscanned(imgs: list[str], cache: dict) -> tuple[list[str], int]:
    unscanned = []
    skipped = 0
    for s in imgs:
        p = Path(s)
        sig = file_sig(p)
        rec = cache["images"].get(s)
        if rec and tuple(rec) == sig:
            skipped += 1
        else:
            unscanned.append(s)
    return unscanned, skipped

# -------- UI --------
def term_width(default=50):
    try:
        return max(20, shutil.get_terminal_size().columns - 20)
    except Exception:
        return default

def progress_bar(done, total, width):
    pct = done/total if total else 1.0
    filled = int(pct * width)
    bar = "█"*filled + " "*(width - filled)
    sys.stdout.write(f"\r{GREEN}[{bar}] {pct*100:5.2f}% ({done}/{total}){RESET}")
    sys.stdout.flush()

def require_deps():
    try:
        import pytesseract  # noqa
        from PIL import Image  # noqa
    except Exception:
        sys.exit("❌ Thiếu thư viện. Cài:\n   pkg install tesseract\n   pip install pillow pytesseract")

# -------- Main --------
def main():
    require_deps()
    ap = argparse.ArgumentParser(description="OCR MAC từ ảnh — song song + cache + wake-lock, Ctrl+C để dừng an toàn.")
    ap.add_argument("--dir", required=True, help="Thư mục ảnh (vd: /sdcard/OCR)")
    ap.add_argument("--out", default="scanmac", help="File kết quả")
    ap.add_argument("--lang", default="eng", help="Ngôn ngữ OCR (eng/vie/...)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2, help="Số tiến trình song song (mặc định = số lõi CPU)")
    ap.add_argument("--cache", help="Đường dẫn file cache (mặc định: DIR/.scanmac_cache.json)")
    ap.add_argument("--reset-cache", action="store_true", help="Bỏ cache cũ, quét lại toàn bộ")
    args = ap.parse_args()

    folder = Path(args.dir)
    if not folder.is_dir(): sys.exit("❌ Không tìm thấy thư mục ảnh.")
    all_imgs = list_images(folder)
    total_imgs = len(all_imgs)
    if total_imgs == 0: sys.exit("❌ Không có ảnh hợp lệ trong thư mục.")

    cpath = cache_path_for(folder, args.cache)
    cache = {"images": {}} if args.reset_cache else load_cache(cpath)

    # Wake lock để máy không sleep khi đang chạy
    acquire_wakelock()

    # Lọc ảnh đã quét (không đổi mtime/size)
    to_scan, skipped = filter_unscanned(all_imgs, cache)
    total = len(to_scan)
    if total == 0:
        # Không in progress, chỉ in tóm tắt đẹp
        labels = [
            "📂 Thư mục", "🖼️  Tổng ảnh", "🚫 Bỏ qua (cache)",
            "▶️  Quét lần này", "🔎 MAC trích xuất", "➕ Mới lưu", "💾 File kết quả"
        ]
        values = [str(folder), str(total_imgs), str(skipped), "0", "0", "0", args.out]
        w = max(len(l) for l in labels)
        print("✅ Không có ảnh mới để quét.")
        for l, v in zip(labels, values):
            print(f"{l:<{w}} : {v}")
        release_wakelock()
        return

    width   = term_width()
    start   = time.time()
    all_mac = set()
    done    = 0
    interrupted = False

    try:
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as ex:
            future_map = {ex.submit(ocr_one_image, p, args.lang): p for p in to_scan}
            for fut in as_completed(future_map):
                p = Path(future_map[fut])
                try:
                    all_mac |= (fut.result() or set())
                except Exception:
                    pass
                cache["images"][str(p)] = list(file_sig(p))  # cập nhật cache ngay khi xong ảnh
                done += 1
                progress_bar(done, total, width)
    except KeyboardInterrupt:
        interrupted = True
        # Rơi xuống finally để lưu & thông báo
    finally:
        print()  # xuống dòng sau progress
        save_cache(cpath, cache)         # lưu cache dù bị ngắt
        added  = save_new(all_mac, Path(args.out))
        spent  = round(time.time() - start, 2)
        release_wakelock()               # thả wake-lock

        # Tóm tắt đẹp, căn thẳng dấu :
        labels = [
            "📂 Thư mục", "🖼️  Tổng ảnh", "🚫 Bỏ qua (cache)",
            "▶️  Quét lần này", "✔️  Đã xử lý", "🔎 MAC trích xuất",
            "➕ Mới lưu", "⏱️  Thời gian", "💾 File kết quả"
        ]
        values = [
            str(folder), str(total_imgs), str(skipped),
            str(total), str(done), str(len(all_mac)),
            str(added), f"{spent}s", args.out
        ]
        w = max(len(l) for l in labels)

        if interrupted:
            print("⛔ Đã dừng bởi người dùng (Ctrl+C). Kết quả đã được lưu an toàn.")
        else:
            print("✅ Hoàn tất")

        for l, v in zip(labels, values):
            print(f"{l:<{w}} : {v}")

if __name__ == "__main__":
    main()
