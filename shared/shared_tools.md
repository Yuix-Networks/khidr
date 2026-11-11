# Paylaşılan Araçlar

Elinizde iki sınıf araç var.

## A — Claude Code'un standart dev araçları

Agent'lar Claude Agent SDK ile çalışır; Claude Code'un yerleşik araçları
hazır gelir (parametre açıklamaları tool şemasında):

- `Bash` — shell komutu (repo dizininde çalışır; varsayılan timeout 5 dk).
  Uzun işler (crawler, build, dev server) için `run_in_background: true`
  ver, sonra çıktıyı oku / süreci durdur.
- `Read` / `Write` / `Edit` — dosya oku, yaz, tek eşleşmeli değiştir
- `Glob` — `**/*.py` tarzı dosya bulma
- `Grep` — regex arama (ripgrep)
- `WebFetch` — URL içeriğini oku
- `TodoWrite` / `TaskCreate` / `TaskUpdate` / `TaskList` — kendi ilerleme
  listeni tut. Bunlar **kanban kartı değildir**; kart için aşağıdaki
  `create_kanbancard` / `get_kanbancard` / `search_kanbancards` kullan.

Yok: `AskUserQuestion` (yerine `ask_user`), `WebSearch` (yerine `web_search`),
plan modu, worktree, alt-agent.

## B — Orchestrator'a özel araçlar (`khidr` MCP sunucusu)

Tool listesinde `mcp__khidr__<isim>` olarak görünürler (ör.
`mcp__khidr__set_status`); aşağıda kısa isimleriyle anlatılıyor. Kanban
kartı + proje hafızası + .env + harici servisler için bizim tarafımızı
tetiklerler:

- Kart: `note`, `set_status`, `ask_user`, `reject_task`,
  `get_kanbancard`, `search_kanbancards`, `create_kanbancard`
- Hafıza: `get_project_memory`, `add_project_memory`, `save_memory`,
  `search_memory`, `list_memories`
- Diğer: `web_search`, `understand_image`, `get_glitchtip_issue`

### `note(text)`

Chat'te highlighted bir system note bırakır. İlerleme özeti, handoff,
bitiş raporu için.

### `set_status(status)`

Mevcut kanban kartının kolonunu değiştirir.
`status ∈ {backlog, todo, in_progress, review, done}`.
İş bittiyse `set_status("review")` çağırırsın — karta otomatik Telegram
bildirim gider.

> `set_status("review")` / `set_status("done")` repo cleanliness guard'ından
> geçer: uncommitted değişiklik, main dışı branch, veya push edilmemiş
> commit varsa reddedilir. Önce temizle, sonra review'e al. Kod
> yazmadığın işlerde bu kontrol zaten otomatik geçer.

### `ask_user(message)`

Kullanıcıya soru sor ve **dur**. Bu çağrı:
1. Kartı `waiting_for_user=True` moduna alır — orchestrator requeue etmez.
2. Kullanıcıya **Telegram üzerinden anında bildirim** gönderir (chat linkiyle).

Plain text yazıp durmak yeterli değil — tool çağrısı olmazsa orchestrator
session'ı normal "finished" sayıp silent requeue yapar.

### `get_kanbancard(card_id, recent_messages=5)`

Başka bir kanban kartının detaylarını döndürür: title, description,
project, status, complexity, assigned/current agent, ve son N mesaj
(max 20). Read-only, cross-project OK (kendi projen dışındaki karta
da bakabilir). Mevcut kart başka bir karta referans veriyor ya da
bağımlıysa kullan.

Örnek çıktı:
```
# Kanban card #18: Forum sayfasinda tasarım değişikliği yap
- Project: Fenerbahçe Fan
- Status: Done  (complexity: 3)
- Assigned: claude-sonnet
- Currently working: (none)

## Description
...

## Last 5 chat messages
- [claude-sonnet] ...
- [user] ...
```

