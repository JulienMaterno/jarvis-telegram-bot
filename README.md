# Jarvis Telegram Bot 🤖

Simple Telegram bot that receives voice messages and uploads them to Google Drive for processing by the Jarvis audio pipeline.

## Features

- 🎙️ Receive voice messages
- 🎵 Receive audio files (mp3, m4a, etc.)
- 📤 Upload to Google Drive automatically
- 🔒 Optional user authorization

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your tokens
   ```

3. **Copy Google credentials:**
   ```bash
   mkdir data
   cp ../jarvis-audio-pipeline/data/token.json data/
   ```

4. **Run:**
   ```bash
   python main.py
   ```

## Usage

1. Open Telegram and find your bot (@NewWorldJarvisBot)
2. Send a voice message
3. Bot uploads to Google Drive
4. jarvis-audio-pipeline processes it automatically

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `GOOGLE_DRIVE_FOLDER_ID` | Target folder for uploads |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs (optional) |
