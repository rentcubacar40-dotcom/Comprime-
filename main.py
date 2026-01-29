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

# IDs de usuarios permitidos (administradores + usuarios específicos)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "7363341763").split(",") if x.strip()]
USER_IDS = [int(x.strip()) for x in os.getenv("USER_IDS", "7766312183").split(",") if x.strip()]

# Combinar todas las IDs permitidas
ALLOWED_IDS = list(set(ADMIN_IDS + USER_IDS))

PORT = int(os.getenv("PORT", 10000))

DOWNLOAD_DIR = "downloads"
OUTPUT_DIR = "output"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===================== SISTEMA DE COLAS =====================
class UserType(Enum):
    ADMIN = 1
    USER = 2

class TaskStatus(Enum):
    PENDING = "⏳ En cola"
    DOWNLOADING = "📥 Descargando"
    COMPRESSING = "⚙️ Comprimiendo"
    UPLOADING = "📤 Subiendo"
    COMPLETED = "✅ Completado"
    ERROR = "❌ Error"

class CompressionTask:
    def __init__(self, user_id, user_name, user_type, message, input_path, resolution):
        self.user_id = user_id
        self.user_name = user_name
        self.user_type = user_type
        self.message = message
        self.input_path = input_path
        self.resolution = resolution
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.status_msg = None
        self.output_path = None
        self.start_time = time.time()
        self.task_id = None

class QueueSystem:
    def __init__(self):
        self.admin_queue = deque()
        self.user_queue = deque()
        self.active_tasks = []
        self.max_active_tasks = 2  # Máximo de procesos activos simultáneos
        self.task_counter = 0
        self.lock = asyncio.Lock()
    
    def add_task(self, task):
        # Asignar ID único
        self.task_counter += 1
        task.task_id = self.task_counter
        
        # Agregar a la cola correspondiente
        if task.user_type == UserType.ADMIN:
            self.admin_queue.append(task)
            position = len(self.admin_queue)
            queue_type = "admin"
        else:
            self.user_queue.append(task)
            position = len(self.admin_queue) + len(self.user_queue)
            queue_type = "user"
        
        return task.task_id, position, queue_type
    
    def get_next_task(self):
        # Prioridad 1: Administradores
        if self.admin_queue:
            return self.admin_queue.popleft()
        # Prioridad 2: Usuarios normales
        elif self.user_queue:
            return self.user_queue.popleft()
        return None
    
    def get_queue_info(self):
        info = {
            "active": len(self.active_tasks),
            "max_active": self.max_active_tasks,
            "admin_pending": len(self.admin_queue),
            "user_pending": len(self.user_queue),
            "admin_queue": [],
            "user_queue": [],
            "active_tasks": []
        }
        
        # Cola de administradores
        for i, task in enumerate(self.admin_queue, 1):
            info["admin_queue"].append({
                "position": i,
                "user_id": task.user_id,
                "user_name": task.user_name,
                "resolution": task.resolution
            })
        
        # Cola de usuarios
        for i, task in enumerate(self.user_queue, 1):
            info["user_queue"].append({
                "position": i + len(self.admin_queue),
                "user_id": task.user_id,
                "user_name": task.user_name,
                "resolution": task.resolution
            })
        
        # Tareas activas
        for task in self.active_tasks:
            info["active_tasks"].append({
                "user_id": task.user_id,
                "user_name": task.user_name,
                "user_type": task.user_type,
                "resolution": task.resolution,
                "status": task.status.value,
                "progress": task.progress
            })
        
        return info
    
    def can_start_new_task(self):
        return len(self.active_tasks) < self.max_active_tasks
    
    def add_active_task(self, task):
        task.status = TaskStatus.DOWNLOADING
        self.active_tasks.append(task)
    
    def remove_active_task(self, task):
        if task in self.active_tasks:
            self.active_tasks.remove(task)
    
    def update_task_status(self, task, status, progress=None):
        task.status = status
        if progress is not None:
            task.progress = progress

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

async def safe_reply(msg, text):
    try:
        await msg.reply(text)
    except:
        pass

