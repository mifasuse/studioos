# M35: Dev Agent Exec — Code Mutation + Deploy Trigger

> Agent'ların kod yazıp commit edebilmesi, push ile GHA deploy tetiklemesi, Codemagic build başlatması.

## Kapsam

4 yeni tool, hepsi approval-gated. Mevcut read-only exec tool'larına (git_status, git_log, docker_compose_ps) eklenir.

### Kapsam dışı
- Doğrudan `docker compose up` veya `alembic upgrade` (GHA pipeline yapıyor)
- Branch yönetimi (sadece main branch'e push)
- PR oluşturma (gelecek milestone)

---

## 1. Tool'lar

### `exec.git_commit`

Bir repo'da dosya değişikliği + commit oluşturur.

- Input: `repo` (path, allow-list'te olmalı), `message` (commit message), `files` (list of relative paths to stage)
- Davranış: `cd {repo} && git add {files} && git commit -m "{message}"`
- **Approval-gated:** tool çağrıldığında approval row oluşturur, onay gelene kadar commit yapılmaz
- Güvenlik: allow-list enforcement, `.env` ve credential dosyaları commit edilemez (hardcoded blocklist)

### `exec.git_push`

Bir repo'yu remote'a push eder. Push → GHA deploy.yml otomatik tetiklenir.

- Input: `repo` (path), `branch` (default "main")
- Davranış: `cd {repo} && git push origin {branch}`
- **Approval-gated**
- Güvenlik: `--force` yasak, sadece fast-forward push

### `exec.gh_workflow_dispatch`

GitHub Actions workflow'unu manuel tetikler (push olmadan deploy).

- Input: `repo_name` (format: "owner/repo", ör. "mifasuse/pricefinder"), `workflow` (default "deploy.yml"), `branch` (default "main")
- Davranış: `gh workflow run {workflow} -R {repo_name} --ref {branch}`
- **Approval-gated**
- Güvenlik: sadece allow-list'teki repo'lar

### `exec.codemagic_trigger`

Codemagic CI/CD build tetikler (App Studio uygulamaları için).

- Input: `app_id` (Codemagic app ID), `branch` (default "main")
- Davranış: `POST https://api.codemagic.io/builds` with `{"appId": app_id, "workflowId": "default", "branch": branch}`
- Auth: `STUDIOOS_CODEMAGIC_TOKEN` header
- **Approval-gated**

---

## 2. Approval Gate Mekanizması

Tüm exec tool'ları aynı pattern'i kullanır:

1. Tool çağrılır → approval row oluşturulur (mevcut `approvals` tablosu)
2. Agent'ın run'ı `awaiting_approval` state'ine geçer
3. Dashboard'da veya Slack'te onay gelir
4. Onay sonrası tool gerçekten execute edilir

Bu pattern zaten `amz-repricer`'da çalışıyor (dry_run=true modunda). Aynı mekanizma.

---

## 3. Güvenlik

### Allow-list
Mevcut `STUDIOOS_DEV_REPO_ALLOWLIST` enforce edilir. Listede olmayan repo'ya commit/push yapılamaz.

### Blocklist (hardcoded)
Commit'te stage edilemeyecek dosyalar:
- `.env`, `.env.*`
- `*credentials*`, `*secret*`, `*token*`
- `*.pem`, `*.key`

### Destructive command block
- `git push --force` → reddet
- `git reset --hard` → reddet
- Branch silme → reddet

---

## 4. Config

Yeni env var'lar:
- `STUDIOOS_CODEMAGIC_TOKEN` — Codemagic API token (mevcut: `xFg55kB_6318LK-YOFw7Jiw33CtcRgqdvd5oatutEjg`)
- `STUDIOOS_GH_TOKEN` — GitHub token for `gh` CLI (mevcut: OpenClaw'dan)

Mevcut:
- `STUDIOOS_DEV_REPO_ALLOWLIST` — allow-list'e App Studio repo'ları da eklenir

---

## 5. Test Plan

1. **Allow-list enforcement**: izin olmayan repo → reddet
2. **Blocklist**: `.env` stage etmeye çalış → reddet
3. **Force push block**: `--force` flag → reddet
4. **Approval gate**: tool çağrıldığında approval row oluşur
5. **Codemagic trigger**: mock HTTP, doğru payload gönderildi mi
