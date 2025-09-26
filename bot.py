from __future__ import annotations

import os, io, re, sys, sqlite3, datetime as dt, traceback
from dataclasses import dataclass
from typing import Optional, List, Tuple

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
GUILD_ID = os.getenv("GUILD_ID")            # string; we'll int() later if present
OWNER_ID = int(os.getenv("OWNER_ID") or "0")

WORDLE_CHANNEL_ID = int(os.getenv("WORDLE_CHANNEL_ID") or "0")   # optional scope
WORDLE_BOT_ID = int(os.getenv("WORDLE_BOT_ID") or "0")           # recommended
DB_PATH = os.getenv("DB_PATH") or "wordle_scores.db"

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
                score       INTEGER,           -- 1..6, NULL if X
                solved      INTEGER NOT NULL,  -- 1 if solved, 0 if X
                ts          TEXT NOT NULL,
                PRIMARY KEY (user_id, day)
            );
            """
        )

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
                dt.datetime.utcnow().isoformat(),
            ),
        )

def fetch_scores(days_back: int, user_id: Optional[int] = None) -> List[Tuple]:
    """
    Return rows in the last `days_back` days, using ts (ISO-8601) so it works
    for both real Wordle numbers and inferred YYYYMMDD day-keys.
    """
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=(days_back or 30) - 1)).isoformat()
    with sqlite3.connect(DB_PATH) as con:
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

def fetch_leaderboard(days_back: int, min_games: int = 5) -> List[Tuple]:
    """
    Aggregate over the last `days_back` days via ts cutoff.
    X is treated as 7 for averages.
    """
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=(days_back or 30) - 1)).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("""
            SELECT user_id, MAX(username),
                   AVG(CASE WHEN solved=1 THEN score ELSE 7 END) AS avg_score,
                   SUM(CASE WHEN solved=1 THEN 1 ELSE 0 END) AS solves,
                   SUM(CASE WHEN solved=0 THEN 1 ELSE 0 END) AS misses,
                   COUNT(*) AS games
              FROM scores
             WHERE ts >= ?
             GROUP BY user_id
            HAVING games >= ?
             ORDER BY avg_score ASC, solves DESC;
        """, (cutoff, min_games))
        return list(cur.fetchall())


# ========= Data model & parsing =========
@dataclass
class ParsedScore:
    user_id: Optional[int]
    username: str
    day: int
    score: Optional[int]   # None => X
    solved: bool

# Example daily summary lines:
#   "👑 2/6: @Where Jon Al Gaib"
#   "3/6: <@2866475...> <@1296962...> <@...> ..."
DAY_RE_TEXT = re.compile(r"\bWordle\s+(?:No\.?\s*)?(?P<day>\d+)\b", re.IGNORECASE)
SCORE_LINE_RE = re.compile(r"^(?:\*\*)?(?:👑\s*)?(?P<score>[Xx]|\d)\/6:\s*(?P<rest>.+)$")

def _extract_day_from_message(msg: discord.Message) -> Optional[int]:
    # Try text
    m = DAY_RE_TEXT.search(msg.content or "")
    if m:
        return int(m.group("day"))
    # Try embeds (title/description)
    for emb in msg.embeds:
        if emb.title:
            m = DAY_RE_TEXT.search(emb.title)
            if m: return int(m.group("day"))
        if emb.description:
            m = DAY_RE_TEXT.search(emb.description)
            if m: return int(m.group("day"))
    return None

def parse_group_summary_style(msg: discord.Message) -> List[ParsedScore]:
    """
    Parses lines like:
      '👑 2/6: @Where Jon Al Gaib'
      '3/6: <@2866> <@1296> <@2680> <@3827>'
      '4/6: <@1486>'
    If the Wordle number isn't present, we infer "yesterday" and encode it as YYYYMMDD.
    """
    day = _extract_day_from_message(msg)
    if day is None:
        # Infer yesterday's date in UTC, encode as YYYYMMDD (e.g., 20250925)
        # This becomes our 'day' key for storage/uniqueness.
        utc_dt = msg.created_at  # discord.py gives this in UTC
        puzzle_date = (utc_dt - dt.timedelta(days=1)).date()
        day = puzzle_date.year * 10000 + puzzle_date.month * 100 + puzzle_date.day

    results: List[ParsedScore] = []
    for raw in (msg.content or "").splitlines():
        line = raw.strip()
        m = SCORE_LINE_RE.match(line)
        if not m:
            continue

        raw_score = m.group("score")
        rest = m.group("rest").strip()
        score_val = None if raw_score.lower() == "x" else int(raw_score)
        solved = score_val is not None

        # Prefer actual mentions
        mention_ids = [int(mm.group("id")) for mm in re.finditer(r"<@!?(?P<id>\d+)>", rest)]
        if mention_ids:
            id_to_member = {mem.id: mem for mem in msg.mentions}
            for uid in mention_ids:
                member = id_to_member.get(uid)
                username = member.display_name if member else f"user:{uid}"
                results.append(ParsedScore(user_id=uid, username=username, day=day,
                                           score=score_val, solved=solved))
            continue

        # Fallback: entire remainder is a spaced display name like '@Where Jon Al Gaib'
        username = rest
        if username.startswith("**") and username.endswith("**"):
            username = username[2:-2].strip()
        if username.startswith("@"):
            username = username[1:].strip()
        if username:
            results.append(ParsedScore(user_id=0, username=username, day=day,
                                       score=score_val, solved=solved))
    return results


def message_in_scope(msg: discord.Message) -> bool:
    return (not WORDLE_CHANNEL_ID) or (msg.channel.id == WORDLE_CHANNEL_ID)


# ========= Bot & helpers =========
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True, thinking: bool = True) -> bool:
    """Acknowledge the interaction; return False if it's already expired (cold start)."""
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


