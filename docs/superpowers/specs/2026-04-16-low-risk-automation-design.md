# M28: Düşük Riskli Otomasyon

> StudioOS'u karar-destek sisteminden gerçek otonom operasyona taşıyan ilk adım.

## Kapsam

İki bağımsız iş:

1. **Live Repricing** — 3 strateji (buy_box_win, profit_max, stock_bleed) onaysız çalışır
2. **Stranded Auto-List** — Amazon'da stranded olan ürünler otomatik eBay'e listelenir

### Kapsam dışı (bilinçli)

- ACOS auto-pause (gerçek ACOS verisi yok, AdsOptimizer placeholder)
- Stok<3 eBay auto-stop (manual kalacak)
- eBay fiyat senkronizasyonu (manual kalacak)
- TR satın alma otomasyonu (gelecek milestone)

---

## 1. Live Repricing

### Mevcut durum

```
pricer (30m schedule)
  → lost buybox + aging inventory tarar
  → strateji seçer (buy_box_win / profit_max / stock_bleed)
  → amz.reprice.recommended event emit eder
  → repricer (event-triggered, approval-gated, dry_run=true)
     → onay bekler
     → onay gelirse buyboxpricer.api.run_single_repricing çağırır
```

### Hedef durum

```
pricer (30m schedule)
  → aynı tarama + strateji
  → amz.reprice.recommended event emit eder
  → repricer (event-triggered, APPROVAL-FREE, dry_run=false)
     → doğrudan buyboxpricer.api.run_single_repricing çağırır
     → sonucu Telegram'a bildirir
```

### Değişiklikler

1. **studio.yaml**: `amz-repricer` agent goals'da `dry_run: false` set et
2. **amz_repricer.py**: Approval gate'i kaldır — repricer doğrudan execute etsin
3. Bildirim: her başarılı reprice sonrası Telegram digest (zaten var)

### Güvenlik katmanları (mevcut, dokunulmayacak)

| Koruma | Nerede | Nasıl |
|--------|--------|-------|
| Floor price | `_propose_price()` | `min_price` altına asla düşmez |
| Günde max 2 reprice/listing | `node_recommend()` | `reprice_log` state dict, 24h pencere |
| 15 dk bekleme | `node_recommend()` | İlk buybox kaybından 15 dk sonra aksiyon |
| Price-war eskalasyon | `node_recommend()` | comp>10 + son 24h reprice → CEO approval |
| Ceiling price | `_propose_price()` | `max_price` üstüne çıkmaz (profit_max stratejisi) |

### Etki analizi

- **Risk:** Düşük. BuyBoxPricer zaten floor/ceiling enforce ediyor. Worst case: günde 2 fiyat değişikliği × 600 listing = 1200 API call, SP-API rate limit (5/s) dahilinde.
- **Kazanım:** Buy Box kazanma oranı %26 → hedef %80'e yaklaşması. Şu an hiçbir fiyat değişikliği yapılmıyor.

---

## 2. Stranded Inventory Auto-List

### Mevcut durum

```
crosslister (6h schedule)
  → ebaycrosslister.db.stranded_inventory tarar
  → approval row oluşturur ("stranded priority")
  → Telegram bildirim gönderir
  → bekler (human'ın eBay'e listlemesini)
```

### Hedef durum

```
crosslister (6h schedule)
  → ebaycrosslister.db.stranded_inventory tarar
  → her stranded ürün için:
     1. ebaycrosslister.api.create_draft (YENİ TOOL)
     2. ebaycrosslister.api.publish_listing (MEVCUT)
  → başarılı listing'leri Telegram ile bildirir
```

### Değişiklikler

1. **Yeni tool**: `ebaycrosslister.api.create_draft` — POST /listings/ ile draft oluşturur
   - Input: asin, title, price, quantity, condition
   - Fiyat: `amazon_price * 1.175` (mevcut `_ebay_target_price()` fonksiyonu)
   - Output: `{listing_id, status: "draft"}`

2. **amz_crosslister.py**: `node_emit` içinde stranded ürünler için:
   - `create_draft` → `publish_listing` zinciri
   - Batch başına max 5 listing (taşma koruması)
   - Her publish sonrası Telegram bildirim

3. **studio.yaml**: `amz-crosslister` tool_scope'a `ebaycrosslister.api.create_draft` ekle

### Güvenlik katmanları

| Koruma | Nasıl |
|--------|-------|
| Sadece stranded | `is_stranded=true` filtresi (DB query) |
| Stok kontrolü | `fulfillable_quantity > 0` (DB query) |
| Batch limiti | Tek run'da max 5 listing |
| Fiyat formülü | `amazon_price * 1.175` — markup her zaman pozitif |
| İzlenebilirlik | Her listing Telegram'a bildirilir |

### Etki analizi

- **Risk:** Düşük. Stranded ürünler zaten Amazon'da satılamıyor. eBay'e listelemek sıfır ek risk, potansiyel gelir.
- **Mevcut stranded sayısı:** SYSTEM_STATE'e göre 10 ürün. Her 6 saatte max 5 listelenecek.

---

## Test planı

1. **Repricing**: Staging'de dry_run=false ile bir test listing'e reprice yap, floor price korumasını doğrula
2. **eBay listing**: Bir stranded ürünü create_draft + publish ile eBay'e listele, fiyat doğrula
3. **Edge cases**: Floor price eşitliği, stok=0 stranded, zaten eBay'de olan stranded
