import os
import asyncio
import threading
import time
import math
import subprocess
import json
import re
import sys
import psutil
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from pathlib import Path

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
TEMP_DIR = "/tmp/compressor"  # Usar RAM disk si es posible

# Crear directorios optimizados
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Configuración de paralelización basada en RAM disponible
TOTAL_RAM = 32 * 1024 * 1024 * 1024  # 32GB en bytes
AVAILABLE_CPUS = psutil.cpu_count()
MAX_WORKERS = min(AVAILABLE_CPUS * 2, 16)  # Aumentar workers para RAM alta

# ===================== MEMORY MANAGER =====================
class MemoryManager:
    def __init__(self):
        self.cache_size = 4 * 1024 * 1024 * 1024  # 4GB cache en RAM
        self.cache_dir = Path(TEMP_DIR)
        self.active_processes = {}
        
    def allocate_buffer(self, process_id: str, size_mb: int):
        """Asignar buffer en RAM para proceso"""
        buffer_path = self.cache_dir / f"{process_id}_buffer.bin"
        # Simular buffer en RAM (en producción usaría ramfs/tmpfs)
        return str(buffer_path)
    
    def cleanup(self, process_id: str):
        """Limpiar recursos del proceso"""
        for file in self.cache_dir.glob(f"{process_id}_*"):
            try:
                file.unlink()
            except:
                pass

memory_manager = MemoryManager()

# ===================== FFMPEG OPTIMIZATIONS =====================
def get_optimized_ffmpeg_params(resolution: str, input_path: str):
    """Configuración optimizada de FFmpeg para máxima velocidad"""
    scale_map = {
        "360": "640:360",
        "480": "854:480",
        "720": "1280:720"
    }
    
    # Detectar características del hardware
    threads = AVAILABLE_CPUS * 2
    
    # Parámetros ultra optimizados para RAM alta
    base_params = [
        "ffmpeg", "-y",
        "-hwaccel", "auto",  # Auto-detectar hardware acceleration
        "-hwaccel_device", "0",
        "-threads", str(threads),
        "-i", input_path,
    ]
    
    # Filtros optimizados con paralelización
    video_filters = [
        f"scale={scale_map[resolution]}:flags=fast_bilinear",
        "fps=24",  # Mejor que 16 para calidad/velocidad
    ]
    
    video_params = [
        "-vf", ",".join(video_filters),
        "-c:v", "libx264",
        "-preset", "superfast",  # Balance mejor que ultrafast
        "-tune", "fastdecode",  # Optimizado para decodificación rápida
        "-crf", "28",  # Un poco mejor calidad que 30
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-movflags", "+faststart",
        "-threads", str(threads),
        "-x264-params", f"threads={threads}:lookahead-threads=2",
    ]
    
    # Audio optimizado
    audio_params = [
        "-c:a", "aac",
        "-b:a", "64k",  # Mejor calidad de audio
        "-ac", "2",  # Estéreo
        "-ar", "44100",
    ]
    
    # Buffer y caché optimizados para RAM alta
    buffer_params = [
        "-bufsize", "10M",  # Buffer más grande
        "-maxrate", "2M",
        "-f", "mp4",
    ]
    
    return base_params + video_params + audio_params + buffer_params

# ===================== WEB APP =====================
web = Flask(__name__)

@web.route("/")
def home():
    return "Telegram Video Compressor Bot running - 32GB RAM Optimized"

@web.route("/status")
def status():
    """Endpoint para monitorear recursos"""
    mem = psutil.virtual_memory()
    return {
        "ram_used_gb": mem.used / (1024**3),
        "ram_free_gb": mem.free / (1024**3),
        "ram_total_gb": mem.total / (1024**3),
        "cpu_cores": AVAILABLE_CPUS,
        "active_processes": len(memory_manager.active_processes)
    }

def run_web():
    web.run(host="0.0.0.0", port=PORT, threaded=True)

# ===================== BOT INIT =====================
app = Client(
    name="video-compressor",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=MAX_WORKERS,  # Más workers para RAM alta
    in_memory=True,
    max_concurrent_transmissions=5,  # Más transmisiones concurrentes
)

# Executor para operaciones CPU intensivas
process_executor = ProcessPoolExecutor(max_workers=min(AVAILABLE_CPUS, 8))
thread_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ===================== UTILS OPTIMIZADOS =====================
def progress_bar(percent: int, size: int = 20) -> str:
    filled = int(size * percent / 100)
    return "█" * filled + "░" * (size - filled)