def clean_files(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

# ===================== MIDDLEWARE PARA USUARIOS PERMITIDOS =====================
def allowed_users_only(func):
    async def wrapper(client, message):
        if message.from_user.id not in ALLOWED_IDS:
            await message.reply("🚫 **Acceso denegado**\n\nNo tienes permiso para usar este bot.")
            return
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
@allowed_users_only
async def start(_, msg):
    if msg.from_user.id in ADMIN_IDS:
        user_type = "👑 **Administrador**"
        instructions = "Tienes **prioridad máxima** en la cola de procesamiento."
    else:
        user_type = "👤 **Usuario**"
        instructions = "Tu video se procesará después de los administradores."
    
    await msg.reply(
        f"🎬 **Video Compressor Bot 2026**\n\n"
        f"{user_type}\n"
        f"{instructions}\n\n"
        "✔ Hasta **4GB reales**\n"
        "✔ Progreso REAL con barra\n"
        "✔ Sistema de colas con prioridad\n"
        "✔ 360p / 480p / 720p\n\n"
        "📋 **Comandos disponibles:**\n"
        "• /compress - Elegir compresión\n"
        "• /cola - Ver estado de la cola\n"
        "• /cancelar - Cancelar tu tarea\n\n"
        "📥 **Flujo de trabajo:**\n"
        "1. Usa /compress para elegir resolución\n"
        "2. Envía el video directamente\n"
        "3. Espera tu turno en la cola"
    )

# ===================== COMANDO COLA =====================
@app.on_message(filters.command("cola"))
@allowed_users_only
async def show_queue(_, msg):
    queue_info = queue_system.get_queue_info()
    
    text = "📊 **Estado de la Cola de Compresión**\n\n"
    
    # Capacidad
    text += f"🔄 **Procesos activos:** {queue_info['active']}/{queue_info['max_active']}\n\n"
    
    # Tareas activas
    if queue_info["active_tasks"]:
        text += "🔥 **Procesando ahora:**\n"
        for task in queue_info["active_tasks"]:
            user_icon = "👑" if task["user_type"] == UserType.ADMIN else "👤"
            status_icon = {
                "📥 Descargando": "📥",
                "⚙️ Comprimiendo": "⚙️",
                "📤 Subiendo": "📤"
            }.get(task["status"], "⏳")
            
            text += (
                f"{user_icon} **{task['user_name']}**\n"
                f"   {status_icon} {task['status']} - {task['progress']}%\n"
                f"   📐 {task['resolution']}p\n"
            )
        text += "\n"
    
    # Cola de administradores
    if queue_info["admin_pending"] > 0:
        text += f"👑 **Administradores en espera:** {queue_info['admin_pending']}\n"
        for task in queue_info["admin_queue"][:5]:
            text += f"  #{task['position']} - {task['user_name']} ({task['resolution']}p)\n"
        text += "\n"
    
    # Cola de usuarios
    if queue_info["user_pending"] > 0:
        text += f"👤 **Usuarios en espera:** {queue_info['user_pending']}\n"
        for task in queue_info["user_queue"][:5]:
            text += f"  #{task['position']} - {task['user_name']} ({task['resolution']}p)\n"
    
    if queue_info["active"] == 0 and queue_info["admin_pending"] == 0 and queue_info["user_pending"] == 0:
        text += "✅ **La cola está vacía**\nNo hay tareas pendientes."
    
    await msg.reply(text)

# ===================== COMANDO COMPRESS =====================
@app.on_message(filters.command("compress"))
@allowed_users_only
async def compress_command(_, msg):
    # Verificar si ya tiene una compresión configurada
    user_id = msg.from_user.id
    if user_id in user_compression:
        current = user_compression[user_id]
        await msg.reply(
            f"📝 **Configuración actual:** {current}p\n\n"
            "¿Quieres cambiar la resolución?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Sí, cambiar", callback_data="choose_compression")],
                [InlineKeyboardButton("❌ No, mantener", callback_data=f"keep_{current}")]
            ])
        )
    else:
        await choose_compression_ui(msg)

async def choose_compression_ui(msg):
    await msg.reply(
        "🎯 **Elige resolución de compresión**\n\n"
        "Esta configuración se aplicará al próximo video que envíes.\n\n"
        "👇 Selecciona:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("360p", callback_data="set_360"),
                InlineKeyboardButton("480p", callback_data="set_480"),
                InlineKeyboardButton("720p", callback_data="set_720")
            ],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel_set")]
        ])
    )

