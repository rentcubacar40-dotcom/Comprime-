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
from datetime import timedelta

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

# ===================== CONFIG =====================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# IDs de administradores
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "7363341763").split(",") if x.strip()]

# Archivo para almacenar usuarios permitidos
USERS_FILE = "allowed_users.json"

PORT = int(os.getenv("PORT", 10000))

DOWNLOAD_DIR = "downloads"
OUTPUT_DIR = "output"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===================== GESTIÓN DE USUARIOS =====================
def load_allowed_users():
    """Cargar usuarios permitidos desde archivo"""
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    return {"users": [], "admins": ADMIN_IDS}

def save_allowed_users():
    """Guardar usuarios permitidos a archivo"""
    data = {
        "users": ALLOWED_USERS["users"],
        "admins": ALLOWED_USERS["admins"]
    }
    with open(USERS_FILE, 'w') as f:
        json.dump(data, f)

# Cargar usuarios al iniciar
ALLOWED_USERS = load_allowed_users()

def get_all_allowed_ids():
    """Obtener todas las IDs permitidas"""
    return set(ALLOWED_USERS["admins"] + ALLOWED_USERS["users"])

# ===================== SISTEMA DE COLAS =====================
class UserType(Enum):
    ADMIN = 1
    USER = 2

class TaskStatus(Enum):
    PENDING = "⏳ En espera"
    DOWNLOADING = "📥 Descargando"
    COMPRESSING = "⚙️ Comprimiendo"
    UPLOADING = "📤 Subiendo"
    COMPLETED = "✅ Completado"
    ERROR = "❌ Error"