### `search_kanbancards(query, max_results=20, include_done=true)`

**Aynı proje** içindeki kartların title + description'ında case-insensitive
substring araması. `updated_at desc` sırasında satır başına
`#<id> [<status>] <title>` formatında döner.

- `max_results`: 1–50 (default 20). Aşılırsa `... truncated at N`.
- `include_done`: false verirsen Done kartlar gelmez (sadece aktif iş).

`create_kanbancard` açmadan **önce** aynı işi daha önce yapılmış ya da şu an
yapılmakta olan bir kart var mı kontrol için ideal.

Örnek çıktı:
```
Found 2 kanban card(s) matching 'admin stats' in Fenerbahçe Fan:
- #42 [in_progress] Add admin stats counter
- #31 [done] Stats panel cleanup
```

### `get_project_memory()` / `add_project_memory(text)`

Proje-seviyesinde **uzun-vade ortak hafıza**. Bulunduğun projenin
`description` alanını okur (`get_project_memory`) ya da altına ekleme
yapar (`add_project_memory`).

`note` ile karıştırma: `note` **şu anki kart**'ın chat'ine düşer —
o görevi bitirirken bırakılan iz. `add_project_memory` ise **bu projede
ileride çalışacak başka ajanların** bilmesi gereken şeyler için.

Tipik kullanım:

- Sen `settings.py`'de bir ayarı değiştirdin → diğer ajanlar yeni
  yerini arar → `add_project_memory("PG_* artık .env'den okunuyor, settings.py hardcoded dolu değil")`.
- Projede her yerde `fab deploy` kullanılıyor, senin kartın da öyle
  yaptıysa → yazma, zaten bilinen bir şey.
- Discovery türünden: "the crawler `~/workspace/myproject/crawl.py`'de,
  DB'ye standings da çekiyor artık" gibi gerçek yararlı bilgi.

Yazım:

- 1-2 cümle. Uzun anlatım karta gider, description'a değil.
- Otomatik olarak `[2026-04-22 14:30 · <slug>]` başlar, sen tarih yazma.
- `## Agent memory` başlığı altında bullet olarak birikir — description
  kirlenmez.

Yeni bir karta başlamadan önce `get_project_memory()` çek, son 10-15
satıra bak; önceki ajan bu ortamda ne öğrenmiş?

### `create_kanbancard(title, description?, complexity=3, status="todo", assigned_agent?)`

**Aynı projede** yeni kanban kartı oluşturur (agent başka projeye iş
yaratamaz). Subtask akışı için: şu anki iş büyükse parçala, orchestrator
alt kartları sırasıyla alır. Yeni kartın id'si döner.

- `status` sadece `todo` veya `backlog` — orchestrator geri kalan
  lifecycle'ı zaten yönetir.
- `complexity` 1–5; agent'ın `talent >= complexity` olmalı ki
  dispatcher'da seçilebilsin.
- `assigned_agent`: opsiyonel slug. Verirsen sadece o agent alır;
  boşsa dispatcher uygun olanı seçer.
- Yeni kartın chat'ine otomatik `[created by card-<parent>]` system
  mesajı düşer (provenance).

## Nasıl bitiririm?

- Kod + test tamam → `note` ile özet + `set_status("review")`.
- Üstüne istersen düz text özet de yazabilirsin; zaten chat'e düşer.

## Nasıl yardım isterim?

- `ask_user("şu netleşmeli: …")`. Telegram'dan sana ping gider.

## Subtask akışı önerilen sıra

1. `search_kanbancards("anahtar kelime")` — duplicate var mı bak.
2. Varsa `get_kanbancard(<id>)` ile detayına bak; orada devam etmek daha iyiyse
   üstüne iş yaratma.
3. Yoksa `create_kanbancard(...)` ile yeni subtask'ı aç. Id'yi `note`'una
   yazarsan kullanıcı UI'dan direkt o karta atlayabilir.
