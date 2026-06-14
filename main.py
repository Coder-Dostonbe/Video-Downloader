import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
import instaloader
import yt_dlp
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    Filters,
    MessageHandler,
    Updater,
)

load_dotenv()
TOKEN = os.getenv("TOKEN")

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

L = instaloader.Instaloader(
    download_comments=False,
    download_video_thumbnails=False,
    save_metadata=False,
)

INSTA_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([^/?]+)")
YOUTUBE_RE = re.compile(r"(youtube\.com/(?:watch|shorts)|youtu\.be/)")
YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")

CHOICE_KB = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("🎥 Video", callback_data="video"),
        InlineKeyboardButton("🎵 Audio", callback_data="audio"),
    ]]
)


def find_files(folder: str, exts):
    """Papka ichidagi barcha fayllarni rekursiv qidiradi."""
    result = []
    for root, _, files in os.walk(folder):
        for f in sorted(files):
            if f.endswith(exts):
                result.append(os.path.join(root, f))
    return result


# ── handlers ──────────────────────────────────────────────────────────────────

def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Salom 👋\n\n"
        "Instagram yoki YouTube linkini yuboring — yuklab beraman 😉"
    )


def handle_message(update: Update, context: CallbackContext):
    text = update.message.text.strip()

    if INSTA_RE.search(text):
        m = INSTA_RE.search(text)
        context.user_data["url"] = text
        context.user_data["source"] = "instagram"
        context.user_data["shortcode"] = m.group(1)
        update.message.reply_text("Nima kerak?", reply_markup=CHOICE_KB)

    elif YOUTUBE_RE.search(text):
        context.user_data["url"] = text
        context.user_data["source"] = "youtube"
        update.message.reply_text("Nima kerak?", reply_markup=CHOICE_KB)

    else:
        update.message.reply_text("Instagram yoki YouTube link yuboring 🫢")


def handle_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    choice = query.data
    source = context.user_data.get("source")

    if not source:
        query.edit_message_text("Iltimos, qaytadan link yuboring.")
        return

    query.edit_message_text("Yuklanmoqda ⏳...")

    folder = tempfile.mkdtemp()
    try:
        if source == "instagram":
            _download_instagram(query, context, choice, folder)
        else:
            _download_youtube(query, context, choice, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ── Instagram ─────────────────────────────────────────────────────────────────

def _download_instagram(query, context: CallbackContext, choice: str, folder: str):
    shortcode = context.user_data["shortcode"]

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        L.download_post(post, target=Path(folder))
    except Exception as e:
        query.message.reply_text("Yuklab olishda xatolik yuz berdi 🫣")
        print("Instagram download error:", e)
        return

    mp4s = find_files(folder, ".mp4")
    imgs = find_files(folder, (".jpg", ".png"))

    if choice == "audio":
        if not mp4s:
            query.message.reply_text("Bu postda video yo'q, rasmlar yuborilmoqda 🖼")
            _send_images(query, imgs)
            return
        for mp4_path in mp4s:
            mp3_path = mp4_path.replace(".mp4", ".mp3")
            _extract_audio(mp4_path, mp3_path)
            with open(mp3_path, "rb") as f:
                query.message.reply_audio(audio=f)
    else:
        _send_images(query, imgs)
        for mp4_path in mp4s:
            with open(mp4_path, "rb") as f:
                query.message.reply_video(video=f)

    query.message.reply_text("Yuklab olindi 😍")


def _send_images(query, imgs: list):
    if not imgs:
        return
    if len(imgs) == 1:
        with open(imgs[0], "rb") as f:
            query.message.reply_photo(photo=f)
    else:
        media = [InputMediaPhoto(open(p, "rb")) for p in imgs]
        query.message.reply_media_group(media=media)
        for m in media:
            m.media.close()


# ── YouTube ───────────────────────────────────────────────────────────────────

def _download_youtube(query, context: CallbackContext, choice: str, folder: str):
    url = context.user_data["url"]

    common_opts = {
        "outtmpl": os.path.join(folder, "%(title)s.%(ext)s"),
        "extractor_args": {
            "youtube": {"player_client": ["tv_embedded", "android_vr"]}
        },
        "check_formats": True,
        "ffmpeg_location": FFMPEG_PATH,
    }

    if choice == "audio":
        ydl_opts = {
            **common_opts,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
    else:
        ydl_opts = {
            **common_opts,
            "format": (
                "bestvideo[ext=mp4][filesize<50M]+bestaudio[ext=m4a]"
                "/best[ext=mp4][filesize<50M]/best[filesize<50M]"
            ),
            "merge_output_format": "mp4",
        }

    if not _yt_download(ydl_opts, url):
        m = YOUTUBE_ID_RE.search(url)
        if m:
            embed_url = f"https://www.youtube.com/embed/{m.group(1)}"
            print(f"Retrying with embed URL: {embed_url}")
            if not _yt_download(ydl_opts, embed_url):
                query.message.reply_text("Yuklab olishda xatolik yuz berdi 🫣")
                return
        else:
            query.message.reply_text("Yuklab olishda xatolik yuz berdi 🫣")
            return

    if choice == "audio":
        files = find_files(folder, (".mp3", ".m4a", ".opus"))
        for fpath in files:
            with open(fpath, "rb") as f:
                query.message.reply_audio(audio=f)
    else:
        files = find_files(folder, (".mp4", ".webm", ".mkv"))
        for fpath in files:
            with open(fpath, "rb") as f:
                query.message.reply_video(video=f)

    if files:
        query.message.reply_text("Yuklab olindi 😍")
    else:
        query.message.reply_text("Fayl topilmadi yoki hajmi juda katta 😔")


# ── helpers ───────────────────────────────────────────────────────────────────

def _yt_download(ydl_opts: dict, url: str) -> bool:
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception as e:
        print(f"YouTube download error: {e}")
        return False


def _extract_audio(mp4_path: str, mp3_path: str):
    subprocess.run(
        [FFMPEG_PATH, "-i", mp4_path, "-q:a", "0", "-map", "a", mp3_path, "-y"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_handler(CallbackQueryHandler(handle_callback))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
