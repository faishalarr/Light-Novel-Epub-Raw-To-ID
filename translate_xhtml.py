#!/usr/bin/env python3
"""
translate_xhtml.py

Menerjemahkan teks di dalam file XHTML (Jepang -> bahasa target)
menggunakan DeepL API, Gemini API, ATAU DeepSeek API, sambil mempertahankan
struktur tag, atribut, DOCTYPE, dan namespace persis seperti aslinya (penting
untuk EPUB).

Bisa dipakai untuk SATU FILE atau SATU FOLDER (otomatis proses semua
file .xhtml/.html di dalamnya, termasuk subfolder).

CARA PAKAI DEEPL (satu file):
    python3 translate_xhtml.py input.xhtml output.xhtml --key "DEEPL_API_KEY"

CARA PAKAI GEMINI (satu file):
    python3 translate_xhtml.py input.xhtml output.xhtml --engine gemini --key "GEMINI_API_KEY"

CARA PAKAI DEEPSEEK (satu file):
    python3 translate_xhtml.py input.xhtml output.xhtml --engine deepseek --key "DEEPSEEK_API_KEY"
    python3 translate_xhtml.py input.xhtml output.xhtml --engine deepseek --model deepseek-v4-pro --key "DEEPSEEK_API_KEY"

CARA PAKAI (satu folder, engine apa saja):
    python3 translate_xhtml.py folder_input folder_output --key "API_KEY_KAMU"
    python3 translate_xhtml.py folder_input folder_output --engine gemini --key "API_KEY_KAMU"
    python3 translate_xhtml.py folder_input folder_output --engine deepseek --key "API_KEY_KAMU"

    -> Struktur folder & nama file dipertahankan persis, hanya isi teks
       di dalam file .xhtml/.html/.htm yang diterjemahkan. File lain
       (gambar, css, font, opf, ncx, dll) otomatis disalin apa adanya.

Opsional:
    --engine deepl|gemini|deepseek (default: deepl)
    --model MODEL_NAME     (khusus --engine gemini/deepseek, default beda per engine)
    --target ID            (default: ID / Indonesia)
    --source JA            (default: JA / Jepang)
    --ext .xhtml .html     (ekstensi file yang dianggap "halaman" saat mode
                            folder; default: .xhtml .html .htm)

CATATAN PENTING (v4):
- Setiap PARAGRAF/BLOK (<p>, <li>, <h1>-<h6>, dll) diterjemahkan SEBAGAI
  SATU KESATUAN UTUH beserta tag inline di dalamnya (<span>, <b>, <ruby>,
  dst). Untuk DeepL ini memakai fitur tag_handling=xml. Untuk Gemini/DeepSeek
  ini memakai output terstruktur JSON (array string berisi fragmen XML).
  Ini penting supaya model punya konteks kalimat penuh -- kalau dipecah
  kecil-kecil per fragmen teks, hasil terjemahan jadi ngawur/tercampur
  (terutama untuk nama diri / istilah yang "terpotong" oleh tag ruby/furigana).
- <rt>/<rp> (furigana) dibuang & teks dasar <ruby> digabung SEBELUM
  dikirim ke API (lihat flatten_ruby), karena furigana cuma panduan
  cara baca kanji dan tidak relevan diterjemahkan.
- Menggunakan lxml.etree (bukan BeautifulSoup) supaya output selalu
  berupa XML yang valid & well-formed.
- Entitas HTML yang tidak dikenal parser XML murni (mis. &nbsp;,
  &hellip;, &mdash;, &rsquo;, dst) otomatis dikonversi ke karakter
  unicode aslinya SEBELUM parsing. Entitas standar XML
  (&amp; &lt; &gt; &quot; &apos;) TIDAK disentuh.
- Baik Gemini maupun DeepSeek adalah LLM (bukan mesin terjemahan
  deterministik seperti DeepL), jadi keduanya dibatasi ke batch kecil
  dan hasilnya divalidasi: (a) jumlah item hasil harus sama persis
  dengan jumlah input, dan (b) hasil yang masih mengandung banyak huruf
  hiragana (indikasi belum diterjemahkan / cuma di-copy balik) akan
  memicu retry.
"""

