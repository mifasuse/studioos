# M32: Slack Inbound — Agent'larla Konuşma

> Slack'ten gelen mention'ları StudioOS event'lerine çevirip ilgili agent'ı tetikleyen webhook altyapısı. Agent-to-agent mention desteği dahil.

## Kapsam

- `POST /slack/events` webhook endpoint
- Mention-based routing (`@agent-name mesaj` → agent run tetikle)
- Agent yanıtlarında mention detection → zincirleme agent tetikleme
- Thread-based yanıtlar (ana kanal temiz kalır)
- Kanal sadeleştirme: her studio tek kanal (`#amz-hq`, `#app-hq`)

### Kapsam dışı

- Socket Mode (webhook yeterli)
- Slash commands
- Interactive components (buttons, modals)
- DM desteği (sadece kanal mention)

---

## 1. Mimari

```
Slack                    StudioOS
──────                   ────────
User: @amz-pricer        POST /slack/events
  "stok eritme           ─────────────────►
   stratejisi ne?"       
                         1. 200 OK (immediate, <3s)
                         2. Parse: mention → agent_id=amz-pricer
                         3. Emit event: slack.mention.received
                         4. Subscription match → amz-pricer run
                         5. Workflow runs (LLM reasoning)
                         6. Agent responds via slack.notify
                            (same thread_ts)
              ◄──────────────────────────
              amz-pricer:
              "Mevcut aging stock..."
```

### Agent-to-agent mention

```
Agent amz-pricer yanıtında yazıyor:
  "@amz-analyst bu ASIN'in risk skoruna bak"

slack.notify gönderildikten sonra:
  → yanıt metninde @mention var mı? parse et
  → varsa: yeni slack.mention.received event emit et
  → amz-analyst tetiklenir, aynı thread'de yanıtlar

Cascade koruması:
  → Aynı thread'de aynı agent max 3 kez tetiklenebilir
  → Bir agent kendi kendini mention edemez (ignore)
  → Thread depth > 10 → yeni mention'lar ignore (sonsuz döngü koruması)
```

---

## 2. Webhook Endpoint

