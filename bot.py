#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Social Media Downloader
يدعم الروابط العامة من:
TikTok - Instagram - Facebook

الميزات:
- تحميل الفيديو بجودة يختارها المستخدم.
- تحميل الصور المتعددة.
- إرسال الصور في مجموعات، كل مجموعة 10 صور.
- تحميل كل الوسائط المتاحة.
- إرسال وصف المنشور والهاشتاغات إن توفرت.
- إرسال الصوت المستقل إذا وفره المصدر.
- فك روابط TikTok المختصرة.
- قائمة انتظار للتحميلات.
- تحديد عدد التحميلات المتزامنة.
- تنظيف الملفات المؤقتة.
- فحص الاشتراك الإجباري بالقنوات إن تم تفعيله.
- مناسب للتشغيل على Render.
"""

import asyncio
import html
import json
import logging
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
import yt_dlp

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# الإعدادات
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# مثال:
# REQUIRED_CHANNELS=@channel1,@channel2
REQUIRED_CHANNELS = [
    item.strip()
    for item in os.getenv("REQUIRED_CHANNELS", "").split(",")
    if item.strip()
]

# أرقام مشرفي البوت، مثال:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_IDS", "").split(",")
    if item.strip().isdigit()
}

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))
MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")
)
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "20"))
MAX_IMAGES_PER_REQUEST = int(
    os.getenv("MAX_IMAGES_PER_REQUEST", "50")
)
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "240"))

# Telegram يسمح بحد أقصى 10 عناصر في مجموعة الصور.
MEDIA_GROUP_SIZE = 10

# مجلد الملفات المؤقتة
TEMP_ROOT = Path(
    os.getenv("TEMP_DOWNLOAD_DIR", "/tmp/social_downloader")
)
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

# ============================================================
# السجلات
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("social-downloader")

# ============================================================
# الحالات والبيانات
# ============================================================


@dataclass
class DownloadTask:
    user_id: int
    chat_id: int
    url: str
    mode: str = "all"
    quality: str = "best"
    task_id: str = field(
        default_factory=lambda: uuid.uuid4().hex[:10]
    )


download_queue: asyncio.Queue[DownloadTask] = asyncio.Queue(
    maxsize=MAX_QUEUE_SIZE
)

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

user_modes: dict[int, str] = {}
user_qualities: dict[int, str] = {}

queue_workers_started = False

# ============================================================
# أدوات عامة
# ============================================================


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def safe_filename(name: str, default: str = "media") -> str:
    """تنظيف اسم الملف من الرموز غير المناسبة."""
    name = str(name or "").strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(". ")
    return (name[:150] or default)


def format_bytes(value: Optional[int]) -> str:
    if not value:
        return "غير معروف"

    value = float(value)
    units = ["B", "KB", "MB", "GB"]

    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} TB"


def is_supported_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]

        supported_hosts = (
            "tiktok.com",
            "vt.tiktok.com",
            "vm.tiktok.com",
            "instagram.com",
            "instagr.am",
            "facebook.com",
            "fb.watch",
            "m.facebook.com",
            "www.facebook.com",
            "web.facebook.com",
        )

        return any(
            host == item or host.endswith("." + item)
            for item in supported_hosts
        )
    except Exception:
        return False


def extract_url(text: str) -> Optional[str]:
    """استخراج أول رابط من الرسالة."""
    if not text:
        return None

    pattern = r"https?://[^\s<>\"]+"
    match = re.search(pattern, text)

    if not match:
        return None

    url = match.group(0).rstrip(".,!?)]}")
    return url


def platform_name(url: str) -> str:
    host = urlparse(url).netloc.lower()

    if "tiktok" in host:
        return "TikTok"

    if "instagram" in host or "instagr.am" in host:
        return "Instagram"

    if "facebook" in host or "fb.watch" in host:
        return "Facebook"

    return "الموقع"


def split_caption(text: str, limit: int = 1000) -> list[str]:
    """تقسيم النص الطويل حتى لا يتجاوز حدود Telegram."""
    if not text:
        return []

    return [
        text[i:i + limit]
        for i in range(0, len(text), limit)
    ]


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("فشل حذف %s: %s", path, exc)


# ============================================================
# فك الروابط المختصرة
# ============================================================


def resolve_url_sync(url: str) -> str:
    """
    فك روابط TikTok المختصرة.
    لا نستخدم allow_redirects بشكل مفرط، ونضع مهلة.
    """
    host = urlparse(url).netloc.lower()

    if not (
        "vt.tiktok.com" in host
        or "vm.tiktok.com" in host
        or "tiktok.com" in host
    ):
        return url

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13) "
                    "AppleWebKit/537.36 Chrome/125 Mobile Safari/537.36"
                )
            },
            allow_redirects=True,
            timeout=15,
        )

        final_url = response.url or url

        if "tiktok.com" in final_url:
            return final_url

    except requests.RequestException as exc:
        logger.warning("تعذر فك الرابط المختصر: %s", exc)

    return url


async def get_url_candidates(url: str) -> list[str]:
    resolved = await asyncio.to_thread(resolve_url_sync, url)

    candidates = [url]

    if resolved and resolved not in candidates:
        candidates.append(resolved)

    return candidates


# ============================================================
# إعدادات yt-dlp
# ============================================================


def quality_format(quality: str) -> str:
    """
    اختيار صيغة الفيديو.
    نستخدم bestvideo+bestaudio مع fallback إلى best.
    """
    formats = {
        "360": (
            "bestvideo[height<=360]+bestaudio/"
            "best[height<=360]/best"
        ),
        "480": (
            "bestvideo[height<=480]+bestaudio/"
            "best[height<=480]/best"
        ),
        "720": (
            "bestvideo[height<=720]+bestaudio/"
            "best[height<=720]/best"
        ),
        "1080": (
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080]/best"
        ),
        "best": (
            "bestvideo+bestaudio/"
            "best"
        ),
    }

    return formats.get(quality, formats["best"])


def common_ydl_options(
    output_dir: Path,
    quality: str = "best",
) -> dict[str, Any]:
    """
    إعدادات عامة لاستخراج الفيديو والصور.
    """

    return {
        "outtmpl": str(output_dir / "%(title).120s_%(id)s.%(ext)s"),
        "format": quality_format(quality),
        "merge_output_format": "mp4",
        "noplaylist": False,
        "ignoreerrors": True,
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "reconnect": True,
        "continuedl": True,
        "overwrites": False,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "noplaylist": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Mobile) "
                "AppleWebKit/537.36 "
                "Chrome/125.0.0.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        },
    }


def build_video_options(
    output_dir: Path,
    quality: str,
) -> dict[str, Any]:
    options = common_ydl_options(output_dir, quality)
    options.update(
        {
            "noplaylist": True,
            "playlistend": 1,
        }
    )
    return options


def build_media_options(
    output_dir: Path,
    quality: str,
) -> dict[str, Any]:
    """
    استخراج الوسائط المتعددة إن كان المستخرج يدعمها.
    """
    options = common_ydl_options(output_dir, quality)
    options.update(
        {
            "noplaylist": False,
            "playlistend": MAX_IMAGES_PER_REQUEST,
        }
    )
    return options


def build_info_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "retries": 2,
        "socket_timeout": 25,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Mobile) "
                "AppleWebKit/537.36 Chrome/125 Safari/537.36"
            )
        },
    }


# ============================================================
# استخراج معلومات الرابط
# ============================================================


def extract_info_sync(
    url: str,
    download: bool = False,
    options: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    try:
        with yt_dlp.YoutubeDL(options or build_info_options()) as ydl:
            return ydl.extract_info(url, download=download)
    except Exception as exc:
        logger.warning("خطأ في استخراج المعلومات: %s", exc)
        return None


async def extract_info(
    url: str,
    download: bool = False,
    options: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    return await asyncio.to_thread(
        extract_info_sync,
        url,
        download,
        options,
    )


def collect_entries(info: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    جمع العناصر من نتيجة yt-dlp.
    """
    if not info:
        return []

    entries = info.get("entries")

    if entries:
        result = []

        for entry in entries:
            if entry:
                result.append(entry)

        return result

    return [info]


