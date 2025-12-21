# 🤖 Telegram Bot - LLM Integration Guide

> **For AI Agents / Coding Assistants**: This document explains how the Telegram Bot interacts with other services.

## ⚠️ CRITICAL: This Service Has NO AI Logic

**DO NOT** add AI/LLM calls here. This bot:
1. Receives voice/audio from Telegram
2. Sends to Audio Pipeline for transcription
3. Displays results to user
4. Handles contact linking UI

All "thinking" happens in the Intelligence Service.

---

## 🔌 External Service Calls

### 1. Audio Pipeline - Voice Processing

**When**: User sends a voice message or audio file

```python
POST {AUDIO_PIPELINE_URL}/process/upload
Content-Type: multipart/form-data

files = {"file": (filename, audio_bytes, "audio/ogg")}
data = {"username": "bertan"}
```

**Response**:
```json
{
  "status": "success",
  "category": "meeting",
  "summary": "📅 Meeting: Sales call with John\n✅ 3 task(s) created",
  "details": {
    "transcript_id": "550e8400-...",
    "transcript_length": 1523,
    "meetings_created": 1,
    "reflections_created": 0,
    "tasks_created": 3,
    "meeting_ids": ["uuid"],
    "task_ids": ["uuid", "uuid", "uuid"],
    "contact_matches": [
      {
        "searched_name": "John",
        "matched": false,
        "suggestions": [
          {"id": "uuid", "name": "John Smith", "company": "Acme Corp"},
          {"id": "uuid", "name": "John Doe", "company": ""}
        ]
      }
    ]
  }
}
```

### 2. Intelligence Service - Contact Linking

**When**: User selects a contact from suggestions or types a name

```python
# Link to existing contact
PATCH {INTELLIGENCE_SERVICE_URL}/api/v1/meetings/{meeting_id}/link-contact
Content-Type: application/json

{"contact_id": "550e8400-..."}
```

**Response**:
```json
{
  "status": "linked",
  "contact_id": "550e8400-...",
  "contact_name": "John Smith",
  "company": "Acme Corp"
}
```

### 3. Intelligence Service - Search Contacts

**When**: User types a name that might match existing contacts

```python
GET {INTELLIGENCE_SERVICE_URL}/api/v1/contacts/search?q=John%20Smith&limit=5
```

**Response**:
```json
{
  "contacts": [
    {"id": "uuid", "name": "John Smith", "company": "Acme Corp"},
    {"id": "uuid", "name": "John Doe", "company": ""}
  ]
}
```

### 4. Intelligence Service - Create Contact

**When**: User confirms creating a new contact

```python
POST {INTELLIGENCE_SERVICE_URL}/api/v1/contacts
Content-Type: application/json

{
  "first_name": "John",
  "last_name": "Smith",
  "link_to_meeting_id": "550e8400-..."
}
```

---

## 📊 State Management

### Pending Contact Actions (In-Memory)

The bot tracks pending contact linking actions:

```python
# Global dictionaries (in-memory)
pending_contact_actions = {}   # For inline keyboard callbacks
pending_contact_creation = {}  # For text-based contact creation

# Structure:
pending_contact_creation[user_id] = {
    'meeting_id': 'uuid',
    'searched_name': 'John',
    'suggestions': [...],
    'mode': 'link_or_create',  # or 'correct', 'select_or_create'
    'expires': timestamp
}
```

**Timeout**: 10 minutes for text-based, 5 minutes for keyboard-based.

---

## 🔄 Contact Linking Flow

```
1. Audio Pipeline returns contact_matches with unmatched names
         │
         ▼
2. build_contact_text_prompt() creates prompt
         │
         ├── If suggestions exist:
         │   "Reply with 1, 2, 3... or type name"
         │
         └── If no suggestions:
             "Type the correct name or '0' to skip"
         │
         ▼
3. Store in pending_contact_creation[user_id]
         │
         ▼
4. User replies with text
         │
         ▼
5. handle_text_message() processes:
         │
         ├── '0' → Skip
         │
         ├── '1', '2', '3' → Link to suggestion[n-1]
         │
         └── 'John Smith' → Search for matches
                   │
                   ├── Found → Ask to select or create new
                   │
                   └── Not found → Create new contact
```

---

## 🔐 Authorization

```python
# Environment variable
ALLOWED_USER_IDS = [123456789, 987654321]

# Check function
def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # Open if not configured
    return user_id in ALLOWED_USER_IDS
```

**Find your Telegram User ID**: Message @userinfobot on Telegram.

---

## 📡 Sending Notifications

Other services can send messages to users via the `/send_message` endpoint:

```python
import httpx

async def notify_user(chat_id: int, message: str):
    """Send a notification to a user via the Telegram bot."""
    
    url = "https://jarvis-telegram-bot-xxx.run.app/send_message"
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        })
        return response.json()
```

**Note**: If Markdown parsing fails, the bot automatically retries without formatting.

---

## ⚠️ Common Errors

### Callback Data Too Long
Telegram limits callback_data to 64 bytes. We use short keys:

```python
def _short_key(prefix: str) -> str:
    """Generate short callback keys like 'L:1', 'C:2', 'S:3'"""
    global _callback_counter
    _callback_counter += 1
    return f"{prefix}:{_callback_counter}"

# Store actual data in memory
pending_contact_actions["L:1"] = {
    'meeting_id': 'uuid',
    'contact_id': 'uuid',
    ...
}
```

### Duplicate Voice Messages
Telegram sometimes sends the same message twice. We deduplicate:

```python
recently_processed_files = {}  # {file_unique_id: timestamp}

def _is_duplicate_file(file_unique_id: str) -> bool:
    # Clean up entries older than 5 minutes
    # Return True if already processed
```

---

## 🛠️ Debugging

### Check Bot Webhook
```bash
curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo
```

### Test Send Message
```bash
curl -X POST https://jarvis-telegram-bot-xxx.run.app/send_message \
  -H "Content-Type: application/json" \
  -d '{"chat_id": 123456789, "text": "Test message"}'
```

### View Cloud Run Logs
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=jarvis-telegram-bot" --limit=50
```

---

## 📁 Key Files

| File | Purpose |
|------|---------|
| `main_webhook.py` | **Production entry point** - FastAPI + webhook handlers |
| `main.py` | Development entry point - polling mode |
| `cloudbuild.yaml` | Cloud Build deployment config |

---

## 🚫 DO NOT MODIFY

1. **Authorization logic** - Security critical
2. **Deduplication logic** - Prevents double processing
3. **Service URLs** - Configured via environment

---

## ✅ Safe to Modify

- User-facing messages (prompts, help text)
- Contact linking UX flow
- Error message formatting
