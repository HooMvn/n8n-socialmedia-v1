# راهنمای اجرای محلی Risheh Engage

این راهنما برای توسعه‌دهنده‌ای است که برای اولین‌بار پروژه را اجرا می‌کند.

## پیش‌نیازها

- Git
- Docker Desktop یا Docker Engine + Compose
- حداقل 4GB RAM آزاد برای اجرای راحت Stack توسعه
- پورت‌های آزاد: `3000`, `5432`, `5678`, `6379`, `8000`

## 1. دریافت پروژه

```bash
git clone https://github.com/HooMvn/n8n-socialmedia-v1.git
cd n8n-socialmedia-v1
```

## 2. Environment

```bash
cp .env.example .env
```

برای محیط واقعی حتماً secretهای زیر را عوض کنید:

```text
APP_SECRET_KEY
N8N_WEBHOOK_SECRET
N8N_ENCRYPTION_KEY
```

> `.env.example` فقط قرارداد تنظیمات است؛ Secret واقعی نباید Commit شود.

## 3. اجرای Stack

```bash
docker compose up --build
```

ترتیب مورد انتظار:

```text
PostgreSQL healthy
Redis healthy
      ↓
migrate → alembic upgrade head
      ↓
API
      ↓
Web

n8n در کنار این جریان بالا می‌آید.
```

## 4. بررسی سرویس‌ها

### API health

```bash
curl http://localhost:8000/health
```

خروجی:

```json
{"status":"ok","service":"api","environment":"development"}
```

### Readiness واقعی

```bash
curl http://localhost:8000/ready
```

این Endpoint دیتابیس را Probe می‌کند. اگر PostgreSQL قابل دسترسی نباشد باید `503` بدهد.

### OpenAPI

```text
http://localhost:8000/docs
```

### Web

```text
http://localhost:3000
```

### Vertical Slice Studio

```text
http://localhost:3000/studio
```

### n8n

```text
http://localhost:5678
```

## 5. Smoke Test

پس از بالا آمدن Stack:

```bash
python -m pip install httpx
python scripts/smoke_vertical_slice.py
```

این تست موارد زیر را واقعاً روی API و PostgreSQL اجرا می‌کند:

```text
Workspace
→ Brand
→ Content Draft
→ Review
→ Approval
→ Publish Request
→ Idempotent Replay
→ Outbox boundary
```

Smoke Test عمداً هیچ محتوایی را روی شبکه اجتماعی واقعی منتشر نمی‌کند.

## 6. Migration

مشاهده وضعیت:

```bash
docker compose run --rm migrate alembic current
```

ارتقا:

```bash
docker compose run --rm migrate alembic upgrade head
```

ساخت Migration جدید در توسعه:

```bash
cd apps/api
alembic revision --autogenerate -m "describe change"
```

قبل از Commit، Migration تولیدشده باید دستی Review شود.

## 7. Reset دیتابیس توسعه

هشدار: تمام داده محلی پاک می‌شود.

```bash
docker compose down -v
docker compose up --build
```

## 8. تست Backend

```bash
cd apps/api
pip install -e '.[dev]'
ruff check .
pytest
```

## 9. Build Frontend

```bash
cd apps/web
npm install
npm run build
```

## 10. خطاهای رایج

### API بالا می‌آید ولی `/ready` خطا دارد

ابتدا وضعیت PostgreSQL و Migration را ببینید:

```bash
docker compose ps
docker compose logs db
docker compose logs migrate
```

### Studio خطای CORS می‌دهد

مقدار `CORS_ORIGINS` باید Origin واقعی Web را داشته باشد. برای توسعه:

```text
http://localhost:3000
```

### Migration اجرا نمی‌شود

بررسی کنید Docker image جدید شامل پوشه `alembic/` و `alembic.ini` باشد و Build cache قدیمی نباشد:

```bash
docker compose build --no-cache migrate
```

### n8n به API دسترسی ندارد

داخل شبکه Docker، API با این آدرس دیده می‌شود:

```text
http://api:8000
```

از داخل n8n نباید `localhost:8000` برای Backend استفاده شود؛ localhost داخل Container به خود n8n اشاره می‌کند.

## تعریف موفقیت این مرحله

اگر موارد زیر درست باشند Foundation Slice سالم است:

- `/health` = 200
- `/ready` = 200
- Studio تا `publish_requested` جلو می‌رود
- Publication و Outbox رکورد ایجاد می‌شوند
- تکرار Publish با همان idempotency key رکورد جدید نمی‌سازد
- `pytest` و `ruff` پاس می‌شوند
- `npm run build` پاس می‌شود

بعد از این Gate، تیم اجازه دارد Provider Adapter واقعی و Slice دوم Inbox/Lead را شروع کند.
