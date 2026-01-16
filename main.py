import os
import asyncio
import threading
import time
import math
import subprocess
import json
import re
from datetime import timedelta

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
    workers=4,
    in_memory=True
)

# ===================== UTILS =====================
def progress_bar(percent: int, size: int = 20) -> str:
    filled = int(size * percent / 100)
    return "█" * filled + "░" * (size - filled)

def format_time(seconds: float) -> str:
    """Formatea segundos a MM:SS o HH:MM:SS"""
    if seconds < 3600:
        return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"
    else:
        return str(timedelta(seconds=int(seconds)))

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
    
    # Mapeo de resoluciones
    scale_map = {
        "360": "640:360",
        "480": "854:480", 
        "720": "1280:720"
    }
    
    # Si el video original es menor a la resolución objetivo, mantener original
    if original_height <= int(target_res):
        scale = f"-2:{target_res}"
    else:
        scale = scale_map[target_res]
    
    # Ajustar FPS: mantener original si es cine, sino limitar
    fps = video_info["fps"]
    if 23 <= fps <= 30:
        fps_setting = f"fps={fps}"
    elif fps > 30:
        fps_setting = "fps=24"
    else:
        fps_setting = "fps=23.976"
    
    # Configuración por resolución (optimizada para Render gratis)
    settings = {
        "360": {"crf": 24, "preset": "faster", "audio_bitrate": "64k", "threads": 2},
        "480": {"crf": 23, "preset": "fast", "audio_bitrate": "80k", "threads": 2},
        "720": {"crf": 22, "preset": "medium", "audio_bitrate": "96k", "threads": 1}
    }
    
    config = settings[target_res]
    
    return {
        "scale": scale,
        "fps_setting": fps_setting,
        "crf": config["crf"],
        "preset": config["preset"],
        "audio_bitrate": config["audio_bitrate"],
        "threads": config["threads"]
    }

def format_file_size(size_bytes):
    """Formatea bytes a KB, MB, GB"""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

