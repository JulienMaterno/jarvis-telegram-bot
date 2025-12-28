"""
Jarvis Telegram Bot - Webhook Mode (for Cloud Run)
Receives voice messages and uploads them to Google Drive for processing.
"""

import os
import io
import json
import logging
import httpx
import hashlib
import time
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
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()  # Strip any whitespace from secret
GOOGLE_DRIVE_FOLDER_ID = os.getenv('GOOGLE_DRIVE_FOLDER_ID', '').strip()
WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # e.g., https://your-bot.run.app
AUDIO_PIPELINE_URL = os.getenv('AUDIO_PIPELINE_URL', '').strip()  # e.g., https://jarvis-audio-pipeline-xxx.run.app
INTELLIGENCE_SERVICE_URL = os.getenv('INTELLIGENCE_SERVICE_URL', '').strip()  # For contact operations
ALLOWED_USER_IDS = [int(id.strip()) for id in os.getenv('ALLOWED_USER_IDS', '').split(',') if id.strip()]

# Global bot application
bot_app = None

# Store pending contact actions (in-memory, good enough for single instance)
# Format: { "short_key": {"meeting_id": ..., "searched_name": ..., ...} }
pending_contact_actions = {}

# Store users waiting to type a contact name or selection
# Format: { user_id: {"pending_links": [...], "current_index": 0} }
# pending_links is a queue of unmatched contacts to process one by one
pending_contact_creation = {}

# Track recently processed file IDs to prevent duplicates (TTL ~5 minutes)
# Format: { file_unique_id: timestamp }
recently_processed_files = {}

# Counter for generating short callback keys (avoids 64-byte Telegram limit)
_callback_counter = 0

def _short_key(prefix: str) -> str:
    """Generate a short unique callback key to stay under Telegram's 64-byte limit."""
    global _callback_counter
    _callback_counter += 1
    return f"{prefix}:{_callback_counter}"