# Diccionario global para almacenar la compresión elegida por usuario
user_compression = {}

@app.on_callback_query(filters.regex(r"set_(360|480|720)"))
@allowed_users_only
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
        f"✅ **Compresión configurada correctamente**\n\n"
        f"📐 **Resolución:** {res}p ({scale_map[res]})\n"
        f"👤 **Tipo:** {user_type}\n"
        f"👤 **Usuario:** {cb.from_user.first_name}\n\n"
        "📤 **Ahora puedes enviar el video**\n"
        "El video se agregará a la cola con esta configuración.\n\n"
        "📋 **Recuerda:**\n"
        "• Administradores tienen prioridad\n"
        "• Usa /cola para ver tu posición\n"
        "• Máximo 2 procesos simultáneos"
    )

@app.on_callback_query(filters.regex(r"keep_(360|480|720)"))
@allowed_users_only
async def keep_compression(_, cb):
    res = cb.data.split("_")[1]
    user_id = cb.from_user.id
    user_type = "👑 Administrador" if user_id in ADMIN_IDS else "👤 Usuario"
    
    await cb.message.edit_text(
        f"✅ **Configuración mantenida**\n\n"
        f"📐 **Resolución:** {res}p\n"
        f"👤 **Tipo:** {user_type}\n\n"
        "📤 **Ahora puedes enviar el video**\n"
        "El video se agregará a la cola con esta configuración."
    )

@app.on_callback_query(filters.regex("cancel_set"))
@allowed_users_only
async def cancel_set_compression(_, cb):
    await cb.message.edit_text("❌ **Configuración cancelada**\n\nUsa /compress cuando quieras configurar la compresión.")

@app.on_callback_query(filters.regex("choose_compression"))
@allowed_users_only
async def callback_choose_compression(_, cb):
    await choose_compression_ui(cb.message)

# ===================== RECEIVE VIDEO =====================
@app.on_message(filters.video | filters.document)
@allowed_users_only
async def receive_video(_, msg):
    user_id = msg.from_user.id
    
    # Verificar si el usuario ya eligió compresión
    if user_id not in user_compression:
        await msg.reply(
            "⚠️ **Primero debes configurar la compresión**\n\n"
            "Usa el comando /compress para elegir la resolución antes de enviar el video.\n\n"
            "📋 **Flujo correcto:**\n"
            "1. /compress (elegir 360p/480p/720p)\n"
            "2. Enviar video\n"
            "3. Esperar en cola"
        )
        return
    
    res = user_compression[user_id]
    
    # Determinar tipo de usuario
    user_type = UserType.ADMIN if user_id in ADMIN_IDS else UserType.USER
    user_type_text = "👑 Administrador" if user_type == UserType.ADMIN else "👤 Usuario"
    
    # Crear nombre de archivo único
    media = msg.video or msg.document
    unique_id = f"{user_id}_{int(time.time())}_{media.file_unique_id}"
    input_path = f"{DOWNLOAD_DIR}/{unique_id}.mp4"
    
    # Crear mensaje de estado inicial
    status = await msg.reply(
        f"📝 **Video recibido - Agregando a cola**\n\n"
        f"👤 **Tipo:** {user_type_text}\n"
        f"👤 **Usuario:** {msg.from_user.first_name}\n"
        f"📐 **Resolución:** {res}p\n"
        f"⏳ **Estado:** Preparando...\n\n"
        f"🔄 **Procesando cola...**"
    )
    
    # Crear tarea
    task = CompressionTask(
        user_id=user_id,
        user_name=msg.from_user.first_name,
        user_type=user_type,
        message=msg,
        input_path=input_path,
        resolution=res
    )
    
    task.status_msg = status
    
    # Agregar a la cola
    task_id, position, queue_type = queue_system.add_task(task)
    
    # Actualizar mensaje con posición en cola
    priority_text = "🔴 **ALTA** (Administrador)" if queue_type == "admin" else "🟡 **NORMAL** (Usuario)"
    
    await safe_edit(
        status,
        f"📝 **Video agregado a la cola**\n\n"
        f"👤 **Tipo:** {user_type_text}\n"
        f"👤 **Usuario:** {msg.from_user.first_name}\n"
        f"📐 **Resolución:** {res}p\n"
        f"🎯 **Prioridad:** {priority_text}\n"
        f"📍 **Posición en cola:** #{position}\n"
        f"⏳ **Estado:** En espera...\n\n"
        f"📊 Usa /cola para ver el progreso"
    )
    
    # Iniciar procesador de cola
    asyncio.create_task(process_queue())

