import os
import csv
import time
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from flask_cors import CORS  # Import CORS
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch
from telegram import Bot
import pandas as pd
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE")
BOT_TOKENS = [os.getenv(f"BOT_TOKEN_{i+1}") for i in range(2)]
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_FILE_PATH = "members.csv"
SESSION_NAME = "session"
DAILY_LIMIT_PER_BOT = 50

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "https://oassisjob.web.app"}})  # Allow Firebase domain
bots = [Bot(token=token) for token in BOT_TOKENS]
daily_limits = {token: {"count": 0, "reset_date": datetime.now()} for token in BOT_TOKENS}

# Rest of your original app.py code remains unchanged
# ...
# GitHub API functions
def upload_to_github(content):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    # Get current file SHA
    response = requests.get(url, headers=headers)
    sha = response.json().get("sha") if response.status_code == 200 else None
    # Encode content to base64
    import base64
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    data = {
        "message": "Update members.csv",
        "content": encoded_content,
        "sha": sha
    } if sha else {
        "message": "Create members.csv",
        "content": encoded_content
    }
    response = requests.put(url, headers=headers, json=data)
    return response.status_code == 200 or response.status_code == 201

def download_from_github():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        import base64
        content = base64.b64decode(response.json()["content"]).decode("utf-8")
        with open("members.csv", "w", encoding="utf-8") as f:
            f.write(content)
        return True
    return False

# Scrape members
async def scrape_members(source_group, progress_callback):
    async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
        await client.start(phone=PHONE)
        group = await client.get_entity(source_group)
        participants = await client(GetParticipantsRequest(
            channel=group, filter=ChannelParticipantsSearch(""), offset=0, limit=1000, hash=0
        ))
        members = []
        total = len(participants.users)
        for i, user in enumerate(participants.users):
            username = user.username if user.username else ""
            members.append({"user_id": user.id, "username": username, "first_name": user.first_name or "", "last_name": user.last_name or ""})
            progress_callback((i + 1) / total * 100)
        
        # Save to CSV
        csv_content = "user_id,username,first_name,last_name\n" + "\n".join(
            f"{m['user_id']},{m['username']},{m['first_name']},{m['last_name']}" for m in members
        )
        # Upload to GitHub
        upload_to_github(csv_content)
        return members

# Add members
async def add_members(target_group, progress_callback):
    if not download_from_github():
        return 0, "No members found in GitHub. Please scrape members first."
    
    df = pd.read_csv("members.csv")
    added_count = 0
    total = len(df)
    
    # Account rotation
    for i, bot in enumerate(bots):
        token = BOT_TOKENS[i]
        # Reset daily limit if 24 hours have passed
        if daily_limits[token]["reset_date"] < datetime.now():
            daily_limits[token] = {"count": 0, "reset_date": datetime.now() + timedelta(days=1)}
        
        bot_limit = daily_limits[token]["count"]
        if bot_limit >= DAILY_LIMIT_PER_BOT:
            continue
        
        for j, row in df.iterrows():
            if bot_limit >= DAILY_LIMIT_PER_BOT:
                break
            try:
                await bot.invite_chat_member(chat_id=target_group, user_id=row["user_id"])
                added_count += 1
                bot_limit += 1
                daily_limits[token]["count"] = bot_limit
                progress_callback((added_count) / total * 100)
                time.sleep(1)  # Respect rate limits
            except Exception as e:
                print(f"Error adding user {row['user_id']}: {str(e)}")
    
    return added_count, f"Added {added_count} members"

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
    return render_template("index.html")

@app.route("/api/scrape", methods=["POST"])
def scrape():
    global scrape_progress
    data = request.get_json()
    source_group = data.get("sourceGroup")
    user_id = data.get("userId")

    if not source_group or not user_id:
        return jsonify({"message": "Missing source group URL or user ID"}), 400

    def progress_callback(progress):
        global scrape_progress
        scrape_progress = round(progress, 2)

    try:
        import asyncio
        members = asyncio.run(scrape_members(source_group, progress_callback))
        scrape_progress = 100
        return jsonify({"message": f"Scraped {len(members)} members and saved to GitHub"})
    except Exception as e:
        return jsonify({"message": f"Error scraping members: {str(e)}"}), 500

@app.route("/api/add", methods=["POST"])
def add():
    global add_progress
    data = request.get_json()
    target_group = data.get("targetGroup")
    user_id = data.get("userId")

    if not target_group or not user_id:
        return jsonify({"message": "Missing target group URL or user ID"}), 400

    def progress_callback(progress):
        global add_progress
        add_progress = round(progress, 2)

    try:
        import asyncio
        count, message = asyncio.run(add_members(target_group, progress_callback))
        add_progress = 100
        return jsonify({"message": message})
    except Exception as e:
        return jsonify({"message": f"Error adding members: {str(e)}"}), 500

@app.route("/api/members")
def get_members():
    if not download_from_github():
        return jsonify([])
    df = pd.read_csv("members.csv")
    return jsonify(df.to_dict(orient="records"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))