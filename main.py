import os
import asyncio
import threading
import time
import math
import subprocess
import json
import re
from concurrent.futures import ThreadPoolExecutor

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ===================== CONFIG =====================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "7363341763").split(",") if x.strip()]
PORT = int(os.getenv("PORT", 10000))

DOWNLOAD_DIR = "downloads"
OUTPUT_DIR = "output"
MAX_WORKERS = 4  # Número máximo de procesos paralelos

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===================== WEB (Render needs PORT) =====================
web = Flask(__name__)

@web.route("/")
def home():
    return "Telegram Video Compressor Bot running 2026"

def run_web():
    web.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

# ===================== BOT INIT =====================
app = Client(
    name="video-compressor",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=4,
    in_memory=True,
    max_concurrent_transmissions=2  # Limitar transmisiones concurrentes
)

# Pool de threads para operaciones de CPU
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ===================== UTILS =====================
def progress_bar(percent: int, size: int = 20) -> str:
    filled = int(size * percent / 100)
    return "█" * filled + "░" * (size - filled)

def get_video_duration(path: str) -> float:
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "json", path
        ]
        result = subprocess.check_output(cmd, timeout=10)
        data = json.loads(result)
        return float(data["format"]["duration"])
    except Exception as e:
        print(f"Error obteniendo duración: {e}")
        return 0

async def safe_edit(msg, text):
    try:
        await msg.edit_text(text[:4096])
    except Exception as e:
        print(f"Error editando mensaje: {e}")

def clean_files(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

def optimize_video_parameters(original_size, target_res):
    """Calcula parámetros óptimos basados en tamaño y resolución"""
    if original_size > 100 * 1024 * 1024:  # > 100MB
        return {
            'crf': 28,
            'preset': 'medium',
            'audio_bitrate': '64k',
            'threads': 2
        }
    elif original_size > 50 * 1024 * 1024:  # > 50MB
        return {
            'crf': 26,
            'preset': 'fast',
            'audio_bitrate': '96k',
            'threads': 4
        }
    else:
        return {
            'crf': 24,
            'preset': 'veryfast',
            'audio_bitrate': '128k',
            'threads': 8
        }

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
        "✔ 360p / 480p / 720p\n"
        "✔ Optimizado para velocidad\n\n"
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
            ],
            [InlineKeyboardButton("✨ Optimizado (Auto)", callback_data="set_auto")]
        ])
    )

# Diccionario global para almacenar la compresión elegida por usuario
user_compression = {}
user_last_active = {}

@app.on_callback_query(filters.regex(r"set_(360|480|720|auto)"))
@admin_only
async def set_compression(_, cb):
    res = cb.data.split("_")[1]
    user_id = cb.from_user.id
    user_compression[user_id] = res
    user_last_active[user_id] = time.time()
    
    scale_map = {
        "360": "640:360",
        "480": "854:480",
        "720": "1280:720",
        "auto": "auto"
    }
    
    res_text = f"{res}p" if res != "auto" else "Optimizado (Auto)"
    
    await cb.message.edit_text(
        f"✅ **Compresión {res_text} configurada**\n\n"
        f"📐 Resolución: {scale_map[res]}\n"
        f"👤 Usuario: {cb.from_user.first_name}\n\n"
        "📤 **Ahora envía el video**\n"
        "El bot procesará con esta configuración automáticamente."
    )

# ===================== RECEIVE VIDEO (SOLO CON COMPRESIÓN ELEGIDA) =====================
@app.on_message(filters.video | filters.document)
@admin_only
async def receive_video(client, msg):
    user_id = msg.from_user.id
    
    # Limpiar configuraciones antiguas (> 1 hora)
    current_time = time.time()
    to_remove = [uid for uid, last_time in user_last_active.items() 
                if current_time - last_time > 3600]
    for uid in to_remove:
        user_compression.pop(uid, None)
        user_last_active.pop(uid, None)
    
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
    
    # Obtener tamaño del archivo para optimización
    file_size = media.file_size if hasattr(media, 'file_size') else 0
    
    # Crear nombre único para el archivo
    timestamp = int(time.time())
    input_path = f"{DOWNLOAD_DIR}/{user_id}_{timestamp}_{media.file_unique_id}.mp4"
    
    status = await msg.reply(f"📥 **Descargando video...**\n\n░░░░░░░░░░░░░░░░░░ 0%")
    
    last_update = time.time()
    last_percent = 0
    
    async def download_progress(current, total):
        nonlocal last_update, last_percent
        if total == 0:
            return
        
        current_time = time.time()
        percent = int(current * 100 / total)
        
        # Actualizar solo cada 0.5 segundos o si hay cambio significativo
        if current_time - last_update < 0.5 and abs(percent - last_percent) < 5:
            return
        
        last_update = current_time
        last_percent = percent
        bar = progress_bar(percent)
        
        speed = current / (current_time - start_time) if current_time > start_time else 0
        speed_mb = speed / (1024 * 1024)
        
        await safe_edit(
            status,
            f"📥 **Descargando video...**\n\n"
            f"{bar} {percent}%\n"
            f"📊 Velocidad: {speed_mb:.1f} MB/s"
        )
    
    start_time = time.time()
    
    try:
        # Descargar con timeout
        await asyncio.wait_for(
            client.download_media(
                media,
                file_name=input_path,
                progress=download_progress
            ),
            timeout=300  # 5 minutos timeout
        )
        
        # Verificar que el archivo se descargó correctamente
        if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
            await status.edit_text("❌ Error: Archivo descargado vacío o corrupto")
            clean_files(input_path)
            return
        
        await safe_edit(status, f"✅ **Descarga completada**\n\nPreparando compresión {res}p...")
        
        # Proceder a comprimir
        await compress_video(client, msg, status, input_path, res, file_size)
        
    except asyncio.TimeoutError:
        await status.edit_text("❌ **Timeout**: La descarga tardó demasiado")
        clean_files(input_path)
    except Exception as e:
        await status.edit_text(f"❌ **Error en descarga**: {str(e)[:100]}")
        clean_files(input_path)
        print(f"Error descargando: {e}")

