"""
Телеграм-бот-редактор постов для канала @mnogomorya.

Функции:
• Черновик: текст + медиа.
• Альбомы: 2–10 фото/видео в одном посте (media group).
• Публикация по кнопке.
• Таймер: /timer HH:MM | YYYY-MM-DD HH:MM | in 10m|2h|1d
• /when — посмотреть время публикации, /cancel_timer — отменить.

Совместим с Python 3.12–3.14 (есть фикс event loop).
Зависимости: python-telegram-bot==21.6

Для Railway:
- загрузи этот проект в GitHub;
- в Railway создай Worker из репозитория;
- добавь переменную окружения TELEGRAM_BOT_TOKEN;
- команда запуска: python app.py
"""

import os
import re
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("postbot")

# ---------- КОНФИГ ----------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TARGET_CHAT = "@mnogomorya"          # канал назначения
ADMIN_USER_ID = 211779388            # твой user_id (только ты управляешь)
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")  # таймзона для таймера

if not BOT_TOKEN:
    raise SystemExit(
        "\n[CONFIG] TELEGRAM_BOT_TOKEN не задан.\n"
        "Добавь переменную окружения TELEGRAM_BOT_TOKEN в Railway и перезапусти.\n"
    )

# ---------- МОДЕЛИ ----------
@dataclass
class Draft:
    text: str = ""
    # список медиа: ("photo"|"video"|"document"|"animation"|"audio"|"voice", file_id)
    media: List[Tuple[str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.text and not self.media

    def copy(self) -> "Draft":
        return Draft(text=self.text, media=list(self.media))


@dataclass
class ScheduledJob:
    when: datetime
    task: asyncio.Task


# хранилища в памяти процесса
DRAFTS: Dict[int, Draft] = {}
SCHEDULES: Dict[int, ScheduledJob] = {}

# ---------- УТИЛИТЫ ----------
def authorized(user_id: int) -> bool:
    return int(user_id) == int(ADMIN_USER_ID)


def get_draft(user_id: int) -> Draft:
    if user_id not in DRAFTS:
        DRAFTS[user_id] = Draft()
    return DRAFTS[user_id]


def summarize_draft(d: Draft) -> str:
    parts = []
    if d.text:
        parts.append(f"📝 <b>Текст</b>:\n{d.text}")
    if d.media:
        kinds = [k for (k, _) in d.media]
        parts.append("🖼 <b>Медиа</b>: " + ", ".join(kinds))
    if not parts:
        return "Черновик пуст. Пришли текст или фото/видео (можно несколько подряд для альбома)."
    return "\n\n".join(parts)


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 Предпросмотр", callback_data="prev")],
            [
                InlineKeyboardButton("📢 Опубликовать", callback_data="pub"),
                InlineKeyboardButton("🗑 Очистить", callback_data="clr"),
            ],
            [InlineKeyboardButton("⏰ Подсказка по таймеру: /timer", callback_data="noop")],
        ]
    )


def set_text_from(update: Update, draft: Draft) -> None:
    msg = update.effective_message
    if not msg:
        return
    if msg.text:
        entities = msg.entities or []
        cmd = next((e for e in entities if e.type == MessageEntity.BOT_COMMAND), None)
        draft.text = (msg.text[cmd.offset + cmd.length :] if cmd else msg.text).strip()
    elif msg.caption:
        entities = msg.caption_entities or []
        cmd = next((e for e in entities if e.type == MessageEntity.BOT_COMMAND), None)
        draft.text = (msg.caption[cmd.offset + cmd.length :] if cmd else msg.caption).strip()


def add_media_to_draft(draft: Draft, kind: str, file_id: str) -> None:
    """
    Фото/видео копятся для альбома (до 10).
    Остальные типы ведём как одиночки (берём последние, но не трогаем альбом фото/видео).
    """
    if kind in ("photo", "video"):
        draft.media.append((kind, file_id))
        draft.media = draft.media[-10:]  # лимит Телеграма
    else:
        draft.media.append((kind, file_id))
        seen = set()
        new_media: List[Tuple[str, str]] = []
        for k, fid in reversed(draft.media):
            if k in ("photo", "video"):
                new_media.append((k, fid))
            elif k not in seen:
                seen.add(k)
                new_media.append((k, fid))
        draft.media = list(reversed(new_media))


def draft_to_media_group(d: Draft) -> Optional[List]:
    """
    Если в черновике фото/видео >= 2 — вернуть список InputMedia для send_media_group.
    Подпись ставим только в первый элемент.
    """
    pv = [(k, fid) for (k, fid) in d.media if k in ("photo", "video")]
    if len(pv) < 2:
        return None
    items = []
    for idx, (k, fid) in enumerate(pv):
        caption = d.text if idx == 0 else None
        if k == "photo":
            items.append(
                InputMediaPhoto(
                    media=fid,
                    caption=caption,
                    parse_mode=ParseMode.HTML if caption else None,
                )
            )
        else:
            items.append(
                InputMediaVideo(
                    media=fid,
                    caption=caption,
                    parse_mode=ParseMode.HTML if caption else None,
                )
            )
    return items