# ===================== PROCESADOR DE COLA =====================
async def process_queue():
    async with queue_system.lock:
        while queue_system.can_start_new_task():
            task = queue_system.get_next_task()
            if not task:
                break
            
            # Agregar a tareas activas
            queue_system.add_active_task(task)
            
            # Iniciar procesamiento en segundo plano
            asyncio.create_task(process_task(task))
            
            # Pequeña pausa entre inicios
            await asyncio.sleep(0.5)

async def process_task(task):
    try:
        # PASO 1: Descargar video
        await download_video(task)
        
        # PASO 2: Comprimir video
        await compress_video(task)
        
        # PASO 3: Subir video
        await upload_video(task)
        
        # Tarea completada
        task.status = TaskStatus.COMPLETED
        task.progress = 100
        
        await safe_edit(
            task.status_msg,
            f"✅ **Video procesado exitosamente**\n\n"
            f"👤 **Usuario:** {task.user_name}\n"
            f"📐 **Resolución:** {task.resolution}p\n"
            f"⏱️ **Tiempo total:** {int(time.time() - task.start_time)} segundos\n\n"
            f"🎉 **Proceso completado**"
        )
        
        # Esperar antes de eliminar el mensaje
        await asyncio.sleep(5)
        await task.status_msg.delete()
        
    except Exception as e:
        task.status = TaskStatus.ERROR
        await safe_edit(
            task.status_msg,
            f"❌ **Error en el procesamiento**\n\n"
            f"👤 **Usuario:** {task.user_name}\n"
            f"📐 **Resolución:** {task.resolution}p\n"
            f"💥 **Error:** {str(e)}\n\n"
            f"⚠️ **La tarea ha sido cancelada**"
        )
        # Esperar antes de eliminar
        await asyncio.sleep(10)
        await task.status_msg.delete()
    finally:
        # Limpiar archivos
        if hasattr(task, 'input_path'):
            clean_files(task.input_path)
        if hasattr(task, 'output_path') and task.output_path:
            clean_files(task.output_path)
        
        # Remover de tareas activas
        queue_system.remove_active_task(task)
        
        # Procesar siguiente tarea en cola
        asyncio.create_task(process_queue())

# ===================== FUNCIÓN DE DESCARGA =====================
async def download_video(task):
    # Actualizar estado
    queue_system.update_task_status(task, TaskStatus.DOWNLOADING, 0)
    
    user_type_text = "👑 Administrador" if task.user_type == UserType.ADMIN else "👤 Usuario"
    
    await safe_edit(
        task.status_msg,
        f"📥 **Descargando video...**\n\n"
        f"👤 **Tipo:** {user_type_text}\n"
        f"👤 **Usuario:** {task.user_name}\n"
        f"📐 **Resolución:** {task.resolution}p\n"
        f"⏳ **Estado:** Descargando...\n\n"
        f"░░░░░░░░░░░░░░░░░░ 0%"
    )
    
    last_update = time.time()
    
    async def download_progress(current, total):
        nonlocal last_update
        if total == 0:
            return
        
        percent = int(current * 100 / total)
        
        # Actualizar cada segundo
        if time.time() - last_update >= 1:
            last_update = time.time()
            bar = progress_bar(percent)
            
            queue_system.update_task_status(task, TaskStatus.DOWNLOADING, percent)
            
            await safe_edit(
                task.status_msg,
                f"📥 **Descargando video...**\n\n"
                f"👤 **Tipo:** {user_type_text}\n"
                f"👤 **Usuario:** {task.user_name}\n"
                f"📐 **Resolución:** {task.resolution}p\n"
                f"⏳ **Estado:** Descargando...\n\n"
                f"{bar} {percent}%"
            )
    
    # Descargar el video
    media = task.message.video or task.message.document
    await app.download_media(
        media,
        file_name=task.input_path,
        progress=download_progress
    )
    
    # Descarga completada
    queue_system.update_task_status(task, TaskStatus.DOWNLOADING, 100)
    
    await safe_edit(
        task.status_msg,
        f"✅ **Descarga completada**\n\n"
        f"👤 **Usuario:** {task.user_name}\n"
        f"📐 **Resolución:** {task.resolution}p\n"
        f"⏳ **Siguiente:** Comprimiendo video..."
    )

