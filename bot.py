# ============================================================
# Telegram Media Downloader Bot
# TikTok / Instagram / Facebook / YouTube / yt-dlp
#
# ============================================================

import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import yt_dlp

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
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
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN غير موجود في Environment Variables."
    )

# القنوات الإجبارية، ويمكن تركها فارغة:
# REQUIRED_CHANNELS=@channel1,@channel2
REQUIRED_CHANNELS = [
    item.strip()
    for item in os.getenv(
        "REQUIRED_CHANNELS",
        "",
    ).split(",")
    if item.strip()
]

# أرقام المشرفين:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(item.strip())
    for item in os.getenv(
        "ADMIN_IDS",
        "",
    ).split(",")
    if item.strip().isdigit()
}

MAX_FILE_SIZE_MB = int(
    os.getenv("MAX_FILE_SIZE_MB", "150")
)

MAX_FILE_SIZE_BYTES = (
    MAX_FILE_SIZE_MB * 1024 * 1024
)

MAX_CONCURRENT_DOWNLOADS = max(
    1,
    int(
        os.getenv(
            "MAX_CONCURRENT_DOWNLOADS",
            "2",
        )
    ),
)

MAX_QUEUE_SIZE = max(
    1,
    int(
        os.getenv(
            "MAX_QUEUE_SIZE",
            "30",
        )
    ),
)

MAX_IMAGES_PER_REQUEST = max(
    1,
    int(
        os.getenv(
            "MAX_IMAGES_PER_REQUEST",
            "10",
        )
    ),
)

DOWNLOAD_TIMEOUT = max(
    60,
    int(
        os.getenv(
            "DOWNLOAD_TIMEOUT",
            "600",
        )
    ),
)

BOT_NAME = "بوت التحميل الذكي"
DEVELOPER_NAME = "مهدي الربيعي"


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(
    "telegram_media_downloader"
)


# ============================================================
# Runtime state
# ============================================================

download_queue: asyncio.Queue = asyncio.Queue(
    maxsize=MAX_QUEUE_SIZE
)

download_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)

user_preferences: dict[int, dict] = {}
active_jobs: dict[str, dict] = {}

bot_stats = {
    "total_requests": 0,
    "successful_downloads": 0,
    "failed_downloads": 0,
}

worker_tasks: list[asyncio.Task] = []


# ============================================================
# yt-dlp logger
# ============================================================

class YTDLPLogger:
    def debug(self, message):
        if message.startswith("[debug]"):
            return

        logger.debug("yt-dlp: %s", message)

    def info(self, message):
        logger.info("yt-dlp: %s", message)

    def warning(self, message):
        logger.warning("yt-dlp: %s", message)

    def error(self, message):
        logger.error("yt-dlp: %s", message)


# ============================================================
# General helpers
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"

    if size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"

    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"

    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def clean_filename(filename: str) -> str:
    filename = re.sub(
        r'[\\/*?:"<>|]',
        "_",
        filename,
    )

    filename = filename.strip()

    if not filename:
        filename = "download"

    return filename[:180]


def extract_urls(text: str) -> list[str]:
    if not text:
        return []

    pattern = r"https?://[^\s<>\"]+"

    urls = re.findall(
        pattern,
        text,
    )

    result = []

    for url in urls:
        url = url.rstrip(
            ".,!?؛،)]}"
        )

        if url not in result:
            result.append(url)

    return result


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme in (
                "http",
                "https",
            )
            and bool(parsed.netloc)
        )
    except Exception:
        return False


def get_hostname(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""


def is_tiktok_url(url: str) -> bool:
    host = get_hostname(url)

    return (
        host == "tiktok.com"
        or host.endswith(".tiktok.com")
    )


def is_instagram_url(url: str) -> bool:
    host = get_hostname(url)

    return (
        host == "instagram.com"
        or host.endswith(".instagram.com")
    )


def is_facebook_url(url: str) -> bool:
    host = get_hostname(url)

    return (
        host == "facebook.com"
        or host.endswith(".facebook.com")
        or host == "fb.watch"
    )


def is_supported_social_url(url: str) -> bool:
    return (
        is_tiktok_url(url)
        or is_instagram_url(url)
        or is_facebook_url(url)
    )


# ============================================================
# URL resolving
# ============================================================

def resolve_short_url(url: str) -> str:
    """
    فك روابط TikTok المختصرة مثل:
    vt.tiktok.com
    vm.tiktok.com

    إذا فشلت العملية نعيد الرابط الأصلي.
    """

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; Mobile) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0.0.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            allow_redirects=True,
            timeout=25,
        )

        if response.url:
            return response.url

    except Exception as exc:
        logger.warning(
            "URL resolving failed: %s",
            exc,
        )

    return url


