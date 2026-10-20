# راهنمای Product Pipeline — WooCommerce → وبلاگ وردپرس → تلگرام/بله/روبیکا/واتساپ

این سند قرارداد端到 سرتِ Automation «محصول جدید» را توضیح می‌دهد: وقتی در WooCommerce محصولی ساخته
می‌شود، سیستم یک پست وبلاگ فارسی تولید می‌کند، آن را در وردپرس منتشر می‌کند و پیام معرفی محصول را
روی همه کانال‌های فعال (Telegram، Bale، Rubika، WhatsApp) فانت‌اوت می‌کند.

## اصل معماری

```text
WooCommerce Webhook  ->  FastAPI (ingest + snapshot + job)
FastAPI Outbox       ->  Outbox Worker               (at-least-once, retry/backoff/DLQ)
Outbox Worker        ->  n8n Workflow (risheh-product)
n8n                  ->  WordPress REST / Bot APIs
n8n                  ->  FastAPI Callbacks (publish-callback / notify-ack)
PostgreSQL           =  حقیقت کامل Business State؛ n8n فقط Orchestrator است
```

هیچ Secret داخل فایل JSON workflow ذخیره نشده است. همه کلیدها از `$env` ن8n خوانده می‌شوند.

## فایل‌ها

| فایل | نقش |
|---|---|
| `workflows/product_pipeline_v1.json` | Workflow قابل Import در n8n |
| `workflows/gen_product_pipeline.py` | اسکریپت تولید مجدد JSON (Source of Truth برای تغییرات) |

Workflow را دستی در n8n ویرایش نکنید؛ تغییرات را در `gen_product_pipeline.py` بدهید و دوباره تولید کنید.

## رویدادها (Outbox → n8n)

همه رویدادها با `POST /webhook/risheh-product` و هدر `x-automation-secret` (مقدار
`N8N_WEBHOOK_SECRET` بک‌اند) وارد می‌شوند. بدنه توسط `app/outbox_worker.py` ارسال می‌شود:

```json
{ "event_id": "...", "event_type": "product.*", "workspace_id": "...", "payload": { ... } }
```

| event_type | زمان ایجاد | payload کلیدی | شاخه Workflow |
|---|---|---|---|
| `product.content.generate.requested` | Webhook ورودی محصول جدید | `job_id`, `content_id`, `snapshot_id`, `workspace_id` | Generate Draft (API) |
| `product.wordpress.publish.requested` | Publish job (auto یا review) | `publication_id`, `title`, `body_markdown`, `excerpt`, `image`, `idempotency_key` | WP Create Post |
| `product.notify.fanout.requested` | موفق‌شدن انتشار وردپرس | `notification_id`, `message`, `links`, `targets[]` | Channel Router |

## سه شاخه Workflow

### ۱. تولید پیش‌نویس (Generate)
`Generate Draft (API)` صدا می‌زند:

```http
POST /api/v1/workspaces/{workspace_id}/products/jobs/{job_id}/generate-draft
x-automation-secret: <N8N_WEBHOOK_SECRET>
{ "instruction": "..." }
```

داخل API انجام می‌شود (نه در n8n): فراخوانی AI با grounding روی snapshot محصول، اعتبارسنجی
قطعی (validate_draft)، چرخه بازنگری خودکار تا `product_max_revisions` و در حالت
`PRODUCT_AUTO_PUBLISH=true` انتشار خودکار. اگر AI خطا دهد، Job در دیتابیس به `failed` می‌رود و
Callback لازم نیست.

### ۲. انتشار وردپرس (WordPress)
1. `Prepare WP Post`: تبدیل Markdown به HTML ساده.
2. در صورت وجود تصویر: دانلود + آپلود در `wp/v2/media` و ست‌کردن `featured_media`.
3. ساخت پست با `POST {WORDPRESS_SITE_URL}/wp-json/wp/v2/posts` (Credential نوع WordPress API در n8n).
4. Callback همیشه اجرا می‌شود:
   - موفق → `POST /api/v1/automation/products/publish-callback` با `status:"published"` و `external_url`.
   - ناموفق → همان Endpoint با `status:"failed"` و `error_code:"wp_publish_failed"`.

Callback باعث fan-out اعلان‌ها می‌شود (ایجاد Notification + Deliveryها + رویداد `product.notify.fanout.requested`).

### ۳. فانت‌اوت کانال‌ها (Notify)
`Split Targets` آیتم‌های `targets[]` را جدا و `Channel Router` بر اساس `channel` مسیر می‌کند:

| channel | Adapter | Secret env (n8n) | target_ref |
|---|---|---|---|
| `telegram` | `https://api.telegram.org/bot<token>/sendMessage` | `TELEGRAM_BOT_TOKEN` | chat id / @channel |
| `ble` (بله) | `https://tapi.bale.ai/bot<token>/sendMessage` | `BALE_BOT_TOKEN` | chat id / @channel |
| `rubika` | `https://bot.rubika.ir:555/v2/sendText` | `RUBIKA_BOT_TOKEN` | chat_id روبیکا |
| `whatsapp` | Meta Cloud API `graph.facebook.com/v20.0/<phone_id>/messages` | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | شماره E.164 |