import argparse
import html.entities
import json
import re
import shutil
import sys
import time
from io import BytesIO
from pathlib import Path

import requests
from lxml import etree

# Waktu panggilan Gemini terakhir (buat throttle proaktif -- lihat _throttle_gemini)
_last_gemini_call_ts = 0.0

# Tag yang isinya TIDAK boleh diterjemahkan sama sekali (kode, script, dsb)
SKIP_TAGS = {"script", "style", "code", "pre"}

# Tag "blok" -- unit terjemahan. Elemen dengan tag ini (dan tidak berisi
# blok lain di dalamnya) diterjemahkan sebagai SATU permintaan utuh ke
# API, lengkap dengan tag inline di dalamnya (span, b, ruby, dst).
BLOCK_TAGS = {
    "p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "dt", "dd", "caption", "figcaption", "title", "div",
}

# Batas aman jumlah karakter/item per batch -- beda per engine, karena
# DeepL deterministik (aman batch besar), sedangkan Gemini/DeepSeek (LLM)
# cenderung "salah hitung" jumlah item kalau dikasih batch besar sekaligus
# (hasil array JSON-nya bisa kurang/lebih dari jumlah input) -- jadi
# batch-nya harus jauh lebih kecil demi keandalan.
BATCH_LIMITS = {
    "deepl": {"max_chars": 4000, "max_items": 50},
    "gemini": {"max_chars": 1200, "max_items": 6},
    "deepseek": {"max_chars": 1200, "max_items": 6},
}

# Model default per engine (dipakai kalau user tidak memberi --model)
DEFAULT_MODELS = {
    "gemini": "gemini-3.6-flash",
    "deepseek": "deepseek-v4-flash",
}

# Entitas XML standar -- JANGAN pernah dikonversi jadi karakter literal,
# karena & dan < literal akan merusak struktur XML.
XML_RESERVED_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

_entity_re = re.compile(r"&([a-zA-Z][a-zA-Z0-9]*);")


def fix_html_entities(text: str) -> str:
    """
    Ganti entitas bernama HTML (&nbsp; &hellip; dst) jadi karakter unicode
    asli, supaya parser XML ketat tidak gagal karena entitas tak dikenal.
    Entitas standar XML (amp/lt/gt/quot/apos) dibiarkan apa adanya.
    """
    def repl(m):
        name = m.group(1)
        if name in XML_RESERVED_ENTITIES:
            return m.group(0)
        char = html.entities.html5.get(name + ";")
        if char is not None:
            return char
        return m.group(0)  # entitas tak dikenal, biarkan (jarang terjadi)

    return _entity_re.sub(repl, text)


def get_deepl_endpoint(api_key: str) -> str:
    """Pilih endpoint DeepL berdasarkan jenis API key."""
    if api_key.strip().endswith(":fx"):
        return "https://api-free.deepl.com/v2/translate"
    return "https://api.deepl.com/v2/translate"


