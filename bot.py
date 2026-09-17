import asyncio
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

import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
    raise RuntimeError(
        "BOT_TOKEN غير موجود في متغيرات البيئة في Render"
    )

# =========================================================
# القنوات الإلزامية
# =========================================================

# القناة الأولى
CHANNEL_1_ID = "@Mi369iq"
CHANNEL_1_LINK = "https://t.me/Mi369iq"

# القناة الثانية
CHANNEL_2_ID = "@Mi_fanaa"
CHANNEL_2_LINK = "https://t.me/Mi_fanaa"

REQUIRED_CHANNELS = [
    CHANNEL_1_ID,
    CHANNEL_2_ID,
]

CHANNEL_LINKS = [
    CHANNEL_1_LINK,
    CHANNEL_2_LINK,
]

# =========================================================
# المشرفون
# =========================================================

# يمكنك وضع أرقام المشرفين في Render:
# ADMIN_IDS=123456789,987654321

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

# =========================================================
# حدود النظام
# =========================================================

MAX_FILE_SIZE_MB = int(
    os.getenv("MAX_FILE_SIZE_MB", "150")
)

MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")
)

MAX_QUEUE_SIZE = int(
    os.getenv("MAX_QUEUE_SIZE", "30")
)

DOWNLOAD_TIMEOUT = int(
    os.getenv("DOWNLOAD_TIMEOUT", "600")
)

# =========================================================
# إعدادات Render Webhook
# =========================================================

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    ""
).strip().rstrip("/")

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "MHM_secure_webhook_2026"
).strip()

PORT = int(os.getenv("PORT", "10000"))

# =========================================================
# بيانات النظام المؤقتة
# =========================================================

queue: asyncio.Queue = asyncio.Queue(
    maxsize=MAX_QUEUE_SIZE
)

download_semaphore = asyncio.Semaphore(
    MAX_CONCURRENT_DOWNLOADS
)

active_jobs = {}

worker_tasks = []

stats = {
    "received": 0,
    "completed": 0,
    "failed": 0,
    "cancelled": 0,
}

# =========================================================
# نموذج طلب التحميل
# =========================================================


@dataclass
class DownloadJob:
    user_id: int
    chat_id: int
    url: str
    mode: str = "auto"


# =========================================================
# أدوات عامة
# =========================================================


def is_valid_url(url: str) -> bool:
    """
    التحقق من أن الرابط من المنصات المدعومة.
    """

    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]

        allowed_hosts = (
            "tiktok.com",
            "instagram.com",
            "facebook.com",
            "fb.watch",
        )

        return (
            parsed.scheme in ("http", "https")
            and any(
                host == domain or host.endswith("." + domain)
                for domain in allowed_hosts
            )
        )

    except Exception:
        return False


def clean_url(text: str) -> Optional[str]:
    """
    استخراج الرابط من الرسالة.
    """

    match = re.search(
        r"https?://[^\s<>]+",
        text
    )

    if not match:
        return None

    url = match.group(0).rstrip(
        ".,!?)]}"
    )

    if not is_valid_url(url):
        return None

    return url


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()

    if "tiktok" in host:
        return "TikTok"

    if "instagram" in host:
        return "Instagram"

    if "facebook" in host or "fb.watch" in host:
        return "Facebook"

    return "غير معروف"


def human_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    return f"{size / (1024 * 1024):.1f} MB"


def get_queue_position(user_id: int) -> int:
    """
    إرجاع موقع طلب المستخدم في الطابور.
    """

    try:
        for index, job in enumerate(
            list(queue._queue),
            start=1
        ):
            if job.user_id == user_id:
                return index
    except Exception:
        pass

    return 0


# =========================================================
# التحقق من الاشتراك
# =========================================================