# ===================== FUNCIÓN DE COMPRESIÓN OPTIMIZADA =====================
async def compress_video(client, msg, status, input_path, res, original_size):
    try:
        # Determinar resolución automáticamente si es 'auto'
        if res == "auto":
            try:
                cmd_probe = [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0", input_path
                ]
                result = subprocess.check_output(cmd_probe, timeout=5).decode().strip()
                if result:
                    width, height = map(int, result.split(','))
                    if width <= 640 or height <= 360:
                        res = "360"
                    elif width <= 854 or height <= 480:
                        res = "480"
                    else:
                        res = "720"
                else:
                    res = "480"  # Por defecto
            except:
                res = "480"
        
        scale_map = {
            "360": "640:360",
            "480": "854:480",
            "720": "1280:720"
        }
        
        scale = scale_map[res]
        
        # Obtener parámetros óptimos
        params = optimize_video_parameters(original_size, res)
        
        # Crear nombre de salida
        timestamp = int(time.time())
        output_path = f"{OUTPUT_DIR}/{res}p_{msg.from_user.id}_{timestamp}.mp4"
        
        # Obtener duración
        duration = await asyncio.get_event_loop().run_in_executor(
            executor, get_video_duration, input_path
        )
        
        if duration == 0:
            await status.edit_text("❌ Error: No se pudo obtener la duración del video")
            clean_files(input_path)
            return
        
        await safe_edit(
            status,
            f"⚙️ **Comprimiendo a {res}p...**\n\n"
            f"🔄 Preset: {params['preset']}\n"
            f"🎚️ CRF: {params['crf']}\n"
            f"📊 Tamaño original: {original_size/(1024*1024):.1f} MB\n\n"
            f"░░░░░░░░░░░░░░░░░░ 0%"
        )
        
        # Comando FFmpeg optimizado
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", f"scale={scale}:force_original_aspect_ratio=decrease",
            "-c:v", "libx264",
            "-preset", params['preset'],
            "-crf", str(params['crf']),
            "-maxrate", "2000k",
            "-bufsize", "4000k",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac",
            "-b:a", params['audio_bitrate'],
            "-threads", str(params['threads']),
            "-progress", "pipe:1",
            "-nostats",
            "-loglevel", "error",
            output_path
        ]
        
        # Iniciar proceso
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        
        time_regex = re.compile(r"out_time_ms=(\d+)")
        last_update = time.time()
        last_percent = 0
        
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=0.1)
                if not line:
                    break
                    
                line = line.decode()
                match = time_regex.search(line)
                if match:
                    current_time = int(match.group(1)) / 1_000_000
                    percent = min(100, int(current_time * 100 / duration))
                    
                    current_time_now = time.time()
                    if current_time_now - last_update >= 1 or abs(percent - last_percent) >= 5:
                        last_update = current_time_now
                        last_percent = percent
                        bar = progress_bar(percent)
                        
                        # Calcular ETA
                        if percent > 0:
                            elapsed = current_time_now - start_time
                            eta = (elapsed / percent) * (100 - percent)
                            eta_str = f"{int(eta//60)}m {int(eta%60)}s"
                        else:
                            eta_str = "calculando..."
                        
                        await safe_edit(
                            status,
                            f"⚙️ **Comprimiendo a {res}p...**\n\n"
                            f"{bar} {percent}%\n"
                            f"⏱️ ETA: {eta_str}"
                        )
                        
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
        
        # Esperar a que termine el proceso
        await process.wait()
        
        if process.returncode != 0:
            await status.edit_text("❌ Error durante la compresión")
            clean_files(input_path, output_path)
            return
        
        # Verificar que el archivo de salida existe
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            await status.edit_text("❌ Error: Archivo de salida vacío")
            clean_files(input_path, output_path)
            return
        
        output_size = os.path.getsize(output_path)
        compression_ratio = (1 - (output_size / original_size)) * 100 if original_size > 0 else 0
        
        await safe_edit(
            status,
            f"✅ **Compresión completada!**\n\n"
            f"📊 Reducción: {compression_ratio:.1f}%\n"
            f"📁 Tamaño final: {output_size/(1024*1024):.1f} MB\n\n"
            f"📤 **Subiendo video...**\n"
            f"░░░░░░░░░░░░░░░░░░ 0%"
        )
        
        # Subir el video
        last_update = time.time()
        last_percent = 0
        
        async def upload_progress(current, total):
            nonlocal last_update, last_percent
            if total == 0:
                return
            
            current_time = time.time()
            percent = int(current * 100 / total)
            
            if current_time - last_update < 1 and abs(percent - last_percent) < 5:
                return
            
            last_update = current_time
            last_percent = percent
            bar = progress_bar(percent)
            
            await safe_edit(
                status,
                f"📤 **Subiendo video {res}p...**\n\n"
                f"{bar} {percent}%\n"
                f"📊 {current/(1024*1024):.1f}/{total/(1024*1024):.1f} MB"
            )
        
        try:
            await msg.reply_video(
                video=output_path,
                caption=(
                    f"✅ **Video comprimido a {res}p**\n\n"
                    f"👤 Enviado por: {msg.from_user.first_name}\n"
                    f"📊 Reducción: {compression_ratio:.1f}%\n"
                    f"📁 Tamaño final: {output_size/(1024*1024):.1f} MB"
                ),
                supports_streaming=True,
                progress=upload_progress,
                thumb=f"{DOWNLOAD_DIR}/thumb_{msg.from_user.id}.jpg" 
                if os.path.exists(f"{DOWNLOAD_DIR}/thumb_{msg.from_user.id}.jpg") 
                else None
            )
            
            await safe_edit(status, f"✅ **Video enviado exitosamente!**")
            
        except Exception as e:
            await status.edit_text(f"❌ Error al subir: {str(e)[:100]}")
        finally:
            # Limpiar archivos
            clean_files(input_path, output_path)
            
    except Exception as e:
        await status.edit_text(f"❌ Error en compresión: {str(e)[:100]}")
        clean_files(input_path, output_path if 'output_path' in locals() else None)
        print(f"Error en compresión: {e}")

