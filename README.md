<div align="center">

# 🌿 Risheh Engage

### AI Content & Customer Engagement Platform

**از محتوا تا مکالمه، از مکالمه تا پاسخ مستند، از Customer تا Lead و Conversion — با Human Control، Grounded AI و Automation قابل‌اعتماد**

`Content → Conversation → Grounded AI → Human Review → Outbound → Lead → Follow-up → Conversion → Learning`

[![Version](https://img.shields.io/badge/version-v0.8_Final_Candidate-174A3A?style=flat-square)](#-وضعیت-release)
[![Architecture](https://img.shields.io/badge/Architecture-Modular_Monolith-111827?style=flat-square)](#-معماری)
[![Automation](https://img.shields.io/badge/Automation-n8n-EA4B71?style=flat-square)](#-نقش-n8n)
[![AI](https://img.shields.io/badge/AI-Grounded_%2B_Human_Review-312E81?style=flat-square)](#-ai--rag)
[![Database](https://img.shields.io/badge/Source_of_Truth-PostgreSQL-336791?style=flat-square)](#-اصل-معماری)
[![Release](https://img.shields.io/badge/Next-Pilot_%E2%86%92_v1.0-0F766E?style=flat-square)](#-مسیر-v10)

</div>

---

## ✨ Risheh Engage چیست؟

Risheh Engage یک **Product Platform برای Content Operations + Customer Communication + LeadOps** است.

هدف پروژه ساخت چند Workflow شبکه اجتماعی نیست. هدف ایجاد یک Product Layer واحد است که بتواند:

- محتوا را بسازد، Review و Publish کند؛
- پیام‌های چند کانال را وارد یک Domain استاندارد کند؛
- Customer و Identity را مدیریت کند؛
- Unified Inbox ارائه دهد؛
- Intent را با AI تشخیص دهد؛
- پاسخ را با Knowledge معتبر Ground کند؛
- Evidence را به Reviewer نشان دهد؛
- AI Draft را قبل از ارسال کنترل کند؛
- پاسخ Human و AI-approved را از یک Outbound Pipeline ارسال کند؛
- Lead و Follow-up بسازد؛
- Consent، SLA و Escalation را enforce کند؛
- Customer 360 و Attribution foundation ارائه دهد؛
- Failure/Retry/Delivery را قابل مشاهده کند.

---

# 🧭 Product Flow

```text
Idea / Product / URL
        ↓
     ContentOps
        ↓
   Human Approval
        ↓
      Publish
        ↓
 Audience Interaction
        ↓
  Channel Adapter
        ↓
 Inbound Message
        ↓
Customer Identity
        ↓
     Customer
        ↓
  Conversation
        ↓
Intent Classifier
        ↓
 Risk Policy
   ┌────┴─────┐
   ↓          ↓
Grounded AI  Human
   ↓          ↓
Evidence   Handoff
   ↓          ↓
Review Queue ─┘
        ↓
 OutboundMessage
        ↓
Transactional Outbox
        ↓
 Reliable Worker
        ↓
       n8n
        ↓
 Provider Adapter
        ↓
Delivery Callback
        ↓
 Lead / Follow-up
        ↓
 Attribution
```

---

# 🏗️ معماری

**Modular Monolith + Event-driven Automation**

```text
┌─────────────────────────────────────────┐
│              Next.js UI                 │
│ Studio · Inbox · AI Queue · Knowledge  │
└───────────────────┬─────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│               FastAPI                   │
│ Auth · Domain · Policy · AI · Outbound │
└───────────────────┬─────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│             PostgreSQL                  │
│             Source of Truth             │
└───────────────────┬─────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│ Outbox · Worker · Retry · Backoff · DLQ│
└───────────────────┬─────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│                  n8n                    │
│        Provider / Channel Adapters      │
└───────────────────┬─────────────────────┘
                    ↓
      Telegram · Instagram · WhatsApp
```

## اصل معماری

1. **PostgreSQL = Source of Truth**
2. **n8n = Orchestration / Adapter Layer، نه Backend**
3. AI مستقیماً Business State را مالک نیست.
4. Confidence با Permission فرق دارد.
5. Human Review برای Actionهای حساس حفظ می‌شود.
6. هر داده Tenant-owned با Workspace scope کنترل می‌شود.
7. Retry باید bounded و observable باشد.
8. Webhook/Outbound باید Idempotent طراحی شوند.
9. Customer با Provider Identity یکی نیست.
10. Follow-up بدون Stop Condition یک Bug است.
11. Production با Config ناامن نباید Boot شود.
12. Release فقط با Gate قابل اندازه‌گیری انجام می‌شود.

---

# 🧩 ماژول‌های پیاده‌سازی‌شده

## ContentOps

`Draft → Review → Approved → Publish Requested → Published / Failed`

شامل Brand Profile، Approval، Publication، Idempotency، Outbox و Provider boundary.

## Authentication / RBAC

- JWT Access Token
- Argon2 password hashing
- Workspace Membership
- Roleهای `owner/admin/editor/reviewer/analyst/viewer`
- Tenant access guard

## Customer Communication

- Customer
- CustomerIdentity
- Conversation
- Message
- Inbound deduplication
- Conversation reuse
- Unified Inbox
- Human Handoff

## LeadOps / Follow-up

- Conversation → Lead
- Lead score / pipeline
- Follow-up lifecycle
- Customer reply stop condition
- terminal Lead protection

## AI / RAG

- OpenAI-compatible AI interface
- Intent Classification
- Structured output validation
- deterministic Risk Policy
- Fail-Closed behavior
- AI Draft persistence
- Human Review Queue
- KnowledgeSource / KnowledgeChunk
- RetrievalEvidence
- Grounded Draft: **No Evidence → No Grounded Draft**

> Retrieval فعلی Foundation قابل‌تست دارد. Semantic/vector retrieval و evaluation در Pilot hardening قابل ارتقا هستند؛ نبود آن مانع استفاده از مدل Evidence-based فعلی نمی‌شود.

## Identity Hardening

- IdentityVerification
- verified-only merge
- conflict guards
- حفظ Conversation/Message/Outbound/Attribution/Lead/Consent هنگام Merge

## Consent / Opt-out

Consent با Purpose + Channel scope ذخیره می‌شود.

- Marketing/Sales Follow-up → explicit `granted`
- Support/Transactional → در صورت `revoked` Block

## SLA / Escalation

- First Response SLA
- Resolution SLA field
- breach detection
- escalation level
- اولین Outbound موفق → first response recorded

## Provider Credentials

- Workspace-scoped credential
- encrypted-at-rest foundation
- Secret در API Response برنمی‌گردد
- Workflow export نباید Credential داشته باشد

## Customer 360 / Attribution

Customer 360 یک **Read Model** است، نه Source of Truth جدید.

Attribution foundation:

`Content / Source / Channel / Campaign → Touchpoint → Customer / Lead`

---

# ⚙️ Reliability

Outbox Worker:

```text
pending
  ↓
FOR UPDATE SKIP LOCKED
  ↓
processing
  ↓
Delivery
 ┌─┴─────────────┐
 ↓               ↓
success        failure
 ↓               ↓
delivered   retry/backoff
                 ↓
             dead_letter
```

داریم:

- stale lock recovery
- exponential backoff
- max attempts
- dead-letter state
- event-specific n8n routing

---

# 👁️ Observability — v0.8

API حالا دارد:

- `X-Request-ID`
- request count
- request latency histogram
- structured request completion log
- `/metrics`
- `/health`
- `/ready`

این Foundation برای Prometheus/Grafana یا Observability stack مشابه آماده است.

---

# 🔐 Production Safety — v0.8

وقتی:

```env
ENVIRONMENT=production
```

باشد، Application Configهای خطرناک را Reject می‌کند؛ از جمله:

- Secretهای placeholder/کوتاه
- CORS شامل localhost یا wildcard
- Database credential توسعه

این Gate جای Secret Manager/KMS را نمی‌گیرد؛ ولی جلوی Deploy اشتباه واضح را می‌گیرد.

---

# 🧪 Quality Gates — v0.8

CI طراحی‌شده برای:

```text
Critical static checks
↓
PostgreSQL service
↓
Alembic upgrade head
↓
Unit / Contract tests
↓
PostgreSQL integration tests
↓
Next.js production build
↓
Required release files gate
```

Integration test جدید Schema migration و constraint واقعی PostgreSQL را بررسی می‌کند.

> GitHub Actions این Repository در Runهای اخیر قبل از Stepها Fail شده (`steps=null`). تا وقتی Runner واقعاً Stepها را اجرا نکند، CI سبز اعلام نمی‌شود.

---

# 🚑 Backup / Restore

Backup:

```bash
DATABASE_URL='postgresql://...' ./ops/backup.sh
```

Restore محافظت‌شده:

```bash
DATABASE_URL='postgresql://...' \
ALLOW_DESTRUCTIVE_RESTORE=yes \
./ops/restore.sh backups/risheh-engage-....dump
```

Backup باید خارج از همان VPS نگهداری شود و Restore Drill دوره‌ای انجام شود.

---

# 🚀 Smoke Test

بعد از Deploy:

```bash
SMOKE_API_URL=https://api.example.com \
SMOKE_WEB_URL=https://engage.example.com \
python scripts/production_smoke.py
```

بررسی می‌شود:

- API health
- DB readiness
- metrics
- Web
- Inbox
- AI Queue
- Knowledge UI

---

# 🌐 Provider Status

| Provider | Architecture | Workflow/Foundation | Live Production Verification |
|---|---|---|---|
| Telegram | ✅ | ✅ inbound/outbound/publish | ⏳ نیازمند Credential و Live Smoke |
| Instagram | ✅ | Adapter contract آماده | ⏳ Live Meta verification |
| WhatsApp | ✅ | Adapter contract آماده | ⏳ Live Meta verification |

Provider را بدون Live Smoke برای مشتری Production-ready اعلام نکنید.

---

# 🧱 Stack

| Layer | Technology |
|---|---|
| Web | Next.js + React + TypeScript |
| API | Python + FastAPI |
| Database | PostgreSQL |
| ORM | SQLAlchemy |
| Migration | Alembic |
| Automation | n8n |
| Cache/Coordination | Redis |
| Auth | JWT + Argon2 |
| Secret encryption foundation | Fernet |
| Metrics | Prometheus client |
| Runtime | Docker Compose |
| CI | GitHub Actions |

---

# 🚀 اجرای Local

```bash
git clone https://github.com/HooMvn/n8n-socialmedia-v1.git
cd n8n-socialmedia-v1
cp .env.example .env
docker compose up --build
```

سرویس‌های Local:

- Web: `http://localhost:3000`
- API: `http://localhost:8000`
- API Docs: `http://localhost:8000/docs`
- n8n: `http://localhost:5678`

---

# 📚 مسیر مطالعه تیم

## Product / Architecture
- `docs/PROJECT_GUIDE_FA.md`
- `docs/ARCHITECTURE_FA.md`
- `docs/DOMAIN_MODEL_FA.md`
- `docs/TECHNICAL_GLOSSARY_FA.md`

## Customer / AI
- `docs/VERTICAL_SLICE_02_CUSTOMER_COMMUNICATION_FA.md`
- `docs/CUSTOMER_COMMUNICATION_GLOSSARY_FA.md`
- `docs/AI_CUSTOMER_COMMUNICATION_GUIDE_FA.md`
- `docs/AI_OUTBOUND_GLOSSARY_FA.md`

## Grounded AI / Hardening
- `docs/GROUNDED_AI_RAG_GUIDE_FA.md`
- `docs/PRODUCTION_HARDENING_GLOSSARY_FA.md`
- `docs/LEARNING_PATH_V07_FA.md`

## Finalization / Production
- `docs/FINALIZATION_GLOSSARY_FA.md`
- `docs/FINALIZATION_TRAINING_FA.md`
- `docs/PRODUCTION_DEPLOYMENT_FA.md`
- `docs/FINAL_RELEASE_CHECKLIST_FA.md`

## Automation
- `automation/PUBLISH_PIPELINE_FA.md`
- `automation/OUTBOUND_RUNBOOK_FA.md`
- `automation/MULTICHANNEL_ADAPTERS_V07_FA.md`

---

# 📊 وضعیت Release

### ✅ Product Core
Architecture و Domainهای اصلی تکمیل شده‌اند.

### ✅ v0.8 Finalization Code
- Production config guard
- Observability foundation
- PostgreSQL integration gate
- Migration in CI
- Smoke script
- Backup/Restore scripts
- Deployment Runbook
- Final Release Checklist
- Final training pack

### ⏳ Evidence لازم قبل از v1.0
- GitHub Actions باید واقعاً Stepها را اجرا و سبز شود
- Docker/runtime smoke روی ماشین واقعی
- Backup Restore Drill
- Telegram live smoke
- Providerهای Meta در صورت فعال‌شدن: live verification
- Pilot acceptance

---

# 🏁 مسیر v1.0

از اینجا **Feature Freeze** توصیه می‌شود.

```text
v0.8 Final Candidate
        ↓
CI واقعی و سبز
        ↓
Deploy محیط Pilot
        ↓
Production Smoke
        ↓
Backup / Restore Drill
        ↓
Telegram Live Test
        ↓
Pilot Customer Acceptance
        ↓
🌿 Risheh Engage v1.0
```

`v1.0` پایان توسعه نیست؛ اولین نسخه‌ای است که Evidence کافی برای ارائه به مشتری واقعی دارد.

---

<div align="center">

### 🌿 Risheh Engage

**Automation should disappear into the product. Evidence should remain visible.**

</div>
