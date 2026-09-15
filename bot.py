import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# إعدادات عامة
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود في متغيرات البيئة")

# معرفات المشرفين، مثال:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "150"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")
)

MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "30"))

MAX_IMAGES_PER_REQUEST = int(
    os.getenv("MAX_IMAGES_PER_REQUEST", "10")
)

DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "600"))

# رابط Render العام، مثال:
# https://your-bot.onrender.com
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

# مسار سري للـ Webhook
WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "telegram-webhook-secret-2026"
).strip()

# =========================================================
# بيانات مؤقتة في الذاكرة
# =========================================================

queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

user_preferences = {}
active_jobs = {}
stats = {
    "received": 0,
    "completed": 0,
    "failed": 0,
    "cancelled": 0,
}

worker_tasks = []


@dataclass
class DownloadJob:
    user_id: int
    chat_id: int
    url: str
    mode: str
    quality: str
    status_message_id: int
    created_at: float


# =========================================================
# أدوات مساعدة
# =========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def safe_filename(name: str, default: str = "download") -> str:
    name = name or default
    name = re.sub(r'[\\/*?:"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] or default


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def is_tiktok_short_url(url: str) -> bool:
    lowered = url.lower()
    return (
        "vm.tiktok.com" in lowered
        or "vt.tiktok.com" in lowered
    )


def resolve_short_url(url: str) -> str:
    """
    يحاول تحويل روابط TikTok المختصرة إلى الرابط الأصلي.
    إذا فشل، يعيد الرابط نفسه.
    """
    if not is_tiktok_short_url(url):
        return url

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13; Mobile) "
                    "AppleWebKit/537.36 "
                    "Chrome/125.0.0.0 Mobile Safari/537.36"
                )
            },
            allow_redirects=True,
            timeout=20,
        )

        if response.url:
            logger.info("Resolved URL: %s -> %s", url, response.url)
            return response.url

    except Exception as exc:
        logger.warning("Could not resolve short URL: %s", exc)

    return url


def extract_urls(text: str) -> list[str]:
    pattern = r"https?://[^\s<>\"]+"
    urls = re.findall(pattern, text or "")
    return [url.rstrip(".,!?)]}") for url in urls]


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"

    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"

    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def get_user_preference(user_id: int) -> dict:
    if user_id not in user_preferences:
        user_preferences[user_id] = {
            "mode": "video",
            "quality": "720",
        }

    return user_preferences[user_id]


def main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🎬 فيديو", callback_data="mode_video"),
            InlineKeyboardButton("🎵 صوت MP3", callback_data="mode_audio"),
        ],
        [
            InlineKeyboardButton("🖼 صور", callback_data="mode_images"),
            InlineKeyboardButton("📄 ملف", callback_data="mode_document"),
        ],
        [
            InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings"),
            InlineKeyboardButton("❓ المساعدة", callback_data="help"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def quality_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔹 360p", callback_data="quality_360"),
            InlineKeyboardButton("🔸 480p", callback_data="quality_480"),
        ],
        [
            InlineKeyboardButton("⭐ 720p", callback_data="quality_720"),
            InlineKeyboardButton("💎 1080p", callback_data="quality_1080"),
        ],
        [
            InlineKeyboardButton("↩️ رجوع", callback_data="back_main"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ إلغاء الطلب",
                    callback_data="cancel_current",
                )
            ]
        ]
    )


def mode_name(mode: str) -> str:
    return {
        "video": "فيديو",
        "audio": "صوت MP3",
        "images": "صور",
        "document": "ملف",
    }.get(mode, mode)


# =========================================================
# yt-dlp
# =========================================================

