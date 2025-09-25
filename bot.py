# bot.py
import os
import io
import re
import sys
import sqlite3
import datetime as dt
import traceback
from contextlib import closing
from dataclasses import dataclass
from typing import Optional, List, Tuple

# ---- discord / pyplot setup -------------------------------------------------
import discord
from discord import app_commands
from discord.ext import commands

# dotenv is optional on Render; useful locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Headless plotting on servers
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---- Config -----------------------------------------------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WORDLE_CHANNEL_ID = int(os.getenv("WORDLE_CHANNEL_ID") or "0")   # optional
WORDLE_BOT_ID = int(os.getenv("WORDLE_BOT_ID") or "0")           # recommended
DB_PATH = os.getenv("DB_PATH") or "wordle_scores.db"

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

# ---- DB ---------------------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id     INTEGER NOT NULL,
            username    TEXT NOT NULL,
            day         INTEGER NOT NULL,
            score       INTEGER,           -- 1..6, NULL if X
            solved      INTEGER NOT NULL,  -- 1 if solved, 0 if X
            ts          TEXT NOT NULL,
            PRIMARY KEY (user_id, day)
        );
        """)

def upsert_score(user_id: int, username: str, day: int, score: Optional[int], solved: bool):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO scores (user_id, username, day, score, solved, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET
                username=excluded.username,
                score=excluded.score,
                solved=excluded.solved,
                ts=excluded.ts;
        """, (user_id, username, day,
              None if score is None else score,
              1 if solved else 0,
              dt.datetime.utcnow().isoformat()))

def fetch_scores(days_back: int, user_id: Optional[int] = None) -> List[Tuple]:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT MAX(day) FROM scores;").fetchone()
        max_day = row[0] if row and row[0] is not None else 0
        min_day = max(0, max_day - (days_back or 30) + 1)
        if user_id:
            cur = con.execute("""
                SELECT user_id, username, day, score, solved, ts
                  FROM scores
                 WHERE day BETWEEN ? AND ? AND user_id = ?
                 ORDER BY day ASC;
            """, (min_day, max_day, user_id))
        else:
            cur = con.execute("""
                SELECT user_id, username, day, score, solved, ts
                  FROM scores
                 WHERE day BETWEEN ? AND ?
                 ORDER BY user_id, day ASC;
            """, (min_day, max_day))
        return list(cur.fetchall())

def fetch_leaderboard(days_back: int, min_games: int = 5) -> List[Tuple]:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT MAX(day) FROM scores;").fetchone()
        max_day = row[0] if row and row[0] is not None else 0
        min_day = max(0, max_day - (days_back or 30) + 1)
        cur = con.execute("""
            SELECT user_id, MAX(username),
                   AVG(CASE WHEN solved=1 THEN score ELSE 7 END) AS avg_score,
                   SUM(CASE WHEN solved=1 THEN 1 ELSE 0 END) AS solves,
                   SUM(CASE WHEN solved=0 THEN 1 ELSE 0 END) AS misses,
                   COUNT(*) AS games
              FROM scores
             WHERE day BETWEEN ? AND ?
             GROUP BY user_id
            HAVING games >= ?
             ORDER BY avg_score ASC, solves DESC;
        """, (min_day, max_day, min_games))
        return list(cur.fetchall())

# ---- Data model used by parsers (define BEFORE using it) --------------------
@dataclass
class ParsedScore:
    user_id: Optional[int]
    username: str
    day: int
    score: Optional[int]   # None => X
    solved: bool

# ---- Parsing for your group's daily summary ---------------------------------
# Example lines:
#   "👑 2/6: @Where Jon Al Gaib"
#   "3/6: <@2866475...> <@1296962...> ..."
DAY_RE_TEXT = re.compile(r"\bWordle\s+(?:No\.?\s*)?(?P<day>\d+)\b", re.IGNORECASE)
SCORE_LINE_RE = re.compile(r"^(?:\*\*)?(?:👑\s*)?(?P<score>[Xx]|\d)\/6:\s*(?P<rest>.+)$")

def _extract_day_from_message(msg: discord.Message) -> Optional[int]:
    # Text
    m = DAY_RE_TEXT.search(msg.content or "")
    if m:
        return int(m.group("day"))
    # Embeds
    for emb in msg.embeds:
        if emb.title:
            m = DAY_RE_TEXT.search(emb.title)
            if m:
                return int(m.group("day"))
        if emb.description:
            m = DAY_RE_TEXT.search(emb.description)
            if m:
                return int(m.group("day"))
    return None

def parse_group_summary_style(msg: discord.Message) -> List[ParsedScore]:
    """
    Parses lines like:
      '👑 2/6: @Where Jon Al Gaib'
      '3/6: <@2866> <@1296> <@2680> <@3827>'
      '4/6: <@1486>'
    """
    day = _extract_day_from_message(msg)
    if day is None:
        return []

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

        # Fallback: treat entire remainder as a single display name (handles '@Where Jon Al Gaib')
        username = rest
        if username.startswith("**") and username.endswith("**"):
            username = username[2:-2].strip()
        if username.startswith("@"):
            username = username[1:].strip()
        if username:
            results.append(ParsedScore(user_id=0, username=username, day=day,
                                       score=score_val, solved=solved))
    return results

# ---- Scope helper -----------------------------------------------------------
def message_in_scope(msg: discord.Message) -> bool:
    return (not WORDLE_CHANNEL_ID) or (msg.channel.id == WORDLE_CHANNEL_ID)