# ===================== START =====================
@app.on_message(filters.command("start"))
async def start(_, msg):
    await msg.reply(
        "🎬 **Video Compressor Bot 2026**\n\n"
        "✨ **Optimizado para Render Gratis**\n"
        "✔ Progreso en tiempo real\n"
        "✔ Tiempo estimado mostrado\n"
        "✔ Optimizado para películas\n\n"
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
    start_time = time.time()

    async def download_progress(current, total):
        nonlocal last_update, start_time
        if total == 0:
            return
        
        current_time = time.time()
        if current_time - last_update < 1:
            return
        
        last_update = current_time
        elapsed = current_time - start_time
        
        percent = int(current * 100 / total)
        bar = progress_bar(percent)
        
        # Calcular velocidad y tiempo estimado
        if percent > 0:
            speed = current / elapsed  # bytes por segundo
            if speed > 0:
                remaining = (total - current) / speed
                eta = format_time(remaining)
            else:
                eta = "--:--"
            
            speed_mb = speed / (1024 * 1024)
            
            await safe_edit(
                status,
                f"⬇️ **Descargando...**\n\n"
                f"{bar} {percent}%\n"
                f"📊 {format_file_size(current)} / {format_file_size(total)}\n"
                f"⚡ {speed_mb:.1f} MB/s\n"
                f"⏱️ ETA: {eta}"
            )
        else:
            await safe_edit(
                status,
                f"⬇️ **Descargando...**\n\n"
                f"{bar} {percent}%\n"
                f"📊 {format_file_size(current)} / {format_file_size(total)}"
            )

    await app.download_media(
        media,
        file_name=input_path,
        progress=download_progress
    )

    # Obtener información del video
    try:
        video_info = get_video_info(input_path)
        duration_str = format_time(video_info["duration"])
        await status.edit(
        f"✅ **Video recibido**\n\n"
        f"📊 **Información:**\n"
        f"• Resolución: {video_info['width']}x{video_info['height']}\n"
        f"• FPS: {video_info['fps']:.2f}\n"
        f"• Duración: {duration_str}\n"
        f"• Tamaño: {format_file_size(media.file_size)}\n\n"
        f"🎬 **Elige resolución:**",
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
    except Exception as e:
        await status.edit(f"❌ Error al analizar video: {str(e)}")
        clean_files(input_path)
        return

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
    except Exception as e:
        await cb.message.edit_text(f"❌ Error al analizar video: {str(e)}")
        clean_files(input_path)
        return
    
    # Configuración inteligente
    settings = get_compression_settings(video_info, res)
    
    output_path = f"{OUTPUT_DIR}/{res}_{os.path.basename(input_path)}"
    original_size = os.path.getsize(input_path)
    
    # Calcular tiempo estimado (aproximado)
    duration_minutes = video_info["duration"] / 60
    est_time_minutes = duration_minutes * 2.5  # Estimación conservadora
    
    await cb.message.edit_text(
        f"⚙️ **Iniciando compresión a {res}p**\n\n"
        f"📊 **Configuración:**\n"
        f"• CRF: {settings['crf']}\n"
        f"• Preset: {settings['preset']}\n"
        f"• Threads: {settings['threads']}\n"
        f"• Audio: {settings['audio_bitrate']}\n\n"
        f"⏱️ **Tiempo estimado:** ~{int(est_time_minutes)} minutos\n\n"
        f"░░░░░░░░░░░░░░░░░░ 0%"
    )

    # 🎬 CONFIGURACIÓN OPTIMIZADA
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        
        # ⚡ Limitar recursos para Render Gratis
        "-threads", str(settings["threads"]),
        
        # 🎥 Video optimizado
        "-c:v", "libx264",
        "-preset", settings["preset"],
        "-tune", "film",
        "-crf", str(settings["crf"]),
        "-profile:v", "high",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        
        # 🔄 Scaling y FPS
        "-vf", f"scale={settings['scale']}:flags=spline,{settings['fps_setting']}",
        
        # 🔊 Audio
        "-c:a", "aac",
        "-b:a", settings["audio_bitrate"],
        "-ac", "2",
        "-ar", "44100",
        
        # 📊 Progreso
        "-progress", "pipe:1",
        "-nostats",
        
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
    speed_regex = re.compile(r"speed=([\d.]+)x")
    bitrate_regex = re.compile(r"bitrate=([\d.]+)kbits/s")
    
    last_update = time.time()
    last_percent = 0
    last_speed = "0.00"
    start_time = time.time()
    last_bitrate = "0"
    estimated_total_time = None

    try:
        while True:
            line = process.stdout.readline()
            
            # Si el proceso terminó
            if line == '' and process.poll() is not None:
                process.wait()
                
                # Mostrar 100% completado
                bar = progress_bar(100)
                total_elapsed = time.time() - start_time
                
                await safe_edit(
                    cb.message,
                    f"✅ **Compresión completada!**\n\n"
                    f"{bar} 100%\n"
                    f"⏱️ Tiempo total: {format_time(total_elapsed)}\n"
                    f"⚡ Velocidad promedio: {last_speed}x\n"
                    f"📊 Procesando archivo final..."
                )
                break
                
            if not line:
                await asyncio.sleep(0.05)
                continue
            
            # Extraer información de progreso
            time_match = time_regex.search(line)
            speed_match = speed_regex.search(line)
            bitrate_match = bitrate_regex.search(line)
            
            if time_match:
                current_time = int(time_match.group(1)) / 1_000_000
                percent = min(99, int(current_time * 100 / video_info["duration"]))
                
                if speed_match:
                    last_speed = speed_match.group(1)
                
                if bitrate_match:
                    last_bitrate = bitrate_match.group(1)
                
                # Calcular tiempo transcurrido y estimado
                elapsed = time.time() - start_time
                
                # Calcular ETA solo si tenemos velocidad
                if percent > 5 and float(last_speed) > 0:
                    estimated_total = elapsed / (percent / 100)
                    remaining = estimated_total - elapsed
                    eta_str = format_time(remaining)
                    estimated_total_time = estimated_total
                else:
                    eta_str = "Calculando..."
                    remaining = 0
                
                # Actualizar cada 1% de progreso o cada 2 segundos
                if percent > last_percent or time.time() - last_update >= 2:
                    last_update = time.time()
                    last_percent = percent
                    bar = progress_bar(percent)
                    
                    # Calcular compresión aproximada
                    if percent > 10 and estimated_total_time:
                        compression_ratio = (elapsed / estimated_total_time) * 100
                        compression_str = f"{compression_ratio:.0f}%"
                    else:
                        compression_str = "--%"
                    
                    await safe_edit(
                        cb.message,
                        f"⚙️ **Comprimiendo a {res}p**\n\n"
                        f"{bar} {percent}%\n"
                        f"⏱️ {format_time(current_time)} / {format_time(video_info['duration'])}\n"
                        f"⚡ Velocidad: {last_speed}x\n"
                        f"📶 Bitrate: {last_bitrate} kb/s\n"
                        f"⏳ Tiempo transcurrido: {format_time(elapsed)}\n"
                        f"🎯 ETA: {eta_str}"
                    )
            
            await asyncio.sleep(0.05)
            
    except Exception as e:
        print(f"Error en compresión: {e}")
        await cb.message.edit_text(f"❌ Error durante compresión: {str(e)}")
        clean_files(input_path, output_path)
        return
    
    # Verificar que el archivo se creó
    if not os.path.exists(output_path):
        await cb.message.edit_text("❌ Error: No se generó el archivo de salida")
        clean_files(input_path)
        return
    
    output_size = os.path.getsize(output_path)
    total_time = time.time() - start_time
    compression_ratio = (1 - output_size / original_size) * 100
    
    # ===================== UPLOAD =====================
    await cb.message.edit_text(
        f"✅ **Compresión exitosa!**\n\n"
        f"📊 **Resultados:**\n"
        f"• Tamaño original: {format_file_size(original_size)}\n"
        f"• Tamaño final: {format_file_size(output_size)}\n"
        f"• Reducción: {compression_ratio:.1f}%\n"
        f"• Tiempo total: {format_time(total_time)}\n\n"
        f"⬆️ **Subiendo video...**\n"
        f"░░░░░░░░░░░░░░░░░░ 0%"
    )

    last_update = time.time()
    upload_start = time.time()

    async def upload_progress(current, total):
        nonlocal last_update, upload_start
        if total == 0:
            return
        
        current_time = time.time()
        if current_time - last_update < 1:
            return
        
        last_update = current_time
        elapsed = current_time - upload_start
        
        percent = int(current * 100 / total)
        bar = progress_bar(percent)
        
        # Calcular velocidad de subida
        if percent > 0:
            speed = current / elapsed
            speed_mb = speed / (1024 * 1024)
            
            if speed > 0:
                remaining = (total - current) / speed
                eta = format_time(remaining)
            else:
                eta = "--:--"
            
            await safe_edit(
                cb.message,
                f"⬆️ **Subiendo video...**\n\n"
                f"{bar} {percent}%\n"
                f"📊 {format_file_size(current)} / {format_file_size(total)}\n"
                f"⚡ {speed_mb:.1f} MB/s\n"
                f"⏱️ ETA: {eta}"
            )
    
    try:
        await cb.message.reply_video(
            video=output_path,
            supports_streaming=True,
            progress=upload_progress,
            caption=f"🎬 **Video comprimido a {res}p**\n"
                   f"📊 {format_file_size(output_size)} | {format_time(video_info['duration'])}\n"
                   f"⚡ Compresión: {compression_ratio:.1f}% más pequeño"
        )
    except Exception as e:
        await cb.message.edit_text(f"❌ Error al subir: {str(e)}")
    finally:
        clean_files(input_path, output_path)
        await cb.message.delete()

# ===================== COMMANDS =====================
@app.on_message(filters.command("info"))
async def bot_info(_, msg):
    await msg.reply(
        "🤖 **Video Compressor Bot**\n\n"
        "✨ **Características:**\n"
        "• Progreso en tiempo real\n"
        "• Tiempo estimado (ETA)\n"
        "• Optimizado para Render Gratis\n"
        "• Preserva calidad de películas\n"
        "• Límite: 4GB por video\n\n"
        "📞 **Soporte:** @TuUsuario"
    )

@app.on_message(filters.command("clean"))
async def clean_cache(_, msg):
    import shutil
    try:
        if os.path.exists(DOWNLOAD_DIR):
            shutil.rmtree(DOWNLOAD_DIR)
        if os.path.exists(OUTPUT_DIR):
            shutil.rmtree(OUTPUT_DIR)
        
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        
        await msg.reply("✅ Caché limpiado correctamente")
    except Exception as e:
        await msg.reply(f"❌ Error al limpiar: {str(e)}")

# ===================== ERROR HANDLER =====================
@app.on_message(filters.command("help"))
async def help_command(_, msg):
    await msg.reply(
        "📚 **Ayuda del Bot**\n\n"
        "**Comandos disponibles:**\n"
        "• /start - Iniciar bot\n"
        "• /info - Información del bot\n"
        "• /clean - Limpiar caché\n"
        "• /help - Esta ayuda\n\n"
        "**Cómo usar:**\n"
        "1. Envía un video (máx. 4GB)\n"
        "2. Elige resolución (360p, 480p, 720p)\n"
        "3. Espera la compresión\n"
        "4. Recibe el video comprimido\n\n"
        "⚠️ **Nota:** En Render Gratis, videos grandes pueden tardar varias horas."
    )

# ===================== MAIN =====================
if __name__ == "__main__":
    # Configurar prioridad baja para Render
    try:
        os.nice(10)
    except:
        pass
    
    print("=" * 50)
    print("🎬 Video Compressor Bot 2026")
    print("✨ Optimizado para Render Gratis")
    print(f"📁 Descargas: {DOWNLOAD_DIR}")
    print(f"📁 Salida: {OUTPUT_DIR}")
    print("=" * 50)
    
    threading.Thread(target=run_web, daemon=True).start()
    
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n👋 Bot detenido por el usuario")
    except Exception as e:
        print(f"❌ Error al iniciar bot: {e}")