async def check_subscription(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
) -> bool:
    """
    يتحقق من اشتراك المستخدم في القناتين.

    يجب أن يكون البوت مشرفًا في القنوات
    حتى يستطيع التحقق من الأعضاء.
    """

    for channel in REQUIRED_CHANNELS:

        try:

            member = await context.bot.get_chat_member(
                chat_id=channel,
                user_id=user_id
            )

            if member.status in (
                "left",
                "kicked"
            ):
                return False

        except Exception as exc:

            logger.warning(
                "تعذر التحقق من القناة %s: %s",
                channel,
                exc
            )

            # منع الاستخدام إذا فشل التحقق
            return False

    return True


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 الاشتراك في القناة الأولى",
                url=CHANNEL_1_LINK
            )
        ],
        [
            InlineKeyboardButton(
                "📢 الاشتراك في القناة الثانية",
                url=CHANNEL_2_LINK
            )
        ],
        [
            InlineKeyboardButton(
                "✅ تحققت من الاشتراك",
                callback_data="check_subscription"
            )
        ],
    ])


async def require_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> bool:

    user = update.effective_user

    if not user:
        return False

    if await check_subscription(context, user.id):
        return True

    text = (
        "🔒 **يجب الاشتراك في القناتين أولًا لاستخدام البوت.**\n\n"
        "1️⃣ اشترك في القناة الأولى.\n"
        "2️⃣ اشترك في القناة الثانية.\n"
        "3️⃣ اضغط «تحققت من الاشتراك»."
    )

    if update.callback_query:

        await update.callback_query.answer()

        await update.callback_query.edit_message_text(
            text=text,
            reply_markup=subscription_keyboard(),
            parse_mode="Markdown"
        )

    elif update.message:

        await update.message.reply_text(
            text=text,
            reply_markup=subscription_keyboard(),
            parse_mode="Markdown"
        )

    return False


# =========================================================
# لوحة المفاتيح الرئيسية
# =========================================================


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📖 طريقة الاستخدام",
                callback_data="help"
            ),
            InlineKeyboardButton(
                "📊 حالة الطابور",
                callback_data="queue_status"
            ),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ حول البوت",
                callback_data="about"
            ),
        ],
    ])


