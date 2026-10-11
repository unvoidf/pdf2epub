# pdf2epub

**Türkçe kurgu PDF'lerini EPUB'a çeviren bir ajan skill'i.**

*An agent skill that converts Turkish fiction PDFs to EPUB.*

## Kullanım

1. Terminali aç.
2. Repoyu klonla:

   ```
   git clone https://github.com/unvoidf/pdf2epub.git
   ```

3. Klasöre gir:

   ```
   cd pdf2epub
   ```

4. Kullandığın ajanı çalıştır. Antigravity (`agy`), Claude Code (`claude`), OpenCode (`opencode`) gibi.
5. Ajana kitabın yolunu ver:

   ```
   "C:\kitaplar\Kitap Adı - Yazar Adı.pdf" dosyasını epub'a çevir
   ```

Gerisini ajan halleder. Gereken araçlar eksikse (Python 3.10 veya üstü, PyMuPDF, pandoc, taranmış PDF için Tesseract) ajan söyler ve kurar.

Ajan PDF'nin yanına iki şey yazar:

- `Kitap Adı - Yazar Adı.epub`: sonuç
- `Kitap Adı - Yazar Adı.pdf2epub/`: çalışma klasörü (sayfa resimleri ve metin dosyaları)

EPUB'da bir hata bulursan ajana hatalı kelimeleri yaz. Ajan ilgili sayfayı düzeltir ve EPUB'ı yeniden üretir. Aynı kitabı baştan çevirmek için ajana "baştan çevir" de.

Skill `.agents/skills/pdf2epub` klasöründedir; `.agents/skills/` ajanlar arasında ortak bir yapıdır, klonlanan klasörde çalışan ajan onu kendisi bulur. Ajanın kendi skill klasörü ayrı bir yerdeyse bu klasörü oraya kopyala. Bağlarsan (Windows'ta `mklink /J`, Linux ve macOS'ta `ln -s`) bağlantıyı `rm -rf` ile silme, `rmdir` kullan.

## Neler yapar

- Her sayfayı bir resme ve bir metin dosyasına ayırır. Sayfada metin katmanı yoksa sayfayı Tesseract ile okur.
- Sayfa resmine bakan hızlı alt ajanlar metni düzeltir. Basılı tablo satır satır yazılır ve EPUB'da gerçek tablo olur.
- Kitaptaki başlık işaretlerinden bölüm başlıklarını seçer ve içindekiler listesini kurar.
- Kapak resmini seçer; arka kapak ve reklam gibi kitaba ait olmayan sayfaları EPUB'a koymaz.
- Bir alt ajan bir sayfayı aşırı değiştirirse o sayfayı geri alır ve raporda işaretler.

## Sınırlar

- EPUB metin odaklıdır: sayfa içi resimler hiç girmez, yalnız kapak resmi girer.
- Dipnot bağlantıları, şiir düzeni ve iki sütunlu sayfalar korunmaz.
- İçindekiler listesi yalnız kitapta var olan başlık işaretlerinden kurulur. Ajan eksik bir başlık eklemez; bölüm başlıkları işaretsiz olan bir kitapta içindekiler boş kalır.
- EPUB dili Türkçe olarak ayarlanır.
- Sonuç bir makine çıktısıdır. Önemli bir kitabı okumadan önce birkaç sayfayı PDF ile karşılaştır.

## Lisans

MIT. Kodu kullanabilir, değiştirebilir ve dağıtabilirsin. Ayrıntı `LICENSE` dosyasında.

Bu araç sadece dönüştürme yapar; çevirdiğin kitabın telif hakkına uymak senin sorumluluğundadır. Skill çalışırken PyMuPDF (AGPL) ve pandoc (GPL) araçlarını kullanır; bu araçlar repoya dahil değildir, kendi lisansları ile ayrı kurulur.