def _is_duplicate_file(file_unique_id: str) -> bool:
    """Check if file was recently processed (deduplication)."""
    import time
    now = time.time()
    
    # Clean up old entries (older than 5 minutes)
    expired = [k for k, v in recently_processed_files.items() if now - v > 300]
    for k in expired:
        recently_processed_files.pop(k, None)
    
    # Check if already processed
    if file_unique_id in recently_processed_files:
        return True
    
    # Mark as processed
    recently_processed_files[file_unique_id] = now
    return False

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


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any pending contact creation."""
    user_id = update.effective_user.id
    if user_id in pending_contact_creation:
        pending_contact_creation.pop(user_id, None)
        await update.message.reply_text("❌ Contact creation cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


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
    
    # Check for duplicate processing (Telegram sometimes resends)
    if _is_duplicate_file(voice.file_unique_id):
        logger.warning(f"Duplicate voice message detected, skipping: {voice.file_unique_id}")
        return
    
    # Clear any pending contact linking - new voice message takes priority
    if _clear_pending_contacts(user.id):
        logger.info(f"Cleared pending contact linking for user {user.id} (new voice message)")
    
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
                            
                            # Check if we need contact linking
                            contact_matches = details.get("contact_matches", [])
                            meeting_ids = details.get("meeting_ids", [])
                            
                            # Build text-based contact prompt (works in Beeper/bridges)
                            contact_prompt = build_contact_text_prompt(
                                contact_matches, meeting_ids, user.id
                            )
                            
                            # Send main result
                            await status_msg.edit_text(
                                f"✅ *Voice memo processed!*\n\n"
                                f"{summary}\n\n"
                                f"📝 Transcript: {details.get('transcript_length', 0)} chars",
                                parse_mode='Markdown'
                            )
                            
                            # Send contact prompt separately if needed
                            if contact_prompt:
                                await update.message.reply_text(contact_prompt)
                            
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
    
    # Check for duplicate processing
    if _is_duplicate_file(audio.file_unique_id):
        logger.warning(f"Duplicate audio file detected, skipping: {audio.file_unique_id}")
        return
    
    # Clear any pending contact linking - new audio message takes priority
    if _clear_pending_contacts(user.id):
        logger.info(f"Cleared pending contact linking for user {user.id} (new audio message)")
    
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
# CONTACT LINKING HELPERS (Text-based for Beeper/bridge compatibility)
# =========================================================================

def build_contact_text_prompt(contact_matches: list, meeting_ids: list, user_id: int) -> str | None:
    """
    Build a text-based prompt for contact linking (works in Beeper/bridges).
    Queues ALL unmatched contacts for the user to process one by one.
    Returns the prompt text for the FIRST unmatched contact, or None if all matched.
    
    Key behavior:
    - Multiple unmatched contacts are QUEUED, not overwritten
    - No time-based timeout - expires when user sends new voice message
    - User processes contacts one at a time
    """
    if not contact_matches:
        return None
    
    prompts = []
    pending_links = []  # Queue of unmatched contacts to process
    
    for i, match in enumerate(contact_matches):
        meeting_id = match.get('meeting_id') or (meeting_ids[i] if i < len(meeting_ids) else None)
        if not meeting_id:
            continue
            
        searched_name = match.get('searched_name', 'Unknown')
        
        # Skip if already matched with high confidence
        if match.get('matched'):
            linked = match.get('linked_contact', {})
            linked_name = linked.get('name', searched_name)
            company = linked.get('company', '')
            if company:
                prompts.append(f"👤 Linked to: {linked_name} ({company})")
            else:
                prompts.append(f"👤 Linked to: {linked_name}")
            continue
        
        # Queue this unmatched contact for later processing
        suggestions = match.get('suggestions', [])
        pending_links.append({
            'meeting_id': meeting_id,
            'searched_name': searched_name,
            'suggestions': suggestions,
            'mode': 'link_or_create'
        })
    
    # Store the queue if we have unmatched contacts
    if pending_links:
        pending_contact_creation[user_id] = {
            'pending_links': pending_links,
            'current_index': 0
        }
        
        # Build prompt for the FIRST unmatched contact
        first_contact = pending_links[0]
        searched_name = first_contact['searched_name']
        suggestions = first_contact['suggestions']
        
        total_pending = len(pending_links)
        progress_note = f"(1/{total_pending})" if total_pending > 1 else ""
        
        if suggestions:
            prompt_lines = [f"❓ Unknown contact {progress_note}: *{searched_name}*", ""]
            prompt_lines.append("Reply with:")
            for j, suggestion in enumerate(suggestions[:5], 1):
                name = suggestion.get('name', 'Unknown')
                company = suggestion.get('company', '')
                if company:
                    prompt_lines.append(f"  {j} = {name} ({company})")
                else:
                    prompt_lines.append(f"  {j} = {name}")
            prompt_lines.append(f"  0 = Skip")
            prompt_lines.append(f"  Or type the correct full name")
            prompts.append("\n".join(prompt_lines))
        else:
            prompt_lines = [
                f"❓ Unknown contact {progress_note}: *{searched_name}*",
                "",
                "Reply with:",
                "  The correct full name (e.g. 'John Smith')",
                "  Or '0' to skip"
            ]
            prompts.append("\n".join(prompt_lines))
    
    return "\n\n".join(prompts) if prompts else None


def _get_current_pending_contact(user_id: int) -> dict | None:
    """Get the current contact to process from the queue."""
    if user_id not in pending_contact_creation:
        return None
    
    data = pending_contact_creation[user_id]
    pending_links = data.get('pending_links', [])
    current_index = data.get('current_index', 0)
    
    if current_index >= len(pending_links):
        # All contacts processed, clean up
        pending_contact_creation.pop(user_id, None)
        return None
    
    return pending_links[current_index]


def _advance_to_next_contact(user_id: int) -> str | None:
    """
    Move to the next contact in the queue.
    Returns the prompt for the next contact, or None if done.
    """
    if user_id not in pending_contact_creation:
        return None
    
    data = pending_contact_creation[user_id]
    pending_links = data.get('pending_links', [])
    current_index = data.get('current_index', 0) + 1
    
    if current_index >= len(pending_links):
        # All done!
        pending_contact_creation.pop(user_id, None)
        return None
    
    # Update index
    pending_contact_creation[user_id]['current_index'] = current_index
    
    # Build prompt for next contact
    contact = pending_links[current_index]
    searched_name = contact['searched_name']
    suggestions = contact['suggestions']
    
    total = len(pending_links)
    progress = f"({current_index + 1}/{total})"
    
    if suggestions:
        prompt_lines = [f"❓ Next contact {progress}: *{searched_name}*", ""]
        prompt_lines.append("Reply with:")
        for j, suggestion in enumerate(suggestions[:5], 1):
            name = suggestion.get('name', 'Unknown')
            company = suggestion.get('company', '')
            if company:
                prompt_lines.append(f"  {j} = {name} ({company})")
            else:
                prompt_lines.append(f"  {j} = {name}")
        prompt_lines.append(f"  0 = Skip")
        prompt_lines.append(f"  Or type the correct full name")
        return "\n".join(prompt_lines)
    else:
        return (
            f"❓ Next contact {progress}: *{searched_name}*\n\n"
            "Reply with:\n"
            "  The correct full name (e.g. 'John Smith')\n"
            "  Or '0' to skip"
        )


def _clear_pending_contacts(user_id: int) -> bool:
    """Clear any pending contact linking for a user. Returns True if there was pending work."""
    if user_id in pending_contact_creation:
        pending_contact_creation.pop(user_id, None)
        return True
    return False


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
        
        # If already matched, add a "Correct" button in case it's wrong
        if match.get('matched'):
            linked = match.get('linked_contact', {})
            linked_name = linked.get('name', searched_name)
            correct_key = _short_key("R")  # R = Re-link/correct
            pending_contact_actions[correct_key] = {
                'meeting_id': meeting_id,
                'searched_name': searched_name,
                'current_contact': linked_name
            }
            keyboard.append([
                InlineKeyboardButton(f"✏️ Wrong? Correct '{linked_name}'", callback_data=correct_key)
            ])
            continue
        
        suggestions = match.get('suggestions', [])
        
        if suggestions:
            # Add buttons for each suggestion
            row = []
            for suggestion in suggestions[:3]:  # Max 3 suggestions per row
                contact_id = suggestion.get('id')
                name = suggestion.get('name', 'Unknown')
                # Use short key to avoid 64-byte Telegram limit
                callback_key = _short_key("L")
                pending_contact_actions[callback_key] = {
                    'meeting_id': meeting_id,
                    'contact_id': contact_id,
                    'contact_name': name,
                    'searched_name': searched_name
                }
                row.append(InlineKeyboardButton(name, callback_data=callback_key))
            keyboard.append(row)
            
            # Add "Create New" and "Skip" buttons
            create_key = _short_key("C")
            skip_key = _short_key("S")
            pending_contact_actions[create_key] = {
                'meeting_id': meeting_id,
                'searched_name': searched_name
            }
            pending_contact_actions[skip_key] = {'meeting_id': meeting_id}
            
            # Truncate display name if too long
            display_name = searched_name[:15] + "..." if len(searched_name) > 15 else searched_name
            keyboard.append([
                InlineKeyboardButton(f"➕ Create '{display_name}'", callback_data=create_key),
                InlineKeyboardButton("⏭️ Skip", callback_data=skip_key)
            ])
        else:
            # No suggestions - just Create or Skip
            create_key = _short_key("C")
            skip_key = _short_key("S")
            pending_contact_actions[create_key] = {
                'meeting_id': meeting_id,
                'searched_name': searched_name
            }
            pending_contact_actions[skip_key] = {'meeting_id': meeting_id}
            
            display_name = searched_name[:15] + "..." if len(searched_name) > 15 else searched_name
            keyboard.append([
                InlineKeyboardButton(f"➕ Create '{display_name}'", callback_data=create_key),
                InlineKeyboardButton("⏭️ Skip", callback_data=skip_key)
            ])
    
    return InlineKeyboardMarkup(keyboard) if keyboard else None


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()  # Acknowledge the callback
    
    callback_data = query.data
    logger.info(f"Callback received: {callback_data}")
    
    # Check if action exists in pending (short keys: L=link, C=create, S=skip)
    action_data = pending_contact_actions.get(callback_data)
    
    if callback_data.startswith("L:"):
        # Link to existing contact
        await handle_link_contact(query, callback_data, action_data)
    elif callback_data.startswith("C:"):
        # Create new contact
        await handle_create_contact(query, callback_data, action_data)
    elif callback_data.startswith("S:"):
        # Skip linking
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏭️ Skipped contact linking.")
        pending_contact_actions.pop(callback_data, None)
    elif callback_data.startswith("R:"):
        # Re-link/correct a wrong match
        await handle_correct_contact(query, callback_data, action_data)


async def handle_link_contact(query, callback_data: str, action_data: dict) -> None:
    """Link meeting to an existing contact."""
    if not action_data:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("❌ Action expired. Please process a new audio message.")
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


async def handle_correct_contact(query, callback_data: str, action_data: dict) -> None:
    """Handle correcting a wrongly matched contact - prompt for the right person."""
    if not action_data:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("❌ Action expired. Please process a new audio message.")
        return
    
    meeting_id = action_data['meeting_id']
    searched_name = action_data['searched_name']
    current_contact = action_data.get('current_contact', 'Unknown')
    user_id = query.from_user.id
    
    # Store pending creation state for this user (expires in 5 minutes)
    # mode='correct' tells the handler this is a correction, not a new contact
    pending_contact_creation[user_id] = {
        'meeting_id': meeting_id,
        'suggested_name': searched_name,
        'mode': 'correct',
        'expires': time.time() + 300
    }
    
    # Remove keyboard and ask for the correct name
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"✏️ *Who should this be linked to?*\n\n"
        f"_(Currently: {current_contact})_\n\n"
        f"Type the correct name, e.g. `Jasi Mueller`\n"
        f"Or type `/cancel` to keep current.",
        parse_mode='Markdown'
    )
    
    # Clean up the callback action
    pending_contact_actions.pop(callback_data, None)


async def handle_create_contact(query, callback_data: str, action_data: dict) -> None:
    """Prompt user to type the correct name for a new contact."""
    if not action_data:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("❌ Action expired. Please process a new audio message.")
        return
    
    meeting_id = action_data['meeting_id']
    searched_name = action_data['searched_name']
    user_id = query.from_user.id
    
    # Store pending creation state for this user (expires in 5 minutes)
    pending_contact_creation[user_id] = {
        'meeting_id': meeting_id,
        'suggested_name': searched_name,
        'expires': time.time() + 300  # 5 minute timeout
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
    """Handle text messages - used for typing contact names or selections."""
    user = update.effective_user
    user_id = user.id
    
    # Check authorization
    if not is_authorized(user_id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    # Check if this user is in the middle of contact linking
    current_contact = _get_current_pending_contact(user_id)
    if not current_contact:
        # Not expecting input, provide help
        await update.message.reply_text(
            "👋 Send me a voice message or audio file to process!\n\n"
            "Type /help for more info."
        )
        return
    
    # Get the current contact data from queue
    meeting_id = current_contact['meeting_id']
    searched_name = current_contact.get('searched_name', 'Unknown')
    suggestions = current_contact.get('suggestions', [])
    typed_text = update.message.text.strip()
    
    # Handle '0' = skip this contact
    if typed_text == '0':
        # Move to next contact in queue
        next_prompt = _advance_to_next_contact(user_id)
        if next_prompt:
            await update.message.reply_text(f"⏭️ Skipped.\n\n{next_prompt}")
        else:
            await update.message.reply_text("⏭️ Skipped. All contacts processed!")
        return
    
    # Handle numeric selection (1, 2, 3, etc.)
    if typed_text.isdigit() and suggestions:
        selection = int(typed_text)
        if 1 <= selection <= len(suggestions):
            # Link to selected suggestion
            selected = suggestions[selection - 1]
            contact_id = selected.get('id')
            contact_name = selected.get('name', 'Unknown')
            
            # Check if intelligence service URL is configured
            if not INTELLIGENCE_SERVICE_URL:
                logger.error("INTELLIGENCE_SERVICE_URL not configured - cannot link contact")
                await update.message.reply_text("❌ Intelligence service not configured.")
                return
            
            try:
                logger.info(f"Linking meeting {meeting_id} to contact {contact_id} ({contact_name})")
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.patch(
                        f"{INTELLIGENCE_SERVICE_URL}/api/v1/meetings/{meeting_id}/link-contact",
                        json={"contact_id": contact_id}
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        company = result.get('company', '')
                        link_msg = f"✅ Linked to: {contact_name}" + (f" ({company})" if company else "")
                        logger.info(f"Successfully linked meeting {meeting_id} to contact {contact_id}")
                        
                        # Move to next contact
                        next_prompt = _advance_to_next_contact(user_id)
                        if next_prompt:
                            await update.message.reply_text(f"{link_msg}\n\n{next_prompt}")
                        else:
                            await update.message.reply_text(f"{link_msg}\n\n✅ All contacts processed!")
                    else:
                        logger.error(f"Failed to link contact - status={response.status_code}, response={response.text}")
                        await update.message.reply_text(f"❌ Failed to link: {response.text}")
            except Exception as e:
                logger.error(f"Error linking contact: {e}", exc_info=True)
                await update.message.reply_text(f"❌ Error: {str(e)}")
            return
        else:
            await update.message.reply_text(f"❌ Invalid selection. Reply 1-{len(suggestions)} or type a name.")
            return
    
    # User typed a name - search or create
    typed_name = typed_text
    
    if not typed_name or len(typed_name) < 2:
        await update.message.reply_text("❌ Please provide a valid name (at least 2 characters).")
        return
    
    # Parse name into first/last
    name_parts = typed_name.split()
    first_name = name_parts[0] if name_parts else typed_name
    last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else None
    
    try:
        if not INTELLIGENCE_SERVICE_URL:
            await update.message.reply_text("❌ Intelligence service not configured.")
            return
            
        async with httpx.AsyncClient(timeout=30.0) as client:
            # First, search for existing contact with this name
            search_response = await client.get(
                f"{INTELLIGENCE_SERVICE_URL}/api/v1/contacts/search",
                params={"q": typed_name, "limit": 5}
            )
            
            existing_contacts = []
            if search_response.status_code == 200:
                existing_contacts = search_response.json().get('contacts', [])
            
            # If we found matches, update current contact's suggestions and ask user to select
            if existing_contacts:
                # Update the current contact in the queue with new suggestions
                if user_id in pending_contact_creation:
                    data = pending_contact_creation[user_id]
                    idx = data.get('current_index', 0)
                    if idx < len(data.get('pending_links', [])):
                        data['pending_links'][idx]['suggestions'] = existing_contacts
                        data['pending_links'][idx]['searched_name'] = typed_name
                
                prompt_lines = [f"Found existing contacts matching '{typed_name}':", ""]
                for j, contact in enumerate(existing_contacts[:5], 1):
                    name = contact.get('name', 'Unknown')
                    company = contact.get('company', '')
                    if company:
                        prompt_lines.append(f"  {j} = {name} ({company})")
                    else:
                        prompt_lines.append(f"  {j} = {name}")
                prompt_lines.append(f"  0 = Create new '{typed_name}'")
                
                await update.message.reply_text("\n".join(prompt_lines))
                return
            
            # No existing contacts found - create new one
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
                create_msg = f"✅ Created and linked: {contact_name}"
                logger.info(f"Created contact '{contact_name}' and linked to meeting {meeting_id}")
                
                # Move to next contact
                next_prompt = _advance_to_next_contact(user_id)
                if next_prompt:
                    await update.message.reply_text(f"{create_msg}\n\n{next_prompt}")
                else:
                    await update.message.reply_text(f"{create_msg}\n\n✅ All contacts processed!")
            else:
                await update.message.reply_text(f"❌ Failed to create contact: {response.text}")
                
    except Exception as e:
        logger.error(f"Error handling contact: {e}")
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
    bot_app.add_handler(CommandHandler("cancel", cancel_command))
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
        # If Markdown parsing fails, retry without parse_mode
        if "parse entities" in str(e).lower():
            logger.warning(f"Markdown parse failed, retrying without formatting: {e}")
            try:
                await bot_app.bot.send_message(
                    chat_id=msg.chat_id,
                    text=msg.text,
                    parse_mode=None
                )
                return {"status": "sent", "note": "sent_without_formatting"}
            except Exception as e2:
                logger.error(f"Failed to send message even without formatting: {e2}")
                raise HTTPException(status_code=500, detail=str(e2))
        logger.error(f"Failed to send message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