def get_description(info: Optional[dict[str, Any]]) -> str:
    if not info:
        return ""

    description = (
        info.get("description")
        or info.get("title")
        or ""
    )

    uploader = (
        info.get("uploader")
        or info.get("channel")
        or info.get("creator")
        or ""
    )

    webpage_url = info.get("webpage_url") or ""

    parts = []

    if uploader:
        parts.append(f"👤 الحساب: {uploader}")

    if description:
        parts.append(f"📝 الوصف:\n{description}")

    if webpage_url:
        parts.append(f"🔗 الرابط:\n{webpage_url}")

    return "\n\n".join(parts)


def get_media_urls_from_info(
    info: Optional[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """
    محاولة جمع روابط الصور والفيديوهات من المعلومات.
    هذه الروابط قد تكون مؤقتة، لذلك يجب استخدامها مباشرة.
    """
    images: list[str] = []
    videos: list[str] = []

    for entry in collect_entries(info):
        ext = (entry.get("ext") or "").lower()
        url = entry.get("url")

        if not url:
            continue

        if ext in {"jpg", "jpeg", "png", "webp", "gif"}:
            images.append(url)
        elif ext in {"mp4", "mov", "webm", "m4v"}:
            videos.append(url)

        thumbnails = entry.get("thumbnails") or []

        # لا نضيف thumbnails تلقائيًا إذا كان هناك فيديو،
        # لأنها قد تكون صور معاينة وليست صور المنشور.
        if not url and thumbnails:
            for thumbnail in thumbnails:
                thumb_url = thumbnail.get("url")
                if thumb_url:
                    images.append(thumb_url)

    return list(dict.fromkeys(images)), list(dict.fromkeys(videos))


# ============================================================
# تنزيل الملفات
# ============================================================


def download_with_ytdlp_sync(
    url: str,
    output_dir: Path,
    quality: str,
    mode: str,
) -> tuple[Optional[dict[str, Any]], list[Path]]:
    """
    تنزيل الملفات باستخدام yt-dlp.
    """
    before = set(output_dir.iterdir())

    if mode == "video":
        options = build_video_options(output_dir, quality)
    else:
        options = build_media_options(output_dir, quality)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        logger.exception("فشل التنزيل من %s: %s", url, exc)
        return None, []

    after = set(output_dir.iterdir())
    files = [
        item for item in (after - before)
        if item.is_file()
    ]

    # ترتيب الملفات حسب الاسم
    files.sort(key=lambda item: item.name.lower())

    return info, files


async def download_with_ytdlp(
    url: str,
    output_dir: Path,
    quality: str,
    mode: str,
) -> tuple[Optional[dict[str, Any]], list[Path]]:
    return await asyncio.to_thread(
        download_with_ytdlp_sync,
        url,
        output_dir,
        quality,
        mode,
    )


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
    }


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in {
        ".mp4",
        ".mkv",
        ".mov",
        ".webm",
        ".m4v",
        ".avi",
    }


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in {
        ".mp3",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wav",
        ".flac",
    }


def is_too_large(path: Path) -> bool:
    return file_size(path) > MAX_FILE_SIZE_MB * 1024 * 1024


# ============================================================
# فحص الاشتراك
# ============================================================


async def check_required_channels(
    bot: Bot,
    user_id: int,
) -> tuple[bool, list[str]]:
    """
    يفحص اشتراك المستخدم بالقنوات المطلوبة.
    يجب أن يكون البوت مشرفًا في القنوات حتى يعمل الفحص.
    """
    if not REQUIRED_CHANNELS:
        return True, []

    missing = []

    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(
                chat_id=channel,
                user_id=user_id,
            )

            status = member.status

            if status in {"left", "kicked"}:
                missing.append(channel)

        except TelegramError as exc:
            logger.warning(
                "تعذر فحص القناة %s: %s",
                channel,
                exc,
            )
            # لا نمنع المستخدم إذا تعذر الفحص بسبب إعدادات القناة.
            continue

    return len(missing) == 0, missing


def subscription_keyboard(channels: list[str]) -> InlineKeyboardMarkup:
    rows = []

    for channel in channels:
        clean = channel.lstrip("@")
        rows.append(
            [
                InlineKeyboardButton(
                    f"📢 الاشتراك في {channel}",
                    url=f"https://t.me/{clean}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "✅ تحقّق من الاشتراك",
                callback_data="check_subscription",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


# ============================================================
# واجهة المستخدم
# ============================================================


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎬 تحميل فيديو",
                    callback_data="mode_video",
                ),
                InlineKeyboardButton(
                    "🖼 تحميل صور",
                    callback_data="mode_images",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📦 كل الوسائط",
                    callback_data="mode_all",
                ),
                InlineKeyboardButton(
                    "📝 النص والوصف",
                    callback_data="mode_text",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚙️ اختيار الجودة",
                    callback_data="quality_menu",
                ),
                InlineKeyboardButton(
                    "📊 حالة الانتظار",
                    callback_data="queue_status",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❓ المساعدة",
                    callback_data="help",
                )
            ],
        ]
    )