# ===================== FUNCIÓN DE COMPRESIÓN =====================
async def compress_video(task):
    # Actualizar estado
    queue_system.update_task_status(task, TaskStatus.COMPRESSING, 0)
    
    user_type_text = "👑 Administrador" if task.user_type == UserType.ADMIN else "👤 Usuario"
    scale_map = {
        "360": "640:360",
        "480": "854:480",
        "720": "1280:720"
    }
    
    scale = scale_map[task.resolution]
    task.output_path = f"{OUTPUT_DIR}/{task.user_id}_{int(time.time())}_{task.resolution}.mp4"
    
    # Obtener duración del video
    try:
        duration = get_video_duration(task.input_path)
    except Exception as e:
        raise Exception(f"Error al obtener duración: {str(e)}")
    
    await safe_edit(
        task.status_msg,
        f"⚙️ **Comprimiendo video...**\n\n"
        f"👤 **Tipo:** {user_type_text}\n"
        f"👤 **Usuario:** {task.user_name}\n"
        f"📐 **Resolución:** {task.resolution}p ({scale})\n"
        f"⏱️ **Duración:** {int(duration)} segundos\n"
        f"⏳ **Estado:** Comprimiendo...\n\n"
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
        task.output_path
    ]
    
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        universal_newlines=True
    )
    
    time_regex = re.compile(r"out_time_ms=(\d+)")
    last_update = time.time()
    
    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        
        if line:
            match = time_regex.search(line)
            if match:
                current_time = int(match.group(1)) / 1_000_000
                progress = min(99, int(current_time * 100 / duration))
                
                # Actualizar cada segundo
                if time.time() - last_update >= 1:
                    last_update = time.time()
                    bar = progress_bar(progress)
                    
                    queue_system.update_task_status(task, TaskStatus.COMPRESSING, progress)
                    
                    await safe_edit(
                        task.status_msg,
                        f"⚙️ **Comprimiendo video...**\n\n"
                        f"👤 **Tipo:** {user_type_text}\n"
                        f"👤 **Usuario:** {task.user_name}\n"
                        f"📐 **Resolución:** {task.resolution}p ({scale})\n"
                        f"⏱️ **Duración:** {int(duration)} segundos\n"
                        f"⏳ **Estado:** Comprimiendo...\n\n"
                        f"{bar} {progress}%"
                    )
        
        await asyncio.sleep(0.1)
    
    process.wait()
    
    if process.returncode != 0:
        raise Exception("Error en la compresión FFmpeg")
    
    # Compresión completada
    queue_system.update_task_status(task, TaskStatus.COMPRESSING, 100)
    
    await safe_edit(
        task.status_msg,
        f"✅ **Compresión completada**\n\n"
        f"👤 **Usuario:** {task.user_name}\n"
        f"📐 **Resolución:** {task.resolution}p\n"
        f"⏳ **Siguiente:** Subiendo video..."
    )

