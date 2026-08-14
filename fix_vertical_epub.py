#!/usr/bin/env python3
"""
fix_vertical_epub.py

Ubah EPUB novel Jepang yang defaultnya "vertical writing mode" (tategaki --
teks dibaca atas-ke-bawah, kolom kanan-ke-kiri, seperti buku Jepang asli)
supaya jadi horizontal biasa (seperti buku Indonesia/Barat pada umumnya).

Yang diperbaiki:
  1. CSS: semua deklarasi writing-mode / -webkit-writing-mode / -epub-writing-mode
     / -ms-writing-mode yang bernilai vertikal (vertical-rl, vertical-lr,
     tb-rl, tb, sideways-rl, dst) dipaksa jadi horizontal-tb.
  2. XHTML/HTML: atribut class yang mengarah ke layout vertikal (default:
     "vrtl", bisa ditambah lewat --vertical-class) diganti jadi class
     horizontal (default: "hltr", lewat --horizontal-class), dan atribut
     style inline yang mengandung writing-mode vertikal dibersihkan.
  3. content.opf (package document): atribut page-progression-direction="rtl"
     di tag <spine> diubah jadi "ltr" (atau dihapus), karena ini yang
     menentukan arah balik halaman (kanan-ke-kiri) terlepas dari CSS.

Bisa dipakai untuk:
  - File .epub langsung (di-unzip, diproses, di-zip ulang jadi .epub baru)
  - Folder hasil ekstrak EPUB (diproses jadi salinan baru)

CARA PAKAI:
    # Langsung dari file .epub -> hasil .epub baru
    python3 fix_vertical_epub.py buku.epub buku_horizontal.epub

    # Dari folder EPUB yang sudah di-ekstrak -> folder hasil baru
    python3 fix_vertical_epub.py folder_epub folder_epub_horizontal

    # Lihat dulu file mana saja yang bakal berubah tanpa benar-benar menulis apa pun
    python3 fix_vertical_epub.py buku.epub buku_horizontal.epub --dry-run

Opsional:
    --vertical-class NAMA   Class CSS yang dianggap "vertikal" (default: vrtl).
                             Bisa diulang: --vertical-class vrtl --vertical-class tategaki
    --horizontal-class NAMA Class pengganti (default: hltr). Kalau class ini
                             tidak ada definisinya di CSS, cukup dihapus attribute
                             class-nya (aman, default browser sudah horizontal).
    --keep-ppd               Jangan ubah page-progression-direction di .opf
                             (kalau kamu cuma mau benerin CSS-nya saja).
    --dry-run                 Tampilkan ringkasan perubahan tanpa menulis file.
"""

import argparse
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# --- CSS -----------------------------------------------------------------

# Properti writing-mode (termasuk prefix vendor) yang jadi target.
WRITING_MODE_PROP_RE = re.compile(
    r"(?P<prop>-webkit-writing-mode|-epub-writing-mode|-ms-writing-mode|writing-mode)"
    r"(?P<ws>\s*:\s*)"
    r"(?P<value>[^;}\n]+)"
    r"(?P<term>;?)",
    re.IGNORECASE,
)

# Nilai-nilai yang dianggap "vertikal" dan perlu diganti.
VERTICAL_VALUES_RE = re.compile(
    r"^\s*(vertical-rl|vertical-lr|tb-rl|tb-lr|tb|sideways-rl|sideways-lr)\s*$",
    re.IGNORECASE,
)


def fix_css_text(css_text: str):
    """
    Ganti semua deklarasi writing-mode vertikal jadi horizontal-tb.
    Mengembalikan (css_baru, jumlah_perubahan).
    """
    count = 0

    def repl(m):
        nonlocal count
        value = m.group("value").strip()
        if VERTICAL_VALUES_RE.match(value):
            count += 1
            return f'{m.group("prop")}{m.group("ws")}horizontal-tb{m.group("term")}'
        return m.group(0)

    new_text = WRITING_MODE_PROP_RE.sub(repl, css_text)
    return new_text, count


# --- XHTML/HTML ------------------------------------------------------------