# ---- Bot --------------------------------------------------------------------
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

@bot.event
async def on_ready():
    init_db()
    try:
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Slash sync error:", repr(e))
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if bot.user and message.author.id == bot.user.id:
        return
    if not message_in_scope(message):
        return

    # Parse the Wordle bot's daily summary
    try:
        if (WORDLE_BOT_ID and message.author.id == WORDLE_BOT_ID) or \
           (not WORDLE_BOT_ID and message.author.bot):
            for p in parse_group_summary_style(message):
                upsert_score(p.user_id or 0, p.username, p.day, p.score, p.solved)
    except Exception:
        print("Error parsing group summary:")
        traceback.print_exc()

    # Also capture human shares like "Wordle 1558 3/6"
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
        print("Error parsing human share:")
        traceback.print_exc()

    await bot.process_commands(message)

# ---- Global error handler for slash commands --------------------------------
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    print("Slash command error:", file=sys.stderr)
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        msg = "Sorry — that command hit an error. It’s been logged."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ---- Slash Commands ---------------------------------------------------------
@tree.command(description="Ping (quick test)")
async def ping(interaction: discord.Interaction):
    if not interaction.response.is_done():
        await interaction.response.send_message("Pong!", ephemeral=True)

@tree.command(description="Show leaderboard over recent days (default 30).")
@app_commands.describe(days="How many recent days to include (default 30).")
async def leaderboard(interaction: discord.Interaction, days: Optional[int] = 30):
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        rows = fetch_leaderboard(days_back=days or 30)
        if not rows:
            await interaction.followup.send(f"No data yet for the last {days} day(s).", ephemeral=True)
            return
        lines = [f"**Leaderboard (last {days} day(s))**\n_Min 5 games to rank_"]
        rank = 1
        for user_id, username, avg_score, solves, misses, games in rows:
            name = f"<@{user_id}>" if user_id else (username or "Unknown")
            lines.append(f"**#{rank}** {name} — avg **{avg_score:.2f}** "
                         f"(solves {solves}, X {misses}, games {games})")
            rank += 1
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating leaderboard.", ephemeral=True)

@tree.command(description="Plot scores over time for a user or everyone.")
@app_commands.describe(user="Which user to plot (leave blank for everyone).",
                       days="How many recent days (default 30).")
async def plot(interaction: discord.Interaction, user: Optional[discord.Member] = None,
               days: Optional[int] = 30):
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        rows = fetch_scores(days_back=days or 30, user_id=user.id if user else None)
        if not rows:
            await interaction.followup.send("No scores found for that selection.", ephemeral=True)
            return

        # group -> series
        series = {}
        for uid, uname, day, score, solved, _ in rows:
            series.setdefault((uid, uname), []).append((day, 7 if solved == 0 else score))

        plt.figure()
        for (uid, uname), pts in series.items():
            pts.sort(key=lambda t: t[0])
            xs = [d for d, _ in pts]
            ys = [y for _, y in pts]
            plt.plot(xs, ys, marker="o", label=f"{uname or uid}")
        plt.gca().invert_yaxis()
        plt.xlabel("Wordle Day #")
        plt.ylabel("Guesses (X shown as 7)")
        plt.title(f"Wordle scores — last {days} day(s)" + (f" — {user.display_name}" if user else ""))
        plt.legend()
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=180)
        buf.seek(0)
        await interaction.followup.send(file=discord.File(buf, "wordle_scores.png"), ephemeral=True)
        plt.close()
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating plot.", ephemeral=True)

@tree.command(description="Summary stats for a user or everyone.")
@app_commands.describe(user="User to summarize (blank = everyone).",
                       days="How many recent days (default 30).")
async def stats(interaction: discord.Interaction, user: Optional[discord.Member] = None,
                days: Optional[int] = 30):
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        rows = fetch_scores(days_back=days or 30, user_id=user.id if user else None)
        if not rows:
            await interaction.followup.send("No scores found for that selection.", ephemeral=True)
            return

        from statistics import mean, median
        by_user = {}
        for uid, uname, day, score, solved, _ in rows:
            by_user.setdefault((uid, uname), []).append((score if solved == 1 else None))

        lines = [f"**Stats for last {days} day(s):**"]
        for (uid, uname), scores in by_user.items():
            solved_scores = [s for s in scores if s is not None]
            misses = sum(1 for s in scores if s is None)
            games = len(scores)
            name = f"<@{uid}>" if uid else (uname or "Unknown")
            if solved_scores:
                lines.append(
                    f"• {name}: games {games}, solves {len(solved_scores)}, X {misses}, "
                    f"avg {mean(solved_scores):.2f}, median {median(solved_scores):.2f}"
                )
            else:
                lines.append(f"• {name}: games {games}, solves 0, X {misses}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error computing stats.", ephemeral=True)

@tree.command(description="Re-parse the last N messages in this channel (admin).")
@app_commands.describe(limit="How many recent messages to rescan (default 500).")
async def rescan(interaction: discord.Interaction, limit: Optional[int] = 500):
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Run this in a text channel with history.", ephemeral=True)
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

        await interaction.followup.send(f"Rescan complete. Parsed {parsed} entries.", ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error during rescan.", ephemeral=True)

# ---- Entrypoint -------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN in environment.")
    bot.run(TOKEN)
