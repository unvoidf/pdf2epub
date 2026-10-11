from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import os
import shutil
import subprocess
import re
import sys
import unicodedata
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")

DPI = 150
SCAN_DPI = 300    # taranmış sayfa: metin katmanı yok, OCR ve işçi bu görüntüyü kullanır
COVER_LONG_EDGE = 1680   # kapak okuma ekranına göre çizilir: 7 inç 300 ppi ekranın uzun kenarı
COVER_QUALITY = 80       # kapak JPEG kalitesi: ölçümde 6-7 inç e-mürekkep ekranda fark üretmiyor
SCAN_CHARS = 20   # bir sayfanın metin katmanı bundan kısaysa sayfa taranmıştır
OCR_LANG = "tur"
OCR_WORKERS = 4
BATCH = 10
REWRITE_MIN_CHARS = 200   # bundan kısa sayfada benzerlik gürültülü: kural uygulanmaz
REWRITE_RATIO = 0.80      # metin katmanlı sayfa bundan az benziyorsa işçi sayfayı baştan yazmıştır
TR_FIX = str.maketrans("ýþðÝÞÐ", "ışğİŞĞ")
SKILL_DIR = Path(__file__).resolve().parent.parent


def fix_tr(s):
    return s.translate(TR_FIX)


def nfc(s):
    # Metni NFC'ye normalize et: birleşik ve ayrışık yazım tek biçime iner. Kanonik eşdeğerliktir,
    # harfleri değiştirmez. Kaynak PDF bazı harfleri ayrışık verir (u + U+0308), bazılarını birleşik
    # (ç, ş); öyle kalırsa okuyucuda arama ve kopyalama tutarsız olur. Sayfa metnini yazan ve okuyan
    # her yer bu işlevden geçer. Geçmezse chapters.txt ile sayfa başlığı farklı biçimde olur ve
    # demote eşleşmeyi kaçırır: başlık sessizce paragrafa iner.
    return unicodedata.normalize("NFC", s)


def line_text(chars):
    # chars: (char, x0, x1). Drop a space that the next glyph starts on (a justification artifact inside a word).
    # Keep it before a capital: kerned capitals overlap real gaps too, and a glued word is worse than a stray space.
    return "".join(
        c for i, (c, x0, x1) in enumerate(chars)
        if not (c == " " and i + 1 < len(chars) and chars[i + 1][1] < x0 + 0.25 * (x1 - x0) and not chars[i + 1][0].isupper())
    )


def spans_text(spans):
    # spans: (text, italic). Keep the whitespace between spans and put the asterisks inside it.
    out = ""
    for t, italic in spans:
        m = t.strip()
        out += t.replace(m, f"*{m}*", 1) if italic and m else t
    return re.sub(r" {2,}", " ", out).strip()