def get_url_candidates(url: str) -> list[str]:
    """
    ينشئ قائمة روابط لتجربة أكثر من خطة.
    """

    candidates: list[str] = []

    def add_candidate(candidate: str):
        if not candidate:
            return

        candidate = candidate.strip()

        if candidate and candidate not in candidates:
            candidates.append(candidate)

    # الخطة الأولى: الرابط الأصلي
    add_candidate(url)

    # الخطة الثانية: الرابط بعد فك الاختصار
    if is_tiktok_url(url):
        resolved_url = resolve_short_url(url)
        add_candidate(resolved_url)

    return candidates


# ============================================================
# Menus
# ============================================================

def main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                "🎬 فيديو",
                callback_data="mode_video",
            ),
            InlineKeyboardButton(
                "🎵 صوت MP3",
                callback_data="mode_audio",
            ),
        ],
        [
            InlineKeyboardButton(
                "🖼 صور",
                callback_data="mode_images",
            ),
            InlineKeyboardButton(
                "📝 نصوص وترجمة",
                callback_data="mode_text",
            ),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ معلومات الرابط",
                callback_data="mode_info",
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ الإعدادات",
                callback_data="settings",
            ),
            InlineKeyboardButton(
                "📋 حالة الانتظار",
                callback_data="queue_status",
            ),
        ],
        [
            InlineKeyboardButton(
                "❓ المساعدة",
                callback_data="help",
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def quality_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                "🏆 أفضل جودة",
                callback_data="quality_best",
            ),
        ],
        [
            InlineKeyboardButton(
                "1080p",
                callback_data="quality_1080",
            ),
            InlineKeyboardButton(
                "720p",
                callback_data="quality_720",
            ),
        ],
        [
            InlineKeyboardButton(
                "480p",
                callback_data="quality_480",
            ),
            InlineKeyboardButton(
                "360p",
                callback_data="quality_360",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 رجوع",
                callback_data="back_main",
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def settings_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                "🎬 تغيير الجودة",
                callback_data="settings_quality",
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 حالة الانتظار",
                callback_data="queue_status",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 الرئيسية",
                callback_data="back_main",
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# ============================================================
# Subscription
# ============================================================

async def check_required_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    يجب أن يكون البوت مشرفًا في القنوات حتى يعمل الفحص بصورة موثوقة.
    """

    if not REQUIRED_CHANNELS:
        return True

    user = update.effective_user

    if not user:
        return True

    not_joined = []

    for channel in REQUIRED_CHANNELS:
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel,
                user_id=user.id,
            )

            if member.status in (
                "left",
                "kicked",
            ):
                not_joined.append(channel)

        except Exception as exc:
            logger.warning(
                "Subscription check error for %s: %s",
                channel,
                exc,
            )

            # لا نمنع المستخدم إذا تعذر فحص القناة
            continue

    if not_joined:
        buttons = []

        for channel in not_joined:
            channel_name = channel.lstrip("@")

            buttons.append([
                InlineKeyboardButton(
                    f"📢 الاشتراك في {channel}",
                    url=(
                        f"https://t.me/{channel_name}"
                    ),
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "✅ التحقق من الاشتراك",
                callback_data="check_subscription",
            )
        ])

        text = (
            "🔒 <b>الاشتراك مطلوب</b>\n\n"
            "اشترك في القنوات التالية ثم اضغط "
            "على زر التحقق:\n\n"
            + "\n".join(
                f"• {channel}"
                for channel in not_joined
            )
        )

        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        elif update.message:
            await update.message.reply_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        return False

    return True


# ============================================================
# yt-dlp options
# ============================================================

def build_ydl_options(
    mode: str,
    quality: Optional[str],
    output_dir: str,
    source_url: Optional[str] = None,
) -> dict:
    """
    إعدادات متعددة لتحسين التعامل مع TikTok
    والمواقع الأخرى.
    """

    output_template = str(
        Path(output_dir)
        / "%(title).150s_%(id)s.%(ext)s"
    )

    tiktok = bool(
        source_url and is_tiktok_url(source_url)
    )

    options = {
        "outtmpl": output_template,

        # لا تحمل قائمة تشغيل كاملة للفيديو العادي
        "noplaylist": (
            False if mode == "images" else True
        ),

        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "ignoreerrors": False,

        # خطط إعادة المحاولة
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 8,

        "socket_timeout": 60,

        "continuedl": True,
        "overwrites": True,

        "writethumbnail": False,
        "writeinfojson": False,

        "writesubtitles": False,
        "writeautomaticsub": False,

        "max_filesize": MAX_FILE_SIZE_BYTES,

        "merge_output_format": "mp4",

        "logger": YTDLPLogger(),

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Mobile) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        },
    }

    # إعدادات خاصة بتيك توك
    if tiktok:
        options.update({
            "noplaylist": True,
            "retries": 12,
            "fragment_retries": 12,
            "extractor_retries": 10,
            "socket_timeout": 75,

            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13; Mobile) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Mobile Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,image/avif,"
                    "image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.tiktok.com/",
                "Connection": "keep-alive",
            },
        })

    # ========================================================
    # Video / Document
    # ========================================================

    if mode in (
        "video",
        "document",
    ):
        if quality and str(quality).isdigit():
            options["format"] = (
                f"bestvideo[height<={quality}]+bestaudio/"
                f"best[height<={quality}]/"
                "bestvideo+bestaudio/"
                "best[ext=mp4]/"
                "best"
            )
        else:
            options["format"] = (
                "bestvideo+bestaudio/"
                "best[ext=mp4]/"
                "best"
            )

        options["merge_output_format"] = "mp4"

    # ========================================================
    # Audio
    # ========================================================

    elif mode == "audio":
        options["format"] = (
            "bestaudio/"
            "best[ext=m4a]/"
            "best[ext=mp4]/"
            "best"
        )

        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    # ========================================================
    # Images
    # ========================================================

    elif mode == "images":
        options["format"] = "best"
        options["noplaylist"] = False

    # ========================================================
    # Text / subtitles
    # ========================================================

    elif mode in (
        "text",
        "subtitles",
    ):
        options["skip_download"] = True
        options["writesubtitles"] = True
        options["writeautomaticsub"] = True
        options["subtitleslangs"] = ["all"]
        options["subtitlesformat"] = "srt/vtt/best"

    # ========================================================
    # Metadata
    # ========================================================

    elif mode == "info":
        options["skip_download"] = True
        options["noplaylist"] = True

    return options


# ============================================================
# File collection
# ============================================================

def collect_media_files(
    output_dir: str,
    mode: str,
) -> list[Path]:
    """
    جمع الملفات التي أنشأها yt-dlp.
    """

    directory = Path(output_dir)

    if not directory.exists():
        return []

    ignored_extensions = {
        ".part",
        ".ytdl",
        ".json",
        ".description",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".vtt",
        ".srt",
        ".ass",
        ".lrc",
        ".tmp",
    }

    files = []

    for file_path in directory.iterdir():
        if not file_path.is_file():
            continue

        if file_path.name.startswith("."):
            continue

        if file_path.suffix.lower() in ignored_extensions:
            continue

        try:
            size = file_path.stat().st_size
        except OSError:
            continue

        if size <= 0:
            continue

        if size > MAX_FILE_SIZE_BYTES:
            logger.warning(
                "File exceeds size limit: %s",
                file_path,
            )

            try:
                file_path.unlink()
            except OSError:
                pass

            continue

        files.append(file_path)

    files.sort(
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

    if mode == "images":
        return files[:MAX_IMAGES_PER_REQUEST]

    return files[:1]


def collect_text_files(
    output_dir: str,
) -> list[Path]:
    directory = Path(output_dir)

    if not directory.exists():
        return []

    allowed_extensions = {
        ".srt",
        ".vtt",
        ".ass",
        ".lrc",
        ".txt",
    }

    files = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower()
        in allowed_extensions
    ]

    files.sort(
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

    return files


# ============================================================
# Download plan 1 and plan 2: yt-dlp
# ============================================================

def download_with_ytdlp(
    url: str,
    mode: str,
    quality: Optional[str],
    output_dir: str,
) -> list[Path]:
    """
    تجربة الرابط الأصلي ثم الرابط المفكوك.
    """

    Path(output_dir).mkdir(
        parents=True,
        exist_ok=True,
    )

    candidates = get_url_candidates(url)

    if not candidates:
        raise RuntimeError(
            "لا يوجد رابط صالح."
        )

    last_error = None

    for attempt_number, candidate in enumerate(
        candidates,
        start=1,
    ):
        logger.info(
            "Download attempt %s/%s: %s",
            attempt_number,
            len(candidates),
            candidate,
        )

        try:
            options = build_ydl_options(
                mode=mode,
                quality=quality,
                output_dir=output_dir,
                source_url=candidate,
            )

            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([candidate])

            files = collect_media_files(
                output_dir=output_dir,
                mode=mode,
            )

            if files:
                return files

            raise RuntimeError(
                "لم يتم إنشاء ملف قابل للإرسال."
            )

        except Exception as exc:
            last_error = exc

            logger.exception(
                "yt-dlp attempt failed: %s",
                exc,
            )

            continue

    raise RuntimeError(
        "فشل التحميل بجميع محاولات yt-dlp. "
        f"آخر خطأ: {last_error}"
    )


# ============================================================
# Text download
# ============================================================

def download_text_with_ytdlp(
    url: str,
    output_dir: str,
) -> list[Path]:
    """
    محاولة تحميل الترجمة الأصلية أو التلقائية.
    """

    Path(output_dir).mkdir(
        parents=True,
        exist_ok=True,
    )

    candidates = get_url_candidates(url)
    last_error = None

    for candidate in candidates:
        try:
            options = build_ydl_options(
                mode="text",
                quality=None,
                output_dir=output_dir,
                source_url=candidate,
            )

            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([candidate])

            files = collect_text_files(output_dir)

            if files:
                return files

            raise RuntimeError(
                "لا توجد ترجمة أو نصوص متاحة."
            )

        except Exception as exc:
            last_error = exc

            logger.exception(
                "Text extraction failed: %s",
                exc,
            )

    raise RuntimeError(
        f"تعذر تحميل النصوص: {last_error}"
    )


# ============================================================
# Metadata extraction
# ============================================================

def extract_media_info(url: str) -> dict:
    """
    استخراج معلومات الرابط دون تحميله.
    """

    candidates = get_url_candidates(url)
    last_error = None

    for candidate in candidates:
        try:
            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "retries": 6,
                "extractor_retries": 6,
                "socket_timeout": 60,
                "logger": YTDLPLogger(),
                "http_headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 13; Mobile) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Mobile Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.tiktok.com/",
                },
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(
                    candidate,
                    download=False,
                )

            if info:
                return info

        except Exception as exc:
            last_error = exc

            logger.exception(
                "Info extraction failed: %s",
                exc,
            )

    raise RuntimeError(
        f"تعذر استخراج المعلومات: {last_error}"
    )


# ============================================================
# Send media files
# ============================================================

async def send_one_file(
    message,
    file_path: Path,
    mode: str,
):
    """
    إرسال ملف واحد مع خطط احتياطية.
    """

    if not file_path.exists():
        return False

    file_size = file_path.stat().st_size

    if file_size > MAX_FILE_SIZE_BYTES:
        await message.reply_text(
            "⚠️ الملف أكبر من الحد المسموح به "
            f"({MAX_FILE_SIZE_MB} MB)."
        )
        return False

    caption = (
        "✅ تم التحميل بنجاح\n"
        f"📦 الحجم: {human_size(file_size)}\n"
        f"👨‍💻 برمجة: {DEVELOPER_NAME}"
    )

    suffix = file_path.suffix.lower()

    try:
        if mode == "audio" or suffix in (
            ".mp3",
            ".m4a",
            ".wav",
            ".ogg",
            ".flac",
        ):
            with file_path.open("rb") as file_handle:
                await message.reply_audio(
                    audio=file_handle,
                    caption=caption,
                )

            return True

        if mode == "images" or suffix in (
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif",
        ):
            with file_path.open("rb") as file_handle:
                await message.reply_photo(
                    photo=file_handle,
                    caption=caption,
                )

            return True

        if mode in (
            "text",
            "subtitles",
        ) or suffix in (
            ".srt",
            ".vtt",
            ".ass",
            ".lrc",
            ".txt",
        ):
            with file_path.open("rb") as file_handle:
                await message.reply_document(
                    document=file_handle,
                    caption=caption,
                )

            return True

        # محاولة الإرسال كفيديو
        with file_path.open("rb") as file_handle:
            await message.reply_video(
                video=file_handle,
                caption=caption,
                supports_streaming=True,
            )

        return True

    except TelegramError as exc:
        logger.warning(
            "Specialized sending failed: %s",
            exc,
        )

    except Exception as exc:
        logger.warning(
            "File sending failed: %s",
            exc,
        )

    # الخطة الاحتياطية: إرسال كملف
    try:
        with file_path.open("rb") as file_handle:
            await message.reply_document(
                document=file_handle,
                caption=caption,
            )

        return True

    except Exception as exc:
        logger.exception(
            "Document fallback failed: %s",
            exc,
        )

        return False


async def send_downloaded_files(
    update: Update,
    files: list[Path],
    mode: str,
):
    message = update.effective_message

    if not message:
        return

    if not files:
        await message.reply_text(
            "❌ لم يتم العثور على ملفات للإرسال."
        )
        return

    sent_count = 0

    # إرسال الصور كمجموعة عندما يكون ذلك ممكنًا
    if mode == "images" and len(files) > 1:
        media_group = []

        for file_path in files[:10]:
            if not file_path.exists():
                continue

            try:
                if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                    continue

                media_group.append(
                    InputMediaPhoto(
                        media=file_path.open("rb"),
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Could not prepare image: %s",
                    exc,
                )

        # لا نستخدم المجموعة إذا لم توجد صور كافية
        if media_group:
            try:
                await message.reply_media_group(
                    media=media_group,
                )
                sent_count = len(media_group)
                return
            except Exception as exc:
                logger.warning(
                    "Media group sending failed: %s",
                    exc,
                )

        # fallback: إرسال كل صورة منفردة
        for file_path in files:
            if await send_one_file(
                message,
                file_path,
                mode,
            ):
                sent_count += 1

        return

    for file_path in files:
        if await send_one_file(
            message,
            file_path,
            mode,
        ):
            sent_count += 1

    if sent_count == 0:
        await message.reply_text(
            "❌ تعذر إرسال الملفات إلى تيليجرام."
        )


# ============================================================
# Send metadata
# ============================================================

async def send_media_info(
    update: Update,
    info: dict,
):
    message = update.effective_message

    if not message:
        return

    title = info.get("title") or "غير متوفر"
    uploader = info.get("uploader") or "غير متوفر"
    duration = info.get("duration")
    view_count = info.get("view_count")
    like_count = info.get("like_count")
    webpage_url = info.get("webpage_url") or ""

    lines = [
        "ℹ️ <b>معلومات الرابط</b>",
        "",
        f"🎬 <b>العنوان:</b> "
        f"{html.escape(str(title))}",
        f"👤 <b>الحساب:</b> "
        f"{html.escape(str(uploader))}",
    ]

    if duration is not None:
        lines.append(
            f"⏱ <b>المدة:</b> {duration} ثانية"
        )

    if view_count is not None:
        lines.append(
            f"👁 <b>المشاهدات:</b> {view_count}"
        )

    if like_count is not None:
        lines.append(
            f"❤️ <b>الإعجابات:</b> {like_count}"
        )

    if webpage_url:
        safe_url = html.escape(
            str(webpage_url),
            quote=True,
        )

        lines.append(
            f'🔗 <a href="{safe_url}">فتح الرابط</a>'
        )

    await message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ============================================================
# Queue and jobs
# ============================================================

async def enqueue_job(
    update: Update,
    url: str,
    mode: str,
    quality: Optional[str],
):
    message = update.effective_message

    if not message:
        return

    if download_queue.full():
        await message.reply_text(
            "⏳ قائمة الانتظار ممتلئة حاليًا.\n"
            "انتظر قليلًا ثم حاول مرة أخرى."
        )
        return

    await download_queue.put({
        "update": update,
        "url": url,
        "mode": mode,
        "quality": quality,
    })

    position = download_queue.qsize()

    await message.reply_text(
        "📥 تمت إضافة طلبك إلى قائمة الانتظار.\n"
        f"📊 عدد الطلبات المنتظرة: {position}\n\n"
        "سيبدأ التحميل تلقائيًا."
    )


async def process_download_job(
    update: Update,
    url: str,
    mode: str,
    quality: Optional[str],
):
    message = update.effective_message
    user = update.effective_user

    if not message:
        return

    user_id = user.id if user else 0

    job_id = (
        f"{user_id}_"
        f"{int(time.time() * 1000)}"
    )

    active_jobs[job_id] = {
        "user_id": user_id,
        "url": url,
        "mode": mode,
        "started_at": time.time(),
    }

    bot_stats["total_requests"] += 1

    temporary_dir = tempfile.mkdtemp(
        prefix="media_download_"
    )

    try:
        await message.reply_text(
            "⏳ بدأت معالجة الرابط...\n"
            "قد يستغرق ذلك بعض الوقت."
        )

        try:
            await update.get_bot().send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.UPLOAD_DOCUMENT,
            )
        except Exception:
            pass

        async with download_semaphore:
            if mode == "text":
                files = await asyncio.wait_for(
                    asyncio.to_thread(
                        download_text_with_ytdlp,
                        url,
                        temporary_dir,
                    ),
                    timeout=DOWNLOAD_TIMEOUT,
                )

            elif mode == "info":
                info = await asyncio.wait_for(
                    asyncio.to_thread(
                        extract_media_info,
                        url,
                    ),
                    timeout=DOWNLOAD_TIMEOUT,
                )

                await send_media_info(
                    update,
                    info,
                )

                bot_stats["successful_downloads"] += 1
                return

            else:
                files = await asyncio.wait_for(
                    asyncio.to_thread(
                        download_with_ytdlp,
                        url,
                        mode,
                        quality,
                        temporary_dir,
                    ),
                    timeout=DOWNLOAD_TIMEOUT,
                )

        if not files:
            raise RuntimeError(
                "لم يتم العثور على ملفات."
            )

        await send_downloaded_files(
            update=update,
            files=files,
            mode=mode,
        )

        bot_stats["successful_downloads"] += 1

    except asyncio.TimeoutError:
        bot_stats["failed_downloads"] += 1

        await message.reply_text(
            "⌛ انتهى وقت التحميل.\n\n"
            "قد يكون الرابط كبيرًا أو أن الموقع "
            "لا يستجيب من الخادم. حاول برابط آخر."
        )

    except Exception as exc:
        bot_stats["failed_downloads"] += 1

        logger.exception(
            "Download job failed: %s",
            exc,
        )

        await message.reply_text(
            "❌ تعذر تحميل الرابط.\n\n"
            "الأسباب المحتملة:\n"
            "• الرابط خاص أو محذوف.\n"
            "• الموقع منع الطلبات مؤقتًا.\n"
            "• الفيديو غير متاح.\n"
            "• حجم الملف أكبر من الحد.\n"
            "• يحتاج yt-dlp إلى تحديث.\n\n"
            "جرّب رابطًا عامًا آخر."
        )

    finally:
        active_jobs.pop(job_id, None)

        try:
            shutil.rmtree(
                temporary_dir,
                ignore_errors=True,
            )
        except Exception:
            pass


async def queue_worker(worker_id: int):
    logger.info(
        "Queue worker %s started",
        worker_id,
    )

    while True:
        job = await download_queue.get()

        try:
            await process_download_job(
                update=job["update"],
                url=job["url"],
                mode=job["mode"],
                quality=job["quality"],
            )

        except Exception as exc:
            logger.exception(
                "Worker error: %s",
                exc,
            )

        finally:
            download_queue.task_done()


async def start_workers(
    application: Application,
):
    if worker_tasks:
        return

    for worker_id in range(
        MAX_CONCURRENT_DOWNLOADS
    ):
        task = asyncio.create_task(
            queue_worker(worker_id + 1)
        )

        worker_tasks.append(task)

    logger.info(
        "Started %s workers",
        len(worker_tasks),
    )


# ============================================================
# Commands
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await check_required_subscription(
        update,
        context,
    ):
        return

    user = update.effective_user
    first_name = (
        user.first_name
        if user
        else ""
    )

    text = (
        f"👋 أهلاً بك {html.escape(first_name)}\n\n"
        f"🤖 <b>{BOT_NAME}</b>\n\n"
        "أرسل رابطًا عامًا من:\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Facebook\n"
        "• YouTube\n"
        "• المواقع المدعومة من yt-dlp\n\n"
        "ثم اختر نوع التحميل.\n\n"
        f"👨‍💻 برمجة: {DEVELOPER_NAME}"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        "❓ <b>طريقة الاستخدام</b>\n\n"
        "1️⃣ اختر نوع التحميل.\n"
        "2️⃣ أرسل الرابط.\n"
        "3️⃣ انتظر حتى تتم المعالجة.\n"
        "4️⃣ يصلك الملف.\n\n"
        "🎬 فيديو: تحميل الفيديو.\n"
        "🎵 صوت: استخراج MP3.\n"
        "🖼 صور: محاولة استخراج صور المنشور.\n"
        "📝 نصوص: محاولة تحميل الترجمة.\n"
        "ℹ️ معلومات: عرض معلومات الرابط.\n\n"
        "⚠️ يجب أن يكون الرابط عامًا."
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user or not is_admin(user.id):
        await update.message.reply_text(
            "⛔ هذا الأمر مخصص للإدارة."
        )
        return

    text = (
        "📊 <b>إحصائيات البوت</b>\n\n"
        f"📥 إجمالي الطلبات: "
        f"{bot_stats['total_requests']}\n"
        f"✅ الناجحة: "
        f"{bot_stats['successful_downloads']}\n"
        f"❌ الفاشلة: "
        f"{bot_stats['failed_downloads']}\n"
        f"⏳ في الانتظار: "
        f"{download_queue.qsize()}\n"
        f"⚙️ العمال: "
        f"{MAX_CONCURRENT_DOWNLOADS}"
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
    )


# ============================================================
# Callback handler
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "check_subscription":
        if await check_required_subscription(
            update,
            context,
        ):
            await query.edit_message_text(
                "✅ تم التحقق من الاشتراك.\n"
                "يمكنك الآن استخدام البوت.",
                reply_markup=main_menu(),
            )

        return

    if data == "back_main":
        await query.edit_message_text(
            "🏠 <b>القائمة الرئيسية</b>\n\n"
            "اختر نوع العملية:",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "help":
        await query.edit_message_text(
            "❓ <b>طريقة الاستخدام</b>\n\n"
            "اختر نوع التحميل ثم أرسل الرابط.\n\n"
            "يدعم البوت الفيديو والصوت والصور "
            "ومحاولة استخراج الترجمة.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "settings":
        await query.edit_message_text(
            "⚙️ <b>الإعدادات</b>",
            parse_mode="HTML",
            reply_markup=settings_menu(),
        )
        return

    if data == "settings_quality":
        await query.edit_message_text(
            "🎬 اختر الجودة:",
            reply_markup=quality_menu(),
        )
        return

    if data == "queue_status":
        await query.edit_message_text(
            "📋 <b>حالة الانتظار</b>\n\n"
            f"الطلبات المنتظرة: "
            f"{download_queue.qsize()}\n"
            f"عدد العمال: "
            f"{MAX_CONCURRENT_DOWNLOADS}\n"
            f"الحد الأقصى: "
            f"{MAX_QUEUE_SIZE}",
            parse_mode="HTML",
            reply_markup=settings_menu(),
        )
        return

    if data == "mode_video":
        user_preferences[user_id] = {
            **user_preferences.get(user_id, {}),
            "mode": "video",
        }

        await query.edit_message_text(
            "🎬 <b>تحميل فيديو</b>\n\n"
            "أرسل الآن رابط الفيديو، "
            "ثم اختر الجودة.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "mode_audio":
        user_preferences[user_id] = {
            **user_preferences.get(user_id, {}),
            "mode": "audio",
        }

        await query.edit_message_text(
            "🎵 <b>استخراج الصوت</b>\n\n"
            "أرسل رابط الفيديو لاستخراج "
            "الصوت بصيغة MP3.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "mode_images":
        user_preferences[user_id] = {
            **user_preferences.get(user_id, {}),
            "mode": "images",
        }

        await query.edit_message_text(
            "🖼 <b>تحميل الصور</b>\n\n"
            "أرسل رابط منشور الصور أو السلايد شو.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "mode_text":
        user_preferences[user_id] = {
            **user_preferences.get(user_id, {}),
            "mode": "text",
        }

        await query.edit_message_text(
            "📝 <b>تحميل النصوص والترجمة</b>\n\n"
            "أرسل رابط الفيديو.\n"
            "سيحاول البوت تحميل الترجمة الأصلية "
            "أو التلقائية إن كانت متاحة.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data == "mode_info":
        user_preferences[user_id] = {
            **user_preferences.get(user_id, {}),
            "mode": "info",
        }

        await query.edit_message_text(
            "ℹ️ <b>معلومات الرابط</b>\n\n"
            "أرسل الرابط الآن لعرض معلوماته.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if data.startswith("quality_"):
        quality_value = data.replace(
            "quality_",
            "",
        )

        quality = (
            "best"
            if quality_value == "best"
            else quality_value
        )

        preferences = user_preferences.get(
            user_id,
            {},
        )

        pending_url = preferences.get(
            "pending_url"
        )

        # إذا كان هناك رابط بانتظار اختيار الجودة
        if pending_url:
            user_preferences[user_id] = {
                **preferences,
                "quality": quality,
                "pending_url": None,
            }

            await query.edit_message_text(
                f"✅ الجودة المختارة: {quality}\n"
                "⏳ جارٍ إضافة الطلب إلى الانتظار..."
            )

            await enqueue_job(
                update=update,
                url=pending_url,
                mode="video",
                quality=quality,
            )

            return

        # إذا لم يوجد رابط، نحفظ الجودة كإعداد افتراضي
        user_preferences[user_id] = {
            **preferences,
            "quality": quality,
        }

        await query.edit_message_text(
            f"✅ تم حفظ الجودة الافتراضية: {quality}",
            reply_markup=settings_menu(),
        )
        return


# ============================================================
# Incoming text messages
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    if not await check_required_subscription(
        update,
        context,
    ):
        return

    text = update.message.text or ""
    urls = extract_urls(text)

    if not urls:
        await update.message.reply_text(
            "🔗 أرسل رابطًا صحيحًا من تيك توك "
            "أو إنستغرام أو فيسبوك أو موقع "
            "يدعمه yt-dlp.",
            reply_markup=main_menu(),
        )
        return

    url = urls[0]

    if not is_valid_url(url):
        await update.message.reply_text(
            "❌ الرابط غير صحيح."
        )
        return

    user_id = update.effective_user.id

    preferences = user_preferences.get(
        user_id,
        {},
    )

    mode = preferences.get(
        "mode",
        "video",
    )

    quality = preferences.get(
        "quality",
        "best",
    )

    # وضع الفيديو: نطلب اختيار الجودة
    if mode == "video":
        user_preferences[user_id] = {
            **preferences,
            "pending_url": url,
        }

        await update.message.reply_text(
            "🎬 اختر جودة الفيديو:",
            reply_markup=quality_menu(),
        )

        return

    await enqueue_job(
        update=update,
        url=url,
        mode=mode,
        quality=quality,
    )


# ============================================================
# Error handler
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled error: %s",
        context.error,
        exc_info=context.error,
    )


# ============================================================
# Application lifecycle
# ============================================================

async def post_init(
    application: Application,
):
    await start_workers(application)

    logger.info(
        "Bot initialized successfully."
    )

    logger.info(
        "yt-dlp version: %s",
        getattr(
            yt_dlp.version,
            "__version__",
            "unknown",
        ),
    )


async def post_shutdown(
    application: Application,
):
    for task in worker_tasks:
        task.cancel()

    worker_tasks.clear()

    logger.info(
        "Bot shutdown completed."
    )


# ============================================================
# Create application
# ============================================================

def create_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    application.add_error_handler(
        error_handler
    )

    return application


# ============================================================
# Main
# ============================================================

def main():
    application = create_application()

    logger.info(
        "Starting Telegram bot with polling..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
