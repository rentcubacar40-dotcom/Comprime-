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

# Configuraciones de compresión optimizadas para velocidad
COMPRESSION_PROFILES = {
    "360": {
        "scale": "640:360",
        "crf": 28,  # Un poco menos compresión para más velocidad
        "preset": "veryfast",  # Más rápido que 'ultrafast' pero con mejor compresión
        "bitrate": "96k"
    },
    "480": {
        "scale": "854:480",
        "crf": 26,
        "preset": "veryfast",
        "bitrate": "128k"
    },
    "720": {
        "scale": "1280:720",
        "crf": 24,
        "preset": "fast",  # Balance entre velocidad y calidad
        "bitrate": "160k"
    }
}

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

def get_optimal_threads():
    """Obtiene el número óptimo de threads para FFmpeg"""
    try:
        cpu_count = os.cpu_count()
        return min(cpu_count, 4) if cpu_count else 2  # Limitar a 4 threads máximo
    except:
        return 2

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
        "✔ Compresión RÁPIDA optimizada\n"
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
        "⚡ **Perfiles optimizados para velocidad:**\n"
        "• 360p: Compresión muy rápida\n"
        "• 480p: Balance velocidad/calidad\n"
        "• 720p: Calidad buena y rápida\n\n"
        "Luego de elegir, envía el video directamente.\n"
        "El bot detectará que ya elegiste compresión.\n\n"
        "👇 Selecciona:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("360p ⚡", callback_data="set_360"),
                InlineKeyboardButton("480p ⚡", callback_data="set_480"),
                InlineKeyboardButton("720p ⚡", callback_data="set_720")
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
    
    profile = COMPRESSION_PROFILES[res]
    
    await cb.message.edit_text(
        f"✅ **Compresión {res}p configurada**\n\n"
        f"📐 Resolución: {profile['scale']}\n"
        f"⚡ Preset: {profile['preset']}\n"
        f"🎚️ Calidad: CRF {profile['crf']}\n"
        f"👤 Usuario: {cb.from_user.first_name}\n\n"
        "📤 **Ahora envía el video**\n"
        "El bot procesará con esta configuración optimizada."
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

# ===================== FUNCIÓN DE COMPRESIÓN OPTIMIZADA =====================
async def compress_video(msg, status, input_path, res):
    profile = COMPRESSION_PROFILES[res]
    
    scale_filter = f"scale={profile['scale']}:flags=lanczos"
    output_path = f"{OUTPUT_DIR}/{res}_{msg.from_user.id}_{int(time.time())}.mp4"
    threads = get_optimal_threads()
    
    try:
        duration = get_video_duration(input_path)
    except:
        await status.edit_text("❌ Error al obtener duración del video")
        clean_files(input_path)
        return
    
    await status.edit_text(
        f"⚙️ **Comprimiendo a {res}p (optimizado)...**\n\n░░░░░░░░░░░░░░░░░░ 0%"
    )
    
    # Configuración FFmpeg optimizada para velocidad
    cmd = [
        'ffmpeg',
        '-y',  # Sobrescribir automáticamente
        '-i', input_path,
        '-vf', scale_filter,
        '-c:v', 'libx265',  # H.265 para mejor compresión
        '-crf', str(profile['crf']),
        '-preset', profile['preset'],  # Optimizado para velocidad
        '-tag:v', 'hvc1',  # Mejor compatibilidad H.265
        '-c:a', 'aac',  # Mantener audio sin procesar mucho
        '-b:a', profile['bitrate'],
        '-movflags', '+faststart',  # Para streaming rápido
        '-threads', str(threads),  # Usar threads optimizados
        '-x265-params', 'no-sao=1:strong-intra-smoothing=0',  # Optimizaciones H.265
        '-progress', 'pipe:1',
        '-nostats',
        output_path
    ]
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True
    )
    
    time_regex = re.compile(r"out_time_ms=(\d+)")
    last_update = time.time()
    
    # Leer progreso en tiempo real
    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        
        match = time_regex.search(line)
        if match:
            current_time = int(match.group(1)) / 1_000_000
            percent = min(99, int(current_time * 100 / duration))
            if time.time() - last_update >= 0.5:  # Actualizar más frecuente
                last_update = time.time()
                bar = progress_bar(percent)
                await safe_edit(
                    status,
                    f"⚙️ **Comprimiendo a {res}p (optimizado)...**\n\n{bar} {percent}%\n"
                    f"⚡ Usando {threads} threads | Preset: {profile['preset']}"
                )
        
        await asyncio.sleep(0.01)
    
    process.wait()
    
    # Verificar si el archivo fue creado
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        await status.edit_text(f"❌ Error en la compresión\n\nFFmpeg falló al crear el archivo.")
        clean_files(input_path, output_path)
        return
    
    # ===================== UPLOAD OPTIMIZADO =====================
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
            f"📤 **Subiendo video {res}p...**\n\n{bar} {percent}%\n"
            f"📊 Tamaño: {os.path.getsize(output_path) // 1024 // 1024}MB"
        )
    
    try:
        await msg.reply_video(
            video=output_path,
            caption=f"✅ **Video comprimido a {res}p**\n\n"
                   f"👤 Enviado por: {msg.from_user.first_name}\n"
                   f"⚡ Procesado con H.265 (optimizado)",
            supports_streaming=True,
            progress=upload_progress
        )
    except Exception as e:
        await status.edit_text(f"❌ Error al subir: {str(e)}")
    
    # Limpiar archivos
    clean_files(input_path, output_path)
    await status.delete()
    
    # Opcional: mantener la compresión para el usuario o resetear
    # Para resetear: del user_compression[msg.from_user.id]

# ===================== COMANDO PARA CAMBIAR COMPRESIÓN =====================
@app.on_message(filters.command("compression"))
@admin_only
async def change_compression(_, msg):
    await choose_compression(_, msg)

# ===================== COMANDO PARA INFO =====================
@app.on_message(filters.command("info"))
@admin_only
async def info_command(_, msg):
    user_id = msg.from_user.id
    current_res = user_compression.get(user_id, "No configurada")
    
    info_text = f"📊 **Información del Bot**\n\n"
    info_text += f"👤 Tu compresión actual: **{current_res}p**\n"
    info_text += f"📂 Descargas activas: {len([f for f in os.listdir(DOWNLOAD_DIR) if f.endswith('.mp4')])}\n"
    info_text += f"📤 Procesados: {len([f for f in os.listdir(OUTPUT_DIR) if f.endswith('.mp4')])}\n\n"
    info_text += "⚡ **Configuraciones optimizadas:**\n"
    
    for res, profile in COMPRESSION_PROFILES.items():
        info_text += f"• {res}p: CRF {profile['crf']}, Preset {profile['preset']}\n"
    
    await msg.reply(info_text)

# ===================== MAIN =====================
if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    app.run()
