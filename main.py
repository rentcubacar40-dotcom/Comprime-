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

# Añadir ID del administrador (puedes poner varios separados por comas)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "7363341763").split(",") if x.strip()]

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

# ===================== MIDDLEWARE PARA ADMIN =====================
def admin_only(func):
    async def wrapper(client, message):
        if message.from_user.id not in ADMIN_IDS:
            await message.reply("🚫 **Acceso denegado**\n\nEste bot es solo para administradores.")
            return
        await func(client, message)
    return wrapper

# ===================== START =====================
@app.on_message(filters.command("start"))
@admin_only
async def start(_, msg):
    await msg.reply(
        "🎬 **Video Compressor Bot 2026**\n\n"
        "✔ Hasta **4GB reales**\n"
        "✔ Progreso REAL con barra\n"
        "✔ 360p / 480p / 720p\n\n"
        "📥 **Nuevo flujo:**\n"
        "1. Primero elige compresión\n"
        "2. Luego envía el video\n\n"
        "👇 Presiona el botón para empezar:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Elegir Compresión", callback_data="choose_compression")]
        ])
    )

# ===================== ELEGIR COMPRESIÓN =====================
@app.on_callback_query(filters.regex("choose_compression"))
@admin_only
async def choose_compression(_, cb):
    await cb.message.edit_text(
        "🎯 **Elige resolución de compresión**\n\n"
        "Luego de elegir, envía el video directamente.\n"
        "El bot detectará que ya elegiste compresión.\n\n"
        "👇 Selecciona:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("360p", callback_data="set_360"),
                InlineKeyboardButton("480p", callback_data="set_480"),
                InlineKeyboardButton("720p", callback_data="set_720")
            ]
        ])
    )

# Diccionario global para almacenar la compresión elegida por usuario
user_compression = {}

@app.on_callback_query(filters.regex(r"set_(360|480|720)"))
@admin_only
async def set_compression(_, cb):
    res = cb.data.split("_")[1]
    user_id = cb.from_user.id
    user_compression[user_id] = res
    
    scale_map = {
        "360": "640:360",
        "480": "854:480",
        "720": "1280:720"
    }
    
    await cb.message.edit_text(
        f"✅ **Compresión {res}p configurada**\n\n"
        f"📐 Resolución: {scale_map[res]}\n"
        f"👤 Usuario: {cb.from_user.first_name}\n\n"
        "📤 **Ahora envía el video**\n"
        "El bot procesará con esta configuración automáticamente."
    )

# ===================== RECEIVE VIDEO (SOLO CON COMPRESIÓN ELEGIDA) =====================
@app.on_message(filters.video | filters.document)
@admin_only
async def receive_video(_, msg):
    user_id = msg.from_user.id
    
    # Verificar si el usuario ya eligió compresión
    if user_id not in user_compression:
        await msg.reply(
            "⚠️ **Primero elige compresión**\n\n"
            "Debes seleccionar la resolución antes de enviar el video.\n"
            "Usa /start para comenzar.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 Elegir Compresión", callback_data="choose_compression")]
            ])
        )
        return
    
    res = user_compression[user_id]
    media = msg.video or msg.document
    input_path = f"{DOWNLOAD_DIR}/{user_id}_{int(time.time())}_{media.file_unique_id}.mp4"
    
    status = await msg.reply(f"📥 **Descargando para {res}p...**\n\n░░░░░░░░░░░░░░░░░░ 0%")
    
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
            f"📥 **Descargando para {res}p...**\n\n{bar} {percent}%"
        )
    
    await app.download_media(
        media,
        file_name=input_path,
        progress=download_progress
    )
    
    # Ahora procedemos a comprimir directamente
    await compress_video(msg, status, input_path, res)

# ===================== FUNCIÓN DE COMPRESIÓN =====================
async def compress_video(msg, status, input_path, res):
    scale_map = {
        "360": "640:360",
        "480": "854:480",
        "720": "1280:720"
    }
    
    scale = scale_map[res]
    output_path = f"{OUTPUT_DIR}/{res}_{msg.from_user.id}_{int(time.time())}.mp4"
    
    try:
        duration = get_video_duration(input_path)
    except:
        await status.edit_text("❌ Error al obtener duración del video")
        clean_files(input_path)
        return
    
    await status.edit_text(
        f"⚙️ **Comprimiendo a {res}p...**\n\n░░░░░░░░░░░░░░░░░░ 0%"
    )
    
    cmd = [
    "ffmpeg", "-y",
    "-i", input_path,
    
    # Video más comprimido
    "-vf", f"scale={scale},fps=23",  # ↓ 16 a 12 FPS
    "-c:v", "libx264",
    "-preset", "ultrafast",  # ↑ ultrafast a slow (más compresión)
    "-crf", "35",  # ↑ 30 a 36 (más compresión)
    "-pix_fmt", "yuv420p",
    "-profile:v", "baseline",
    "-movflags", "+faststart",
    
    # Audio más comprimido
    "-c:a", "aac",
    "-b:a", "60k",  # ↓ 60k a 32k
    "-ac", "1",  # ↓ Estéreo a mono
    
    # Optimizaciones adicionales
    "-threads", "2",  # Menos threads para más compresión
    "-x264-params", "scenecut=0:open_gop=0",  # Optimización
    
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
                    status,
                    f"⚙️ **Comprimiendo a {res}p...**\n\n{bar} {percent}%"
                )
        
        await asyncio.sleep(0.05)
    
    process.wait()
    
    # ===================== UPLOAD =====================
    await status.edit_text(
        f"📤 **Subiendo video {res}p...**\n\n░░░░░░░░░░░░░░░░░░ 0%"
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
            status,
            f"📤 **Subiendo video {res}p...**\n\n{bar} {percent}%"
        )
    
    try:
        await msg.reply_video(
            video=output_path,
            caption=f"✅ **Video comprimido a {res}p**\n\n👤 Enviado por: {msg.from_user.first_name}",
            supports_streaming=True,
            progress=upload_progress
        )
    except Exception as e:
        await status.edit_text(f"❌ Error al subir: {str(e)}")
    
    # Limpiar archivos y opcionalmente resetear compresión
    clean_files(input_path, output_path)
    await status.delete()
    
    # Opcional: mantener la compresión para el usuario o resetear
    # Para resetear: del user_compression[msg.from_user.id]

# ===================== COMANDO PARA CAMBIAR COMPRESIÓN =====================
@app.on_message(filters.command("compression"))
@admin_only
async def change_compression(_, msg):
    await choose_compression(_, msg)

# ===================== MAIN =====================
if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    app.run()
