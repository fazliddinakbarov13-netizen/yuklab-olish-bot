"""
Telegram Video/Image Downloader Bot
====================================
YouTube, Instagram, Pinterest dan video/rasm yuklab olish boti.

YouTube uchun: pytubefix (cookie kerak emas!) + sifat tanlash + FFmpeg merge
Instagram/Pinterest uchun: yt-dlp
"""

import os
import re
import sys
import glob
import time
import asyncio
import logging
import subprocess
from pathlib import Path

# Windows konsol muammosini hal qilish
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ─────────────────────────────────────────────
# ⚙️  SOZLAMALAR
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8738914730:AAFLlh4_omzDLi36zZyJ-EiMHReHt3L2EIE")

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

TG_FILE_LIMIT = 50 * 1024 * 1024

# FFmpeg yo'li (imageio-ffmpeg orqali)
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG_PATH = "ffmpeg"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _safe_filesize(stream) -> int:
    """Stream hajmini xavfsiz olish (xatolik bo'lsa 0 qaytaradi)."""
    try:
        return stream.filesize or 0
    except Exception:
        return 0


def _cleanup_old_files(max_age_seconds: int = 3600):
    """1 soatdan eski fayllarni tozalash."""
    try:
        now = time.time()
        for f in DOWNLOAD_DIR.glob("*"):
            if f.is_file() and (now - f.stat().st_mtime) > max_age_seconds:
                f.unlink(missing_ok=True)
                logger.info(f"Eski fayl tozalandi: {f.name}")
    except Exception as e:
        logger.warning(f"Tozalashda xatolik: {e}")


# ─────────────────────────────────────────────
# 🔍  Platform aniqlash
# ─────────────────────────────────────────────
PLATFORM_PATTERNS = {
    "youtube": re.compile(
        r"(https?://)?(www\.)?"
        r"(youtube\.com|youtu\.be|youtube\.com/shorts)"
        r"(/\S+)?"
    ),
    "instagram": re.compile(
        r"(https?://)?(www\.)?"
        r"(instagram\.com|instagr\.am)"
        r"(/\S+)?"
    ),
    "pinterest": re.compile(
        r"(https?://)?(www\.|pin\.)?"
        r"(pinterest\.\w+|pin\.it)"
        r"(/\S+)?"
    ),
}

PLATFORM_EMOJI = {
    "youtube": "🎬 YouTube",
    "instagram": "📸 Instagram",
    "pinterest": "📌 Pinterest",
}


def detect_platform(url: str) -> str | None:
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return platform
    return None


def extract_url(text: str) -> str | None:
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


# ─────────────────────────────────────────────
# 📥  YouTube: mavjud sifatlarni olish (progressive + adaptive)
# ─────────────────────────────────────────────
def get_youtube_qualities(url: str) -> dict:
    """YouTube video uchun barcha mavjud sifatlarni qaytaradi."""
    from pytubefix import YouTube

    yt = YouTube(url)

    available = {}

    # 1. Progressive streamlar (video+audio birgalikda, past sifat)
    progressive = yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc()
    for s in progressive:
        res = s.resolution
        if res and res not in available:
            fsize = _safe_filesize(s)
            size_mb = round(fsize / (1024 * 1024), 1) if fsize else 0
            available[res] = {"size_mb": size_mb, "type": "progressive"}

    # 2. Adaptive streamlar (faqat video, audio alohida — FFmpeg bilan birlashtiriladi)
    adaptive = yt.streams.filter(adaptive=True, file_extension="mp4", only_video=True).order_by("resolution").desc()
    for s in adaptive:
        res = s.resolution
        if res and res not in available:
            fsize = _safe_filesize(s)
            video_size = fsize / (1024 * 1024) if fsize else 0
            total_size = round(video_size + 2.0, 1)
            available[res] = {"size_mb": total_size, "type": "adaptive"}

    # Sifat bo'yicha tartiblash (yuqoridan pastga)
    res_order = {"2160p": 1, "1440p": 2, "1080p": 3, "720p": 4, "480p": 5, "360p": 6, "240p": 7, "144p": 8}
    sorted_available = dict(
        sorted(available.items(), key=lambda x: res_order.get(x[0], 99))
    )

    return {
        "title": yt.title,
        "duration": yt.length,
        "streams": sorted_available,
    }


