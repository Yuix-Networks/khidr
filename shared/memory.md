# Genel Kurallar

Tüm agent'lar her iş başında bu dosyayı okur. Proje-bağımsız, değişmeyen davranış kuralları burada
**Branch açma.** Agent'lar her zaman `main` üzerinde çalışır

1. Task'ı oku (başlık, açıklama, chat geçmişi)
2. Gerekirse plan yap ve chat'e yaz — büyük/belirsiz işlerde kullanıcıdan onay iste, küçük/net işlerde direkt başla
3. Repoyu mutlaka güncelle çünkü birden fazla ai ajanı bu projede çalışıyor: `git pull origin main`
4. İşi yap — **doğrudan main branch üzerinde**:
   - düzenle → test et → `git add -A && git commit -m "…"` → `git push` → `fab deploy`
5. Kendi testini yap, gerekirse test yaz
6. İş bittiyse `set_status("review")` ile task'ı Review'a al, `note` ile kısa bir özet bırak
7. İş bitmediyse ya da hata varsa düzeltmeye devam et, Review'a taşıma

> **Not:** `set_status("review")` repo'nun temiz
> olmasını kontrol ediyor. Uncommitted değişiklik, main dışı bir branch'te
> bulunma, ya da push edilmemiş commit varsa çağrı reddedilir. Önce temizle
> ve push et, sonra review'e taşı. Kod yazmadığın işlerde bu kontrol pas
> geçilir çünkü repo zaten temiz olur

## Test politikası

- Her işi test et. Test şekline iş tipine göre karar ver (unit test, manuel smoke check, lint, build)
- Yeni test dosyası yazmak zorunlu değil iş gereği yoksa yazma
- Testler geçmeden `git push` yapma

## Takılma / yardım

- Kullanıcıdan yardım gerekiyorsa **`ask_user(message)` aracını çağır**. Plain
  text yazıp durmak yeterli değil — orchestrator aracı çağrısı olmadan
  "bekliyor" olduğunu anlamaz, session'ı sessizce bitirip başka bir tura
  sokar. `ask_user` çağrıldığında orchestrator:
  1. task'ı `waiting_for_user=True` durumuna alır ve yeniden
     dispatch etmez (kullanıcı cevap verene kadar),
  2. kullanıcıya Telegram bildirimi gönderir (chat URL'i ile birlikte).
- İş yapılamaz durumdaysa yine `ask_user` ile sebebini söyle — chat'e not
  yazmak + durmak tek başına yeterli değil.
- Cevapsız 30 dakika geçerse orchestrator seni yeniden tetikler — takıldıysan dürüstçe söyle.

## Çalışma ortamı: Docker container

Kodun bir Docker container içinde koşuyor. Container'ın adı
`khidr-agent-<slug>` (senin slug'ın); host `khidr` workstation'ı
(Ubuntu 24.04, 10.0.0.5).

- Home, user, path layout host ile aynıdır: `khidr` kullanıcısı (uid=1000),
  `/home/khidr/workspace/` altında projeler, `/home/khidr/.pyenv/versions/<venv>/`
  altında sanal ortamlar. Komutlarını yazarken container-host ayrımıyla
  uğraşma — path'ler iki tarafta da aynıdır.
- Bind-mount'lar:
  - `/home/khidr/workspace/` — senin kendi rw alanın. Diğer ajanlar buraya
    dokunamaz (her ajanın ayrı `workspace-agents/<slug>/` klasörü mount ediliyor).
  - `/home/khidr/.ssh/` — host'un SSH anahtarlarına read-only erişim.
    `ssh <sunucu>`, `git clone git@...`, `fab deploy` hepsi çalışır.
  - `/home/khidr/workspace/khidr/` — orchestrator repo'sunun
    **read-only** kopyası. Orada değişiklik yapmaya çalışma; yapman gereken
    bir şey varsa Khidr kartı aç (`create_kanbancard`).
- `khidr` hostname'i container içinden host'a (Docker gateway) çözülür.
  `ssh khidr` host workstation'a gider, 10.0.0.5'e değil.

## Shell / path kuralları

- Shell başlangıçta Environment'taki `Local project path`'te açılır. File tool'ları (`Read`, `Write`, `Edit`, `Glob`, `Grep`) mutlak path ister — Environment'taki proje yolunu kullan.
- ⚠️ `Bash` state'i (cwd) çağrılar arası **kalıcıdır**. Bir önceki komutta `cd subdir/` yaptıysan sonraki `./manage.py` oraya bakar, proje köküne dönmez.
  - Sık kullandığın script'ler için **mutlak path** tercih et: `<python> <local>/manage.py check`.
  - Belirli bir dizin gerekiyorsa komutu sar: `cd <local> && <cmd>`.
- Sunucuya erişmek için `ssh <bağlantı>` wrapper'ı. Sunucuda asla `scp` / doğrudan dosya düzenleme yok — kod local → commit → push → `fab deploy`.

## Sunucu / deploy kuralları

- Sunucu üzerinde doğrudan dosya düzenlemeyin. `scp` yok. Servis manuel restart'ı yok.
- Her zaman: local commit → push → `fab deploy`.
- Server'a SSH sadece inceleme/debug için (log okuma, DB shell). Kod değişikliği için **asla**.
