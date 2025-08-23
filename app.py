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

# Initialize applications
async def initialize_applications():
    for application in applications:
        try:
            await application.initialize()
            logger.info("Application initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize application: {str(e)}")

# GitHub API functions
def upload_to_github(content, file_path):
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        response = requests.get(url, headers=headers)
        sha = response.json().get("sha") if response.status_code == 200 else None
        encoded_content = base64.b64encode(content).decode("utf-8")
        data = {
            "message": f"Update {file_path}",
            "content": encoded_content,
            "sha": sha
        } if sha else {
            "message": f"Create {file_path}",
            "content": encoded_content
        }
        logger.info(f"Uploading to GitHub: {file_path}, content length: {len(encoded_content)}")
        response = requests.put(url, headers=headers, json=data)
        if response.status_code not in (200, 201):
            logger.error(f"GitHub upload failed: {response.status_code}, {response.json()}")
            raise Exception(f"GitHub upload failed: {response.json().get('message', 'Unknown error')}")
        logger.info(f"Successfully uploaded {file_path} to GitHub")
        return True
    except Exception as e:
        logger.error(f"Error in upload_to_github: {str(e)}")
        raise e

def download_from_github(file_path, local_path, retries=5, delay=10):
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"Current working directory: {os.getcwd()}")
            logger.info(f"Write permission for {local_path}: {os.access(os.getcwd(), os.W_OK)}")
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
            headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
            logger.info(f"Attempt {attempt}: Downloading {file_path} from GitHub API to {local_path}")
            response = requests.get(url, headers=headers, timeout=15)
            logger.info(f"GitHub API response status: {response.status_code}, headers: {response.headers}")
            if response.status_code == 200:
                content = base64.b64decode(response.json()["content"])
                if not content:
                    logger.error("Downloaded content is empty")
                    return False
                write_content = content.decode("utf-8") if not file_path.endswith(".session") else content
                logger.info(f"Content type: {'text' if not file_path.endswith('.session') else 'binary'}, length: {len(content)} bytes")
                with open(local_path, "w" if not file_path.endswith(".session") else "wb") as f:
                    f.write(write_content)
                logger.info(f"Downloaded {file_path} to {local_path}, size: {len(content)} bytes")
                if not os.path.exists(local_path):
                    logger.error(f"File {local_path} not found after writing")
                    return False
                with open(local_path, "r" if not file_path.endswith(".session") else "rb") as f:
                    preview = f.read(100)
                    logger.info(f"File {local_path} preview: {preview}")
                return True
            elif response.status_code == 403 and "rate limit" in response.text.lower():
                logger.warning(f"GitHub rate limit exceeded, retrying after {delay} seconds...")
                time.sleep(delay)
            else:
                logger.error(f"GitHub API download failed: {response.status_code}, {response.json()}")
                logger.info(f"Attempt {attempt}: Trying raw URL for {file_path}")
                raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{file_path}"
                raw_response = requests.get(raw_url, timeout=15)
                logger.info(f"Raw URL response status: {raw_response.status_code}, headers: {raw_response.headers}")
                if raw_response.status_code == 200:
                    content = raw_response.content
                    write_content = content.decode("utf-8") if not file_path.endswith(".session") else content
                    logger.info(f"Raw content type: {'text' if not file_path.endswith('.session') else 'binary'}, length: {len(content)} bytes")
                    with open(local_path, "w" if not file_path.endswith(".session") else "wb") as f:
                        f.write(write_content)
                    logger.info(f"Downloaded {file_path} to {local_path} via raw URL, size: {len(content)} bytes")
                    if not os.path.exists(local_path):
                        logger.error(f"File {local_path} not found after writing via raw URL")
                        return False
                    with open(local_path, "r" if not file_path.endswith(".session") else "rb") as f:
                        preview = f.read(100)
                        logger.info(f"File {local_path} preview: {preview}")
                    return True
                else:
                    logger.error(f"Raw URL download failed: {raw_response.status_code}, {raw_response.text[:100]}")
                    return False
        except Exception as e:
            logger.error(f"Error in download_from_github (attempt {attempt}): {str(e)}")
            if attempt == retries:
                return False
            time.sleep(delay)
    logger.error(f"Failed to download {file_path} after {retries} attempts")
    return False

# Download session file before starting client
def ensure_session():
    if not os.path.exists(f"{SESSION_NAME}.session"):
        return download_from_github(GITHUB_SESSION_PATH, f"{SESSION_NAME}.session")
    return True

# Scrape members
async def scrape_members(source_group, progress_callback):
    if not ensure_session():
        raise Exception("Failed to download session file")
    async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
        try:
            await client.start(phone=lambda: PHONE, password=lambda: os.getenv("TELEGRAM_PASSWORD"))
        except Exception as e:
            logger.error(f"Error starting TelegramClient: {str(e)}")
            if os.path.exists(f"{SESSION_NAME}.session"):
                with open(f"{SESSION_NAME}.session", "rb") as f:
                    upload_to_github(f.read(), GITHUB_SESSION_PATH)
            raise e
        try:
            group = await client.get_entity(source_group)
            participants = await client(GetParticipantsRequest(
                channel=group, filter=ChannelParticipantsSearch(""), offset=0, limit=1000, hash=0
            ))
            members = []
            total = len(participants.users)
            logger.info(f"Scraped {total} members from {source_group}")
            if total == 0:
                raise Exception("No members found in the group")
            for i, user in enumerate(participants.users):
                username = user.username if user.username else ""
                members.append({
                    "user_id": user.id,
                    "username": username,
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or ""
                })
                progress_callback((i + 1) / total * 100)
                await asyncio.sleep(0.5)
            # Generate CSV content
            csv_content = "user_id,username,first_name,last_name\n"
            for m in members:
                csv_content += f"{m['user_id']},{m['username']},{m['first_name']},{m['last_name']}\n"
            logger.info(f"CSV content length: {len(csv_content)}")
            # Save debug CSV locally for verification
            with open("debug.csv", "w") as f:
                f.write(csv_content)
            logger.info("Saved debug.csv locally")
            # Upload to GitHub
            if not upload_to_github(csv_content.encode("utf-8"), GITHUB_FILE_PATH):
                raise Exception("Failed to upload members.csv to GitHub")
            # Save and upload session file
            if os.path.exists(f"{SESSION_NAME}.session"):
                with open(f"{SESSION_NAME}.session", "rb") as f:
                    upload_to_github(f.read(), GITHUB_SESSION_PATH)
            return members
        except Exception as e:
            logger.error(f"Error in scrape_members: {str(e)}")
            raise e

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
        # Process target_group 
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