# ===================== COMANDO PARA CAMBIAR COMPRESIÓN =====================
@app.on_message(filters.command("compression"))
@admin_only
async def change_compression(_, msg):
    await choose_compression(_, msg)

# ===================== COMANDO PARA ESTADO =====================
@app.on_message(filters.command("status"))
@admin_only
async def bot_status(_, msg):
    import psutil
    import shutil
    
    cpu_percent = psutil.cpu_percent()
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(".")
    
    active_users = len([uid for uid, last_time in user_last_active.items() 
                       if time.time() - last_time < 300])  # Últimos 5 minutos
    
    status_text = (
        f"🤖 **Estado del Bot**\n\n"
        f"🖥️ CPU: {cpu_percent}%\n"
        f"💾 RAM: {memory.percent}%\n"
        f"💿 Disco: {disk.used/(1024**3):.1f}/{disk.total/(1024**3):.1f} GB\n"
        f"👥 Usuarios activos: {active_users}\n"
        f"⚙️ Workers: {MAX_WORKERS}\n"
        f"⏰ Hora: {time.strftime('%H:%M:%S')}"
    )
    
    await msg.reply(status_text)

# ===================== CLEANUP TASK =====================
async def cleanup_task():
    """Tarea periódica para limpiar archivos temporales"""
    while True:
        try:
            current_time = time.time()
            # Limpiar archivos temporales > 1 hora
            for dir_path in [DOWNLOAD_DIR, OUTPUT_DIR]:
                if os.path.exists(dir_path):
                    for filename in os.listdir(dir_path):
                        file_path = os.path.join(dir_path, filename)
                        try:
                            if os.path.getmtime(file_path) < current_time - 3600:
                                os.remove(file_path)
                        except:
                            pass
        except Exception as e:
            print(f"Error en cleanup: {e}")
        
        await asyncio.sleep(1800)  # Cada 30 minutos

# ===================== MAIN =====================
async def main():
    # Iniciar tarea de limpieza
    asyncio.create_task(cleanup_task())
    
    # Iniciar web en segundo plano
    threading.Thread(target=run_web, daemon=True).start()
    
    # Iniciar bot
    print("🚀 Iniciando Video Compressor Bot 2026...")
    await app.start()
    
    # Obtener información del bot
    me = await app.get_me()
    print(f"✅ Bot iniciado como: {me.first_name}")
    print(f"📞 ID: {me.id}")
    print(f"🔗 @{me.username}")
    print(f"👥 Admins: {ADMIN_IDS}")
    
    # Mantener el bot corriendo
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot detenido")
    except Exception as e:
        print(f"❌ Error fatal: {e}")
    finally:
        # Limpiar executor
        executor.shutdown(wait=True)
        print("✅ Recursos liberados")
