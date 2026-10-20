#!/usr/bin/env python3
"""Generate product_pipeline_v1.json — n8n workflow for the WooCommerce→Blog→Multichannel pipeline.

Single entry webhook (`risheh-product`) receives all `product.*` outbox events from the
Engage Outbox Worker and routes them to three branches:

  1. product.content.generate.requested   -> calls API generate-draft (+ optional publish)
  2. product.wordpress.publish.requested  -> creates WordPress post via WP REST API,
                                             uploads featured image, calls publish-callback
  3. product.notify.fanout.requested      -> fans out to Telegram / Bale / Rubika / WhatsApp,
                                             calls notify-ack per delivery

Regenerate with:  python3 gen_product_pipeline.py
"""
from __future__ import annotations

import json
import uuid

def nid() -> str:
    return str(uuid.uuid4())

API = "={{ $env.RISHEH_API_BASE_URL }}"

nodes: list[dict] = []
connections: dict = {}


def add(node: dict) -> str:
    nodes.append(node)
    return node["name"]


def link(src: str, dst: str, src_out: int = 0, dst_in: int = 0) -> None:
    conn = connections.setdefault(src, {"main": [[]]})
    while len(conn["main"]) <= src_out:
        conn["main"].append([])
    conn["main"][src_out].append({"node": dst, "type": "main", "index": dst_in})


# --------------------------------------------------------------------- #
# Entry: webhook + validation + router
# --------------------------------------------------------------------- #
add({
    "parameters": {
        "httpMethod": "POST",
        "path": "risheh-product",
        "responseMode": "responseNode",
        "options": {},
    },
    "id": nid(), "name": "Product Webhook", "type": "n8n-nodes-base.webhook",
    "typeVersion": 2, "position": [0, 300],
    "webhookId": "risheh-product",
})

VALIDATE_CODE = """// Single entry webhook for all product.* outbox events (see app/outbox_worker.py).
const secret = $json.headers?.['x-automation-secret'];
if (!secret || secret !== $env.RISHEH_AUTOMATION_SECRET) throw new Error('invalid automation secret');
const e = $json.body ?? $json;
const allowed = ['product.content.generate.requested', 'product.wordpress.publish.requested', 'product.notify.fanout.requested'];
if (!allowed.includes(e.event_type)) throw new Error('unsupported event type: ' + e.event_type);
if (!e.payload) throw new Error('missing payload');
return [{ json: {
  event_type: e.event_type,
  event_id: e.event_id,
  workspace_id: e.workspace_id ?? e.payload.workspace_id,
  correlation_id: e.correlation_id ?? null,
  payload: e.payload
} }];"""
add({
    "parameters": {"jsCode": VALIDATE_CODE},
    "id": nid(), "name": "Validate Event", "type": "n8n-nodes-base.code",
    "typeVersion": 2, "position": [220, 300],
})
link("Product Webhook", "Validate Event")

def rule(value: str, key: str) -> dict:
    return {
        "conditions": {"combinator": "and", "conditions": [
            {"leftValue": "={{ $json.event_type }}", "rightValue": value,
             "operator": {"type": "string", "operation": "equals"}}]},
        "renameOutput": True, "outputKey": key,
    }

ROUTER = "Route Event"
add({
    "parameters": {
        "rules": {"values": [
            rule("product.content.generate.requested", "generate"),
            rule("product.wordpress.publish.requested", "wordpress"),
            rule("product.notify.fanout.requested", "notify"),
        ]},
        "options": {"fallbackOutput": "extra"},
    },
    "id": nid(), "name": ROUTER, "type": "n8n-nodes-base.switch",
    "typeVersion": 3, "position": [440, 300],
})
link("Validate Event", ROUTER)

add({
    "parameters": {"jsCode": "throw new Error('unrouted event type: ' + ($json.event_type ?? 'unknown'));"},
    "id": nid(), "name": "Unknown Event", "type": "n8n-nodes-base.code",
    "typeVersion": 2, "position": [680, 780],
})
link(ROUTER, "Unknown Event", 3)

# --------------------------------------------------------------------- #
# Branch A: draft generation (delegates AI work to the API service)
# --------------------------------------------------------------------- #
GEN_HTTP = "Generate Draft (API)"
add({
    "parameters": {
        "method": "POST",
        "url": f"{API}/api/v1/workspaces/{{{{ $json.workspace_id }}}}/products/jobs/{{{{ $json.payload.job_id }}}}/generate-draft",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "x-automation-secret", "value": "={{ $env.RISHEH_AUTOMATION_SECRET }}"},
            {"name": "Content-Type", "value": "application/json"},
        ]},
        "sendBody": True,
        "contentType": "raw",
        "rawContentType": "application/json",
        "body": "={{ JSON.stringify({ instruction: $json.payload.instruction ?? 'این محصول را به‌صورت یک پست وبلاگ فارسی، صادقانه و فقط بر اساس داده ارائه‌شده معرفی کن.' }) }}",
        "options": {"timeout": 180000},
    },
    "id": nid(), "name": GEN_HTTP, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [680, 40],
    "retryOnFail": True, "maxTries": 2, "waitBetweenTries": 5000,
})

