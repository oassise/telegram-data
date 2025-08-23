import os
import csv
import time
import base64
import requests
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch
from telegram import Bot, Update
from telegram.ext import Application
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
import asyncio
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Configuration
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")
BOT_TOKENS = [os.getenv(f"BOT_TOKEN_{i+1}") for i in range(2) if os.getenv(f"BOT_TOKEN_{i+1}")]
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "oassise/telegram-data")
GITHUB_FILE_PATH = "members.csv"
GITHUB_SESSION_PATH = "session.session"
SESSION_NAME = "session"
DAILY_LIMIT_PER_BOT = 50
RENDER_URL = "https://telegram-data.onrender.com"

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "https://oassisjob.web.app"}})
bots = []
applications = []
for token in BOT_TOKENS:
    try:
        bot = Bot(token=token)
        application = Application.builder().token(token).build()
        bots.append(bot)
        applications.append(application)
        logger.info(f"Initialized bot with token {token[:10]}...")
    except Exception as e:
        logger.error(f"Failed to initialize bot with token {token[:10]}...: {str(e)}")
daily_limits = {token: {"count": 0, "reset_date": datetime.now()} for token in BOT_TOKENS}

# ... (keep existing initialize_applications, upload_to_github, download_from_github, ensure_session, scrape_members functions unchanged)

# Add members
async def add_members(target_group, progress_callback):
    if not download_from_github(GITHUB_FILE_PATH, "members.csv"):
        logger.error("Failed to download members.csv from GitHub")
        return 0, "No members found in GitHub. Please scrape members first."
    try:
        if not os.path.exists("members.csv"):
            logger.error("members.csv not found locally after download attempt")
            return 0, "No members found in GitHub. Please scrape members first."
        with open("members.csv", "r") as f:
            preview = f.read(100)
            logger.info(f"members.csv content preview: {preview}...")
        df = pd.read_csv("members.csv")
        if df.empty:
            logger.error("members.csv is empty")
            return 0, "No members found in GitHub. Please scrape members first."
        added_count = 0
        total = len(df)
        # Process target_group URL
        chat_id = target_group.replace("https://t.me/", "@")
        logger.info(f"Adding {total} members to {chat_id}")
        for i, bot in enumerate(bots):
            token = BOT_TOKENS[i]
            if daily_limits[token]["reset_date"] < datetime.now():
                daily_limits[token] = {"count": 0, "reset_date": datetime.now() + timedelta(days=1)}
            bot_limit = daily_limits[token]["count"]
            if bot_limit >= DAILY_LIMIT_PER_BOT:
                logger.warning(f"Bot {i+1} has reached daily limit of {DAILY_LIMIT_PER_BOT}")
                continue
            for j, row in df.iterrows():
                if bot_limit >= DAILY_LIMIT_PER_BOT:
                    logger.warning(f"Bot {i+1} has reached daily limit of {DAILY_LIMIT_PER_BOT}")
                    break
                try:
                    await bot.invite_chat_members(chat_id=chat_id, user_ids=[row["user_id"]])
                    added_count += 1
                    bot_limit += 1
                    daily_limits[token]["count"] = bot_limit
                    progress_callback((added_count) / total * 100)
                    logger.info(f"Added user {row['user_id']} to {chat_id}")
                    await asyncio.sleep(1)  # Avoid Telegram rate limits
                except Exception as e:
                    logger.error(f"Error adding user {row['user_id']} to {chat_id}: {str(e)}")
                    if "bot is not a member" in str(e).lower() or "chat_admin_required" in str(e).lower():
                        return 0, f"Bot {i+1} is not an admin or member of {chat_id}"
                    if "too many requests" in str(e).lower():
                        logger.warning(f"Rate limit hit for bot {i+1}, waiting 10 seconds...")
                        await asyncio.sleep(10)
                    if "user_id_invalid" in str(e).lower():
                        logger.warning(f"Invalid user ID {row['user_id']} for {chat_id}, skipping...")
                        continue
        return added_count, f"Added {added_count} members to {chat_id}"
    except Exception as e:
        logger.error(f"Error in add_members: {str(e)}")
        return 0, f"Error adding members: {str(e)}"

# ... (keep existing telegram_webhook, health, scrape_progress_stream, add_progress_stream, index, scrape, members, debug-csv, check-members, setup_webhooks functions unchanged)

async def setup_webhooks():
    for i, bot in enumerate(bots):
        try:
            await bot.set_webhook(url=f"{RENDER_URL}/telegram/{BOT_TOKENS[i]}")
            logger.info(f"Webhook set for bot {i+1}")
        except Exception as e:
            logger.error(f"Failed to set webhook for bot {i+1}: {str(e)}")

if __name__ == "__main__":
    asyncio.run(initialize_applications())
    asyncio.run(setup_webhooks())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))