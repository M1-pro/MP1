import os
import glob
import logging
import asyncio
import tempfile
import urllib.request
import urllib.parse
import json
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.error import TelegramError, BadRequest

import yt_dlp

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN", "8559243141:AAGYfIuTuZ2BCzjcFrENHfUquj_TDM5emgs")
CHANNELS = ["@Mi369iq", "@Mi_fanaa"]

async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    for channel in CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except TelegramError as e:
            logger.error(f"Error checking subscription for {channel}: {e}")
            return False
    return True

def quality_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("360p", callback_data="q_360"),
         InlineKeyboardButton("480p", callback_data="q_480"),
         InlineKeyboardButton("720p HD", callback_data="q_720")],
        [InlineKeyboardButton("1080p FHD", callback_data="q_1080"),
         InlineKeyboardButton("1440p / 2K", callback_data="q_1440")],
        [InlineKeyboardButton("أفضل جودة", callback_data="q_best")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name

    is_subscribed = await check_subscription(user_id, context)
    if not is_subscribed:
        keyboard = [
            [InlineKeyboardButton("اشترك في القناة الأولى 📢", url="https://t.me/Mi369iq")],
            [InlineKeyboardButton("اشترك في القناة الثانية 📢", url="https://t.me/Mi_fanaa")],
            [InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_sub")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"أهلاً بك يا {user_name} في بوت التنزيل الشامل 🎬\n\n"
            "❌ عذراً، يجب عليك الاشتراك في قناتينا أولاً لتتمكن من استخدام البوت.\n\n"
            "يرجى الاشتراك فيهما ثم اضغط على زر التحقق أدناه 👇",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return

    welcome_message = (
        f"أهلاً بك يا {user_name} في بوت التنزيل الشامل @EEMQ_BOT 🎬\n\n"
        "أرسل رابط أي من المنصات التالية (تيك توك، إنستغرام، فيسبوك):\n"
        "• إذا كان فيديو 🎞️: ستظهر لك خيارات الدقة (من 360p إلى 2K).\n"
        "• إذا كان صور 🖼️: سيتم جلب الصور، النص، والموسيقى تلقائياً دفعة واحدة!\n\n"
        "أرسل الرابط لنبدأ!"
    )
    await update.message.reply_text(welcome_message)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    is_subscribed = await check_subscription(user_id, context)
    if not is_subscribed:
        keyboard = [
            [InlineKeyboardButton("اشترك في القناة الأولى 📢", url="https://t.me/Mi369iq")],
            [InlineKeyboardButton("اشترك في القناة الثانية 📢", url="https://t.me/Mi_fanaa")],
            [InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_sub")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "❌ عذراً، يجب عليك الاشتراك في قناتينا أولاً لتتمكن من استخدام البوت.\n\n"
            "يرجى الاشتراك فيهما ثم اضغط على زر التحقق أدناه 👇",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return

    url = update.message.text.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("الرجاء إرسال رابط صالح يبدأ بـ http أو https.")
        return

    context.user_data["url"] = url

    if "tiktok.com" in url or "vt.tiktok.com" in url:
        await update.message.reply_text("⏳ جاري فحص الرابط...")
        folder = tempfile.mkdtemp(prefix="eemq_check_")
        try:
            loop = asyncio.get_running_loop()
            tk_data = await loop.run_in_executor(None, get_tiktok_data, url)
            if tk_data and tk_data.get("images"):
                files = await loop.run_in_executor(None, download_content, url, folder, "photos", "best")
                if files:
                    await update.message.reply_text("📤 جارٍ إرسال الصور والموسيقى والنص...")
                    await send_media_files(update.message, files)
                    await update.message.reply_text("✅ تم الانتهاء بنجاح!\n\n📥 أرسل رابطًا آخر.")
                    return
        except Exception as e:
            logger.error(f"Quick check error: {e}")
        finally:
            import shutil
            shutil.rmtree(folder, ignore_errors=True)

    await update.message.reply_text("🎬 اختر دقة الفيديو المطلوبة:", reply_markup=quality_keyboard())

def get_tiktok_data(url: str):
    try:
        api_url = f"https://tikwm.com/api/?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode())
            if data.get("code") == 0:
                return data.get("data")
    except Exception as e:
        logger.error(f"TikWM API Error: {e}")
    return None

def download_content(url, folder, media_type, quality):
    if "tiktok.com" in url or "vt.tiktok.com" in url:
        tk_data = get_tiktok_data(url)
        if tk_data:
            files = []
            desc = tk_data.get("title", "")
            if desc:
                desc_path = os.path.join(folder, "caption.txt")
                with open(desc_path, "w", encoding="utf-8") as f:
                    f.write(desc)
                files.append(desc_path)

            if "images" in tk_data and tk_data["images"]:
                for idx, img_url in enumerate(tk_data["images"]):
                    dest = os.path.join(folder, f"tiktok_img_{idx}.jpg")
                    urllib.request.urlretrieve(img_url, dest)
                    files.append(dest)
                audio_url = tk_data.get("music")
                if audio_url:
                    dest = os.path.join(folder, "tiktok_music.mp3")
                    urllib.request.urlretrieve(audio_url, dest)
                    files.append(dest)
                return files
            else:
                video_url = tk_data.get("play")
                if video_url:
                    dest = os.path.join(folder, "tiktok_video.mp4")
                    urllib.request.urlretrieve(video_url, dest)
                    files.append(dest)
                return files

    output = str(Path(folder) / "%(id)s_%(autonumber)s.%(ext)s")
    options = {
        "outtmpl": output,
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "writedescription": True,
    }

    if quality == "best":
        video_format = "best/bestvideo+bestaudio"
    else:
        video_format = f"best[height<={quality}]/bestvideo[height<={quality}]+bestaudio/best"
    
    options.update({"format": video_format, "merge_output_format": "mp4"})

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = []
    for path in glob.glob(os.path.join(folder, "**"), recursive=True):
        if not os.path.isfile(path):
            continue
        ext = Path(path).suffix.lower()
        if ext in [".part", ".ytdl", ".vtt", ".srt"]:
            continue
        files.append(path)
    return sorted(files)

async def send_media_files(message, files):
    images = [f for f in files if Path(f).suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]]
    videos = [f for f in files if Path(f).suffix.lower() in [".mp4", ".mkv", ".webm", ".mov"]]
    audios = [f for f in files if Path(f).suffix.lower() in [".mp3", ".m4a", ".aac", ".wav", ".opus"]]
    texts = [f for f in files if Path(f).suffix.lower() == ".txt"]

    caption_text = "✅ تم الانتهاء بنجاح عبر @EEMQ_BOT"
    for txt in texts:
        try:
            with open(txt, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    caption_text = f"{content}\n\n—\n✅ تم التنزيل بواسطة @EEMQ_BOT"
        except Exception:
            pass

    if images:
        for i in range(0, len(images), 10):
            batch = images[i:i+10]
            media_group = []
            opened_files = []
            try:
                for idx, img_path in enumerate(batch):
                    f_obj = open(img_path, "rb")
                    opened_files.append(f_obj)
                    if i == 0 and idx == 0:
                        media_group.append(InputMediaPhoto(media=f_obj, caption=caption_text))
                    else:
                        media_group.append(InputMediaPhoto(media=f_obj))
                if media_group:
                    await message.reply_media_group(media=media_group)
            finally:
                for f_obj in opened_files:
                    f_obj.close()

    for vid in videos:
        try:
            with open(vid, "rb") as f:
                await message.reply_video(video=f, caption=caption_text, supports_streaming=True)
        except Exception as e:
            logger.error(f"Video send error: {e}")
            with open(vid, "rb") as f:
                await message.reply_document(document=f, caption=caption_text)

    for aud in audios:
        try:
            with open(aud, "rb") as f:
                await message.reply_audio(audio=f, caption="🎵 الموسيقى المرتبطة بالمنشور عبر @EEMQ_BOT")
        except Exception as e:
            logger.error(f"Audio send error: {e}")

async def perform_download(update: Update, context: ContextTypes.DEFAULT_TYPE, quality: str = "best"):
    query = update.callback_query
    user_id = query.from_user.id

    if not await check_subscription(user_id, context):
        await query.edit_message_text("🔒 يجب الاشتراك في القناتين أولاً.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("اشترك في القناة الأولى 📢", url="https://t.me/Mi369iq")],
            [InlineKeyboardButton("اشترك في القناة الثانية 📢", url="https://t.me/Mi_fanaa")],
            [InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_sub")]
        ]))
        return

    url = context.user_data.get("url")
    if not url:
        await query.edit_message_text("❌ لم أجد الرابط. أرسل الرابط من جديد.")
        return

    await query.edit_message_text("⏳ جاري معالجة الرابط وتحميل الفيديو بالدقة المطلوبة...")
    folder = tempfile.mkdtemp(prefix="eemq_bot_")

    try:
        loop = asyncio.get_running_loop()
        files = await loop.run_in_executor(None, download_content, url, folder, "video", quality)

        if not files:
            await query.message.reply_text("❌ لم يتم العثور على ملفات قابلة للإرسال. تأكد أن الرابط عام ومتاح.")
            return

        await query.message.reply_text(f"📤 جارٍ إرسال الفيديو...")
        await send_media_files(query.message, files)
        await query.message.reply_text("✅ تم الانتهاء بنجاح!\n\n📥 أرسل رابطًا آخر.")
    except Exception as e:
        logger.error(f"Download process error: {e}")
        await query.message.reply_text("❌ حدث خطأ أثناء التنزيل. قد يكون الرابط خاصاً أو غير مدعوم.")
    finally:
        context.user_data.pop("url", None)
        import shutil
        shutil.rmtree(folder, ignore_errors=True)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    user_id = query.from_user.id

    if action == "check_sub":
        if await check_subscription(user_id, context):
            await query.edit_message_text("✅ تم التحقق بنجاح!\n\nأنت مشترك الآن، أرسل رابط المحتوى الذي تريد تحميله.", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ عذراً، لم تقم بالاشتراك في القناتين:\n1️⃣ @Mi369iq\n2️⃣ @Mi_fanaa\n\nيرجى الاشتراك ثم اضغط تحقق.", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("اشترك في القناة الأولى 📢", url="https://t.me/Mi369iq")],
                [InlineKeyboardButton("اشترك في القناة الثانية 📢", url="https://t.me/Mi_fanaa")],
                [InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_sub")]
            ]))
        return

    if action == "cancel":
        context.user_data.pop("url", None)
        await query.edit_message_text("❌ تم إلغاء العملية.")
        return

    if action.startswith("q_"):
        q_val = action.replace("q_", "")
        if q_val in ["360", "480", "720", "1080", "1440"]:
            res = q_val
        else:
            res = "best"
        await perform_download(update, context, quality=res)

def main():
    if not TOKEN:
        print("Error: TOKEN is not set!")
        return

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("✅ @EEMQ_BOT يعمل الآن بنجاح...")
    application.run_polling()

if __name__ == '__main__':
    main()
