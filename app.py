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
from telegram.error import BadRequest, Forbidden
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
import asyncio
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Log library version
import telegram
logger.info(f"python-telegram-bot version: {telegram.__version__}")

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
OASSISJOB_CHAT_ID = "-1002077471332"  # Numeric ID for

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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for application in applications:
        try:
            await application.initialize()
            logger.info("Application initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize application: {str(e)}")
    return loop

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
        return download_from_github(GITHUB_SESSION_PATH, f