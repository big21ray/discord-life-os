import discord
from discord.ext import commands, tasks
import gspread
import datetime
import os
from dotenv import load_dotenv

from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import dateutil.parser
from dateutil.relativedelta import relativedelta
import re


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

CHECKIN_HOUR = 9   # 09:00
CHECKIN_MINUTE = 30

TODO_REMINDER_OFFSET_MIN = 00

CALENDAR_HOUR = 9  # 09:00 for daily calendar notifications
CALENDAR_MINUTE = 0

RESET_HOUR = 22    # 22:30 (10:30 PM)
RESET_MINUTE = 30

# Google Calendar IDs
PERSONAL_CALENDAR_ID = os.getenv("PERSONAL_CALENDAR_ID", "primary")
PROFESSIONAL_CALENDAR_ID = os.getenv("PROFESSIONAL_CALENDAR_ID")

CALENDAR_CHANNEL = "calendar-events"

# Google Calendar API
SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_SERVICE = None

# Google Sheets
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = os.getenv("GOOGLE_SHEETS_ID")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
SHEETS_CLIENT = None
HABITS_SHEET = None
TODOS_SHEET = None
# ----------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- GOOGLE SHEETS FUNCTIONS ----------
def init_google_sheets():
    """Initialize Google Sheets connection"""
    global SHEETS_CLIENT, HABITS_SHEET, TODOS_SHEET
    try:
        # Try to load from environment variable first (Railway)
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if creds_json:
            import json
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SHEETS_SCOPES)
        else:
            # Fall back to local file for development
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SHEETS_SCOPES)
        SHEETS_CLIENT = gspread.authorize(creds)
        
        # Open the spreadsheet
        spreadsheet = SHEETS_CLIENT.open_by_key(SHEET_ID)
        
        # Get or create worksheets
        try:
            HABITS_SHEET = spreadsheet.worksheet("habits")
        except gspread.exceptions.WorksheetNotFound:
            HABITS_SHEET = spreadsheet.add_worksheet(title="habits", rows=1000, cols=5)
            HABITS_SHEET.append_row(["date", "habit", "completed"])
        
        try:
            TODOS_SHEET = spreadsheet.worksheet("todos")
        except gspread.exceptions.WorksheetNotFound:
            TODOS_SHEET = spreadsheet.add_worksheet(title="todos", rows=1000, cols=10)
            TODOS_SHEET.append_row(["id", "content", "status", "created_at", "completed_at", "deadline", "type", "frequency", "next_due", "priority"])
        
        print("✅ Google Sheets initialized successfully")
        return True
    except Exception as e:
        print(f"❌ Error initializing Google Sheets: {e}")
        return False

def add_habit(date_str, habit, completed):
    """Add or update habit in Google Sheets"""
    try:
        # Check if habit exists for this date
        existing = HABITS_SHEET.findall(date_str)
        for cell in existing:
            row = HABITS_SHEET.row_values(cell.row)
            if len(row) > 1 and row[1] == habit:
                # Update existing
                HABITS_SHEET.update_cell(cell.row, 3, 1 if completed else 0)
                return
        
        # Add new habit
        HABITS_SHEET.append_row([date_str, habit, 1 if completed else 0])
    except Exception as e:
        print(f"Error adding habit: {e}")

def get_habit_streak(habit):
    """Get streak for a habit"""
    try:
        all_habits = HABITS_SHEET.get_all_values()
        streak = 0
        current = datetime.date.today()
        
        # Filter habits for this type and completed=1, reverse sort by date
        habit_records = [row for row in all_habits[1:] if len(row) > 1 and row[1] == habit and row[2] == '1']
        habit_records.sort(key=lambda x: x[0], reverse=True)
        
        for record in habit_records:
            if record[0] == current.isoformat():
                streak += 1
                current -= datetime.timedelta(days=1)
            else:
                break
        
        return streak
    except Exception as e:
        print(f"Error getting habit streak: {e}")
        return 0

def get_todos(status=None):
    """Get todos from Google Sheets"""
    try:
        all_todos = TODOS_SHEET.get_all_values()
        todos = []
        for row in all_todos[1:]:
            if len(row) < 6:
                continue
            if status and row[2] != status:
                continue
            todos.append({
                "id": row[0],
                "content": row[1],
                "status": row[2],
                "created_at": row[3],
                "completed_at": row[4] if len(row) > 4 else "",
                "deadline": row[5] if len(row) > 5 else ""
            })
        return todos
    except Exception as e:
        print(f"Error getting todos: {e}")
        return []

