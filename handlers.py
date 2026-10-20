"""User-facing handlers: links, search, quality menu, download pipeline."""
import os
import time
import uuid
import asyncio
import logging

from pyrogram import filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
)
from pyrogram.errors import UserNotParticipant, ChannelPrivate, FloodWait

import config
import utils
import engine

log = logging.getLogger("ytdl.handlers")

QUALITIES = [
    {"label": "📱 360p",  "h": 360,  "fmt": "bv*[height<=360]+ba/b[height<=360]/b"},
    {"label": "💻 720p",  "h": 720,  "fmt": "bv*[height<=720]+ba/b[height<=720]/b"},
    {"label": "🖥️ 1080p", "h": 1080, "fmt": "bv*[height<=1080]+ba/b[height<=1080]/b"},
    {"label": "🎞️ Best",  "h": None, "fmt": "bv*+ba/b"},
    {"label": "🎵 MP3",   "audio": True, "fmt": "bestaudio/b"},
]

WELCOME = (
    "👋 <b>Video Downloader Bot</b>\n\n"
    "Send me a <b>video link</b> — or type a <b>song/movie name</b> to search.\n\n"
    "✅ YouTube + 1000+ sites (yt-dlp engine)\n"
    "✅ Up to 1080p + MP3\n"
    "✅ Large files via MTProto (no 50MB limit)\n"
    "✅ Live search with 🔍\n\n"
    "Just paste a link to get started! 🚀"
)
HELP_TEXT = (
    "📖 <b>How to use</b>\n\n"
    "1️⃣ Send a video URL — or type a name to 🔍 search\n"
    "2️⃣ Pick a quality (sizes shown where known)\n"
    "3️⃣ Watch live progress — download & upload\n\n"
    "💡 <b>Tips</b>\n"
    "• /cancel — stop a download\n"
    "• /ping — bot status\n"
    f"• Max file size: {config.MAX_FILE_GB} GB (Telegram limit)\n"
    f"• Cooldown: {config.COOLDOWN_SECONDS}s between requests"
)

sem = asyncio.Semaphore(config.MAX_CONCURRENT)


