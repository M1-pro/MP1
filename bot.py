import os
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import yt_dlp
import requests

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
CHANNELS = ["@Mi369iq", "@Mi_fanaa"]

def get_tiktok_video(url):
    try:
        api_url = f"https://www.tikwm.com/api/?url={url}"
        response = requests.get(api_url, timeout=10).json()
        if response.get("code") == 0:
            return response["data"]["play"]
    except Exception as e:
        logger.error(f"TikTok API Error: {e}")
    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    url = message.text.strip()
    
    if not url.startswith("http"):
        return

    sent_msg = await message.reply_text("⏳ جاري معالجة الرابط وتحميل الفيديو بالدقة المطلوبة...")

    try:
        download_url = None
        
        if "tiktok.com" in url or "vt.tiktok.com" in url:
            download_url = get_tiktok_video(url)
            if download_url:
                await message.reply_video(video=download_url, caption="✅ تم التحميل بنجاح بواسطة البوت")
                await sent_msg.delete()
                return

        ydl_opts = {
            'format': 'best',
            'outtmpl': 'downloads/%(id)s.%(ext)s',
            'noplaylist': True,
        }
        
        os.makedirs("downloads", exist_ok=True)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
        with open(filename, 'rb') as video_file:
            await message.reply_video(video=video_file, caption="✅ تم التحميل بنجاح بواسطة البوت")
            
        if os.path.exists(filename):
            os.remove(filename)
            
        await sent_msg.delete()

    except Exception as e:
        logger.error(f"Download Error: {e}")
        await sent_msg.edit_text("❌ حدث خطأ أثناء التنزيل. قد يكون الرابط خاصاً أو غير مدعوم.")

def main():
    if not TOKEN:
        logger.error("No BOT_TOKEN found in environment variables!")
        return

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
