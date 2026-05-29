# 🎵 بوت أغاني تلغرام

بوت تلغرام يبحث في موقع [mp3j.cc](https://mp3j.cc/ar) وينزل الأغاني مباشرة ويرسلها في تلغرام.

## الأوامر

| الأمر | الوظيفة |
|---|---|
| `يوت [اسم الأغنية]` | بحث في يوتيوب فقط |
| `بحث [اسم الأغنية]` | بحث شامل (SoundCloud + YouTube) |

### أمثلة
```
يوت طلال مداح
يوت محمد عبده
بحث بيلي ايليش
```

---

## 🚀 النشر على Railway (الطريقة الأسهل)

1. افتح [railway.com](https://railway.com) وسجّل دخول
2. اضغط **New Project** ← **Deploy from GitHub repo**
3. اختار **telegram-music-bot**
4. بعد ما يفتح المشروع، اضغط على الـ Service ← تبويب **Variables**
5. أضف متغير:
   - `TELEGRAM_BOT_TOKEN` = توكن البوت من @BotFather
6. Railway راح يشتغل تلقائياً ✅

> البوت يشتغل كـ worker (بدون بورت)، لا تحتاج إعدادات إضافية.

---

## 💻 التثبيت المحلي

### المتطلبات
- Python 3.11+
- ffmpeg مثبت على الجهاز

### خطوات

```bash
# نسخ المشروع
git clone https://github.com/nomak8658/telegram-music-bot.git
cd telegram-music-bot

# تثبيت المكتبات
pip install -r requirements.txt

# إضافة التوكن
export TELEGRAM_BOT_TOKEN="your_token_here"

# تشغيل البوت
python bot.py
```

---

## الملفات

| الملف | الوظيفة |
|---|---|
| `bot.py` | الكود الكامل للبوت |
| `requirements.txt` | المكتبات المطلوبة |
| `Procfile` | أمر تشغيل البوت على Railway |
| `railway.toml` | إعدادات Railway |
| `runtime.txt` | إصدار Python |

---

## الميزات
- ✅ يشتغل في المحادثات الخاصة والمجموعات
- ✅ بحث في SoundCloud و YouTube
- ✅ تنزيل مباشر بصيغة MP3
- ✅ دعم الأغاني العربية والإنجليزية
- ✅ إذا فشل مصدر يجرب مصدر ثاني تلقائياً