# ─────────────────────────────────────────────
# 🗜️  Video siqish (50 MB dan katta bo'lsa)
# ─────────────────────────────────────────────
def compress_video(input_path: str, max_size_mb: int = 49) -> str:
    """
    FFmpeg bilan videoni siqadi, hajmini max_size_mb gacha tushiradi.
    Sifatni imkon qadar saqlaydi.
    """
    file_size = os.path.getsize(input_path)
    max_size_bytes = max_size_mb * 1024 * 1024

    if file_size <= max_size_bytes:
        return input_path  # Siqish kerak emas

    # Video davomiyligini aniqlash (FFprobe bilan)
    try:
        probe_cmd = [
            FFMPEG_PATH, "-i", input_path,
            "-f", "null", "-"
        ]
        probe_result = subprocess.run(
            probe_cmd, capture_output=True, timeout=30
        )
        duration_match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+\.\d+)",
            probe_result.stderr.decode(errors="replace")
        )
        if duration_match:
            hours = int(duration_match.group(1))
            mins = int(duration_match.group(2))
            secs = float(duration_match.group(3))
            duration = hours * 3600 + mins * 60 + secs
        else:
            duration = 180  # Fallback: 3 daqiqa
    except Exception:
        duration = 180

    # Target bitrate hisoblash (bit/s)
    # max_size_bytes * 8 / duration = total_bitrate
    # Audio uchun 128kbps ajratamiz
    audio_bitrate = 128 * 1024  # 128 kbps
    total_bitrate = (max_size_bytes * 8) / duration
    video_bitrate = int(total_bitrate - audio_bitrate)

    if video_bitrate < 200_000:  # Minimal 200 kbps
        video_bitrate = 200_000

    output_path = input_path.replace(".mp4", "_compressed.mp4")

    logger.info(f"Video siqilmoqda: {file_size//(1024*1024)} MB -> ~{max_size_mb} MB (bitrate: {video_bitrate//1024} kbps, duration: {duration:.0f}s)")

    try:
        cmd = [
            FFMPEG_PATH,
            "-y",
            "-i", input_path,
            "-c:v", "libx264",
            "-b:v", str(video_bitrate),
            "-maxrate", str(int(video_bitrate * 1.5)),
            "-bufsize", str(int(video_bitrate * 2)),
            "-preset", "fast",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=600)

        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            compressed_size = os.path.getsize(output_path)
            logger.info(f"Siqish muvaffaqiyatli: {file_size//(1024*1024)} MB -> {compressed_size//(1024*1024)} MB")
            _safe_remove(input_path)
            return output_path
        else:
            logger.error(f"Siqish xatolik: {result.stderr.decode(errors='replace')[-300:]}")
            _safe_remove(output_path)
            return input_path
    except Exception as e:
        logger.error(f"Siqish istisno: {e}")
        _safe_remove(output_path)
        return input_path


