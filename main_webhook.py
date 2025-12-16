"""
Jarvis Telegram Bot - Webhook Mode (for Cloud Run)
Receives voice messages and uploads them to Google Drive for processing.
"""

import os
import io
import json
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from fastapi import FastAPI, Request, Response
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_DRIVE_FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID', '').strip()
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # e.g., https://your-bot.run.app
ALLOWED_USER_IDS = [int(id.strip()) for id in os.getenv('ALLOWED_USER_IDS', '').split(',') if id.strip()]

# Global bot application
bot_app = None

# Google Drive setup - use same scope as the token
SCOPES = ['https://www.googleapis.com/auth/drive']


def get_drive_service():
    """Get authenticated Google Drive service."""
    token_json = os.getenv('GOOGLE_TOKEN_JSON')
    if not token_json:
        raise ValueError("GOOGLE_TOKEN_JSON not set")
    
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    
    # Refresh if needed
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
    
    return build('drive', 'v3', credentials=creds)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hi {user.first_name}! 👋\n\n"
        "I'm Jarvis, your voice memo assistant.\n\n"
        "Send me a voice message and I'll process it for you:\n"
        "• Transcribe it\n"
        "• Extract key information\n"
        "• Save it to your knowledge base\n\n"
        "Just hold the microphone button and speak!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "🎙️ *How to use Jarvis:*\n\n"
        "1. Send a voice message (hold mic button)\n"
        "2. I'll upload it for processing\n"
        "3. Results will be in your Supabase database\n\n"
        "*Tips:*\n"
        "• Speak clearly\n"
        "• Start with context: 'Meeting with John...'\n"
        "• Mention names and dates clearly",
        parse_mode='Markdown'
    )


def is_authorized(user_id: int) -> bool:
    """Check if user is authorized to use the bot."""
    if not ALLOWED_USER_IDS:
        return True  # No restrictions if not configured
    return user_id in ALLOWED_USER_IDS


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming voice messages."""
    user = update.effective_user
    
    # Check authorization
    if not is_authorized(user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        logger.warning(f"Unauthorized access attempt by user {user.id} ({user.username})")
        return
    
    voice = update.message.voice
    logger.info(f"Received voice message from {user.username} ({user.id})")
    
    # Send processing status
    status_msg = await update.message.reply_text("⏳ Downloading voice message...")
    
    try:
        # Download voice file
        file = await context.bot.get_file(voice.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        
        # Generate filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"voice_{timestamp}_{user.username or user.id}.ogg"
        
        await status_msg.edit_text("☁️ Uploading to Google Drive...")
        
        # Upload to Google Drive
        drive_service = get_drive_service()
        
        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaIoBaseUpload(
            file_bytes,
            mimetype='audio/ogg',
            resumable=True
        )
        
        uploaded_file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,name'
        ).execute()
        
        logger.info(f"Uploaded to Drive: {uploaded_file['name']}")
        
        await status_msg.edit_text(
            f"✅ Audio file uploaded!\n\n"
            f"📁 File: `{filename}`\n\n"
            f"Processing will begin automatically via webhook.",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error processing voice message: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error: {str(e)}")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming audio files (similar to voice)."""
    user = update.effective_user
    
    if not is_authorized(user.id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    audio = update.message.audio
    logger.info(f"Received audio file from {user.username} ({user.id})")
    
    status_msg = await update.message.reply_text("⏳ Downloading audio file...")
    
    try:
        file = await context.bot.get_file(audio.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        ext = audio.mime_type.split('/')[-1] if audio.mime_type else 'mp3'
        filename = f"audio_{timestamp}_{user.username or user.id}.{ext}"
        
        await status_msg.edit_text("☁️ Uploading to Google Drive...")
        
        drive_service = get_drive_service()
        
        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaIoBaseUpload(
            file_bytes,
            mimetype=audio.mime_type or 'audio/mpeg',
            resumable=True
        )
        
        uploaded_file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,name'
        ).execute()
        
        logger.info(f"Uploaded to Drive: {uploaded_file['name']}")
        
        await status_msg.edit_text(
            f"✅ Audio file uploaded!\n\n"
            f"📁 File: `{filename}`\n\n"
            f"Processing will begin automatically via webhook.",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error processing audio file: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error: {str(e)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize bot application on startup."""
    global bot_app
    
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    
    if not GOOGLE_DRIVE_FOLDER_ID:
        raise ValueError("GOOGLE_DRIVE_FOLDER_ID not set")
    
    # Create bot application
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_command))
    bot_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    bot_app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    
    # Initialize bot
    await bot_app.initialize()
    await bot_app.start()
    
    # Set webhook
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await bot_app.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to: {webhook_url}")
    
    logger.info("Jarvis Telegram bot started (webhook mode)")
    
    yield
    
    # Cleanup
    await bot_app.stop()
    await bot_app.shutdown()


# FastAPI app for webhook
app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "Jarvis Telegram Bot is running", "mode": "webhook"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/webhook")
async def webhook(request: Request):
    """Handle incoming Telegram updates via webhook."""
    global bot_app
    
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return Response(status_code=200)
    
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return Response(status_code=500)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