هر ارسال (موفق یا خطا) یک Ack به این Endpoint می‌فرستد:

```http
POST /api/v1/automation/products/notify-ack
x-automation-secret: <N8N_WEBHOOK_SECRET>
{ "notification_id": "...", "idempotency_key": "...", "channel": "telegram",
  "target_ref": "...", "delivered": true, "external_message_id": "123" }
```

وضعیت هر Delivery و جمع کل Notification (`pending/sent/failed`) در PostgreSQL نگهداری می‌شود.
کانال ناشناخته با `error_code:"channel_not_supported"` Ack می‌شود تا هیچ Delivery معلق نماند.

## راه‌اندازی (راه جدید)

1. **بک‌اند** —环境变量 (env) مربوطه:
   ```env
   WOOCOMMERCE_WEBHOOK_SECRET=<حداقل ۱۶ کاراکتر تصادفی>
   N8N_WEBHOOK_SECRET=<حداقل ۳۲ کاراکتر تصادفی>
   N8N_PRODUCT_WEBHOOK_URL=http://n8n:5678/webhook/risheh-product
   PRODUCT_AUTO_PUBLISH=true        # یا false برای Review انسانی
   AI_API_BASE_URL / AI_API_KEY / AI_MODEL
   ```
2. **اعلان کانال‌ها** — برای هر Workspace:
   ```http
   POST /api/v1/workspaces/{ws}/notification-channels
   { "channel": "telegram", "target_ref": "-1001234567890", "display_name": "کانال فروش" }
   ```
   (به همین ترتیب `ble`, `rubika`, `whatsapp`)
3. **n8n**:
   - Import `workflows/product_pipeline_v1.json` و Active کردن Workflow.
   - Credentials: یک «WordPress API» Credential با username + Application Password.
   - Environment Variables n8n: `RISHEH_API_BASE_URL` (مثلاً `http://api:8000`)،
     `RISHEH_AUTOMATION_SECRET` (= `N8N_WEBHOOK_SECRET`)، توکن‌های چهار کانال بالا،
     `WORDPRESS_SITE_URL` و اختیاری: `RISHEH_WP_POST_STATUS` (`publish|draft`)،
     `WHATSAPP_GRAPH_BASE_URL`.
4. **WooCommerce** (در پیشخوان وردپرس):
   Settings → Advanced → Webhooks → Add webhook:
   - Topic: `Product created`
   - Delivery URL: `https://<api-host>/api/v1/integrations/woocommerce/webhooks/<workspace_id>`
   - Secret: همان `WOOCOMMERCE_WEBHOOK_SECRET`
   - Status: Active (API version: V3)

احراز هویت Webhook دو لایه است: هدر `X-Webhook-Secret` یا امضای استاندارد
`X-WC-Webhook-Signature` (HMAC-SHA256 که WooCommerce با Secret روی بدنه می‌زند).

## Idempotency و Retry

- Webhook تکراری WooCommerce → همان Snapshot (hash محتوایی) و Job موجود؛ رویداد جدید نمی‌سازد.
- هر Event Outbox با `FOR UPDATE SKIP LOCKED` حداکثر یک‌بار claim می‌شود؛ backoff نما‌یی و DLQ دارد.
- Callbackها با `idempotency_key` کار می‌کنند؛ تکرار Ack وضعیت را خراب نمی‌کند.
- اگر n8n Down باشد، رویدادها در جدول outbox می‌مانند و بعداً deliver می‌شوند.

## عیب‌یابی

| نشانه | علت احتمالی | کار |
|---|---|---|
| Job در `queued` مانده | Outbox Worker خاموش / webhook آدرس اشتباه | `docker compose logs outbox-worker`، بررسی `N8N_PRODUCT_WEBHOOK_URL` |
| Job `failed` با `ai_unavailable` | تنظیمات AI | `AI_API_*` را بررسی کنید؛ سپس generate-draft را دستی retry کنید |
| Job `needs_attention` | رد شدن Draft بعد از سقف بازنگری | نسخه‌ها و `validation_report` را ببینید، با instruction اصلاحی دوباره generate کنید |
| Publication `failed` با `wp_publish_failed` | Credential/URL وردپرس | Application Password و `WORDPRESS_SITE_URL` را چک کنید |
| Deliveryها `pending` | Ack نرسیده | لاگ n8n execution؛ مغایرت `notification_id/idempotency_key` |
| `401 Invalid automation secret` | عدم تطابق secret | `RISHEH_AUTOMATION_SECRET` == `N8N_WEBHOOK_SECRET` |

## تست End-to-End بدون WooCommerce

```bash
curl -X POST "$API/api/v1/integrations/woocommerce/webhooks/$WORKSPACE_ID" \
  -H "Content-Type: application/json" -H "X-Webhook-Secret: $WOOCOMMERCE_WEBHOOK_SECRET" \
  -d '{"id": 999, "name": "عسل کوهی", "description": "<p>عسل طبیعی...</p>", "price": "450000",
       "permalink": "https://shop.example/product/asal", "images":[{"src":"https://cdn.example/asal.jpg"}]}'
```

سپس وضعیت Job را ببینید:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/workspaces/$WORKSPACE_ID/products/jobs?status_filter=completed"
```