async def get_video_duration_async(path: str) -> float:
    """Obtener duración en paralelo"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_executor, get_video_duration, path)

def get_video_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "json", path
    ]
    try:
        result = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        return float(json.loads(result)["format"]["duration"])
    except:
        return 0

async def safe_edit(msg, text):
    try:
        await msg.edit_text(text, parse_mode="html")
    except:
        pass

def clean_files(*paths):
    """Limpieza de archivos en segundo plano"""
    def cleanup_task():
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass
    threading.Thread(target=cleanup_task, daemon=True).start()

# ===================== VIDEO PROCESSING =====================
async def process_video_async(cmd: list, duration: float, callback_message):
    """Procesar video de forma asíncrona con mejor manejo de RAM"""
    process_id = str(int(time.time()))
    
    # Configurar entorno para máximo rendimiento
    env = os.environ.copy()
    env['FFMPEG_BIN'] = 'ffmpeg'
    
    # Iniciar proceso con buffers optimizados
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        limit=1024 * 1024 * 10  # 10MB buffer
    )
    
    time_regex = re.compile(r"out_time_ms=(\d+)")
    last_update = time.time()
    last_percent = 0
    
    # Leer salida de forma asíncrona
    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            break
            
        line = line_bytes.decode('utf-8', errors='ignore')
        match = time_regex.search(line)
        
        if match:
            current_time = int(match.group(1)) / 1_000_000
            if duration > 0:
                percent = min(100, int(current_time * 100 / duration))
                
                # Actualizar solo si hay cambio significativo
                if percent != last_percent and (percent - last_percent >= 2 or time.time() - last_update >= 0.5):
                    last_percent = percent
                    last_update = time.time()
                    bar = progress_bar(percent)
                    
                    await safe_edit(
                        callback_message,
                        f"⚙️ Comprimiendo...\n\n{bar} {percent}%\n"
                        f"RAM: {psutil.virtual_memory().percent}%"
                    )
    
    await process.wait()
    return process.returncode

# ===================== START =====================
@app.on_message(filters.command("start"))
async def start(_, msg):
    await msg.reply(
        "🎬 **Video Compressor Bot 2026**\n\n"
        "🚀 **Optimizado para 32GB RAM**\n"
        "✔ Hasta **4GB reales**\n"
        "✔ Progreso REAL con barra\n"
        "✔ 360p / 480p / 720p\n"
        f"✔ **{AVAILABLE_CPUS} núcleos** activos\n\n"
        "📤 Envíame un video",
        quote=True
    )

# ===================== RECEIVE VIDEO =====================
@app.on_message(filters.video | filters.document)
async def receive_video(_, msg):
    media = msg.video or msg.document
    input_path = f"{DOWNLOAD_DIR}/{media.file_unique_id}_{int(time.time())}.mp4"

    status = await msg.reply("⬇️ Descargando...\n\n░░░░░░░░░░░░░░░░░░ 0%")

    # Buffer más grande para descarga rápida
    chunk_size = 1024 * 1024 * 4  # 4MB chunks
    last_update = time.time()

    async def download_progress(current, total):
        nonlocal last_update
        if time.time() - last_update < 0.3:  # Actualizar más frecuente
            return
        last_update = time.time()
        
        if total > 0:
            percent = min(100, int(current * 100 / total))
            bar = progress_bar(percent)
            
            await safe_edit(
                status,
                f"⬇️ Descargando...\n\n{bar} {percent}%\n"
                f"RAM libre: {psutil.virtual_memory().free / (1024**3):.1f}GB"
            )

    await app.download_media(
        media,
        file_name=input_path,
        progress=download_progress,
        block=False  # No bloquear mientras descarga
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

# ===================== CALLBACK OPTIMIZADO =====================
@app.on_callback_query()
async def compress_callback(_, cb):
    try:
        res, input_path = cb.data.split("|")
        
        if not os.path.exists(input_path):
            await cb.answer("❌ Archivo no encontrado", show_alert=True)
            return
            
        output_path = f"{OUTPUT_DIR}/{res}_{os.path.basename(input_path)}"
        
        # Obtener duración en paralelo
        duration = await get_video_duration_async(input_path)
        
        await cb.message.edit_text(
            f"⚙️ Comprimiendo a {res}p...\n\n"
            f"Usando {AVAILABLE_CPUS} núcleos\n"
            "░░░░░░░░░░░░░░░░░░ 0%"
        )
        
        # Configuración optimizada de FFmpeg
        cmd = get_optimized_ffmpeg_params(res, input_path) + [
            "-progress", "pipe:1",
            "-nostats",
            output_path
        ]
        
        # Procesar video
        start_time = time.time()
        return_code = await process_video_async(cmd, duration, cb.message)
        
        if return_code != 0:
            raise Exception(f"FFmpeg error: {return_code}")
            
        processing_time = time.time() - start_time
        
        # ===================== UPLOAD OPTIMIZADO =====================
        await cb.message.edit_text(
            f"⬆️ Subiendo video...\n\n"
            f"Tiempo procesamiento: {processing_time:.1f}s\n"
            "░░░░░░░░░░░░░░░░░░ 0%"
        )
        
        last_update = time.time()
        
        async def upload_progress(current, total):
            nonlocal last_update
            if time.time() - last_update < 0.3:
                return
            last_update = time.time()
            
            if total > 0:
                percent = min(100, int(current * 100 / total))
                bar = progress_bar(percent)
                
                await safe_edit(
                    cb.message,
                    f"⬆️ Subiendo video...\n\n{bar} {percent}%"
                )
        
        # Subir con buffer grande
        await cb.message.reply_video(
            video=output_path,
            supports_streaming=True,
            progress=upload_progress,
            thumb=None,  # No generar thumbnail para más velocidad
        )
        
        # Limpiar en segundo plano
        clean_files(input_path, output_path)
        await cb.message.delete()
        
    except Exception as e:
        await safe_edit(cb.message, f"❌ Error: {str(e)}")
        clean_files(input_path, output_path)
        raise

# ===================== MAIN OPTIMIZADO =====================
if __name__ == "__main__":
    # Configurar para máximo rendimiento
    import uvloop
    uvloop.install()
    
    # Ajustar límites del sistema para RAM alta
    if sys.platform != "win32":
        import resource
        # Aumentar límites del sistema
        resource.setrlimit(resource.RLIMIT_NOFILE, (8192, 8192))
    
    # Iniciar web en thread separado
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
    print(f"🚀 Bot iniciado con {AVAILABLE_CPUS} núcleos y 32GB RAM")
    print(f"📊 RAM libre inicial: {psutil.virtual_memory().free / (1024**3):.1f}GB")
    
    # Iniciar bot con configuración optimizada
    app.run()