def quality_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "360p",
                    callback_data="quality_360",
                ),
                InlineKeyboardButton(
                    "480p",
                    callback_data="quality_480",
                ),
            ],
            [
                InlineKeyboardButton(
                    "720p HD",
                    callback_data="quality_720",
                ),
                InlineKeyboardButton(
                    "1080p Full HD",
                    callback_data="quality_1080",
                ),
            ],
            [
                InlineKeyboardButton(
                    "أفضل جودة متاحة",
                    callback_data="quality_best",
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ رجوع",
                    callback_data="back_main",
                )
            ],
        ]
    )


def mode_label(mode: str) -> str:
    return {
        "video": "🎬 فيديو",
        "images": "🖼 صور",
        "all": "📦 كل الوسائط",
        "text": "📝 النص والوصف",
    }.get(mode, "📦 كل الوسائط")


def quality_label(quality: str) -> str:
    return {
        "360": "360p",
        "480": "480p",
        "720": "720p",
        "1080": "1080p",
        "best": "أفضل جودة",
    }.get(quality, "أفضل جودة")


def welcome_text() -> str:
    return (
        "👋 أهلاً بك في بوت تحميل الوسائط\n\n"
        "يدعم الروابط العامة من:\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Facebook\n\n"
        "أرسل رابط منشور، ثم اختر نوع التحميل.\n\n"
        "📦 يمكن للبوت محاولة تحميل الفيديو والصور "
        "والوسائط المتعددة والوصف المتاح.\n"
        "🖼 الصور تُرسل في مجموعات، كل مجموعة 10 صور."
    )


