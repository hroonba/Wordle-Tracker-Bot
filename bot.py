from __future__ import annotations

import os, io, re, sqlite3, datetime as dt, traceback, unicodedata
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict

import discord
from discord import app_commands
from discord.ext import commands

# Load .env locally; on Render, env vars come from the dashboard.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Headless plotting for servers
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ========= Config =========
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
OWNER_ID = int(os.getenv("OWNER_ID") or "0")

WORDLE_CHANNEL_ID = int(os.getenv("WORDLE_CHANNEL_ID") or "0")
WORDLE_BOT_ID = int(os.getenv("WORDLE_BOT_ID") or "0")
DB_PATH = os.getenv("DB_PATH") or "wordle_scores.db"
MAX_BACKFILL = int(os.getenv("MAX_BACKFILL") or "1500")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True


# ========= DB =========
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scores (
                user_id     INTEGER NOT NULL,
                username    TEXT NOT NULL,
                day         INTEGER NOT NULL,
                score       INTEGER,
                solved      INTEGER NOT NULL,
                ts          TEXT NOT NULL,
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

def init_aliases_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS aliases (
            name_norm  TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL
        );
        """)

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

def fetch_scores(days_back: Optional[int] = None, user_id: Optional[int] = None) -> List[Tuple]:
    with sqlite3.connect(DB_PATH) as con:
        if days_back is None:
            if user_id:
                cur = con.execute("""
                    SELECT user_id, username, day, score, solved, ts
                      FROM scores
                     WHERE user_id = ?
                     ORDER BY user_id, ts ASC;
                """, (user_id,))
            else:
                cur = con.execute("""
                    SELECT user_id, username, day, score, solved, ts
                      FROM scores
                     ORDER BY user_id, ts ASC;
                """)
        else:
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(1, days_back) - 1)).isoformat()
            if user_id:
                cur = con.execute("""
                    SELECT user_id, username, day, score, solved, ts
                      FROM scores
                     WHERE ts >= ? AND user_id = ?
                     ORDER BY user_id, ts ASC;
                """, (cutoff, user_id))
            else:
                cur = con.execute("""
                    SELECT user_id, username, day, score, solved, ts
                      FROM scores
                     WHERE ts >= ?
                     ORDER BY user_id, ts ASC;
                """, (cutoff,))
        return list(cur.fetchall())

def fetch_leaderboard(days_back: Optional[int] = None, min_games: int = 5) -> List[Tuple]:
    with sqlite3.connect(DB_PATH) as con:
        if days_back is None:
            rows = list(con.execute("""
                SELECT user_id, username, score, solved, ts
                  FROM scores
                 ORDER BY ts ASC;
            """))
        else:
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(1, days_back) - 1)).isoformat()
            rows = list(con.execute("""
                SELECT user_id, username, score, solved, ts
                  FROM scores
                 WHERE ts >= ?
                 ORDER BY ts ASC;
            """, (cutoff,)))

    agg: Dict[tuple, Dict] = {}
    for uid, uname, score, solved, _ts in rows:
        key = identity_key(uid or 0, uname or "")
        label = identity_label(uid or 0, uname or "Unknown")
        g = agg.setdefault(key, {"label": label, "scores": [], "solves": 0, "misses": 0})
        if solved == 1:
            g["scores"].append(score)
            g["solves"] += 1
        else:
            g["misses"] += 1

    out = []
    for key, g in agg.items():
        games = len(g["scores"]) + g["misses"]
        if games < min_games:
            continue
        avg = (sum(g["scores"]) / len(g["scores"])) if g["scores"] else 7.0
        out.append((key, g["label"], avg, g["solves"], g["misses"], games))

    out.sort(key=lambda t: (t[2], -t[3]))
    return out


# ========= Identity & alias helpers =========
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

def resolve_name_to_member(guild: discord.Guild | None, name: str) -> tuple[int, str]:
    if not guild:
        return 0, name
    target = normalize_username(name)
    for m in guild.members:
        if normalize_username(m.display_name) == target:
            return m.id, m.display_name
    for m in guild.members:
        if normalize_username(getattr(m, "global_name", "") or "") == target:
            return m.id, m.display_name
    mapped = alias_lookup(name)
    if mapped:
        m = guild.get_member(mapped)
        if m and m.display_name:
            return m.id, m.display_name
        return mapped, name
    return 0, name


# ========= Data model & parsing =========
@dataclass
class ParsedScore:
    user_id: Optional[int]
    username: str
    day: int
    score: Optional[int]
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

        mention_ids = [int(mm.group("id")) for mm in re.finditer(r"<@!?(?P<id>\d+)>", rest)]
        if mention_ids:
            id_to_member = {mem.id: mem for mem in msg.mentions}
            for uid in mention_ids:
                member = id_to_member.get(uid)
                username = member.display_name if member else f"user:{uid}"
                out.append(ParsedScore(user_id=uid, username=username, day=day, score=score_val, solved=solved))
            continue

        names = [t.strip() for t in rest.split("@") if t.strip()] if "@" in rest else [rest]
        for name in names:
            uid, display = resolve_name_to_member(msg.guild, name)
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
        print("safe_defer: interaction expired.")
        return False
    except Exception as e:
        print("safe_defer error:", repr(e))
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
        print(f"Auto-backfill ran: parsed {count}")
    except Exception:
        traceback.print_exc()


@bot.event
async def on_ready():
    init_db()
    init_aliases_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"Synced to guild {GUILD_ID}: {len(synced)}")
        else:
            synced = await tree.sync()
            print(f"Synced globally: {len(synced)}")
    except Exception as e:
        print("Slash sync error:", repr(e))
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if bot.user and message.author.id == bot.user.id:
        return
    if not message_in_scope(message):
        return
    try:
        if (WORDLE_BOT_ID and message.author.id == WORDLE_BOT_ID) or \
           (not WORDLE_BOT_ID and message.author.bot):
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


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("Error.", ephemeral=True)
        else:
            await interaction.response.send_message("Error.", ephemeral=True)
    except Exception:
        pass


# Commands
@tree.command(description="Ping test")
async def ping(interaction: discord.Interaction):
    if not interaction.response.is_done():
        await interaction.response.send_message("Pong!", ephemeral=True)

@tree.command(description="Admin: resync commands")
async def sync(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        synced = await tree.sync(guild=interaction.guild) if interaction.guild else await tree.sync()
        await interaction.followup.send(f"Synced {len(synced)} commands.", ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Sync error: {e!r}", ephemeral=True)

@tree.command(description="Admin: rescan channel")
@app_commands.describe(limit="Messages to look back (default 500)")
async def rescan(interaction: discord.Interaction, limit: Optional[int] = 500):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        channel = interaction.channel
        count = await do_rescan_channel(channel, limit or 500)
        kv_set("last_backfill_ymd", dt.datetime.now(dt.timezone.utc).date().isoformat())
        await interaction.followup.send(f"Parsed {count} messages.", ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Rescan error: {e!r}", ephemeral=True)

@tree.command(description="Leaderboard (avg guesses, entire history).")
async def leaderboard(interaction: discord.Interaction):
    if not await safe_defer(interaction, ephemeral=False): return
    await ensure_daily_backfill(interaction)
    try:
        lb = fetch_leaderboard(None)
        if not lb:
            await interaction.followup.send("No scores found.")
            return
        lines = ["**Leaderboard (entire history):**"]
        for _k, label, avg, solves, misses, games in lb:
            lines.append(f"{label}: {games} games • avg {avg:.2f} • solves {solves} • X {misses}")
        await interaction.followup.send("\n".join(lines))
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating leaderboard.")

@tree.command(name="plot", description="Histogram of guess counts (entire history).")
@app_commands.describe(top_n="Optional: show only top N users")
async def plot(interaction: discord.Interaction, top_n: Optional[int] = None):
    if not await safe_defer(interaction, ephemeral=False): return
    await ensure_daily_backfill(interaction)
    try:
        rows = fetch_scores(None)
        if not rows:
            await interaction.followup.send("No scores found.")
            return

        from statistics import mean
        per_user: Dict[tuple, Dict] = {}
        for uid, uname, _day, score, solved, _ts in rows:
            key = identity_key(uid or 0, uname or "")
            label = label_plain_for_hist(interaction.guild, uid or 0, uname or "Unknown")
            entry = per_user.setdefault(key, {"label": label, "counts": {i:0 for i in range(1,8)}, "scores":[]})
            s = score if solved == 1 else 7
            entry["scores"].append(s)
            entry["counts"][s] += 1

        ranked = []
        for k, d in per_user.items():
            if not d["scores"]:
                continue
            avg = sum(d["scores"]) / len(d["scores"])
            ranked.append((avg, d))
        ranked.sort(key=lambda t: t[0])
        if top_n:
            ranked = ranked[:top_n]

        guesses = list(range(1,8))
        width = 0.8 / max(1, len(ranked))
        centers = list(range(len(guesses)))

        plt.figure(figsize=(12, 7))
        for i, (avg, d) in enumerate(ranked):
            counts = [d["counts"].get(g, 0) for g in guesses]
            xs = [c + (i - (len(ranked)-1)/2)*width for c in centers]
            plt.bar(xs, counts, width=width, label=f"{d['label']} (avg {avg:.2f})")

        plt.xticks(centers, [1,2,3,4,5,6,"X"])
        plt.xlabel("Guesses (X = fail)")
        plt.ylabel("Count")
        plt.title(f"Guess histogram — entire history ({len(ranked)} users)")
        ncols = max(2, min(len(ranked), 6))
        plt.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=ncols)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=180)
        buf.seek(0)
        await interaction.followup.send(file=discord.File(buf, "hist.png"))
        plt.close()
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating plot.")
    

@tree.command(description="Admin: add alias mapping")
@app_commands.describe(name="Old nickname", user="Real user")
async def alias_add(interaction: discord.Interaction, name: str, user: discord.Member):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        alias_set(name, user.id)
        apply_alias_to_history(name, user.id, user.display_name)
        await interaction.followup.send(f"Alias added: “{name}” → {user.mention}. History updated.", ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Alias add error: {e!r}", ephemeral=True)

@tree.command(description="Admin: remove alias")
async def alias_remove(interaction: discord.Interaction, name: str):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
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
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not await safe_defer(interaction, ephemeral=True): return
    try:
        items = alias_list()
        if not items:
            await interaction.followup.send("No aliases.", ephemeral=True)
            return
        lines = ["**Aliases:**"]
        for name_norm, uid in items:
            lines.append(f"• `{name_norm}` → <@{uid}>")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception as e:
        traceback.print_exc()
        await interaction.followup.send(f"Alias list error: {e!r}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN")
    bot.run(TOKEN)
