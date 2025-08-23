import os
import csv
import time
import base64
import requests
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest, InviteToChannelRequest
from telethon.tl.types import ChannelParticipantsSearch
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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "oassise/telegram-data")
GITHUB_FILE_PATH = "members.csv"
GITHUB_SESSION_PATH = "session.session"
SESSION_NAME = "session"
DAILY_LIMIT = 50
RENDER_URL = "https://telegram-data.onrender.com"

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "https://oassisjob.web.app"}})
daily_limit = {"count": 0, "reset_date": datetime.now()}

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
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{file_path}"
            headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
            logger.info(f"Attempt {attempt}: Downloading {file_path} from GitHub API to {local_path}")
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 200:
                content = base64.b64decode(response.json()["content"])
                write_content = content.decode("utf-8") if not file_path.endswith(".session") else content
                with open(local_path, "w" if not file_path.endswith(".session") else "wb") as f:
                    f.write(write_content)
                logger.info(f"Downloaded {file_path} to {local_path}, size: {len(content)} bytes")
                return True
            else:
                logger.error(f"GitHub API download failed: {response.status_code}, {response.json()}")
                raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{file_path}"
                raw_response = requests.get(raw_url, timeout=15)
                if raw_response.status_code == 200:
                    content = raw_response.content
                    write_content = content.decode("utf-8") if not file_path.endswith(".session") else content
                    with open(local_path, "w" if not file_path.endswith(".session") else "wb") as f:
                        f.write(write_content)
                    logger.info(f"Downloaded {file_path} to {local_path} via raw URL, size: {len(content)} bytes")
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

# Download session file
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
            csv_content = "user_id,username,first_name,last_name\n"
            for m in members:
                csv_content += f"{m['user_id']},{m['username']},{m['first_name']},{m['last_name']}\n"
            with open("debug.csv", "w") as f:
                f.write(csv_content)
            if not upload_to_github(csv_content.encode("utf-8"), GITHUB_FILE_PATH):
                raise Exception("Failed to upload members.csv to GitHub")
            if os.path.exists(f"{SESSION_NAME}.session"):
                with open(f"{SESSION_NAME}.session", "rb") as f:
                    upload_to_github(f.read(), GITHUB_SESSION_PATH)
            return members
        except Exception as e:
            logger.error(f"Error in scrape_members: {str(e)}")
            raise e

# Add members using Telethon
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
        chat_id = target_group.replace("https://t.me/", "@")
        logger.info(f"Adding {total} members to {chat_id}")
        async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
            try:
                await client.start(phone=lambda: PHONE, password=lambda: os.getenv("TELEGRAM_PASSWORD"))
                group = await client.get_entity(chat_id)
                existing_members = set()
                participants = await client(GetParticipantsRequest(
                    channel=group, filter=ChannelParticipantsSearch(""), offset=0, limit=1000, hash=0
                ))
                for user in participants.users:
                    existing_members.add(user.id)
                if daily_limit["reset_date"] < datetime.now():
                    daily_limit["count"] = 0
                    daily_limit["reset_date"] = datetime.now() + timedelta(days=1)
                if daily_limit["count"] >= DAILY_LIMIT:
                    logger.warning(f"Reached daily limit of {DAILY_LIMIT}")
                    return 0, f"Reached daily limit of {DAILY_LIMIT}"
                for i, row in df.iterrows():
                    user_id = int(row["user_id"])
                    if user_id in existing_members:
                        logger.info(f"User {user_id} already in {chat_id}, skipping...")
                        continue
                    if daily_limit["count"] >= DAILY_LIMIT:
                        logger.warning(f"Reached daily limit of {DAILY_LIMIT}")
                        break
                    try:
                        await client(InviteToChannelRequest(channel=group, users=[user_id]))
                        added_count += 1
                        daily_limit["count"] += 1
                        progress_callback((added_count) / total * 100)
                        logger.info(f"Added user {user_id} to {chat_id}")
                        await asyncio.sleep(60)  # Avoid flood limits
                    except Exception as e:
                        logger.error(f"Error adding user {user_id} to {chat_id}: {str(e)}")
                        if "flood" in str(e).lower():
                            logger.warning(f"Flood limit hit, waiting 900 seconds...")
                            await asyncio.sleep(900)
                            continue
                        if "user already participant" in str(e).lower():
                            logger.info(f"User {user_id} already in {chat_id}, skipping...")
                            continue
                        if "user_id_invalid" in str(e).lower():
                            logger.warning(f"Invalid user ID {user_id}, skipping...")
                            continue
                        if "chat_admin_required" in str(e).lower():
                            return 0, f"Client is not an admin of {chat_id}"
                        logger.warning(f"Failed to add user {user_id}, skipping...")
                return added_count, f"Added {added_count} members to {chat_id}"
            except Exception as e:
                logger.error(f"Error in add_members: {str(e)}")
                return 0, f"Error adding members: {str(e)}"
    except Exception as e:
        logger.error(f"Error in add_members: {str(e)}")
        return 0, f"Error adding members: {str(e)}"

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))