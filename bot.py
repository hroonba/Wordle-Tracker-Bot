from __future__ import annotations

import os, io, re, sqlite3, datetime as dt, traceback, unicodedata, difflib, json, math
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict

import discord
from discord import app_commands
from discord.ext import commands

# Optional .env locally; in Render, env vars come from dashboard.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Headless plotting
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

# Avatar color support
import requests
from PIL import Image
from io import BytesIO


# ========= Config =========
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # string; we'll int() later if present
OWNER_ID = int(os.getenv("OWNER_ID") or "0")

WORDLE_CHANNEL_ID = int(os.getenv("WORDLE_CHANNEL_ID") or "0")   # optional channel scope
WORDLE_BOT_ID = int(os.getenv("WORDLE_BOT_ID") or "0")           # optional, but recommended
DB_PATH = os.getenv("DB_PATH") or "wordle_scores.db"
MAX_BACKFILL = int(os.getenv("MAX_BACKFILL") or "1500")

ALIASES_FILE = os.getenv("ALIASES_FILE") or "aliases.json"       # file-based aliases

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True  # needed for robust name matching + avatars


# ========= DB =========
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scores (
                user_id     INTEGER NOT NULL,
                username    TEXT NOT NULL,
                day         INTEGER NOT NULL,  -- Wordle number or YYYYMMDD (inferred)
                score       INTEGER,           -- 1..6, NULL if X
                solved      INTEGER NOT NULL,  -- 1 if solved, 0 if X
                ts          TEXT NOT NULL,     -- ISO-8601 capture time (UTC)
                PRIMARY KEY (user_id, day)
            );
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            );
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS aliases (
                name_norm  TEXT PRIMARY KEY,   -- normalize_username(name)
                user_id    INTEGER NOT NULL
            );
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS retcons (
                user_id   INTEGER PRIMARY KEY,
                delta_x   INTEGER NOT NULL  -- can be 0 or negative; never positive overall
            );
            """
        )

def kv_get(key: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT v FROM kv WHERE k=?;", (key,)).fetchone()
        return row[0] if row else None

def kv_set(key: str, value: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v;", (key, value))

def upsert_score(user_id: int, username: str, day: int, score: Optional[int], solved: bool):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO scores (user_id, username, day, score, solved, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET
                username=excluded.username,
                score=excluded.score,
                solved=excluded.solved,
                ts=excluded.ts;
            """,
            (
                user_id,
                username,
                day,
                None if score is None else score,
                1 if solved else 0,
                dt.datetime.now(dt.timezone.utc).isoformat(),
            ),
        )

