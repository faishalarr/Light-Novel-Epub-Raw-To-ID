# Light Novel EPUB Raw → ID

Dua script Python buat bantu proses terjemahan light novel EPUB Jepang jadi Bahasa Indonesia:

1. **`translate_xhtml.py`** — menerjemahkan isi teks di dalam EPUB (Jepang → target) pakai DeepL, Gemini, atau DeepSeek, sambil mempertahankan struktur tag/HTML persis seperti aslinya.
2. **`fix_vertical_epub.py`** — mengubah EPUB yang defaultnya *vertical writing mode* (tategaki — dibaca atas-ke-bawah, kolom kanan-ke-kiri, gaya buku Jepang) jadi horizontal biasa seperti buku pada umumnya.

Keduanya bisa langsung menerima file `.epub`, folder hasil ekstrak EPUB, atau satu file `.xhtml`/`.html` saja — tidak perlu proses ekstrak/kemas manual.

---

## Requirements

```bash
pip install requests lxml
```

Python 3.9+ direkomendasikan.

---

## 1. `translate_xhtml.py`

Menerjemahkan seluruh teks di dalam EPUB per blok paragraf (`<p>`, `<li>`, heading, dst) sebagai satu kesatuan utuh — bukan per potongan kecil — supaya konteks kalimat tetap utuh dan hasil terjemahan tidak ngawur/tercampur. Furigana (`<rt>`/`<rp>`) otomatis dibuang sebelum diterjemahkan karena hanya panduan cara baca, bukan konten yang perlu diterjemahkan.

### Cara pakai

**Langsung dari file `.epub` (paling praktis, tanpa ekstrak manual):**
```bash
python translate_xhtml.py buku.epub buku_id.epub --engine gemini --key "API_KEY_KAMU"
```

**Dari folder EPUB yang sudah diekstrak:**
```bash
python translate_xhtml.py folder_epub folder_epub_id --engine gemini --key "API_KEY_KAMU"
```
Script otomatis mencari semua file `.xhtml`/`.html`/`.htm` di mana pun letaknya (rekursif ke semua subfolder, misal `OEBPS/Text/`), dan menyalin file lain (CSS, gambar, font, `.opf`, `.ncx`) apa adanya. Struktur folder & nama file dipertahankan persis.

**Satu file saja:**
```bash
python translate_xhtml.py input.xhtml output.xhtml --engine gemini --key "API_KEY_KAMU"
```

> Input dan output boleh dicampur bentuknya — misal input `.epub` → output folder, atau input folder → output `.epub` — tergantung ekstensi yang kamu berikan di argumen `output`.

### Engine yang didukung