def add_todo(content, deadline=None, todo_type="one-time", frequency=None, priority="medium"):
    """Add todo to Google Sheets"""
    try:
        todo_id = len(TODOS_SHEET.get_all_values())
        now_str = datetime.datetime.now().isoformat()
        
        TODOS_SHEET.append_row([
            str(todo_id),
            content,
            "pending",
            now_str,
            "",  # completed_at
            deadline or "",
            todo_type,
            frequency or "",
            "",  # next_due
            priority
        ])
    except Exception as e:
        print(f"Error adding todo: {e}")

def update_todo_status(row_num, status, completed_at=None):
    """Update todo status in Google Sheets"""
    try:
        TODOS_SHEET.update_cell(row_num, 3, status)
        if completed_at:
            TODOS_SHEET.update_cell(row_num, 5, completed_at)
    except Exception as e:
        print(f"Error updating todo: {e}")

def today_str():
    return datetime.date.today().isoformat()

def now_str():
    return datetime.datetime.now().isoformat()

# ---------- GOOGLE CALENDAR FUNCTIONS ----------
def init_google_calendar():
    """Initialize Google Calendar API connection"""
    try:
        # Try to load from environment variable first (Railway)
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if creds_json:
            import json
            creds_dict = json.loads(creds_json)
            credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        else:
            # Fall back to local file for development
            credentials = Credentials.from_service_account_file(
                "credentials.json", scopes=SCOPES
            )
        service = build("calendar", "v3", credentials=credentials)
        return service
    except Exception as e:
        print(f"Error initializing Google Calendar: {e}")
        return None