def get_retcon_delta(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT delta_x FROM retcons WHERE user_id=?;", (user_id,)).fetchone()
        return int(row[0]) if row else 0

def apply_retcon(user_id: int, change: int) -> int:
    """
    change: +n or -n step for the user's delta.
    Enforces invariant: final delta_x <= 0.
    Returns new delta.
    """
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT delta_x FROM retcons WHERE user_id=?;", (user_id,)).fetchone()
        cur = int(row[0]) if row else 0
        new_val = cur + change
        if new_val > 0:  # clamp at 0; never allow positive total delta
            new_val = 0
        con.execute(
            "INSERT INTO retcons(user_id, delta_x) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET delta_x=excluded.delta_x;",
            (user_id, new_val),
        )
        return new_val

def fetch_all_scores() -> List[Tuple]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("""
            SELECT user_id, username, day, score, solved, ts
              FROM scores
             ORDER BY user_id, ts ASC;
        """)
        return list(cur.fetchall())


# ========= Aliases (DB + file) =========
def normalize_username(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.strip()
    if s.startswith("@"):
        s = s[1:]
    s = s.lower()
    s = "".join(ch for ch in s if ch.isalnum())
    return s

def alias_lookup(name: str) -> int:
    n = normalize_username(name)
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT user_id FROM aliases WHERE name_norm = ?;", (n,)).fetchone()
        return int(row[0]) if row else 0

def alias_set(name: str, user_id: int):
    n = normalize_username(name)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO aliases (name_norm, user_id)
            VALUES (?, ?)
            ON CONFLICT(name_norm) DO UPDATE SET user_id=excluded.user_id;
        """, (n, user_id))

def alias_delete(name: str):
    n = normalize_username(name)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM aliases WHERE name_norm = ?;", (n,))

def alias_list() -> list[tuple[str,int]]:
    with sqlite3.connect(DB_PATH) as con:
        return list(con.execute("SELECT name_norm, user_id FROM aliases ORDER BY name_norm;"))

def apply_alias_to_history(name: str, user_id: int, display_name: str):
    n = normalize_username(name)
    with sqlite3.connect(DB_PATH) as con:
        rows = list(con.execute("SELECT rowid, user_id, username, day, score, solved, ts FROM scores WHERE user_id = 0;"))
        for rowid, _u, un, day, score, solved, ts in rows:
            if normalize_username(un) == n:
                con.execute("DELETE FROM scores WHERE rowid = ?;", (rowid,))
                con.execute("""
                    INSERT INTO scores (user_id, username, day, score, solved, ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, day) DO UPDATE SET
                      username=excluded.username,
                      score=COALESCE(excluded.score, score),
                      solved=excluded.solved,
                      ts=MAX(ts, excluded.ts);
                """, (user_id, display_name, day, score, solved, ts))
        con.commit()

def load_aliases_file(guild: Optional[discord.Guild] = None) -> int:
    """
    Load aliases from aliases.json:
    { "aliases": [ { "name": "scoopthusiast", "user_id": 123 }, ... ] }
    Returns count imported/updated.
    """
    if not os.path.exists(ALIASES_FILE):
        return 0
    try:
        with open(ALIASES_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return 0  # empty file; ignore
            data = json.loads(content)
        items = data.get("aliases", [])
        count = 0
        for it in items:
            name = it.get("name", "")
            uid = int(it.get("user_id", 0))
            if not name or not uid:
                continue
            alias_set(name, uid)
            # retro-migrate history for this alias
            display = None
            if guild:
                m = guild.get_member(uid)
                if m and m.display_name:
                    display = m.display_name
            apply_alias_to_history(name, uid, display or name)
            count += 1
        return count
    except Exception:
        # swallow JSON/IO errors so the bot still boots
        traceback.print_exc()
        return 0


# ========= Identity helpers =========
def identity_key(user_id: int, username: str) -> tuple:
    if user_id:
        return (user_id, "")
    mapped = alias_lookup(username)
    if mapped:
        return (mapped, "")
    return (0, normalize_username(username))

def identity_label(user_id: int, username: str) -> str:
    if user_id:
        return f"<@{user_id}>"
    mapped = alias_lookup(username)
    if mapped:
        return f"<@{mapped}>"
    return username or "Unknown"

def label_plain_for_hist(guild: discord.Guild | None, user_id: int, username: str) -> str:
    if user_id:
        if guild:
            m = guild.get_member(user_id)
            if m and m.display_name:
                return m.display_name
        return username or f"user:{user_id}"
    mapped = alias_lookup(username)
    if mapped:
        if guild:
            m = guild.get_member(mapped)
            if m and m.display_name:
                return m.display_name
        return username or f"user:{mapped}"
    return username or "Unknown"


# ========= Member index (for robust matching) =========
# { guild_id: { normalized_name: (user_id, display_name) } }
MEMBER_INDEX: Dict[int, Dict[str, Tuple[int, str]]] = {}

async def build_member_index(guild: discord.Guild):
    try:
        async for _ in guild.fetch_members(limit=None):
            pass
    except Exception:
        traceback.print_exc()

    index: Dict[str, Tuple[int, str]] = {}
    for m in guild.members:
        label = m.display_name or (getattr(m, "global_name", "") or m.name)
        norm = normalize_username(label)
        if norm:
            index[norm] = (m.id, label)
        gn = getattr(m, "global_name", "") or ""
        if gn:
            norm_gn = normalize_username(gn)
            if norm_gn and norm_gn not in index:
                index[norm_gn] = (m.id, label)
    MEMBER_INDEX[guild.id] = index

def member_index_lookup(guild: discord.Guild | None, norm: str) -> Optional[Tuple[int, str]]:
    if not guild or guild.id not in MEMBER_INDEX:
        return None
    return MEMBER_INDEX[guild.id].get(norm)


# ========= Matching helpers =========
def resolve_name_to_member(guild: discord.Guild | None, name: str) -> tuple[int, str]:
    if not guild:
        return 0, name
    target = normalize_username(name)

    hit = member_index_lookup(guild, target)
    if hit:
        return hit

    for m in guild.members:
        if normalize_username(m.display_name) == target:
            return m.id, m.display_name
        if normalize_username(getattr(m, "global_name", "") or "") == target:
            return m.id, m.display_name

    mapped = alias_lookup(name)
    if mapped:
        m = guild.get_member(mapped)
        if m and m.display_name:
            return m.id, m.display_name
        return mapped, name
    return 0, name

def detect_names_in_free_text(guild: discord.Guild | None, text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    if not text:
        return hits
    target = normalize_username(text)
    seen_ids: set[int] = set()

    if guild and guild.id in MEMBER_INDEX:
        for norm, (uid, label) in MEMBER_INDEX[guild.id].items():
            if norm and norm in target and uid not in seen_ids:
                hits.append((uid, label)); seen_ids.add(uid)

    try:
        with sqlite3.connect(DB_PATH) as con:
            for name_norm, uid in con.execute("SELECT name_norm, user_id FROM aliases;"):
                if name_norm and name_norm in target and uid not in seen_ids:
                    label = None
                    if guild:
                        gm = guild.get_member(uid)
                        if gm and gm.display_name:
                            label = gm.display_name
                    hits.append((uid, label or f"user:{uid}")); seen_ids.add(uid)
    except Exception:
        traceback.print_exc()
    return hits

def best_match_member_or_alias(
    guild: discord.Guild | None,
    raw_name: str,
    ratio_threshold: float = 0.72
) -> tuple[int, str]:
    cleaned = raw_name.strip()
    cleaned = re.sub(r"[^A-Za-z0-9'’ _-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return 0, raw_name

    target_norm = normalize_username(cleaned)
    hit = member_index_lookup(guild, target_norm) if guild else None
    if hit:
        return hit

    best = (0.0, 0, cleaned)  # (score, user_id, label)

    if guild and guild.id in MEMBER_INDEX:
        for norm, (uid, label) in MEMBER_INDEX[guild.id].items():
            score = difflib.SequenceMatcher(None, norm, target_norm).ratio()
            if score > best[0]:
                best = (score, uid, label)

    try:
        with sqlite3.connect(DB_PATH) as con:
            for name_norm, uid in con.execute("SELECT name_norm, user_id FROM aliases;"):
                if not name_norm:
                    continue
                if name_norm == target_norm:
                    lab = None
                    if guild:
                        gm = guild.get_member(uid)
                        if gm and gm.display_name:
                            lab = gm.display_name
                    return uid, (lab or cleaned)
                score = difflib.SequenceMatcher(None, name_norm, target_norm).ratio()
                if score > best[0]:
                    lab = cleaned
                    if guild:
                        gm = guild.get_member(uid)
                        if gm and gm.display_name:
                            lab = gm.display_name
                    best = (score, uid, lab)
    except Exception:
        traceback.print_exc()

    if best[1] and best[0] >= ratio_threshold:
        return best[1], best[2]
    return 0, cleaned


# ========= Data model & parsing =========
@dataclass
class ParsedScore:
    user_id: Optional[int]
    username: str
    day: int
    score: Optional[int]   # None => X
    solved: bool

DAY_RE_TEXT = re.compile(r"\bWordle\s+(?:No\.?\s*)?(?P<day>\d+)\b", re.IGNORECASE)
SCORE_LINE_RE = re.compile(r"^(?:\*\*)?(?:👑\s*)?(?P<score>[Xx]|\d)\/6:\s*(?P<rest>.+)$")

def _extract_day_from_message(msg: discord.Message) -> Optional[int]:
    m = DAY_RE_TEXT.search(msg.content or "")
    if m:
        return int(m.group("day"))
    for emb in msg.embeds:
        if emb.title:
            m = DAY_RE_TEXT.search(emb.title)
            if m: return int(m.group("day"))
        if emb.description:
            m = DAY_RE_TEXT.search(emb.description)
            if m: return int(m.group("day"))
    return None

def parse_group_summary_style(msg: discord.Message) -> List[ParsedScore]:
    day = _extract_day_from_message(msg)
    if day is None:
        d = (msg.created_at - dt.timedelta(days=1)).date()
        day = d.year * 10000 + d.month * 100 + d.day

    out: List[ParsedScore] = []
    for raw in (msg.content or "").splitlines():
        line = raw.strip()
        m = SCORE_LINE_RE.match(line)
        if not m:
            continue

        raw_score = m.group("score")
        rest = m.group("rest").strip()
        score_val = None if raw_score.lower() == "x" else int(raw_score)
        solved = score_val is not None

        # 1) Real mentions
        mention_ids = [int(mm.group("id")) for mm in re.finditer(r"<@!?(?P<id>\d+)>", rest)]
        if mention_ids:
            id_to_member = {mem.id: mem for mem in msg.mentions}
            for uid in mention_ids:
                member = id_to_member.get(uid)
                username = member.display_name if member else f"user:{uid}"
                out.append(ParsedScore(user_id=uid, username=username, day=day, score=score_val, solved=solved))
            rest = re.sub(r"<@!?\d+>", " ", rest)
            rest = re.sub(r"\s+", " ", rest).strip()

        # 2) Plain-text '@' segments
        if "@" in rest:
            segs = [s.strip() for s in rest.split("@") if s.strip()]
            for seg in segs:
                uid, label = best_match_member_or_alias(msg.guild, seg)
                out.append(ParsedScore(user_id=uid, username=label, day=day, score=score_val, solved=solved))
            continue

        # 3) Free-text fallback
        if rest:
            detected = detect_names_in_free_text(msg.guild, rest)
            if detected:
                for uid, display in detected:
                    out.append(ParsedScore(user_id=uid, username=display, day=day, score=score_val, solved=solved))
            else:
                uid, display = resolve_name_to_member(msg.guild, rest)
                out.append(ParsedScore(user_id=uid, username=display, day=day, score=score_val, solved=solved))

    return out

def message_in_scope(msg: discord.Message) -> bool:
    return (not WORDLE_CHANNEL_ID) or (msg.channel.id == WORDLE_CHANNEL_ID)


# ========= Bot & helpers =========
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True, thinking: bool = True) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except discord.NotFound:
        print("safe_defer: interaction expired (likely cold start).")
        return False
    except Exception as e:
        print("safe_defer: unexpected error:", repr(e))
        return False

async def do_rescan_channel(channel: discord.abc.Messageable, limit: int) -> int:
    parsed = 0
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return parsed
    async for msg in channel.history(limit=limit, oldest_first=True):
        if bot.user and msg.author.id == bot.user.id:
            continue
        if not message_in_scope(msg):
            continue
        if (WORDLE_BOT_ID and msg.author.id == WORDLE_BOT_ID) or (not WORDLE_BOT_ID and msg.author.bot):
            for p in parse_group_summary_style(msg):
                upsert_score(p.user_id or 0, p.username, p.day, p.score, p.solved)
                parsed += 1
            continue
        m = re.search(r"\bWordle\s+(?P<day>\d+)\s+(?P<score>[Xx]|\d)\/6\b",
                      msg.content or "", re.IGNORECASE)
        if m:
            day = int(m.group("day"))
            s = m.group("score")
            score_val = None if s.lower() == "x" else int(s)
            upsert_score(msg.author.id, msg.author.display_name, day,
                         score_val, score_val is not None)
            parsed += 1
    return parsed

async def ensure_daily_backfill(interaction: discord.Interaction) -> None:
    today_ymd = dt.datetime.now(dt.timezone.utc).date().isoformat()
    last = kv_get("last_backfill_ymd")
    if last == today_ymd:
        return

    channel: discord.abc.Messageable
    if WORDLE_CHANNEL_ID and interaction.guild:
        ch = interaction.guild.get_channel(WORDLE_CHANNEL_ID)
        channel = ch or interaction.channel
    else:
        channel = interaction.channel

    try:
        count = await do_rescan_channel(channel, MAX_BACKFILL)
        kv_set("last_backfill_ymd", today_ymd)
        print(f"Auto-backfill ran: parsed {count} items; set day={today_ymd}")
    except Exception:
        traceback.print_exc()


# ========= Avatar colors (cached) =========
_AVATAR_COLOR_CACHE: Dict[int, Tuple[float,float,float]] = {}

def _dominant_color_from_bytes(b: bytes) -> Tuple[float,float,float]:
    img = Image.open(BytesIO(b)).convert("RGB")
    img = img.resize((32, 32))
    pixels = list(img.getdata())
    r = sum(p[0] for p in pixels) / len(pixels)
    g = sum(p[1] for p in pixels) / len(pixels)
    b_ = sum(p[2] for p in pixels) / len(pixels)
    return (r/255.0, g/255.0, b_/255.0)

def get_user_color(guild: Optional[discord.Guild], user_id: int) -> Tuple[float,float,float]:
    if user_id in _AVATAR_COLOR_CACHE:
        return _AVATAR_COLOR_CACHE[user_id]
    def fallback(uid: int) -> Tuple[float,float,float]:
        h = (uid % 360) / 360.0
        r = 0.5 + 0.4*math.sin(2*math.pi*h)
        g = 0.5 + 0.4*math.sin(2*math.pi*(h+1/3))
        b = 0.5 + 0.4*math.sin(2*math.pi*(h+2/3))
        return (max(0,min(1,r)), max(0,min(1,g)), max(0,min(1,b)))
    try:
        if guild:
            m = guild.get_member(user_id)
            if m:
                url = m.display_avatar.replace(size=64).url
                r = requests.get(url, timeout=5)
                if r.ok:
                    col = _dominant_color_from_bytes(r.content)
                    _AVATAR_COLOR_CACHE[user_id] = col
                    return col
    except Exception:
        pass
    col = fallback(user_id)
    _AVATAR_COLOR_CACHE[user_id] = col
    return col


# ========= Events =========
@bot.event
async def on_ready():
    init_db()
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            print(f"Slash commands synced to guild {GUILD_ID}: {len(synced)}")
        else:
            synced = await tree.sync()
            print(f"Slash commands synced globally: {len(synced)}")
    except Exception as e:
        print("Slash sync error:", repr(e))

    try:
        for g in bot.guilds:
            await build_member_index(g)
            load_aliases_file(g)  # import file aliases on boot
            print(f"Indexed {len(MEMBER_INDEX.get(g.id, {}))} members for guild {g.id}")
    except Exception:
        traceback.print_exc()

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_guild_join(guild: discord.Guild):
    await build_member_index(guild)
    load_aliases_file(guild)

@bot.event
async def on_guild_available(guild: discord.Guild):
    await build_member_index(guild)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    await build_member_index(after.guild)

@bot.event
async def on_member_join(member: discord.Member):
    await build_member_index(member.guild)

@bot.event
async def on_member_remove(member: discord.Member):
    await build_member_index(member.guild)

@bot.event
async def on_message(message: discord.Message):
    if bot.user and message.author.id == bot.user.id:
        return
    if not message_in_scope(message):
        return
    try:
        if (WORDLE_BOT_ID and message.author.id == WORDLE_BOT_ID) or (not WORDLE_BOT_ID and message.author.bot):
            for p in parse_group_summary_style(message):
                upsert_score(p.user_id or 0, p.username, p.day, p.score, p.solved)
    except Exception:
        traceback.print_exc()

    try:
        m = re.search(r"\bWordle\s+(?P<day>\d+)\s+(?P<score>[Xx]|\d)\/6\b",
                      message.content or "", re.IGNORECASE)
        if m:
            day = int(m.group("day"))
            s = m.group("score")
            score_val = None if s.lower() == "x" else int(s)
            upsert_score(message.author.id, message.author.display_name, day,
                         score_val, score_val is not None)
    except Exception:
        traceback.print_exc()

    await bot.process_commands(message)


# ========= Global slash error handler =========
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        msg = "Sorry — that command hit an error."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ========= Slash commands =========
@tree.command(description="Ping (quick test)")
async def ping(interaction: discord.Interaction):
    if not interaction.response.is_done():
        await interaction.response.send_message("Pong!", ephemeral=True)

@tree.command(description="Admin: resync/clear application commands")
@app_commands.describe(scope="Use one of: guild (default), global, purge_guild, purge_global")
async def sync(interaction: discord.Interaction, scope: Optional[str] = "guild"):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    scope = (scope or "guild").lower()
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        if scope == "guild":
            if not interaction.guild:
                await interaction.followup.send("Run in a server.", ephemeral=True)
                return
            guild_obj = discord.Object(id=interaction.guild_id)
            tree.copy_global_to(guild=guild_obj)
            out = await tree.sync(guild=guild_obj)
            await interaction.followup.send(f"Synced {len(out)} command(s) to this guild.", ephemeral=True)
        elif scope == "global":
            out = await tree.sync()
            await interaction.followup.send(f"Synced {len(out)} global command(s).", ephemeral=True)
        elif scope == "purge_guild":
            if not interaction.guild:
                await interaction.followup.send("Run in a server.", ephemeral=True)
                return
            guild_obj = discord.Object(id=interaction.guild_id)
            tree.clear_commands(guild=guild_obj)
            await tree.sync(guild=guild_obj)
            tree.copy_global_to(guild=guild_obj)
            out = await tree.sync(guild=guild_obj)
            await interaction.followup.send(f"Purged and re-synced guild commands ({len(out)}).", ephemeral=True)
        elif scope == "purge_global":
            tree.clear_commands()
            await tree.sync()
            await interaction.followup.send("Purged all GLOBAL commands from Discord.", ephemeral=True)
        else:
            await interaction.followup.send("Unknown scope.", ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Sync error: {e!r}", ephemeral=True)

# ---- Alias controls
@tree.command(description="Admin: add an alias mapping (old nickname -> @user)")
@app_commands.describe(name="Old display name as it appears in summaries", user="The real user")
async def alias_add(interaction: discord.Interaction, name: str, user: discord.Member):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True); return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        alias_set(name, user.id)
        apply_alias_to_history(name, user.id, user.display_name)
        await interaction.followup.send(f"Alias added: “{name}” → {user.mention}. History updated.", ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Alias add error: {e!r}", ephemeral=True)

@tree.command(description="Admin: remove an alias by name")
async def alias_remove(interaction: discord.Interaction, name: str):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True); return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        alias_delete(name)
        await interaction.followup.send(f"Alias removed: “{name}”.", ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Alias remove error: {e!r}", ephemeral=True)

@tree.command(description="Admin: list aliases")
async def alias_list_cmd(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True); return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        items = alias_list()
        if not items:
            await interaction.followup.send("No aliases.", ephemeral=True); return
        lines = ["**Aliases:**"]
        for name_norm, uid in items:
            label = name_norm
            if interaction.guild:
                m = interaction.guild.get_member(uid)
                if m:
                    label = m.display_name
            lines.append(f"• `{name_norm}` → {label} (<@{uid}>)")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Alias list error: {e!r}", ephemeral=True)

@tree.command(description="Admin: reload aliases.json and apply to history")
async def alias_reload(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True); return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        n = load_aliases_file(interaction.guild)
        await interaction.followup.send(f"Reloaded aliases.json (imported/updated {n}).", ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Alias reload error: {e!r}", ephemeral=True)

# ---- Public: Leaderboard (entire history; min 20 games)
@tree.command(description="Leaderboard")
async def leaderboard(interaction: discord.Interaction):
    await ensure_daily_backfill(interaction)
    if not await safe_defer(interaction, ephemeral=False): return
    try:
        rows = fetch_all_scores()
        if not rows:
            await interaction.followup.send("No data yet."); return

        # Aggregate
        agg: Dict[tuple, Dict] = {}
        for uid, uname, _day, score, solved, _ts in rows:
            key = identity_key(uid or 0, uname or "")
            label = identity_label(uid or 0, uname or "Unknown")
            g = agg.setdefault(key, {"label": label, "solved_scores": [], "misses": 0, "uid": uid or 0, "uname": uname})
            if solved == 1:
                g["solved_scores"].append(score)
            else:
                g["misses"] += 1

        # Apply retcon deltas and build output
        out = []
        for key, g in agg.items():
            uid_guess = key[0] if key[0] else alias_lookup(g["uname"])
            delta = get_retcon_delta(uid_guess) if uid_guess else 0
            effective_misses = max(0, g["misses"] + delta)  # delta <= 0
            games = len(g["solved_scores"]) + effective_misses
            if games < 20:
                continue
            total_points = sum(g["solved_scores"]) + 7 * effective_misses
            avg = total_points / games if games > 0 else 7.0
            out.append((avg, g["label"], len(g["solved_scores"]), effective_misses, games))

        out.sort(key=lambda t: (t[2] == 0, t[0], -t[2]))  # prefer anyone with games, then avg asc, solves desc
        if not out:
            await interaction.followup.send("No one meets the minimum games (20)."); return

        lines = ["**Leaderboard**\n_Min 20 games to rank_"]
        for i, (avg, label, solves, misses, games) in enumerate(out, 1):
            lines.append(f"#{i} {label}: avg {avg:.2f} (solves {solves}, X {misses}, games {games})")
        await interaction.followup.send("\n".join(lines))
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating leaderboard.")

# ---- Public: Stats (single user)
@tree.command(description="Stats for a single player")
@app_commands.describe(user="Mention the user or type a name to match")
async def stats(interaction: discord.Interaction, user: Optional[str] = None):
    await ensure_daily_backfill(interaction)
    if not await safe_defer(interaction, ephemeral=False): return
    try:
        # Resolve user
        target_id = 0; target_label = ""
        member_obj: Optional[discord.Member] = None
        if interaction.guild and user and user.startswith("<@") and user.endswith(">"):
            try:
                num = int(re.sub(r"[<@!>]", "", user))
                member_obj = interaction.guild.get_member(num)
            except Exception:
                member_obj = None
        if not member_obj and interaction.guild and user:
            uid, label = best_match_member_or_alias(interaction.guild, user)
            if uid:
                member_obj = interaction.guild.get_member(uid)
                target_id, target_label = uid, (member_obj.display_name if member_obj else label)
            else:
                target_label = label
        if member_obj:
            target_id = member_obj.id
            target_label = member_obj.display_name or member_obj.name
        if not target_id:
            target_id = interaction.user.id
            target_label = interaction.user.display_name or interaction.user.name

        rows = fetch_all_scores()
        user_rows = [(uid, uname, day, score, solved, ts) for (uid, uname, day, score, solved, ts) in rows
                     if (uid == target_id) or (uid == 0 and alias_lookup(uname) == target_id)]

        if not user_rows:
            await interaction.followup.send(f"No scores found for **{target_label}**."); return

        # Tally
        counts = {i: 0 for i in range(1, 8)}  # 1..6 + 7 (X)
        for _uid, _uname, _day, score, solved, _ts in user_rows:
            if solved == 1:
                counts[score] += 1
            else:
                counts[7] += 1

        delta = get_retcon_delta(target_id)
        if delta < 0:
            counts[7] = max(0, counts[7] + delta)

        games = sum(counts.values())
        wins = games - counts[7]
        fails = counts[7]

        # Average/median with X=7
        flat = []
        for g, c in counts.items():
            flat.extend([g]*c)
        avg = (sum(flat) / len(flat)) if flat else 7.0
        med = 7
        if flat:
            flat_sorted = sorted(flat)
            n = len(flat_sorted)
            if n % 2 == 1:
                med = flat_sorted[n//2]
            else:
                med = (flat_sorted[n//2 - 1] + flat_sorted[n//2]) / 2.0

        buckets = " • ".join([f"{lbl}: {counts[i]}" for i, lbl in enumerate([None,"1","2","3","4","5","6","X"]) if i])
        inc_line = f"Incomplete games (retcon): {abs(delta)}" if delta < 0 else "Incomplete games (retcon): 0"
        text = (
            f"**Stats — {target_label}**\n"
            f"Games: {games}  •  Wins: {wins}  •  Fails: {fails}\n"
            f"Average: {avg:.2f}  •  Median: {med if isinstance(med,int) else f'{med:.2f}'}\n"
            f"{inc_line}\n\n"
            f"Guess counts:\n{buckets}"
        )
        await interaction.followup.send(text)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating stats.")

# ---- Helpers for bar labels
def _annotate_bars(ax, rects, values, fmt=lambda v: str(v), min_show: float = 0.0):
    """
    Place text labels centered above each bar.
    - rects: list of Rectangle patches
    - values: same-length list of numeric values to display
    - fmt: formatter returning a string
    - min_show: don't show labels for values <= min_show
    """
    for rect, val in zip(rects, values):
        if val is None or val <= min_show:
            continue
        height = rect.get_height()
        ax.annotate(
            fmt(val),
            xy=(rect.get_x() + rect.get_width() / 2, height),
            xytext=(0, 2),  # offset in points
            textcoords="offset points",
            ha="center", va="bottom", fontsize=8,
        )

# ---- Public: Histogram (/plot) with value labels
@tree.command(name="plot", description="Histogram of guess counts per user")
async def plot_histogram(interaction: discord.Interaction, top_n: Optional[int] = None):
    await ensure_daily_backfill(interaction)
    if not await safe_defer(interaction, ephemeral=False): return
    try:
        rows = fetch_all_scores()
        if not rows:
            await interaction.followup.send("No scores found."); return

        per_user: Dict[tuple, Dict] = {}
        for uid, uname, _day, score, solved, _ts in rows:
            key = identity_key(uid or 0, uname or "")
            label = label_plain_for_hist(interaction.guild, uid or 0, uname or "Unknown")
            entry = per_user.setdefault(key, {"label": label, "uid": (uid or alias_lookup(uname)), "counts": {i:0 for i in range(1,8)}})
            s = (score if solved == 1 else 7)
            entry["counts"][s] += 1

        # Apply retcon deltas to X bin
        for key, d in per_user.items():
            uid = d["uid"]
            if uid:
                delta = get_retcon_delta(uid)
                if delta < 0:
                    d["counts"][7] = max(0, d["counts"][7] + delta)

        # Rank by avg and optionally clip to top_n
        ranked = []
        for key, d in per_user.items():
            flat = []
            for g, c in d["counts"].items():
                flat.extend([g]*c)
            if not flat:
                continue
            avg = sum(flat)/len(flat)
            ranked.append((avg, key, d))
        ranked.sort(key=lambda t: t[0])
        if top_n:
            ranked = ranked[: top_n]

        guesses = list(range(1, 8))  # 1..6 + 7=X
        n_users = max(1, len(ranked))
        width = 0.8 / n_users
        centers = list(range(len(guesses)))

        # Wider canvas scaling with user count
        fig_w = min(28, 10 + n_users * 1.2)
        fig_h = 9.5  # a tad taller for labels + legend
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))

        handles, labels = [], []
        max_y = 0
        for i, (avg, _key, d) in enumerate(ranked):
            counts = [d["counts"].get(g, 0) for g in guesses]
            xs = [c + (i - (n_users-1)/2)*width for c in centers]
            uid = d["uid"] or 0
            color = get_user_color(interaction.guild, uid) if uid else None
            bars = ax.bar(xs, counts, width=width, label=f"{d['label']} (avg {avg:.2f})", color=color)
            _annotate_bars(ax, bars, counts, fmt=lambda v: f"{int(v)}", min_show=0.0)
            max_y = max(max_y, max(counts) if counts else 0)
            handles.append(Patch(color=color if color else 'tab:blue'))
            labels.append(f"{d['label']} (avg {avg:.2f})")

        ax.set_xticks(centers, [1,2,3,4,5,6,"X"])
        ax.set_xlabel("Guesses (X = fail)")
        ax.set_ylabel("Count")
        ax.set_title(f"Guess histogram ({n_users} users)")
        # Headroom for labels
        ax.set_ylim(top=max_y * 1.15 + 0.5)

        ncols = max(2, min(n_users, 6))
        ax.legend(handles=handles, labels=labels, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=ncols, frameon=False)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=180, bbox_inches="tight")
        buf.seek(0)
        await interaction.followup.send(file=discord.File(buf, "hist.png"))
        plt.close(fig)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating histogram.")

# ---- Public: Normalized histogram (/plot_normalized) with percent labels
@tree.command(name="plot_normalized", description="Histogram normalized per user (percent)")
async def plot_normalized(interaction: discord.Interaction, top_n: Optional[int] = None):
    await ensure_daily_backfill(interaction)
    if not await safe_defer(interaction, ephemeral=False): return
    try:
        rows = fetch_all_scores()
        if not rows:
            await interaction.followup.send("No scores found."); return

        per_user: Dict[tuple, Dict] = {}
        for uid, uname, _day, score, solved, _ts in rows:
            key = identity_key(uid or 0, uname or "")
            label = label_plain_for_hist(interaction.guild, uid or 0, uname or "Unknown")
            entry = per_user.setdefault(key, {"label": label, "uid": (uid or alias_lookup(uname)), "counts": {i:0 for i in range(1,8)}})
            s = (score if solved == 1 else 7)
            entry["counts"][s] += 1

        # Apply retcon deltas to X bin
        for key, d in per_user.items():
            uid = d["uid"]
            if uid:
                delta = get_retcon_delta(uid)
                if delta < 0:
                    d["counts"][7] = max(0, d["counts"][7] + delta)

        # Rank by avg, then normalize counts to shares
        ranked = []
        for key, d in per_user.items():
            total = sum(d["counts"].values())
            if total == 0:
                continue
            flat = []
            for g, c in d["counts"].items():
                flat.extend([g]*c)
            avg = sum(flat)/len(flat)
            ranked.append((avg, key, d, total))
        ranked.sort(key=lambda t: t[0])
        if top_n:
            ranked = ranked[: top_n]

        guesses = list(range(1, 8))
        n_users = max(1, len(ranked))
        width = 0.8 / n_users
        centers = list(range(len(guesses)))

        fig_w = min(28, 10 + n_users * 1.2)
        fig_h = 9.5
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))

        handles, labels = [], []
        max_y = 0.0
        for i, (avg, _key, d, total) in enumerate(ranked):
            shares = [(d["counts"].get(g, 0) / total) for g in guesses]
            percents = [s*100.0 for s in shares]
            xs = [c + (i - (n_users-1)/2)*width for c in centers]
            uid = d["uid"] or 0
            color = get_user_color(interaction.guild, uid) if uid else None
            bars = ax.bar(xs, percents, width=width, label=f"{d['label']} (avg {avg:.2f})", color=color)
            _annotate_bars(ax, bars, percents, fmt=lambda v: f"{v:.0f}%", min_show=0.5)  # hide super tiny labels
            max_y = max(max_y, max(percents) if percents else 0.0)
            handles.append(Patch(color=color if color else 'tab:blue'))
            labels.append(f"{d['label']} (avg {avg:.2f})")

        ax.set_xticks(centers, [1,2,3,4,5,6,"X"])
        ax.set_xlabel("Guesses (X = fail)")
        ax.set_ylabel("Percent")
        ax.set_title(f"Guess histogram (normalized) ({n_users} users)")
        # Headroom for labels
        ax.set_ylim(0, max(100.0, max_y * 1.15 + 2.0))

        ncols = max(2, min(n_users, 6))
        ax.legend(handles=handles, labels=labels, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=ncols, frameon=False)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=180, bbox_inches="tight")
        buf.seek(0)
        await interaction.followup.send(file=discord.File(buf, "hist_normalized.png"))
        plt.close(fig)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating normalized histogram.")

# ---- Public: /retcon (adjust X total; deltas are ≤ 0 overall)
@tree.command(description="Adjust a player's X total (use to remove unfair X or undo a removal)")
@app_commands.describe(user="Target user", action="remove or add", count="How many (default 1)")
async def retcon(interaction: discord.Interaction, user: discord.Member, action: str, count: Optional[int] = 1):
    # Auth: owner can retcon anyone; users can retcon themselves
    if user.id != interaction.user.id and (OWNER_ID and interaction.user.id != OWNER_ID):
        await interaction.response.send_message("Not authorized to modify others.", ephemeral=True)
        return

    action = (action or "").strip().lower()
    if action not in ("remove", "add"):
        await interaction.response.send_message("Action must be 'remove' or 'add'.", ephemeral=True)
        return

    c = int(count or 1)
    if c <= 0:
        await interaction.response.send_message("Count must be positive.", ephemeral=True)
        return

    # remove => delta -c ; add => delta +c but overall clamped ≤ 0
    change = (-c) if action == "remove" else (+c)
    new_delta = apply_retcon(user.id, change)
    # Public confirmation
    verb = "removed" if action == "remove" else "added"
    await interaction.response.send_message(
        f"{user.mention}: {verb} {c} X (retcon). New incomplete-games delta: {new_delta}",
        ephemeral=False,
    )

# ---- Admin: manual rescan (ephemeral)
@tree.command(description="Re-parse last N messages.")
@app_commands.describe(limit="How many messages (default 500)")
async def rescan(interaction: discord.Interaction, limit: Optional[int] = 500):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True); return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Run in a text channel.", ephemeral=True); return
        parsed = await do_rescan_channel(channel, (limit or 500))
        await interaction.followup.send(f"Rescan complete. Parsed {parsed}.", ephemeral=True)
        kv_set("last_backfill_ymd", dt.datetime.now(dt.timezone.utc).date().isoformat())
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error during rescan.", ephemeral=True)


# ========= Entrypoint =========
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN in environment.")
    bot.run(TOKEN)
