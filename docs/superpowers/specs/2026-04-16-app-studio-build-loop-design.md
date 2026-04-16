# M30: App Studio Build Loop — Dev + QA

> Orchestration-only build agents. İzleme + koordinasyon + raporlama. Gerçek kod exec (git commit, codemagic trigger) gelecek milestone'da eklenir.

## Kapsam

2 agent + 4 event schema + studio.yaml güncelleme.

### Kapsam dışı

- Game Designer (gelecek — Game Studio kurulunca)
- Gerçek kod yazma / git commit / codemagic build trigger (gelecek exec milestone)
- GitHub API entegrasyonu (gelecek — şimdilik exec.git_status yeterli)

---

## 1. Yeni Event Schemas

`studioos/events/schemas_app.py`'ye eklenir (mevcut dosya):

| Event Type | Emitter | Payload |
|-----------|---------|---------|
| `app.build.completed` | dev | `{app_id, repo, commit_sha, build_status, summary}` |
| `app.build.failed` | dev | `{app_id, repo, error, commit_sha}` |
| `app.qa.passed` | qa | `{app_id, checks_passed, checks_total, summary}` |
| `app.qa.failed` | qa | `{app_id, checks_passed, checks_total, failed_checks[], summary}` |

(CEO delegation event'leri `app.task.dev` ve `app.task.qa` zaten AppTaskV1 ile kayıtlı.)

---

## 2. app-studio-dev

**Schedule:** `@every 1h` (pulse modu)
**Event triggers:** `app.task.dev`
**Template:** `app_studio_dev`

**Nodes:** START → collect → report → END

**collect:**
- goals.repos listesindeki her repo için `exec.git_status` çağır (AMZ Dev pattern'i)
  - Repo'lar: `/home/deployer/openclaw/workspace/workspace/studios/app-studio/projects/quit_smoking_now`, `/home/deployer/openclaw/workspace/workspace/studios/app-studio/projects/sms_forward` (veya /srv altında — prod'da kontrol edilecek)
- Son 1 saatteki app-studio agent run failure'larını DB'den çek (AMZ Dev pattern'i)

**report:**
- Dirty repo varsa → Slack `#build` (C0AMWBZF67R) uyarı
- Failure varsa → Slack `#build` uyarı
- Temiz ise → kısa "all green" pulse
- Telegram notify
- Memory kaydet

**Goals:**
```yaml
repos: []  # prod'da doldurulacak — app repo path'leri
```

**Tool scope:** `exec.git_status`, `exec.git_log`, `telegram.notify`, `slack.notify`, `memory.search`

---

## 3. app-studio-qa

**Schedule:** `@every 6h`
**Event triggers:** `app.task.qa`
**Template:** `app_studio_qa`

**Nodes:** START → collect → check → verdict → END

**collect:**
- Hub API'den her tracked app için overview (crash signal olarak kullanılır — revenue drop = potansiyel bug)
- Son 6 saatteki app-studio run'larını DB'den oku (failure count)

**check:**
- Deterministic health checks:
  - Hub API overview çalışıyor mu? (basit connectivity)
  - Son 6h'de failure oranı > %20 mi? → flag
  - Revenue WoW drop > %30 mi? → flag (potansiyel bug/regression)

**verdict:**
- 0 flag → PASS, emit `app.qa.passed`
- 1+ flag → FAIL, emit `app.qa.failed`, @dev mention
- Slack `#build` + Telegram raporla
- PASS/FAIL formatı (AMZ QA pattern'ine benzer ama daha basit)

**Goals:**
```yaml
tracked_apps: [quit_smoking, sms_forward]
failure_rate_threshold: 20.0
revenue_drop_threshold: 30.0
```

**Tool scope:** `hub.api.overview`, `telegram.notify`, `slack.notify`, `memory.search`

---

## 4. Slack Routing

Mevcut env var'lara eklenir:

STUDIOOS_SLACK_AGENT_TOKENS'a:
```
app-studio-dev=xoxb-REDACTED-dev-token
app-studio-qa=xoxb-REDACTED-qa-token
```

STUDIOOS_SLACK_AGENT_CHANNELS'a:
```
app-studio-dev=C0AMWBZF67R
app-studio-qa=C0AMWBZF67R
```

(İkisi de `#build` kanalına yazıyor.)

Gerçek token'lar:
- Dev: `xoxb-REDACTED`
- QA: `xoxb-REDACTED`

---

## 5. studio.yaml

Mevcut 7 agent'a 2 yeni eklenir (toplam 9). Templates + subscriptions.

CEO'nun valid delegation targets'ına `app-studio-dev` ve `app-studio-qa` eklenir.

Subscriptions:
```yaml
- subscriber: app-studio-dev
  event_pattern: "app.task.dev"
  priority: 20
- subscriber: app-studio-qa
  event_pattern: "app.task.qa"
  priority: 20
```

---

## 6. Test Plan

1. **QA health check logic**: deterministic flag detection (failure rate, revenue drop)
2. **Event schema registration**: 4 yeni event type
3. **Smoke import**: both workflows compile
