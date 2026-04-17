# M36: Learning Feedback Loop

> Agent'lar aldıkları aksiyonların sonuçlarını takip eder, başarı/başarısızlık istatistiklerini biriktirir, sonraki kararlarında bu verileri kullanır.

## Mimari

Yeni bileşen yok — mevcut reflector workflow'u genişletilir.

```
Agent aksiyon alır (reprice, crosslist, scout discovery...)
  → Event emit edilir (zaten var: amz.reprice.recommended, amz.crosslist.candidate...)
    → Reflector günlük çalışır:
       1. Son 24h aksiyonları çek (events tablosundan)
       2. Her aksiyon için sonucu kontrol et (tool çağrısıyla)
       3. Outcome'u memory'ye yaz
       4. Agent state'inde strategy_stats güncelle
       5. LLM ile öğrenme insight'ı üret
         → ReAct konuşmalarda agent bu stats'ı context olarak görür
```

## 1. Outcome Kontrol Kuralları

Her aksiyon tipinin kendi outcome checker'ı var:

| Aksiyon | Event Tipi | Outcome Check | Araç | Başarı Kriteri |
|---------|-----------|---------------|------|----------------|
| Reprice | `amz.reprice.recommended` | 24h sonra Buy Box durumu | `buyboxpricer.db.lost_buybox` | listing artık lost_buybox listesinde DEĞİL |
| CrossList | `amz.crosslist.candidate` | 48h sonra eBay satış | `ebaycrosslister.db.listable_items` | listing satıldı veya aktif |
| Scout Discovery | `amz.opportunity.discovered` | 7 gün sonra confirmed mı | events tablosu | aynı ASIN için `amz.opportunity.confirmed` var mı |
| Analyst Verdict | `amz.opportunity.confirmed` | 7 gün sonra alındı mı | memory search | satın alma kaydı var mı |
| App Growth | `app.growth.weekly_report` | sonraki hafta MRR değişimi | `hub.api.overview` | MRR arttı mı |

## 2. Reflector Genişletmesi

`studioos/workflows/amz_reflector.py` güncellenir:

### Yeni node: `node_check_outcomes`

Mevcut akışa eklenir:
```
START → collect → reflect → check_outcomes → report → END
```

`node_check_outcomes`:
- Events tablosundan son 24-48h'deki aksiyon event'lerini çek
- Her birinin outcome checker'ını çalıştır
- Sonucu `outcome_results` listesine yaz:
  ```python
  {
      "action_type": "reprice",
      "asin": "B00XYZ",
      "strategy": "buy_box_win",
      "outcome": "success",  # success | failure | pending | unknown
      "detail": "Buy Box kazanıldı, 24h içinde recovered"
  }
  ```
- Agent state'inde `strategy_stats` güncelle:
  ```python
  state["strategy_stats"] = {
      "buy_box_win": {"total": 45, "success": 33, "rate": 0.73},
      "profit_max": {"total": 12, "success": 5, "rate": 0.42},
      "stock_bleed": {"total": 8, "success": 7, "rate": 0.88},
  }
  ```

### Yeni node: `node_learning_insight`

LLM'e outcome sonuçlarını + strategy_stats'ı ver:
- "Bu hafta buy_box_win %73 başarılı, profit_max %42. Ne önerirsin?"
- LLM insight üretir → memory'ye yaz (procedural memory)
- Bu insight sonraki kararları etkiler

## 3. ReAct Context Entegrasyonu

`react_conversation.py` `node_load_context`'te `strategy_stats`'ı state'den çekip system prompt'a ekle:

```
Geçmiş performansın:
- buy_box_win: %73 başarılı (45 aksiyon, 33 başarı)
- profit_max: %42 başarılı (12 aksiyon, 5 başarı)
- stock_bleed: %88 başarılı (8 aksiyon, 7 başarı)

Bu verilere dayanarak karar ver.
```

## 4. Outcome Checker Pure Functions

`studioos/workflows/outcome_checker.py` (yeni dosya):

```python
OUTCOME_RULES = {
    "amz.reprice.recommended": {
        "check_after_hours": 24,
        "check_tool": "buyboxpricer.db.lost_buybox",
        "success_if": "asin NOT in lost_buybox list",
    },
    "amz.crosslist.candidate": {
        "check_after_hours": 48,
        "check_tool": None,  # manual for now
        "success_if": "listing active on eBay",
    },
    "amz.opportunity.discovered": {
        "check_after_hours": 168,  # 7 days
        "check_tool": None,  # check events table
        "success_if": "confirmed event exists for same ASIN",
    },
}
```

## 5. Kapsam

### Bu spec'te:
- AMZ Reflector'a `node_check_outcomes` + `node_learning_insight` ekle
- `outcome_checker.py` pure functions
- ReAct context'e strategy_stats ekle
- Reprice outcome checker (en somut, test edilebilir)

### Gelecek:
- App Studio reflector'a aynı pattern
- CrossList + Scout outcome checker'ları
- Otomatik strateji ağırlık ayarlama (strategy_stats'a göre)

## 6. Test Plan

1. **Outcome checker**: reprice event + lost_buybox verisi → success/failure doğru mu
2. **Strategy stats**: state'e doğru yazılıyor mu
3. **ReAct context**: strategy_stats prompt'a ekleniyor mu
