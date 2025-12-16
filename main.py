"""
Jarvis Telegram Bot
Receives voice messages and uploads them to Google Drive for processing.
"""

import os
import io
import logging
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
GOOGLE_DRIVE_FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID')
ALLOWED_USER_IDS = [int(id.strip()) for id in os.getenv('ALLOWED_USER_IDS', '').split(',') if id.strip()]

# Google Drive setup
SCOPES = ['https://www.googleapis.com/auth/drive.file']


def get_drive_service():
    """Get authenticated Google Drive service."""
    creds = None
    
    # Try environment variable first (for cloud deployment)
    token_json = os.getenv('GOOGLE_TOKEN_JSON')
    if token_json:
        import json
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    
    # Fallback to file
    token_file = Path('data/token.json')
    if not creds and token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    
    # Refresh if needed
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    
    if not creds:
        raise ValueError("No valid Google credentials found")
    
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
        "3. Check your Supabase/Notion for results\n\n"
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
        await update.message.reply_text("❌ Sorry, you're not authorized to use this bot.")
        logger.warning(f"Unauthorized access attempt from user {user.id} ({user.username})")
        return
    
    voice = update.message.voice
    logger.info(f"Received voice message from {user.username} ({user.id}): {voice.duration}s")
    
    # Send processing message
    status_msg = await update.message.reply_text("🎙️ Receiving voice message...")
    
    try:
        # Download voice file
        file = await context.bot.get_file(voice.file_id)
        voice_bytes = await file.download_as_bytearray()
        
        await status_msg.edit_text("📤 Uploading to Google Drive...")
        
        # Generate filename
        timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
        filename = f"voice_{timestamp}_{user.first_name}.ogg"
        
        # Upload to Google Drive
        drive_service = get_drive_service()
        
        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaIoBaseUpload(
            io.BytesIO(voice_bytes),
            mimetype='audio/ogg',
            resumable=True
        )
        
        uploaded_file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,name'
        ).execute()
        
        logger.info(f"Uploaded to Drive: {uploaded_file['name']} (ID: {uploaded_file['id']})")
        
        await status_msg.edit_text(
            f"✅ Voice message uploaded!\n\n"
            f"📁 File: `{filename}`\n"
            f"⏱️ Duration: {voice.duration}s\n\n"
            f"Processing will begin shortly. "
            f"Check your knowledge base in a few minutes.",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error processing voice message: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error uploading voice message: {str(e)}")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle audio files (mp3, m4a, etc.)."""
    user = update.effective_user
    
    if not is_authorized(user.id):
        await update.message.reply_text("❌ Sorry, you're not authorized to use this bot.")
        return
    
    audio = update.message.audio
    logger.info(f"Received audio file from {user.username}: {audio.file_name}")
    
    status_msg = await update.message.reply_text("🎵 Receiving audio file...")
    
    try:
        # Download audio file
        file = await context.bot.get_file(audio.file_id)
        audio_bytes = await file.download_as_bytearray()
        
        await status_msg.edit_text("📤 Uploading to Google Drive...")
        
        # Use original filename or generate one
        filename = audio.file_name or f"audio_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.mp3"
        
        # Upload to Google Drive
        drive_service = get_drive_service()
        
        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaIoBaseUpload(
            io.BytesIO(audio_bytes),
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
            f"Processing will begin shortly.",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error processing audio file: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error: {str(e)}")


def main() -> None:
    """Start the bot."""
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    
    if not GOOGLE_DRIVE_FOLDER_ID:
        raise ValueError("GOOGLE_DRIVE_FOLDER_ID not set")
    
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    
    # Start the bot
    logger.info("Starting Jarvis Telegram bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