def build_ydl_options(
    mode: str,
    quality: str,
    output_dir: str,
) -> dict:
    output_template = os.path.join(
        output_dir,
        "%(title).150s_%(id)s.%(ext)s",
    )

    common = {
        "outtmpl": output_template,
        "noplaylist": False if mode == "images" else True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "ignoreerrors": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
        "overwrites": True,
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "max_filesize": MAX_FILE_SIZE_BYTES,
    }

    if mode == "audio":
        common.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )

    elif mode == "images":
        common.update(
            {
                "format": "best",
                "noplaylist": False,
            }
        )

    elif mode == "document":
        common.update(
            {
                "format": "bestvideo+bestaudio/best",
                "merge_output_format": "mp4",
            }
        )

    else:
        # فيديو
        if quality == "best":
            format_selector = (
                "bestvideo+bestaudio/best"
            )
        else:
            format_selector = (
                f"bestvideo[height<={quality}]"
                f"+bestaudio/"
                f"best[height<={quality}]/best"
            )

        common.update(
            {
                "format": format_selector,
                "merge_output_format": "mp4",
            }
        )

    return common


def download_with_ytdlp(
    url: str,
    mode: str,
    quality: str,
    output_dir: str,
) -> list[Path]:
    """
    دالة متزامنة؛ سيتم تشغيلها داخل asyncio.to_thread.
    """
    resolved_url = resolve_short_url(url)

    options = build_ydl_options(
        mode=mode,
        quality=quality,
        output_dir=output_dir,
    )

    logger.info(
        "Downloading | mode=%s | quality=%s | url=%s",
        mode,
        quality,
        resolved_url,
    )

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([resolved_url])

    files = []

    for path in Path(output_dir).rglob("*"):
        if not path.is_file():
            continue

        # تجاهل الملفات الوصفية
        if path.suffix.lower() in {
            ".part",
            ".ytdl",
            ".json",
            ".description",
            ".vtt",
            ".srt",
            ".ass",
        }:
            continue

        if path.stat().st_size <= 0:
            continue

        if path.stat().st_size > MAX_FILE_SIZE_BYTES:
            logger.warning("File too large: %s", path)
            continue

        files.append(path)

    files.sort(key=lambda p: p.stat().st_mtime)

    if mode == "images":
        files = files[:MAX_IMAGES_PER_REQUEST]
    else:
        files = files[:1]

    return files


# =========================================================
# إرسال الملفات إلى تيليجرام
# =========================================================

async def send_video_file(
    bot,
    chat_id: int,
    file_path: Path,
    caption: str,
):
    with file_path.open("rb") as file:
        await bot.send_video(
            chat_id=chat_id,
            video=file,
            caption=caption[:1024],
            supports_streaming=True,
        )


async def send_audio_file(
    bot,
    chat_id: int,
    file_path: Path,
    caption: str,
):
    with file_path.open("rb") as file:
        await bot.send_audio(
            chat_id=chat_id,
            audio=file,
            caption=caption[:1024],
            title=safe_filename(file_path.stem),
        )


async def send_document_file(
    bot,
    chat_id: int,
    file_path: Path,
    caption: str,
):
    with file_path.open("rb") as file:
        await bot.send_document(
            chat_id=chat_id,
            document=file,
            caption=caption[:1024],
        )


async def send_images(
    bot,
    chat_id: int,
    files: list[Path],
    caption: str,
):
    """
    Telegram يسمح بإرسال مجموعة صور بحد أقصى 10 صور.
    """
    media = []

    for index, file_path in enumerate(files):
        media.append(
            InputMediaPhoto(
                media=file_path.open("rb"),
                caption=caption[:1024] if index == 0 else None,
            )
        )

    try:
        if media:
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
            )
    finally:
        for item in media:
            try:
                item.media.close()
            except Exception:
                pass