# =========================================================
# الأوامر
# =========================================================


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_subscription(update, context):
        return

    text = (
        "🎬 **مرحبًا بك في بوت التحميل**\n\n"
        "يدعم البوت:\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Facebook\n\n"
        "📥 تحميل الفيديوهات والصور المتاحة.\n"
        "📝 استخراج عنوان المنشور ووصفه كنص.\n"
        "⏳ نظام طابور للطلبات.\n\n"
        "أرسل رابط المنشور الآن."
    )

    await update.message.reply_text(
        text=text,
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_subscription(update, context):
        return

    await update.message.reply_text(
        "📖 **طريقة الاستخدام**\n\n"
        "1️⃣ اشترك في القناتين.\n"
        "2️⃣ أرسل رابطًا عامًا.\n"
        "3️⃣ انتظر دورك في الطابور.\n"
        "4️⃣ سيصل الملف تلقائيًا.\n\n"
        "🔗 المنصات: TikTok / Instagram / Facebook\n\n"
        "⚠️ يجب أن يكون المحتوى عامًا ومتاحًا.",
        parse_mode="Markdown"
    )


async def queue_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await require_subscription(update, context):
        return

    user_id = update.effective_user.id
    position = get_queue_position(user_id)

    if user_id in active_jobs:
        user_status = "⚙️ لديك طلب قيد التحميل الآن."
    elif position:
        user_status = f"📍 موقع طلبك في الطابور: {position}"
    else:
        user_status = "ℹ️ لا يوجد لك طلب في الطابور."

    await update.message.reply_text(
        "📊 **حالة الطابور**\n\n"
        f"⏳ الطلبات المنتظرة: {queue.qsize()}\n"
        f"⚙️ التحميلات النشطة: {len(active_jobs)}\n"
        f"{user_status}",
        parse_mode="Markdown"
    )


async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or user.id not in ADMIN_IDS:
        return

    await update.message.reply_text(
        "📊 **إحصائيات البوت**\n\n"
        f"📥 المستلمة: {stats['received']}\n"
        f"✅ المكتملة: {stats['completed']}\n"
        f"❌ الفاشلة: {stats['failed']}\n"
        f"🚫 الملغاة: {stats['cancelled']}\n"
        f"⏳ في الطابور: {queue.qsize()}\n"
        f"⚙️ النشطة: {len(active_jobs)}",
        parse_mode="Markdown"
    )


# =========================================================
# الأزرار
# =========================================================


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    if query.data == "check_subscription":

        if await check_subscription(
            context,
            query.from_user.id
        ):

            await query.answer(
                "تم التحقق من اشتراكك ✅",
                show_alert=True
            )

            await query.edit_message_text(
                "✅ تم التحقق من اشتراكك بنجاح.\n\n"
                "أرسل رابط الفيديو أو المنشور الآن."
            )

        else:

            await query.answer(
                "لم يكتمل الاشتراك في القناتين ❌",
                show_alert=True
            )

            await query.edit_message_reply_markup(
                reply_markup=subscription_keyboard()
            )

        return

    if query.data == "help":

        await query.answer()

        await query.edit_message_text(
            "📖 **طريقة الاستخدام**\n\n"
            "أرسل رابطًا عامًا من TikTok أو Instagram أو Facebook.\n"
            "سيتم وضعه في الطابور ثم تحميله وإرساله لك.",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

        return

    if query.data == "about":

        await query.answer()

        await query.edit_message_text(
            "ℹ️ **حول البوت**\n\n"
            "بوت تحميل وسائط من المنصات الاجتماعية.\n"
            "يدعم الفيديوهات والصور المتاحة.\n"
            "يحتوي على نظام طابور وتنظيف تلقائي.\n\n"
            "برمجة وتطوير: مهدي الربيعي",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

        return

    if query.data == "queue_status":

        await query.answer()

        position = get_queue_position(
            query.from_user.id
        )

        await query.edit_message_text(
            "📊 **حالة الطابور**\n\n"
            f"⏳ الطلبات المنتظرة: {queue.qsize()}\n"
            f"⚙️ التحميلات النشطة: {len(active_jobs)}\n"
            f"📍 موقع طلبك: "
            f"{position if position else 'لا يوجد طلب'}",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )


# =========================================================
# yt-dlp
# =========================================================


def ytdlp_download(
    url: str,
    workdir: str
) -> dict:
    """
    تنزيل المحتوى من الرابط.
    """

    output_template = str(
        Path(workdir) / "%(id)s.%(ext)s"
    )

    options = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,

        # يدعم المنشورات التي تحتوي على أكثر من عنصر
        "noplaylist": False,
        "ignoreerrors": True,

        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,

        "restrictfilenames": True,

        # حفظ معلومات المنشور لاستخراج النص
        "writeinfojson": True,

        # أفضل صيغة متاحة
        "format": (
            "best[ext=mp4]/best[ext=webm]/best"
        ),

        "merge_output_format": "mp4",
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=True
        )

    return info or {}


def find_downloaded_files(
    workdir: str
) -> list[Path]:

    allowed_extensions = {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".m4v",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".mp3",
        ".m4a",
        ".opus",
        ".wav",
    }

    files = []

    for path in Path(workdir).iterdir():

        if not path.is_file():
            continue

        if path.suffix.lower() in allowed_extensions:
            files.append(path)

    return sorted(
        files,
        key=lambda p: p.stat().st_size,
        reverse=True
    )


def extract_text_from_info(info: dict) -> str:
    """
    استخراج النصوص المتاحة من معلومات المنشور.
    """

    if not info:
        return ""

    title = info.get("title") or ""
    description = info.get("description") or ""
    uploader = (
        info.get("uploader")
        or info.get("uploader_id")
        or ""
    )

    lines = []

    if uploader:
        lines.append(f"👤 الحساب: {uploader}")

    if title:
        lines.append(f"📝 العنوان: {title}")

    if description:
        lines.append(
            f"📄 النص / الوصف:\n{description[:3500]}"
        )

    return "\n\n".join(lines)


# =========================================================
# إرسال الملفات
# =========================================================


async def send_single_file(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    file_path: Path,
    caption: str = ""
) -> bool:

    if not file_path.exists():
        return False

    size = file_path.stat().st_size

    if size > MAX_FILE_SIZE_BYTES:

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ الملف "
                f"{file_path.name}\n"
                f"حجمه {human_size(size)}، "
                f"ويتجاوز الحد {MAX_FILE_SIZE_MB} MB."
            )
        )

        return False

    suffix = file_path.suffix.lower()

    with file_path.open("rb") as file:

        if suffix in (
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif"
        ):

            await context.bot.send_photo(
                chat_id=chat_id,
                photo=file,
                caption=caption[:1024] if caption else None
            )

        elif suffix in (
            ".mp4",
            ".mkv",
            ".webm",
            ".mov",
            ".avi",
            ".m4v"
        ):

            await context.bot.send_video(
                chat_id=chat_id,
                video=file,
                caption=caption[:1024] if caption else None,
                supports_streaming=True
            )

        elif suffix in (
            ".mp3",
            ".m4a",
            ".opus",
            ".wav"
        ):

            await context.bot.send_audio(
                chat_id=chat_id,
                audio=file,
                caption=caption[:1024] if caption else None
            )

        else:

            await context.bot.send_document(
                chat_id=chat_id,
                document=file,
                caption=caption[:1024] if caption else None
            )

    return True