def local_name(tag) -> str:
    """Ambil nama tag tanpa namespace, mis. '{ns}script' -> 'script'."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def flatten_ruby(elem):
    """
    Gabungkan teks dasar (kanji) di dalam elemen <ruby> jadi SATU string utuh,
    dan buang seluruh anak <rt>/<rp> (furigana / cara-baca) beserta isinya.

    Contoh: <ruby>資格<rt>しかく</rt></ruby> -> base text "資格" (furigana dibuang).
    Contoh multi-segmen: <ruby>私<rt>し</rt>費<rt>か</rt></ruby> -> "私費" (tetap digabung).
    """
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in list(elem):
        if local_name(child.tag) in ("rt", "rp"):
            if child.tail:
                parts.append(child.tail)
            elem.remove(child)
    elem.text = "".join(parts)


def flatten_all_ruby(root):
    """Terapkan flatten_ruby ke SEMUA elemen <ruby> di seluruh tree."""
    for elem in root.iter():
        if isinstance(elem.tag, str) and local_name(elem.tag) == "ruby":
            flatten_ruby(elem)


def find_translation_units(root):
    """
    Cari elemen "blok" (lihat BLOCK_TAGS) yang akan jadi satu unit
    terjemahan utuh. Kalau sebuah blok masih berisi blok lain di
    dalamnya (mis. <div> yang berisi beberapa <p>), turun lebih dalam
    dan pakai blok anaknya sebagai unit (lebih granular & lebih aman
    ukurannya), bukan blok induknya.
    """
    units = []

    def has_nested_block(elem):
        for d in elem.iterdescendants():
            if isinstance(d.tag, str) and local_name(d.tag) in BLOCK_TAGS:
                return True
        return False

    def walk(elem):
        if not isinstance(elem.tag, str):
            return
        name = local_name(elem.tag)
        if name in SKIP_TAGS:
            return
        if name in BLOCK_TAGS and not has_nested_block(elem):
            units.append(elem)
            return
        for child in elem:
            walk(child)

    walk(root)
    return units


def serialize_inner(elem) -> str:
    """Serialisasi ISI (bukan tag pembungkusnya sendiri) elemen jadi string XML."""
    parts = [elem.text or ""]
    for child in elem:
        parts.append(etree.tostring(child, encoding="unicode", with_tail=True))
    return "".join(parts)


def replace_inner(elem, translated_xml: str):
    """Ganti ISI elemen dengan hasil parse ulang string XML terjemahan."""
    wrapper = f"<_unit_>{translated_xml}</_unit_>"
    try:
        frag_root = etree.fromstring(wrapper.encode("utf-8"))
    except etree.XMLSyntaxError:
        parser = etree.XMLParser(recover=True)
        frag_root = etree.fromstring(wrapper.encode("utf-8"), parser)

    for child in list(elem):
        elem.remove(child)
    elem.text = frag_root.text
    for child in list(frag_root):
        elem.append(child)


def chunk_units(units, max_chars, max_items):
    """Bagi unit-unit (blok) jadi batch sesuai batas karakter & jumlah item."""
    batch = []
    batch_chars = 0
    for elem, xml_text in units:
        if batch and (
            len(batch) >= max_items
            or batch_chars + len(xml_text) > max_chars
        ):
            yield batch
            batch = []
            batch_chars = 0
        batch.append((elem, xml_text))
        batch_chars += len(xml_text)
    if batch:
        yield batch


# Deteksi hasil "terjemahan" yang sebenarnya masih mentah bahasa Jepang
# (model kadang cuma copy-paste balik sebagian item tanpa benar-benar
# menerjemahkan, meski jumlah hasilnya tetap pas). Cek keberadaan huruf
# HIRAGANA saja (bukan kanji) karena hampir semua kalimat Jepang asli
# pasti mengandung hiragana (partikel, akhiran kata kerja, dst), sementara
# nama diri berbentuk kanji yang sengaja dipertahankan itu wajar & bukan
# indikasi gagal terjemahan.
HIRAGANA_RE = re.compile(r"[\u3041-\u3096]")


def find_untranslated_indices(translations, target_lang):
    """Kembalikan indeks item yang kelihatannya masih mentah bahasa Jepang."""
    if target_lang.strip().upper() in ("JA", "JA-JP", "JP"):
        return []  # target memang bahasa Jepang, jangan dicurigai
    bad = []
    for i, t in enumerate(translations):
        if len(HIRAGANA_RE.findall(t)) >= 4:
            bad.append(i)
    return bad


def translate_batch_deepl(texts, api_key, source_lang, target_lang, retries=3):
    """
    Kirim satu batch teks (fragmen XML per blok) ke DeepL dengan
    tag_handling=xml, supaya tag inline (span, b, ruby, dst) dipertahankan
    dan teks diterjemahkan dengan konteks satu blok/kalimat penuh.
    """
    endpoint = get_deepl_endpoint(api_key)
    data = [
        ("target_lang", target_lang),
        ("tag_handling", "xml"),
        ("preserve_formatting", "1"),
    ]
    if source_lang:
        data.append(("source_lang", source_lang))
    for t in texts:
        data.append(("text", t))

    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(endpoint, data=data, headers=headers, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                translations = [item["text"] for item in result["translations"]]
                bad = find_untranslated_indices(translations, target_lang)
                if bad:
                    raise ValueError(
                        f"{len(bad)} dari {len(texts)} hasil masih mengandung teks Jepang "
                        f"mentah (indeks: {bad})"
                    )
                return translations
            else:
                last_err = f"HTTP {resp.status_code}: {resp.text}"
        except (requests.RequestException, ValueError) as e:
            last_err = str(e)

        wait = 2 ** attempt
        print(f"    [!] Gagal (percobaan {attempt}/{retries}): {last_err}", file=sys.stderr)
        if attempt < retries:
            print(f"        Coba lagi dalam {wait} detik...", file=sys.stderr)
            time.sleep(wait)

    raise RuntimeError(f"Gagal menerjemahkan batch setelah {retries} percobaan: {last_err}")


LLM_SYSTEM_PROMPT = (
    "Kamu adalah mesin penerjemah profesional untuk fragmen XML/XHTML dari novel/teks Jepang.\n"
    "Kamu akan menerima beberapa fragmen XML bernomor. Untuk SETIAP fragmen, kamu WAJIB "
    "MENERJEMAHKAN SELURUH TEKS yang terlihat oleh pembaca ke bahasa target. Hasil akhir TIDAK "
    "BOLEH mengandung satu pun kata/kalimat bahasa sumber yang tidak diterjemahkan -- ini adalah "
    "kesalahan fatal, bukan pilihan gaya.\n"
    "Aturan lain:\n"
    "- Yang HARUS disalin persis apa adanya (TIDAK diterjemahkan/diubah) hanyalah TAG XML/HTML-nya "
    "sendiri (mis. <span>, <ruby>, <br/>, <b>) beserta seluruh atributnya (class, id, dst), dan "
    "urutan/nesting tag tersebut. Ini TIDAK berlaku untuk teks di dalam tag -- teks di dalam tag "
    "tetap wajib diterjemahkan.\n"
    "- Pertahankan gaya bahasa naratif/dialog (jangan terlalu kaku atau formal), dan adaptasikan "
    "tanda baca/simbol khas novel (— … 「」 『』 dll) secara wajar ke bahasa target.\n"
    "- Kembalikan hasil sebagai JSON dengan SATU key 'translations': array berisi TEPAT sejumlah "
    "fragmen input, urutannya harus sama persis dengan urutan input.\n"
    "- Setiap item di 'translations' adalah SATU fragmen XML utuh (lengkap dengan tag aslinya), "
    "bukan teks polos tanpa tag.\n"
    "- JANGAN menambahkan komentar, penjelasan, atau teks lain di luar objek JSON tersebut.\n\n"
    "Contoh (sumber Jepang -> target Indonesia):\n"
    "Input fragmen: <p>これは<b>ペン</b>です。</p>\n"
    "Output translations yang BENAR: <p>Ini adalah <b>pena</b>.</p>\n"
    "Output translations yang SALAH (dilarang): <p>これは<b>ペン</b>です。</p>  <- ini SALAH karena "
    "teksnya tidak diterjemahkan sama sekali."
)

# Nama bahasa yang lebih jelas buat prompt, supaya model tidak bingung
# menafsirkan kode singkat seperti "ID"/"JA" sebagai bagian dari teks.
LANG_NAMES = {
    "ID": "Bahasa Indonesia", "EN": "Bahasa Inggris", "JA": "Bahasa Jepang",
    "JP": "Bahasa Jepang", "ZH": "Bahasa Mandarin", "KO": "Bahasa Korea",
    "FR": "Bahasa Prancis", "DE": "Bahasa Jerman", "ES": "Bahasa Spanyol",
    "PT": "Bahasa Portugis", "RU": "Bahasa Rusia", "VI": "Bahasa Vietnam",
    "TH": "Bahasa Thailand", "AR": "Bahasa Arab",
}


def _lang_display(code: str) -> str:
    name = LANG_NAMES.get(code.strip().upper())
    return f"{name} (kode: {code})" if name else code

# Alias supaya nama lama (dipakai fungsi Gemini) tetap jalan.
GEMINI_SYSTEM_PROMPT = LLM_SYSTEM_PROMPT

GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "translations": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        }
    },
    "required": ["translations"],
}


def _build_numbered_prompt(texts, source_lang, target_lang):
    numbered = "\n\n".join(f"[{i + 1}]\n{t}" for i, t in enumerate(texts))
    return (
        f"Bahasa sumber: {_lang_display(source_lang)}\n"
        f"Bahasa target: {_lang_display(target_lang)}\n"
        f"Jumlah fragmen input: {len(texts)}\n\n"
        f"TERJEMAHKAN seluruh fragmen di bawah ini ke {_lang_display(target_lang)}. "
        f"Jangan biarkan satu pun kalimat tetap dalam {_lang_display(source_lang)}.\n\n"
        f"{numbered}"
    )


def _throttle_gemini(rpm):
    """
    Kasih jeda proaktif sebelum tiap panggilan Gemini, supaya laju request
    gak nabrak limit RPM (request per menit) free tier dari awal --
    lebih baik dicegah daripada nunggu kena 429 dulu baru retry.
    """
    global _last_gemini_call_ts
    if rpm <= 0:
        return
    min_interval = 60.0 / rpm
    elapsed = time.time() - _last_gemini_call_ts
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_gemini_call_ts = time.time()


def _parse_retry_delay(resp, default=15.0):
    """
    Ambil durasi tunggu yang disarankan dari respons error Gemini (429),
    baik dari field terstruktur (RetryInfo.retryDelay) maupun dari teks
    pesan error ("...retry in 25.95s"). Fallback ke `default` kalau
    tidak ketemu.
    """
    try:
        data = resp.json()
        for d in data.get("error", {}).get("details", []):
            if str(d.get("@type", "")).endswith("RetryInfo"):
                delay = d.get("retryDelay", "")
                if delay.endswith("s"):
                    return float(delay[:-1])
    except Exception:
        pass
    m = re.search(r"retry in ([\d.]+)\s*s", resp.text or "", re.IGNORECASE)
    if m:
        return float(m.group(1))
    return default


def _extract_json_translations(raw_text, expected_count, target_lang):
    """
    Parse balasan model (harus JSON dengan key 'translations') dan validasi
    jumlah item + deteksi teks Jepang mentah. Dipakai bareng oleh Gemini
    dan DeepSeek supaya logika validasinya konsisten.
    """
    cleaned = raw_text.strip()
    # Jaga-jaga kalau model membungkus JSON dengan ```json ... ``` walau
    # sudah diminta JSON murni.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"```$", "", cleaned).strip()

    parsed = json.loads(cleaned)
    translations = parsed["translations"]
    if len(translations) != expected_count:
        raise ValueError(
            f"Jumlah hasil ({len(translations)}) tidak sama dengan jumlah input ({expected_count})"
        )
    bad = find_untranslated_indices(translations, target_lang)
    if bad:
        raise ValueError(
            f"{len(bad)} dari {expected_count} hasil masih mengandung teks Jepang "
            f"mentah (indeks: {bad})"
        )
    return translations


def translate_batch_gemini(texts, api_key, model, source_lang, target_lang, rpm=12, retries=5):
    """
    Kirim satu batch fragmen XML ke Gemini API (Google AI Studio), memakai
    structured output (responseSchema) supaya hasilnya berupa JSON array
    yang urutannya selalu sinkron dengan input -- bukan teks bebas yang
    perlu di-parse manual.
    """
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    prompt = _build_numbered_prompt(texts, source_lang, target_lang)

    payload = {
        "systemInstruction": {"role": "system", "parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_RESPONSE_SCHEMA,
            "temperature": 0.2,
            # Ruang output digenerosikan supaya tidak kepotong di tengah
            # jalan (JSON terpotong = jumlah item ikut salah/rusak).
            "maxOutputTokens": 8192,
        },
        # Novel/teks naratif sering mengandung kekerasan, tema dewasa, atau
        # bahasa kasar -- kalau safety filter default aktif, Gemini kadang
        # "kabur" dari tugas dengan cara diam-diam mengembalikan teks asli
        # (bukan error eksplisit), yang persis gejala "masih Jepang mentah".
        # Ini HANYA mematikan filter safety Google untuk permintaan terjemahan
        # ini, bukan mengubah kebijakan konten Claude/Anthropic.
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}

    last_err = None
    for attempt in range(1, retries + 1):
        _throttle_gemini(rpm)
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=90)
            if resp.status_code == 200:
                result = resp.json()
                candidate = result["candidates"][0]
                finish_reason = candidate.get("finishReason", "")
                parts = candidate.get("content", {}).get("parts", [])
                if not parts:
                    raise ValueError(
                        f"Respons kosong dari Gemini (finishReason={finish_reason}) -- "
                        f"kemungkinan diblokir safety filter"
                    )
                raw_text = parts[0]["text"]
                try:
                    return _extract_json_translations(raw_text, len(texts), target_lang)
                except (ValueError, KeyError, json.JSONDecodeError) as e:
                    raise ValueError(f"{e} (finishReason={finish_reason})")
            elif resp.status_code == 429:
                # Rate limit -- ikuti durasi tunggu yang disarankan Google sendiri,
                # bukan backoff pendek biasa, dan jangan buru-buru kehabisan jatah retry.
                wait = _parse_retry_delay(resp) + 1.0
                last_err = f"HTTP 429 (kuota/limit tercapai): menunggu {wait:.0f} detik sesuai saran API..."
                print(f"    [!] {last_err}", file=sys.stderr)
                time.sleep(wait)
                continue
            else:
                last_err = f"HTTP {resp.status_code}: {resp.text}"
        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as e:
            last_err = str(e)

        wait = 2 ** attempt
        print(f"    [!] Gagal (percobaan {attempt}/{retries}): {last_err}", file=sys.stderr)
        if attempt < retries:
            print(f"        Coba lagi dalam {wait} detik...", file=sys.stderr)
            time.sleep(wait)

    raise RuntimeError(f"Gagal menerjemahkan batch setelah {retries} percobaan: {last_err}")


def translate_batch_deepseek(texts, api_key, model, source_lang, target_lang, retries=5):
    """
    Kirim satu batch fragmen XML ke DeepSeek API (endpoint OpenAI-compatible
    /chat/completions), memakai response_format json_object supaya hasilnya
    JSON yang bisa di-parse dan divalidasi (jumlah item + cek teks Jepang
    mentah), sama seperti jalur Gemini.
    """
    endpoint = "https://api.deepseek.com/chat/completions"

    user_prompt = _build_numbered_prompt(texts, source_lang, target_lang)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    LLM_SYSTEM_PROMPT
                    + "\n\nKembalikan JAWABANMU HANYA berupa objek JSON valid, "
                      "tanpa markdown fence, tanpa teks lain di luar JSON."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 8192,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=90)
            if resp.status_code == 200:
                result = resp.json()
                raw_text = result["choices"][0]["message"]["content"]
                return _extract_json_translations(raw_text, len(texts), target_lang)
            elif resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else (2 ** attempt)
                last_err = f"HTTP 429 (rate limit): menunggu {wait:.0f} detik..."
                print(f"    [!] {last_err}", file=sys.stderr)
                time.sleep(wait)
                continue
            else:
                last_err = f"HTTP {resp.status_code}: {resp.text}"
        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as e:
            last_err = str(e)

        wait = 2 ** attempt
        print(f"    [!] Gagal (percobaan {attempt}/{retries}): {last_err}", file=sys.stderr)
        if attempt < retries:
            print(f"        Coba lagi dalam {wait} detik...", file=sys.stderr)
            time.sleep(wait)

    raise RuntimeError(f"Gagal menerjemahkan batch setelah {retries} percobaan: {last_err}")


def translate_batch(texts, engine, api_key, model, source_lang, target_lang, rpm=12):
    """Dispatcher: panggil implementasi DeepL, Gemini, atau DeepSeek sesuai --engine."""
    if engine == "gemini":
        return translate_batch_gemini(texts, api_key, model, source_lang, target_lang, rpm=rpm)
    if engine == "deepseek":
        return translate_batch_deepseek(texts, api_key, model, source_lang, target_lang)
    return translate_batch_deepl(texts, api_key, source_lang, target_lang)


def parse_xhtml(raw_bytes: bytes):
    """
    Parse bytes XHTML jadi lxml tree, dengan preprocessing entitas HTML.
    Mengembalikan lxml ElementTree.
    """
    encoding = "utf-8"
    m = re.match(rb'<\?xml[^>]*encoding=["\']([^"\']+)["\']', raw_bytes)
    if m:
        encoding = m.group(1).decode("ascii", errors="ignore")

    try:
        text = raw_bytes.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = raw_bytes.decode("utf-8", errors="replace")

    text = fix_html_entities(text)
    data = text.encode("utf-8")

    parser = etree.XMLParser(recover=False, resolve_entities=True, load_dtd=False)
    try:
        tree = etree.parse(BytesIO(data), parser)
    except etree.XMLSyntaxError as e:
        print(f"    [!] Parsing ketat gagal ({e}), mencoba mode recover...", file=sys.stderr)
        parser = etree.XMLParser(recover=True, resolve_entities=True, load_dtd=False)
        tree = etree.parse(BytesIO(data), parser)

    return tree


def translate_file(input_path: Path, output_path: Path, engine, api_key, model,
                    source_lang, target_lang, rpm=12):
    """Terjemahkan satu file XHTML dan simpan ke output_path."""
    raw_bytes = input_path.read_bytes()
    tree = parse_xhtml(raw_bytes)
    root = tree.getroot()

    # 1) Bersihkan furigana & gabungkan base text <ruby> di seluruh dokumen
    flatten_all_ruby(root)

    # 2) Cari unit terjemahan (blok paragraf/list/heading, dst)
    block_elems = find_translation_units(root)
    units = []
    for elem in block_elems:
        xml_text = serialize_inner(elem)
        if xml_text.strip():
            units.append((elem, xml_text))

    if not units:
        print("  [!] Tidak ada teks untuk diterjemahkan, file disalin apa adanya.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(input_path, output_path)
        return 0

    batches = list(chunk_units(units, **BATCH_LIMITS[engine]))
    translated_count = 0
    for i, batch in enumerate(batches, start=1):
        original_texts = [xml_text for _, xml_text in batch]
        print(f"  [i] Batch {i}/{len(batches)} ({len(batch)} blok paragraf)...")
        translations = translate_batch(
            original_texts, engine, api_key, model, source_lang, target_lang, rpm=rpm
        )
        for (elem, _), translated_xml in zip(batch, translations):
            replace_inner(elem, translated_xml)
            translated_count += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doctype = tree.docinfo.doctype or None
    out_bytes = etree.tostring(
        tree,
        xml_declaration=True,
        encoding="utf-8",
        doctype=doctype,
    )
    output_path.write_bytes(out_bytes)

    return translated_count


def main():
    parser = argparse.ArgumentParser(description="Terjemahkan file/folder XHTML pakai DeepL, Gemini, atau DeepSeek API")
    parser.add_argument("input", help="Path file ATAU folder XHTML input (Jepang)")
    parser.add_argument("output", help="Path file/folder hasil terjemahan")
    parser.add_argument("--engine", choices=["deepl", "gemini", "deepseek"], default="deepl",
                         help="Mesin terjemahan yang dipakai (default: deepl)")
    parser.add_argument("--key", required=True, help="API key kamu (sesuai --engine)")
    parser.add_argument("--model", default=None,
                         help="Nama model (khusus --engine gemini/deepseek). Default: "
                              "gemini-3.6-flash untuk gemini, deepseek-v4-flash untuk deepseek "
                              "(pakai deepseek-v4-pro kalau mau kualitas lebih tinggi)")
    parser.add_argument("--target", default="ID", help="Bahasa target (default: ID)")
    parser.add_argument("--source", default="JA", help="Bahasa sumber (default: JA)")
    parser.add_argument("--skip-existing", action="store_true",
                         help="Lewati file yang outputnya sudah ada di folder tujuan "
                              "(hemat kuota API -- cocok buat lanjutin proses yang sempat gagal)")
    parser.add_argument("--rpm", type=int, default=12,
                         help="Batas request per menit (khusus --engine gemini, "
                              "default: 12 -- turunkan kalau masih kena limit 429)")
    parser.add_argument("--ext", nargs="+", default=[".xhtml", ".html", ".htm"],
                         help="Ekstensi file yang diterjemahkan saat mode folder "
                              "(default: .xhtml .html .htm)")
    args = parser.parse_args()

    model = args.model or DEFAULT_MODELS.get(args.engine)

    if args.engine == "gemini":
        print(f"[i] Menggunakan Gemini API, model: {model}")
    elif args.engine == "deepseek":
        print(f"[i] Menggunakan DeepSeek API, model: {model}")
    else:
        print(f"[i] Menggunakan endpoint: {get_deepl_endpoint(args.key)}")

    input_path = Path(args.input)
    output_path = Path(args.output)
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.ext}

    if input_path.is_dir():
        all_files = [p for p in input_path.rglob("*") if p.is_file()]
        page_files = [p for p in all_files if p.suffix.lower() in exts]
        other_files = [p for p in all_files if p.suffix.lower() not in exts]

        print(f"[i] Ditemukan {len(page_files)} file halaman ({', '.join(sorted(exts))}) "
              f"dan {len(other_files)} file lain di dalam folder.")

        total_translated = 0
        skipped = 0
        failed = []
        for idx, src in enumerate(page_files, start=1):
            rel = src.relative_to(input_path)
            dst = output_path / rel
            if args.skip_existing and dst.exists():
                print(f"[{idx}/{len(page_files)}] {rel} - dilewati (output sudah ada)")
                skipped += 1
                continue
            print(f"[{idx}/{len(page_files)}] {rel}")
            try:
                count = translate_file(src, dst, args.engine, args.key, model, args.source, args.target, rpm=args.rpm)
                total_translated += count
                print(f"  [v] {count} blok paragraf diterjemahkan -> {dst}")
            except Exception as e:
                print(f"  [!] GAGAL memproses {rel}: {e}", file=sys.stderr)
                failed.append(str(rel))

        for src in other_files:
            rel = src.relative_to(input_path)
            dst = output_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        if other_files:
            print(f"[i] {len(other_files)} file non-halaman disalin apa adanya.")

        print(f"[v] Selesai. Total {total_translated} blok paragraf diterjemahkan di seluruh folder.")
        print(f"[v] Hasil disimpan ke folder: {output_path}")
        if skipped:
            print(f"[i] {skipped} file dilewati karena outputnya sudah ada.")
        if failed:
            print(f"[!] {len(failed)} file GAGAL diproses:")
            for f in failed:
                print(f"    - {f}")

    else:
        print(f"[i] Memproses file: {input_path}")
        count = translate_file(input_path, output_path, args.engine, args.key, model, args.source, args.target, rpm=args.rpm)
        print(f"[v] Selesai. {count} blok paragraf diterjemahkan.")
        print(f"[v] Hasil disimpan ke: {output_path}")


if __name__ == "__main__":
    main()
    ##