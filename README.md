# 📱 Jarvis Telegram Bot

> ⚠️ **LOCKED SERVICE** - This service is stable and production-ready. DO NOT modify without explicit user approval.

> **Human interface for Jarvis.** Send voice notes and receive AI responses - all via Telegram.

## 🎯 Role in the Ecosystem

This bot is the **mobile entry point** to Jarvis. It does ONE thing: **connect humans to the AI system**.

```
┌──────────────┐      ┌────────────────┐      ┌─────────────────────┐
│  Human       │ ───► │  Telegram Bot  │ ───► │  Audio Pipeline     │
│  (Voice/Text)│      │  (Interface)   │      │  (Transcription)    │
└──────────────┘      └────────────────┘      └─────────┬───────────┘
                                                        │
                                                        ▼
                                              ┌─────────────────────┐
                                              │  Intelligence       │
                                              │  Service (THE BRAIN)│
                                              └─────────────────────┘
```

**Why no AI here?** All intelligence lives in `jarvis-intelligence-service`. This bot is just a pipe.

---

## 🏗️ Architecture

### Two Entry Points

| File | Mode | Use Case |
|------|------|----------|
| `main.py` | Polling | Local development |
| `main_webhook.py` | Webhook (FastAPI) | Production (Cloud Run) |

### Data Flow

```
1. Voice Message received
         │
         ▼
2. Download audio bytes
         │
         ▼
3. POST to Audio Pipeline (/process/upload)
         │       │
         │       └── Returns: transcript + analysis + created records
         │
         ▼
4. Display summary to user
         │
         ▼
5. (Optional) Contact linking prompts
```

**Fallback**: If Audio Pipeline is unreachable, upload to Google Drive for async processing.

---

## 🔌 Endpoints

### `GET /health`
Health check for Cloud Run.
```json
{"status": "healthy"}
```

### `POST /webhook`
Telegram webhook endpoint. Receives all updates from Telegram.

### `POST /send_message`
**Internal API** for other services to send notifications.
```bash
curl -X POST https://jarvis-telegram-bot-xxx.run.app/send_message \
  -H "Content-Type: application/json" \
  -d '{"chat_id": 123456789, "text": "Hello!", "parse_mode": "Markdown"}'
```

---

## 📨 Message Handlers

| Handler | Trigger | Action |
|---------|---------|--------|
| `/start` | Command | Welcome message |
| `/help` | Command | Usage instructions |
| `/cancel` | Command | Cancel pending contact creation |
| Voice | Voice message | Process via Audio Pipeline |
| Audio | Audio file | Process via Audio Pipeline |
| Text | Any text | Contact linking OR help prompt |

---

## 👤 Contact Linking Flow

When a voice memo mentions someone not in the CRM:

```
1. Audio Pipeline returns: contact_matches: [{searched_name: "John", matched: false, suggestions: [...]}]
         │
         ▼
2. Bot prompts user:
   ❓ Unknown contact: *John*
   Reply with:
     1 = John Smith (Acme Corp)
     2 = John Doe
     0 = Skip
     Or type the correct full name
         │
         ▼
3. User replies: "1" or "John Smith" or "0"
         │
         ▼
4. Bot calls Intelligence Service:
   PATCH /api/v1/meetings/{id}/link-contact
         │
         ▼
5. Confirmation: ✅ Linked to: John Smith (Acme Corp)
```

---

## 🔐 Security

### Authorization
Only users in `ALLOWED_USER_IDS` can use the bot.

```python
ALLOWED_USER_IDS = [int(id.strip()) for id in os.getenv('ALLOWED_USER_IDS', '').split(',')]

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # Open access if not configured
    return user_id in ALLOWED_USER_IDS
```

### Duplicate Prevention
Voice messages are tracked by `file_unique_id` to prevent double processing.

```python
recently_processed_files = {}  # {file_unique_id: timestamp}

def _is_duplicate_file(file_unique_id: str) -> bool:
    # Returns True if file was processed in last 5 minutes
```

---

## ⚙️ Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot API token from @BotFather |
| `WEBHOOK_URL` | Yes* | Public URL of this service (webhook mode) |
| `GOOGLE_DRIVE_FOLDER_ID` | Yes | Fallback upload folder |
| `GOOGLE_TOKEN_JSON` | Yes | OAuth credentials for Drive |
| `AUDIO_PIPELINE_URL` | Yes | Audio Pipeline service URL |
| `INTELLIGENCE_SERVICE_URL` | Yes | Intelligence Service URL |
| `ALLOWED_USER_IDS` | No | Comma-separated list of authorized Telegram user IDs |

*Required for webhook mode (production)

---

## 🚀 Deployment

### Automatic (Production)
Push to `main` → Cloud Build → Cloud Run

### Manual (Development)
```bash
# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env

# Run in polling mode (local)
python main.py

# Run in webhook mode (with ngrok)
python main_webhook.py
# In another terminal: ngrok http 8080
# Set WEBHOOK_URL to ngrok URL
```

---

## 🔧 Error Handling

### Audio Pipeline Unreachable
```python
try:
    response = await client.post(f"{AUDIO_PIPELINE_URL}/process/upload", ...)
except Exception as e:
    # Fall back to Google Drive upload
    await upload_to_drive(file_bytes, filename)
```

### Markdown Parsing Errors
```python
try:
    await bot.send_message(chat_id, text, parse_mode='Markdown')
except Exception as e:
    if "parse entities" in str(e).lower():
        # Retry without formatting
        await bot.send_message(chat_id, text, parse_mode=None)
```

---

## 📁 Project Structure

```
jarvis-telegram-bot/
├── main.py              # Polling mode (development)
├── main_webhook.py      # Webhook mode (production) - THE MAIN FILE
├── cloudbuild.yaml      # Cloud Build config
├── Dockerfile
├── requirements.txt
└── data/
    └── token.json       # Google OAuth token (local only)
```

---

## 🔗 Related Services

| Service | Purpose | Communication |
|---------|---------|---------------|
| **jarvis-audio-pipeline** | Transcription + handoff | POST /process/upload |
| **jarvis-intelligence-service** | Contact linking, AI responses | PATCH /api/v1/meetings/{id}/link-contact |
| **jarvis-sync-service** | Background sync (no direct communication) | N/A |

---

## ⚠️ Important Notes

### DO NOT
- ❌ Add AI/LLM logic here (goes in Intelligence Service)
- ❌ Process transcripts here (goes in Audio Pipeline)
- ❌ Access Supabase directly (use Intelligence Service APIs)
- ❌ Manually deploy (push to main for Cloud Build)

### DO
- ✅ Handle Telegram-specific UI (keyboards, prompts)
- ✅ Route voice/audio to Audio Pipeline
- ✅ Handle contact linking flow
- ✅ Send notifications from other services via `/send_message`