# Webhook handler
@app.route("/telegram/<token>", methods=["POST"])
async def telegram_webhook(token):
    try:
        if token not in BOT_TOKENS:
            logger.error(f"Invalid bot token: {token[:10]}...")
            return jsonify({"status": "error", "message": "Invalid bot token"}), 401
        data = request.get_json(silent=True)
        if not data:
            logger.error("Invalid JSON payload in webhook")
            return jsonify({"status": "error", "message": "Invalid JSON payload"}), 400
        application = applications[BOT_TOKENS.index(token)]
        update = Update.de_json(data, bots[BOT_TOKENS.index(token)])
        if update:
            await application.process_update(update)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error for token {token[:10]}...: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Health check
@app.route("/healthcheck", methods=["GET"])
def health():
    return "The bot is running fine :)"

# Progress streaming
scrape_progress = 0
add_progress = 0

@app.route("/api/scrape-progress")
def scrape_progress_stream():
    def generate():
        global scrape_progress
        yield f"data: {{\"progress\": {scrape_progress}}}\n\n"
    return Response(stream_with_context(generate()), content_type="text/event-stream")

@app.route("/api/add-progress")
def add_progress_stream():
    def generate():
        global add_progress
        yield f"data: {{\"progress\": {add_progress}}}\n\n"
    return Response(stream_with_context(generate()), content_type="text/event-stream")

# Routes
@app.route("/")
def index():
    return jsonify({"message": "Please access the Mini App via Telegram"})

@app.route("/api/scrape", methods=["POST"])
async def scrape():
    global scrape_progress
    data = request.get_json(silent=True)
    if not data:
        logger.error("Invalid JSON payload in /api/scrape")
        return jsonify({"message": "Invalid JSON payload"}), 400
    source_group = data.get("sourceGroup")
    user_id = data.get("userId")
    if not source_group or not user_id:
        logger.error("Missing source group URL or user ID")
        return jsonify({"message": "Missing source group URL or user ID"}), 400
    def progress_callback(progress):
        global scrape_progress
        scrape_progress = round(progress, 2)
    try:
        members = await scrape_members(source_group, progress_callback)
        scrape_progress = 100
        return jsonify({"message": f"Scraped {len(members)} members and saved to GitHub"})
    except Exception as e:
        logger.error(f"Scrape error: {str(e)}")
        return jsonify({"message": f"Error scraping members: {str(e)}"}), 500

@app.route("/api/add", methods=["POST"])
async def add():
    global add_progress
    data = request.get_json(silent=True)
    if not data:
        logger.error("Invalid JSON payload in /api/add")
        return jsonify({"message": "Invalid JSON payload"}), 400
    target_group = data.get("targetGroup")
    user_id = data.get("userId")
    if not target_group or not user_id:
        logger.error("Missing target group URL or user ID")
        return jsonify({"message": "Missing target group URL or user ID"}), 400
    def progress_callback(progress):
        global add_progress
        add_progress = round(progress, 2)
    try:
        count, message = await add_members(target_group, progress_callback)
        add_progress = 100
        return jsonify({"message": message})
    except Exception as e:
        logger.error(f"Add error: {str(e)}")
        return jsonify({"message": f"Error adding members: {str(e)}"}), 500

@app.route("/api/members")
def get_members():
    if not download_from_github(GITHUB_FILE_PATH, "members.csv"):
        logger.error("Failed to download members.csv")
        return jsonify([])
    try:
        df = pd.read_csv("members.csv")
        return jsonify(df.to_dict(orient="records"))
    except Exception as e:
        logger.error(f"Error reading members.csv: {str(e)}")
        return jsonify([])

@app.route("/api/debug-csv")
def get_debug_csv():
    try:
        with open("debug.csv", "r") as f:
            return Response(f.read(), mimetype="text/csv")
    except Exception as e:
        logger.error(f"Error reading debug.csv: {str(e)}")
        return jsonify({"message": "Debug CSV not found"}), 404

@app.route("/api/check-members")
def check_members():
    try:
        if not download_from_github(GITHUB_FILE_PATH, "members.csv"):
            logger.error("check-members: Failed to download members.csv")
            return jsonify({"message": "Failed to download members.csv"}), 500
        with open("members.csv", "r") as f:
            content = f.read()
        df = pd.read_csv("members.csv")
        return jsonify({"message": "Members found", "rows": len(df), "content_preview": content[:100]})
    except Exception as e:
        logger.error(f"Error checking members.csv: {str(e)}")
        return jsonify({"message": f"Error: {str(e)}"}), 500

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