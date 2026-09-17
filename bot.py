‏import asyncio
‏import json
‏import logging
‏import os
‏import re
‏import shutil
‏import tempfile
‏import time
‏from dataclasses import dataclass
‏from pathlib import Path
‏from typing import Optional
‏from urllib.parse import urlparse
‏
‏import requests
‏import yt_dlp
‏
‏from telegram import (
‏    Update,
‏    InlineKeyboardButton,
‏    InlineKeyboardMarkup,
‏    InputMediaPhoto,
‏)
‏from telegram.constants import ChatAction
‏from telegram.ext import (
‏    Application,
‏    ApplicationBuilder,
‏    CallbackQueryHandler,
‏    CommandHandler,
‏    ContextTypes,
‏    MessageHandler,
‏    filters,
‏)
‏
‏=========================================================
‏إعدادات عامة
‏=========================================================
‏
‏logging.basicConfig(
‏    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
‏    level=logging.INFO,
‏)
‏
‏logger = logging.getLogger(name)
‏
‏BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
‏
‏if not BOT_TOKEN:
‏    raise RuntimeError("BOT_TOKEN غير موجود في متغيرات البيئة")
‏
‏معرفات المشرفين، مثال:
‏ADMIN_IDS=123456789,987654321
‏ADMIN_IDS = {
‏    int(x.strip())
‏    for x in os.getenv("ADMIN_IDS", "").split(",")
‏    if x.strip().isdigit()
‏}
‏
‏MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "150"))
‏MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
‏
‏MAX_CONCURRENT_DOWNLOADS = int(
‏    os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")
‏)
‏
‏MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "30"))
‏
‏MAX_IMAGES_PER_REQUEST = int(
‏    os.getenv("MAX_IMAGES_PER_REQUEST", "10")
‏)
‏
‏DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "600"))
‏
‏رابط Render العام، مثال:
‏https://your-bot.onrender.com
‏RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
‏
‏مسار سري للـ Webhook
‏WEBHOOK_SECRET = os.getenv(
‏    "WEBHOOK_SECRET",
‏    "telegram-webhook-secret-2026"
‏).strip()
‏
‏=========================================================
‏بيانات مؤقتة في الذاكرة
‏=========================================================
‏
‏queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
‏
‏download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
‏
‏user_preferences = {}
‏active_jobs = {}
‏stats = {
‏    "received": 0,
‏    "completed": 0,
‏    "failed": 0,
‏    "cancelled": 0,
‏}
‏
‏worker_tasks = []
‏
‏
‏@dataclass
‏class DownloadJob:
‏    user_id: int
‏    chat_id: int
‏    url: str
‏    mode: str
‏
‏
