"""MongoDB persistence layer (motor / async).

Collections:
  users        {_id, username, first_name, joined_at, last_seen, downloads, blocked}
  channels     {_id: chat_id, title, invite_link, added_at}
  settings     {_id: key, value}
  stats_daily  {_id: "YYYY-MM-DD", downloads}
  downloads_log{user_id, title, quality, size, url, platform, at}

Every method degrades gracefully: if MongoDB is unreachable the bot keeps
working (without persistence) instead of crashing.
"""
import time
import logging
from datetime import datetime, timezone

import config
import utils

log = logging.getLogger("ytdl.db")

try:
    from motor.motor_asyncio import AsyncIOMotorClient
    import certifi
    _MOTOR_OK = True
except Exception as e:  # pragma: no cover
    log.error("motor/certifi missing: %s", e)
    _MOTOR_OK = False


class Database:
    def __init__(self, uri: str, name: str):
        self.ok = False
        self._client = None
        self.db = None
        if uri and _MOTOR_OK:
            try:
                self._client = AsyncIOMotorClient(
                    uri, serverSelectionTimeoutMS=8000,
                    tlsCAFile=certifi.where(),
                )
                self.db = self._client[name]
            except Exception as e:
                log.error("Mongo init failed: %s", e)
                self._client = None

    async def ping(self) -> bool:
        if self.db is None:
            self.ok = False
            return False
        try:
            await self.db.command("ping")
            self.ok = True
        except Exception as e:
            log.error("Mongo ping failed: %s", e)
            self.ok = False
        return self.ok

    # ---------- internal ----------
    def _col(self, name):
        if self.db is None:
            return None
        return self.db[name]

    # ---------- users ----------
    async def upsert_user(self, uid: int, username: str = None,
                          first_name: str = None) -> None:
        col = self._col("users")
        if col is None:
            return
        try:
            await col.update_one(
                {"_id": uid},
                {"$set": {"username": username or "", "last_seen": datetime.now(timezone.utc)},
                 "$setOnInsert": {"first_name": first_name or "",
                                  "joined_at": datetime.now(timezone.utc),
                                  "downloads": 0, "blocked": False}},
                upsert=True,
            )
        except Exception:
            log.exception("upsert_user")

    async def is_blocked(self, uid: int) -> bool:
        col = self._col("users")
        if col is None:
            return False
        try:
            doc = await col.find_one({"_id": uid}, {"blocked": 1})
            return bool(doc and doc.get("blocked"))
        except Exception:
            return False

    async def set_blocked(self, uid: int, flag: bool) -> None:
        col = self._col("users")
        if col is None:
            return
        try:
            await col.update_one({"_id": uid}, {"$set": {"blocked": flag}}, upsert=True)
        except Exception:
            log.exception("set_blocked")

    async def bump_download(self, uid: int, title: str, quality: str,
                            size: int, url: str, platform: str) -> None:
        today = time.strftime("%Y-%m-%d")
        try:
            col = self._col("users")
            if col is not None:
                await col.update_one({"_id": uid}, {"$inc": {"downloads": 1}})
            sd = self._col("stats_daily")
            if sd is not None:
                await sd.update_one({"_id": today}, {"$inc": {"downloads": 1}}, upsert=True)
            dl = self._col("downloads_log")
            if dl is not None:
                await dl.insert_one({
                    "user_id": uid, "title": title[:200], "quality": quality,
                    "size": int(size or 0), "url": url[:500],
                    "platform": platform, "at": datetime.now(timezone.utc),
                })
        except Exception:
            log.exception("bump_download")

    async def recent_users(self, page: int = 0, per_page: int = 10):
        col = self._col("users")
        if col is None:
            return [], 0
        try:
            total = await col.count_documents({})
            cursor = (col.find({})
                      .sort("last_seen", -1).skip(page * per_page).limit(per_page))
            rows = [doc async for doc in cursor]
            return rows, total
        except Exception:
            log.exception("recent_users")
            return [], 0

    async def all_user_ids(self, blocked: bool = False):
        col = self._col("users")
        if col is None:
            return []
        try:
            cursor = col.find({"blocked": blocked}, {"_id": 1})
            return [doc["_id"] async for doc in cursor]
        except Exception:
            log.exception("all_user_ids")
            return []

    # ---------- channels (force-join) ----------
    async def get_channels(self):
        col = self._col("channels")
        if col is None:
            return []
        try:
            cursor = col.find({}).sort("added_at", 1)
            return [doc async for doc in cursor]
        except Exception:
            log.exception("get_channels")
            return []

    async def add_channel(self, chat_id: int, title: str = "", invite: str = "") -> None:
        col = self._col("channels")
        if col is None:
            return
        try:
            await col.update_one(
                {"_id": chat_id},
                {"$set": {"title": title, "invite_link": invite,
                          "added_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except Exception:
            log.exception("add_channel")

    async def del_channel(self, chat_id: int) -> None:
        col = self._col("channels")
        if col is None:
            return
        try:
            await col.delete_one({"_id": chat_id})
        except Exception:
            log.exception("del_channel")

    # ---------- settings ----------
    async def get_setting(self, key: str, default=None):
        col = self._col("settings")
        if col is None:
            return default
        try:
            doc = await col.find_one({"_id": key})
            return doc.get("value", default) if doc else default
        except Exception:
            return default

    async def set_setting(self, key: str, value) -> None:
        col = self._col("settings")
        if col is None:
            return
        try:
            await col.update_one({"_id": key},
                                 {"$set": {"value": value}}, upsert=True)
        except Exception:
            log.exception("set_setting")

    # ---------- stats ----------
    async def stats(self) -> dict:
        out = {"mongo": self.ok, "users": 0, "blocked": 0, "downloads": 0,
               "today": 0, "channels": 0}
        try:
            u = self._col("users")
            if u is not None:
                out["users"] = await u.count_documents({})
                out["blocked"] = await u.count_documents({"blocked": True})
                pipe = [{"$group": {"_id": None, "s": {"$sum": "$downloads"}}}]
                async for doc in u.aggregate(pipe):
                    out["downloads"] = doc.get("s", 0)
            sd = self._col("stats_daily")
            if sd is not None:
                doc = await sd.find_one({"_id": time.strftime("%Y-%m-%d")})
                out["today"] = (doc or {}).get("downloads", 0)
            c = self._col("channels")
            if c is not None:
                out["channels"] = await c.count_documents({})
        except Exception:
            log.exception("stats")
        return out

    async def ensure_indexes(self) -> None:
        if self.db is None:
            return
        try:
            await self.db["users"].create_index("last_seen")
            await self.db["downloads_log"].create_index("at")
            await self.db["downloads_log"].create_index("user_id")
        except Exception:
            log.exception("ensure_indexes")