GEN_CHECK = "Check Generate Result"
add({
    "parameters": {"jsCode": """const body = $json.body ?? $json;
if (body.detail && !body.job) {
  // 4xx/5xx handled by API state machine already (job -> failed/needs_attention). Log & accept.
  return [{ json: { job_status: null, error: JSON.stringify(body.detail).slice(0, 500) } }];
}
return [{ json: { job_status: body.job?.status ?? null, version_id: body.version?.id ?? null, auto_publish: body.auto_publish ?? false } }];"""},
    "id": nid(), "name": GEN_CHECK, "type": "n8n-nodes-base.code",
    "typeVersion": 2, "position": [900, 40],
})

OK_GEN = "Accepted (Generate)"
add({
    "parameters": {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify({ ok: true, stage: 'generate', job_status: $json.job_status, error: $json.error ?? null }) }}",
        "options": {"responseCode": 200},
    },
    "id": nid(), "name": OK_GEN, "type": "n8n-nodes-base.respondToWebhook",
    "typeVersion": 1.1, "position": [1120, 40],
})

link(ROUTER, GEN_HTTP, 0)
link(GEN_HTTP, GEN_CHECK)
link(GEN_CHECK, OK_GEN)

# --------------------------------------------------------------------- #
# Branch B: WordPress publication
# --------------------------------------------------------------------- #
WP_PREP = "Prepare WP Post"
add({
    "parameters": {"jsCode": r"""const p = $json.payload;
// Minimal Markdown -> HTML conversion (headings, bold, italic, lists, links).
let md = String(p.body_markdown ?? '');
md = md.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
md = md.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>')
       .replace(/^##\s+(.+)$/gm, '<h2>$1</h2>')
       .replace(/^#\s+(.+)$/gm, '<h1>$1</h1>')
       .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
       .replace(/\*(.+?)\*/g, '<em>$1</em>')
       .replace(/^-\s+(.+)$/gm, '<li>$1</li>')
       .replace(/((?:<li>.*<\/li>\s*)+)/g, m => '<ul>' + m.trim() + '</ul>')
       .replace(/\[(.*?)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2">$1</a>');
md = md.split(/\n{2,}/).map(b => /^<(h\d|ul|ol|p|blockquote)/.test(b.trim()) ? b : ('<p>' + b.replace(/\n/g,'<br />') + '</p>')).join('\n');
return [{ json: {
  publication_id: p.publication_id,
  content_id: p.content_id,
  job_id: p.job_id,
  workspace_id: p.workspace_id,
  idempotency_key: p.idempotency_key,
  title: p.title ?? 'محصول جدید',
  body_html: md,
  excerpt: p.excerpt ?? '',
  status: $env.RISHEH_WP_POST_STATUS ?? 'publish',
  image: p.image ?? ''
} }];"""},
    "id": nid(), "name": WP_PREP, "type": "n8n-nodes-base.code",
    "typeVersion": 2, "position": [680, 220],
})