# ===================== FUNCIÓN DE SUBIDA =====================
async def upload_video(task):
    # Actualizar estado
    queue_system.update_task_status(task, TaskStatus.UPLOADING, 0)
    
    user_type_text = "👑 Administrador" if task.user_type == UserType.ADMIN else "👤 Usuario"
    
    await safe_edit(
        task.status_msg,
        f"📤 **Subiendo video...**\n\n"
        f"👤 **Tipo:** {user_type_text}\n"
        f"👤 **Usuario:** {task.user_name}\n"
        f"📐 **Resolución:** {task.resolution}p\n"
        f"⏳ **Estado:** Subiendo...\n\n"
        f"░░░░░░░░░░░░░░░░░░ 0%"
    )
    
    last_update = time.time()
    
    async def upload_progress(current, total):
        nonlocal last_update
        if total == 0:
            return
        
        percent = int(current * 100 / total)
        
        # Actualizar cada segundo
        if time.time() - last_update >= 1:
            last_update = time.time()
            bar = progress_bar(percent)
            
            queue_system.update_task_status(task, TaskStatus.UPLOADING, percent)
            
            await safe_edit(
                task.status_msg,
                f"📤 **Subiendo video...**\n\n"
                f"👤 **Tipo:** {user_type_text}\n"
                f"👤 **Usuario:** {task.user_name}\n"
                f"📐 **Resolución:** {task.resolution}p\n"
                f"⏳ **Estado:** Subiendo...\n\n"
                f"{bar} {percent}%"
            )
    
    try:
        await task.message.reply_video(
            video=task.output_path,
            caption=f"✅ **Video comprimido a {task.resolution}p**\n\n"
                   f"👤 Enviado por: {task.user_name}\n"
                   f"🎯 Tipo: {user_type_text}\n"
                   f"⏱️ Procesado en: {int(time.time() - task.start_time)} segundos",
            supports_streaming=True,
            progress=upload_progress
        )
        
        # Subida completada
        queue_system.update_task_status(task, TaskStatus.UPLOADING, 100)
        
    except Exception as e:
        raise Exception(f"Error al subir video: {str(e)}")

# ===================== COMANDO CANCELAR =====================
@app.on_message(filters.command("cancelar"))
@allowed_users_only
async def cancel_task(_, msg):
    user_id = msg.from_user.id
    queue_info = queue_system.get_queue_info()
    
    cancelled = False
    
    # Buscar en tareas activas
    for task in queue_system.active_tasks[:]:
        if task.user_id == user_id:
            queue_system.remove_active_task(task)
            await task.status_msg.edit_text(
                f"❌ **Tarea cancelada por el usuario**\n\n"
                f"👤 **Usuario:** {task.user_name}\n"
                f"📐 **Resolución:** {task.resolution}p\n\n"
                f"⚠️ El proceso ha sido detenido."
            )
            clean_files(task.input_path, task.output_path if hasattr(task, 'output_path') else None)
            cancelled = True
            break
    
    # Buscar en cola de administradores
    if not cancelled:
        for i, task in enumerate(queue_system.admin_queue):
            if task.user_id == user_id:
                queue_system.admin_queue.remove(task)
                cancelled = True
                break
    
    # Buscar en cola de usuarios
    if not cancelled:
        for i, task in enumerate(queue_system.user_queue):
            if task.user_id == user_id:
                queue_system.user_queue.remove(task)
                cancelled = True
                break
    
    if cancelled:
        await msg.reply("✅ **Tu tarea ha sido cancelada y eliminada de la cola.**")
        
        # Eliminar configuración de compresión
        if user_id in user_compression:
            del user_compression[user_id]
    else:
        await msg.reply("ℹ️ **No tienes tareas pendientes en la cola.**")

# ===================== COMANDO PARA LIMPIAR COLA (SOLO ADMIN) =====================
@app.on_message(filters.command("limpiarcola"))
@admin_command
async def clear_queue(_, msg):
    queue_info = queue_system.get_queue_info()
    total_pending = queue_info["admin_pending"] + queue_info["user_pending"]
    
    # Limpiar colas
    queue_system.admin_queue.clear()
    queue_system.user_queue.clear()
    
    # Limpiar configuraciones de usuarios en cola
    for task in list(queue_system.admin_queue) + list(queue_system.user_queue):
        if task.user_id in user_compression:
            del user_compression[task.user_id]
    
    await msg.reply(
        f"✅ **Cola limpiada exitosamente**\n\n"
        f"🗑️ **Tareas eliminadas:** {total_pending}\n"
        f"🔄 **Procesos activos:** {queue_info['active']} (no afectados)\n\n"
        f"⚠️ **Nota:** Los procesos en curso continúan normalmente."
    )

# ===================== MAIN =====================
if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    app.run()