def register(app, db):
    # ---------------- gates ----------------
    async def base_gate(m: Message) -> bool:
        """register + blocked + maintenance. False = stop processing."""
        uid = m.from_user.id if m.from_user else 0
        if not uid:
            return False
        await db.upsert_user(uid, m.from_user.username, m.from_user.first_name)
        if await db.is_blocked(uid):
            try:
                await m.reply_text("⛔ <b>You are blocked from using this bot.</b>")
            except Exception:
                pass
            return False
        if await db.get_setting("maintenance", "off") == "on" and not utils.is_owner(uid):
            try:
                await m.reply_text("🛠️ <b>Bot is under maintenance.</b> Please try later.")
            except Exception:
                pass
            return False
        return True

    async def force_join_ok(uid: int):
        """Returns (ok, missing_channels). Channels the bot cannot verify are skipped."""
        if await db.get_setting("fjoin", "off") != "on":
            return True, []
        missing = []
        for ch in await db.get_channels():
            try:
                await app.get_chat_member(ch["_id"], uid)
            except UserNotParticipant:
                missing.append(ch)
            except (ChannelPrivate, Exception):
                # bot can't verify membership — never punish the user for it
                continue
        return (len(missing) == 0), missing

    def join_kb(missing):
        rows = []
        for ch in missing:
            label = ch.get("title") or str(ch["_id"])
            if ch.get("invite_link"):
                rows.append([InlineKeyboardButton(f"📢 Join {label}",
                                                  url=ch["invite_link"])])
        rows.append([InlineKeyboardButton("✅ I've Joined — Verify",
                                          callback_data="recheck")])
        return InlineKeyboardMarkup(rows)

    async def join_gate(uid: int) -> bool:
        ok, missing = await force_join_ok(uid)
        return ok  # callers that need buttons build them from force_join_ok

    # ---------------- shared: error hints ----------------
    def err_hint(msg: str) -> str:
        ml = (msg or "").lower()
        if "403" in ml or "forbidden" in ml:
            return "\n\n💡 YouTube blocked this one — try again in a bit."
        if "sign in" in ml or "age" in ml and "restrict" in ml:
            return "\n\n💡 Age-restricted video needs cookies (owner: set YOUTUBE_COOKIES)."
        if "larger" in ml or "max_filesize" in ml or "too big" in ml:
            return f"\n\n💡 File exceeds the {config.MAX_FILE_GB} GB limit — pick a lower quality."
        if "geo" in ml or "country" in ml:
            return "\n\n💡 Not available in the server's region."
        if "unsupported url" in ml:
            return "\n\n💡 This site isn't supported. Try a YouTube/IG/TikTok link."
        return ""

    # ---------------- main menus ----------------
    def main_menu_kb(uid: int):
        rows = [
            [InlineKeyboardButton("❓ Help", callback_data="menu:help"),
             InlineKeyboardButton("🔍 Search tip", callback_data="menu:tip")],
        ]
        if utils.is_owner(uid):
            rows.append([InlineKeyboardButton("🛠️ Admin Panel",
                                              callback_data="menu:admin")])
        return InlineKeyboardMarkup(rows)

    # ---------------- commands ----------------
    @app.on_message(filters.private & filters.command("start"))
    async def start_cmd(_, m: Message):
        if not await base_gate(m):
            return
        uid = m.from_user.id
        ok, missing = await force_join_ok(uid)
        if not ok:
            await m.reply_text("🔒 <b>Join our channel(s) to continue:</b>",
                               reply_markup=join_kb(missing))
            return
        await m.reply_text(WELCOME, reply_markup=main_menu_kb(uid))

    @app.on_message(filters.private & filters.command("help"))
    async def help_cmd(_, m: Message):
        await m.reply_text(HELP_TEXT, reply_markup=main_menu_kb(
            m.from_user.id if m.from_user else 0))

    @app.on_message(filters.private & filters.command("cancel"))
    async def cancel_cmd(_, m: Message):
        uid = m.from_user.id if m.from_user else 0
        if uid in utils.UI_STATE:
            utils.UI_STATE.pop(uid, None)
            await m.reply_text("✖️ Cancelled.", reply_markup=main_menu_kb(uid))
            return
        key = utils.USER_ACTIVE.get(uid)
        if key:
            utils.CANCEL_FLAGS[key] = True
            t = utils.ACTIVE_TASKS.get(key)
            if t and not t.done():
                t.cancel()
            await m.reply_text("🛑 Cancelling your download…")
            return
        await m.reply_text("ℹ️ Nothing to cancel.")

    @app.on_message(filters.private & filters.command("ping"))
    async def ping_cmd(_, m: Message):
        t0 = time.time()
        msg = await m.reply_text("🏓 <i>ping…</i>")
        await msg.edit_text(
            f"🏓 <b>Pong!</b> {int((time.time() - t0) * 1000)} ms\n"
            f"⏱️ Uptime: {utils.uptime()}\n"
            f"🗄️ Mongo: {'✅' if db.ok else '❌'}\n"
            f"⚙️ Active: {len(utils.ACTIVE_TASKS)} • Queue: {utils.WAITING}")

    # ---------------- menu callbacks ----------------
    @app.on_callback_query(filters.regex(r"^menu:"))
    async def menu_cb(_, q: CallbackQuery):
        if not q.message:
            await q.answer(); return
        action = q.data.split(":", 1)[1]
        uid = q.from_user.id
        if action == "main":
            await q.message.edit_text(WELCOME, reply_markup=main_menu_kb(uid))
        elif action == "help":
            await q.message.edit_text(HELP_TEXT, reply_markup=main_menu_kb(uid))
        elif action == "tip":
            await q.message.edit_text(
                "🔍 <b>Search</b>\n\nType any song/movie/topic name — "
                "I'll show the top 5 YouTube results as buttons.\n\n"
                "Example: <code>kesariya song</code>",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Main", callback_data="menu:main")]]))
        elif action == "admin":
            if not utils.is_owner(uid):
                await q.answer("Owner only.", show_alert=True); return
            import admin
            await q.message.edit_text(
                "🛠️ <b>Admin Panel</b>\nChoose an option:",
                reply_markup=await admin.panel_kb(db))
        await q.answer()

    @app.on_callback_query(filters.regex(r"^recheck$"))
    async def recheck_cb(_, q: CallbackQuery):
        ok, missing = await force_join_ok(q.from_user.id)
        if ok:
            if q.message:
                await q.message.edit_text(WELCOME,
                                          reply_markup=main_menu_kb(q.from_user.id))
            await q.answer("✅ Verified!")
        else:
            await q.answer("Please join all channel(s) first.", show_alert=True)

    # ---------------- quality menu ----------------
    async def present_quality_menu(src: Message, url: str):
        uid = src.from_user.id if src.from_user else 0
        status = await src.reply_text("🔍 <b>Fetching video info…</b>")

        def _info():
            return engine.extract_info(url)
        try:
            info, client = await asyncio.to_thread(_info)
        except Exception as e:
            log.warning("extract failed for %s: %.200s", url, e)
            await utils.safe_edit(status,
                                  f"❌ <b>Could not fetch this video.</b>\n"
                                  f"<code>{utils.esc(str(e)[:250])}</code>"
                                  f"{err_hint(str(e))}")
            return
        if info.get("is_live"):
            await utils.safe_edit(status, "🔴 <b>Live streams are not supported.</b>")
            return
        if info.get("extractor_key", "").lower() == "youtube" and \
                (info.get("_type") == "playlist" and not info.get("webpage_url")):
            await utils.safe_edit(status,
                                  "📋 <b>Playlists are not supported</b> — send a single video link.")
            return

        title = info.get("title") or "Video"
        uploader = info.get("uploader") or info.get("channel") or "—"
        duration = info.get("duration")
        up_date = info.get("upload_date") or ""
        date_s = f"{up_date[6:8]}/{up_date[4:6]}/{up_date[:4]}" \
            if len(up_date) == 8 else ""
        platform = engine.platform_of(info.get("webpage_url") or url)

        key = f"c{src.chat.id}m{src.id}"
        ests = [engine.estimate_size(info, qo) for qo in QUALITIES]
        utils.pending_put(key, {
            "type": "url", "url": info.get("webpage_url") or url,
            "title": title, "thumb": info.get("thumbnail"),
            "duration": duration, "client": client, "ests": ests,
        })

        rows = []
        for i, qo in enumerate(QUALITIES):
            est = ests[i]
            too_big = est and est > config.MAX_FILE_GB * (1024 ** 3)
            label = qo["label"] + (f" · ≈{utils.fmt_size(est)}" if est else "")
            if too_big:
                label = "⚠️ " + label + " (too big)"
            rows.append([InlineKeyboardButton(label, callback_data=f"q|{key}|{i}")])
        view_s = (f" • 👁️ {utils.fmt_views(info.get('view_count'))}"
                  if info.get("view_count") else "")
        date_full = f" • 📅 {date_s}" if date_s else ""
        caption = (
            f"🎬 <b>{utils.esc(title)}</b>\n\n"
            f"👤 {utils.esc(uploader)}\n"
            f"⏱️ {utils.fmt_dur(duration)}{view_s}{date_full}\n"
            f"🌐 {platform}\n\n<b>Select quality:</b>"
        )
        markup = InlineKeyboardMarkup(rows)
        thumb = utils.pending_get(key).get("thumb")
        try:
            if thumb:
                await status.delete()
                await src.reply_photo(thumb, caption=caption, reply_markup=markup)
            else:
                await status.edit_text(caption, reply_markup=markup)
        except Exception:
            try:
                await utils.safe_edit(status, caption, markup)
            except Exception:
                pass

    # ---------------- URL handler ----------------
    @app.on_message(filters.private & filters.regex(utils.URL_RE), group=0)
    async def url_handler(_, m: Message):
        text = m.text or m.caption or ""
        mt = utils.URL_RE.search(text)
        if not mt:
            return
        if not await base_gate(m):
            return
        uid = m.from_user.id
        cd = utils.on_cooldown(uid)
        if cd:
            await m.reply_text(f"⏳ <b>Slow down!</b> Try again in {cd}s.")
            return
        ok, missing = await force_join_ok(uid)
        if not ok:
            await m.reply_text("🔒 <b>Join our channel(s) to continue:</b>",
                               reply_markup=join_kb(missing))
            return
        utils.mark_request(uid)
        await present_quality_menu(m, mt.group(0).rstrip(").>,]"))
        m.stop_propagation()

    # ---------------- search ----------------
    async def do_search(m: Message, query: str):
        status = await m.reply_text("🔍 <i>Searching YouTube…</i>")
        qtext = query.strip()

        def _s():
            info, _c = engine.extract_info(f"ytsearch5:{qtext}")
            return info
        try:
            info = await asyncio.to_thread(_s)
        except Exception as e:
            await utils.safe_edit(status,
                                  f"❌ <b>Search failed.</b> <code>{utils.esc(str(e)[:200])}</code>")
            return
        entries = [e for e in (info.get("entries") or [])
                   if e and (e.get("id") or e.get("url"))][:5]
        if not entries:
            await utils.safe_edit(status,
                                  f"😶 <b>No results</b> for <i>{utils.esc(qtext)}</i>")
            return
        key = "s" + uuid.uuid4().hex[:8]
        results = []
        for e in entries:
            results.append({
                "url": e.get("webpage_url") or e.get("url")
                       or f"https://youtu.be/{e.get('id')}",
                "title": e.get("title") or "Untitled",
                "duration": e.get("duration"),
            })
        utils.pending_put(key, {"type": "search", "results": results})
        rows = []
        for i, r in enumerate(results):
            label = f"🎬 {r['title'][:40]} · {utils.fmt_dur(r['duration'])}"
            rows.append([InlineKeyboardButton(label, callback_data=f"s|{key}|{i}")])
        await utils.safe_edit(
            status,
            f"🔎 <b>Results for:</b> <i>{utils.esc(qtext)}</i>\n\nPick one:",
            InlineKeyboardMarkup(rows))

    @app.on_callback_query(filters.regex(r"^s\|"))
    async def search_pick_cb(_, q: CallbackQuery):
        if not q.message:
            await q.answer(); return
        try:
            _, key, idx = q.data.split("|")
            idx = int(idx)
        except ValueError:
            await q.answer("Invalid.", show_alert=True); return
        data = utils.pending_get(key)
        if not data or data.get("type") != "search":
            await q.answer("Session expired — search again.", show_alert=True)
            return
        try:
            url = data["results"][idx]["url"]
        except (IndexError, KeyError):
            await q.answer("Invalid result.", show_alert=True); return
        uid = q.from_user.id
        if await db.is_blocked(uid) or (
                await db.get_setting("maintenance", "off") == "on"
                and not utils.is_owner(uid)):
            await q.answer("Not allowed right now.", show_alert=True); return
        cd = utils.on_cooldown(uid)
        if cd:
            await q.answer(f"Wait {cd}s.", show_alert=True); return
        ok, missing = await force_join_ok(uid)
        if not ok:
            await q.answer("Join channel(s) first.", show_alert=True); return
        utils.mark_request(uid)
        await q.answer("🔍 Fetching…")
        await present_quality_menu(q.message, url)

    # ---------------- download pipeline ----------------
    def upload_progress_cb(st, status_msg, title, label, markup):
        """Sync callback used by pyrogram uploads (runs in the event loop)."""
        def _hook(cur, tot):
            now = time.time()
            if now - st.get("last", 0) < 4:
                return
            st["last"] = now
            frac = (cur / tot) if tot else 0
            txt = (f"⬆️ <b>Uploading</b>\n📌 {utils.esc(title)}\n🎚️ {label}\n\n"
                   f"{utils.bar(frac)} {int(frac * 100)}%\n"
                   f"📦 {utils.fmt_size(cur)} / {utils.fmt_size(tot)}")
            asyncio.ensure_future(utils.safe_edit(status_msg, txt, markup))
        return _hook

    async def run_download(status_msg, uid, key, data, qo):
        """The whole download+upload pipeline, guarded by the queue."""
        url, title = data["url"], data["title"]
        client = data.get("client") or ""
        label = qo["label"]
        task_dir = engine.new_task_dir()
        cancel_btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛑 Cancel", callback_data=f"x|{key}")]])
        utils.CANCEL_FLAGS[key] = False
        utils.USER_ACTIVE[uid] = key

        def cancelled():
            return utils.CANCEL_FLAGS.get(key, False)

        loop = asyncio.get_running_loop()
        dlast = {"t": 0.0}

        def dprogress(frac, speed, eta):
            now = time.time()
            if now - dlast["t"] < 4:
                return
            dlast["t"] = now
            txt = (f"⬇️ <b>Downloading</b>\n📌 {utils.esc(title)}\n"
                   f"🎚️ {label}\n\n{utils.bar(frac)} {int(frac * 100)}%\n"
                   f"⚡ {utils.fmt_speed(speed)} • ⏳ ETA {int(eta) if eta else '—'}s")
            asyncio.run_coroutine_threadsafe(
                utils.safe_edit(status_msg, txt, cancel_btn), loop)

        acquired = False
        try:
            if sem.locked():
                utils.WAITING += 1
                await utils.safe_edit(
                    status_msg,
                    f"⏳ <b>Queued</b> — position {utils.WAITING}.\n"
                    f"📌 {utils.esc(title)}", cancel_btn)
            await sem.acquire()
            acquired = True
            utils.WAITING = max(0, utils.WAITING - 1)

            await utils.safe_edit(
                status_msg,
                f"⬇️ <b>Downloading</b>\n📌 {utils.esc(title)}\n"
                f"🎚️ {label}\n\n<i>Starting…</i>", cancel_btn)
            try:
                path, real_title, info = await asyncio.to_thread(
                    engine.download_media, url, qo["fmt"], client,
                    task_dir, dprogress, cancelled)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("download failed: %.200s", e)
                await utils.safe_edit(
                    status_msg,
                    f"❌ <b>Download failed.</b>\n"
                    f"<code>{utils.esc(str(e)[:250])}</code>{err_hint(str(e))}")
                return

            size = os.path.getsize(path) if os.path.isfile(path) else 0
            if size < 1024:
                await utils.safe_edit(status_msg,
                                      "❌ <b>Download failed.</b> File is empty — try another quality.")
                return
            if size > config.MAX_FILE_GB * (1024 ** 3):
                await utils.safe_edit(
                    status_msg,
                    f"❌ <b>Too large:</b> {utils.fmt_size(size)} > {config.MAX_FILE_GB} GB limit.\n"
                    "💡 Pick a lower quality or MP3.")
                return

            thumb = ""
            if not qo.get("audio"):
                thumb = engine.make_thumbnail(
                    path, os.path.join(task_dir, "thumb.jpg"))

            # honest note if we could not deliver the requested quality
            got_h = info.get("height") or 0
            note = ""
            if not qo.get("audio") and qo.get("h") and got_h \
                    and got_h + 60 < qo["h"]:
                note = (f"\n⚠️ {qo['label']} available nahi thi — "
                        f"best {got_h}p delivered.")

            up_state = {"last": 0.0}
            up_cb = upload_progress_cb(up_state, status_msg, real_title,
                                       label, cancel_btn)
            cap = (f"🎬 <b>{utils.esc(real_title)}</b>\n"
                   f"🎚️ {label} • 📦 {utils.fmt_size(size)}{note}\n"
                   f"@{((await app.get_me()).username or '')}")
            chat_id = status_msg.chat.id
            duration = int(info.get("duration") or 0) or None
            try:
                if qo.get("audio"):
                    await app.send_audio(
                        chat_id, path, caption=cap,
                        title=real_title[:60],
                        performer=(info.get("uploader") or info.get("channel") or None),
                        duration=duration, thumb=thumb or None,
                        progress=up_cb)
                else:
                    try:
                        await app.send_video(
                            chat_id, path, caption=cap,
                            duration=duration,
                            width=info.get("width") or None,
                            height=info.get("height") or None,
                            thumb=thumb or None, supports_streaming=True,
                            progress=up_cb)
                    except Exception:
                        log.warning("send_video failed; sending as document")
                        await app.send_document(chat_id, path, caption=cap,
                                                thumb=thumb or None,
                                                progress=up_cb)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("upload failed")
                await utils.safe_edit(
                    status_msg,
                    f"❌ <b>Upload failed.</b>\n<code>{utils.esc(str(e)[:250])}</code>")
                return

            await db.bump_download(uid, real_title, label, size, url,
                                   engine.platform_of(url))
            await utils.safe_edit(status_msg, "✅ <b>Done!</b> Enjoy 🎉")
        except asyncio.CancelledError:
            await utils.safe_edit(status_msg, "🛑 <b>Download cancelled.</b>")
        except Exception:
            log.exception("pipeline error")
            await utils.safe_edit(status_msg, "❌ <b>Unexpected error.</b> Try again.")
        finally:
            acquired and sem.release()
            utils.ACTIVE_TASKS.pop(key, None)
            utils.USER_ACTIVE.pop(uid, None)
            utils.CANCEL_FLAGS.pop(key, None)
            utils.pending_pop(key)
            engine.cleanup_task_dir(task_dir)

    @app.on_callback_query(filters.regex(r"^q\|"))
    async def quality_cb(_, q: CallbackQuery):
        if not q.message:
            await q.answer(); return
        try:
            _, key, idx = q.data.split("|")
            idx = int(idx)
        except ValueError:
            await q.answer("Invalid.", show_alert=True); return
        uid = q.from_user.id
        if utils.is_owner(uid) is False and await db.is_blocked(uid):
            await q.answer("⛔ You are blocked.", show_alert=True); return
        if await db.get_setting("maintenance", "off") == "on" \
                and not utils.is_owner(uid):
            await q.answer("🛠️ Under maintenance.", show_alert=True); return
        data = utils.pending_get(key)
        if not data or data.get("type") != "url":
            await q.answer("Session expired — send the link again.", show_alert=True)
            return
        if uid in utils.USER_ACTIVE:
            await q.answer("You already have a download running — wait for it "
                           "or /cancel it.", show_alert=True)
            return
        cd = utils.on_cooldown(uid)
        if cd:
            await q.answer(f"Wait {cd}s.", show_alert=True); return
        if idx >= len(QUALITIES):
            await q.answer("Invalid quality.", show_alert=True); return
        ests = data.get("ests") or []
        est = ests[idx] if idx < len(ests) else 0
        if est and est > config.MAX_FILE_GB * (1024 ** 3):
            await q.answer(
                f"This quality is ≈{utils.fmt_size(est)} — above the "
                f"{config.MAX_FILE_GB} GB Telegram limit. Pick a lower one.",
                show_alert=True)
            return
        ok, _missing = await force_join_ok(uid)
        if not ok:
            await q.answer("Join channel(s) first.", show_alert=True); return
        utils.mark_request(uid)
        await q.answer("🚀 Starting…")
        task = asyncio.create_task(run_download(
            q.message, uid, key, data, QUALITIES[idx]))
        utils.ACTIVE_TASKS[key] = task

    @app.on_callback_query(filters.regex(r"^x\|"))
    async def cancel_cb(_, q: CallbackQuery):
        _, key = q.data.split("|", 1)
        utils.CANCEL_FLAGS[key] = True
        t = utils.ACTIVE_TASKS.get(key)
        if t and not t.done():
            t.cancel()
            await q.answer("🛑 Cancelling…")
        else:
            await q.answer("Nothing to cancel.", show_alert=True)

    # ---------------- text router (group 1) ----------------
    @app.on_message(filters.private & filters.text, group=1)
    async def text_router(_, m: Message):
        text = (m.text or "").strip()
        if not text or text.startswith("/"):
            return
        if utils.URL_RE.search(text):
            return  # handled by url_handler in group 0
        uid = m.from_user.id if m.from_user else 0
        state = utils.UI_STATE.get(uid)
        if state and utils.is_owner(uid):
            import admin
            if await admin.handle_admin_text(app, db, m, state):
                return
        if not await base_gate(m):
            return
        cd = utils.on_cooldown(uid)
        if cd:
            await m.reply_text(f"⏳ Wait {cd}s before searching again.")
            return
        ok, missing = await force_join_ok(uid)
        if not ok:
            await m.reply_text("🔒 <b>Join our channel(s) to continue:</b>",
                               reply_markup=join_kb(missing))
            return
        if len(text) < 2:
            await m.reply_text("🤔 Send a video link or type something to search.")
            return
        utils.mark_request(uid)
        await do_search(m, text)
