import os
import asyncio
import threading
import time
import math
import subprocess
import json
import re
from collections import deque
from enum import Enum

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

# ===================== SISTEMA DE COLAS =====================
class UserType(Enum):
    ADMIN = 1
    USER = 2

class CompressionTask:
    def __init__(self, user_id, user_name, user_type, message, status_msg, input_path, resolution, timestamp):
        self.user_id = user_id
        self.user_name = user_name
        self.user_type = user_type
        self.message = message
        self.status_msg = status_msg
        self.input_path = input_path
        self.resolution = resolution
        self.timestamp = timestamp
        self.progress = 0
        self.is_active = False

class QueueSystem:
    def __init__(self):
        self.admin_queue = deque()
        self.user_queue = deque()
        self.active_tasks = []
        self.max_active_tasks = 2  # Máximo de procesos activos simultáneos
        self.task_counter = 0
        self.lock = asyncio.Lock()
    
    def add_task(self, task):
        if task.user_type == UserType.ADMIN:
            self.admin_queue.append(task)
            position = len(self.admin_queue)
            queue_type = "admin"
        else:
            self.user_queue.append(task)
            position = len(self.admin_queue) + len(self.user_queue)
            queue_type = "user"
        
        self.task_counter += 1
        task.task_id = self.task_counter
        return task.task_id, position, queue_type
    
    def get_next_task(self):
        # Primero de la cola de administradores
        if self.admin_queue:
            return self.admin_queue.popleft()
        # Si no hay administradores, de la cola de usuarios
        elif self.user_queue:
            return self.user_queue.popleft()
        return None
    
    def get_queue_info(self):
        info = {
            "active": len(self.active_tasks),
            "admin_pending": len(self.admin_queue),
            "user_pending": len(self.user_queue),
            "admin_queue": [],
            "user_queue": []
        }
        
        for task in self.admin_queue:
            info["admin_queue"].append({
                "user_id": task.user_id,
                "user_name": task.user_name,
                "resolution": task.resolution
            })
        
        for task in self.user_queue:
            info["user_queue"].append({
                "user_id": task.user_id,
                "user_name": task.user_name,
                "resolution": task.resolution
            })
        
        info["active_tasks"] = []
        for task in self.active_tasks:
            info["active_tasks"].append({
                "user_id": task.user_id,
                "user_name": task.user_name,
                "resolution": task.resolution,
                "progress": task.progress
            })
        
        return info
    
    def can_start_new_task(self):
        return len(self.active_tasks) < self.max_active_tasks
    
    def add_active_task(self, task):
        task.is_active = True
        self.active_tasks.append(task)
    
    def remove_active_task(self, task):
        task.is_active = False
        if task in self.active_tasks:
            self.active_tasks.remove(task)
    
    def update_task_progress(self, task_id, progress):
        for task in self.active_tasks:
            if task.task_id == task_id:
                task.progress = progress
                break

# Instancia global del sistema de colas
queue_system = QueueSystem()

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
            # Usuarios normales también pueden usar el bot
            # pero con menor prioridad
            await func(client, message)
        else:
            await func(client, message)
    return wrapper

def admin_command(func):
    async def wrapper(client, message):
        if message.from_user.id not in ADMIN_IDS:
            await message.reply("🚫 **Acceso denegado**\n\nEste comando es solo para administradores.")
            return
        await func(client, message)
    return wrapper

# ===================== START =====================
@app.on_message(filters.command("start"))
async def start(_, msg):
    if msg.from_user.id in ADMIN_IDS:
        user_type = "👑 **Administrador**"
    else:
        user_type = "👤 **Usuario**"
    
    await msg.reply(
        f"🎬 **Video Compressor Bot 2026**\n\n"
        f"{user_type}\n"
        "✔ Hasta **4GB reales**\n"
        "✔ Progreso REAL con barra\n"
        "✔ Sistema de colas con prioridad\n"
        "✔ 360p / 480p / 720p\n\n"
        "📥 **Nuevo flujo:**\n"
        "1. Primero elige compresión\n"
        "2. Luego envía el video\n\n"
        "👇 Presiona el botón para empezar:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Elegir Compresión", callback_data="choose_compression")]
        ])
    )