def help_text() -> str:
    return (
        "📚 طريقة الاستخدام:\n\n"
        "1️⃣ أرسل رابط فيديو أو منشور أو صور.\n"
        "2️⃣ اختر نوع التحميل.\n"
        "3️⃣ اختر جودة الفيديو إن لزم.\n"
        "4️⃣ انتظر حتى تكتمل المعالجة.\n\n"
        "الأنواع:\n"
        "🎬 فيديو: تحميل فيديو واحد.\n"
        "🖼 صور: محاولة تحميل صور المنشور.\n"
        "📦 كل الوسائط: فيديو وصور وملفات متاحة.\n"
        "📝 النص والوصف: إرسال وصف المنشور إن توفر.\n\n"
        "ملاحظات:\n"
        "• يجب أن يكون الرابط عامًا.\n"
        "• لا يمكن ضمان الحسابات الخاصة.\n"
        "• الصور تُرسل كل 10 صور في مجموعة.\n"
        "• الصوت يبقى داخل الفيديو، أو يُرسل منفصلًا "
        "إذا وفر المصدر ملفًا صوتيًا مستقلًا.\n"
        "• لا يمكن تحويل الكلام داخل الفيديو إلى نص "
        "من دون نظام تحويل كلام إلى نص إضافي."
    )


# ============================================================
# إرسال الملفات إلى Telegram
# ============================================================


async def send_text_chunks(
    bot: Bot,
    chat_id: int,
    text: str,
) -> None:
    if not text:
        return

    for chunk in split_caption(text, 3800):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.warning("فشل إرسال النص: %s", exc)