# ─────────────────────────────────────────────
# 📥  YouTube yuklab olish (pytubefix + FFmpeg merge)
# ─────────────────────────────────────────────
async def download_youtube(url: str, chat_id: int, resolution: str = None) -> list[dict]:
    """pytubefix orqali YouTube video yuklab oladi. Yuqori sifatda FFmpeg bilan birlashtiradi."""
    from pytubefix import YouTube

    loop = asyncio.get_running_loop()

    def _download():
        yt = YouTube(url)
        video_id = yt.video_id

        # 1. Avval progressive (video+audio birgalikda) da qidirish
        if resolution:
            stream = yt.streams.filter(
                progressive=True, file_extension="mp4", resolution=resolution
            ).first()
        else:
            stream = yt.streams.filter(
                progressive=True, file_extension="mp4"
            ).order_by("resolution").desc().first()

        # Progressive topilsa — to'g'ridan-to'g'ri yuklab berish
        if stream:
            filename = f"{chat_id}_{video_id}.mp4"
            filepath = stream.download(output_path=str(DOWNLOAD_DIR), filename=filename)
            logger.info(f"Progressive yuklab olindi: {filepath} ({os.path.getsize(filepath)} bytes)")
            return filepath

        # 2. Progressive bo'lmasa — adaptive (video + audio alohida) yuklab, FFmpeg bilan birlashtirish
        if resolution:
            video_stream = yt.streams.filter(
                adaptive=True, file_extension="mp4", only_video=True, resolution=resolution
            ).first()
        else:
            video_stream = yt.streams.filter(
                adaptive=True, file_extension="mp4", only_video=True
            ).order_by("resolution").desc().first()

        if not video_stream:
            # Fallback: har qanday stream
            stream = yt.streams.first()
            if stream:
                filename = f"{chat_id}_{video_id}.mp4"
                return stream.download(output_path=str(DOWNLOAD_DIR), filename=filename)
            raise RuntimeError("Video stream topilmadi.")

        # Eng yaxshi audio stream
        audio_stream = yt.streams.filter(
            adaptive=True, only_audio=True
        ).order_by("abr").desc().first()

        if not audio_stream:
            raise RuntimeError("Audio stream topilmadi.")

        # Audio format kengaytmasini aniqlash (webm, m4a, mp4 bo'lishi mumkin)
        audio_ext = audio_stream.subtype or "mp4"
        logger.info(f"Video stream: {video_stream.resolution}, Audio stream: {audio_stream.abr} ({audio_ext})")

        # Video va audio ni alohida yuklab olish
        video_filename = f"{chat_id}_{video_id}_video.mp4"
        audio_filename = f"{chat_id}_{video_id}_audio.{audio_ext}"
        output_filename = f"{chat_id}_{video_id}.mp4"

        video_path = video_stream.download(output_path=str(DOWNLOAD_DIR), filename=video_filename)
        audio_path = audio_stream.download(output_path=str(DOWNLOAD_DIR), filename=audio_filename)
        output_path = str(DOWNLOAD_DIR / output_filename)

        logger.info(f"Video yuklab olindi: {os.path.getsize(video_path)} bytes")
        logger.info(f"Audio yuklab olindi: {os.path.getsize(audio_path)} bytes")

        # FFmpeg bilan birlashtirish
        merge_success = False
        try:
            cmd = [
                FFMPEG_PATH,
                "-y",
                "-i", video_path,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-strict", "experimental",
                "-movflags", "+faststart",
                output_path,
            ]
            logger.info(f"FFmpeg buyrug'i: {' '.join(cmd[:4])} ...")
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=300,
            )
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                merge_success = True
                logger.info(f"FFmpeg muvaffaqiyatli! Output: {os.path.getsize(output_path)} bytes")
            else:
                logger.error(f"FFmpeg xatolik (code={result.returncode}): {result.stderr.decode(errors='replace')[-500:]}")
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg timeout (300s)")
        except Exception as e:
            logger.error(f"FFmpeg istisno: {e}")

        if merge_success:
            # Birlashtirish muvaffaqiyatli — vaqtinchalik fayllarni o'chirish
            _safe_remove(video_path)
            _safe_remove(audio_path)
            return output_path
        else:
            # Birlashtirish muvaffaqiyatsiz — faqat video ni qaytarish (ovozisz)
            _safe_remove(audio_path)
            _safe_remove(output_path)
            logger.warning("FFmpeg muvaffaqiyatsiz, faqat video qaytarilmoqda (ovozsiz)")
            return video_path

    filepath = await loop.run_in_executor(None, _download)

    # Agar fayl 50 MB dan katta bo'lsa — avtomatik siqish
    if os.path.getsize(filepath) > TG_FILE_LIMIT:
        logger.info(f"Fayl katta ({os.path.getsize(filepath)//(1024*1024)} MB), siqilmoqda...")
        filepath = await loop.run_in_executor(None, compress_video, filepath)

    return [{"path": filepath, "type": "video"}]