# ===================== COMANDO COLA =====================
@app.on_message(filters.command("cola"))
async def show_queue(_, msg):
    queue_info = queue_system.get_queue_info()
    
    text = "📊 **Estado de la Cola de Compresión**\n\n"
    
    # Tareas activas
    if queue_info["active_tasks"]:
        text += "🔄 **Procesos Activos:**\n"
        for task in queue_info["active_tasks"]:
            user_type = "👑" if task["user_id"] in ADMIN_IDS else "👤"
            text += f"{user_type} {task['user_name']}: {task['resolution']}p - {task['progress']}%\n"
        text += "\n"
    
    # Cola de administradores
    if queue_info["admin_pending"] > 0:
        text += f"👑 **Administradores en cola:** {queue_info['admin_pending']}\n"
        for i, task in enumerate(queue_info["admin_queue"][:3], 1):
            text += f"{i}. {task['user_name']} - {task['resolution']}p\n"
        if queue_info["admin_pending"] > 3:
            text += f"... y {queue_info['admin_pending'] - 3} más\n"
        text += "\n"
    
    # Cola de usuarios
    if queue_info["user_pending"] > 0:
        text += f"👤 **Usuarios en cola:** {queue_info['user_pending']}\n"
        for i, task in enumerate(queue_info["user_queue"][:3], 1):
            text += f"{i}. {task['user_name']} - {task['resolution']}p\n"
        if queue_info["user_pending"] > 3:
            text += f"... y {queue_info['user_pending'] - 3} más\n"
    
    if queue_info["active"] == 0 and queue_info["admin_pending"] == 0 and queue_info["user_pending"] == 0:
        text += "✅ **La cola está vacía**\nNo hay tareas pendientes."
    
    await msg.reply(text)

# ===================== ELEGIR COMPRESIÓN =====================
@app.on_callback_query(filters.regex("choose_compression"))
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
async def set_compression(_, cb):
    res = cb.data.split("_")[1]
    user_id = cb.from_user.id
    user_compression[user_id] = res
    
    scale_map = {
        "360": "640:360",
        "480": "854:480",
        "720": "1280:720"
    }
    
    user_type = "👑 Administrador" if user_id in ADMIN_IDS else "👤 Usuario"
    
    await cb.message.edit_text(
        f"✅ **Compresión {res}p configurada**\n\n"
        f"📐 Resolución: {scale_map[res]}\n"
        f"👤 Tipo: {user_type}\n"
        f"📝 Nombre: {cb.from_user.first_name}\n\n"
        "📤 **Ahora envía el video**\n"
        "Se agregará a la cola de procesamiento."
    )

# ===================== RECEIVE VIDEO =====================
@app.on_message(filters.video | filters.document)
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
    
    # Determinar tipo de usuario
    user_type = UserType.ADMIN if user_id in ADMIN_IDS else UserType.USER
    user_type_text = "👑 Administrador" if user_type == UserType.ADMIN else "👤 Usuario"
    
    # Crear task
    input_path = f"{DOWNLOAD_DIR}/{user_id}_{int(time.time())}_{media.file_unique_id}.mp4"
    status = await msg.reply(f"⏳ **Preparando descarga...**\n\nTipo: {user_type_text}\nResolución: {res}p")
    
    # Descargar video
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
            f"📥 **Descargando video...**\n\n"
            f"Tipo: {user_type_text}\n"
            f"Resolución: {res}p\n\n"
            f"{bar} {percent}%"
        )
    
    await app.download_media(
        media,
        file_name=input_path,
        progress=download_progress
    )
    
    # Crear tarea y agregar a la cola
    task = CompressionTask(
        user_id=user_id,
        user_name=msg.from_user.first_name,
        user_type=user_type,
        message=msg,
        status_msg=status,
        input_path=input_path,
        resolution=res,
        timestamp=time.time()
    )
    
    task_id, position, queue_type = queue_system.add_task(task)
    
    await safe_edit(
        status,
        f"✅ **Video descargado**\n\n"
        f"👤 Tipo: {user_type_text}\n"
        f"📐 Resolución: {res}p\n"
        f"📊 Posición en cola: #{position}\n"
        f"🎯 Prioridad: {'Alta' if queue_type == 'admin' else 'Normal'}\n\n"
        f"⏳ Esperando turno..."
    )
    
    # Iniciar procesador de cola si no está corriendo
    asyncio.create_task(process_queue())

# ===================== PROCESADOR DE COLA =====================
async def process_queue():
    async with queue_system.lock:
        while queue_system.can_start_new_task():
            task = queue_system.get_next_task()
            if not task:
                break
            
            # Actualizar estado
            await safe_edit(
                task.status_msg,
                f"🚀 **Iniciando compresión...**\n\n"
                f"👤 Tipo: {'👑 Administrador' if task.user_type == UserType.ADMIN else '👤 Usuario'}\n"
                f"📐 Resolución: {task.resolution}p\n"
                f"👤 Nombre: {task.user_name}\n\n"
                f"⏳ Procesando..."
            )
            
            # Agregar a tareas activas
            queue_system.add_active_task(task)
            
            # Iniciar compresión en segundo plano
            asyncio.create_task(compress_and_upload(task))

