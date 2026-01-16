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
    workers=4,  # Reducido para Render gratis
    in_memory=True
)

# ===================== UTILS =====================
def progress_bar(percent: int, size: int = 20) -> str:
    filled = int(size * percent / 100)
    return "█" * filled + "░" * (size - filled)

def get_video_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=duration",
        "-of", "json", path
    ]
    result = subprocess.check_output(cmd)
    data = json.loads(result)
    return float(data["streams"][0]["duration"])

def get_video_info(path: str) -> dict:
    """Obtiene información del video para optimización"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,codec_name,duration",
        "-of", "json", path
    ]
    result = subprocess.check_output(cmd)
    data = json.loads(result)["streams"][0]
    
    # Calcular FPS
    num, den = map(int, data["r_frame_rate"].split("/"))
    fps = num / den if den else num
    
    return {
        "width": int(data["width"]),
        "height": int(data["height"]),
        "fps": fps,
        "duration": float(data["duration"])
    }

async def safe_edit(msg, text):
    try:
        await msg.edit_text(text)
    except:
        pass

def clean_files(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

def get_compression_settings(video_info, target_res):
    """Configuración inteligente basada en el video original"""
    original_height = video_info["height"]
    fps = video_info["fps"]
    
    # Mapeo de resoluciones
    scale_map = {
        "360": "640:360",
        "480": "854:480", 
        "720": "1280:720"
    }
    
    # Si el video original es menor a la resolución objetivo, mantener original
    if original_height <= int(target_res):
        scale = f"-2:{target_res}"  # Mantener proporción, altura objetivo
    else:
        scale = scale_map[target_res]
    
    # Ajustar FPS: mantener original si es 24/25/30, sino limitar
    if 23 <= fps <= 30:
        fps_setting = f"fps={fps}"
    else:
        fps_setting = "fps=24"  # Estándar cine
    
    # Configuración por resolución
    settings = {
        "360": {"crf": 24, "preset": "faster", "audio_bitrate": "64k"},
        "480": {"crf": 23, "preset": "fast", "audio_bitrate": "80k"},
        "720": {"crf": 22, "preset": "medium", "audio_bitrate": "96k"}
    }
    
    config = settings[target_res]
    
    return {
        "scale": scale,
        "fps_setting": fps_setting,
        "crf": config["crf"],
        "preset": config["preset"],
        "audio_bitrate": config["audio_bitrate"],
        "threads": 2  # Limitado para Render gratis
    }

# ===================== START =====================
@app.on_message(filters.command("start"))
async def start(_, msg):
    await msg.reply(
        "🎬 **Video Compressor Bot 2026**\n\n"
        "✨ **Optimizado para películas**\n"
        "✔ Preserva calidad cinematográfica\n"
        "✔ FPS original (24/23.976)\n"  
        "✔ Audio optimizado\n\n"
        "📤 Envíame un video",
        quote=True
    )

# ===================== RECEIVE VIDEO =====================
@app.on_message(filters.video | filters.document)
async def receive_video(_, msg):
    media = msg.video or msg.document
    input_path = f"{DOWNLOAD_DIR}/{media.file_unique_id}.mp4"
    
    # Verificar tamaño (4GB límite)
    if media.file_size > 4 * 1024 * 1024 * 1024:
        await msg.reply("❌ El video supera el límite de 4GB")
        return

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
            f"⬇️ Descargando...\n\n{bar} {percent}%\n"
            f"📊 {current // (1024*1024)}MB / {total // (1024*1024)}MB"
        )

    await app.download_media(
        media,
        file_name=input_path,
        progress=download_progress
    )

    await status.edit(
        "✅ Video recibido\n\n"
        "🎬 **Resoluciones disponibles:**\n"
        "• 360p - Compresión rápida\n"
        "• 480p - Balance calidad/velocidad\n"
        "• 720p - Máxima calidad\n\n"
        "📉 Elige resolución:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎥 360p", callback_data=f"360|{input_path}"),
                InlineKeyboardButton("🎬 480p", callback_data=f"480|{input_path}"),
                InlineKeyboardButton("🌟 720p", callback_data=f"720|{input_path}")
            ],
            [
                InlineKeyboardButton("⏹️ Cancelar", callback_data="cancel")
            ]
        ])
    )

# ===================== CALLBACK =====================
@app.on_callback_query()
async def compress_callback(_, cb):
    if cb.data == "cancel":
        await cb.message.delete()
        return
    
    res, input_path = cb.data.split("|")
    
    # Obtener información del video original
    try:
        video_info = get_video_info(input_path)
    except:
        await cb.message.edit_text("❌ Error al analizar el video")
        return
    
    # Configuración inteligente
    settings = get_compression_settings(video_info, res)
    
    output_path = f"{OUTPUT_DIR}/{res}_{os.path.basename(input_path)}"

    await cb.message.edit_text(
        f"⚙️ **Comprimiendo a {res}p**\n\n"
        f"📊 Original: {video_info['width']}x{video_info['height']}@{video_info['fps']:.2f}fps\n"
        f"⚡ Config: CRF {settings['crf']}, {settings['preset']}\n\n"
        "░░░░░░░░░░░░░░░░░░ 0%"
    )

    # 🎬 CONFIGURACIÓN OPTIMIZADA PARA PELÍCULAS
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        
        # ⚡ Limitar recursos (CRÍTICO en Render gratis)
        "-threads", str(settings["threads"]),
        
        # 🎥 Video optimizado para cine
        "-c:v", "libx264",
        "-preset", settings["preset"],
        "-tune", "film",  # ★★ OPTIMIZADO PARA PELÍCULAS ★★
        "-crf", str(settings["crf"]),
        "-profile:v", "high",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        
        # 🔄 Scaling inteligente manteniendo aspecto
        "-vf", f"scale={settings['scale']}:flags=spline,{settings['fps_setting']}",
        
        # 🔊 Audio de calidad para películas
        "-c:a", "aac",
        "-b:a", settings["audio_bitrate"],
        "-ac", "2",
        "-ar", "44100",
        
        # 📊 Progreso
        "-progress", "pipe:1",
        "-nostats",
        
        output_path
    ]

    # Alternativa ULTRA rápida (si se detecta video muy largo > 2 horas)
    if video_info["duration"] > 7200:  # > 2 horas
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-threads", "2",
            "-c:v", "libx264",
            "-preset", "faster",
            "-tune", "film",
            "-crf", "24",
            "-vf", f"scale={settings['scale'].split(':')[0]}:-2:flags=fast_bilinear",
            "-c:a", "aac",
            "-b:a", "64k",
            "-progress", "pipe:1",
            "-nostats",
            output_path
        ]
        await cb.message.edit_text("⏩ **Video largo detectado**\nUsando modo rápido...")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,  # Cambiado para debug
        text=True,
        bufsize=1,
        universal_newlines=True
    )

    time_regex = re.compile(r"out_time_ms=(\d+)")
    speed_regex = re.compile(r"speed=([\d.]+)x")
    last_update = time.time()
    last_percent = 0

    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
            
        match = time_regex.search(line)
        if match:
            current_time = int(match.group(1)) / 1_000_000
            percent = min(99, int(current_time * 100 / video_info["duration"]))
            
            # Solo actualizar si hay cambio significativo o cada 2 segundos
            if percent > last_percent or time.time() - last_update >= 2:
                last_update = time.time()
                last_percent = percent
                bar = progress_bar(percent)
                
                # Verificar velocidad
                speed_match = speed_regex.search(line)
                speed = speed_match.group(1) if speed_match else "?"
                
                await safe_edit(
                    cb.message,
                    f"⚙️ **Comprimiendo a {res}p**\n\n"
                    f"{bar} {percent}%\n"
                    f"⏱️ {int(current_time//60)}:{int(current_time%60):02d} / "
                    f"{int(video_info['duration']//60)}:{int(video_info['duration']%60):02d}\n"
                    f"⚡ Velocidad: {speed}x"
                )

        await asyncio.sleep(0.1)

    # Verificar éxito
    if process.returncode != 0:
        error = process.stderr.read()[-500:] if process.stderr else "Error desconocido"
        await cb.message.edit_text(f"❌ **Error en compresión:**\n```{error}```")
        clean_files(input_path, output_path)
        return

    # ===================== UPLOAD =====================
    if not os.path.exists(output_path):
        await cb.message.edit_text("❌ Error: Archivo de salida no generado")
        clean_files(input_path)
        return

    output_size = os.path.getsize(output_path) / (1024*1024)
    compression_ratio = (1 - output_size / (media.file_size/(1024*1024))) * 100
    
    await cb.message.edit_text(
        f"✅ **Compresión completada**\n\n"
        f"📊 Tamaño final: {output_size:.1f}MB\n"
        f"📉 Reducción: {compression_ratio:.1f}%\n\n"
        "⬆️ Subiendo video...\n"
        "░░░░░░░░░░░░░░░░░░ 0%"
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
            f"⬆️ Subiendo video...\n\n{bar} {percent}%\n"
            f"📤 {current // (1024*1024)}MB / {total // (1024*1024)}MB"
        )

    try:
        await cb.message.reply_video(
            video=output_path,
            supports_streaming=True,
            progress=upload_progress,
            caption=f"🎬 Compresión {res}p\n"
                   f"📊 {output_size:.1f}MB | {int(video_info['duration']//60)}min"
        )
    except Exception as e:
        await cb.message.edit_text(f"❌ Error al subir: {str(e)}")
    finally:
        clean_files(input_path, output_path)
        await cb.message.delete()

# ===================== MAIN =====================
if __name__ == "__main__":
    # Configurar prioridad baja para Render
    try:
        os.nice(10)
    except:
        pass
    
    threading.Thread(target=run_web, daemon=True).start()
    
    print("✅ Bot iniciado en modo optimizado para películas")
    app.run()
