import discord
from discord.ext import commands, tasks
import sqlite3
import datetime
import os
from dotenv import load_dotenv

from zoneinfo import ZoneInfo


# ---------- CONFIG ----------
TZ = ZoneInfo("Europe/Paris")


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

HABITS = {
    "🚶‍♂️": "walk",
    "🪥": "teeth",
    "🍳": "cook"
}

CHECKIN_CHANNEL = "daily-checkin"
HABIT_LOG_CHANNEL = "habits-log"
WEEKLY_CHANNEL = "weekly-summary"
MONTHLY_CHANNEL = "monthly-summary"
TODO_CHANNEL = "todo"
DONE_CHANNEL = "done"

CHECKIN_HOUR = 6   # 08:00
CHECKIN_MINUTE = 49


RESET_HOUR = 23    # 23:00
# ----------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- DATABASE ----------
db = sqlite3.connect("life_os.db", check_same_thread=False)
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS habits (
    date TEXT,
    habit TEXT,
    completed INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT,
    status TEXT,
    created_at TEXT,
    completed_at TEXT
)
""")

db.commit()
# ----------------------------

def today():
    return datetime.date.today().isoformat()

def now():
    return datetime.datetime.now().isoformat()

# ---------- BOT EVENTS ----------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    daily_checkin.start()
    daily_reset.start()
    weekly_summary.start()
    monthly_summary.start()

# ---------- DAILY CHECKIN ----------
last_checkin_date = None

@tasks.loop(minutes=1)
async def daily_checkin():
    global last_checkin_date

    now = datetime.datetime.now(TZ)
    today = now.date()

    if (
        now.hour == CHECKIN_HOUR
        and now.minute == CHECKIN_MINUTE
        and last_checkin_date != today
    ):
        last_checkin_date = today

        channel = discord.utils.get(
            bot.get_all_channels(), name=CHECKIN_CHANNEL
        )
        if not channel:
            return

        msg = await channel.send(
            f"☀️ **Daily Check-in — {today}**\n\n"
            "React when completed:\n"
            "🚶‍♂️ Morning walk + water\n"
            "🪥 Brush teeth\n"
            "🍳 Cooked a meal today"
        )

        for emoji in HABITS:
            await msg.add_reaction(emoji)


# ---------- REACTION TRACKING ----------
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    if reaction.emoji not in HABITS:
        return

    if reaction.message.channel.name != CHECKIN_CHANNEL:
        return

    habit = HABITS[reaction.emoji]

    cursor.execute("""
    INSERT OR REPLACE INTO habits (date, habit, completed)
    VALUES (?, ?, 1)
    """, (today(), habit))
    db.commit()

    await log_habit(habit, True)

async def log_habit(habit, completed):
    channel = discord.utils.get(bot.get_all_channels(), name=HABIT_LOG_CHANNEL)
    if not channel:
        return

    streak = get_streak(habit)

    status = "✅" if completed else "❌"
    await channel.send(
        f"{status} **{today()}** — {habit}\n"
        f"🔥 Streak: {streak} days"
    )


async def delete_last_bot_message(channel: discord.TextChannel):
    async for message in channel.history(limit=10):
        if message.author == bot.user:
            await message.delete()
            break


def get_streak(habit):
    cursor.execute("""
    SELECT date FROM habits
    WHERE habit = ? AND completed = 1
    ORDER BY date DESC
    """, (habit,))
    rows = cursor.fetchall()

    streak = 0
    current = datetime.date.today()

    for row in rows:
        if row[0] == current.isoformat():
            streak += 1
            current -= datetime.timedelta(days=1)
        else:
            break
    return streak

# ---------- DAILY RESET ----------
@tasks.loop(minutes=1)
async def daily_reset():
    now_time = datetime.datetime.now()
    if now_time.hour == RESET_HOUR and now_time.minute == 0:
        for habit in HABITS.values():
            cursor.execute("""
            SELECT 1 FROM habits WHERE date = ? AND habit = ?
            """, (today(), habit))
            if not cursor.fetchone():
                cursor.execute("""
                INSERT INTO habits (date, habit, completed)
                VALUES (?, ?, 0)
                """, (today(), habit))
                db.commit()
                await log_habit(habit, False)

# ---------- WEEKLY SUMMARY ----------
@tasks.loop(hours=1)
async def weekly_summary():
    if datetime.date.today().weekday() == 6:  # Sunday
        channel = discord.utils.get(bot.get_all_channels(), name=WEEKLY_CHANNEL)
        if not channel:
            return

        start = datetime.date.today() - datetime.timedelta(days=6)
        report = "📊 **Weekly Habit Report**\n\n"

        for emoji, habit in HABITS.items():
            cursor.execute("""
            SELECT COUNT(*) FROM habits
            WHERE habit = ? AND completed = 1 AND date >= ?
            """, (habit, start.isoformat()))
            count = cursor.fetchone()[0]

            bar = "█" * count + "░" * (7 - count)

            report += f"{emoji} **{habit.capitalize()}**: {count} / 7  {bar}\n"

        channel = discord.utils.get(bot.get_all_channels(), name=WEEKLY_CHANNEL)
        if not channel:
            return

        await delete_last_bot_message(channel)

        await channel.send(report)

# ---------- MONTHLY SUMMARY ----------
@tasks.loop(hours=1)
async def monthly_summary():
    today_date = datetime.date.today()
    if today_date.day == 1:
        channel = discord.utils.get(bot.get_all_channels(), name=MONTHLY_CHANNEL)
        if not channel:
            return

        month_start = today_date.replace(day=1) - datetime.timedelta(days=1)
        month_start = month_start.replace(day=1)

        report = f"📅 **Monthly Habit Report — {today.strftime('%B')}**\n\n"

        for emoji, habit in HABITS.items():
            cursor.execute("""
            SELECT COUNT(*) FROM habits
            WHERE habit = ? AND completed = 1 AND date >= ?
            """, (habit, month_start.isoformat()))
            count = cursor.fetchone()[0]

            # Scale bar to max 10 blocks for readability
            filled = round((count / days_in_month) * 10) if days_in_month else 0
            bar = "█" * filled + "░" * (10 - filled)

            report += (
                f"{emoji} **{habit.capitalize()}**: "
                f"{count} / {days_in_month}  {bar}\n"
    )


        await delete_last_bot_message(channel)

        await channel.send(report)

# ---------- TODO SYSTEM ----------
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name == TODO_CHANNEL:
        cursor.execute("""
        INSERT INTO todos (content, status, created_at)
        VALUES (?, 'pending', ?)
        """, (message.content, now()))
        db.commit()
        await message.add_reaction("⏳")

    await bot.process_commands(message)

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    if reaction.message.channel.name == TODO_CHANNEL and reaction.emoji == "✅":
        cursor.execute("""
        UPDATE todos SET status = 'done', completed_at = ?
        WHERE content = ?
        """, (now(), reaction.message.content))
        db.commit()

        done_channel = discord.utils.get(bot.get_all_channels(), name=DONE_CHANNEL)
        await done_channel.send(f"✅ {reaction.message.content}")

# ---------- RUN ----------

print("NOW (Paris):", datetime.datetime.now(TZ))

bot.run(TOKEN)