async def compress_and_upload(task):
    try:
        await compress_video(task)
    except Exception as e:
        await task.status_msg.edit_text(f"❌ **Error en compresión:** {str(e)}")
        clean_files(task.input_path)
    finally:
        # Remover de tareas activas
        queue_system.remove_active_task(task)
        
        # Procesar siguiente tarea en cola
        asyncio.create_task(process_queue())

# ===================== FUNCIÓN DE COMPRESIÓN MODIFICADA =====================
async def compress_video(task):
    scale_map = {
        "360": "640:360",
        "480": "854:480",
        "720": "1280:720"
    }
    
    scale = scale_map[task.resolution]
    output_path = f"{OUTPUT_DIR}/{task.resolution}_{task.user_id}_{int(time.time())}.mp4"
    
    try:
        duration = get_video_duration(task.input_path)
    except:
        await task.status_msg.edit_text("❌ Error al obtener duración del video")
        clean_files(task.input_path)
        return
    
    user_type_text = "👑 Administrador" if task.user_type == UserType.ADMIN else "👤 Usuario"
    
    await safe_edit(
        task.status_msg,
        f"⚙️ **Comprimiendo a {task.resolution}p...**\n\n"
        f"Tipo: {user_type_text}\n"
        f"Nombre: {task.user_name}\n\n"
        f"░░░░░░░░░░░░░░░░░░ 0%"
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-i", task.input_path,
        "-vf", f"scale={scale},fps=23",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "36",
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-movflags", "+faststart",
        "-c:a", "aac",
        "-b:a", "60k",
        "-ac", "1",
        "-threads", "2",
        "-x264-params", "scenecut=0:open_gop=0",
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
            progress = min(100, int(current_time * 100 / duration))
            
            # Actualizar progreso en la tarea
            task.progress = progress
            queue_system.update_task_progress(task.task_id, progress)
            
            if time.time() - last_update >= 1:
                last_update = time.time()
                bar = progress_bar(progress)
                
                await safe_edit(
                    task.status_msg,
                    f"⚙️ **Comprimiendo a {task.resolution}p...**\n\n"
                    f"Tipo: {user_type_text}\n"
                    f"Nombre: {task.user_name}\n\n"
                    f"{bar} {progress}%"
                )
        
        await asyncio.sleep(0.05)
    
    process.wait()
    
    # ===================== UPLOAD =====================
    await safe_edit(
        task.status_msg,
        f"📤 **Subiendo video {task.resolution}p...**\n\n"
        f"Tipo: {user_type_text}\n"
        f"Nombre: {task.user_name}\n\n"
        f"░░░░░░░░░░░░░░░░░░ 0%"
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
            task.status_msg,
            f"📤 **Subiendo video {task.resolution}p...**\n\n"
            f"Tipo: {user_type_text}\n"
            f"Nombre: {task.user_name}\n\n"
            f"{bar} {percent}%"
        )
    
    try:
        await task.message.reply_video(
            video=output_path,
            caption=f"✅ **Video comprimido a {task.resolution}p**\n\n"
                   f"👤 Enviado por: {task.user_name}\n"
                   f"🎯 Tipo: {user_type_text}",
            supports_streaming=True,
            progress=upload_progress
        )
    except Exception as e:
        await task.status_msg.edit_text(f"❌ Error al subir: {str(e)}")
    
    # Limpiar archivos
    clean_files(task.input_path, output_path)
    await task.status_msg.delete()
    
    # Opcional: mantener la compresión para el usuario
    # Para resetear: del user_compression[task.user_id]

# ===================== COMANDO PARA CAMBIAR COMPRESIÓN =====================
@app.on_message(filters.command("compression"))
async def change_compression(_, msg):
    await choose_compression(_, msg)

# ===================== COMANDO PARA LIMPIAR COLA (SOLO ADMIN) =====================
@app.on_message(filters.command("limpiarcola"))
@admin_command
async def clear_queue(_, msg):
    queue_info = queue_system.get_queue_info()
    total_pending = queue_info["admin_pending"] + queue_info["user_pending"]
    
    # Limpiar colas
    queue_system.admin_queue.clear()
    queue_system.user_queue.clear()
    
    await msg.reply(f"✅ **Cola limpiada**\n\nSe eliminaron {total_pending} tareas pendientes.")

# ===================== MAIN =====================
if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    app.run()