def get_calendar_events(calendar_id, days_ahead=2):
    """Fetch calendar events for the next N days"""
    if not CALENDAR_SERVICE:
        return []
    
    try:
        now = datetime.datetime.now(TZ)
        start_time = now.isoformat()
        end_time = (now + datetime.timedelta(days=days_ahead)).isoformat()
        
        events_result = CALENDAR_SERVICE.events().list(
            calendarId=calendar_id,
            timeMin=start_time,
            timeMax=end_time,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        
        return events_result.get("items", [])
    except Exception as e:
        print(f"Error fetching calendar events: {e}")
        return []

def format_events_message(events, title):
    """Format calendar events into Discord message"""
    if not events:
        return f"{title}\n_No upcoming events_"
    
    message = f"{title}\n\n"
    for event in events:
        start = event.get("start", {})
        start_time = start.get("dateTime", start.get("date"))
        
        try:
            if "T" in start_time:
                dt = dateutil.parser.parse(start_time)
                time_str = dt.strftime("%a, %b %d at %H:%M")
            else:
                dt = dateutil.parser.parse(start_time)
                time_str = dt.strftime("%a, %b %d")
        except:
            time_str = start_time
        
        summary = event.get("summary", "Untitled event")
        message += f"📅 **{summary}** - {time_str}\n"
    
    return message

def parse_event_input(text):
    """Parse natural language event input like 'Tuesday 30th 2025 at 8:00 PM I have meeting'"""
    try:
        # Try to find time pattern (HH:MM or H:MM AM/PM)
        time_match = re.search(r'(\d{1,2}:\d{2}(?:\s*(?:AM|PM|am|pm))?)', text)
        time_str = None
        if time_match:
            time_str = time_match.group(1)
        
        # Extract date and event title
        # Common patterns: "Tuesday 30th 2025 at 8:00 PM I have..."
        date_match = re.search(r'((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday).*?(?:at\s*\d{1,2}:\d{2})?)', text, re.IGNORECASE)
        
        if date_match:
            date_part = date_match.group(1).strip()
            # Extract title (everything after the date/time)
            title_start = date_match.end()
            title = text[title_start:].strip()
            
            # Try to parse the full datetime
            try:
                dt = dateutil.parser.parse(date_part, fuzzy=True)
                # If only date was parsed, add time
                if time_str:
                    time_part = dateutil.parser.parse(time_str, fuzzy=True)
                    dt = dt.replace(hour=time_part.hour, minute=time_part.minute)
                return {
                    "datetime": dt,
                    "title": title if title else "Calendar Event",
                    "success": True
                }
            except:
                return {"success": False, "error": "Could not parse date"}
        else:
            return {"success": False, "error": "Could not find date in input"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def add_calendar_event(calendar_id, title, event_datetime):
    """Add event to Google Calendar"""
    if not CALENDAR_SERVICE:
        return {"success": False, "error": "Calendar service not initialized"}
    
    try:
        event = {
            "summary": title,
            "start": {
                "dateTime": event_datetime.isoformat(),
                "timeZone": "Europe/Paris"
            },
            "end": {
                "dateTime": (event_datetime + datetime.timedelta(hours=1)).isoformat(),
                "timeZone": "Europe/Paris"
            }
        }
        
        created_event = CALENDAR_SERVICE.events().insert(
            calendarId=calendar_id,
            body=event
        ).execute()
        
        return {"success": True, "event": created_event}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ----------------------------


# ---------- BOT EVENTS ----------
@bot.event
async def on_ready():
    global CALENDAR_SERVICE
    print(f"✅ Logged in as {bot.user}")
    
    # Initialize Google Sheets
    try:
        init_google_sheets()
    except Exception as e:
        print(f"⚠️ Google Sheets initialization failed: {e}")
    
    # Initialize Google Calendar API
    try:
        CALENDAR_SERVICE = init_google_calendar()
        print("✅ Google Calendar API initialized")
    except Exception as e:
        print(f"⚠️ Google Calendar API failed: {e}")
    
    daily_checkin.start()
    daily_reset.start()
    weekly_summary.start()
    monthly_summary.start()
    daily_calendar_notification.start()
    weekly_calendar_summary.start()

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

last_todo_ping_date = None

@tasks.loop(minutes=1)
async def daily_todo_reminder():
    global last_todo_ping_date

    now = datetime.datetime.now(TZ)
    today = now.date()

    target_time = (
        datetime.datetime
        .combine(today, datetime.time(CHECKIN_HOUR, CHECKIN_MINUTE), tzinfo=TZ)
        + datetime.timedelta(minutes=TODO_REMINDER_OFFSET_MIN)
    )

    if now >= target_time and last_todo_ping_date != today:
        last_todo_ping_date = today

        todos = get_todos(status="pending")
        
        if not todos:
            return

        channel = discord.utils.get(bot.get_all_channels(), name=TODO_CHANNEL)
        if not channel:
            return

        msg = "📝 **Today's Todos**\n\n"
        for todo in todos:
            if todo["deadline"]:
                msg += f"• {todo['content']} _(due {todo['deadline']})_\n"
            else:
                msg += f"• {todo['content']}\n"

        await channel.send(msg)


# ---------- REACTION TRACKING ----------
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    if reaction.emoji in HABITS and reaction.message.channel.name == CHECKIN_CHANNEL:
        habit = HABITS[reaction.emoji]
        add_habit(today_str(), habit, True)
        await log_habit(habit, True)
    
    if reaction.message.channel.name == TODO_CHANNEL and reaction.emoji == "✅":
        # Find and update the todo
        todos = get_todos()
        for idx, todo in enumerate(todos):
            if todo["content"] in reaction.message.content:
                update_todo_status(idx + 2, "done", now_str())  # +2 because row 1 is header
                break
        
        done_channel = discord.utils.get(
            bot.get_all_channels(), name=DONE_CHANNEL
        )
        await done_channel.send(f"✅ {reaction.message.content}")


async def log_habit(habit, completed):
    channel = discord.utils.get(bot.get_all_channels(), name=HABIT_LOG_CHANNEL)
    if not channel:
        return

    streak = get_habit_streak(habit)

    status = "✅" if completed else "❌"
    await channel.send(
        f"{status} **{today_str()}** — {habit}\n"
        f"🔥 Streak: {streak} days"
    )


async def delete_last_bot_message(channel: discord.TextChannel):
    async for message in channel.history(limit=10):
        if message.author == bot.user:
            await message.delete()
            break


# ---------- DAILY RESET ----------
@tasks.loop(minutes=1)
async def daily_reset():
    now_time = datetime.datetime.now(TZ)
    if now_time.hour == RESET_HOUR and now_time.minute == RESET_MINUTE:
        for habit in HABITS.values():
            # Check if habit already exists for today
            existing_habits = HABITS_SHEET.get_all_values()
            found = False
            for row in existing_habits[1:]:
                if len(row) > 1 and row[0] == today_str() and row[1] == habit:
                    found = True
                    break
            
            if not found:
                add_habit(today_str(), habit, False)
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

        all_habits = HABITS_SHEET.get_all_values()
        
        for emoji, habit in HABITS.items():
            count = 0
            for row in all_habits[1:]:
                if len(row) > 2 and row[1] == habit and row[2] == '1' and row[0] >= start.isoformat():
                    count += 1

            bar = "█" * count + "░" * (7 - count)
            report += f"{emoji} **{habit.capitalize()}**: {count} / 7  {bar}\n"

        channel = discord.utils.get(bot.get_all_channels(), name=WEEKLY_CHANNEL)
        if not channel:
            return

        await delete_last_bot_message(channel)
        await channel.send(report)

# ---------- MONTHLY SUMMARY ----------
last_calendar_date = None
last_weekly_calendar_date = None

@tasks.loop(minutes=1)
async def daily_calendar_notification():
    """Send calendar events for next 2 days at 9:00 AM"""
    global last_calendar_date
    
    now = datetime.datetime.now(TZ)
    today = now.date()
    
    if (
        now.hour == CALENDAR_HOUR
        and now.minute == CALENDAR_MINUTE
        and last_calendar_date != today
    ):
        last_calendar_date = today
        
        channel = discord.utils.get(bot.get_all_channels(), name=CALENDAR_CHANNEL)
        if not channel:
            return
        
        # Get events from both calendars
        personal_events = get_calendar_events(PERSONAL_CALENDAR_ID, days_ahead=2)
        professional_events = []
        if PROFESSIONAL_CALENDAR_ID:
            professional_events = get_calendar_events(PROFESSIONAL_CALENDAR_ID, days_ahead=2)
        
        # Format messages
        personal_msg = format_events_message(personal_events, "📅 **Personal Calendar - Next 2 Days**")
        
        await channel.send(personal_msg)
        
        if PROFESSIONAL_CALENDAR_ID and professional_events:
            professional_msg = format_events_message(professional_events, "💼 **Professional Calendar - Next 2 Days**")
            await channel.send(professional_msg)

@tasks.loop(hours=1)
async def weekly_calendar_summary():
    """Send full calendar for next 2 weeks every Monday at 9:00 AM"""
    global last_weekly_calendar_date
    
    now = datetime.datetime.now(TZ)
    today = now.date()
    
    # Monday = 0, check if today is Monday and time is 9:00 AM
    if (
        today.weekday() == 0  # Monday
        and now.hour == CALENDAR_HOUR
        and now.minute == CALENDAR_MINUTE
        and last_weekly_calendar_date != today
    ):
        last_weekly_calendar_date = today
        
        channel = discord.utils.get(bot.get_all_channels(), name=CALENDAR_CHANNEL)
        if not channel:
            return
        
        # Get events from both calendars for next 14 days
        personal_events = get_calendar_events(PERSONAL_CALENDAR_ID, days_ahead=14)
        professional_events = []
        if PROFESSIONAL_CALENDAR_ID:
            professional_events = get_calendar_events(PROFESSIONAL_CALENDAR_ID, days_ahead=14)
        
        personal_msg = format_events_message(personal_events, "📅 **Personal Calendar - Next 2 Weeks**")
        await channel.send(personal_msg)
        
        if PROFESSIONAL_CALENDAR_ID and professional_events:
            professional_msg = format_events_message(professional_events, "💼 **Professional Calendar - Next 2 Weeks**")
            await channel.send(professional_msg)

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
        
        # Calculate days in month
        if today_date.month == 12:
            month_end = today_date.replace(year=today_date.year + 1, month=1, day=1) - datetime.timedelta(days=1)
        else:
            month_end = today_date.replace(month=today_date.month + 1, day=1) - datetime.timedelta(days=1)
        days_in_month = month_end.day

        report = f"📅 **Monthly Habit Report — {today_date.strftime('%B')}**\n\n"

        all_habits = HABITS_SHEET.get_all_values()
        
        for emoji, habit in HABITS.items():
            count = 0
            for row in all_habits[1:]:
                if len(row) > 2 and row[1] == habit and row[2] == '1' and month_start.isoformat() <= row[0] <= month_end.isoformat():
                    count += 1

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
        # Parse deadline if present
        deadline = None
        content = message.content
        
        if "| due" in content.lower():
            content, due = content.split("|", 1)
            due = due.replace("due", "").strip()
            try:
                deadline = datetime.datetime.fromisoformat(due).date().isoformat()
            except:
                deadline = None
        
        content = content.strip()
        add_todo(content, deadline=deadline)
        await message.add_reaction("⏳")

    await bot.process_commands(message)

# ---------- COMMANDS ----------

@bot.command(name="addevent")
async def add_event(ctx, *, event_input):
    """
    Add event to calendar
    Usage: !addevent Tuesday 30th 2025 at 8:00 PM I have meeting
    Use 'personal' or 'professional' prefix to choose calendar
    """
    # Check if user specified which calendar
    calendar_choice = PERSONAL_CALENDAR_ID  # default
    if event_input.lower().startswith("professional"):
        if not PROFESSIONAL_CALENDAR_ID:
            await ctx.send("❌ Professional calendar not configured")
            return
        calendar_choice = PROFESSIONAL_CALENDAR_ID
        event_input = event_input[len("professional"):].strip()
    elif event_input.lower().startswith("personal"):
        event_input = event_input[len("personal"):].strip()
    
    # Parse the event input
    parsed = parse_event_input(event_input)
    
    if not parsed["success"]:
        await ctx.send(f"❌ Error parsing event: {parsed['error']}")
        return
    
    # Add to calendar
    result = add_calendar_event(
        calendar_choice,
        parsed["title"],
        parsed["datetime"]
    )
    
    if result["success"]:
        event_time = parsed["datetime"].strftime("%a, %b %d at %H:%M")
        await ctx.send(f"✅ Event added: **{parsed['title']}** on {event_time}")
    else:
        await ctx.send(f"❌ Error adding event: {result['error']}")

# ---------- RUN ----------

bot.run(TOKEN)