WP_IF_IMG = "Has Featured Image?"
add({
    "parameters": {
        "conditions": {"combinator": "and", "conditions": [
            {"leftValue": "={{ $json.image }}", "rightValue": "",
             "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}]},
        "options": {},
    },
    "id": nid(), "name": WP_IF_IMG, "type": "n8n-nodes-base.if",
    "typeVersion": 2, "position": [900, 220],
})

WP_DL = "Download Image"
add({
    "parameters": {
        "url": "={{ $json.image }}",
        "options": {"timeout": 30000, "response": {"response": {"responseFormat": "file"}}},
    },
    "id": nid(), "name": WP_DL, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [1120, 140], "onError": "continueRegularOutput",
})

WP_UPLOAD = "WP Media New"
add({
    "parameters": {
        "method": "POST",
        "url": "={{ $env.WORDPRESS_SITE_URL }}/wp-json/wp/v2/media",
        "authentication": "predefinedCredentialType",
        "nodeCredentialType": "wordPressApi",
        "sendBody": True,
        "contentType": "binaryData",
        "inputDataType": "binary",
        "binaryPropertyName": "data",
        "options": {"timeout": 60000},
    },
    "id": nid(), "name": WP_UPLOAD, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [1340, 140], "onError": "continueRegularOutput",
})

WP_MERGE = "Merge Post Fields"
add({
    "parameters": {"jsCode": """const base = $('Prepare WP Post').first().json;
const out = { ...base };
delete out.image;
try {
  const mediaItems = $items('WP Media New');
  const m = mediaItems && mediaItems.length ? mediaItems[mediaItems.length - 1].json : null;
  if (m && m.id) out.featured_media = m.id;
} catch (e) { /* image upload skipped */ }
return [{ json: out }];"""},
    "id": nid(), "name": WP_MERGE, "type": "n8n-nodes-base.code",
    "typeVersion": 2, "position": [1560, 220],
})

WP_CREATE = "WP Create Post"
add({
    "parameters": {
        "method": "POST",
        "url": "={{ $env.WORDPRESS_SITE_URL }}/wp-json/wp/v2/posts",
        "authentication": "predefinedCredentialType",
        "nodeCredentialType": "wordPressApi",
        "sendQuery": True,
        "queryParameters": {"parameters": [{"name": "context", "value": "view"}]},
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ title: $json.title, content: $json.body_html, excerpt: $json.excerpt, status: $json.status, featured_media: $json.featured_media }) }}",
        "options": {"timeout": 60000},
    },
    "id": nid(), "name": WP_CREATE, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [1780, 220], "onError": "continueErrorOutput",
})

WP_CB_OK = "Publish Callback (OK)"
add({
    "parameters": {
        "method": "POST",
        "url": f"{API}/api/v1/automation/products/publish-callback",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "x-automation-secret", "value": "={{ $env.RISHEH_AUTOMATION_SECRET }}"},
            {"name": "Content-Type", "value": "application/json"},
        ]},
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ publication_id: $('Prepare WP Post').first().json.publication_id, idempotency_key: $('Prepare WP Post').first().json.idempotency_key, status: 'published', external_id: String($json.id ?? ''), external_url: $json.link ?? '' }) }}",
        "options": {"timeout": 30000},
    },
    "id": nid(), "name": WP_CB_OK, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [2000, 160],
})

RESP_WP = "Accepted (WordPress)"
add({
    "parameters": {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify({ ok: true, stage: 'wordpress', post_id: $json.external_id }) }}",
        "options": {"responseCode": 200},
    },
    "id": nid(), "name": RESP_WP, "type": "n8n-nodes-base.respondToWebhook",
    "typeVersion": 1.1, "position": [2220, 160],
})

WP_CB_FAIL = "Publish Callback (Failed)"
add({
    "parameters": {
        "method": "POST",
        "url": f"{API}/api/v1/automation/products/publish-callback",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "x-automation-secret", "value": "={{ $env.RISHEH_AUTOMATION_SECRET }}"},
            {"name": "Content-Type", "value": "application/json"},
        ]},
        "sendBody": True,
        "contentType": "json",
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ publication_id: $('Prepare WP Post').first().json.publication_id, idempotency_key: $('Prepare WP Post').first().json.idempotency_key, status: 'failed', error_code: 'wp_publish_failed', error_message: String($json.message ?? $json.error ?? 'wordpress publish failed').slice(0, 1000) }) }}",
        "options": {"timeout": 30000},
    },
    "id": nid(), "name": WP_CB_FAIL, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [2000, 320],
})

RESP_WP_FAIL = "Accepted (WP Failed)"
add({
    "parameters": {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify({ ok: false, stage: 'wordpress', reported: true }) }}",
        "options": {"responseCode": 200},
    },
    "id": nid(), "name": RESP_WP_FAIL, "type": "n8n-nodes-base.respondToWebhook",
    "typeVersion": 1.1, "position": [2220, 320],
})

link(ROUTER, WP_PREP, 1)
link(WP_PREP, WP_IF_IMG)
link(WP_IF_IMG, WP_DL, 0)
link(WP_DL, WP_UPLOAD)
link(WP_UPLOAD, WP_MERGE)
link(WP_IF_IMG, WP_MERGE, 1)
link(WP_MERGE, WP_CREATE)
link(WP_CREATE, WP_CB_OK, 0)
link(WP_CREATE, WP_CB_FAIL, 1)
link(WP_CB_OK, RESP_WP)
link(WP_CB_FAIL, RESP_WP_FAIL)

