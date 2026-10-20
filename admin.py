"""Owner-only admin panel: stats, users, channels, broadcast, maintenance."""
import asyncio
import time
import logging

from pyrogram import filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
)
from pyrogram.errors import FloodWait

import config
import utils

log = logging.getLogger("ytdl.admin")

PAGE_SIZE = 10


async def panel_kb(db):
    """Admin panel keyboard — module level so handlers can use it too."""
    maint = await db.get_setting("maintenance", "off")
    label = "✅ ON" if maint == "on" else "❌ OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics", callback_data="admin:stats"),
         InlineKeyboardButton(f"🛠️ Maintenance: {label}",
                              callback_data="admin:maint")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin:bc"),
         InlineKeyboardButton("👥 Users", callback_data="admin:users:0")],
        [InlineKeyboardButton("📢 Channels", callback_data="admin:channels")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:main")],
    ])


def register(app, db):
    def owner_only(q: CallbackQuery) -> bool:
        if not utils.is_owner(q.from_user.id):
            asyncio.ensure_future(q.answer("Owner only.", show_alert=True))
            return False
        return True

    def channels_kb(chs, fjoin="off"):
        rows = []
        for ch in chs:
            title = ch.get("title") or str(ch["_id"])
            rows.append([InlineKeyboardButton(
                f"🗑️ {title[:30]}", callback_data=f"ch:del:{ch['_id']}")])
        rows.append([InlineKeyboardButton(
            f"🔒 Force-Join: {'✅ ON' if fjoin == 'on' else '❌ OFF'}",
            callback_data="ch:toggle")])
        rows.append([InlineKeyboardButton("➕ Add Channel", callback_data="ch:add")])
        rows.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin:panel")])
        return InlineKeyboardMarkup(rows)

    async def channels_text():
        chs = await db.get_channels()
        fjoin = await db.get_setting("fjoin", "off")
        lines = [f"📢 <b>Force-Join Channels</b> — {'✅ ON' if fjoin == 'on' else '❌ OFF'}\n"]
        if chs:
            for ch in chs:
                lines.append(f"• {ch.get('title') or ch['_id']}")
        else:
            lines.append("<i>(none added)</i>")
        lines.append("\nBot must be <b>admin</b> in the channel.\n"
                     "Tap a channel to remove it.")
        return "\n".join(lines), channels_kb(chs, fjoin)

    # ---------------- admin panel ----------------
    @app.on_callback_query(filters.regex(r"^admin:panel$"))
    async def panel_cb(_, q: CallbackQuery):
        if not owner_only(q) or not q.message:
            return
        await q.message.edit_text("🛠️ <b>Admin Panel</b>\nChoose an option:",
                                  reply_markup=await panel_kb(db))
        await q.answer()

    @app.on_callback_query(filters.regex(r"^admin:"))
    async def admin_cb(_, q: CallbackQuery):
        if not owner_only(q) or not q.message:
            return
        parts = q.data.split(":")
        action = parts[1]
        uid = q.from_user.id

        if action == "stats":
            s = await db.stats()
            await q.message.edit_text(
                "📊 <b>Statistics</b>\n\n"
                f"👥 Users: <b>{s['users']}</b> (⛔ {s['blocked']} blocked)\n"
                f"⬇️ Downloads: <b>{s['downloads']}</b> (today: <b>{s['today']}</b>)\n"
                f"📢 Channels: <b>{s['channels']}</b>\n"
                f"🗄️ Mongo: {'✅ connected' if s['mongo'] else '❌ down'}\n"
                f"⚙️ Active downloads: <b>{len(utils.ACTIVE_TASKS)}</b> • "
                f"Queue: <b>{utils.WAITING}</b>\n"
                f"⏱️ Uptime: {utils.uptime()}",
                reply_markup=await panel_kb(db))
            await q.answer()

        elif action == "maint":
            cur = await db.get_setting("maintenance", "off")
            new = "off" if cur == "on" else "on"
            await db.set_setting("maintenance", new)
            await q.answer(f"Maintenance {new.upper()}")
            await q.message.edit_text("🛠️ <b>Admin Panel</b>\nChoose an option:",
                                      reply_markup=await panel_kb(db))

        elif action == "users":
            page = int(parts[2]) if len(parts) > 2 else 0
            await render_users(q, page)

        elif action == "bc":
            utils.UI_STATE[uid] = {"mode": "broadcast"}
            await q.message.edit_text(
                "📣 <b>Broadcast mode</b>\n\nSend the message to broadcast "
                "(any content type) — you'll get a preview first.\n"
                "Or /cancel to abort.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Admin Panel",
                                           callback_data="admin:panel")]]))
            await q.answer()

        elif action == "channels":
            txt, kb = await channels_text()
            await q.message.edit_text(txt, reply_markup=kb)
            await q.answer()

    async def render_users(q: CallbackQuery, page: int):
        rows, total = await db.recent_users(page=page, per_page=PAGE_SIZE)
        if not rows:
            await q.message.edit_text("👥 No users yet.", reply_markup=await panel_kb(db))
            await q.answer(); return
        kb = []
        for r in rows:
            name = f"@{r.get('username')}" if r.get("username") \
                else str(r.get("first_name") or r["_id"])
            icon = "⛔" if r.get("blocked") else "✅"
            dl = r.get("downloads", 0)
            kb.append([InlineKeyboardButton(
                f"{icon} {name[:20]} ({dl})",
                callback_data=f"user:{r['_id']}:{page}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"admin:users:{page - 1}"))
        nav.append(InlineKeyboardButton(
            f"📄 {page + 1}/{max(1, -(-total // PAGE_SIZE))}", callback_data="noop"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"admin:users:{page + 1}"))
        kb.append(nav)
        kb.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin:panel")])
        await q.message.edit_text(
            f"👥 <b>Users</b> — total <b>{total}</b>\nTap to block/unblock:",
            reply_markup=InlineKeyboardMarkup(kb))
        await q.answer()

    @app.on_callback_query(filters.regex(r"^user:\d+:\d+$"))
    async def user_cb(_, q: CallbackQuery):
        if not owner_only(q) or not q.message:
            return
        _, uids, page = q.data.split(":")
        uid2 = int(uids)
        cur = await db.is_blocked(uid2)
        await db.set_blocked(uid2, not cur)
        await q.answer(f"{'Unblocked' if cur else 'Blocked'} {uid2}")
        await render_users(q, int(page))

    # ---------------- channels ----------------
    @app.on_callback_query(filters.regex(r"^ch:"))
    async def ch_cb(_, q: CallbackQuery):
        if not owner_only(q) or not q.message:
            return
        parts = q.data.split(":")
        if parts[1] == "toggle":
            cur = await db.get_setting("fjoin", "off")
            await db.set_setting("fjoin", "off" if cur == "on" else "on")
            await q.answer(f"Force-join {'OFF' if cur == 'on' else 'ON'}")
        elif parts[1] == "del":
            await db.del_channel(int(parts[2]))
            await q.answer("Channel removed.")
        elif parts[1] == "add":
            utils.UI_STATE[q.from_user.id] = {"mode": "addchannel"}
            await q.message.edit_text(
                "➕ <b>Add Channel</b>\n\nSend the channel's chat ID "
                "(e.g. <code>-1001234567890</code>) or @username.\n"
                "The bot must be <b>admin</b> there first.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 Channels", callback_data="admin:channels")]]))
            await q.answer()
            return
        txt, kb = await channels_text()
        await q.message.edit_text(txt, reply_markup=kb)
        await q.answer()

    async def handle_admin_text(app, dbref, m: Message, state: dict) -> bool:
        """Called from handlers.text_router when owner has an active UI state."""
        uid = m.from_user.id if m.from_user else 0
        mode = state.get("mode")

        if mode == "addchannel":
            utils.UI_STATE.pop(uid, None)
            txt = (m.text or "").strip()
            if txt.lstrip("-").isdigit():
                chat_ref = int(txt)
            elif txt.startswith("@"):
                chat_ref = txt
            else:
                await m.reply_text("❌ Send a numeric chat ID or @username.",
                                   reply_markup=await panel_kb(db))
                return True
            try:
                chat = await app.get_chat(chat_ref)
                title = chat.title or str(chat_ref)
                invite = chat.invite_link or ""
                if not invite:
                    try:
                        invite = await app.export_chat_invite_link(chat.id)
                    except Exception:
                        pass
                await db.add_channel(chat.id, title, invite)
                await m.reply_text(f"✅ Added: <b>{utils.esc(title)}</b>",
                                   reply_markup=await panel_kb(db))
            except Exception as e:
                await m.reply_text(
                    f"❌ Cannot access: <code>{utils.esc(str(e)[:150])}</code>\n"
                    "Make the bot <b>admin</b> there first.",
                    reply_markup=await panel_kb(db))
            return True

        return False

    # ---------------- broadcast ----------------
    @app.on_message(filters.private, group=-1)
    async def broadcast_intercept(_, m: Message):
        uid = m.from_user.id if m.from_user else 0
        if not utils.is_owner(uid):
            return
        state = utils.UI_STATE.get(uid)
        if not state or state.get("mode") != "broadcast":
            return
        if m.text and m.text.startswith("/"):
            return
        utils.UI_STATE[uid] = {"mode": "broadcast_confirm",
                               "chat_id": m.chat.id, "msg_id": m.id}
        ids = await db.all_user_ids(blocked=False)
        await m.reply_text(
            f"📣 <b>Broadcast Preview</b>\n\n👥 Will send to <b>{len(ids)}</b> users.\n"
            "Confirm to start?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm & Send", callback_data="bc:confirm")],
                [InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel")],
            ]))
        m.stop_propagation()

    @app.on_callback_query(filters.regex(r"^bc:"))
    async def bc_cb(_, q: CallbackQuery):
        if not owner_only(q) or not q.message:
            return
        action = q.data.split(":")[1]
        uid = q.from_user.id
        state = utils.UI_STATE.get(uid)

        if action == "cancel":
            utils.UI_STATE.pop(uid, None)
            await q.message.edit_text("❌ Broadcast cancelled.",
                                      reply_markup=await panel_kb(db))
            await q.answer(); return

        if not state or state.get("mode") != "broadcast_confirm":
            await q.answer("No broadcast pending.", show_alert=True); return
        chat_id, msg_id = state["chat_id"], state["msg_id"]
        utils.UI_STATE.pop(uid, None)

        ids = await db.all_user_ids(blocked=False)
        utils.BCAST_STOP[uid] = False
        stop_btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛑 Stop", callback_data="bc:stop")]])
        await q.message.edit_text(
            f"📣 <b>Broadcasting…</b> 0/{len(ids)}", reply_markup=stop_btn)
        await q.answer()

        sent = failed = 0
        delay = 1.0 / config.MAX_BROADCAST_SPEED
        for i, uid2 in enumerate(ids, 1):
            if utils.BCAST_STOP.get(uid):
                break
            try:
                await app.copy_message(uid2, chat_id, msg_id)
                sent += 1
            except FloodWait as e:
                await asyncio.sleep(min(float(e.value), 30))
                try:
                    await app.copy_message(uid2, chat_id, msg_id)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            if i % 20 == 0:
                await utils.safe_edit(
                    q.message, f"📣 <b>Broadcasting…</b> {i}/{len(ids)} "
                               f"(✅ {sent} ❌ {failed})", stop_btn)
            await asyncio.sleep(delay)
        utils.BCAST_STOP.pop(uid, None)
        await q.message.edit_text(
            f"✅ <b>Broadcast done:</b> {sent}/{len(ids)} delivered"
            f"{f', {failed} failed' if failed else ''}"
            f"{' (stopped early)' if utils.BCAST_STOP.get(uid) else ''}.",
            reply_markup=await panel_kb(db))

    @app.on_callback_query(filters.regex(r"^bc:stop$"))
    async def bc_stop_cb(_, q: CallbackQuery):
        if not owner_only(q):
            return
        utils.BCAST_STOP[q.from_user.id] = True
        await q.answer("Stopping…")
