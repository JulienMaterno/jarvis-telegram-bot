# 🤖 Jarvis Telegram Bot

> The mobile entry point. Send voice notes to Jarvis from anywhere.

## ✨ Features

*   **Voice Notes**: Forward voice messages or record them directly.
*   **Audio Files**: Upload mp3/m4a files from your phone.
*   **Auto-Upload**: Instantly uploads to the monitored Google Drive folder.

## 📱 Setup

### 1. Create a Telegram Bot
1.  Open Telegram and search for **@BotFather**.
2.  Send `/newbot` and follow instructions.
3.  Copy the **API Token**.

### 2. Configuration

Create a `.env` file:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token
GOOGLE_DRIVE_FOLDER_ID=same_folder_id_as_audio_pipeline
ALLOWED_USER_IDS=123456789,987654321
```

*   `ALLOWED_USER_IDS`: Comma-separated list of Telegram User IDs allowed to use the bot (security). You can find your ID using @userinfobot.

### 3. Run the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python main.py
```

## 🚀 Usage

1.  Start a chat with your bot.
2.  Hold the microphone button and record a thought.
3.  The bot will reply: "✅ Voice note uploaded to Drive".
4.  The **Audio Pipeline** will pick it up, transcribe it, and the **Intelligence Service** will analyze it.
5.  Check your Notion "Reflections" or "Tasks" database a few minutes later!