@bot.event
async def on_ready():
    init_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)   # copy any globally-declared cmds into this guild
            synced = await tree.sync(guild=guild)
            print(f"Slash commands synced to guild {GUILD_ID}: {len(synced)}")
        else:
            synced = await tree.sync()         # global (slower to appear)
            print(f"Slash commands synced globally: {len(synced)}")
    except Exception as e:
        print("Slash sync error:", repr(e))
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if bot.user and message.author.id == bot.user.id:
        return
    if not message_in_scope(message):
        return

    # Parse daily summary from the Wordle bot
    try:
        if (WORDLE_BOT_ID and message.author.id == WORDLE_BOT_ID) or \
           (not WORDLE_BOT_ID and message.author.bot):
            for p in parse_group_summary_style(message):
                upsert_score(p.user_id or 0, p.username, p.day, p.score, p.solved)
    except Exception:
        traceback.print_exc()

    # Parse human shares like "Wordle 1558 3/6"
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

@tree.command(description="Admin: resync application commands")
@app_commands.describe(scope="Use 'guild' (default) or 'global'")
async def sync(interaction: discord.Interaction, scope: str | None = "guild"):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not await safe_defer(interaction):
        return
    try:
        if (scope or "guild").lower() == "global":
            out = await tree.sync()
            await interaction.followup.send(f"Synced {len(out)} command(s) globally.", ephemeral=True)
        else:
            gid = interaction.guild_id
            out = await tree.sync(guild=discord.Object(id=gid))
            await interaction.followup.send(f"Synced {len(out)} command(s) to this guild.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Sync error: {e!r}", ephemeral=True)