async def send_downloaded_content(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    files: list[Path],
    info: dict
) -> bool:

    if not files:
        return False

    caption = extract_text_from_info(info)
    sent_any = False

    for index, file_path in enumerate(files):

        if not file_path.exists():
            continue

        try:

            current_caption = (
                caption
                if index == 0
                else ""
            )

            sent = await send_single_file(
                context=context,
                chat_id=chat_id,
                file_path=file_path,
                caption=current_caption
            )

            if sent:
                sent_any = True

            await asyncio.sleep(0.5)

        except Exception as exc:

            logger.exception(
                "فشل إرسال الملف %s: %s",
                file_path,
                exc
            )

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ تعذر إرسال الملف:\n"
                    f"{file_path.name}"
                )
            )

    return sent_any


# =========================================================
# معالجة الطلب
# =========================================================


async def process_download_job(
    job: DownloadJob,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = job.user_id
    chat_id = job.chat_id

    workdir = tempfile.mkdtemp(
        prefix=f"download_{user_id}_"
    )

    active_jobs[user_id] = {
        "url": job.url,
        "started_at": time.time(),
        "status": "downloading",
    }

    try:

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚙️ بدأ تحميل طلبك الآن...\n"
                f"🌐 المنصة: {detect_platform(job.url)}"
            )
        )

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.UPLOAD_DOCUMENT
        )

        info = await asyncio.wait_for(
            asyncio.to_thread(
                ytdlp_download,
                job.url,
                workdir
            ),
            timeout=DOWNLOAD_TIMEOUT
        )

        files = find_downloaded_files(workdir)

        valid_files = []

        for file_path in files:

            try:

                if (
                    file_path.stat().st_size
                    <= MAX_FILE_SIZE_BYTES
                ):
                    valid_files.append(file_path)

            except OSError:
                continue

        if not valid_files:

            text = extract_text_from_info(info)

            if text:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "📝 لم يتم العثور على ملف قابل للإرسال، "
                        "لكن هذه معلومات المنشور:\n\n"
                        f"{text[:4000]}"
                    )
                )

            else:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "❌ لم أتمكن من استخراج ملف من هذا الرابط.\n"
                        "تأكد أن الرابط عام ومتاح."
                    )
                )

            stats["failed"] += 1
            return

        sent = await send_downloaded_content(
            context=context,
            chat_id=chat_id,
            files=valid_files,
            info=info
        )

        if sent:

            stats["completed"] += 1

            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ اكتمل التحميل بنجاح."
            )

        else:

            stats["failed"] += 1

    except asyncio.TimeoutError:

        stats["failed"] += 1

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏰ انتهى وقت التحميل.\n"
                "قد يكون الرابط بطيئًا أو غير متاح."
            )
        )

    except Exception as exc:

        stats["failed"] += 1

        logger.exception(
            "خطأ أثناء معالجة الطلب: %s",
            exc
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ حدث خطأ أثناء التحميل.\n\n"
                "تأكد من أن الرابط عام وصحيح، "
                "ثم حاول مرة أخرى."
            )
        )

    finally:

        active_jobs.pop(user_id, None)

        # حذف الملفات المؤقتة تلقائيًا
        try:

            shutil.rmtree(
                workdir,
                ignore_errors=True
            )

            logger.info(
                "تم حذف الملفات المؤقتة: %s",
                workdir
            )

        except Exception as exc:

            logger.warning(
                "تعذر حذف المجلد المؤقت: %s",
                exc
            )