def _class_attr_re(class_names):
    """Regex untuk atribut class="..." yang persis salah satu nama di class_names."""
    alt = "|".join(re.escape(c) for c in class_names)
    return re.compile(rf'class\s*=\s*(["\'])(?P<val>{alt})\1', re.IGNORECASE)


def fix_html_text(html_text: str, vertical_classes, horizontal_class):
    """
    Ganti class vertikal jadi class horizontal (atau hapus attribute-nya kalau
    horizontal_class kosong), dan bersihkan style inline writing-mode vertikal.
    Mengembalikan (html_baru, jumlah_perubahan).
    """
    count = 0
    new_text = html_text

    # 1) class="vrtl" (atau nama lain di vertical_classes) -> class="hltr" / dihapus
    class_re = _class_attr_re(vertical_classes)

    def class_repl(m):
        nonlocal count
        count += 1
        if horizontal_class:
            return f'class={m.group(1)}{horizontal_class}{m.group(1)}'
        return ""  # hapus attribute class sepenuhnya

    new_text = class_re.sub(class_repl, new_text)

    # 2) class yang berisi beberapa token, salah satunya vertical class
    #    mis. class="foo vrtl bar" -> class="foo hltr bar" (atau "foo bar" kalau dihapus)
    multi_class_re = re.compile(r'class\s*=\s*(["\'])(?P<val>[^"\']*)\1', re.IGNORECASE)

    def multi_class_repl(m):
        nonlocal count
        quote = m.group(1)
        tokens = m.group("val").split()
        changed = False
        new_tokens = []
        for tok in tokens:
            if tok in vertical_classes:
                changed = True
                if horizontal_class and horizontal_class not in new_tokens:
                    new_tokens.append(horizontal_class)
                # kalau horizontal_class kosong, token ini dilewati (dihapus)
            else:
                new_tokens.append(tok)
        if not changed:
            return m.group(0)
        count += 1
        return f'class={quote}{" ".join(new_tokens)}{quote}'

    new_text = multi_class_re.sub(multi_class_repl, new_text)

    # 3) style="...writing-mode: vertical-rl..." inline -> writing-mode dipaksa horizontal
    def style_repl(m):
        nonlocal count
        quote = m.group(1)
        style_val = m.group("val")
        fixed_val, n = fix_css_text(style_val)
        if n:
            count += n
            return f'style={quote}{fixed_val}{quote}'
        return m.group(0)

    style_re = re.compile(r'style\s*=\s*(["\'])(?P<val>[^"\']*)\1', re.IGNORECASE)
    new_text = style_re.sub(style_repl, new_text)

    # 4) <style>...</style> block CSS di dalam HTML
    def style_block_repl(m):
        nonlocal count
        fixed_val, n = fix_css_text(m.group("css"))
        if n:
            count += n
            return f'<style{m.group("attrs")}>{fixed_val}</style>'
        return m.group(0)

    style_block_re = re.compile(
        r'<style(?P<attrs>[^>]*)>(?P<css>.*?)</style>', re.IGNORECASE | re.DOTALL
    )
    new_text = style_block_re.sub(style_block_repl, new_text)

    return new_text, count


# --- content.opf (page-progression-direction) -------------------------------

PPD_RE = re.compile(
    r'(<spine\b[^>]*\bpage-progression-direction\s*=\s*)(["\'])rtl\2', re.IGNORECASE
)


def fix_opf_text(opf_text: str):
    """Ubah page-progression-direction="rtl" jadi "ltr" di tag <spine>."""
    count = 0

    def repl(m):
        nonlocal count
        count += 1
        return f'{m.group(1)}{m.group(2)}ltr{m.group(2)}'

    new_text = PPD_RE.sub(repl, opf_text)
    return new_text, count


# --- Pemroses file & folder --------------------------------------------------

CSS_EXTS = {".css"}
HTML_EXTS = {".xhtml", ".html", ".htm"}
OPF_EXTS = {".opf"}