# --------------------------------------------------------------------- #
# Branch C: multichannel fan-out
# --------------------------------------------------------------------- #
NF_SPLIT = "Split Targets"
add({
    "parameters": {"jsCode": """const p = $json.payload;
if (!Array.isArray(p.targets) || p.targets.length === 0) {
  return [{ json: { notification_id: p.notification_id, idempotency_key: p.idempotency_key, channel: null, empty: true } }];
}
return p.targets.map(t => ({ json: {
  notification_id: p.notification_id,
  idempotency_key: p.idempotency_key,
  channel: t.channel,
  target_ref: t.target_ref,
  template_key: t.template_key ?? null,
  message: p.message,
  links: p.links ?? {},
  empty: false
}}));"""},
    "id": nid(), "name": NF_SPLIT, "type": "n8n-nodes-base.code",
    "typeVersion": 2, "position": [680, 520],
})

NF_ROUTER = "Channel Router"
def ch_rule(value: str) -> dict:
    return {
        "conditions": {"combinator": "and", "conditions": [
            {"leftValue": "={{ $json.channel }}", "rightValue": value,
             "operator": {"type": "string", "operation": "equals"}}]},
        "renameOutput": True, "outputKey": value,
    }

add({
    "parameters": {
        "rules": {"values": [ch_rule("telegram"), ch_rule("ble"), ch_rule("rubika"), ch_rule("whatsapp")]},
        "options": {"fallbackOutput": "extra"},
    },
    "id": nid(), "name": NF_ROUTER, "type": "n8n-nodes-base.switch",
    "typeVersion": 3, "position": [900, 520],
})

UNSUPPORTED = "Unsupported Channel Ack"
add({
    "parameters": {"jsCode": """const base = $('Split Targets').first().json;
return [{ json: { __ack: {
  notification_id: base.notification_id,
  idempotency_key: base.idempotency_key,
  channel: String($json.channel ?? 'unknown').slice(0, 40),
  target_ref: String($json.target_ref ?? 'n/a').slice(0, 255),
  delivered: false,
  error_code: 'channel_not_supported',
  error_message: 'no adapter configured for this channel in the workflow'
} } }];"""},
    "id": nid(), "name": UNSUPPORTED, "type": "n8n-nodes-base.code",
    "typeVersion": 2, "position": [1140, 900],
})

BOT_TOKEN_CHANNELS = {
    "telegram": ("Telegram Send", "https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage",
                 "src.result?.message_id", [1140, 340]),
    "ble": ("Bale Send", "https://tapi.bale.ai/bot{{ $env.BALE_BOT_TOKEN }}/sendMessage",
            "src.result?.message_id", [1140, 480]),
}

for channel, (send_name, url, msgid_expr, pos) in BOT_TOKEN_CHANNELS.items():
    send = add_node = {
        "parameters": {
            "method": "POST",
            "url": "=" + url,
            "sendBody": True, "contentType": "json", "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ chat_id: $json.target_ref, text: $json.message, parse_mode: 'HTML', disable_web_page_preview: false }) }}",
            "options": {"timeout": 30000},
        },
        "id": nid(), "name": send_name, "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2, "position": pos, "onError": "continueErrorOutput",
    }
    add(send)

def make_ack(name: str, channel: str, msgid_expr: str, pos: list[int]) -> str:
    code = f"""const src = $input.first().json ?? {{}};
const base = $('Split Targets').first().json;
let extId = null;
try {{ extId = {msgid_expr} ?? null; }} catch (e) {{}}
const ok = Boolean(extId);
return [{{ json: {{ __ack: {{
  notification_id: base.notification_id,
  idempotency_key: base.idempotency_key,
  channel: '{channel}',
  target_ref: base.target_ref,
  delivered: ok,
  external_message_id: ok ? String(extId) : null,
  error_code: ok ? null : 'delivery_failed',
  error_message: ok ? null : String(src.message ?? src.lastErr ?? src.error ?? 'provider did not confirm delivery').slice(0, 1000)
}} }} }}]];"""
    return add({
        "parameters": {"jsCode": code},
        "id": nid(), "name": name, "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": pos,
    })

RB_SEND = "Rubika Send"
add({
    "parameters": {
        "method": "POST",
        "url": "=https://bot.rubika.ir:555/v2/sendText",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "Authorization", "value": "=Bearer {{ $env.RUBIKA_BOT_TOKEN }}"},
            {"name": "Content-Type", "value": "application/json"},
        ]},
        "sendBody": True, "contentType": "json", "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ chat_id: $json.target_ref, text: $json.message }) }}",
        "options": {"timeout": 30000},
    },
    "id": nid(), "name": RB_SEND, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [1140, 620], "onError": "continueErrorOutput",
})