async def send_downloaded_files(
    bot,
    chat_id: int,
    files: list[Path],
    mode: str,
    url: str,
):
    if not files:
        raise RuntimeError(
            "لم يتم العثور على ملف قابل للإرسال. "
            "قد يكون الرابط غير مدعوم أو محميًا."
        )

    caption = (
        "✅ تم التحميل بنجاح\n\n"
        f"📌 النوع: {mode_name(mode)}\n"
        f"📦 الحجم: {human_size(files[0].stat().st_size)}\n"
        "🤖 بواسطة بوت التحميل"
    )

    if mode == "images":
        await send_images(
            bot=bot,
            chat_id=chat_id,
            files=files,
            caption=caption,
        )
        return

    file_path = files[0]

    if mode == "audio":
        await send_audio_file(
            bot=bot,
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
        )

    elif mode == "document":
        await send_document_file(
            bot=bot,
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
        )

    else:
        # محاولة إرسال الفيديو، وإذا فشل نرسله كملف
        try:
            await send_video_file(
                bot=bot,
                chat_id=chat_id,
                file_path=file_path,
                caption=caption,
            )
        except Exception as exc:
            logger.warning(
                "send_video failed, sending as document: %s",
                exc,
            )

            await send_document_file(
                bot=bot,
                chat_id=chat_id,
                file_path=file_path,
                caption=caption,
            )


# =========================================================
# نظام الطابور
# =========================================================

async def process_job(
    application: Application,
    job: DownloadJob,
):
    bot = application.bot
    temp_dir = tempfile.mkdtemp(prefix="eemqbot_")

    try:
        active_jobs[job.user_id] = job

        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=(
                "⏳ بدأ تحميل طلبك الآن...\n\n"
                f"🎬 النوع: {mode_name(job.mode)}\n"
                f"📺 الجودة: {job.quality}p\n"
                "قد يستغرق التحميل وقتًا حسب حجم الملف."
            ),
            reply_markup=cancel_menu(),
        )

        async with download_semaphore:
            files = await asyncio.wait_for(
                asyncio.to_thread(
                    download_with_ytdlp,
                    job.url,
                    job.mode,
                    job.quality,
                    temp_dir,
                ),
                timeout=DOWNLOAD_TIMEOUT,
            )

        if job.user_id not in active_jobs:
            stats["cancelled"] += 1
            return

        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text="📤 اكتمل التحميل، جارٍ إرسال الملف إلى تيليجرام...",
        )

        await send_downloaded_files(
            bot=bot,
            chat_id=job.chat_id,
            files=files,
            mode=job.mode,
            url=job.url,
        )

        stats["completed"] += 1

        try:
            await bot.delete_message(
                chat_id=job.chat_id,
                message_id=job.status_message_id,
            )
        except Exception:
            pass

    except asyncio.TimeoutError:
        stats["failed"] += 1

        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=(
                "⌛ انتهى وقت التحميل.\n"
                "جرّب رابطًا آخر أو جودة أقل."
            ),
        )

    except Exception as exc:
        stats["failed"] += 1
        logger.exception("Job failed: %s", exc)

        error_text = str(exc)

        if "Unsupported URL" in error_text:
            reason = "هذا الرابط غير مدعوم من yt-dlp."
        elif "Sign in" in error_text or "login" in error_text.lower():
            reason = "الموقع يطلب تسجيل دخول أو يمنع التحميل."
        elif "File is larger" in error_text:
            reason = "حجم الملف أكبر من الحد المسموح."
        else:
            reason = (
                "تعذر تحميل الرابط. قد يكون الموقع محميًا "
                "أو الرابط منتهيًا أو غير مدعوم."
            )

        try:
            await bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.status_message_id,
                text=(
                    "❌ فشل التحميل\n\n"
                    f"السبب المحتمل: {reason}\n\n"
                    "جرّب:\n"
                    "• رابطًا آخر\n"
                    "• جودة أقل\n"
                    "• التأكد أن المنشور عام وليس خاصًا"
                ),
            )
        except Exception:
            pass

    finally:
        # حذف الملفات المؤقتة مهما كانت النتيجة
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as exc:
            logger.warning("Temp cleanup failed: %s", exc)

        active_jobs.pop(job.user_id, None)