def process_bytes(path: Path, raw_bytes: bytes, vertical_classes, horizontal_class, fix_ppd):
    """
    Proses isi satu file (berdasarkan ekstensi). Mengembalikan
    (bytes_baru, jumlah_perubahan, keterangan) -- bytes_baru == raw_bytes kalau
    tidak ada perubahan.
    """
    suffix = path.suffix.lower()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes, 0, None  # bukan file teks / encoding aneh, lewati

    if suffix in CSS_EXTS:
        new_text, n = fix_css_text(text)
        return (new_text.encode("utf-8"), n, "css") if n else (raw_bytes, 0, None)

    if suffix in HTML_EXTS:
        new_text, n = fix_html_text(text, vertical_classes, horizontal_class)
        return (new_text.encode("utf-8"), n, "html") if n else (raw_bytes, 0, None)

    if suffix in OPF_EXTS and fix_ppd:
        new_text, n = fix_opf_text(text)
        return (new_text.encode("utf-8"), n, "opf") if n else (raw_bytes, 0, None)

    return raw_bytes, 0, None


def process_folder(input_dir: Path, output_dir: Path, vertical_classes, horizontal_class,
                    fix_ppd, dry_run):
    changed_files = []
    all_files = [p for p in input_dir.rglob("*") if p.is_file()]
    for src in all_files:
        rel = src.relative_to(input_dir)
        dst = output_dir / rel
        raw = src.read_bytes()
        new_bytes, n, kind = process_bytes(src, raw, vertical_classes, horizontal_class, fix_ppd)
        if n:
            changed_files.append((str(rel), kind, n))
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(new_bytes)
    return changed_files


def process_epub(input_epub: Path, output_epub: Path, vertical_classes, horizontal_class,
                  fix_ppd, dry_run):
    changed_files = []
    with zipfile.ZipFile(input_epub, "r") as zin:
        entries = zin.infolist()
        if not dry_run:
            output_epub.parent.mkdir(parents=True, exist_ok=True)
            zout = zipfile.ZipFile(output_epub, "w", zipfile.ZIP_DEFLATED)
        try:
            for entry in entries:
                raw = zin.read(entry.filename)
                path = Path(entry.filename)
                new_bytes, n, kind = process_bytes(
                    path, raw, vertical_classes, horizontal_class, fix_ppd
                )
                if n:
                    changed_files.append((entry.filename, kind, n))
                if not dry_run:
                    # mimetype EPUB wajib disimpan tanpa kompresi & sebagai entry pertama;
                    # zipfile.write dengan ZIP_STORED per-entry kalau nama filenya "mimetype"
                    if entry.filename == "mimetype":
                        zout.writestr(entry, new_bytes, compress_type=zipfile.ZIP_STORED)
                    else:
                        zout.writestr(entry, new_bytes)
        finally:
            if not dry_run:
                zout.close()
    return changed_files


def zip_epub(src_dir: Path, epub_path: Path):
    """
    Kemas folder jadi file .epub (ZIP) yang valid. `mimetype` HARUS jadi
    entry pertama dan TIDAK dikompresi (spesifikasi EPUB), sisanya
    dikompresi normal.
    """
    epub_path.parent.mkdir(parents=True, exist_ok=True)
    all_files = [p for p in src_dir.rglob("*") if p.is_file()]
    mimetype_file = src_dir / "mimetype"
    rest = [p for p in all_files if p != mimetype_file]

    with zipfile.ZipFile(epub_path, "w") as z:
        if mimetype_file.exists():
            z.write(mimetype_file, "mimetype", compress_type=zipfile.ZIP_STORED)
        for p in rest:
            z.write(p, p.relative_to(src_dir), compress_type=zipfile.ZIP_DEFLATED)


