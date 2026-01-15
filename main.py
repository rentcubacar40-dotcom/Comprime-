import os
import asyncio
import threading
import time
import math
import subprocess
import json
import re

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ===================== CONFIG =====================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

PORT = int(os.getenv("PORT", 10000))

DOWNLOAD_DIR = "downloads"
OUTPUT_DIR = "output"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===================== WEB (Render needs PORT) =====================
web = Flask(__name__)

@web.route("/")
def home():
    return "Telegram Video Compressor Bot running"

def run_web():
    web.run(host="0.0.0.0", port=PORT)

# ===================== BOT INIT =====================
app = Client(
    name="video-compressor",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8,
    in_memory=True
)

# ===================== UTILS =====================
def progress_bar(percent: int, size: int = 20) -> str:
    filled = int(size * percent / 100)
    return "█" * filled + "░" * (size - filled)

def get_video_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path
    ]
    result = subprocess.check_output(cmd)
    return float(json.loads(result)["format"]["duration"])

async def safe_edit(msg, text):
    try:
        await msg.edit_text(text)
    except:
        pass

def clean_files(*paths):
    for p in paths:
        if p and os.path.exists(p):
            os.remove(p)

# ===================== START =====================
@app.on_message(filters.command("start"))
async def start(_, msg):
    await msg.reply(
        "🎬 **Video Compressor Bot 2026**\n\n"
        "✔ Hasta **4GB reales**\n"
        "✔ Progreso REAL con barra\n"
        "✔ 360p / 480p / 720p\n\n"
        "📤 Envíame un video",
        quote=True
    )

# ===================== RECEIVE VIDEO =====================
@app.on_message(filters.video | filters.document)
async def receive_video(_, msg):
    media = msg.video or msg.document
    input_path = f"{DOWNLOAD_DIR}/{media.file_unique_id}.mp4"

    status = await msg.reply("⬇️ Descargando...\n\n░░░░░░░░░░░░░░░░░░ 0%")

    last_update = time.time()

    async def download_progress(current, total):
        nonlocal last_update
        if total == 0:
            return
        if time.time() - last_update < 1:
            return
        last_update = time.time()

        percent = int(current * 100 / total)
        bar = progress_bar(percent)

        await safe_edit(
            status,
            f"⬇️ Descargando...\n\n{bar} {percent}%"
        )

    await app.download_media(
        media,
        file_name=input_path,
        progress=download_progress
    )

    await status.edit(
        "✅ Video recibido\n\n📉 Elige resolución:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("360p", callback_data=f"360|{input_path}"),
                InlineKeyboardButton("480p", callback_data=f"480|{input_path}"),
                InlineKeyboardButton("720p", callback_data=f"720|{input_path}")
            ]
        ])
    )

# ===================== CALLBACK =====================
@app.on_callback_query()
async def compress_callback(_, cb):
    res, input_path = cb.data.split("|")

    scale_map = {
        "360": "640:360",
        "480": "854:480",
        "720": "1280:720"
    }

    scale = scale_map[res]
    output_path = f"{OUTPUT_DIR}/{res}_{os.path.basename(input_path)}"

    duration = get_video_duration(input_path)

    await cb.message.edit_text(
        "⚙️ Comprimiendo...\n\n░░░░░░░░░░░░░░░░░░ 0%"
    )

    cmd = [
    "ffmpeg", "-y",
    "-i", input_path,

    "-vf", f"scale={scale},fps=30",
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-crf", "30",
    "-pix_fmt", "yuv420p",
    "-profile:v", "baseline",
    "-movflags", "+faststart",

    "-c:a", "aac",
    "-b:a", "96k",

    "-progress", "pipe:1",
    "-nostats",
    output_path
]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True
    )

    time_regex = re.compile(r"out_time_ms=(\d+)")
    last_update = time.time()

    while True:
        line = process.stdout.readline()
        if not line:
            break

        match = time_regex.search(line)
        if match:
            current_time = int(match.group(1)) / 1_000_000
            percent = min(100, int(current_time * 100 / duration))
            if time.time() - last_update >= 1:
                last_update = time.time()
                bar = progress_bar(percent)
                await safe_edit(
                    cb.message,
                    f"⚙️ Comprimiendo...\n\n{bar} {percent}%"
                )

        await asyncio.sleep(0.05)

    process.wait()

    # ===================== UPLOAD =====================
    await cb.message.edit_text(
        "⬆️ Subiendo video...\n\n░░░░░░░░░░░░░░░░░░ 0%"
    )

    last_update = time.time()

    async def upload_progress(current, total):
        nonlocal last_update
        if total == 0:
            return
        if time.time() - last_update < 1:
            return
        last_update = time.time()

        percent = int(current * 100 / total)
        bar = progress_bar(percent)

        await safe_edit(
            cb.message,
            f"⬆️ Subiendo video...\n\n{bar} {percent}%"
        )

    await cb.message.reply_video(
        video=output_path,
        supports_streaming=True,
        progress=upload_progress
    )

    clean_files(input_path, output_path)
    await cb.message.delete()

# ===================== MAIN =====================
if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    app.run()
