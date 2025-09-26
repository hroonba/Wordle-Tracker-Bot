from __future__ import annotations

import os, io, re, sys, sqlite3, datetime as dt, traceback
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
GUILD_ID = os.getenv("GUILD_ID")  # string; we'll int() later if present
OWNER_ID = int(os.getenv("OWNER_ID") or "0")

WORDLE_CHANNEL_ID = int(os.getenv("WORDLE_CHANNEL_ID") or "0")   # optional scope
WORDLE_BOT_ID = int(os.getenv("WORDLE_BOT_ID") or "0")           # recommended
DB_PATH = os.getenv("DB_PATH") or "wordle_scores.db"

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True  # required for nickname resolution


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

def init_aliases_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS aliases (
            name_norm  TEXT PRIMARY KEY,   -- normalize_username(name)
            user_id    INTEGER NOT NULL
        );
        """)

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

def fetch_scores(days_back: Optional[int] = None, user_id: Optional[int] = None) -> List[Tuple]:
    """
    Return rows in the last `days_back` days using ts (ISO); if days_back is None,
    return the entire history.
    """
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
            cutoff = (dt.datetime.utcnow() - dt.timedelta(days=(days_back or 1) - 1)).isoformat()
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
    """
    Leaderboard over a time window (None => entire history) using Python-side
    grouping by identity. Returns: (key, label, avg, solves, misses, games)
    """
    with sqlite3.connect(DB_PATH) as con:
        if days_back is None:
            rows = list(con.execute("""
                SELECT user_id, username, score, solved, ts
                  FROM scores
                 ORDER BY ts ASC;
            """))
        else:
            cutoff = (dt.datetime.utcnow() - dt.timedelta(days=(days_back or 1) - 1)).isoformat()
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

    out.sort(key=lambda t: (t[2], -t[3]))  # avg asc, solves desc
    return out


# ========= Identity & alias helpers =========
def normalize_username(s: str) -> str:
    """Lower-case, strip spaces & leading '@' for consistent grouping."""
    s = (s or "").strip()
    if s.startswith("@"):
        s = s[1:]
    return s.strip().lower()

def alias_lookup(name: str) -> int:
    """Return mapped user_id for a normalized display name if one exists; else 0."""
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
    """
    Retroactively migrate rows where user_id==0 and normalized username matches 'name'
    to the provided user_id/display_name.
    """
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
    """Prefer real user_id; otherwise try alias; then fall back to normalized name."""
    if user_id:
        return (user_id, "")
    mapped = alias_lookup(username)
    if mapped:
        return (mapped, "")
    return (0, normalize_username(username))

def identity_label(user_id: int, username: str) -> str:
    """Label prefers real user mention; else alias mention; else raw name."""
    if user_id:
        return f"<@{user_id}>"
    mapped = alias_lookup(username)
    if mapped:
        return f"<@{mapped}>"
    return username or "Unknown"

def label_plain_for_hist(guild: discord.Guild | None, user_id: int, username: str) -> str:
    """
    A human-friendly label for plots: prefer a display name string (not a mention).
    """
    if user_id:
        # Best effort: current display name if we can see the member
        if guild:
            m = guild.get_member(user_id)
            if m and m.display_name:
                return m.display_name
        return username or f"user:{user_id}"
    # If alias maps to an ID, try to resolve to a name
    mapped = alias_lookup(username)
    if mapped:
        if guild:
            m = guild.get_member(mapped)
            if m and m.display_name:
                return m.display_name
        return username or f"user:{mapped}"
    return username or "Unknown"

def resolve_name_to_member(guild: discord.Guild | None, name: str) -> tuple[int, str]:
    """
    Resolve a plain '@Display Name' to (user_id, display_name) using guild members.
    If not found, return (0, original_name). Alias mapping will also catch it later.
    """
    if not guild:
        return 0, name
    target = normalize_username(name)
    for m in guild.members:
        if normalize_username(m.display_name) == target:
            return m.id, m.display_name
    # Try global name fallback
    for m in guild.members:
        if normalize_username(getattr(m, "global_name", "") or "") == target:
            return m.id, m.display_name
    mapped = alias_lookup(name)
    if mapped:
        for m in guild.members:
            if m.id == mapped:
                return m.id, m.display_name
        return mapped, name
    return 0, name


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
    Handles lines like:
      '👑 2/6: @Where Jon Al Gaib'
      '3/6: <@2866> <@1296> <@2680> <@3827>'
      '4/6: @Name One @Name Two'   (no real mentions)
    If Wordle number is missing, infer "yesterday UTC" as YYYYMMDD.
    """
    day = _extract_day_from_message(msg)
    if day is None:
        utc_dt = msg.created_at
        d = (utc_dt - dt.timedelta(days=1)).date()
        day = d.year * 10000 + d.month * 100 + d.day

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

        # 1) Real mentions first
        mention_ids = [int(mm.group("id")) for mm in re.finditer(r"<@!?(?P<id>\d+)>", rest)]
        if mention_ids:
            id_to_member = {mem.id: mem for mem in msg.mentions}
            for uid in mention_ids:
                member = id_to_member.get(uid)
                username = member.display_name if member else f"user:{uid}"
                results.append(ParsedScore(user_id=uid, username=username, day=day,
                                           score=score_val, solved=solved))
            continue

        # 2) Multiple plain @Display Name chunks
        if "@" in rest:
            names = [t.strip() for t in rest.split("@") if t.strip()]
        else:
            names = [rest]  # rare fallback

        for name in names:
            uid, display = resolve_name_to_member(msg.guild, name)
            results.append(ParsedScore(user_id=uid, username=display, day=day,
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
    init_aliases_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            print(f"Slash commands synced to guild {GUILD_ID}: {len(synced)}")
        else:
            synced = await tree.sync()
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

# ---- Alias management
@tree.command(description="Admin: add an alias mapping (old nickname -> @user)")
@app_commands.describe(name="The old display name as it appears in summaries", user="The real user to map to")
async def alias_add(interaction: discord.Interaction, name: str, user: discord.Member):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not await safe_defer(interaction): return
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
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if not await safe_defer(interaction): return
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
    if not await safe_defer(interaction): return
    try:
        items = alias_list()
        if not items:
            await interaction.followup.send("No aliases.", ephemeral=True)
            return
        lines = ["**Aliases:**"]
        for name_norm, uid in items:
            # best-effort label
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

# ---- Leaderboard (defaults to entire history)
@tree.command(description="Show leaderboard for the entire history (or last N days).")
@app_commands.describe(days="Optional: limit to last N days. Omit for entire history.")
async def leaderboard(interaction: discord.Interaction, days: Optional[int] = None):
    if not await safe_defer(interaction):
        return
    try:
        rows = fetch_leaderboard(days_back=days)
        if not rows:
            await interaction.followup.send("No data yet.", ephemeral=True)
            return
        title = "entire history" if days is None else f"last {days} days"
        lines = [f"**Leaderboard ({title})**\n_Min 5 games to rank_"]
        for rank, (_key, label, avg, solves, misses, games) in enumerate(rows, 1):
            lines.append(f"#{rank} {label}: avg {avg:.2f} (solves {solves}, X {misses}, games {games})")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating leaderboard.", ephemeral=True)

# ---- Plot (histogram) — defaults to entire history
@tree.command(name="plot", description="Histogram of guess counts per user (ranked by avg).")
@app_commands.describe(days="Optional: limit to last N days (omit for entire history).",
                       top_n="Show top N users by average (default 6)")
async def plot_histogram(interaction: discord.Interaction, days: Optional[int] = None, top_n: Optional[int] = 6):
    if not await safe_defer(interaction): return
    try:
        rows = fetch_scores(days_back=days)
        if not rows:
            await interaction.followup.send("No scores found.", ephemeral=True)
            return

        # Aggregate per identity
        per_user: Dict[tuple, Dict] = {}
        for uid, uname, _day, score, solved, _ts in rows:
            key = identity_key(uid or 0, uname or "")
            # label for legend should be a human name, not <@id>
            label = label_plain_for_hist(interaction.guild, uid or 0, uname or "Unknown")
            entry = per_user.setdefault(key, {"label": label, "counts": {i:0 for i in range(1,8)}})
            s = (score if solved == 1 else 7)
            entry["counts"][s] += 1

        # Rank by average with X=7
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
        ranked = ranked[: (top_n or 6)]

        # Build grouped bar chart
        guesses = list(range(1, 8))  # 1..6 + 7=X
        width = 0.8 / max(1, len(ranked))
        centers = list(range(len(guesses)))

        plt.figure(figsize=(9.5, 6))  # wider canvas
        for i, (avg, key, d) in enumerate(ranked):
            counts = [d["counts"].get(g, 0) for g in guesses]
            xs = [c + (i - (len(ranked)-1)/2)*width for c in centers]
            plt.bar(xs, counts, width=width, label=f"{d['label']} (avg {avg:.2f})")

        plt.xticks(centers, [1,2,3,4,5,6,"X"])
        plt.xlabel("Guesses (X = fail)")
        plt.ylabel("Count")
        title_scope = "entire history" if days is None else f"last {days} days"
        plt.title(f"Guess histogram — {title_scope} (top {len(ranked)} by avg)")
        # Put legend below the plot, allow it to span multiple columns
        ncols = max(2, min(len(ranked), 4))
        plt.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=ncols)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=180, bbox_inches="tight")
        buf.seek(0)
        await interaction.followup.send(file=discord.File(buf, "hist.png"), ephemeral=True)
        plt.close()
    except Exception:
        traceback.print_exc()
        await interaction.followup.send("Error generating histogram.", ephemeral=True)

# ---- Rescan/backfill
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
               (not WORDLE_BOT_ID and message.author.bot):
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
