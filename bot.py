import os
from flask import Flask
from threading import Thread

# 1. خادم الويب الوهمي لإرضاء ريندر وإظهار العلامة الخضراء
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# تشغيل الخادم في الخلفية فوراً
Thread(target=run_web, daemon=True).start()

# 2. تشغيل رسالة بسيطة للتأكد من أن السيرفر يعمل بدون أخطاء توكن حالياً
if __name__ == "__main__":
    print("Bot web service and keep-alive are running successfully.")
    # سنضيف تشغيل البوت بمجرد استقرار الخدمة وتأكيد ظهور العلامة الخضراء
    while True:
        pass