**File:** `studioos/api/slack_events.py` (yeni modül, main.py'ye mount edilir)

### URL verification

```python
@router.post("/slack/events")
async def slack_events(request: Request) -> dict:
    body = await request.json()
    
    # Slack URL verification challenge
    if body.get("type") == "url_verification":
        return {"challenge": body["challenge"]}
    
    # Event callback
    if body.get("type") == "event_callback":
        event = body.get("event", {})
        # Hemen 200 dön — 3s timeout
        background_tasks.add_task(process_slack_event, body)
        return {"ok": True}
    
    return {"ok": True}
```

### Slack request verification

- `X-Slack-Signature` header + `STUDIOOS_SLACK_SIGNING_SECRET` ile HMAC-SHA256 doğrulama
- Timestamp replay attack koruması (5 dakikadan eski mesajları reddet)

### Event processing

```python
async def process_slack_event(body: dict) -> None:
    event = body.get("event", {})
    event_type = event.get("type")
    
    if event_type != "app_mention":
        return  # Sadece mention'lara yanıt ver
    
    text = event.get("text", "")
    user = event.get("user")
    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    ts = event.get("ts")
    
    # Mention'dan agent_id çıkar
    # Format: <@BOT_USER_ID> mesaj
    # Bot user ID → agent_id mapping gerekli
    agent_id = resolve_agent_from_mention(text, channel)
    if not agent_id:
        return
    
    # Idempotency: aynı ts + agent_id → skip
    # Thread depth check: > 10 → skip
    # Self-mention check: agent kendi mention'ını ignore
    
    # StudioOS event emit
    emit_event("slack.mention.received", {
        "agent_id": agent_id,
        "text": clean_mention_text(text),
        "user": user,
        "channel": channel,
        "thread_ts": thread_ts,
        "message_ts": ts,
    })
```

---

## 3. Bot User ID → Agent ID Mapping

Slack mention formatı: `<@U1234ABCD>` (user ID). Her bot'un user ID'si farklı.

**Çözüm:** Startup'ta `auth.test` API çağrısı yaparak her bot token'ın user_id'sini çek, cache'le.

```python
# Map: slack_bot_user_id → agent_id
# Populated at startup from STUDIOOS_SLACK_AGENT_TOKENS
BOT_USER_MAP: dict[str, str] = {}  # {"U0ABC123": "amz-pricer", ...}
```

Bu map bir kez oluşturulur (startup), sonra her gelen mention'da lookup yapılır.

---

## 4. Event Schema

`studioos/events/schemas_slack.py` (yeni dosya):

```
slack.mention.received  →  {agent_id, text, user, channel, thread_ts, message_ts, studio_id}
slack.mention.responded →  {agent_id, text, channel, thread_ts, response_ts}
```

---

## 5. Subscription + Workflow Değişiklikleri

### Yeni subscription pattern

Her agent'a `slack.mention.received` subscription eklenmez — bunun yerine webhook handler, mention'daki agent_id'yi doğrudan event payload'ına koyar ve **genel bir `slack-router` agent** event'i emit eder.

Daha basit: webhook doğrudan hedef agent'ın run'ını oluşturur (subscription bypass). `trigger_type = "slack_mention"`.

### Agent workflow değişikliği

Mevcut workflow'lar `trigger_type == "schedule"` veya `trigger_type == "event"` ile tetikleniyor. Yeni: `trigger_type == "slack_mention"`.

Agent'ın `input` dict'i:
```python
{
    "event_type": "slack.mention.received",
    "payload": {
        "text": "stok eritme stratejisi ne?",
        "user": "U_NURI",
        "channel": "C0AP9GQMB1R",
        "thread_ts": "1713250000.000100",
    }
}
```

Agent workflow `node_report` / `node_emit`'te yanıt verirken `slack.notify`'a `thread_ts` parametresi geçer → yanıt aynı thread'e gider.

### Agent-to-agent chaining

`slack.notify` tool çağrıldıktan sonra, runner yanıt metnini parse eder:
- `<@BOT_USER_ID>` pattern bulursa → yeni `slack.mention.received` event emit eder
- `thread_ts` korunur (aynı thread devam eder)
- Cascade koruma: `state.mention_depth` sayacı, max 3

---

## 6. Config

Yeni env var:
- `STUDIOOS_SLACK_SIGNING_SECRET` — Slack app'in signing secret'ı (webhook doğrulama)

Mevcut env var'lar yeterli (agent tokens + channels zaten var).

---

## 7. Kanal Sadeleştirme

Mevcut kanallar (AMZ: 6, App: 9 = 15 kanal) → sadeleştirilir:

**AMZ:** `#amz-hq` (C0AP9GQMB1R) — tek kanal, herkes burada
**App:** `#app-hq` (C0AMWBZN39V) — tek kanal

Diğer kanallar silinmez (archive) ama agent'lar artık sadece HQ'ya yazar. `STUDIOOS_SLACK_AGENT_CHANNELS`'taki tüm agent → channel mapping'ler HQ channel ID'sine güncellenir.

---

## 8. Cascade Koruması

| Kural | Uygulama |
|-------|----------|
| Aynı thread'de aynı agent max 3 kez | `thread_ts + agent_id` sayaç (Redis veya in-memory) |
| Agent kendi kendini mention edemez | webhook handler'da `mentioned_agent == responding_agent` → skip |
| Thread depth > 10 | thread'deki toplam mesaj sayısı (Slack API `conversations.replies` count) |
| Bot mesajları ignore | `event.bot_id` varsa → skip (bot'lar birbirini doğrudan tetikleyemez, sadece mention ile) |
| Rate limit | Agent başına dakikada max 5 slack-triggered run |

---

## 9. Test Plan

1. **URL verification**: challenge echo
2. **Mention parsing**: `<@U123>` → agent_id resolution
3. **Cascade protection**: self-mention ignore, depth limit, rate limit
4. **Thread-based response**: slack.notify'a thread_ts geçiyor mu
5. **Idempotency**: aynı ts + agent_id → duplicate skip
6. **Agent-to-agent**: yanıt metninde mention → yeni event
