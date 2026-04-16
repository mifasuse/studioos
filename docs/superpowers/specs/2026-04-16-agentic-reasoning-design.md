# M33: Agentic Reasoning — ReAct Loop

> Agent'ları sabit pipeline'dan çıkarıp "düşün → araç kullan → gözlemle → tekrar düşün → yanıt ver" döngüsüne sokan paylaşımlı ReAct workflow.

## Problem

Mevcut: `START → A → B → C → END` — her agent sabit adımları sırayla koşar. Slack'ten "stok eritme stratejisi ne?" diye sorulduğunda agent mesajı okumaz, her zamanki pipeline'ını çalıştırır.

Hedef: Agent mesajı okur, ne yapması gerektiğini düşünür, ihtiyacı olan tool'ları çağırır, sonucu yorumlar, yanıt verir.

## Mimari

### Tek paylaşımlı workflow: `react_conversation`

```
START → load_context → think → [conditional]
                                  ├─ "call_tool" → execute_tool → think (loop)
                                  ├─ "respond"   → format_response → END
                                  └─ max_iterations → force_respond → END
```

Bu workflow her agent için çalışır — agent_id'den:
- **Persona** (system prompt) türetilir
- **Tool scope** (hangi tool'ları kullanabilir) çekilir
- **Context** (son memory'ler, KPI'lar) yüklenir

### LangGraph conditional edge

LLM'in yanıtına göre next node belirlenir:
- LLM bir tool çağrısı döndürürse → `execute_tool` node → sonucu `think`'e geri ver
- LLM final yanıt döndürürse → `format_response` → Slack thread / Telegram / event
- Max 5 iterasyon → zorla yanıt ver (sonsuz döngü koruması)

### Trigger'lar

Bu workflow şu trigger'larla çalışır:
- `slack_mention` — kullanıcı Slack'te agent'ı mentionladı
- `event` — başka bir agent task delegation yaptı (`app.task.*`, `amz.task.*`)

Schedule trigger'lar eski pipeline workflow'larını kullanmaya devam eder. İleride schedule trigger'lar da ReAct'e geçirilebilir ama bu scope'ta değil.

---

## Detay

### 1. Agent Persona Registry

Her agent için bir system prompt. `studioos/workflows/personas.py`:

```python
PERSONAS = {
    "amz-pricer": """Sen AMZ Pricer — Amazon fiyat stratejistisin.
BuyBoxPricer verilerine bakarak repricing önerileri yaparsın.
Kullanabildiğin tool'lar: buyboxpricer.db.lost_buybox, buyboxpricer.db.aging_inventory.
Türkçe yanıt ver. Kısa, somut, rakam odaklı ol.""",

    "amz-scout": """Sen AMZ Scout — fırsat avcısısın.
PriceFinder'dan yüksek ROI'li arbitraj fırsatlarını bulursun.
Kullanabildiğin tool'lar: pricefinder.db.scout_candidates.
Türkçe yanıt ver.""",

    # ... her agent için
}
```

Bilinmeyen agent → genel asistan prompt.

### 2. load_context node

- Agent'ın son 5 memory'sini çek (`memory.search`)
- Agent'ın tool_scope'unu DB'den çek
- Slack mention ise: thread geçmişini al (bağlam için)
- Bu bilgileri state'e yaz

### 3. think node

LLM çağrısı:
- System prompt: agent persona + "Tool çağırmak istersen JSON formatında belirt"
- Messages: context + kullanıcı mesajı + önceki tool sonuçları
- Response format: ya final text yanıt, ya tool çağrısı

Tool çağrısı formatı (LLM'den):
```json
{"tool": "buyboxpricer.db.lost_buybox", "args": {"limit": 10}}
```

Final yanıt: düz text (tool çağrısı yoksa).

### 4. execute_tool node

- LLM'in istediği tool'u `invoke_from_state` ile çağır
- Tool scope kontrolü: agent'ın erişim hakkı var mı?
- Sonucu state'e ekle (observation)
- `think`'e geri dön

### 5. format_response node

- LLM'in final yanıtını al
- Trigger'a göre gönder:
  - `slack_mention` → `slack.notify` + `thread_ts` (aynı thread'e)
  - `event` → Telegram + memory kaydet
- `slack.mention.responded` event emit et

### 6. Cascade — agent-to-agent

Agent yanıtında başka bir agent mention ederse (`@StudioOS analyst bu ASIN'e bak`):
- `format_response` yanıt metnini parse eder
- Mention varsa → yeni `slack.mention.received` event emit → zincirleme tetikleme
- Mevcut cascade koruması geçerli (max 3 per thread, self-mention block)

---

## Config

Yeni env var yok. Mevcut LLM config (`llm.chat`) kullanılır.

## Tool scope

Her agent'ın `studio.yaml`'daki `tool_scope` listesi enforce edilir. ReAct loop'ta LLM sadece o agent'ın erişebildiği tool'ları çağırabilir.

Ek olarak: `llm.chat` her agent'a verilir (reasoning için gerekli), `slack.notify` her agent'a verilir (yanıt vermek için gerekli).

---

## Test Plan

1. **Persona lookup**: agent_id → system prompt
2. **Tool scope enforcement**: agent yetkisi olmayan tool çağrısı → reddet
3. **Max iteration**: 5 tool çağrısından sonra zorla yanıt
4. **Slack thread response**: yanıt doğru thread_ts'e gidiyor mu
5. **Cascade chaining**: yanıtta mention → yeni agent tetikleniyor mu

---

## Kapsam dışı (gelecek)

- Schedule trigger'ları ReAct'e geçirme (Katman 2)
- Multi-turn conversation (aynı thread'de devam eden diyalog)
- Tool auto-discovery (LLM'in kullanılabilir tool listesini görmesi — şimdilik persona'da elle yazılı)
