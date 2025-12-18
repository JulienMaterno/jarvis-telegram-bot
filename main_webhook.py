"""
Jarvis Telegram Bot - Webhook Mode (for Cloud Run)
Receives voice messages and uploads them to Google Drive for processing.
"""

import os
import io
import json
import logging
import httpx
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel
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
AUDIO_PIPELINE_URL = os.getenv('AUDIO_PIPELINE_URL', '').strip()  # e.g., https://jarvis-audio-pipeline-xxx.run.app
INTELLIGENCE_SERVICE_URL = os.getenv('INTELLIGENCE_SERVICE_URL', '').strip()  # For contact operations
ALLOWED_USER_IDS = [int(id.strip()) for id in os.getenv('ALLOWED_USER_IDS', '').split(',') if id.strip()]

# Global bot application
bot_app = None

# Store pending contact actions (in-memory, good enough for single instance)
# Format: { "callback_data": {"meeting_id": ..., "searched_name": ..., ...} }
pending_contact_actions = {}

# Store users waiting to type a contact name
# Format: { user_id: {"meeting_id": ..., "suggested_name": ...} }
pending_contact_creation = {}

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
    status_msg = await update.message.reply_text("⏳ Processing voice message...")
    
    try:
        # Download voice file
        file = await context.bot.get_file(voice.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        
        # Generate filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"voice_{timestamp}_{user.username or user.id}.ogg"
        
        # Try direct upload to audio pipeline first (faster, no Google Drive)
        if AUDIO_PIPELINE_URL:
            try:
                await status_msg.edit_text("🔄 Transcribing and analyzing...")
                
                async with httpx.AsyncClient(timeout=300.0) as client:
                    # Send file directly to pipeline
                    files = {'file': (filename, file_bytes.getvalue(), 'audio/ogg')}
                    data = {'username': user.username or str(user.id)}
                    
                    response = await client.post(
                        f"{AUDIO_PIPELINE_URL}/process/upload",
                        files=files,
                        data=data
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        
                        if result.get("status") == "success":
                            summary = result.get("summary", "Processed successfully")
                            details = result.get("details", {})
                            
                            # Check if we need contact linking buttons
                            contact_matches = details.get("contact_matches", [])
                            meeting_ids = details.get("meeting_ids", [])
                            
                            keyboard = build_contact_keyboard(contact_matches, meeting_ids)
                            
                            if keyboard:
                                await status_msg.edit_text(
                                    f"✅ *Voice memo processed!*\n\n"
                                    f"{summary}\n\n"
                                    f"📝 Transcript: {details.get('transcript_length', 0)} chars",
                                    parse_mode='Markdown',
                                    reply_markup=keyboard
                                )
                            else:
                                await status_msg.edit_text(
                                    f"✅ *Voice memo processed!*\n\n"
                                    f"{summary}\n\n"
                                    f"📝 Transcript: {details.get('transcript_length', 0)} chars",
                                    parse_mode='Markdown'
                                )
                            logger.info(f"Direct processing successful: {summary}")
                            return
                        else:
                            logger.warning(f"Pipeline processing failed: {result.get('error')}")
                            # Fall through to Google Drive backup
                    else:
                        logger.warning(f"Pipeline returned {response.status_code}")
                        # Fall through to Google Drive backup
                        
            except Exception as e:
                logger.error(f"Direct upload failed: {e}")
                # Fall through to Google Drive backup
        
        # Fallback: Upload to Google Drive for scheduler processing
        await status_msg.edit_text("☁️ Uploading to Google Drive...")
        file_bytes.seek(0)  # Reset stream position
        
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
            f"✅ Audio uploaded to Drive\n\n"
            f"📁 File: `{filename}`\n\n"
            f"⏳ Will be processed within 15 minutes.",
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
    
    status_msg = await update.message.reply_text("⏳ Processing audio file...")
    
    try:
        file = await context.bot.get_file(audio.file_id)
        file_bytes = io.BytesIO()
        await file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        ext = audio.mime_type.split('/')[-1] if audio.mime_type else 'mp3'
        mimetype = audio.mime_type or 'audio/mpeg'
        filename = f"audio_{timestamp}_{user.username or user.id}.{ext}"
        
        # Try direct upload to audio pipeline first (faster, no Google Drive)
        if AUDIO_PIPELINE_URL:
            try:
                await status_msg.edit_text("🔄 Transcribing and analyzing...")
                
                async with httpx.AsyncClient(timeout=300.0) as client:
                    # Send file directly to pipeline
                    files = {'file': (filename, file_bytes.getvalue(), mimetype)}
                    data = {'username': user.username or str(user.id)}
                    
                    response = await client.post(
                        f"{AUDIO_PIPELINE_URL}/process/upload",
                        files=files,
                        data=data
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        
                        if result.get("status") == "success":
                            summary = result.get("summary", "Processed successfully")
                            details = result.get("details", {})
                            
                            # Check if we need contact linking buttons
                            contact_matches = details.get("contact_matches", [])
                            meeting_ids = details.get("meeting_ids", [])
                            
                            keyboard = build_contact_keyboard(contact_matches, meeting_ids)
                            
                            if keyboard:
                                await status_msg.edit_text(
                                    f"✅ *Audio processed!*\n\n"
                                    f"{summary}\n\n"
                                    f"📝 Transcript: {details.get('transcript_length', 0)} chars",
                                    parse_mode='Markdown',
                                    reply_markup=keyboard
                                )
                            else:
                                await status_msg.edit_text(
                                    f"✅ *Audio processed!*\n\n"
                                    f"{summary}\n\n"
                                    f"📝 Transcript: {details.get('transcript_length', 0)} chars",
                                    parse_mode='Markdown'
                                )
                            logger.info(f"Direct processing successful: {summary}")
                            return
                        else:
                            logger.warning(f"Pipeline processing failed: {result.get('error')}")
                            # Fall through to Google Drive backup
                    else:
                        logger.warning(f"Pipeline returned {response.status_code}")
                        # Fall through to Google Drive backup
                        
            except Exception as e:
                logger.error(f"Direct upload failed: {e}")
                # Fall through to Google Drive backup
        
        # Fallback: Upload to Google Drive for scheduler processing
        await status_msg.edit_text("☁️ Uploading to Google Drive...")
        file_bytes.seek(0)  # Reset stream position
        
        drive_service = get_drive_service()
        
        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        
        media = MediaIoBaseUpload(
            file_bytes,
            mimetype=mimetype,
            resumable=True
        )
        
        uploaded_file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id,name'
        ).execute()
        
        logger.info(f"Uploaded to Drive: {uploaded_file['name']}")
        
        await status_msg.edit_text(
            f"✅ Audio uploaded to Drive\n\n"
            f"📁 File: `{filename}`\n\n"
            f"⏳ Will be processed within 15 minutes.",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"Error processing audio file: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error: {str(e)}")


# =========================================================================
# CONTACT LINKING HELPERS
# =========================================================================

def build_contact_keyboard(contact_matches: list, meeting_ids: list) -> InlineKeyboardMarkup | None:
    """
    Build inline keyboard for contact linking actions.
    Returns None if no actions needed.
    """
    if not contact_matches:
        return None
    
    keyboard = []
    
    for i, match in enumerate(contact_matches):
        meeting_id = match.get('meeting_id') or (meeting_ids[i] if i < len(meeting_ids) else None)
        if not meeting_id:
            continue
            
        searched_name = match.get('searched_name', 'Unknown')
        
        # If already matched, no buttons needed
        if match.get('matched'):
            continue
        
        suggestions = match.get('suggestions', [])
        
        if suggestions:
            # Add buttons for each suggestion
            row = []
            for suggestion in suggestions[:3]:  # Max 3 suggestions per row
                contact_id = suggestion.get('id')
                name = suggestion.get('name', 'Unknown')
                # Store action data
                callback_key = f"link:{meeting_id}:{contact_id}"
                pending_contact_actions[callback_key] = {
                    'meeting_id': meeting_id,
                    'contact_id': contact_id,
                    'contact_name': name,
                    'searched_name': searched_name
                }
                row.append(InlineKeyboardButton(name, callback_data=callback_key))
            keyboard.append(row)
            
            # Add "Create New" button
            create_key = f"create:{meeting_id}:{searched_name}"
            pending_contact_actions[create_key] = {
                'meeting_id': meeting_id,
                'searched_name': searched_name
            }
            keyboard.append([
                InlineKeyboardButton(f"➕ Create '{searched_name}'", callback_data=create_key),
                InlineKeyboardButton("⏭️ Skip", callback_data=f"skip:{meeting_id}")
            ])
        else:
            # No suggestions - just Create or Skip
            create_key = f"create:{meeting_id}:{searched_name}"
            pending_contact_actions[create_key] = {
                'meeting_id': meeting_id,
                'searched_name': searched_name
            }
            keyboard.append([
                InlineKeyboardButton(f"➕ Create '{searched_name}'", callback_data=create_key),
                InlineKeyboardButton("⏭️ Skip", callback_data=f"skip:{meeting_id}")
            ])
    
    return InlineKeyboardMarkup(keyboard) if keyboard else None


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()  # Acknowledge the callback
    
    callback_data = query.data
    logger.info(f"Callback received: {callback_data}")
    
    if callback_data.startswith("link:"):
        # Link to existing contact
        await handle_link_contact(query, callback_data)
    elif callback_data.startswith("create:"):
        # Create new contact
        await handle_create_contact(query, callback_data)
    elif callback_data.startswith("skip:"):
        # Skip linking
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏭️ Skipped contact linking.")


async def handle_link_contact(query, callback_data: str) -> None:
    """Link meeting to an existing contact."""
    action_data = pending_contact_actions.get(callback_data)
    if not action_data:
        await query.message.reply_text("❌ Action expired. Please try again.")
        return
    
    meeting_id = action_data['meeting_id']
    contact_id = action_data['contact_id']
    contact_name = action_data['contact_name']
    
    try:
        if not INTELLIGENCE_SERVICE_URL:
            await query.message.reply_text("❌ Intelligence service not configured.")
            return
            
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.patch(
                f"{INTELLIGENCE_SERVICE_URL}/api/v1/meetings/{meeting_id}/link-contact",
                json={"contact_id": contact_id}
            )
            
            if response.status_code == 200:
                result = response.json()
                company = result.get('company', '')
                if company:
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_text(f"✅ Linked to: {contact_name} ({company})")
                else:
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_text(f"✅ Linked to: {contact_name}")
                logger.info(f"Linked meeting {meeting_id} to contact {contact_id}")
            else:
                await query.message.reply_text(f"❌ Failed to link contact: {response.text}")
                
    except Exception as e:
        logger.error(f"Error linking contact: {e}")
        await query.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        # Clean up pending action
        pending_contact_actions.pop(callback_data, None)


async def handle_create_contact(query, callback_data: str) -> None:
    """Prompt user to type the correct name for a new contact."""
    action_data = pending_contact_actions.get(callback_data)
    if not action_data:
        await query.message.reply_text("❌ Action expired. Please try again.")
        return
    
    meeting_id = action_data['meeting_id']
    searched_name = action_data['searched_name']
    user_id = query.from_user.id
    
    # Store pending creation state for this user
    pending_contact_creation[user_id] = {
        'meeting_id': meeting_id,
        'suggested_name': searched_name
    }
    
    # Remove keyboard and ask for the name
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"✏️ *Type the correct name* for this contact:\n\n"
        f"_(Detected: {searched_name})_\n\n"
        f"Just send the name, e.g. `Jasi Mueller`",
        parse_mode='Markdown'
    )
    
    # Clean up the callback action
    pending_contact_actions.pop(callback_data, None)


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages - used for typing contact names."""
    user = update.effective_user
    user_id = user.id
    
    # Check authorization
    if not is_authorized(user_id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    # Check if this user is in the middle of creating a contact
    if user_id not in pending_contact_creation:
        # Not expecting a contact name, ignore or provide help
        await update.message.reply_text(
            "👋 Send me a voice message or audio file to process!\n\n"
            "Type /help for more info."
        )
        return
    
    # Get the pending creation data
    creation_data = pending_contact_creation.pop(user_id)
    meeting_id = creation_data['meeting_id']
    typed_name = update.message.text.strip()
    
    if not typed_name:
        await update.message.reply_text("❌ Please provide a name. Try again by tapping 'Create' on the original message.")
        return
    
    # Parse name into first/last
    name_parts = typed_name.split()
    first_name = name_parts[0] if name_parts else typed_name
    last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else None
    
    # Create the contact
    try:
        if not INTELLIGENCE_SERVICE_URL:
            await update.message.reply_text("❌ Intelligence service not configured.")
            return
            
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "first_name": first_name,
                "link_to_meeting_id": meeting_id
            }
            if last_name:
                payload["last_name"] = last_name
            
            response = await client.post(
                f"{INTELLIGENCE_SERVICE_URL}/api/v1/contacts",
                json=payload
            )
            
            if response.status_code == 200:
                result = response.json()
                contact_name = result.get('contact_name', typed_name)
                await update.message.reply_text(f"✅ Created and linked: *{contact_name}*", parse_mode='Markdown')
                logger.info(f"Created contact '{contact_name}' and linked to meeting {meeting_id}")
            else:
                await update.message.reply_text(f"❌ Failed to create contact: {response.text}")
                
    except Exception as e:
        logger.error(f"Error creating contact: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")


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
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    bot_app.add_handler(CallbackQueryHandler(handle_callback_query))
    
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


class MessageRequest(BaseModel):
    chat_id: int
    text: str
    parse_mode: str = None

@app.post("/send_message")
async def send_message_endpoint(msg: MessageRequest):
    """Internal endpoint to send messages via the bot."""
    global bot_app
    
    if not bot_app:
        raise HTTPException(status_code=500, detail="Bot not initialized")
        
    try:
        await bot_app.bot.send_message(
            chat_id=msg.chat_id,
            text=msg.text,
            parse_mode=msg.parse_mode
        )
        return {"status": "sent"}
    except Exception as e:
        logger.error(f"Failed to send message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