WA_SEND = "WhatsApp Send (Cloud API)"
add({
    "parameters": {
        "method": "POST",
        "url": "={{ $env.WHATSAPP_GRAPH_BASE_URL || 'https://graph.facebook.com/v20.0' }}/{{ $env.WHATSAPP_PHONE_NUMBER_ID }}/messages",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "Authorization", "value": "=Bearer {{ $env.WHATSAPP_ACCESS_TOKEN }}"},
            {"name": "Content-Type", "value": "application/json"},
        ]},
        "sendBody": True, "contentType": "json", "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ messaging_product: 'whatsapp', to: $json.target_ref, type: 'text', text: { preview_url: true, body: $json.message } }) }}",
        "options": {"timeout": 30000},
    },
    "id": nid(), "name": WA_SEND, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [1140, 760], "onError": "continueErrorOutput",
})

TG_ACK = make_ack("Telegram Ack", "telegram", "src.result?.message_id", [1380, 300])
TG_ERR = make_ack("Telegram ErrAck", "telegram", "null", [1380, 400])
BLE_ACK = make_ack("Bale Ack", "ble", "src.result?.message_id", [1380, 440])
BLE_ERR = make_ack("Bale ErrAck", "ble", "null", [1380, 540])
RB_ACK = make_ack("Rubika Ack", "rubika", "src.message_id ?? src.id", [1380, 580])
RB_ERR = make_ack("Rubika ErrAck", "rubika", "null", [1380, 680])
WA_ACK = make_ack("WhatsApp Ack", "whatsapp", "src.messages?.[0]?.id", [1380, 720])
WA_ERR = make_ack("WhatsApp ErrAck", "whatsapp", "null", [1380, 820])

ACK_HTTP = "Notify Ack (API)"
add({
    "parameters": {
        "method": "POST",
        "url": f"{API}/api/v1/automation/products/notify-ack",
        "sendHeaders": True,
        "headerParameters": {"parameters": [
            {"name": "x-automation-secret", "value": "={{ $env.RISHEH_AUTOMATION_SECRET }}"},
            {"name": "Content-Type", "value": "application/json"},
        ]},
        "sendBody": True, "contentType": "json", "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify($json.__ack) }}",
        "options": {"timeout": 30000},
    },
    "id": nid(), "name": ACK_HTTP, "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2, "position": [1640, 520],
    "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 3000,
})

RESP_NF = "Accepted (Notify)"
add({
    "parameters": {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify({ ok: true, stage: 'notify', acked: true }) }}",
        "options": {"responseCode": 200},
    },
    "id": nid(), "name": RESP_NF, "type": "n8n-nodes-base.respondToWebhook",
    "typeVersion": 1.1, "position": [1860, 520],
})

link(ROUTER, NF_SPLIT, 2)
link(NF_SPLIT, NF_ROUTER)
link(NF_ROUTER, "Telegram Send", 0)
link(NF_ROUTER, "Bale Send", 1)
link(NF_ROUTER, RB_SEND, 2)
link(NF_ROUTER, WA_SEND, 3)
link(NF_ROUTER, UNSUPPORTED, 4)
link("Telegram Send", TG_ACK, 0); link("Telegram Send", TG_ERR, 1)
link("Bale Send", BLE_ACK, 0); link("Bale Send", BLE_ERR, 1)
link(RB_SEND, RB_ACK, 0); link(RB_SEND, RB_ERR, 1)
link(WA_SEND, WA_ACK, 0); link(WA_SEND, WA_ERR, 1)
for n in (TG_ACK, TG_ERR, BLE_ACK, BLE_ERR, RB_ACK, RB_ERR, WA_ACK, WA_ERR, UNSUPPORTED):
    link(n, ACK_HTTP)
link(ACK_HTTP, RESP_NF)

workflow = {
    "name": "ENGAGE__PRODUCT__PIPELINE__V1",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
    "pinData": {},
}

out = "/workspace/automation/workflows/product_pipeline_v1.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(workflow, f, ensure_ascii=False, indent=2)

# ---- self-validation ----
names = {n["name"] for n in nodes}
for src, conn in connections.items():
    assert src in names, f"connection source missing: {src}"
    for outs in conn["main"]:
        for c in outs:
            assert c["node"] in names, f"connection target missing: {c['node']}"
targeted = {c["node"] for conn in connections.values() for outs in conn["main"] for c in outs}
orphans = names - targeted - {"Product Webhook"}
assert not orphans, f"orphan nodes: {orphans}"
print(f"OK: wrote {out} with {len(nodes)} nodes, {sum(len(o) for c in connections.values() for o in c['main'])} links")