async def queue_worker(application: Application):
    logger.info("Queue worker started")

    while True:
        job = await queue.get()

        try:
            await process_job(application, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected worker error")
        finally:
            queue.task_done()


# =========================================================
# أوامر البوت
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    preference = get_user_preference(user.id)

    text = (
        "👋 أهلًا بك في بوت التحميل الاحترافي\n\n"
        "🚀 يمكنك تحميل:\n"
        "🎬 فيديوهات\n"
        "🎵 صوتيات MP3\n"
        "🖼 صور\n"
        "📄 ملفات\n\n"
        f"⚙️ الإعداد الحالي: {mode_name(preference['mode'])}\n"
        f"📺 الجودة: {preference['quality']}p\n\n"
        "أرسل رابط المنشور بعد اختيار النوع."
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        "❓ طريقة الاستخدام\n\n"
        "1️⃣ اختر نوع التحميل.\n"
        "2️⃣ اختر جودة الفيديو إذا لزم.\n"
        "3️⃣ أرسل رابطًا عامًا.\n"
        "4️⃣ انتظر حتى يأتي دور طلبك.\n\n"
        "المواقع التي قد يدعمها yt-dlp تشمل بعض روابط:\n"
        "TikTok وInstagram وFacebook وYouTube وغيرها، "
        "لكن الدعم يتغير حسب الموقع والحماية.\n\n"
        "⚠️ لا يمكن تحميل المحتوى الخاص أو المحمي "
        "أو الذي يتطلب تجاوز حماية."
    )

    await update.message.reply_text(text)


async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text("⛔ هذا الأمر للمشرف فقط.")
        return

    text = (
        "📊 إحصائيات البوت\n\n"
        f"📥 الطلبات المستلمة: {stats['received']}\n"
        f"✅ المكتملة: {stats['completed']}\n"
        f"❌ الفاشلة: {stats['failed']}\n"
        f"🚫 الملغاة: {stats['cancelled']}\n"
        f"📦 داخل الطابور: {queue.qsize()}\n"
        f"⚙️ التحميلات النشطة: {len(active_jobs)}\n"
    )

    await update.message.reply_text(text)


async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        f"📦 عدد الطلبات في الطابور: {queue.qsize()}\n"
        f"⚙️ التحميلات النشطة: {len(active_jobs)}"
    )


# =========================================================
# أزرار الواجهة
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data
    preference = get_user_preference(user_id)

    if data == "mode_video":
        preference["mode"] = "video"

        await query.edit_message_text(
            "🎬 تم اختيار تحميل الفيديو.\n"
            "اختر الجودة:",
            reply_markup=quality_menu(),
        )
        return

    if data == "mode_audio":
        preference["mode"] = "audio"

        await query.edit_message_text(
            "🎵 تم اختيار تحميل الصوت بصيغة MP3.\n\n"
            "أرسل الرابط الآن.",
            reply_markup=main_menu(),
        )
        return

    if data == "mode_images":
        preference["mode"] = "images"

        await query.edit_message_text(
            "🖼 تم اختيار تحميل الصور.\n\n"
            "أرسل رابط المنشور أو الألبوم الآن.",
            reply_markup=main_menu(),
        )
        return

    if data == "mode_document":
        preference["mode"] = "document"

        await query.edit_message_text(
            "📄 تم اختيار إرسال المحتوى كملف.\n\n"
            "أرسل الرابط الآن.",
            reply_markup=main_menu(),
        )
        return

    if data.startswith("quality_"):
        quality = data.replace("quality_", "")
        preference["quality"] = quality
        preference["mode"] = "video"

        await query.edit_message_text(
            f"✅ تم اختيار جودة {quality}p.\n\n"
            "أرسل رابط الفيديو الآن.",
            reply_markup=main_menu(),
        )
        return

    if data == "settings":
        await query.edit_message_text(
            "⚙️ إعداداتك الحالية\n\n"
            f"🎬 النوع: {mode_name(preference['mode'])}\n"
            f"📺 الجودة: {preference['quality']}p\n\n"
            "يمكنك تغيير النوع من القائمة:",
            reply_markup=main_menu(),
        )
        return

    if data == "help":
        await query.edit_message_text(
            "❓ أرسل رابطًا عامًا بعد اختيار نوع التحميل.\n\n"
            "يدعم البوت المواقع التي يستطيع yt-dlp التعامل معها، "
            "وقد لا تعمل بعض الروابط الخاصة أو المحمية.",
            reply_markup=main_menu(),
        )
        return

    if data == "back_main":
        await query.edit_message_text(
            "🏠 القائمة الرئيسية",
            reply_markup=main_menu(),
        )
        return

    if data == "cancel_current":
        job = active_jobs.get(user_id)

        if job:
            active_jobs.pop(user_id, None)
            stats["cancelled"] += 1

            await query.edit_message_text(
                "🚫 تم إلغاء الطلب من قائمة المتابعة.\n"
                "إذا كان التحميل بدأ فعليًا فقد يحتاج النظام "
                "ثوانٍ قليلة حتى ينتهي تنظيف الملفات."
            )
        else:
            await query.edit_message_text(
                "ℹ️ لا يوجد لديك تحميل نشط حاليًا.",
                reply_markup=main_menu(),
            )