async def send_single_file(
    bot: Bot,
    chat_id: int,
    path: Path,
    caption: Optional[str] = None,
) -> bool:
    """
    إرسال ملف حسب نوعه.
    """
    if is_too_large(path):
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ تم تجاوز حجم Telegram المسموح به في الإعدادات:\n"
                f"{path.name}\n"
                f"الحجم: {format_bytes(file_size(path))}"
            ),
        )
        return False

    try:
        with path.open("rb") as file_handle:
            if is_video_file(path):
                await bot.send_video(
                    chat_id=chat_id,
                    video=file_handle,
                    caption=caption[:1024] if caption else None,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
                return True

            if is_audio_file(path):
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=file_handle,
                    caption=caption[:1024] if caption else None,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
                return True

            if is_image_file(path):
                # Telegram قد لا يقبل بعض صيغ الصور مثل WebP كصورة.
                if path.suffix.lower() in {
                    ".jpg",
                    ".jpeg",
                    ".png",
                }:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=file_handle,
                        caption=caption[:1024] if caption else None,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                    )
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=file_handle,
                        caption=caption[:1024] if caption else None,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                    )
                return True

            await bot.send_document(
                chat_id=chat_id,
                document=file_handle,
                caption=caption[:1024] if caption else None,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
            return True

    except TelegramError as exc:
        logger.warning("فشل إرسال الملف %s: %s", path, exc)

        # محاولة بديلة كملف
        try:
            with path.open("rb") as file_handle:
                await bot.send_document(
                    chat_id=chat_id,
                    document=file_handle,
                    caption=caption[:1024] if caption else None,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
                return True
        except TelegramError as fallback_exc:
            logger.warning(
                "فشل الإرسال البديل %s: %s",
                path,
                fallback_exc,
            )

    return False


async def send_images_in_groups(
    bot: Bot,
    chat_id: int,
    images: list[Path],
    caption: Optional[str] = None,
) -> int:
    """
    إرسال الصور في مجموعات من 10 صور.
    """
    sent_count = 0

    valid_images = [
        path for path in images
        if path.exists() and not is_too_large(path)
    ]

    for start in range(0, len(valid_images), MEDIA_GROUP_SIZE):
        group = valid_images[
            start:start + MEDIA_GROUP_SIZE
        ]

        media_items = []
        opened_files = []

        try:
            for index, path in enumerate(group):
                # Telegram يفضل jpg/png في sendMediaGroup.
                if path.suffix.lower() not in {
                    ".jpg",
                    ".jpeg",
                    ".png",
                }:
                    continue

                file_handle = path.open("rb")
                opened_files.append(file_handle)

                media_items.append(
                    InputMediaPhoto(
                        media=file_handle,
                        caption=(
                            caption[:1024]
                            if caption and start == 0 and index == 0
                            else None
                        ),
                    )
                )

            if media_items:
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=media_items,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                )
                sent_count += len(media_items)

            # إرسال الصيغ غير المدعومة كملفات منفردة.
            unsupported = [
                path for path in group
                if path.suffix.lower()
                not in {".jpg", ".jpeg", ".png"}
            ]

            for path in unsupported:
                if await send_single_file(
                    bot,
                    chat_id,
                    path,
                    None,
                ):
                    sent_count += 1

        except TelegramError as exc:
            logger.warning(
                "فشل إرسال مجموعة الصور: %s",
                exc,
            )

            # fallback: إرسال الصور واحدة واحدة
            for path in group:
                if await send_single_file(
                    bot,
                    chat_id,
                    path,
                    caption if sent_count == 0 else None,
                ):
                    sent_count += 1

        finally:
            for file_handle in opened_files:
                try:
                    file_handle.close()
                except Exception:
                    pass

        await asyncio.sleep(0.5)

    return sent_count


# ============================================================
# معالجة المهمة
# ============================================================


async def process_task(
    application: Application,
    task: DownloadTask,
) -> None:
    bot = application.bot
    work_dir = TEMP_ROOT / task.task_id
    ensure_directory(work_dir)

    status_message = None

    try:
        await bot.send_chat_action(
            chat_id=task.chat_id,
            action=ChatAction.TYPING,
        )

        status_message = await bot.send_message(
            chat_id=task.chat_id,
            text=(
                f"⏳ بدأت معالجة الرابط...\n"
                f"🌐 المنصة: {platform_name(task.url)}\n"
                f"📌 النوع: {mode_label(task.mode)}\n"
                f"🎚 الجودة: {quality_label(task.quality)}"
            ),
        )

        candidates = await get_url_candidates(task.url)

        info = None
        downloaded_files: list[Path] = []
        used_url = task.url

        # النص فقط
        if task.mode == "text":
            for candidate in candidates:
                info = await extract_info(
                    candidate,
                    download=False,
                    options=build_info_options(),
                )

                if info:
                    used_url = candidate
                    break

            if not info:
                await bot.send_message(
                    chat_id=task.chat_id,
                    text=(
                        "❌ لم أستطع استخراج معلومات هذا الرابط.\n"
                        "تأكد أن المنشور عام والرابط صحيح."
                    ),
                )
                return

            description = get_description(info)

            if description:
                await send_text_chunks(
                    bot,
                    task.chat_id,
                    description,
                )
            else:
                await bot.send_message(
                    chat_id=task.chat_id,
                    text="ℹ️ لم يتوفر وصف أو نص لهذا المنشور.",
                )

            return

        # تنزيل الوسائط
        for candidate in candidates:
            try:
                current_info, current_files = (
                    await asyncio.wait_for(
                        download_with_ytdlp(
                            candidate,
                            work_dir,
                            task.quality,
                            "video"
                            if task.mode == "video"
                            else "all",
                        ),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                )

                if current_info:
                    info = current_info
                    used_url = candidate

                if current_files:
                    downloaded_files = current_files
                    break

            except asyncio.TimeoutError:
                logger.warning(
                    "انتهت مهلة التنزيل للرابط %s",
                    candidate,
                )
            except Exception as exc:
                logger.warning(
                    "محاولة تنزيل فاشلة: %s",
                    exc,
                )

        if not info and not downloaded_files:
            await bot.send_message(
                chat_id=task.chat_id,
                text=(
                    "❌ تعذر تحميل هذا الرابط.\n\n"
                    "الأسباب المحتملة:\n"
                    "• الرابط خاص أو غير متاح.\n"
                    "• الموقع يمنع الطلبات الآلية.\n"
                    "• الرابط غير مدعوم حاليًا.\n"
                    "• المنشور يحتاج تسجيل دخول.\n"
                    "• حدث تغيير في الموقع."
                ),
            )
            return

        # إزالة الملفات المكررة
        unique_files = []
        seen_names = set()

        for path in downloaded_files:
            if path.name not in seen_names and path.exists():
                unique_files.append(path)
                seen_names.add(path.name)

        downloaded_files = unique_files

        images = [
            path for path in downloaded_files
            if is_image_file(path)
        ]

        videos = [
            path for path in downloaded_files
            if is_video_file(path)
        ]

        audios = [
            path for path in downloaded_files
            if is_audio_file(path)
        ]

        others = [
            path for path in downloaded_files
            if path not in images
            and path not in videos
            and path not in audios
        ]

        # بعض نتائج yt-dlp قد تكون ملف فيديو فقط.
        # في وضع الصور، نرسل الصور فقط إن وجدت.
        if task.mode == "images":
            if images:
                description = get_description(info)
                sent = await send_images_in_groups(
                    bot,
                    task.chat_id,
                    images,
                    description,
                )

                if sent == 0:
                    await bot.send_message(
                        chat_id=task.chat_id,
                        text=(
                            "⚠️ لم أجد صورًا قابلة للإرسال "
                            "في هذا الرابط."
                        ),
                    )
            else:
                await bot.send_message(
                    chat_id=task.chat_id,
                    text=(
                        "ℹ️ لم يتم العثور على صور متعددة "
                        "قابلة للتحميل من هذا الرابط."
                    ),
                )
            return

        # وضع الفيديو
        if task.mode == "video":
            if videos:
                description = get_description(info)

                for index, path in enumerate(videos):
                    await send_single_file(
                        bot,
                        task.chat_id,
                        path,
                        description if index == 0 else None,
                    )
            else:
                await bot.send_message(
                    chat_id=task.chat_id,
                    text="❌ لم يتم العثور على فيديو في الرابط.",
                )
            return

        # وضع كل الوسائط
        description = get_description(info)

        # إرسال الصور أولًا على شكل مجموعات من 10
        if images:
            await send_images_in_groups(
                bot,
                task.chat_id,
                images,
                description,
            )

        # إرسال الفيديوهات
        for index, path in enumerate(videos):
            await send_single_file(
                bot,
                task.chat_id,
                path,
                description if not images and index == 0 else None,
            )

        # إرسال الصوت المستقل إن توفر
        for path in audios:
            await send_single_file(
                bot,
                task.chat_id,
                path,
                None,
            )

        # إرسال الملفات الأخرى إن وجدت
        for path in others:
            await send_single_file(
                bot,
                task.chat_id,
                path,
                None,
            )

        # إذا لم تكن هناك ملفات لكن توجد معلومات
        if not downloaded_files:
            await bot.send_message(
                chat_id=task.chat_id,
                text=(
                    "ℹ️ تم استخراج معلومات الرابط، "
                    "لكن لم أجد ملفات قابلة للإرسال."
                ),
            )

        # إرسال ملخص نهائي
        total = len(downloaded_files)

        await bot.send_message(
            chat_id=task.chat_id,
            text=(
                f"✅ اكتملت المعالجة.\n"
                f"📦 عدد الملفات التي تم العثور عليها: {total}\n"
                f"🌐 المصدر: {platform_name(used_url)}"
            ),
        )

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        logger.exception("خطأ أثناء معالجة المهمة: %s", exc)

        try:
            await bot.send_message(
                chat_id=task.chat_id,
                text=(
                    "❌ حدث خطأ غير متوقع أثناء المعالجة.\n"
                    "حاول إرسال الرابط مرة أخرى."
                ),
            )
        except TelegramError:
            pass

    finally:
        remove_path(work_dir)


async def queue_worker(
    application: Application,
    worker_id: int,
) -> None:
    logger.info("بدأ عامل قائمة الانتظار رقم %s", worker_id)

    while True:
        task = await download_queue.get()

        try:
            async with download_semaphore:
                await process_task(application, task)
        except Exception as exc:
            logger.exception(
                "خطأ في عامل الانتظار: %s",
                exc,
            )
        finally:
            download_queue.task_done()


# ============================================================
# أوامر Telegram
# ============================================================


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    user_id = update.effective_user.id

    allowed, missing = await check_required_channels(
        context.bot,
        user_id,
    )

    if not allowed:
        await update.message.reply_text(
            "🔒 يجب الاشتراك في القنوات التالية أولًا:",
            reply_markup=subscription_keyboard(missing),
        )
        return

    user_modes.setdefault(user_id, "all")
    user_qualities.setdefault(user_id, "best")

    await update.message.reply_text(
        welcome_text(),
        reply_markup=main_keyboard(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.effective_chat:
        return

    await update.message.reply_text(
        help_text(),
        reply_markup=main_keyboard(),
    )


async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ هذا الأمر مخصص للمشرف."
        )
        return

    await update.message.reply_text(
        "📊 إحصائيات البوت:\n\n"
        f"📥 المهام في الانتظار: {download_queue.qsize()}\n"
        f"⚙️ التحميلات المتزامنة: "
        f"{MAX_CONCURRENT_DOWNLOADS}\n"
        f"📦 الحد الأقصى للانتظار: {MAX_QUEUE_SIZE}\n"
        f"💾 الحد الأقصى للملف: {MAX_FILE_SIZE_MB} MB"
    )


async def handle_text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    message = update.message

    if not message or not message.text:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    url = extract_url(message.text)

    if not url:
        await message.reply_text(
            "🔗 أرسل رابطًا من TikTok أو Instagram أو Facebook.",
            reply_markup=main_keyboard(),
        )
        return

    if not is_supported_url(url):
        await message.reply_text(
            "❌ هذا الرابط غير مدعوم.\n"
            "أرسل رابطًا عامًا من TikTok أو Instagram أو Facebook."
        )
        return

    allowed, missing = await check_required_channels(
        context.bot,
        user_id,
    )

    if not allowed:
        await message.reply_text(
            "🔒 يجب الاشتراك في القنوات المطلوبة أولًا:",
            reply_markup=subscription_keyboard(missing),
        )
        return

    mode = user_modes.get(user_id, "all")
    quality = user_qualities.get(user_id, "best")

    task = DownloadTask(
        user_id=user_id,
        chat_id=chat_id,
        url=url,
        mode=mode,
        quality=quality,
    )

    if download_queue.full():
        await message.reply_text(
            "⏳ قائمة الانتظار ممتلئة حاليًا.\n"
            "انتظر قليلًا ثم أرسل الرابط مرة أخرى."
        )
        return

    await download_queue.put(task)

    await message.reply_text(
        "✅ تمت إضافة الرابط إلى قائمة الانتظار.\n\n"
        f"🌐 المنصة: {platform_name(url)}\n"
        f"📌 النوع: {mode_label(mode)}\n"
        f"🎚 الجودة: {quality_label(quality)}\n"
        f"📊 عدد المهام المنتظرة: {download_queue.qsize()}",
        reply_markup=main_keyboard(),
    )


# ============================================================
# الأزرار
# ============================================================


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if not query or not query.from_user:
        return

    await query.answer()

    user_id = query.from_user.id
    data = query.data or ""

    if data == "check_subscription":
        allowed, missing = await check_required_channels(
            context.bot,
            user_id,
        )

        if allowed:
            await query.edit_message_text(
                "✅ تم التحقق من الاشتراك.\n"
                "يمكنك الآن إرسال الرابط.",
                reply_markup=main_keyboard(),
            )
        else:
            await query.edit_message_text(
                "❌ ما زال الاشتراك ناقصًا في القنوات التالية:",
                reply_markup=subscription_keyboard(missing),
            )
        return

    if data == "back_main":
        await query.edit_message_text(
            welcome_text(),
            reply_markup=main_keyboard(),
        )
        return

    if data == "help":
        await query.edit_message_text(
            help_text(),
            reply_markup=main_keyboard(),
        )
        return

    if data == "queue_status":
        await query.edit_message_text(
            "📊 حالة قائمة الانتظار:\n\n"
            f"📥 المهام المنتظرة: {download_queue.qsize()}\n"
            f"⚙️ عدد التحميلات المتزامنة: "
            f"{MAX_CONCURRENT_DOWNLOADS}",
            reply_markup=main_keyboard(),
        )
        return

    if data == "quality_menu":
        current = user_qualities.get(user_id, "best")

        await query.edit_message_text(
            "🎚 اختر جودة الفيديو الافتراضية:\n\n"
            f"الجودة الحالية: {quality_label(current)}",
            reply_markup=quality_keyboard(),
        )
        return

    if data.startswith("quality_"):
        quality = data.replace("quality_", "", 1)

        if quality not in {
            "360",
            "480",
            "720",
            "1080",
            "best",
        }:
            quality = "best"

        user_qualities[user_id] = quality

        await query.edit_message_text(
            f"✅ تم اختيار الجودة: {quality_label(quality)}\n\n"
            "أرسل رابط الفيديو الآن.",
            reply_markup=main_keyboard(),
        )
        return

    if data.startswith("mode_"):
        mode = data.replace("mode_", "", 1)

        if mode not in {
            "video",
            "images",
            "all",
            "text",
        }:
            mode = "all"

        user_modes[user_id] = mode

        await query.edit_message_text(
            f"✅ تم اختيار الوضع: {mode_label(mode)}\n\n"
            "أرسل رابط المنشور الآن.",
            reply_markup=main_keyboard(),
        )
        return


# ============================================================
# تشغيل البوت
# ============================================================


async def post_init(application: Application) -> None:
    global queue_workers_started

    if queue_workers_started:
        return

    queue_workers_started = True

    for worker_id in range(MAX_CONCURRENT_DOWNLOADS):
        application.create_task(
            queue_worker(application, worker_id + 1)
        )

    logger.info(
        "تم تشغيل %s عامل/عمال لقائمة الانتظار",
        MAX_CONCURRENT_DOWNLOADS,
    )


def validate_configuration() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "لم يتم العثور على BOT_TOKEN. "
            "أضفه في Environment Variables داخل Render."
        )

    if MAX_CONCURRENT_DOWNLOADS < 1:
        raise RuntimeError(
            "MAX_CONCURRENT_DOWNLOADS يجب أن يكون 1 أو أكثر."
        )

    if MAX_QUEUE_SIZE < 1:
        raise RuntimeError(
            "MAX_QUEUE_SIZE يجب أن يكون 1 أو أكثر."
        )


def build_application() -> Application:
    validate_configuration()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text_message,
        )
    )

    return application


def main() -> None:
    logger.info("جاري تشغيل البوت...")

    application = build_application()

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
