from __future__ import annotations

import os, io, re, sys, sqlite3, datetime as dt, traceback
from contextlib import closing
from dataclasses import dataclass
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# load local .env if present (ignored on Render)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
WORDLE_CHANNEL_ID = int(os.getenv("WORDLE_CHANNEL_ID") or "0")
WORDLE_BOT_ID = int(os.getenv("WORDLE_BOT_ID") or "0")
DB_PATH = os.getenv("DB_PATH") or "wordle_scores.db"

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

# ---------- DB ----------
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            user_id     INTEGER NOT NULL,
            username    TEXT NOT NULL,
            day         INTEGER NOT NULL,
            score       INTEGER,
            solved      INTEGER NOT NULL,
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

# ---------- Data model & parsing ----------
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
        if emb.title and (m := DAY_RE_TEXT.search(emb.title)):
            return int(m.group("day"))
        if emb.description and (m := DAY_RE_TEXT.search(emb.description)):
            return int(m.group("day"))
    return None

def parse_group_summary_style(msg: discord.Message) -> List[ParsedScore]:
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

        mention_ids = [int(mm.group("id")) for mm in re.finditer(r"<@!?(?P<id>\d+)>", rest)]
        if mention_ids:
            id_to_member = {mem.id: mem for mem in msg.mentions}
            for uid in mention_ids:
                member = id_to_member.get(uid)
                username = member.display_name if member else f"user:{uid}"
                results.append(ParsedScore(user_id=uid, username=username, day=day,
                                           score=score_val, solved=solved))
            continue

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

# ---------- Bot ----------
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True, thinking: bool = True) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except discord.NotFound:
        print("safe_defer: interaction expired (cold start)")
        return False
    except Exception as e:
        print("safe_defer unexpected:", repr(e))
        return False

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
        msg = "Sorry — that command hit an error."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ---------- Slash Commands ----------
@tree.command(description="Ping (quick test)")
async def ping(interaction: discord.Interaction):
    if not interaction.response.is_done():
        await interaction.response.send_message("Pong!", ephemeral=True)

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
            name = f"<@{uid}>" if uid else username
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
            name = f"<@{uid}>" if uid else uname
            if solved_scores:
                out.append(f"{name}: {len(scores)} games, avg {mean(solved_scores):.2f}, median {median(solved_scores):.2f}, X {misses}")
            else:
                out.append(f"{name}: {len(scores)} games, all X")
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
            m = re.search(r"\bWordle\s+(?P<day>\d+)\s+(?P<score>[Xx]|\d)\/6\b", msg.content or "", re.IGNORECASE)
            if m:
                day = int(m.group("day"))
                s = m.group("score")
                score_val = None if s.lower() == "x" else int(s)
                upsert_score(msg.author.id, msg.author.display_name, day, score_val, score_val is not None)
                parsed += 1
        await interaction.followup.send(f"Rescan complete. Parsed {parsed}.", ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error during rescan.", ephemeral=True)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_BOT_TOKEN in environment.")
    bot.run(TOKEN)