# =========================================================
# استقبال الروابط
# =========================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    urls = extract_urls(text)

    if not urls:
        await update.message.reply_text(
            "⚠️ أرسل رابطًا صحيحًا يبدأ بـ http أو https.",
            reply_markup=main_menu(),
        )
        return

    url = urls[0]

    if not is_valid_url(url):
        await update.message.reply_text(
            "❌ الرابط غير صالح.",
            reply_markup=main_menu(),
        )
        return

    if user.id in active_jobs:
        await update.message.reply_text(
            "⏳ لديك طلب تحميل قيد المعالجة بالفعل.\n"
            "انتظر حتى ينتهي قبل إرسال طلب آخر."
        )
        return

    if queue.full():
        await update.message.reply_text(
            "🚦 الطابور ممتلئ حاليًا.\n"
            "حاول بعد قليل."
        )
        return

    preference = get_user_preference(user.id)

    status_message = await update.message.reply_text(
        "📥 تمت إضافة طلبك إلى الطابور...\n\n"
        f"📌 النوع: {mode_name(preference['mode'])}\n"
        f"📺 الجودة: {preference['quality']}p\n"
        f"📦 ترتيب تقريبي: {queue.qsize() + 1}",
        reply_markup=cancel_menu(),
    )

    job = DownloadJob(
        user_id=user.id,
        chat_id=chat_id,
        url=url,
        mode=preference["mode"],
        quality=preference["quality"],
        status_message_id=status_message.message_id,
        created_at=time.time(),
    )

    stats["received"] += 1

    await queue.put(job)


# =========================================================
# تشغيل وإيقاف التطبيق
# =========================================================

async def post_init(application: Application):
    global worker_tasks

    # عامل واحد يستهلك الطابور بالتسلسل.
    # semaphore يسمح لاحقًا بتوسيع عدد العمال إذا احتجت.
    worker_tasks = [
        asyncio.create_task(queue_worker(application))
    ]

    logger.info("Bot initialized")
    logger.info("Queue size: %s", MAX_QUEUE_SIZE)
    logger.info(
        "Max concurrent downloads: %s",
        MAX_CONCURRENT_DOWNLOADS,
    )


async def post_shutdown(application: Application):
    for task in worker_tasks:
        task.cancel()

    if worker_tasks:
        await asyncio.gather(
            *worker_tasks,
            return_exceptions=True,
        )

    logger.info("Bot shutdown complete")


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
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
        CommandHandler("queue", queue_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    return application


def main():
    application = build_application()

    port = int(os.getenv("PORT", "10000"))

    if RENDER_EXTERNAL_URL:
        webhook_url = (
            f"{RENDER_EXTERNAL_URL}/"
            f"{WEBHOOK_SECRET}"
        )

        logger.info("Starting webhook mode")
        logger.info("Webhook URL: %s", webhook_url)

        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=WEBHOOK_SECRET,
            webhook_url=webhook_url,
            drop_pending_updates=True,
            secret_token=WEBHOOK_SECRET,
        )

    else:
        logger.info("Starting polling mode")

        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )


if __name__ == "__main__":
    main()