| Engine | `--engine` | Catatan |
|---|---|---|
| DeepL | `deepl` (default) | Deterministik, batch besar aman. Perlu API key DeepL. |
| Google Gemini | `gemini` | Free tier cukup besar di Google AI Studio. Model default: `gemini-3.6-flash`. |
| DeepSeek | `deepseek` | Berbayar (prepaid) — perlu top-up saldo di [platform.deepseek.com](https://platform.deepseek.com/). Model default: `deepseek-v4-flash`. |

```bash
# DeepL
python translate_xhtml.py buku.epub buku_id.epub --key "DEEPL_API_KEY"

# Gemini
python translate_xhtml.py buku.epub buku_id.epub --engine gemini --key "GEMINI_API_KEY"

# DeepSeek
python translate_xhtml.py buku.epub buku_id.epub --engine deepseek --key "DEEPSEEK_API_KEY"
python translate_xhtml.py buku.epub buku_id.epub --engine deepseek --model deepseek-v4-pro --key "DEEPSEEK_API_KEY"
```

### Opsi lain

| Flag | Default | Keterangan |
|---|---|---|
| `--target` | `ID` | Kode bahasa target |
| `--source` | `JA` | Kode bahasa sumber |
| `--model` | tergantung engine | Nama model khusus Gemini/DeepSeek |
| `--rpm` | `12` | Batas request/menit (khusus Gemini, turunkan kalau kena limit `429`) |
| `--skip-existing` | off | Lewati file yang outputnya sudah ada — cocok buat lanjutin proses yang sempat gagal/putus tanpa mengulang dari awal |
| `--ext` | `.xhtml .html .htm` | Ekstensi file yang dianggap "halaman" saat mode folder/EPUB |

### Catatan teknis

- Setiap batch hasil terjemahan divalidasi otomatis: jumlah item harus sama persis dengan input, dan hasil yang masih mengandung banyak huruf hiragana (indikasi belum diterjemahkan) akan otomatis di-retry.
- Untuk engine Gemini, safety filter Google diset longgar khusus untuk permintaan terjemahan ini, karena teks novel (kekerasan, tema dewasa, dll) kadang membuat model "kabur" dari tugas alih-alih benar-benar diblokir.
- Entitas HTML non-standar (`&nbsp;`, `&hellip;`, dst) otomatis dikonversi ke karakter unicode sebelum parsing XML supaya tidak membuat parser gagal.

---

## 2. `fix_vertical_epub.py`

Perbaiki EPUB yang teksnya terbaca vertikal (tategaki) jadi horizontal biasa. Tidak butuh API key sama sekali — murni manipulasi teks lokal, jadi tidak kena limit apa pun.

Yang diperbaiki:
- **CSS**: semua `writing-mode` / `-webkit-writing-mode` / `-epub-writing-mode` / `-ms-writing-mode` bernilai vertikal (`vertical-rl`, `vertical-lr`, `tb-rl`, dst) dipaksa jadi `horizontal-tb`.
- **XHTML/HTML**: class yang mengarah ke layout vertikal (default: `vrtl`) diganti jadi class horizontal (default: `hltr`), termasuk `style` inline dan blok `<style>` di dalam HTML.
- **`content.opf`**: `page-progression-direction="rtl"` di tag `<spine>` diubah jadi `"ltr"` — ini penentu arah balik halaman yang sering kelewat kalau cuma benerin CSS-nya saja.

### Cara pakai

```bash
# Langsung dari file .epub -> hasil .epub baru
python fix_vertical_epub.py buku.epub buku_horizontal.epub

# Dari folder EPUB yang sudah diekstrak -> folder hasil baru
python fix_vertical_epub.py folder_epub folder_epub_horizontal

# Cek dulu tanpa menulis file apa pun
python fix_vertical_epub.py buku.epub buku_horizontal.epub --dry-run
```

Sama seperti `translate_xhtml.py`, input dan output boleh dicampur bentuknya (`.epub` ↔ folder) tergantung ekstensi `output` yang diberikan.

### Opsi lain

| Flag | Default | Keterangan |
|---|---|---|
| `--vertical-class` | `vrtl` | Nama class yang dianggap layout vertikal (bisa diulang untuk lebih dari satu nama) |
| `--horizontal-class` | `hltr` | Class pengganti. Kosongkan (`--horizontal-class ""`) untuk sekadar menghapus atribut class-nya |
| `--keep-ppd` | off | Jangan ubah `page-progression-direction` di `content.opf` |
| `--dry-run` | off | Tampilkan ringkasan perubahan tanpa menulis file apa pun |

Kalau setelah dijalankan tidak ada perubahan terdeteksi, kemungkinan besar nama class vertikal di EPUB kamu bukan `vrtl` — cek isi CSS/XHTML-nya lalu jalankan ulang dengan `--vertical-class NAMA_CLASS_KAMU`.

---

## Alur kerja yang disarankan

1. Terjemahkan dulu EPUB mentah pakai `translate_xhtml.py`.
2. Kalau hasilnya masih terbaca vertikal, perbaiki dengan `fix_vertical_epub.py` — dijalankan ke **hasil terjemahan**, bukan ke EPUB mentah, supaya tidak perlu menerjemahkan ulang (hemat kuota API).

```bash
python translate_xhtml.py buku.epub buku_id.epub --engine gemini --key "API_KEY_KAMU"
python fix_vertical_epub.py buku_id.epub buku_id_final.epub
```

---

## Lisensi & disclaimer

Script ini murni alat bantu teknis (parsing/terjemahan/manipulasi struktur EPUB). Konten EPUB itu sendiri (light novel mentah maupun hasil terjemahan) tidak disertakan di repo ini dan sepenuhnya menjadi tanggung jawab pengguna terkait hak cipta.