# ─────────────────────────────────────────────
# 📥  Instagram/Pinterest yuklab olish (yt-dlp)
# ─────────────────────────────────────────────
async def download_with_ytdlp(url: str, platform: str, chat_id: int) -> list[dict]:
    """yt-dlp orqali Instagram/Pinterest dan yuklab oladi."""
    import yt_dlp

    # Unikal ID — boshqa yuklashlar bilan aralashmasligi uchun
    unique_id = f"{chat_id}_{int(time.time())}"
    output_template = str(DOWNLOAD_DIR / f"{unique_id}_%(id)s.%(ext)s")

    if platform == "instagram":
        ydl_opts = {
            "format": "best[ext=mp4]/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
    elif platform == "pinterest":
        ydl_opts = {
            "format": "best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
    else:
        raise ValueError(f"Noma'lum platforma: {platform}")

    loop = asyncio.get_running_loop()

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return info

    info = await loop.run_in_executor(None, _download)
    if info is None:
        raise RuntimeError("Media ma'lumotlarini olishda xatolik.")

    # Faqat shu yuklash fayllarini topish (unique_id bilan)
    downloaded_files = sorted(
        glob.glob(str(DOWNLOAD_DIR / f"{unique_id}_*")),
        key=os.path.getmtime,
        reverse=True,
    )

    if not downloaded_files:
        raise FileNotFoundError("Yuklab olingan fayl topilmadi.")

    filepath = downloaded_files[0]
    ext = Path(filepath).suffix.lower()

    if ext in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
        return [{"path": filepath, "type": "video"}]
    elif ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return [{"path": filepath, "type": "photo"}]
    else:
        return [{"path": filepath, "type": "video"}]


# ─────────────────────────────────────────────
# 🤖  Bot handlerlari
# ─────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "👋 <b>Assalomu alaykum!</b>\n\n"
        "Men video va rasmlarni yuklab beruvchi botman.\n\n"
        "📌 <b>Qo'llab-quvvatlanadigan platformalar:</b>\n"
        "  🎬 YouTube — sifat tanlash bilan (360p—4K)\n"
        "  📸 Instagram (reels, postlar)\n"
        "  📌 Pinterest (video, rasmlar)\n\n"
        "📥 <b>Foydalanish:</b>\n"
        "Menga shunchaki havola yuboring!\n\n"
        "🔗 <b>Misol:</b>\n"
        "<code>https://youtube.com/watch?v=xxx</code>\n"
        "<code>https://www.instagram.com/reel/xxx</code>\n"
        "<code>https://pin.it/xxx</code>"
    )
    await update.message.reply_text(welcome, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "ℹ️ <b>Yordam</b>\n\n"
        "Bu bot YouTube, Instagram va Pinterest dan "
        "video va rasmlarni yuklab beradi.\n\n"
        "📋 <b>Komandalar:</b>\n"
        "/start - Botni ishga tushirish\n"
        "/help - Yordam\n\n"
        "🎬 <b>YouTube sifat tanlash:</b>\n"
        "YouTube havolasini yuborganingizda, sizga\n"
        "barcha mavjud sifatlar ko'rsatiladi:\n"
        "• 360p, 480p — past sifat (kichik hajm)\n"
        "• 720p HD — o'rta sifat\n"
        "• 1080p Full HD — yuqori sifat\n"
        "• 1440p, 2160p 4K — eng yuqori sifat\n\n"
        "⚠️ <b>Eslatma:</b>\n"
        "• Telegram 50 MB gacha fayllarni qo'llab-quvvatlaydi\n"
        "• Yuqori sifat = kattaroq fayl hajmi\n"
        "• Ba'zi shaxsiy akkauntlardagi postlar yuklanmasligi mumkin"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


def _format_duration(seconds: int) -> str:
    if not seconds:
        return "—"
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}:{secs:02d}"


def _extract_video_id(url: str) -> str:
    match = re.search(r"youtu\.be/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]v=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"/shorts/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    return url[-11:]


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text

    url = extract_url(text)
    if not url:
        await update.message.reply_text(
            "❌ Iltimos, to'g'ri havola yuboring.\n"
            "Masalan: https://youtube.com/watch?v=xxx"
        )
        return

    platform = detect_platform(url)
    if not platform:
        await update.message.reply_text(
            "❌ Bu platforma qo'llab-quvvatlanmaydi.\n\n"
            "✅ Qo'llab-quvvatlanadigan platformalar:\n"
            "• YouTube\n• Instagram\n• Pinterest"
        )
        return

    # YouTube — sifat tanlash
    if platform == "youtube":
        await handle_youtube_quality(update, context, url)
        return

    # Instagram / Pinterest — to'g'ridan-to'g'ri yuklab berish
    platform_name = PLATFORM_EMOJI.get(platform, platform)
    status_msg = await update.message.reply_text(
        f"⏳ Yuklanmoqda... ({platform_name})\n"
        f"Iltimos, biroz kuting..."
    )

    try:
        media_list = await download_with_ytdlp(url, platform, update.effective_chat.id)
        await send_media(update, status_msg, media_list, platform_name)
    except Exception as e:
        logger.error(f"Xatolik: {e}", exc_info=True)
        error_text = _get_error_message(e, platform)
        try:
            await status_msg.edit_text(error_text)
        except Exception:
            await update.message.reply_text(error_text)


async def handle_youtube_quality(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """YouTube uchun sifat tanlash tugmalarini ko'rsatadi."""
    wait_msg = await update.message.reply_text("🔍 Video ma'lumotlari olinmoqda...")

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, get_youtube_qualities, url)

        if not info["streams"]:
            await wait_msg.edit_text(
                "❌ Bu video uchun mavjud sifatlar topilmadi.\n"
                "Boshqa havola bilan urinib ko'ring."
            )
            return

        video_id = _extract_video_id(url)
        cb_vid = video_id[:20]

        # URL ni contextda saqlash
        if "yt_urls" not in context.user_data:
            context.user_data["yt_urls"] = {}
        context.user_data["yt_urls"][cb_vid] = url

        # Inline tugmalar yaratish
        buttons = []
        for res, info_data in info["streams"].items():
            size_mb = info_data["size_mb"]
            stream_type = info_data["type"]

            # 200 MB dan katta bo'lsa, o'tkazib yuborish (juda uzoq siqiladi)
            if size_mb > 200:
                continue

            size_text = f"{size_mb} MB" if size_mb > 0 else "?"
            will_compress = size_mb > 48

            # Rangli emoji
            if size_mb <= 20:
                emoji = "🟢"
            elif size_mb <= 40:
                emoji = "🟡"
            else:
                emoji = "🔴"

            # HD/FHD/4K belgilari
            label = res
            if res == "2160p":
                label = "2160p 4K"
            elif res == "1440p":
                label = "1440p 2K"
            elif res == "1080p":
                label = "1080p FHD"
            elif res == "720p":
                label = "720p HD"

            # Siqilishi kerak bo'lsa belgi
            compress_mark = " ⚡" if will_compress else ""

            # Telegram callback_data limiti 64 bayt
            cb_vid = video_id[:20]
            callback_data = f"yt|{res}|{cb_vid}"

            buttons.append([
                InlineKeyboardButton(
                    f"{emoji} {label}  •  {size_text}{compress_mark}",
                    callback_data=callback_data,
                )
            ])

        duration = _format_duration(info.get("duration"))
        title = info.get("title", "Noma'lum")
        if len(title) > 200:
            title = title[:197] + "..."

        keyboard = InlineKeyboardMarkup(buttons)

        await wait_msg.edit_text(
            f"🎬 <b>{_escape_html(title)}</b>\n"
            f"⏱ Davomiyligi: {duration}\n\n"
            f"📐 <b>Sifatni tanlang:</b>\n"
            f"<i>🟢 yengil  🟡 o'rta  🔴 katta  ⚡ siqiladi</i>",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"YouTube info xatolik: {e}", exc_info=True)
        try:
            await wait_msg.edit_text(
                f"❌ Video ma'lumotlarini olishda xatolik.\n"
                f"💬 {str(e)[:200]}"
            )
        except Exception:
            pass


async def handle_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sifat tugmasi bosilganda video yuklab yuboradi."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("yt|"):
        return

    parts = data.split("|", 2)
    if len(parts) != 3:
        return

    _, resolution, video_id = parts

    # Contextdan URL olish
    urls = context.user_data.get("yt_urls", {})
    url = urls.get(video_id, f"https://youtu.be/{video_id}")

    await query.edit_message_text(
        f"⏳ Yuklanmoqda... (🎬 YouTube — {resolution})\n"
        f"Iltimos, biroz kuting..."
    )

    try:
        media_list = await download_youtube(url, query.message.chat_id, resolution)

        if not media_list:
            await query.edit_message_text("❌ Video yuklab olinmadi.")
            return

        sent_count = 0
        for media in media_list:
            filepath = media["path"]

            if not os.path.exists(filepath):
                logger.error(f"Fayl topilmadi: {filepath}")
                continue

            file_size = os.path.getsize(filepath)
            logger.info(f"Yuborilmoqda: {filepath} ({file_size} bytes)")

            if file_size == 0:
                logger.error(f"Fayl bo'sh (0 bytes): {filepath}")
                _safe_remove(filepath)
                continue

            if file_size > TG_FILE_LIMIT:
                await query.edit_message_text(
                    f"⚠️ Fayl juda katta ({file_size // (1024*1024)} MB).\n"
                    f"Telegram {TG_FILE_LIMIT // (1024*1024)} MB gacha "
                    f"qo'llab-quvvatlaydi.\n\n"
                    f"💡 Pastroq sifatni tanlang."
                )
                _safe_remove(filepath)
                return

            try:
                with open(filepath, "rb") as f:
                    await query.message.reply_video(
                        video=f,
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=30,
                    )
                sent_count += 1
            except Exception as send_err:
                logger.error(f"Video yuborishda xatolik: {send_err}", exc_info=True)
            finally:
                _safe_remove(filepath)

        if sent_count > 0:
            await query.edit_message_text(
                f"✅ Muvaffaqiyatli yuklandi! (🎬 YouTube — {resolution})"
            )
        else:
            await query.edit_message_text(
                "❌ Faylni yuborishda xatolik yuz berdi.\n"
                "Boshqa sifatni tanlang yoki qayta urinib ko'ring."
            )

    except Exception as e:
        logger.error(f"YouTube yuklab olish xatolik: {e}", exc_info=True)
        error_text = _get_error_message(e, "youtube")
        try:
            await query.edit_message_text(error_text)
        except Exception:
            pass


# ─────────────────────────────────────────────
# 📤  Media yuborish (Instagram / Pinterest)
# ─────────────────────────────────────────────
async def send_media(update, status_msg, media_list, platform_name):
    if not media_list:
        await status_msg.edit_text("❌ Media yuklab olinmadi.")
        return

    sent_count = 0
    for media in media_list:
        filepath = media["path"]
        media_type = media["type"]

        try:
            file_size = os.path.getsize(filepath)
        except OSError:
            logger.error(f"Fayl topilmadi: {filepath}")
            continue

        if file_size > TG_FILE_LIMIT:
            await status_msg.edit_text(
                f"⚠️ Fayl juda katta ({file_size // (1024*1024)} MB)."
            )
            _safe_remove(filepath)
            continue

        try:
            with open(filepath, "rb") as f:
                if media_type == "video":
                    await update.message.reply_video(
                        video=f, supports_streaming=True,
                        read_timeout=120, write_timeout=120, connect_timeout=30,
                    )
                elif media_type == "photo":
                    await update.message.reply_photo(
                        photo=f,
                        read_timeout=60, write_timeout=60, connect_timeout=30,
                    )
            sent_count += 1
        except Exception as e:
            logger.error(f"Media yuborishda xatolik: {e}", exc_info=True)
        finally:
            _safe_remove(filepath)

    if sent_count > 0:
        await status_msg.edit_text(f"✅ Muvaffaqiyatli yuklandi! ({platform_name})")
    else:
        await status_msg.edit_text("❌ Faylni yuborishda xatolik yuz berdi.")


def _safe_remove(filepath: str):
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Fayl o'chirildi: {filepath}")
    except OSError as e:
        logger.warning(f"Faylni o'chirishda xatolik: {e}")


def _get_error_message(error: Exception, platform: str) -> str:
    error_str = str(error).lower()
    if "private" in error_str or "login" in error_str:
        return "🔒 Bu kontent shaxsiy akkauntga tegishli.\nFaqat ochiq profillardan yuklab olish mumkin."
    elif "not found" in error_str or "404" in error_str:
        return "❌ Kontent topilmadi.\nHavola to'g'ri ekanligini tekshiring."
    elif "age" in error_str:
        return "🔞 Bu kontent yosh cheklangan.\nBunday kontentni yuklab bo'lmaydi."
    elif "copyright" in error_str or "blocked" in error_str:
        return "⛔ Bu kontent mualliflik huquqi bilan himoyalangan\nyoki bloklangan."
    elif "stream" in error_str:
        return "❌ Video stream topilmadi.\nHavola to'g'ri ekanligini tekshiring."
    else:
        return (
            f"❌ Yuklab olishda xatolik yuz berdi.\n\n"
            f"📌 Platforma: {PLATFORM_EMOJI.get(platform, platform)}\n"
            f"💬 Sabab: {str(error)[:200]}\n\n"
            f"Iltimos, havolani tekshirib, qayta urinib ko'ring."
        )


# ─────────────────────────────────────────────
# 🚀  Botni ishga tushirish
# ─────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global xatolik handler — bot crash bo'lmasligi uchun."""
    logger.error(f"Kutilmagan xatolik: {context.error}", exc_info=context.error)


async def main():
    if BOT_TOKEN == "BOT_TOKENINGIZNI_SHU_YERGA_YOZING":
        print("❌ XATOLIK: Bot tokenini sozlang!")
        return

    # Ishga tushganda eski fayllarni tozalash
    _cleanup_old_files()

    print("🤖 Bot ishga tushmoqda...")
    print(f"📂 Yuklab olish papkasi: {DOWNLOAD_DIR}")
    print(f"🎬 FFmpeg: {FFMPEG_PATH}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_quality_callback, pattern=r"^yt\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    print("✅ Bot tayyor! Xabarlar kutilmoqda...")

    async with app:
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await app.start()
        try:
            while True:
                await asyncio.sleep(3600)
                _cleanup_old_files()  # Har soatda eski fayllarni tozalash
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot to'xtatildi.")