def main():
    parser = argparse.ArgumentParser(
        description="Ubah EPUB Jepang dari vertical writing mode ke horizontal biasa"
    )
    parser.add_argument("input", help="File .epub ATAU folder hasil ekstrak EPUB")
    parser.add_argument("output", help="File .epub baru ATAU folder hasil -- ditentukan dari "
                                        "ekstensi yang kamu kasih (.epub -> dikemas jadi EPUB, "
                                        "selain itu -> folder biasa), berlaku untuk input file "
                                        ".epub maupun input folder.")
    parser.add_argument("--vertical-class", action="append", default=None,
                         help="Nama class yang dianggap layout vertikal (bisa diulang). "
                              "Default: vrtl")
    parser.add_argument("--horizontal-class", default="hltr",
                         help="Class pengganti (default: hltr). Kosongkan (--horizontal-class '') "
                              "untuk menghapus saja atribut class-nya.")
    parser.add_argument("--keep-ppd", action="store_true",
                         help="Jangan ubah page-progression-direction di content.opf")
    parser.add_argument("--dry-run", action="store_true",
                         help="Tampilkan ringkasan perubahan tanpa menulis file apa pun")
    args = parser.parse_args()

    vertical_classes = set(args.vertical_class) if args.vertical_class else {"vrtl"}
    horizontal_class = args.horizontal_class

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"[!] Input tidak ditemukan: {input_path}", file=sys.stderr)
        sys.exit(1)

    input_is_epub = input_path.is_file() and input_path.suffix.lower() == ".epub"
    output_is_epub = output_path.suffix.lower() == ".epub"

    print(f"[i] Class vertikal yang dicari: {', '.join(sorted(vertical_classes))}")
    print(f"[i] Diganti jadi class: {horizontal_class or '(dihapus)'}")
    print(f"[i] Perbaiki page-progression-direction: {'tidak' if args.keep_ppd else 'ya'}")
    if args.dry_run:
        print("[i] MODE DRY-RUN -- tidak ada file yang akan ditulis.")

    if input_is_epub:
        if output_is_epub:
            # .epub -> .epub: proses langsung entry-per-entry di dalam ZIP,
            # tidak perlu folder sementara sama sekali.
            print(f"[i] Memproses EPUB: {input_path}")
            changed = process_epub(
                input_path, output_path, vertical_classes, horizontal_class,
                fix_ppd=not args.keep_ppd, dry_run=args.dry_run,
            )
        else:
            # .epub -> folder: ekstrak dulu, lalu proses ke folder tujuan.
            with tempfile.TemporaryDirectory(prefix="epub_in_") as tmp_in:
                tmp_in_dir = Path(tmp_in)
                print(f"[i] Mengekstrak EPUB: {input_path}")
                with zipfile.ZipFile(input_path, "r") as z:
                    z.extractall(tmp_in_dir)
                print(f"[i] Memproses folder hasil ekstrak: {input_path}")
                changed = process_folder(
                    tmp_in_dir, output_path, vertical_classes, horizontal_class,
                    fix_ppd=not args.keep_ppd, dry_run=args.dry_run,
                )

    elif input_path.is_dir():
        if output_is_epub:
            # folder -> .epub: proses ke folder sementara dulu, baru dikemas
            # jadi ZIP .epub yang valid (mimetype disimpan tanpa kompresi).
            print(f"[i] Memproses folder: {input_path}")
            with tempfile.TemporaryDirectory(prefix="epub_out_") as tmp_out:
                tmp_out_dir = Path(tmp_out)
                changed = process_folder(
                    input_path, tmp_out_dir, vertical_classes, horizontal_class,
                    fix_ppd=not args.keep_ppd, dry_run=args.dry_run,
                )
                if not args.dry_run:
                    print(f"[i] Mengemas jadi EPUB: {output_path}")
                    zip_epub(tmp_out_dir, output_path)
        else:
            # folder -> folder biasa (perilaku lama)
            print(f"[i] Memproses folder: {input_path}")
            changed = process_folder(
                input_path, output_path, vertical_classes, horizontal_class,
                fix_ppd=not args.keep_ppd, dry_run=args.dry_run,
            )
    else:
        print(f"[!] Input harus file .epub atau folder, bukan: {input_path}", file=sys.stderr)
        sys.exit(1)

    if changed:
        print(f"\n[v] {len(changed)} file diubah:")
        for name, kind, n in changed:
            print(f"    - {name} ({kind}, {n} perubahan)")
    else:
        print("\n[i] Tidak ada perubahan ditemukan -- mungkin file sudah horizontal, "
              "atau nama class vertikalnya beda (cek dengan --vertical-class).")

    if not args.dry_run:
        print(f"\n[v] Hasil disimpan ke: {output_path}")


if __name__ == "__main__":
    main()