@tree.command(description="Show leaderboard over recent days (default 30).")
@app_commands.describe(days="How many recent days to include (default 30).")
async def leaderboard(interaction: discord.Interaction, days: Optional[int] = 30):
    if not await safe_defer(interaction):
        return
    try:
        rows = fetch_leaderboard(days_back=days or 30)
        if not rows:
            await interaction.followup.send(f"No data yet for {days} day(s).", ephemeral=True)
            return
        lines = [f"**Leaderboard (last {days} days)**\n_Min 5 games_"]
        for rank, (uid, username, avg, solves, misses, games) in enumerate(rows, 1):
            name = f"<@{uid}>" if uid else (username or "Unknown")
            lines.append(f"#{rank} {name}: avg {avg:.2f} (solves {solves}, X {misses}, games {games})")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating leaderboard.", ephemeral=True)

@tree.command(description="Plot scores over time for a user or everyone.")
@app_commands.describe(user="Which user to plot", days="How many days (default 30)")
async def plot(interaction: discord.Interaction, user: Optional[discord.Member] = None, days: Optional[int] = 30):
    if not await safe_defer(interaction):
        return
    try:
        rows = fetch_scores(days_back=days or 30, user_id=user.id if user else None)
        if not rows:
            await interaction.followup.send("No scores found.", ephemeral=True)
            return

        series = {}
        for uid, uname, day, score, solved, _ in rows:
            series.setdefault((uid, uname), []).append((day, 7 if solved == 0 else score))

        plt.figure()
        for (uid, uname), pts in series.items():
            pts.sort()
            xs, ys = zip(*pts)
            plt.plot(xs, ys, marker="o", label=uname)
        plt.gca().invert_yaxis()
        plt.xlabel("Wordle Day #")
        plt.ylabel("Guesses (X=7)")
        plt.title(f"Scores — {days} days" + (f" — {user.display_name}" if user else ""))
        plt.legend()
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=180)
        buf.seek(0)
        await interaction.followup.send(file=discord.File(buf, "scores.png"), ephemeral=True)
        plt.close()
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating plot.", ephemeral=True)

@tree.command(description="Summary stats for a user or everyone.")
@app_commands.describe(user="User to summarize", days="How many days (default 30)")
async def stats(interaction: discord.Interaction, user: Optional[discord.Member] = None, days: Optional[int] = 30):
    if not await safe_defer(interaction):
        return
    try:
        rows = fetch_scores(days_back=days or 30, user_id=user.id if user else None)
        if not rows:
            await interaction.followup.send("No scores found.", ephemeral=True)
            return

        from statistics import mean, median
        out = [f"**Stats last {days} days:**"]
        grouped = {}
        for uid, uname, _, score, solved, _ in rows:
            grouped.setdefault((uid, uname), []).append(score if solved else None)
        for (uid, uname), scores in grouped.items():
            solved_scores = [s for s in scores if s is not None]
            misses = sum(1 for s in scores if s is None)
            name = f"<@{uid}>" if uid else (uname or "Unknown")
            if solved_scores:
                out.append(f"{name}: {len(scores)} games • avg {mean(solved_scores):.2f} • median {median(solved_scores):.2f} • X {misses}")
            else:
                out.append(f"{name}: {len(scores)} games • all X")
        await interaction.followup.send("\n".join(out), ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error computing stats.", ephemeral=True)

@tree.command(description="Re-parse last N messages.")
@app_commands.describe(limit="How many messages (default 500)")
async def rescan(interaction: discord.Interaction, limit: Optional[int] = 500):
    if not await safe_defer(interaction):
        return
    try:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Run in a text channel.", ephemeral=True)
            return

        parsed = 0
        async for msg in channel.history(limit=limit or 500, oldest_first=True):
            if bot.user and msg.author.id == bot.user.id:
                continue
            if not message_in_scope(msg):
                continue

            if (WORDLE_BOT_ID and msg.author.id == WORDLE_BOT_ID) or \
               (not WORDLE_BOT_ID and msg.author.bot):
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

        await interaction.followup.send(f"Rescan complete. Parsed {parsed}.", ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error during rescan.", ephemeral=True)


# ========= Entrypoint =========
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN in environment.")
    bot.run(TOKEN)