# =========================================================
# عامل الطابور
# =========================================================


async def queue_worker(
    worker_id: int,
    application: Application
):

    logger.info(
        "بدأ عامل الطابور رقم %s",
        worker_id
    )

    while True:

        job = await queue.get()

        try:

            async with download_semaphore:

                await process_download_job(
                    job=job,
                    context=application
                )

        except asyncio.CancelledError:

            logger.info(
                "تم إيقاف العامل رقم %s",
                worker_id
            )

            raise

        except Exception as exc:

            logger.exception(
                "خطأ في عامل الطابور: %s",
                exc
            )

        finally:

            queue.task_done()


# =========================================================
# استقبال الروابط
# =========================================================


async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    if not await require_subscription(update, context):
        return

    text = update.message.text or ""

    url = clean_url(text)

    if not url:

        await update.message.reply_text(
            "❌ أرسل رابطًا صحيحًا من TikTok أو Instagram أو Facebook."
        )

        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in active_jobs:

        await update.message.reply_text(
            "⏳ لديك طلب قيد المعالجة حاليًا.\n"
            "انتظر حتى يكتمل قبل إرسال طلب جديد."
        )

        return

    if queue.full():

        await update.message.reply_text(
            "🚫 الطابور ممتلئ حاليًا.\n"
            "حاول بعد قليل."
        )

        return

    job = DownloadJob(
        user_id=user_id,
        chat_id=chat_id,
        url=url,
        mode="auto"
    )

    try:

        queue.put_nowait(job)

        stats["received"] += 1

        position = queue.qsize()

        await update.message.reply_text(
            "📥 **تمت إضافة طلبك إلى الطابور**\n\n"
            f"🌐 المنصة: {detect_platform(url)}\n"
            f"📍 موقعك التقريبي: {position}\n"
            "⏳ سيتم التحميل تلقائيًا.",
            parse_mode="Markdown"
        )

    except asyncio.QueueFull:

        await update.message.reply_text(
            "🚫 الطابور ممتلئ. حاول لاحقًا."
        )


# =========================================================
# تشغيل العمال
# =========================================================


async def post_init(application: Application):

    global worker_tasks

    for worker_id in range(MAX_CONCURRENT_DOWNLOADS):

        task = asyncio.create_task(
            queue_worker(
                worker_id=worker_id + 1,
                application=application
            )
        )

        worker_tasks.append(task)

    logger.info(
        "تم تشغيل %s عامل تحميل",
        MAX_CONCURRENT_DOWNLOADS
    )


async def post_shutdown(application: Application):

    global worker_tasks

    for task in worker_tasks:
        task.cancel()

    if worker_tasks:

        await asyncio.gather(
            *worker_tasks,
            return_exceptions=True
        )

    worker_tasks.clear()

    logger.info(
        "تم إيقاف جميع العمال"
    )


# =========================================================
# بناء التطبيق
# =========================================================


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
        CommandHandler("queue", queue_command)
    )

    application.add_handler(
        CommandHandler("stats", stats_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    return application


# =========================================================
# التشغيل الرئيسي
# =========================================================


def main():

    application = build_application()

    logger.info(
        "البوت بدأ التشغيل"
    )

    if RENDER_EXTERNAL_URL:

        webhook_url = (
            f"{RENDER_EXTERNAL_URL}/webhook/"
            f"{WEBHOOK_SECRET}"
        )

        logger.info(
            "تشغيل Webhook على: %s",
            webhook_url
        )

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"webhook/{WEBHOOK_SECRET}",
            webhook_url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )

    else:

        logger.info(
            "RENDER_EXTERNAL_URL غير موجود، "
            "سيعمل البوت بوضع Polling."
        )

        application.run_polling(
            drop_pending_updates=True
        )


if __name__ == "__main__":
    main()