def tsv_lines(tsv):
    # Tesseract TSV kelimelerini (blok, paragraf, satır) anahtarıyla satırlara toplar.
    # Kutu pikseldir; punto'ya çevrilir ki satır yüksekliği metin katmanının punto'suyla aynı ölçekte olsun.
    scale = 72 / SCAN_DPI
    groups = {}
    for raw in tsv.splitlines():
        c = raw.split("\t", 11)
        if len(c) < 12 or c[0] != "5" or not c[11].strip():
            continue
        try:
            left, top, w, h = (float(v) for v in c[6:10])
        except ValueError:
            continue
        groups.setdefault((c[1], c[2], c[3], c[4]), []).append((c[11].strip(), left, top, left + w, top + h, h))
    out = []
    for key, ws in groups.items():
        ws.sort(key=lambda w: w[1])
        out.append({
            "text": fix_tr(" ".join(w[0] for w in ws)),
            "x0": min(w[1] for w in ws) * scale,
            "x1": max(w[3] for w in ws) * scale,
            "y0": min(w[2] for w in ws) * scale,
            "y1": max(w[4] for w in ws) * scale,
            # yükseklik harf boyu değil: yalnız üst bilgi/alt bilgi satırını seçmeye yarar, başlığa yetmez
            "size": round(sorted(w[5] for w in ws)[len(ws) // 2] * scale, 1),
            "key": key[:3],      # (sayfa, blok, paragraf): satır numarası hariç, birleştirme kararı için
        })
    out.sort(key=lambda l: (l["y0"], l["x0"]))
    # tesseract geniş boşluklu bir satırı iki gruba bölebilir: yalnız aynı paragraftaki, aynı taban
    # çizgisindeki ve arasında boşluk bırakmayan parçalar birleşir. Aksi halde iki sütun ya da üst bilgi
    # aynı satıra yapışır.
    merged = []
    for l in out:
        if (merged and l["key"] == merged[-1]["key"] and abs(l["y0"] - merged[-1]["y0"]) < 2
                and l["x0"] > merged[-1]["x0"] and l["x0"] - merged[-1]["x1"] < (l["y1"] - l["y0"])):
            merged[-1]["text"] += " " + l["text"]
            merged[-1]["x1"] = max(merged[-1]["x1"], l["x1"])
            merged[-1]["y1"] = max(merged[-1]["y1"], l["y1"])
        else:
            merged.append(dict(l))
    return merged


def join_lines(lines):
    out = ""
    for line in map(str.strip, lines):
        if not out:
            out = line
        elif out.endswith("\xad"):
            out = out[:-1] + line
        elif re.search(r"\w-$", out) and line[:1].islower():
            # shortcut: satır sonu tiresi küçük harf öncesinde her zaman silinir, gerçek tire kaybolabilir; işçi resimden düzeltir
            out = out[:-1] + line
        elif re.search(r"\w-$", out) and line[:1].isupper():
            out += line
        else:
            out += " " + line
    # a soft hyphen at the very end stays a hyphen: to_html joins it with the next page
    return re.sub("\xad$", "-", out).replace("\xad", "")


def strip_running(pages, body_size):
    def key(l):
        return re.sub(r"\d+", "#", l["text"].lower())

    def edges(pg):
        if len(pg) < 2:
            return []
        return [l for l in (pg[0], pg[-1]) if l["size"] < body_size * 1.15]

    count = Counter(k for pg in pages for k in {key(l) for l in edges(pg)})
    return [
        [l for l in pg if not any(l is e and count[key(e)] >= 5 for e in edges(pg))]
        for pg in pages
    ]


def is_short(l, lines, left):
    right = max((m["x1"] for m in lines if "x1" in m), default=0)
    return "x1" in l and l["x1"] < right - 0.15 * (right - left)


def join_dropcap(lines, body_size):
    # A heading-size line of one letter with body lines beside it (right of it, same height) is a drop cap:
    # its letter starts the first beside line, and the beside lines take the cap's left edge (they are not indented).
    for cap in lines:
        t = cap["text"].strip()
        if "y1" not in cap or len(t) != 1 or not t.isalpha() or cap["size"] < body_size * 1.15:
            continue
        beside = [l for l in lines if l is not cap and "y1" in l and l["size"] < body_size * 1.15
                  and l["x0"] >= cap["x1"] - 15 and l["y0"] < cap["y1"] and l["y1"] > cap["y0"]]
        if not beside:
            continue
        first = min(beside, key=lambda l: l["y0"])
        return [{**l, "text": t + l["text"], "drop": True} if l is first else {**l, "x0": cap["x0"]} if l in beside else l
                for l in lines if l is not cap]
    return lines


def page_blocks(lines, body_size, left, cont=True, headings=True):
    blocks = []  # [is_heading, [lines]]
    prev = None
    for l in lines:
        # OCR satır yüksekliği harflere göre oynar (ç, ğ, ş, İ, tırnak): taranmış sayfada başlığı işçi seçer
        head = l["size"] >= body_size * 1.15 if headings else False
        # a line that stops well short of the right margin ends its paragraph (unindented books)
        end = prev is not None and is_short(prev, lines, left)
        prev = l
        if head and blocks and blocks[-1][0]:
            blocks[-1][1].append(l["text"])
        elif not head and blocks and not blocks[-1][0] and l["x0"] <= left + 4 and not end:
            blocks[-1][1].append(l["text"])
        else:
            blocks.append([head, [l["text"]]])
    out = [("# " if h else "") + join_lines(t) for h, t in blocks]
    # Tek satırlık sayfa (ithaf, tek satırlık bölüm sonu) kendini önceki sayfanın devamı sanmasın:
    # o sayfada "left" satırın kendi x0'ıdır ve koşul boşuna doğru çıkar.
    if cont and len(lines) >= 2 and blocks and not blocks[0][0] and lines[0]["x0"] <= left + 4 and not lines[0].get("drop"):
        out.insert(0, "<<")
    return out


def pdf_digest(pdf):
    # Damgaya yazılan içerik imzası: dosya adı ve sayfa sayısı aynı olan başka bir kitap
    # sessizce atlanmasın. Parça parça okunur, bütün dosya belleğe alınmaz.
    h = hashlib.sha256()
    with open(pdf, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def is_scanned(lines, page):
    # Bir sayfanın kullanılabilir metin katmanı yoktur: ya hiç metin satırı yoktur, ya da
    # görüntü taşır ve metni çok kısadır. Kısa metinli ama görüntüsüz bir sayfa metin
    # sayfasıdır (ör. yalnız amblem adı yazan sayfa): başlık işareti alabilmeli.
    return sum(len(l["text"]) for l in lines) < SCAN_CHARS and (bool(page.get_images()) or not lines)


def extract(pdf, force=False):
    try:
        import fitz
    except ImportError:
        raise SystemExit("PyMuPDF is missing. Run the check command.")
    work = pdf.with_suffix(".pdf2epub")
    if not pdf.exists():
        raise SystemExit(f"File not found: {pdf}")
    try:
        doc = fitz.open(pdf)
    except Exception as e:                     # bozuk dosya: PyMuPDF izi yerine anlaşılır mesaj
        raise SystemExit(f"Cannot open the PDF: {e}")
    if doc.needs_pass:
        raise SystemExit(f"{pdf} needs a password.")
    if (work / "extract.ok").exists() and not force:
        parts = (work / "extract.ok").read_text(encoding="utf-8").strip().split()
        # Damga: "sayfa_sayısı içerik_imzası". Eski klasörde yalnız sayfa sayısı yazar (tek alan):
        # o zaman eski davranış sürer. İmza, adı ve sayfa sayısı aynı olan başka bir dosyanın
        # sessizce atlanmasını önler.
        if parts:
            if parts[0] != str(doc.page_count):
                raise SystemExit(f"The work folder holds the pages of another book ({parts[0]} pages, this PDF has {doc.page_count}). Run extract with --force.")
            if len(parts) > 1 and parts[1] != pdf_digest(pdf):
                raise SystemExit(f"The work folder holds the pages of another file with {doc.page_count} pages. Run extract with --force.")
        return work, None, None, None
    if force:
        # Damgayı ilk iş sil: --force yarıda kalırsa klasör geçerli sayılmasın, bir sonraki extract baştan çalışsın.
        (work / "extract.ok").unlink(missing_ok=True)
    work.mkdir(exist_ok=True)
    pending = work / ".pending"    # taranmış sayfa PNG'leri burada bekler; bütün kitap bellekte tutulmaz
    shutil.rmtree(pending, ignore_errors=True)
    pages, todo, scan, blank, size_chars = {}, set(), set(), [], Counter()
    for n, page in enumerate(doc, 1):
        # Uzun tarama sessiz kalmasın: ilerleme stderr'e yazılır, stdout sözleşmesi bozulmaz.
        if n % 25 == 0:
            print(f"  reading {n}/{doc.page_count}", file=sys.stderr, flush=True)
        lines = []
        for b in page.get_text("rawdict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    sp["text"] = line_text([(c["c"], c["bbox"][0], c["bbox"][2]) for c in sp["chars"]])
                spans = [sp for sp in ln["spans"] if sp["text"].strip()]
                if not spans:
                    continue
                text = spans_text([(sp["text"], bool(sp["flags"] & 2)) for sp in ln["spans"]])
                size = round(max(spans, key=lambda sp: len(sp["text"]))["size"], 1)
                text = fix_tr(text)
                if lines and abs(ln["bbox"][1] - lines[-1]["y0"]) < 2 and ln["bbox"][0] > lines[-1]["x0"]:
                    # some PDFs emit each word of a line as its own line
                    lines[-1]["text"] += " " + text
                    lines[-1]["size"] = max(lines[-1]["size"], size)
                    lines[-1]["x1"] = ln["bbox"][2]
                else:
                    lines.append({"text": text, "x0": ln["bbox"][0], "size": size, "y0": ln["bbox"][1], "x1": ln["bbox"][2], "y1": ln["bbox"][3]})
                for sp in spans:
                    size_chars[round(sp["size"], 1)] += len(sp["text"])
        if not lines and not page.get_images() and not page.get_drawings():
            # metin yok, resim yok, çizim yok: sayfa boş. Kitaba girmez, ama sessizce kaybolmasın.
            blank.append(f"{n:04d}")
            continue
        if is_scanned(lines, page):
            # Metin katmanı yok: sayfa taranmıştır. Tesseract taslağı yazar, işçi resimden düzeltir.
            # Sayfa 300 dpi çizilir ve diske yazılır: bütün kitabın PNG'sini bellekte tutmak büyük
            # taramada bellek şişirir. OCR dosyayı okuyup yine stdin'den verir.
            pending.mkdir(exist_ok=True)
            (pending / f"{n:04d}.png").write_bytes(page.get_pixmap(dpi=SCAN_DPI).tobytes("png"))
            todo.add(n)
            scan.add(n)
            continue
        pages[n] = lines
    failed = []
    if todo:
        tess = find_tesseract()
        if not tess or OCR_LANG not in ocr_langs(tess):
            # Metin katmanı olan kitap OCR'sız da çevrilir: o sayfaların dosyası boş kalır, işçi resimden yazar.
            # Saf taramada taslak şart: boş dosyalarla bütün kitabı işçiye yazdırmak pahalı ve denetimsiz olur.
            if not pages:
                raise SystemExit(f"{len(todo)} pages have no text layer. Run the check command for the OCR program.")
            failed = sorted(todo)
        else:
            print(f"  ocr: {len(todo)} pages", file=sys.stderr, flush=True)
            with ThreadPoolExecutor(OCR_WORKERS) as pool:
                out = list(pool.map(lambda n: ocr_tsv(pending / f"{n:04d}.png", tess), todo))
            failed = [n for n, tsv in zip(todo, out) if tsv is None]
            for n, tsv in zip(todo, out):
                lines = tsv_lines(tsv) if tsv is not None else []
                pages[n] = lines
                for l in lines:
                    size_chars[l["size"]] += len(l["text"])
    for n in todo:
        pages.setdefault(n, [])
    body_size = size_chars.most_common(1)[0][0] if size_chars else 0
    if force:
        # every check passed: clear every file extract writes, so no stale page stays.
        # (the stamp went first, at the top of extract, so a half-done run is never taken for valid)
        for pat in ("[0-9]*.png", "[0-9]*.md", "[0-9]*.md.orig", "[0-9]*.md.rejected", "img-*.png", "cover.jpg", "book.html", "roles.txt", "scan.txt", "toc.txt", "chapters.txt"):
            for f in work.glob(pat):
                try:
                    f.unlink()
                except OSError as e:
                    # Kilitli dosya (açık bir görüntüleyici) ya da izin: yarı silinmiş klasör bırakma, söyle.
                    raise SystemExit(f"Cannot clear the work folder: {f} ({e.strerror}). Close the program that holds it and run extract again.")
    nums = list(pages)
    stripped = strip_running([pages[n] for n in nums], body_size)
    ended_short = True   # ilk sayfanın öncesi yoktur: ilk sayfa paragraf devamı olamaz
    for k, (n, lines) in enumerate(zip(nums, stripped), 1):
        if k % 25 == 0:
            print(f"  writing {k}/{len(nums)}", file=sys.stderr, flush=True)
        page = doc[n - 1]
        # 10th percentile, not the mode: pages of short indented dialogue have an indented mode
        x0s = sorted(l["x0"] for l in lines)
        # OCR font sizes drift between pages: on full pages measure the body size on the page itself
        size = Counter()
        for l in lines:
            size[l["size"]] += len(l["text"])
        pbody = size.most_common(1)[0][0] if len(lines) >= 10 else body_size
        left = x0s[len(x0s) // 10] if x0s else 0
        # no "<<" when the previous page with text ended on a short line: that paragraph is closed
        if n not in scan:
            lines = join_dropcap(lines, pbody)
        blocks = page_blocks(lines, pbody, left, not ended_short, n not in scan) if x0s else []
        if lines:
            ended_short = is_short(lines[-1], lines, left)
        # Metin odaklı sistem: sayfa içi resim EPUB'a girmez (kullanıcı kararı, 2026-10-11).
        # Kapak roles.txt'teki "cover" sayfasından seçilir; taranmış sayfada iç resim zaten çıkmaz.
        if n in todo:
            os.replace(pending / f"{n:04d}.png", work / f"{n:04d}.png")
        else:
            page.get_pixmap(dpi=DPI).save(work / f"{n:04d}.png")
        page_text = nfc("\n\n".join(blocks))
        for ext in (".md", ".md.orig"):
            (work / f"{n:04d}{ext}").write_text(page_text, encoding="utf-8")
    shutil.rmtree(pending, ignore_errors=True)
    # Hangi sayfaların metin katmanı yoktu: build benzerlik kuralını yalnız metin katmanlı sayfalara uygular
    (work / "scan.txt").write_text("".join(f"{n:04d}\n" for n in sorted(scan)), encoding="utf-8")
    (work / "extract.ok").write_text(f"{doc.page_count} {pdf_digest(pdf)}", encoding="utf-8")   # damga: başka kitabın sayfaları kalmasın
    return work, scan, failed, blank


def batches(work, size=BATCH):
    names = sorted(p.name[: -len(".md.orig")] for p in work.glob("[0-9]*.md.orig"))
    return [f"{names[i]}-{names[min(i + size, len(names)) - 1]}" for i in range(0, len(names), size)]


PROSE_WORD = re.compile(r"[^\W\d_]{2,}")
NOTE = re.compile(r"^\*{1,3}\s+[^\s*]")   # dipnot satırı: yıldız işareti ve ardından metin ("* * *" sahne arası değildir)


def prose_chars(text):
    # Paragraf gibi görünen blokların karakter sayısı. Kısa bloklar ve seyrek uzun bloklar sayılmaz:
    # tesseract bir fotoğrafın tarama gürültüsünü uzun ve kelimesiz satırlar olarak yazar.
    total = 0
    for b in (x.strip() for x in text.split("\n\n")):
        words = len(PROSE_WORD.findall(b))
        if words >= 5 and (len(b) < 200 or words * 25 >= len(b)):
            total += len(b)
    return total


def norm_len(text):
    # Tablo boruları, tireler ve boşluklar bilgi değildir: tabloya çevrilen sayfa uzar ama metin kaybı olmaz.
    return len(re.sub(r"[|\s-]", "", text))


def similar(a, b):
    # autojunk kapalı olmalı: varsayılan sezgi uzun metinde sık geçen karakterleri (boşluk, a, e)
    # çöp sayıp eşleşmeyi çökertiyor — yalnız üst bilgi satırı silinen bir sayfa 0.02 çıkıyordu.
    return difflib.SequenceMatcher(
        None, re.sub(r"\s+", " ", a).strip(), re.sub(r"\s+", " ", b).strip(), autojunk=False).ratio()


def guard(orig, new, ocr=False):
    if new is None:
        return False
    if new == orig:
        # Değişmemiş sayfa: benzerlik hesabı gereksiz. Ölçüm: gerçek kitap sayfasında ~124 ms,
        # 400 sayfalık kitapta ~50 sn (çok tekrarlı metinde daha da fazla).
        return True
    if orig.strip() and not new.strip():
        # Şekil sayfasının tamamı gürültüyse boşaltmak doğrudur. Başlık ya da resim satırı varsa bu kayıptır.
        blocks = [b.strip() for b in orig.split("\n\n") if b.strip()]
        if any(b.startswith(("#", "![](")) for b in blocks):
            return False
        if prose_chars(orig) == 0:
            return True
        if not ocr:
            return False
        # Taranmış sayfada taslak gürültülüdür: tesseract bir çizimin karalamasını "kelime" sayar, o yüzden
        # prose_chars burada yanıltır (ölçüm: 0009'da 79). Boşaltma kararını aşağıdaki gürültü payı verir.
    if len(orig) < 50:
        return True
    if abs(len(new) - len(orig)) <= max(50, 0.15 * len(orig)):
        # Uzunluk yakın: işçi sayfayı kendi kelimeleriyle baştan yazmış olabilir. Metin katmanlı
        # sayfada işçi yalnız resimden farkı düzeltir, sayfayı yeniden yazmaz. Ölçüm: 325 meşru
        # düzenlemede en düşük benzerlik 0.956, yani 0.80 tabanı rahat. Taranmış sayfa kuralın
        # dışında: taslak gürültülüdür, işçi orada çok değiştirir (ölçümde 0.162'ye kadar meşru).
        if not ocr and len(orig) >= REWRITE_MIN_CHARS and similar(orig, new) < REWRITE_RATIO:
            return False
        return True
    # İşçi dağınık tablo satırlarını tabloya çevirdiyse metin uzar: boruyu ve boşluğu saymadan karşılaştır
    if any(is_table(b) for b in new.split("\n\n")) and abs(norm_len(new) - norm_len(orig)) <= max(50, 0.15 * norm_len(orig)):
        return True
    # İşçi yalnız gürültüyü sildiyse metin kaybı yoktur: kabul et. Paragraf da gittiyse geri al.
    # Pay 150 karakter: ölçümde şekil sayfasında işçi 723 karakterin 643'ünü sildi ve bu doğruydu (0033),
    # tablo artığı silinen 0080'de 175 karakter. Yarım paragraf silen işçi yine geri alınır.
    return len(new) < len(orig) and len(orig) - len(new) <= len(orig) - prose_chars(orig) + 150


ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*]+)(?<=\S)\*(?!\*)")
DELIM = re.compile(r"^\|[\s:|-]+\|$")
HEAD = re.compile(r"#{1,6}\s+(.*)")           # bir başlık bloğunun metni (# işaretleri dışında)
DROP_ROLES = {"back-cover", "ads", "flap"}   # bu roller kitabın metni değildir, EPUB'a girmez


def parse_roles(text):
    # roles.txt: her satırda dört haneli sayfa numarası ve rol ("0002 back-cover")
    # Baştaki BOM kırpılır: Windows araçları dosyayı imzayla yazar ve ilk satır sessizce düşerdi.
    roles = {}
    for line in text.removeprefix("\ufeff").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"\d{4}", parts[0]):
            roles[parts[0]] = parts[1].strip().lower()
    return roles


def drop_pages(roles, keep=DROP_ROLES):
    # Yapı geçişi yalnız kitabın başındaki ve sonundaki sayfalara bakar. Düşürme kararı da yalnız
    # gövdenin dışında verilir: yanlış bir etiket böylece gövdeden metin silemez. "?" taşıyan
    # etiket emin değildir, ona dokunulmaz.
    body = [n for n, r in roles.items() if r.startswith("body")]
    if not body:
        return []
    lo, hi = min(body), max(body)
    return sorted(n for n, r in roles.items() if "?" not in r and r.strip() in keep and (n < lo or n > hi))


def is_table(b):
    # Borulu satırlar tablo sayılır ancak ikinci satır ayırıcı olduğunda (|---|---|).
    # Ayırıcı yoksa bu bir tablo değil, borçlu bir paragraftır: yanlış yere tablo kurmayalım.
    ls = b.split("\n")
    return len(ls) >= 2 and ls[0].lstrip().startswith("|") and bool(DELIM.match(ls[1].strip())) and "-" in ls[1]


def parse_chapters(text):
    # chapters.txt: her satırda dört haneli sayfa numarası ve başlığın kendisi ("0005 I. Bölüm").
    # Aynı sayfada birden çok başlık seçilebilir. Boş dosya geçerlidir: kitapta başlık yok demektir.
    # Baştaki BOM kırpılır: Windows araçları dosyayı imzayla yazar ve ilk bölüm sessizce düşerdi.
    # Metin NFC'ye normalize edilir: sayfa başlığı da NFC olduğu için ayrışık yazılmış bir seçim
    # eşleşmeyi kaçırmaz.
    chapters = {}
    for line in nfc(text).removeprefix("\ufeff").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"\d{4}", parts[0]):
            chapters.setdefault(parts[0], []).append(parts[1].strip())
    return chapters


def demote(text, keep):
    # keep: bu sayfada başlık kalacak blokların metni. None: dosya yok, her "# " bloğu başlık kalır.
    # Seçilmeyen "# " bloğu düz paragraf olur. Döner: (yeni metin, eşleşmeyen seçim listesi).
    if keep is None:
        return text, []
    found, out = set(), []
    for b in re.split(r"\n\s*\n", text):
        s = b.strip()
        m = HEAD.match(s)
        if m and "\n" not in s:               # başlık bloğu tek satırdır; tablo başlık değildir
            t = re.sub(r"\s+", " ", m.group(1)).strip()
            if t in keep:
                found.add(t)
            else:
                b = m.group(1)                # "# " işareti gider: blok paragraf olur
        out.append(b)
    return "\n\n".join(out), sorted(set(keep) - found)


def table_cells(line):
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def table_cell_html(c):
    # <br> önce ayrılır: html.escape onu &lt;br&gt; yapmasın. Hücre içi italik korunur.
    parts = re.split(r"<br\s*/?>", c.replace("\\|", "|"))
    return "<br/>".join(ITALIC.sub(r"<i>\1</i>", html.escape(p, quote=False)) for p in parts)


def table_html(b):
    rows = [l for l in b.split("\n") if l.strip()]
    head = table_cells(rows[0])
    width = len(head)
    # Eksik hücre boş kalır, fazlası kırpılır: tek satır kayması tablonun tamamını bozmasın
    body = [(r + [""] * width)[:width] for r in (table_cells(l) for l in rows[2:])]
    th = "".join(f"<th>{table_cell_html(c)}</th>" for c in head)
    tb = "".join("<tr>" + "".join(f"<td>{table_cell_html(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table>"


def to_html(pages):
    def kind(b):
        b = b.strip()
        if re.fullmatch(r"\*\s*\*\s*\*", b):
            return "hr"
        if re.match(r"#{1,6}\s", b):
            return "h1"
        if re.fullmatch(r"!\[\]\([^)]*\)", b):
            return "drop"   # metin odaklı sistem: resim bloğu EPUB'a girmez
        return "table" if is_table(b) else "p"

    blocks = []
    for page in pages:
        bs = [b.strip() for b in re.split(r"\n\s*\n", page) if b.strip()]
        if bs and re.fullmatch(r"<<\s*", bs[0]):
            bs = bs[1:]
            # Sayfanın altındaki dipnot satırları süren paragrafın altındadır: birleşecek blok onların üstündedir.
            i = len(blocks) - 1
            while i >= 0 and NOTE.match(blocks[i]):
                i -= 1
            if bs and i >= 0 and kind(blocks[i]) == "p" and kind(bs[0]) == "p":
                blocks[i] = join_lines([blocks[i], bs.pop(0)])
        blocks += [b for b in bs if not re.fullmatch(r"<<\s*", b)]   # ortada kalan işaret metne girmesin
    out = []
    for b in blocks:
        k = kind(b)
        if k == "hr":
            out.append("<hr/>")
        elif k == "drop":
            continue
        elif k == "table":
            out.append(table_html(b))
        else:
            text = re.sub(r"^#{1,6}\s+", "", b) if k == "h1" else b
            t = ITALIC.sub(r"<i>\1</i>", html.escape(text, quote=False))
            out.append(f"<{k}>{t}</{k}>")
    return "\n".join(out)


def bitmap_dpi(images, has_ink):
    # images: [(piksel_gen, piksel_yük, kutu_gen_pt, kutu_yük_pt)]. has_ink: sayfada metin ya da
    # vektör çizim var mı. Sayfa yalnız resimlerden oluşuyorsa en keskin resmin sayfadaki
    # yoğunluğunu döner. Metin/vektör varsa None: onlar çözünürlükten bağımsızdır, sınır koymak
    # yazıyı bulanıklaştırır. Resim de yoksa None.
    if has_ink:
        return None
    best = 0.0
    for px_w, px_h, box_w, box_h in images:
        if box_w > 0 and box_h > 0:
            best = max(best, px_w * 72 / box_w, px_h * 72 / box_h)
    return best or None


def source_dpi(page):
    # Kapak sayfasının kendi içeriğinin çözünürlüğü. Metin ve vektör çizim çözünürlükten bağımsız
    # olduğu için onlar varsa sınır yoktur (None).
    has_ink = bool(page.get_text().strip()) or bool(page.get_drawings())
    images = [(im["width"], im["height"], im["bbox"][2] - im["bbox"][0], im["bbox"][3] - im["bbox"][1])
              for im in page.get_image_info()]
    return bitmap_dpi(images, has_ink)


def cover_dpi(long_pt, native_dpi=None):
    # Kapağın uzun kenarını COVER_LONG_EDGE piksele getiren çizim yoğunluğu. Taranmış sayfanın
    # yoğunluğunu geçmez: küçük bir sayfayı hedefe şişirmek yalnız bayt harcar, ayrıntı eklemez.
    # native_dpi: sayfa tek bir bitmap ise o resmin yoğunluğu. Kaynağın üstüne çıkmak ayrıntı
    # eklemez, yalnız dosyayı büyütür: kapak EPUB'un tek resmidir, boyutun tamamını o belirler.
    # None: metin/vektör var ya da bilinmiyor; o zaman sınır yalnız tavan ve hedeftir.
    # Tam sayı döner: PyMuPDF dpi olarak float kabul etmez.
    if long_pt <= 0:
        return DPI
    dpi = min(SCAN_DPI, round(COVER_LONG_EDGE * 72 / long_pt))
    if native_dpi:
        dpi = min(dpi, max(1, round(native_dpi)))
    return dpi


def cover_image(pdf, work, name):
    # Kapağı hedef çözünürlükte yeniden çiz ve JPEG yaz. Ham sayfa PNG'si kayıpsızdır ve okuma
    # ekranının gösterdiğinden çok daha büyüktür; kapak EPUB'un tek resmi olduğu için dosya
    # boyutunun tamamını o belirler. Yeniden çizmek kayıp değildir: sayfa vektör metni de taşır,
    # PDF'in gömülü resmini kullanmak ise yanlıştır (o resim sayfanın yalnız bir katmanıdır).
    # Dönen ad pandoc'a verilir; çizilemezse None döner ve çağıran ham PNG'ye düşer.
    import fitz
    try:
        n = int(name.split(".")[0])
        with fitz.open(pdf) as doc:
            page = doc[n - 1]
            dpi = cover_dpi(max(page.rect.width, page.rect.height), source_dpi(page))
            page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB).save(str(work / "cover.jpg"), "jpeg", jpg_quality=COVER_QUALITY)
    except Exception:
        # Eksik sayfa, bozuk resim, kilitli dosya: kapak yüzünden kitap hiç üretilmesin olmaz.
        return None
    return "cover.jpg"


def build(pdf, title, author):
    work = pdf.with_suffix(".pdf2epub")
    if not (work / "extract.ok").exists():
        raise SystemExit("Run the extract command first.")
    pandoc = find_pandoc()
    if not pandoc:
        raise SystemExit("pandoc is missing. Run the check command.")
    roles = parse_roles((work / "roles.txt").read_text(encoding="utf-8")) if (work / "roles.txt").exists() else {}
    # scan.txt yoksa (eski çalışma klasörü) hangi sayfanın taranmış olduğu bilinmez. O zaman benzerlik
    # kuralı uygulanmaz: bilinmeyen bir taranmış sayfaya uygulamak işçinin meşru düzeltmesini geri alırdı.
    scan = set((work / "scan.txt").read_text(encoding="utf-8").split()) if (work / "scan.txt").exists() else None
    drop = set(drop_pages(roles))
    cover = next((f"{n}.png" for n in sorted(roles) if roles[n].rstrip("?").strip() == "cover" and (work / f"{n}.png").exists()), None)
    # Kapak EPUB'un tek resmidir: boyutun tamamını o belirler. Onu okuma ekranının çözünürlüğünde
    # bir JPEG olarak yeniden üret (ölçüm: EPUB 4–10× küçülür, ekranda görünür fark yok). Çizilemezse PNG kalır.
    cover = (cover_image(pdf, work, cover) or cover) if cover else None
    # chapters.txt: toc ajanının seçtiği başlıklar. Dosya varsa yalnız o başlıklar başlık kalır, sayfadaki
    # öteki "# " blokları paragrafa iner. Dosya yoksa ajan çalışmamıştır: her "# " bloğu başlık kalır.
    chapters = parse_chapters((work / "chapters.txt").read_text(encoding="utf-8")) if (work / "chapters.txt").exists() else None
    flagged, changed, pages, nums, dropped, missing = [], 0, [], [], [], []
    kept = 0
    for orig in sorted(work.glob("[0-9]*.md.orig")):
        md = orig.with_suffix("")
        if md.stem in drop:
            dropped.append(md.stem)
            continue
        o = nfc(orig.read_text(encoding="utf-8"))
        n = nfc(md.read_text(encoding="utf-8")) if md.exists() else None
        if not guard(o, n, ocr=scan is None or md.stem in scan):
            # işçinin sürümü kaybolmasın: geri almadan önce sakla, yoksa ne yaptığı görülemez
            if n is not None:
                md.with_suffix(".md.rejected").write_text(n, encoding="utf-8")
            md.write_text(o, encoding="utf-8")
            flagged.append(md.stem)
            n = o
        elif n != o:
            # Sayfa hâlâ geri alınmış durumdaysa kanıtı silme: kullanıcı işçinin sürümünü sonra inceleyebilsin.
            # İşçi sayfayı yeniden yazdıysa eski kanıt bayattır, silinir.
            md.with_suffix(".md.rejected").unlink(missing_ok=True)
        changed += n != o
        ks = None if chapters is None else set(chapters.get(md.stem, []))
        text, miss = demote(n, ks)
        kept += 0 if ks is None else len(ks) - len(miss)
        missing += [(md.stem, t) for t in miss]
        pages.append(text)
        nums.append(md.stem)
    (work / "book.html").write_text(to_html(pages), encoding="utf-8")
    epub = pdf.with_suffix(".epub").resolve()
    covers = sorted(work.glob("[0-9]*.png"))
    if not covers:
        raise SystemExit("No page image in the work folder. Run the extract command again.")
    cmd = [pandoc, "book.html", "-f", "html", "-t", "epub3", "-o", str(epub),
           "--metadata", f"title={title or pdf.stem.strip()}", "--metadata", "lang=tr",
           f"--epub-cover-image={cover or covers[0].name}", "--toc"]
    if author:
        cmd += ["--metadata", f"author={author}"]
    try:
        subprocess.run(cmd, cwd=work, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        # pandoc'un kendi hatası görünmeli: eksik resim, bozuk metadata, kilitli dosya
        msg = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SystemExit("pandoc failed: " + (msg[-1] if msg else "close the EPUB file if a reader has it open."))
    print(f"epub: {epub}")
    print(f"pages: {len(pages)}  changed: {changed}  flagged: {len(flagged)}")
    print("chapters: " + ("every heading kept (no chapters.txt)" if chapters is None else str(kept)))
    if missing:
        # Ajan başlığı kopyalarken yanlış yazdıysa o başlık sessizce paragrafa iner: satırı göster.
        print("unmatched chapters: " + " ".join(f"{n}:{t}" for n, t in missing))
    print("flagged pages: " + (" ".join(flagged) or "none"))
    if dropped:
        print("dropped pages: " + " ".join(f"{n} {roles.get(n, '').strip()}" for n in dropped))
    # İki dosyası da boş sayfa: OCR programı ya da işçi oraya hiç metin yazmadı, kitapta boş kalır
    empty = [num for num, t in zip(nums, pages) if not t.strip()]
    if empty:
        print("empty pages: " + " ".join(empty))


def toc_candidates(work, roles, title, author):
    # Başlık adayları: kitaptaki her "# " blok. build'in kullanacağı metin okunur (işçinin sürümü, yoksa
    # extract'in sürümü). Düşürülen sayfalar listelenmez: o sayfalar kitaba girmiyor, başlıkları da giremez.
    # Sayfa başına karakter sayısı ön/arka sayfayı gövdeden ayırmaya yarar: künye ve iç kapak kısadır.
    drop = set(drop_pages(roles))
    out = ["# Table of contents candidates: one line for each heading block of the book.",
           f"# The book: {title or '?'} / {author or '?'}",
           "# page  role      chars  text"]
    n = 0
    for orig in sorted(work.glob("[0-9]*.md.orig")):
        md = orig.with_suffix("")
        if md.stem in drop:
            continue
        src = md if md.exists() else orig
        text = nfc(src.read_text(encoding="utf-8"))
        for b in (x.strip() for x in re.split(r"\n\s*\n", text)):
            m = HEAD.match(b)
            if m and "\n" not in b:
                out.append(f"{md.stem}  {roles.get(md.stem, '-'):<9} {len(text):5d}  {m.group(1)}")
                n += 1
    return "\n".join(out) + "\n", n


def install_cmd(tool, platform, python):
    winget = "winget install --id {} -e --accept-source-agreements --accept-package-agreements"
    if tool == "pymupdf":
        py = f'"{python}" -m pip install'
        if platform == "win32":
            return py + " pymupdf"
        # PEP 668: a system Python refuses a plain pip install, so try --user, then override
        return f"{py} --user pymupdf || {py} --user --break-system-packages pymupdf"
    if tool == "ocrlang":
        # Türkçe OCR modeli depoya girmez: skill klasörüne indirilir, oradan okunur
        url = f"https://github.com/tesseract-ocr/tessdata_best/raw/main/{OCR_LANG}.traineddata"
        path = SKILL_DIR / "tessdata" / f"{OCR_LANG}.traineddata"
        if platform == "win32":
            return f'mkdir "{path.parent}" ; curl.exe -L -o "{path}" {url}'
        return f'mkdir -p "{path.parent}" && curl -L -o "{path}" {url}'
    if platform == "win32":
        ids = {"python": "Python.Python.3.12", "pandoc": "JohnMacFarlane.Pandoc", "tesseract": "UB-Mannheim.TesseractOCR"}
        return winget.format(ids[tool])
    if platform == "darwin":
        return "brew install " + tool
    apt = {"python": "python3 python3-pip", "pandoc": "pandoc", "tesseract": "tesseract-ocr"}
    return "sudo -n apt-get install -y " + apt[tool]


def find_pandoc():
    # A fresh install is not on the PATH of the running shell yet: look in the usual install folders too.
    found = shutil.which("pandoc")
    if found:
        return found
    if sys.platform == "win32":
        dirs = [os.environ.get(v) for v in ("LOCALAPPDATA", "ProgramFiles")]
        cands = [Path(d) / "Pandoc" / "pandoc.exe" for d in dirs if d]
    elif sys.platform == "darwin":
        cands = [Path("/opt/homebrew/bin/pandoc"), Path("/usr/local/bin/pandoc")]
    else:
        cands = []
    return next((str(c) for c in cands if c.exists()), None)


def find_tesseract():
    # winget kurulumu PATH'e girmeyebilir: bilinen klasörlere de bak
    found = shutil.which("tesseract")
    if found:
        return found
    if sys.platform == "win32":
        dirs = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        cands = [Path(d) / "Tesseract-OCR" / "tesseract.exe" for d in dirs if d]
    elif sys.platform == "darwin":
        cands = [Path("/opt/homebrew/bin/tesseract"), Path("/usr/local/bin/tesseract")]
    else:
        cands = []
    return next((str(c) for c in cands if c.exists()), None)


def tessdata_dir():
    # Türkçe model skill klasöründe durur; yoksa tesseract kendi klasörünü kullanır
    d = SKILL_DIR / "tessdata"
    return d if (d / f"{OCR_LANG}.traineddata").exists() else None


def ocr_langs(tess):
    td = tessdata_dir()
    cmd = [tess] + (["--tessdata-dir", str(td)] if td else []) + ["--list-langs"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Başlık satırı ("List of available languages in ...") stdout'ta ya da stderr'de olabilir:
    # satır sırasına güvenmek yerine dil adı biçimine bak. Aksi halde tek dilli kurulum yok sayılır.
    if r.returncode != 0:
        return []
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).splitlines()
    return [l.strip() for l in out if re.fullmatch(r"[A-Za-z_]{3,}", l.strip())]


def ocr_tsv(png_path, tess):
    # Sayfa diskten okunup stdin'den verilir: bütün kitabın görüntüsü bellekte tutulmaz, ve
    # tesseract'a yol verilmez (ASCII dışı yolu bazı sürümler açamıyor).
    # thresholding_method=2 uyarlamalı eşikleme kullanır: renkli ya da kirli zemine basılmış sayfayı
    # (arka kapak, kuşe kâğıt) okur; ölçümde 103 karakter yerine 1874 karakter verdi ve temiz
    # bitonal taramada çıktı birebir aynı kaldı.
    cmd = [tess, "stdin", "stdout", "-l", OCR_LANG, "--psm", "6", "-c", "tessedit_create_tsv=1",
           "-c", "thresholding_method=2"]
    td = tessdata_dir()
    if td:
        cmd[3:3] = ["--tessdata-dir", str(td)]
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}   # her tesseract tek iş parçacığı açar, dördü paralel çalışır
    try:
        r = subprocess.run(cmd, input=png_path.read_bytes(), capture_output=True, env=env)
    except OSError:
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def check():
    v = ".".join(map(str, sys.version_info[:3]))
    missing = False
    if sys.version_info >= (3, 10):
        print(f"ok: python {v}")
    else:
        missing = True
        print(f"missing: python 3.10 or later (found {v}) | install: {install_cmd('python', sys.platform, sys.executable)}")
    try:
        import fitz
        print(f"ok: pymupdf {getattr(fitz, 'VersionBind', '')}".rstrip())
    except ImportError:
        missing = True
        print(f"missing: pymupdf | install: {install_cmd('pymupdf', sys.platform, sys.executable)}")
    pandoc = find_pandoc()
    if pandoc:
        print(f"ok: pandoc {pandoc}")
    else:
        missing = True
        print(f"missing: pandoc | install: {install_cmd('pandoc', sys.platform, sys.executable)}")
    tess = find_tesseract()
    if not tess:
        missing = True
        print(f"missing: tesseract | install: {install_cmd('tesseract', sys.platform, sys.executable)}")
    else:
        print(f"ok: tesseract {tess}")
        if OCR_LANG in ocr_langs(tess):
            print(f"ok: tesseract {OCR_LANG} language")
        else:
            missing = True
            print(f"missing: tesseract {OCR_LANG} language | install: {install_cmd('ocrlang', sys.platform, sys.executable)}")
    return 1 if missing else 0


def selftest():
    if not __debug__:
        # -O ile assert'ler soyulur: test hiç koşmaz, ama "selftest ok" yazardı. Sessizce geçmesin.
        raise SystemExit("The selftest needs assertions. Do not run it with -O.")
    L = lambda t, x=50, s=10: {"text": t, "x0": x, "size": s}
    T = lambda pages: [[l["text"] for l in pg] for pg in pages]
    C = lambda t, w=5: [(c, i * w, (i + 1) * w) for i, c in enumerate(t)]
    assert line_text(C("ab cd")) == "ab cd"  # a real gap keeps its space
    assert line_text(C("ab ") + [("c", 10, 15)]) == "abc"  # next glyph starts inside the space box
    assert line_text(C("ab ") + [("J", 10, 15)]) == "ab J"  # overlapped space before a capital stays
    assert line_text(C("ab ")) == "ab "  # trailing space: same as before
    assert fix_tr("aðýr þey Ýþ") == "ağır şey İş"
    assert join_lines(["ka\xad", "lemlik evi"]) == "kalemlik evi"
    assert join_lines(["Kitap- ", "lık yeni"]) == "Kitaplık yeni"
    assert join_lines(["Ali-", "Veli geldi"]) == "Ali-Veli geldi"
    assert join_lines(["bir satır ", "iki satır"]) == "bir satır iki satır"
    assert join_lines(["ka\xad"]) == "ka-"  # page-end soft hyphen stays visible so to_html can join the pages
    assert join_lines(["delindi -", "dedi"]) == "delindi - dedi"  # a free dash is not a hyphen
    assert spans_text([("Mavi Ev ", True), ("yesterday", False)]) == "*Mavi Ev* yesterday"
    assert spans_text([("Then", False), (" more ", True), ("text", False)]) == "Then *more* text"
    assert spans_text([("a", False), (" ", False), ("b", False)]) == "a b"
    assert spans_text([("a ", False), (" ", False), ("b", False)]) == "a b"
    # tesseract TSV: bir satırın kelimeleri birleşir, ayrı satırlar ayrı kalır, kutu punto'ya çevrilir
    H = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    tsv = (H + "5\t1\t1\t1\t1\t1\t135\t95\t41\t28\t96.7\t46\n"
               + "5\t1\t1\t2\t1\t1\t183\t250\t142\t46\t96.5\tÖyleyse\n"
               + "5\t1\t1\t2\t1\t2\t344\t257\t131\t33\t96.6\tilkönce\n"
               + "5\t1\t1\t1\t1\t2\t200\t95\t30\t28\t10.0\t\n")
    tl = tsv_lines(tsv)
    assert [l["text"] for l in tl] == ["46", "Öyleyse ilkönce"]
    assert round(tl[0]["x0"], 2) == round(135 * 72 / SCAN_DPI, 2)
    assert round(tl[1]["x1"], 2) == round((344 + 131) * 72 / SCAN_DPI, 2)
    assert tl[0]["size"] == round(28 * 72 / SCAN_DPI, 1)
    assert tsv_lines(H) == []
    assert tsv_lines("5\t1\t1\t1\t1\t1\tx\ty\t1\t2\t3\tkelime\n") == []  # bozuk kutu atlanır
    pages = [[L(f"{n} • kısa başlık dizisi"), L(f"metin {n}"), L(str(n))] for n in range(10, 20)]
    assert T(strip_running(pages, 10)) == [[f"metin {n}"] for n in range(10, 20)]
    assert T(strip_running([[L("Tek satır")]], 10)) == [["Tek satır"]]
    # büyük fontlu bölüm numarası üst bilgi değildir
    pages = [[L(str(n), s=14), L(f"metin {n}"), L("son")] for n in range(1, 9)]
    assert T(strip_running(pages, 10))[0][0] == "1"
    # girinti yeni paragraf başlatır; girintisiz ilk satır önceki sayfayı sürdürür
    assert page_blocks([L("devam eden"), L("satır."), L("Yeni paragraf", x=62), L("sürer.")], 10, 50) ==         ["<<", "devam eden satır.", "Yeni paragraf sürer."]
    # büyük font başlıktır; başlıktan sonraki girintisiz satır yeni paragraftır, << almaz
    assert page_blocks([L("BİRİNCİ BÖLÜM", x=80, s=14), L("İlk cümle"), L("sürer.")], 10, 50) ==         ["# BİRİNCİ BÖLÜM", "İlk cümle sürer."]
    assert page_blocks([L("Girintili başlar", x=62), L("sürer.")], 10, 50) == ["Girintili başlar sürer."]
    assert page_blocks([], 10, 50) == []
    # taranmış sayfa kararı: kısa metinli ama görüntüsüz sayfa metin sayfasıdır (başlık işareti alabilir)
    PG = lambda imgs: type("PG", (), {"get_images": lambda self: imgs})()
    assert not is_scanned([L("ÖTÜKEN")], PG([]))        # amblem sayfası: metin katmanı var, taranmış değil
    assert is_scanned([L("x")], PG([1]))                # görüntü + kısa metin: taranmış
    assert is_scanned([], PG([1]))                      # hiç metin satırı yok
    assert is_scanned([], PG([]))                       # yalnız çizim: metin yok
    assert not is_scanned([L("uzun metin " * 10)], PG([1]))
    assert not is_scanned([L("uzun metin " * 10)], PG([]))
    # kısa satır paragrafı bitirir (girintisiz kitaplar)
    K = lambda t, x1: {**L(t), "x1": x1}
    assert page_blocks([K("tam satır", 200), K("kısa.", 90), K("Yeni", 200), K("sürer.", 120)], 10, 50) ==         ["<<", "tam satır kısa.", "Yeni sürer."]
    assert is_short(K("kısa.", 90), [K("tam", 200), K("kısa.", 90)], 50)
    assert not is_short(K("tam", 200), [K("tam", 200), K("kısa.", 90)], 50)
    assert page_blocks([L("devam"), L("eden.")], 10, 50, cont=False) == ["devam eden."]
    # drop cap beside two body lines (79pt letter), then three full-width lines; chapter number above it
    D = lambda t, x0, y0, y1, s=21.2, x1=570.7: {"text": t, "x0": x0, "y0": y0, "y1": y1, "size": s, "x1": x1}
    p50 = [D("B", 41.0, 74.7, 162.4, 79.1, 93.8), D("-III-", 285.2, 30.7, 58.4, 25.0, 326.8),
           D("ELİRTİ yavaşça uzaklaşıyordu. Pencerede ışıklar da", 97.7, 82.1, 105.6),
           D("kalmamıştı artık. Biraz sonra tamamen karanlıkta", 97.7, 106.8, 130.3),
           D("eriyecekti. Şimdi kız", 97.7, 131.6, 155.1),
           D("gideceği yolun sonunu", 41.0, 156.3, 179.8),
           D("düşünmeliydi. O ana kadar", 41.0, 181.1, 204.6, x1=300)]
    assert page_blocks(join_dropcap(p50, 21.2), 21.2, 41.0) == [
        "# -III-", "BELİRTİ yavaşça uzaklaşıyordu. Pencerede ışıklar da kalmamıştı artık. Biraz sonra tamamen karanlıkta "
        "eriyecekti. Şimdi kız gideceği yolun sonunu düşünmeliydi. O ana kadar"]
    assert join_dropcap([L("A", s=14), L("metin")], 10) == [L("A", s=14), L("metin")]  # no y keys: untouched
    assert join_dropcap([D("V", 41, 10, 40, 25), D("Metin", 41, 60, 80)], 21.2) == [D("V", 41, 10, 40, 25), D("Metin", 41, 60, 80)]  # no body line beside it
    assert join_dropcap([D("B", 41, 10, 90, 79.1, 93.8), D("Metin", 41, 100, 120)], 21.2)[0]["text"] == "B"  # body only below: still a heading
    assert guard("a" * 1000, "a" * 1100)
    assert not guard("a" * 1000, "a" * 1200)
    assert not guard("kelime " * 200, "kelime " * 160)
    assert not guard("kelime " * 60, "")
    assert not guard("a" * 1000, None)  # file deleted
    assert guard("a" * 100, "a" * 140)  # short page: up to 50 chars of difference
    assert not guard("# İKİNCİ BÖLÜM", "")
    assert not guard("![](img-0003-1.png)", "")
    assert guard("", "")
    assert guard("", "Künye sayfası metni " * 40)  # the worker writes pages with no text
    assert not guard("", None)
    # gürültü silmek metin kaybı değildir; paragrafla birlikte silmek kayıptır
    P = "Bu bir paragraf satırıdır ve yeterince uzundur. " * 13
    G = "\n\n".join(["e . .*", "MEC Vİ “4 « Di", "Ki. SV SİKER .-", "BRC F rey se MEM ğ"] * 6)
    assert prose_chars(P + "\n\n" + G) == len(P.strip())
    assert guard(P + "\n\n" + G, P)              # yalnız gürültü gitti
    assert not guard(P + "\n\n" + G, P[:200])    # paragraf da gitti
    # metin katmanlı sayfa baştan yazılırsa geri alınır; taranmış sayfada kural uygulanmaz
    A, B = "kelime kelime " * 50, "sözcük sözcük " * 50
    assert len(A) == len(B) >= REWRITE_MIN_CHARS
    assert not guard(A, B)                       # aynı uzunluk, başka kelimeler
    assert guard(A, B, ocr=True)                 # taranmış sayfa: taslak gürültülü, serbest
    assert guard(A, A)
    assert guard("Ali ata bak.", "Veli eve git.")   # kısa sayfa: benzerlik kuralı dışı
    # yalnız gürültüden oluşan sayfa boşaltılabilir, kısa gürültü bloğuyla birlikte
    assert guard(G, "")
    assert prose_chars(G) == 0
    # Taranmış sayfada tesseract bir çizimin karalamasını "kelime" sayar: prose_chars yanıltır.
    # Boşaltma kararı yine gürültü payına bakar, boş dala takılıp kesilmez (ölçüm: Behrengi 0009).
    N = "\n\n".join(["İN SE, EKA kt", "2 Y", "2“ ii & i", "İğ TT. aç NS ar i s—- A , NF sel KS? hş Fal",
                     "ŞEF Sl . i A j \"€ Ni «© £ BERİ Si VE", "0S i"])
    assert prose_chars(N) > 0 and prose_chars(N) + 150 >= len(N)
    assert not guard(N, "")                          # metin katmanlı sayfa: gürültü olsa da boşaltma yok
    assert guard(N, "", ocr=True)                    # taranmış sayfa: gürültü payı yeter, boşaltma serbest
    assert not guard("kelime " * 60, "", ocr=True)   # taranmış olsa da gerçek metin boşaltılamaz
    # sayfa 0080 gibi dağınık tablo satırları tabloya çevrilince metin uzar ama kayıp olmaz
    S = "\n\n".join(["Yük Baryon Acayiplik Enerji Açısal", "No momentum Korunur",
                     "(yerel olarak) Evet Evet Hemen hemen Evet Evet",
                     "ortaya çıkar Evet Evet Evet Hayır Evet", "kaynağıdır Evet ? ? Evet"])
    T = ("|             | Yük    | Baryon<br>No    | Acayiplik     |\n"
         "|-------------|--------|-----------------|---------------|\n"
         "| Korunur<br>(yerel olarak) | Evet | Evet | Hemen hemen |\n"
         "| Birimler halinde<br>ortaya çıkar | Evet | Evet | Evet |")
    assert len(T) > len(S) + 50
    assert guard(S, T)
    h = to_html(["# Bir\n\n- Merhaba, dedi.\n\nYarım kalan", "<<\n\ncümle *bitti*.\n\n* * *\n\n1. gün & gece"])
    assert h == ("<h1>Bir</h1>\n<p>- Merhaba, dedi.</p>\n<p>Yarım kalan cümle <i>bitti</i>.</p>\n"
                 "<hr/>\n<p>1. gün &amp; gece</p>")
    assert to_html(["a", "", "![](img-0003-1.png)"]) == "<p>a</p>"   # metin odaklı: resim bloğu düşer
    assert to_html(["<<\n\nilk sayfa"]) == "<p>ilk sayfa</p>"
    assert to_html(["ka-", "<<\n\nranlığı"]) == "<p>karanlığı</p>"
    # sayfanın altındaki dipnot satırları süren paragrafın birleşmesini engellemez
    assert to_html(["kelime tama-", "* 1 inç: 2.54 cm", "** 1 mil: 1.609 m.", "<<\n\nmen bağımsız."]) == \
        "<p>kelime tamamen bağımsız.</p>\n<p>* 1 inç: 2.54 cm</p>\n<p>** 1 mil: 1.609 m.</p>"
    assert to_html(["* * *", "<<\n\nsüren"]) == "<hr/>\n<p>süren</p>"      # sahne arası dipnot değildir
    assert to_html(["<<\n\nmetin", "* dipnot"]) == "<p>metin</p>\n<p>* dipnot</p>"   # birleşecek paragraf yok
    assert to_html(["a *b* c", "x * y * z", "w** more"]) == "<p>a <i>b</i> c</p>\n<p>x * y * z</p>\n<p>w** more</p>"
    assert to_html(["*kitap'*ın kapağı"]) == "<p><i>kitap'</i>ın kapağı</p>"  # a suffix glued after the closing asterisk
    assert to_html(["kalem**, selam defter***.", "masa***** sandalye gibi"]) == "<p>kalem**, selam defter***.</p>\n<p>masa***** sandalye gibi</p>"
    assert to_html(["2 * 3 = 6 ve 4 * 5"]) == "<p>2 * 3 = 6 ve 4 * 5</p>"
    assert to_html(["*Mavi Ev*'de"]) == "<p><i>Mavi Ev</i>'de</p>"
    assert to_html(["*bitti*."]) == "<p><i>bitti</i>.</p>"
    assert to_html(["a**<i>, b</i>***."]) == "<p>a**&lt;i&gt;, b&lt;/i&gt;***.</p>"
    assert to_html(["yarım", "<< \n\nmetin"]) == "<p>yarım metin</p>"
    assert to_html(["a\n\n\n# H"]) == "<p>a</p>\n<h1>H</h1>"
    assert to_html(["* * * "]) == "<hr/>"
    assert to_html(["***"]) == "<hr/>"                    # sahne arası böyle de yazılabilir
    assert to_html(["*  *  *"]) == "<hr/>"
    assert to_html(["## Alt başlık"]) == "<h1>Alt başlık</h1>"
    assert to_html(["a\n\n<<\n\nb"]) == "<p>a</p>\n<p>b</p>"   # ortada kalan işaret metne girmez
    # borulu tablo gerçek tabloya dönüşür; satır kayması tabloyu bozmaz
    assert to_html(["| A | B |\n| --- | --- |\n| 1 | 2 |"]) == \
        "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    assert to_html(["| A | B |\n|---|---|\n| 1 |\n| 2 | 3 | 4 |"]) == \
        "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td></td></tr>" \
        "<tr><td>2</td><td>3</td></tr></tbody></table>"
    assert to_html(["| A | B |\n|---|---|\n| x<br>y | *i* |"]) == \
        "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>x<br/>y</td><td><i>i</i></td></tr></tbody></table>"
    assert to_html(["| A | B |\n| 1 | 2 |"]) == "<p>| A | B |\n| 1 | 2 |</p>"   # ayırıcı yok: tablo değil
    assert not is_table("| A | B |")
    assert is_table("| A | B |\n|---|---|\n| 1 | 2 |")
    # yapı geçişi: rol dosyası ve güvenli düşürme
    R = parse_roles("0001 cover\n0002 back-cover\n0007 blank?\n0010 body\n0254 body\n0257 ads\nbozuk satır\n")
    assert len(R) == 6 and R["0002"] == "back-cover" and R["0007"] == "blank?"
    # baştaki BOM ilk satırı düşürmesin (Windows araçları dosyayı imzayla yazabilir)
    assert parse_roles("\ufeff0001 cover\n0002 body\n") == {"0001": "cover", "0002": "body"}
    assert drop_pages(R) == ["0002", "0257"]                                  # gövdenin dışı
    assert drop_pages(parse_roles("0001 cover\n0002 back-cover\n")) == []     # gövde yok: hiçbiri düşmez
    assert drop_pages(parse_roles("0005 body\n0007 ads\n0010 body\n")) == []  # gövdenin içi: dokunulmaz
    assert drop_pages(parse_roles("0010 body\n0257 ads?\n")) == []            # emin değil: dokunulmaz
    # Kapak hedefi: uzun kenar COVER_LONG_EDGE piksel. Yoğunluk sayfa boyuna göre hesaplanır.
    assert cover_dpi(72 * 8) == 210                 # 8 inçlik sayfa: 1680/8
    assert cover_dpi(72 * 11) == 153                # 11 inçlik sayfa: 1680/11
    assert cover_dpi(72 * 4) == SCAN_DPI            # 4 inçlik sayfa: hedef 420 dpi, tavan 300
    assert cover_dpi(0) == DPI                      # bozuk ölçü: güvenli varsayılan
    # Kapak kaynağın üstüne çıkmaz: sayfa tek bir bitmap ise sınır o resmin kendi yoğunluğudur.
    assert cover_dpi(72 * 8, 40) == 40              # kaynak 40 dpi: hedefe şişirmek boşuna bayt
    assert cover_dpi(72 * 8, 600) == 210            # kaynak zengin: sınır hedefi değiştirmez
    assert cover_dpi(72 * 8, 0.4) == 1              # çok küçük kaynak: en az 1 dpi
    assert cover_dpi(72 * 8, None) == 210           # kaynak bilinmiyor: sınır yok
    # Sayfanın kendi yoğunluğu: en keskin resim kazanır, metin/vektör varsa sınır konmaz.
    assert bitmap_dpi([(600, 800, 288, 384)], False) == 150.0            # 600 px / 4 inç
    assert bitmap_dpi([(300, 400, 288, 384)], False) == 75.0             # yarı çözünürlük
    assert bitmap_dpi([(768, 1212, 256, 404), (384, 606, 256, 404)], False) == 216.0   # en keskin
    assert bitmap_dpi([(600, 800, 400, 384)], False) == 150.0            # gergin resim: yüksek yön
    assert bitmap_dpi([(600, 800, 288, 384)], True) is None              # metin/vektör: sınır yok
    assert bitmap_dpi([], False) is None                                 # resim yok: sınır yok
    assert bitmap_dpi([(600, 800, 0, 384)], False) is None               # bozuk kutu: yok sayılır
    # TOC: chapters.txt yalnız seçilmiş başlıkları bırakır, sayfadaki öteki "# " blokları paragrafa iner
    C = parse_chapters("0005 I. Bölüm\n0007 II. Bölüm\n0010 I. Bölüm\n0010 II. Bölüm\nbozuk satır\n")
    assert C == {"0005": ["I. Bölüm"], "0007": ["II. Bölüm"], "0010": ["I. Bölüm", "II. Bölüm"]}
    assert parse_chapters("") == {}                     # boş dosya: kitapta başlık yok
    assert parse_chapters("\ufeff0005 I. Bölüm\n0007 II. Bölüm\n") == \
        {"0005": ["I. Bölüm"], "0007": ["II. Bölüm"]}   # BOM ilk bölümü düşürmesin
    # Ayrışık ve birleşik yazım tek biçime iner: başlık seçimi eşleşmeyi kaçırmaz.
    assert nfc("gu\u0308zel") == "güzel"                 # u + birleştirici çift nokta -> ü
    assert nfc("O\u0308bu\u0308ru\u0308") == "Öbürü"
    assert nfc("zaten birleşik") == "zaten birleşik"      # NFC girdi değişmez
    assert parse_chapters("0005 gu\u0308zel\n") == {"0005": ["güzel"]}
    assert demote(nfc("# gu\u0308zel\n\nmetin"), {"güzel"})[0] == "# güzel\n\nmetin"
    assert demote("# I. Bölüm\n\nmetin\n\n# MODERN KLASİKLER Dizisi - 164", {"I. Bölüm"}) == \
        ("# I. Bölüm\n\nmetin\n\nMODERN KLASİKLER Dizisi - 164", [])
    assert demote("# I. Bölüm\n\nmetin", {"Yok"}) == ("I. Bölüm\n\nmetin", ["Yok"])   # eşleşmeyen seçim bildirilir
    assert demote("# I. Bölüm\n\nmetin", set()) == ("I. Bölüm\n\nmetin", [])          # sayfada seçim yok: hepsi iner
    assert demote("# A\n\nmetin", None) == ("# A\n\nmetin", [])                      # dosya yok: her başlık kalır
    assert demote("<<\n\nmetin", set()) == ("<<\n\nmetin", [])                       # "<< " başlık değildir
    assert demote("# Çok   boşluk", {"Çok boşluk"})[0] == "# Çok   boşluk"           # boşluk farkı eşleşmeyi bozmaz
    assert demote("# Çok   boşluk", {"Yok"})[0] == "Çok   boşluk"
    assert demote("# | A | B |\n|---|---|\n| 1 | 2 |", set())[0].startswith("# | A | B |")  # tablo başlık değildir
    assert to_html([demote("# I. Bölüm\n\nmetin\n\n# MODERN KLASİKLER Dizisi - 164", {"I. Bölüm"})[0]]) == \
        "<h1>I. Bölüm</h1>\n<p>metin</p>\n<p>MODERN KLASİKLER Dizisi - 164</p>"
    assert to_html([demote("# A\n\n# B", set())[0]]) == "<p>A</p>\n<p>B</p>"
    assert to_html(["![](a.png) tail"]) == "<p>![](a.png) tail</p>"
    W = "-e --accept-source-agreements --accept-package-agreements"
    assert install_cmd("pandoc", "win32", "") == f"winget install --id JohnMacFarlane.Pandoc {W}"
    assert install_cmd("pandoc", "darwin", "") == "brew install pandoc"
    assert install_cmd("pandoc", "linux", "") == "sudo -n apt-get install -y pandoc"
    assert install_cmd("python", "linux", "") == "sudo -n apt-get install -y python3 python3-pip"
    P = '"/usr/bin/python3" -m pip install'
    assert install_cmd("pymupdf", "linux", "/usr/bin/python3") == f"{P} --user pymupdf || {P} --user --break-system-packages pymupdf"
    assert install_cmd("pymupdf", "darwin", "/usr/bin/python3") == install_cmd("pymupdf", "linux", "/usr/bin/python3")
    assert install_cmd("pymupdf", "win32", "py") == '"py" -m pip install pymupdf'
    assert install_cmd("python", "win32", "") == f"winget install --id Python.Python.3.12 {W}"
    assert install_cmd("python", "darwin", "") == "brew install python"
    assert install_cmd("tesseract", "win32", "") == f"winget install --id UB-Mannheim.TesseractOCR {W}"
    assert install_cmd("tesseract", "darwin", "") == "brew install tesseract"
    assert install_cmd("tesseract", "linux", "") == "sudo -n apt-get install -y tesseract-ocr"
    D = install_cmd("ocrlang", "win32", "")
    assert "tessdata_best" in D and str(SKILL_DIR / "tessdata") in D and f"{OCR_LANG}.traineddata" in D
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    ep = sub.add_parser("extract")
    ep.add_argument("pdf", type=Path)
    ep.add_argument("--force", action="store_true")
    sub.add_parser("check")
    bp = sub.add_parser("build")
    bp.add_argument("pdf", type=Path)
    bp.add_argument("--title", default="")
    bp.add_argument("--author", default="")
    tp = sub.add_parser("toc")
    tp.add_argument("pdf", type=Path)
    tp.add_argument("--title", default="")
    tp.add_argument("--author", default="")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.cmd == "extract":
        work, scan, failed, blank = extract(a.pdf, a.force)
        print(f"work: {work.resolve()}")
        print(f"pages: {len(list(work.glob('[0-9]*.md.orig')))}")
        if scan:
            print(f"scan: {len(scan)}")
        if failed:
            print("ocr failed: " + " ".join(f"{n:04d}" for n in failed))
        if blank:
            print("blank: " + " ".join(blank))
        print("batches: " + " ".join(batches(work)))
    elif a.cmd == "check":
        sys.exit(check())
    elif a.cmd == "build":
        build(a.pdf, a.title, a.author)
    elif a.cmd == "toc":
        work = a.pdf.with_suffix(".pdf2epub")
        if not (work / "extract.ok").exists():
            raise SystemExit("Run the extract command first.")
        roles = parse_roles((work / "roles.txt").read_text(encoding="utf-8")) if (work / "roles.txt").exists() else {}
        text, n = toc_candidates(work, roles, a.title or a.pdf.stem.strip(), a.author)
        (work / "toc.txt").write_text(text, encoding="utf-8")
        print(f"toc: {(work / 'toc.txt').resolve()}")
        print(f"candidates: {n}")
    else:
        ap.print_usage()
        sys.exit(2)