class CompressionTask:
    def __init__(self, user_id, username, user_type, message, file_id, resolution):
        self.user_id = user_id
        self.username = self._format_username(username)
        self.user_type = user_type
        self.message = message
        self.file_id = file_id  # ID del archivo en Telegram
        self.resolution = resolution
        self.status = TaskStatus.PENDING
        self.progress = 0
        self.status_msg = None
        self.input_path = None
        self.output_path = None
        self.start_time = time.time()
        self.task_id = None
        self.download_start = None
        self.compress_start = None
        self.upload_start = None
        self.duration = None  # Duración del video
    
    def _format_username(self, username):
        """Formatear username para mostrar"""
        if username:
            return f"@{username}"
        else:
            return f"ID: {self.user_id}"
    
    def get_elapsed_time(self):
        """Obtener tiempo transcurrido formateado"""
        elapsed = time.time() - self.start_time
        return self._format_time(elapsed)
    
    def get_current_step_time(self):
        """Obtener tiempo del paso actual"""
        if self.status == TaskStatus.DOWNLOADING and self.download_start:
            return self._format_time(time.time() - self.download_start)
        elif self.status == TaskStatus.COMPRESSING and self.compress_start:
            return self._format_time(time.time() - self.compress_start)
        elif self.status == TaskStatus.UPLOADING and self.upload_start:
            return self._format_time(time.time() - self.upload_start)
        return "0s"
    
    def _format_time(self, seconds):
        """Formatear segundos a texto legible"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{mins}m {secs}s"
        else:
            hours = int(seconds // 3600)
            mins = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            return f"{hours}h {mins}m {secs}s"

class QueueSystem:
    def __init__(self):
        self.admin_queue = deque()
        self.user_queue = deque()
        self.active_tasks = []
        self.max_active_tasks = 2  # MÁXIMO 2 PROCESOS ACTIVOS
        self.task_counter = 0
        self.lock = asyncio.Lock()
        self.user_active_counts = {}  # Control de tareas por usuario
    
    def can_user_add_task(self, user_id, user_type):
        """Verificar si un usuario puede agregar más tareas"""
        if user_type == UserType.ADMIN:
            # Administradores pueden tener todas las tareas que quieran
            return True
        else:
            # Usuarios normales solo pueden tener 1 tarea en cola
            user_tasks = 0
            # Contar en cola de usuarios
            for task in self.user_queue:
                if task.user_id == user_id:
                    user_tasks += 1
            # Contar en tareas activas
            for task in self.active_tasks:
                if task.user_id == user_id and task.user_type == UserType.USER:
                    user_tasks += 1
            return user_tasks < 1  # Solo 1 tarea máxima
    
    def add_task(self, task):
        """Agregar una nueva tarea a la cola"""
        self.task_counter += 1
        task.task_id = self.task_counter
        
        # Actualizar conteo de usuario
        if task.user_id not in self.user_active_counts:
            self.user_active_counts[task.user_id] = 0
        self.user_active_counts[task.user_id] += 1
        
        if task.user_type == UserType.ADMIN:
            self.admin_queue.append(task)
            position = len(self.admin_queue)
            queue_type = "admin"
        else:
            self.user_queue.append(task)
            position = len(self.admin_queue) + len(self.user_queue)
            queue_type = "user"
        
        return task.task_id, position, queue_type
    
    def remove_task(self, task):
        """Remover una tarea del sistema"""
        # Remover de colas
        if task in self.admin_queue:
            self.admin_queue.remove(task)
        elif task in self.user_queue:
            self.user_queue.remove(task)
        
        # Remover de activas
        if task in self.active_tasks:
            self.active_tasks.remove(task)
        
        # Actualizar conteo de usuario
        if task.user_id in self.user_active_counts:
            self.user_active_counts[task.user_id] -= 1
            if self.user_active_counts[task.user_id] <= 0:
                del self.user_active_counts[task.user_id]
    
    def get_next_task(self):
        """Obtener siguiente tarea para procesar (prioridad admin)"""
        if self.admin_queue:
            return self.admin_queue.popleft()
        elif self.user_queue:
            return self.user_queue.popleft()
        return None
    
    def get_queue_info(self):
        """Obtener información completa de la cola"""
        return {
            "active": len(self.active_tasks),
            "max_active": self.max_active_tasks,
            "admin_pending": len(self.admin_queue),
            "user_pending": len(self.user_queue),
            "admin_queue": list(self.admin_queue),
            "user_queue": list(self.user_queue),
            "active_tasks": list(self.active_tasks),
            "user_counts": self.user_active_counts.copy()
        }
    
    def can_start_new_task(self):
        """Verificar si se puede iniciar una nueva tarea"""
        return len(self.active_tasks) < self.max_active_tasks
    
    def add_active_task(self, task):
        """Agregar tarea a procesamiento activo"""
        task.status = TaskStatus.DOWNLOADING
        task.download_start = time.time()
        self.active_tasks.append(task)
    
    def remove_active_task(self, task):
        """Remover tarea de procesamiento activo"""
        if task in self.active_tasks:
            self.active_tasks.remove(task)
    
    def update_task_status(self, task, status, progress=None):
        """Actualizar estado de una tarea"""
        task.status = status
        if progress is not None:
            task.progress = progress
        
        # Actualizar tiempos de inicio
        if status == TaskStatus.COMPRESSING and not task.compress_start:
            task.compress_start = time.time()
        elif status == TaskStatus.UPLOADING and not task.upload_start:
            task.upload_start = time.time()

# Instancia global del sistema de colas
queue_system = QueueSystem()

# ===================== WEB =====================
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
def progress_bar(percent: int, size: int = 12) -> str:
    """Crear barra de progreso visual"""
    filled = int(size * percent / 100)
    bar = "▓" * filled + "░" * (size - filled)
    return f"[{bar}]"

def get_video_duration(path: str) -> float:
    """Obtener duración exacta del video"""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "json", path
        ]
        result = subprocess.check_output(cmd)
        data = json.loads(result)
        if "streams" in data and len(data["streams"]) > 0:
            return float(data["streams"][0]["duration"])
    except:
        pass
    
    # Fallback a método alternativo
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", path
        ]
        result = subprocess.check_output(cmd)
        return float(json.loads(result)["format"]["duration"])
    except:
        return 0

def format_video_duration(seconds: float) -> str:
    """Formatear duración del video exacta"""
    if not seconds or seconds == 0:
        return "Desconocida"
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds_remain = int(seconds % 60)
    
    if hours > 0:
        return f"{hours}h {minutes}m {seconds_remain}s"
    elif minutes > 0:
        return f"{minutes}m {seconds_remain}s"
    else:
        return f"{seconds_remain}s"

async def safe_edit(msg, text):
    """Editar mensaje de forma segura"""
    try:
        await msg.edit_text(text)
    except:
        pass

def clean_files(*paths):
    """Limpiar archivos temporales"""
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except:
                pass

def format_file_size(bytes_size):
    """Formatear tamaño de archivo"""
    if bytes_size < 1024:
        return f"{bytes_size} B"
    elif bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    elif bytes_size < 1024 * 1024 * 1024:
        return f"{bytes_size / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_size / (1024 * 1024 * 1024):.1f} GB"

# ===================== MIDDLEWARE =====================
def allowed_users_only(func):
    async def wrapper(client, message):
        if message.from_user.id not in get_all_allowed_ids():
            await message.reply("🚫 **Acceso denegado**\n\nNo tienes permiso para usar este bot.")
            return
        await func(client, message)
    return wrapper

def admin_command(func):
    async def wrapper(client, message):
        if message.from_user.id not in ALLOWED_USERS["admins"]:
            await message.reply("🚫 **Acceso denegado**\n\nEste comando es solo para administradores.")
            return
        await func(client, message)
    return wrapper

# ===================== COMANDO START =====================
@app.on_message(filters.command("start"))
@allowed_users_only
async def start(_, msg):
    if msg.from_user.id in ALLOWED_USERS["admins"]:
        user_type = "👑 **Administrador**"
        instructions = "• **Prioridad máxima** en cola\n• **Sin límite** de tareas"
    else:
        user_type = "👤 **Usuario**"
        instructions = "• **1 tarea máxima** en cola\n• **Prioridad normal**"
    
    await msg.reply(
        f"🎬 **VIDEO COMPRESSOR BOT**\n"
        f"═══════════════════════\n\n"
        f"**{user_type}**\n"
        f"{instructions}\n\n"
        f"📋 **COMANDOS:**\n"
        f"• /compress - Elegir compresión\n"
        f"• /cola - Ver cola de procesamiento\n"
        f"• /estado - Ver tu estado actual\n"
        f"• /cancelar - Cancelar tu tarea\n\n"
        f"⚙️ **FLUJO DE TRABAJO:**\n"
        f"1. Usa /compress para elegir resolución\n"
        f"2. Envía el video\n"
        f"3. Espera en cola (NO se descarga inmediatamente)\n"
        f"4. Cuando sea tu turno, se procesa automáticamente"
    )

# ===================== VISUALIZACIÓN DE COLA MEJORADA =====================
@app.on_message(filters.command("cola"))
@allowed_users_only
async def show_queue(_, msg):
    queue_info = queue_system.get_queue_info()
    
    # Encabezado
    text = "📊 **COLA DE PROCESAMIENTO**\n"
    text += "═══════════════════════\n\n"
    
    # Estado del sistema
    text += "🔄 **ESTADO DEL SISTEMA**\n"
    text += f"• Procesos activos: **{queue_info['active']}/{queue_info['max_active']}**\n"
    text += f"• En espera: **{queue_info['admin_pending'] + queue_info['user_pending']}** tareas\n\n"
    
    # Tareas activas (procesando ahora)
    if queue_info["active_tasks"]:
        text += "🔥 **PROCESANDO AHORA**\n"
        text += "─────────────────────\n"
        
        for task in queue_info["active_tasks"]:
            user_icon = "👑" if task.user_type == UserType.ADMIN else "👤"
            status_icon = "📥" if task.status == TaskStatus.DOWNLOADING else "⚙️" if task.status == TaskStatus.COMPRESSING else "📤"
            
            text += (
                f"{user_icon} **{task.username}**\n"
                f"{status_icon} **{task.status.value}** {progress_bar(task.progress)} **{task.progress}%**\n"
                f"📐 {task.resolution}p | ⏱️ {task.get_current_step_time()}\n"
                f"🕐 Total: {task.get_elapsed_time()}\n"
            )
            if task.duration:
                text += f"⏳ Duración: {format_video_duration(task.duration)}\n"
            text += "\n"
    
    # Cola de administradores
    if queue_info["admin_pending"] > 0:
        text += "👑 **ADMINISTRADORES EN ESPERA**\n"
        text += "─────────────────────\n"
        
        for i, task in enumerate(queue_info["admin_queue"], 1):
            position_text = "🎯 **PRÓXIMO**" if i == 1 and queue_info["active"] < queue_info["max_active"] else f"📍 **#{i}**"
            text += (
                f"{position_text} - {task.username}\n"
                f"   📐 {task.resolution}p | ⏱️ Pendiente\n"
            )
        
        text += "\n"
    
    # Cola de usuarios
    if queue_info["user_pending"] > 0:
        text += "👤 **USUARIOS EN ESPERA**\n"
        text += "─────────────────────\n"
        
        for i, task in enumerate(queue_info["user_queue"], 1):
            position = i + queue_info["admin_pending"]
            position_text = "🎯 **PRÓXIMO**" if position == 1 and queue_info["active"] < queue_info["max_active"] else f"📍 **#{position}**"
            text += (
                f"{position_text} - {task.username}\n"
                f"   📐 {task.resolution}p | ⏱️ Pendiente\n"
            )
    
    # Si no hay nada
    if (queue_info["active"] == 0 and 
        queue_info["admin_pending"] == 0 and 
        queue_info["user_pending"] == 0):
        text += "✅ **COLA VACÍA**\n"
        text += "─────────────────────\n"
        text += "No hay tareas pendientes.\n"
        text += "Envía un video usando /compress primero\n\n"
    
    # Información de límites
    text += "📋 **LÍMITES DEL SISTEMA:**\n"
    text += "• Máximo 2 procesos simultáneos\n"
    text += "• Admins: Sin límite de tareas\n"
    text += "• Usuarios: 1 tarea máxima\n\n"
    
    # Pie de página
    text += "═══════════════════════\n"
    text += "📱 Usa /estado para ver tu progreso personal"
    
    await msg.reply(text)

# ===================== COMANDO ESTADO =====================
@app.on_message(filters.command("estado"))
@allowed_users_only
async def show_status(_, msg):
    user_id = msg.from_user.id
    queue_info = queue_system.get_queue_info()
    
    # Buscar en tareas activas
    for task in queue_info["active_tasks"]:
        if task.user_id == user_id:
            user_icon = "👑" if task.user_type == UserType.ADMIN else "👤"
            
            text = "🎯 **TU ESTADO ACTUAL**\n"
            text += "═══════════════════════\n\n"
            text += f"{user_icon} **Usuario:** {task.username}\n"
            text += f"📐 **Resolución:** {task.resolution}p\n"
            text += f"🔄 **Estado:** {task.status.value}\n"
            text += f"📊 **Progreso:** {progress_bar(task.progress)} {task.progress}%\n"
            text += f"⏱️ **Tiempo actual:** {task.get_current_step_time()}\n"
            text += f"🕐 **Tiempo total:** {task.get_elapsed_time()}\n"
            if task.duration:
                text += f"⏳ **Duración video:** {format_video_duration(task.duration)}\n"
            text += "\n✅ **Procesándose ahora**"
            
            await msg.reply(text)
            return
    
    # Buscar en colas
    position = None
    queue_type = None
    task_info = None
    
    # Buscar en cola de admin
    for i, task in enumerate(queue_info["admin_queue"], 1):
        if task.user_id == user_id:
            position = i
            queue_type = "admin"
            task_info = task
            break
    
    # Buscar en cola de usuario
    if not position:
        for i, task in enumerate(queue_info["user_queue"], 1):
            if task.user_id == user_id:
                position = i + queue_info["admin_pending"]
                queue_type = "user"
                task_info = task
                break
    
    if position and task_info:
        user_icon = "👑" if queue_type == "admin" else "👤"
        priority = "ALTA 🔴" if queue_type == "admin" else "NORMAL 🟡"
        
        text = "🎯 **TU ESTADO ACTUAL**\n"
        text += "═══════════════════════\n\n"
        text += f"{user_icon} **Usuario:** {task_info.username}\n"
        text += f"📐 **Resolución:** {task_info.resolution}p\n"
        text += f"📍 **Posición:** #{position} en cola\n"
        text += f"🎯 **Prioridad:** {priority}\n"
        text += f"⏱️ **Estado:** En espera\n"
        text += f"🕐 **Esperando:** {task_info.get_elapsed_time()}\n\n"
        
        if position == 1 and queue_info["active"] < queue_info["max_active"]:
            text += "✅ **Serás el próximo en procesar**\n"
            text += "El video se descargará cuando sea tu turno."
        else:
            text += "⏳ **Esperando turno...**\n"
            text += "El video NO se ha descargado aún."
        
        await msg.reply(text)
        return
    
    # No tiene tareas
    await msg.reply(
        "📭 **NO TIENES TAREAS ACTIVAS**\n"
        "═══════════════════════\n\n"
        "Usa /compress para configurar la compresión\n"
        "y luego envía un video para agregarlo a la cola."
    )

# ===================== GESTIÓN DE USUARIOS =====================
@app.on_message(filters.command("agregar"))
@admin_command
async def add_user(_, msg):
    """Agregar un usuario al sistema"""
    if not msg.reply_to_message and len(msg.command) < 2:
        await msg.reply(
            "📝 **USO DEL COMANDO:**\n"
            "═══════════════════════\n\n"
            "• **Responde** a un usuario con /agregar\n"
            "• **O usa** /agregar <user_id>\n\n"
            "📊 **ESTADÍSTICAS:**\n"
            f"• Administradores: {len(ALLOWED_USERS['admins'])}\n"
            f"• Usuarios normales: {len(ALLOWED_USERS['users'])}"
        )
        return
    
    user_id = None
    username = ""
    
    if msg.reply_to_message:
        user_id = msg.reply_to_message.from_user.id
        username = msg.reply_to_message.from_user.username or msg.reply_to_message.from_user.first_name or f"ID:{user_id}"
    else:
        try:
            user_id = int(msg.command[1])
            username = f"ID:{user_id}"
        except:
            await msg.reply("❌ **ID INVÁLIDA**\n\nDebe ser un número.")
            return
    
    if user_id in get_all_allowed_ids():
        await msg.reply(f"ℹ️ **USUARIO YA EXISTE**\n\nID: `{user_id}`")
        return
    
    ALLOWED_USERS["users"].append(user_id)
    save_allowed_users()
    
    await msg.reply(
        f"✅ **USUARIO AGREGADO**\n"
        f"═══════════════════════\n\n"
        f"👤 **ID:** `{user_id}`\n"
        f"📝 **Nombre:** {username}\n"
        f"🎯 **Tipo:** Usuario Normal\n\n"
        f"📊 **Total usuarios:** {len(ALLOWED_USERS['users'])}"
    )

@app.on_message(filters.command("remover"))
@admin_command
async def remove_user(_, msg):
    """Remover un usuario del sistema"""
    if not msg.reply_to_message and len(msg.command) < 2:
        await msg.reply(
            "📝 **USO DEL COMANDO:**\n"
            "═══════════════════════\n\n"
            "• **Responde** a un usuario con /remover\n"
            "• **O usa** /remover <user_id>\n\n"
            "⚠️ **NOTA:** No se pueden remover administradores"
        )
        return
    
    user_id = None
    
    if msg.reply_to_message:
        user_id = msg.reply_to_message.from_user.id
    else:
        try:
            user_id = int(msg.command[1])
        except:
            await msg.reply("❌ **ID INVÁLIDA**\n\nDebe ser un número.")
            return
    
    if user_id in ALLOWED_USERS["admins"]:
        await msg.reply("❌ **NO SE PUEDEN REMOVER ADMINISTRADORES**")
        return
    
    if user_id not in ALLOWED_USERS["users"]:
        await msg.reply(f"ℹ️ **USUARIO NO ENCONTRADO**\n\nID: `{user_id}`")
        return
    
    # Remover de lista
    ALLOWED_USERS["users"].remove(user_id)
    save_allowed_users()
    
    # Cancelar todas las tareas del usuario
    cancelled = 0
    queue_info = queue_system.get_queue_info()
    
    # Buscar y cancelar tareas pendientes
    for task in list(queue_info["admin_queue"]):
        if task.user_id == user_id:
            queue_system.admin_queue.remove(task)
            cancelled += 1
            # Notificar cancelación
            try:
                await task.status_msg.edit_text(
                    f"❌ **TAREA CANCELADA**\n"
                    f"═══════════════════════\n\n"
                    f"👤 **Usuario:** {task.username}\n"
                    f"📐 **Resolución:** {task.resolution}p\n\n"
                    f"⚠️ **Motivo:** Usuario removido del sistema"
                )
            except:
                pass
    
    for task in list(queue_info["user_queue"]):
        if task.user_id == user_id:
            queue_system.user_queue.remove(task)
            cancelled += 1
            try:
                await task.status_msg.edit_text(
                    f"❌ **TAREA CANCELADA**\n"
                    f"═══════════════════════\n\n"
                    f"👤 **Usuario:** {task.username}\n"
                    f"📐 **Resolución:** {task.resolution}p\n\n"
                    f"⚠️ **Motivo:** Usuario removido del sistema"
                )
            except:
                pass
    
    # Limpiar configuraciones
    if user_id in user_compression:
        del user_compression[user_id]
    
    await msg.reply(
        f"✅ **USUARIO REMOVIDO**\n"
        f"═══════════════════════\n\n"
        f"👤 **ID:** `{user_id}`\n"
        f"🗑️ **Tareas canceladas:** {cancelled}\n\n"
        f"📊 **Usuarios restantes:** {len(ALLOWED_USERS['users'])}"
    )

@app.on_message(filters.command("listar"))
@admin_command
async def list_users(_, msg):
    """Listar todos los usuarios permitidos"""
    text = "📋 **LISTA DE USUARIOS**\n"
    text += "═══════════════════════\n\n"
    
    text += "👑 **ADMINISTRADORES**\n"
    text += "─────────────────────\n"
    if ALLOWED_USERS["admins"]:
        for admin_id in ALLOWED_USERS["admins"]:
            text += f"• `{admin_id}`\n"
    else:
        text += "No hay administradores\n"
    
    text += "\n👤 **USUARIOS NORMALES**\n"
    text += "─────────────────────\n"
    if ALLOWED_USERS["users"]:
        for user_id in ALLOWED_USERS["users"]:
            text += f"• `{user_id}`\n"
    else:
        text += "No hay usuarios\n"
    
    text += "\n═══════════════════════\n"
    text += f"📊 **Total:** {len(get_all_allowed_ids())} usuarios"
    
    await msg.reply(text)

# ===================== COMANDO COMPRESS =====================
user_compression = {}

@app.on_message(filters.command("compress"))
@allowed_users_only
async def compress_command(_, msg):
    user_id = msg.from_user.id
    
    # Verificar si ya tiene tarea en cola
    queue_info = queue_system.get_queue_info()
    has_pending = False
    for task in queue_info["admin_queue"] + queue_info["user_queue"] + queue_info["active_tasks"]:
        if task.user_id == user_id and task.user_type == UserType.USER:
            has_pending = True
            break
    
    if user_id in user_compression:
        current = user_compression[user_id]
        if has_pending and user_id not in ALLOWED_USERS["admins"]:
            await msg.reply(
                f"⚠️ **YA TIENES UNA TAREA EN COLA**\n"
                f"═══════════════════════\n\n"
                f"📐 **Resolución actual:** {current}p\n"
                f"📊 **Estado:** Tarea pendiente en cola\n\n"
                f"❌ **No puedes agregar más tareas**\n"
                f"Los usuarios normales solo pueden tener 1 tarea a la vez.\n\n"
                f"Usa /cancelar para remover tu tarea actual."
            )
            return
        
        await msg.reply(
            f"⚙️ **CONFIGURACIÓN ACTUAL**\n"
            f"═══════════════════════\n\n"
            f"📐 **Resolución:** {current}p\n\n"
            f"¿Quieres cambiar la resolución?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Cambiar", callback_data="choose_compression")],
                [InlineKeyboardButton("✅ Mantener", callback_data=f"keep_{current}")]
            ])
        )
    else:
        await msg.reply(
            "🎯 **ELIGE RESOLUCIÓN**\n"
            "═══════════════════════\n\n"
            "Esta configuración se aplicará al próximo video.\n\n"
            "👇 **Selecciona una opción:**",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("360p", callback_data="set_360"),
                    InlineKeyboardButton("480p", callback_data="set_480"),
                    InlineKeyboardButton("720p", callback_data="set_720")
                ]
            ])
        )

@app.on_callback_query(filters.regex(r"set_(360|480|720)"))
@allowed_users_only
async def set_compression(_, cb):
    res = cb.data.split("_")[1]
    user_id = cb.from_user.id
    user_compression[user_id] = res
    
    user_type = "👑 Administrador" if user_id in ALLOWED_USERS["admins"] else "👤 Usuario"
    username = cb.from_user.username or cb.from_user.first_name or f"ID:{user_id}"
    
    await cb.message.edit_text(
        f"✅ **CONFIGURACIÓN GUARDADA**\n"
        f"═══════════════════════\n\n"
        f"📐 **Resolución:** {res}p\n"
        f"👤 **Usuario:** {username}\n"
        f"🎯 **Tipo:** {user_type}\n\n"
        f"📤 **Ahora puedes enviar tu video**\n\n"
        f"📋 **INFORMACIÓN IMPORTANTE:**\n"
        f"• El video se agregará a la cola\n"
        f"• **NO se descargará inmediatamente**\n"
        f"• Se procesará cuando sea tu turno\n"
        f"• Usa /cola para ver tu posición\n"
        f"• Usa /estado para ver tu progreso"
    )

@app.on_callback_query(filters.regex(r"keep_(360|480|720)"))
@allowed_users_only
async def keep_compression(_, cb):
    res = cb.data.split("_")[1]
    user_id = cb.from_user.id
    user_type = "👑 Administrador" if user_id in ALLOWED_USERS["admins"] else "👤 Usuario"
    username = cb.from_user.username or cb.from_user.first_name or f"ID:{user_id}"
    
    await cb.message.edit_text(
        f"✅ **CONFIGURACIÓN MANTENIDA**\n"
        f"═══════════════════════\n\n"
        f"📐 **Resolución:** {res}p\n"
        f"👤 **Usuario:** {username}\n"
        f"🎯 **Tipo:** {user_type}\n\n"
        f"📤 **Ahora puedes enviar tu video**\n\n"
        f"📋 **INFORMACIÓN IMPORTANTE:**\n"
        f"• El video se agregará a la cola\n"
        f"• **NO se descargará inmediatamente**\n"
        f"• Se procesará cuando sea tu turno"
    )

@app.on_callback_query(filters.regex("choose_compression"))
@allowed_users_only
async def callback_choose_compression(_, cb):
    await compress_command(_, cb.message)

# ===================== RECIBIR VIDEO =====================
@app.on_message(filters.video | filters.document)
@allowed_users_only
async def receive_video(_, msg):
    user_id = msg.from_user.id
    
    # Verificar configuración de compresión
    if user_id not in user_compression:
        await msg.reply(
            "⚠️ **CONFIGURACIÓN REQUERIDA**\n"
            "═══════════════════════\n\n"
            "Debes configurar la compresión primero.\n\n"
            "📋 **Usa el comando:**\n"
            "`/compress`\n\n"
            "📝 **FLUJO CORRECTO:**\n"
            "1. /compress (elige resolución)\n"
            "2. Envía el video\n"
            "3. Espera en la cola\n"
            "4. Se procesa automáticamente"
        )
        return
    
    res = user_compression[user_id]
    user_type = UserType.ADMIN if user_id in ALLOWED_USERS["admins"] else UserType.USER
    
    # Verificar límites de usuario
    if not queue_system.can_user_add_task(user_id, user_type):
        await msg.reply(
            "⚠️ **LÍMITE DE TAREAS ALCANZADO**\n"
            "═══════════════════════\n\n"
            "Los usuarios normales solo pueden tener **1 tarea** en cola.\n\n"
            "📋 **OPCIONES:**\n"
            "• Espera a que tu tarea actual termine\n"
            "• Usa /cancelar para remover tu tarea actual\n"
            "• Usa /cola para ver tu estado actual"
        )
        return
    
    # Obtener información del archivo
    media = msg.video or msg.document
    file_id = media.file_id
    
    # Obtener tamaño del archivo si está disponible
    file_size = media.file_size if hasattr(media, 'file_size') else 0
    file_size_text = format_file_size(file_size) if file_size > 0 else "Desconocido"
    
    # Obtener username
    username = msg.from_user.username or msg.from_user.first_name or f"ID:{user_id}"
    
    # Crear tarea (SOLO CON REFERENCIA, SIN DESCARGAR)
    task = CompressionTask(
        user_id=user_id,
        username=username,
        user_type=user_type,
        message=msg,
        file_id=file_id,
        resolution=res
    )
    
    # Crear mensaje de estado inicial
    status = await msg.reply(
        f"📥 **VIDEO RECIBIDO**\n"
        f"═══════════════════════\n\n"
        f"👤 **Usuario:** {task.username}\n"
        f"📐 **Resolución:** {res}p\n"
        f"💾 **Tamaño:** {file_size_text}\n"
        f"🎯 **Agregando a cola...**"
    )
    
    task.status_msg = status
    
    # Agregar a la cola
    task_id, position, queue_type = queue_system.add_task(task)
    
    priority = "ALTA 🔴" if queue_type == "admin" else "NORMAL 🟡"
    position_text = "🎯 **PRÓXIMO**" if position == 1 else f"📍 **#{position}**"
    
    await safe_edit(
        status,
        f"✅ **AGREGADO A LA COLA**\n"
        f"═══════════════════════\n\n"
        f"👤 **Usuario:** {task.username}\n"
        f"📐 **Resolución:** {res}p\n"
        f"💾 **Tamaño:** {file_size_text}\n"
        f"{position_text} en cola\n"
        f"🎯 **Prioridad:** {priority}\n"
        f"⏱️ **Estado:** En espera\n\n"
        f"📋 **INFORMACIÓN:**\n"
        f"• **NO se ha descargado** el video\n"
        f"• Se descargará cuando sea tu turno\n"
        f"• Usa /cola para ver progreso general\n"
        f"• Usa /estado para ver tu progreso"
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
            
            queue_system.add_active_task(task)
            asyncio.create_task(process_task(task))
            await asyncio.sleep(1)  # Pequeña pausa entre tareas

async def process_task(task):
    try:
        # PASO 1: Descargar video (SOLO CUANDO ES SU TURNO)
        await download_video(task)
        
        # PASO 2: Comprimir video
        await compress_video(task)
        
        # PASO 3: Subir video
        await upload_video(task)
        
        # Completado
        task.status = TaskStatus.COMPLETED
        task.progress = 100
        
        total_time = task.get_elapsed_time()
        
        await safe_edit(
            task.status_msg,
            f"✅ **PROCESO COMPLETADO**\n"
            f"═══════════════════════\n\n"
            f"👤 **Usuario:** {task.username}\n"
            f"📐 **Resolución:** {task.resolution}p\n"
            f"⏱️ **Tiempo total:** {total_time}\n"
        )
        
        if task.duration:
            await safe_edit(
                task.status_msg,
                f"✅ **PROCESO COMPLETADO**\n"
                f"═══════════════════════\n\n"
                f"👤 **Usuario:** {task.username}\n"
                f"📐 **Resolución:** {task.resolution}p\n"
                f"⏱️ **Tiempo total:** {total_time}\n"
                f"⏳ **Duración original:** {format_video_duration(task.duration)}\n\n"
                f"🎉 **¡Video procesado exitosamente!**"
            )
        else:
            await safe_edit(
                task.status_msg,
                f"✅ **PROCESO COMPLETADO**\n"
                f"═══════════════════════\n\n"
                f"👤 **Usuario:** {task.username}\n"
                f"📐 **Resolución:** {task.resolution}p\n"
                f"⏱️ **Tiempo total:** {total_time}\n\n"
                f"🎉 **¡Video procesado exitosamente!**"
            )
        
        # Esperar y eliminar mensaje
        await asyncio.sleep(10)
        try:
            await task.status_msg.delete()
        except:
            pass
        
    except Exception as e:
        task.status = TaskStatus.ERROR
        error_msg = str(e)[:100]
        
        await safe_edit(
            task.status_msg,
            f"❌ **ERROR EN PROCESO**\n"
            f"═══════════════════════\n\n"
            f"👤 **Usuario:** {task.username}\n"
            f"📐 **Resolución:** {task.resolution}p\n"
            f"⏱️ **Tiempo:** {task.get_elapsed_time()}\n\n"
            f"💥 **Error:** {error_msg}\n\n"
            f"⚠️ **La tarea ha sido cancelada**"
        )
        
        await asyncio.sleep(10)
        try:
            await task.status_msg.delete()
        except:
            pass
    finally:
        # Limpiar archivos temporales
        clean_files(task.input_path, task.output_path)
        
        # Remover de tareas activas
        queue_system.remove_active_task(task)
        
        # Liberar espacio de usuario
        if task.user_id in queue_system.user_active_counts:
            queue_system.user_active_counts[task.user_id] -= 1
            if queue_system.user_active_counts[task.user_id] <= 0:
                del queue_system.user_active_counts[task.user_id]
        
        # Procesar siguiente tarea
        asyncio.create_task(process_queue())

# ===================== FUNCIONES DE PROCESAMIENTO =====================
async def download_video(task):
    """Descargar video SOLO cuando es su turno"""
    queue_system.update_task_status(task, TaskStatus.DOWNLOADING, 0)
    task.download_start = time.time()
    
    # Crear nombre de archivo único
    task.input_path = f"{DOWNLOAD_DIR}/{task.user_id}_{int(time.time())}_{task.file_id}.mp4"
    
    await safe_edit(
        task.status_msg,
        f"📥 **DESCARGANDO VIDEO**\n"
        f"═══════════════════════\n\n"
        f"👤 **Usuario:** {task.username}\n"
        f"📐 **Resolución:** {task.resolution}p\n"
        f"⏱️ **Tiempo:** {task.get_current_step_time()}\n\n"
        f"{progress_bar(0)} 0%"
    )
    
    last_update = time.time()
    
    async def download_progress(current, total):
        nonlocal last_update
        if total == 0:
            return
        
        percent = int(current * 100 / total)
        
        if time.time() - last_update >= 1:
            last_update = time.time()
            bar = progress_bar(percent)
            
            queue_system.update_task_status(task, TaskStatus.DOWNLOADING, percent)
            
            await safe_edit(
                task.status_msg,
                f"📥 **DESCARGANDO VIDEO**\n"
                f"═══════════════════════\n\n"
                f"👤 **Usuario:** {task.username}\n"
                f"📐 **Resolución:** {task.resolution}p\n"
                f"⏱️ **Tiempo:** {task.get_current_step_time()}\n\n"
                f"{bar} {percent}%"
            )
    
    # Descargar el video (SOLO AHORA)
    try:
        await task.message.download(
            file_name=task.input_path,
            progress=download_progress
        )
    except Exception as e:
        raise Exception(f"Error al descargar: {str(e)}")
    
    queue_system.update_task_status(task, TaskStatus.DOWNLOADING, 100)
    
    # Obtener duración exacta del video
    try:
        task.duration = get_video_duration(task.input_path)
        duration_text = format_video_duration(task.duration)
        
        # Obtener tamaño del archivo
        file_size = os.path.getsize(task.input_path) if os.path.exists(task.input_path) else 0
        file_size_text = format_file_size(file_size)
        
        await safe_edit(
            task.status_msg,
            f"✅ **DESCARGA COMPLETADA**\n"
            f"═══════════════════════\n\n"
            f"👤 **Usuario:** {task.username}\n"
            f"📐 **Resolución:** {task.resolution}p\n"
            f"⏳ **Duración:** {duration_text}\n"
            f"💾 **Tamaño:** {file_size_text}\n"
            f"🕐 **Tiempo descarga:** {task.get_current_step_time()}\n\n"
            f"⚙️ **Siguiente:** Comprimiendo..."
        )
    except Exception as e:
        await safe_edit(
            task.status_msg,
            f"✅ **DESCARGA COMPLETADA**\n"
            f"═══════════════════════\n\n"
            f"👤 **Usuario:** {task.username}\n"
            f"📐 **Resolución:** {task.resolution}p\n"
            f"🕐 **Tiempo descarga:** {task.get_current_step_time()}\n\n"
            f"⚙️ **Siguiente:** Comprimiendo..."
        )

async def compress_video(task):
    """Comprimir video descargado"""
    queue_system.update_task_status(task, TaskStatus.COMPRESSING, 0)
    task.compress_start = time.time()
    
    scale_map = {"360": "640:360", "480": "854:480", "720": "1280:720"}
    scale = scale_map[task.resolution]
    task.output_path = f"{OUTPUT_DIR}/{task.user_id}_{int(time.time())}_{task.resolution}.mp4"
    
    await safe_edit(
        task.status_msg,
        f"⚙️ **COMPRIMIENDO VIDEO**\n"
        f"═══════════════════════\n\n"
        f"👤 **Usuario:** {task.username}\n"
        f"📐 **Resolución:** {task.resolution}p ({scale})\n"
        f"⏱️ **Tiempo:** {task.get_current_step_time()}\n\n"
        f"{progress_bar(0)} 0%"
    )
    
    # Comando de compresión
    cmd = [
        "ffmpeg", "-y",
        "-i", task.input_path,
        "-vf", f"scale={scale},fps=23",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "30",
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-movflags", "+faststart",
        "-c:a", "aac",
        "-b:a", "60k",
        "-ac", "1",
        "-threads", "0",
        "-x264-params", "scenecut=0:open_gop=0",
        "-progress", "pipe:1",
        "-nostats",
        task.output_path
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
    
    # Calcular duración para progreso exacto
    total_duration = task.duration or 0
    
    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        
        if line:
            match = time_regex.search(line)
            if match and total_duration > 0:
                current_time = int(match.group(1)) / 1_000_000
                progress = min(99, int(current_time * 100 / total_duration))
                
                if time.time() - last_update >= 1:
                    last_update = time.time()
                    bar = progress_bar(progress)
                    
                    queue_system.update_task_status(task, TaskStatus.COMPRESSING, progress)
                    
                    await safe_edit(
                        task.status_msg,
                        f"⚙️ **COMPRIMIENDO VIDEO**\n"
                        f"═══════════════════════\n\n"
                        f"👤 **Usuario:** {task.username}\n"
                        f"📐 **Resolución:** {task.resolution}p\n"
                        f"⏱️ **Tiempo:** {task.get_current_step_time()}\n\n"
                        f"{bar} {progress}%"
                    )
        
        await asyncio.sleep(0.1)
    
    process.wait()
    
    if process.returncode != 0:
        error_output = process.stderr.read() if process.stderr else "Error desconocido"
        raise Exception(f"Error en compresión: {error_output[:100]}")
    
    queue_system.update_task_status(task, TaskStatus.COMPRESSING, 100)
    
    await safe_edit(
        task.status_msg,
        f"✅ **COMPRESIÓN COMPLETADA**\n"
        f"═══════════════════════\n\n"
        f"👤 **Usuario:** {task.username}\n"
        f"📐 **Resolución:** {task.resolution}p\n"
        f"🕐 **Tiempo compresión:** {task.get_current_step_time()}\n\n"
        f"📤 **Siguiente:** Subiendo..."
    )

async def upload_video(task):
    """Subir video comprimido"""
    queue_system.update_task_status(task, TaskStatus.UPLOADING, 0)
    task.upload_start = time.time()
    
    await safe_edit(
        task.status_msg,
        f"📤 **SUBIENDO VIDEO**\n"
        f"═══════════════════════\n\n"
        f"👤 **Usuario:** {task.username}\n"
        f"📐 **Resolución:** {task.resolution}p\n"
        f"⏱️ **Tiempo:** {task.get_current_step_time()}\n\n"
        f"{progress_bar(0)} 0%"
    )
    
    last_update = time.time()
    
    async def upload_progress(current, total):
        nonlocal last_update
        if total == 0:
            return
        
        percent = int(current * 100 / total)
        
        if time.time() - last_update >= 1:
            last_update = time.time()
            bar = progress_bar(percent)
            
            queue_system.update_task_status(task, TaskStatus.UPLOADING, percent)
            
            await safe_edit(
                task.status_msg,
                f"📤 **SUBIENDO VIDEO**\n"
                f"═══════════════════════\n\n"
                f"👤 **Usuario:** {task.username}\n"
                f"📐 **Resolución:** {task.resolution}p\n"
                f"⏱️ **Tiempo:** {task.get_current_step_time()}\n\n"
                f"{bar} {percent}%"
            )
    
    try:
        # Obtener tamaño del archivo comprimido
        compressed_size = os.path.getsize(task.output_path) if os.path.exists(task.output_path) else 0
        compressed_size_text = format_file_size(compressed_size)
        
        user_type_text = "👑 Administrador" if task.user_type == UserType.ADMIN else "👤 Usuario"
        
        caption = f"✅ **Video comprimido a {task.resolution}p**\n"
        caption += f"═══════════════════════\n\n"
        caption += f"👤 **Enviado por:** {task.username}\n"
        caption += f"🎯 **Tipo:** {user_type_text}\n"
        caption += f"⏱️ **Tiempo total:** {task.get_elapsed_time()}\n"
        caption += f"💾 **Tamaño comprimido:** {compressed_size_text}\n"
        
        if task.duration:
            caption += f"⏳ **Duración original:** {format_video_duration(task.duration)}\n"
        
        await task.message.reply_video(
            video=task.output_path,
            caption=caption,
            supports_streaming=True,
            progress=upload_progress
        )
        
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
    task_to_cancel = None
    
    # Buscar en tareas activas
    for task in queue_info["active_tasks"]:
        if task.user_id == user_id:
            task_to_cancel = task
            break
    
    # Buscar en cola de administradores
    if not task_to_cancel:
        for task in queue_info["admin_queue"]:
            if task.user_id == user_id:
                task_to_cancel = task
                break
    
    # Buscar en cola de usuarios
    if not task_to_cancel:
        for task in queue_info["user_queue"]:
            if task.user_id == user_id:
                task_to_cancel = task
                break
    
    if task_to_cancel:
        # Remover del sistema
        queue_system.remove_task(task_to_cancel)
        
        # Notificar cancelación
        await safe_edit(
            task_to_cancel.status_msg,
            f"❌ **TAREA CANCELADA**\n"
            f"═══════════════════════\n\n"
            f"👤 **Usuario:** {task_to_cancel.username}\n"
            f"📐 **Resolución:** {task_to_cancel.resolution}p\n"
            f"⏱️ **Tiempo en cola:** {task_to_cancel.get_elapsed_time()}\n\n"
            f"⚠️ **Cancelada por el usuario**"
        )
        
        # Limpiar archivos si se estaban procesando
        if task_to_cancel.input_path and os.path.exists(task_to_cancel.input_path):
            clean_files(task_to_cancel.input_path)
        if task_to_cancel.output_path and os.path.exists(task_to_cancel.output_path):
            clean_files(task_to_cancel.output_path)
        
        # Eliminar configuración de compresión
        if user_id in user_compression:
            del user_compression[user_id]
        
        cancelled = True
    
    if cancelled:
        await msg.reply(
            "✅ **TAREA CANCELADA**\n"
            "═══════════════════════\n\n"
            "Tu tarea ha sido removida de la cola.\n"
            "Los archivos temporales han sido limpiados."
        )
        
        # Intentar procesar siguiente tarea
        asyncio.create_task(process_queue())
    else:
        await msg.reply(
            "ℹ️ **NO HAY TAREAS**\n"
            "═══════════════════════\n\n"
            "No tienes tareas pendientes en la cola."
        )

# ===================== COMANDO LIMPIAR COLA =====================
@app.on_message(filters.command("limpiarcola"))
@admin_command
async def clear_queue(_, msg):
    queue_info = queue_system.get_queue_info()
    total_pending = queue_info["admin_pending"] + queue_info["user_pending"]
    
    # Notificar cancelación a todos los usuarios
    for task in list(queue_info["admin_queue"]):
        try:
            await task.status_msg.edit_text(
                f"❌ **TAREA CANCELADA**\n"
                f"═══════════════════════\n\n"
                f"👤 **Usuario:** {task.username}\n"
                f"📐 **Resolución:** {task.resolution}p\n\n"
                f"⚠️ **Motivo:** Cola limpiada por administrador"
            )
        except:
            pass
    
    for task in list(queue_info["user_queue"]):
        try:
            await task.status_msg.edit_text(
                f"❌ **TAREA CANCELADA**\n"
                f"═══════════════════════\n\n"
                f"👤 **Usuario:** {task.username}\n"
                f"📐 **Resolución:** {task.resolution}p\n\n"
                f"⚠️ **Motivo:** Cola limpiada por administrador"
            )
        except:
            pass
    
    # Limpiar colas
    queue_system.admin_queue.clear()
    queue_system.user_queue.clear()
    
    # Limpiar configuraciones de usuarios en cola
    for task in list(queue_info["admin_queue"]) + list(queue_info["user_queue"]):
        if task.user_id in user_compression:
            del user_compression[task.user_id]
    
    await msg.reply(
        f"✅ **COLA LIMPIADA**\n"
        f"═══════════════════════\n\n"
        f"🗑️ **Tareas eliminadas:** {total_pending}\n"
        f"🔄 **Procesos activos:** {queue_info['active']} (no afectados)\n\n"
        f"⚠️ **Nota:** Los procesos en curso continúan normalmente."
    )

# ===================== COMANDO AYUDA =====================
@app.on_message(filters.command("ayuda"))
@allowed_users_only
async def show_help(_, msg):
    if msg.from_user.id in ALLOWED_USERS["admins"]:
        admin_commands = "\n".join([
            "• /agregar - Agregar usuario",
            "• /remover - Remover usuario", 
            "• /listar - Listar usuarios",
            "• /limpiarcola - Limpiar toda la cola"
        ])
        admin_section = f"\n👑 **COMANDOS DE ADMIN:**\n{admin_commands}"
    else:
        admin_section = ""
    
    await msg.reply(
        f"📚 **AYUDA - COMANDOS DISPONIBLES**\n"
        f"═══════════════════════\n\n"
        f"🎯 **PARA TODOS:**\n"
        f"• /start - Información del bot\n"
        f"• /compress - Elegir resolución (360p/480p/720p)\n"
        f"• /cola - Ver cola de procesamiento\n"
        f"• /estado - Ver tu estado actual\n"
        f"• /cancelar - Cancelar tu tarea\n"
        f"• /ayuda - Mostrar esta ayuda\n"
        f"{admin_section}\n\n"
        f"📋 **LÍMITES DEL SISTEMA:**\n"
        f"• Máximo 2 procesos simultáneos\n"
        f"• Admins: Sin límite de tareas\n"
        f"• Usuarios: 1 tarea máxima\n\n"
        f"⚙️ **FLUJO DE TRABAJO:**\n"
        f"1. Usa /compress para elegir resolución\n"
        f"2. Envía el video\n"
        f"3. Se agrega a cola (NO se descarga)\n"
        f"4. Cuando sea tu turno, se procesa automáticamente"
    )

# ===================== MAIN =====================
if __name__ == "__main__":
    print("🎬 Iniciando Video Compressor Bot...")
    print(f"👑 Administradores: {len(ALLOWED_USERS['admins'])}")
    print(f"👤 Usuarios normales: {len(ALLOWED_USERS['users'])}")
    print(f"⚙️ Máximo procesos simultáneos: 2")
    print(f"📁 Descargas: {DOWNLOAD_DIR}")
    print(f"📁 Salida: {OUTPUT_DIR}")
    
    # Iniciar servidor web en segundo plano
    threading.Thread(target=run_web, daemon=True).start()
    
    # Iniciar bot
    app.run()