async def send_preview(uid: int, d: Draft, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    group = draft_to_media_group(d)
    if group:
        await ctx.bot.send_media_group(chat_id=uid, media=group)
        return
    if d.media:
        kind, fid = d.media[-1]
        if kind == "photo":
            await ctx.bot.send_photo(uid, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "video":
            await ctx.bot.send_video(uid, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "document":
            await ctx.bot.send_document(uid, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "animation":
            await ctx.bot.send_animation(uid, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "audio":
            await ctx.bot.send_audio(uid, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "voice":
            await ctx.bot.send_voice(uid, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        else:
            await ctx.bot.send_message(uid, summarize_draft(d), parse_mode=ParseMode.HTML)
    else:
        await ctx.bot.send_message(uid, d.text or "Черновик пуст.", parse_mode=ParseMode.HTML)


async def publish_to_channel(d: Draft, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    group = draft_to_media_group(d)
    if group:
        await ctx.bot.send_media_group(chat_id=TARGET_CHAT, media=group)
        return
    if d.media:
        kind, fid = d.media[-1]
        if kind == "photo":
            await ctx.bot.send_photo(TARGET_CHAT, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "video":
            await ctx.bot.send_video(TARGET_CHAT, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "document":
            await ctx.bot.send_document(TARGET_CHAT, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "animation":
            await ctx.bot.send_animation(TARGET_CHAT, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "audio":
            await ctx.bot.send_audio(TARGET_CHAT, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        elif kind == "voice":
            await ctx.bot.send_voice(TARGET_CHAT, fid, caption=d.text or None, parse_mode=ParseMode.HTML)
        else:
            await ctx.bot.send_message(TARGET_CHAT, d.text or "", parse_mode=ParseMode.HTML)
    else:
        await ctx.bot.send_message(TARGET_CHAT, d.text or "", parse_mode=ParseMode.HTML)


# ---------- ПАРСИНГ ВРЕМЕНИ ----------
TIME_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
TIME_ABS = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})\s*$")
TIME_REL = re.compile(r"^\s*in\s+(\d+)\s*(m|min|h|hr|d)\s*$", re.IGNORECASE)


def parse_when(s: str, now: datetime) -> Optional[datetime]:
    # HH:MM
    m = TIME_HHMM.match(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now:
            dt = dt + timedelta(days=1)
        return dt

    # YYYY-MM-DD HH:MM
    m = TIME_ABS.match(s)
    if m:
        y, mo, d, hh, mm = map(int, m.groups())
        try:
            return datetime(y, mo, d, hh, mm, tzinfo=now.tzinfo)
        except ValueError:
            return None

    # in 10m / 2h / 1d
    m = TIME_REL.match(s)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        if unit in ("m", "min"):
            return now + timedelta(minutes=amount)
        if unit in ("h", "hr"):
            return now + timedelta(hours=amount)
        if unit == "d":
            return now + timedelta(days=amount)

    return None


# ---------- ХЕНДЛЕРЫ ----------
async def ensure_auth(update: Update) -> Optional[int]:
    u = update.effective_user
    if not u:
        return None
    if authorized(u.id):
        return u.id
    await update.effective_message.reply_text("⛔️ У тебя нет прав управлять этим ботом.")
    return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    await update.effective_message.reply_text(
        "Привет! Я бот-редактор постов для @mnogomorya.\n\n"
        "• Пришли текст — создам черновик.\n"
        "• Пришли фото/видео (несколько подряд) — соберу альбом (до 10).\n"
        "• Кнопки: «Предпросмотр», «Опубликовать», «Очистить».\n\n"
        "Таймер:\n"
        "• /timer HH:MM (сегодня; если время прошло — завтра)\n"
        "• /timer YYYY-MM-DD HH:MM\n"
        "• /timer in 10m | 2h | 1d\n"
        "• /when — узнать время\n"
        "• /cancel_timer — отмена",
        reply_markup=keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    await update.effective_message.reply_html(f"Твой user_id: <code>{uid}</code>")


async def cmd_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Форматы: /timer HH:MM | YYYY-MM-DD HH:MM | in 10m|2h|1d"
        )
        return

    when_str = " ".join(context.args)
    now = datetime.now(LOCAL_TZ)
    when = parse_when(when_str, now)
    if not when:
        await update.effective_message.reply_text(
            "Не понял время. Примеры: /timer 18:30  |  /timer 2025-10-14 09:00  |  /timer in 45m"
        )
        return

    draft = get_draft(uid).copy()
    if draft.is_empty():
        await update.effective_message.reply_text("Черновик пуст — нечего планировать.")
        return

    old = SCHEDULES.get(uid)
    if old and not old.task.done():
        old.task.cancel()

    async def job():
        try:
            delay = (when - datetime.now(LOCAL_TZ)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            await publish_to_channel(draft, context)
            await context.bot.send_message(
                uid,
                f"✅ Опубликовано по таймеру: {when.strftime('%Y-%m-%d %H:%M')}",
            )
            SCHEDULES.pop(uid, None)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Scheduled publish error")
            await context.bot.send_message(uid, f"Ошибка отложенной публикации: {e}")

    t = asyncio.create_task(job())
    SCHEDULES[uid] = ScheduledJob(when=when, task=t)
    await update.effective_message.reply_text(
        f"⏰ Запланировал на {when.strftime('%Y-%m-%d %H:%M %Z')}"
    )


async def cmd_cancel_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    sched = SCHEDULES.pop(uid, None)
    if sched and not sched.task.done():
        sched.task.cancel()
        await update.effective_message.reply_text("❌ Таймер отменён.")
    else:
        await update.effective_message.reply_text("Нет активного таймера.")


async def cmd_when(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    sched = SCHEDULES.get(uid)
    if not sched:
        await update.effective_message.reply_text("Нет запланированной публикации.")
    else:
        await update.effective_message.reply_text(
            f"⏰ Запланировано на {sched.when.strftime('%Y-%m-%d %H:%M %Z')}"
        )


# ---- текст и медиа ----
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    d = get_draft(uid)
    set_text_from(update, d)
    await update.effective_message.reply_html(summarize_draft(d), reply_markup=keyboard())


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    d = get_draft(uid)
    file_id = update.effective_message.photo[-1].file_id
    add_media_to_draft(d, "photo", file_id)
    set_text_from(update, d)
    await update.effective_message.reply_html(summarize_draft(d), reply_markup=keyboard())


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    d = get_draft(uid)
    add_media_to_draft(d, "video", update.effective_message.video.file_id)
    set_text_from(update, d)
    await update.effective_message.reply_html(summarize_draft(d), reply_markup=keyboard())


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    d = get_draft(uid)
    add_media_to_draft(d, "document", update.effective_message.document.file_id)
    set_text_from(update, d)
    await update.effective_message.reply_html(summarize_draft(d), reply_markup=keyboard())


async def on_animation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    d = get_draft(uid)
    add_media_to_draft(d, "animation", update.effective_message.animation.file_id)
    set_text_from(update, d)
    await update.effective_message.reply_html(summarize_draft(d), reply_markup=keyboard())


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    d = get_draft(uid)
    add_media_to_draft(d, "audio", update.effective_message.audio.file_id)
    set_text_from(update, d)
    await update.effective_message.reply_html(summarize_draft(d), reply_markup=keyboard())


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = await ensure_auth(update)
    if uid is None:
        return
    d = get_draft(uid)
    add_media_to_draft(d, "voice", update.effective_message.voice.file_id)
    set_text_from(update, d)
    await update.effective_message.reply_html(summarize_draft(d), reply_markup=keyboard())


# ---- кнопки ----
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not authorized(uid):
        await q.edit_message_text("⛔️ У тебя нет прав управлять этим ботом.")
        return
    d = get_draft(uid)

    if q.data == "noop":
        await q.answer("Используй: /timer, /when, /cancel_timer", show_alert=False)
        return

    if q.data == "prev":
        if d.is_empty():
            await q.edit_message_text("Черновик пуст. Пришли текст или медиа.", reply_markup=keyboard())
            return
        await send_preview(uid, d, context)
        return

    if q.data == "pub":
        if d.is_empty():
            await q.edit_message_text("Нечего публиковать.", reply_markup=keyboard())
            return
        try:
            await publish_to_channel(d, context)
            await q.edit_message_text("✅ Опубликовано в @mnogomorya")
            DRAFTS[uid] = Draft()
            sched = SCHEDULES.pop(uid, None)
            if sched and not sched.task.done():
                sched.task.cancel()
        except Exception as e:
            logger.exception("Publish error")
            await q.edit_message_text(f"Ошибка публикации: {e}", reply_markup=keyboard())
        return

    if q.data == "clr":
        DRAFTS[uid] = Draft()
        await q.edit_message_text("🧹 Черновик очищен.")
        sched = SCHEDULES.pop(uid, None)
        if sched and not sched.task.done():
            sched.task.cancel()
        return


# ---------- СТАРТ ----------
async def on_startup(app):
    me = await app.bot.get_me()
    logger.info("Bot started as @%s", me.username)
    logger.info("Target channel: %s", TARGET_CHAT)
    logger.info("Admin user_id: %s", ADMIN_USER_ID)


def main() -> None:
    # Фикс для Python 3.14: создать event loop в главном потоке при необходимости
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("timer", cmd_timer))
    app.add_handler(CommandHandler("cancel_timer", cmd_cancel_timer))
    app.add_handler(CommandHandler("when", cmd_when))

    # Медиа и текст
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, on_photo))
    app.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, on_video))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, on_document))
    app.add_handler(MessageHandler(filters.ANIMATION & ~filters.COMMAND, on_animation))
    app.add_handler(MessageHandler(filters.AUDIO & ~filters.COMMAND, on_audio))
    app.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Кнопки
    app.add_handler(CallbackQueryHandler(on_cb))

    app.run_polling()


if __name__ == "__main__":
    main()
