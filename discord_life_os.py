import discord
from discord.ext import commands, tasks
import gspread
import sqlite3
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

# HABITS will be loaded from Google Sheets (initialized later)
HABITS = {}  # Format: {emoji: habit_name}

CHECKIN_CHANNEL = "daily-checkin"
HABIT_LOG_CHANNEL = "habits-log"
WEEKLY_CHANNEL = "weekly-summary"
MONTHLY_CHANNEL = "monthly-summary"
TODO_CHANNEL = "todo"
DONE_CHANNEL = "done"

CHECKIN_HOUR = 9   # 09:00
CHECKIN_MINUTE = 30

TODO_REMINDER_OFFSET_MIN = 00

CALENDAR_HOUR = 9  # 09:30 for daily calendar notifications
CALENDAR_MINUTE = 30

RESET_HOUR = 22    # 22:30 (10:30 PM)
RESET_MINUTE = 30

# Google Calendar IDs
PERSONAL_CALENDAR_ID = os.getenv("PERSONAL_CALENDAR_ID", "primary")
PROFESSIONAL_CALENDAR_ID = os.getenv("PROFESSIONAL_CALENDAR_ID")
COMPETITIVE_CALENDAR_ID = os.getenv("COMPETITIVE_CALENDAR_ID", "060331564c64fcccb5bf016ca0083db1c76ac904a118b97ca5bb5676beb2ce89@group.calendar.google.com")

CALENDAR_CHANNEL = "calendar-events"
CALENDAR_CHANNEL_ID = 1452167083402985563

# Discord channel where professional commands (like !add_scrim) should be used
PROFESSIONAL_CHANNEL_ID = int(os.getenv("PROFESSIONAL_CHANNEL_ID", "1455622941705507008"))

# Project Management Configuration
PROJECTS = {
    "project1": {
        "name": "Project 1",
        "channel_id": 1457103401560178993,
    },
    "project2": {
        "name": "Project 2",
        "channel_id": 1457103590043816250,
    },
    "project3": {
        "name": "Project 3",
        "channel_id": 1457103753936113694,
    },
    "project4": {
        "name": "Project 4",
        "channel_id": 1457104352228675859,
    },
}

# Initialize SQLite for projects/tickets
projects_db = sqlite3.connect("projects.db", check_same_thread=False)
projects_cursor = projects_db.cursor()

# Create tables
projects_cursor.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    title TEXT,
    status TEXT,
    created_at TEXT,
    completed_at TEXT
)
""")
projects_db.commit()


def get_calendar_channel():
    """Resolve the calendar channel.

    Prefer explicit channel ID (more reliable across guilds and renames),
    fallback to channel name.
    """
    if CALENDAR_CHANNEL_ID:
        try:
            channel = bot.get_channel(int(CALENDAR_CHANNEL_ID))
            if channel:
                return channel
        except Exception:
            pass
    return discord.utils.get(bot.get_all_channels(), name=CALENDAR_CHANNEL)

# Google Calendar API
SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_SERVICE = None

# Google Sheets
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = os.getenv("GOOGLE_SHEETS_ID")
SHEETS_CLIENT = None
HABITS_SHEET = None
TODOS_SHEET = None
HABITS_CONFIG_SHEET = None
TICKETS_SHEET = None

# Event reminder tracking (in-memory, resets on bot restart)
# (kept later near the reminder task implementation)

# Todo message tracking: maps message_id -> todo_row_number (for individual todo reactions)
todo_message_map = {}

# ----------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- GOOGLE SHEETS FUNCTIONS ----------
def init_google_sheets():
    """Initialize Google Sheets connection"""
    global SHEETS_CLIENT, HABITS_SHEET, TODOS_SHEET, HABITS_CONFIG_SHEET, TICKETS_SHEET, HABITS
    try:
        # Try to load from environment variable first (Railway)
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if creds_json:
            import json
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SHEETS_SCOPES)
        elif os.path.exists("google_credentials.json"):
            # Fall back to local file for development
            creds = Credentials.from_service_account_file("google_credentials.json", scopes=SHEETS_SCOPES)
        else:
            raise FileNotFoundError(
                "❌ GOOGLE_CREDENTIALS not set and google_credentials.json not found. "
                "Please add GOOGLE_CREDENTIALS to your environment variables or place google_credentials.json in the project root."
            )
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
            TODOS_SHEET = spreadsheet.add_worksheet(title="todos", rows=1000, cols=12)
            TODOS_SHEET.append_row(["id", "content", "status", "completed", "created_at", "completed_at", "deadline", "type", "frequency", "next_due", "priority", "tags"])
        
        try:
            HABITS_CONFIG_SHEET = spreadsheet.worksheet("habits_config")
        except gspread.exceptions.WorksheetNotFound:
            HABITS_CONFIG_SHEET = spreadsheet.add_worksheet(title="habits_config", rows=100, cols=2)
            HABITS_CONFIG_SHEET.append_row(["emoji", "name"])
            # Add default habits
            HABITS_CONFIG_SHEET.append_row(["🚶‍♂️", "walk"])
            HABITS_CONFIG_SHEET.append_row(["🪥", "teeth"])
            HABITS_CONFIG_SHEET.append_row(["🍳", "cook"])
        
        try:
            TICKETS_SHEET = spreadsheet.worksheet("tickets")
        except gspread.exceptions.WorksheetNotFound:
            TICKETS_SHEET = spreadsheet.add_worksheet(title="tickets", rows=1000, cols=6)
            TICKETS_SHEET.append_row(["id", "project_id", "title", "status", "created_at", "completed_at"])
        
        # Load habits from config
        load_habits_from_config()
        
        print("✅ Google Sheets initialized successfully")
        return True
    except Exception as e:
        print(f"❌ Error initializing Google Sheets: {e}")
        return False

def load_habits_from_config():
    """Load habits from HABITS_CONFIG sheet into HABITS dictionary"""
    global HABITS, HABITS_CONFIG_SHEET
    HABITS = {}
    try:
        all_rows = HABITS_CONFIG_SHEET.get_all_values()
        # Skip header row
        for row in all_rows[1:]:
            if len(row) >= 2 and row[0] and row[1]:
                emoji, name = row[0], row[1]
                HABITS[emoji] = name
        print(f"✅ Loaded {len(HABITS)} habits from config")
    except Exception as e:
        print(f"❌ Error loading habits config: {e}")

def add_habit_to_config(emoji, name):
    """Add a new habit to the config"""
    global HABITS, HABITS_CONFIG_SHEET
    try:
        # Check if emoji already exists
        all_rows = HABITS_CONFIG_SHEET.get_all_values()
        for row in all_rows[1:]:
            if len(row) >= 1 and row[0] == emoji:
                return {"success": False, "error": f"Emoji {emoji} already exists"}
        
        # Add the new habit
        HABITS_CONFIG_SHEET.append_row([emoji, name])
        HABITS[emoji] = name
        return {"success": True, "emoji": emoji, "name": name}
    except Exception as e:
        return {"success": False, "error": str(e)}

def remove_habit_from_config(emoji):
    """Remove a habit from the config"""
    global HABITS, HABITS_CONFIG_SHEET
    try:
        # Find and delete the row
        all_rows = HABITS_CONFIG_SHEET.get_all_values()
        for idx, row in enumerate(all_rows):
            if len(row) >= 1 and row[0] == emoji:
                # Use batch_delete to remove the row (gspread doesn't have delete_row)
                HABITS_CONFIG_SHEET.delete_rows(idx + 1, idx + 1)  # Delete row at position idx+1
                if emoji in HABITS:
                    del HABITS[emoji]
                return {"success": True, "emoji": emoji}
        
        return {"success": False, "error": f"Emoji {emoji} not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}

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

def get_todos(status=None, tag=None):
    """Get todos from Google Sheets, optionally filtered by status or tag"""
    try:
        all_todos = TODOS_SHEET.get_all_values()
        todos = []
        for row in all_todos[1:]:
            if len(row) < 6:
                continue
            if status and row[2] != status:
                continue
            
            # Parse tags
            tags_str = row[10] if len(row) > 10 else ""
            tags = [t.strip() for t in tags_str.split(",") if t.strip()]
            
            # Filter by tag if specified
            if tag and tag not in tags:
                continue
            
            todos.append({
                "id": row[0],
                "content": row[1],
                "status": row[2],
                "created_at": row[3],
                "completed_at": row[4] if len(row) > 4 else "",
                "deadline": row[5] if len(row) > 5 else "",
                "type": row[6] if len(row) > 6 else "one-time",
                "frequency": row[7] if len(row) > 7 else "",
                "next_due": row[8] if len(row) > 8 else "",
                "priority": row[9] if len(row) > 9 else "medium",
                "tags": tags
            })
        return todos
    except Exception as e:
        print(f"Error getting todos: {e}")
        return []

def add_todo(content, deadline=None, todo_type="one-time", frequency=None, priority="medium", tags=None):
    """Add todo to Google Sheets"""
    try:
        todo_id = len(TODOS_SHEET.get_all_values())
        now_str = datetime.datetime.now().isoformat()
        tags_str = ",".join(tags) if tags else ""
        
        TODOS_SHEET.append_row([
            str(todo_id),
            content,
            "pending",
            "0",  # completed (0 = not done, 1 = done)
            now_str,  # created_at
            "",  # completed_at
            deadline or "",
            todo_type,
            frequency or "",
            "",  # next_due
            priority,
            tags_str
        ])
    except Exception as e:
        print(f"Error adding todo: {e}")

def update_todo_status(row_num, status, completed_at=None):
    """Update todo status in Google Sheets"""
    try:
        TODOS_SHEET.update_cell(row_num, 3, status)  # status column
        
        # Update completed flag (0 or 1)
        if status == "done":
            TODOS_SHEET.update_cell(row_num, 4, "1")  # completed = 1
            if completed_at:
                TODOS_SHEET.update_cell(row_num, 6, completed_at)  # completed_at column
        else:
            TODOS_SHEET.update_cell(row_num, 4, "0")  # completed = 0
    except Exception as e:
        print(f"Error updating todo: {e}")

def today_str():
    return datetime.date.today().isoformat()

def now_str():
    return datetime.datetime.now().isoformat()

# ---------- NATURAL LANGUAGE PARSER ----------
def parse_todo_input(text):
    """
    Parse todo input with patterns like:
    - "Book Scrims every-tuesday-21:00 tag:pro" → recurring weekly at 21:00 with pro tag
    - "Do taxes in-2-weeks tag:perso" → one-time in 2 weeks with perso tag
    - "Dentist appointment deadline:2025-02-28 tag:perso" → with deadline and tag
    - "Daily exercise every-day tag:health" → daily recurring with custom tag
    - Multiple tags: "task tag:pro tag:urgent"
    """
    import re
    
    result = {
        "content": text.strip(),
        "type": "one-time",
        "frequency": None,
        "next_due": None,
        "deadline": None,
        "priority": "medium",
        "tags": []
    }
    
    # Extract all tags: tag:word (case-insensitive)
    tag_matches = re.findall(r'tag:(\w+)', text, re.IGNORECASE)
    if tag_matches:
        result["tags"] = [tag.lower() for tag in tag_matches]
    
    # Pattern 1: every-[day]-[time] (e.g., "every-tuesday-21:00")
    every_day_time = re.search(r'every-(\w+)-(\d{1,2}):(\d{2})', text, re.IGNORECASE)
    
    # Pattern 1: every-[day]-[time] (e.g., "every-tuesday-21:00")
    every_day_time = re.search(r'every-(\w+)-(\d{1,2}):(\d{2})', text, re.IGNORECASE)
    if every_day_time:
        day = every_day_time.group(1).lower()
        hour = int(every_day_time.group(2))
        minute = int(every_day_time.group(3))
        
        result["type"] = "recurring"
        result["frequency"] = f"every-{day.capitalize()}-{hour:02d}:{minute:02d}"
        
        # Calculate next occurrence
        days_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, 
                   "friday": 4, "saturday": 5, "sunday": 6}
        target_day = days_map.get(day, None)
        
        if target_day is not None:
            today = datetime.date.today()
            current_day = today.weekday()
            days_ahead = (target_day - current_day) % 7
            if days_ahead == 0:
                days_ahead = 7  # If today is the day, schedule for next week
            
            next_date = today + datetime.timedelta(days=days_ahead)
            next_datetime = datetime.datetime.combine(
                next_date, 
                datetime.time(hour, minute)
            )
            result["next_due"] = next_datetime.isoformat()
        
        # Remove pattern from content
        result["content"] = re.sub(r'\s*every-\w+-\d{1,2}:\d{2}', '', text, flags=re.IGNORECASE).strip()
        return result
    
    # Pattern 2: every-day or daily
    if re.search(r'every-day|daily', text, re.IGNORECASE):
        result["type"] = "recurring"
        result["frequency"] = "daily"
        result["next_due"] = datetime.date.today().isoformat()
        result["content"] = re.sub(r'every-day|daily', '', text, flags=re.IGNORECASE).strip()
        return result
    
    # Pattern 3: every-[number]-[unit] (e.g., "every-2-weeks", "every-3-days")
    every_interval = re.search(r'every-(\d+)-(\w+)', text, re.IGNORECASE)
    if every_interval:
        number = int(every_interval.group(1))
        unit = every_interval.group(2).lower()
        
        result["type"] = "recurring"
        result["frequency"] = f"every-{number}-{unit}"
        
        # Calculate next due
        today = datetime.date.today()
        if unit in ["day", "days"]:
            next_date = today + datetime.timedelta(days=number)
        elif unit in ["week", "weeks"]:
            next_date = today + datetime.timedelta(weeks=number)
        elif unit in ["month", "months"]:
            next_date = today + datetime.timedelta(days=30*number)
        else:
            next_date = today
        
        result["next_due"] = next_date.isoformat()
        result["content"] = re.sub(r'every-\d+-\w+', '', text, flags=re.IGNORECASE).strip()
        return result
    
    # Pattern 4: in-[number]-[unit] (e.g., "in-2-weeks", "in-3-days")
    in_future = re.search(r'in-(\d+)-(\w+)', text, re.IGNORECASE)
    if in_future:
        number = int(in_future.group(1))
        unit = in_future.group(2).lower()
        
        result["type"] = "future"
        today = datetime.date.today()
        
        if unit in ["day", "days"]:
            deadline_date = today + datetime.timedelta(days=number)
        elif unit in ["week", "weeks"]:
            deadline_date = today + datetime.timedelta(weeks=number)
        elif unit in ["month", "months"]:
            deadline_date = today + datetime.timedelta(days=30*number)
        else:
            deadline_date = today
        
        result["deadline"] = deadline_date.isoformat()
        result["next_due"] = deadline_date.isoformat()
        result["content"] = re.sub(r'in-\d+-\w+', '', text, flags=re.IGNORECASE).strip()
        return result
    
    # Pattern 5: deadline:[date] (e.g., "deadline:2025-02-28")
    deadline_match = re.search(r'deadline[:\s]+(\d{4}-\d{2}-\d{2})', text, re.IGNORECASE)
    if deadline_match:
        deadline_str = deadline_match.group(1)
        result["deadline"] = deadline_str
        result["content"] = re.sub(r'deadline[:\s]*\d{4}-\d{2}-\d{2}', '', text, flags=re.IGNORECASE).strip()
        return result
    
    # Pattern 6: priority:[high|medium|low]
    priority_match = re.search(r'priority[:=]\s*(high|medium|low)', text, re.IGNORECASE)
    if priority_match:
        result["priority"] = priority_match.group(1).lower()
        result["content"] = re.sub(r'priority[:=]\s*(high|medium|low)', '', text, flags=re.IGNORECASE).strip()
    
    # Remove all tag: patterns from content
    result["content"] = re.sub(r'\s*tag:\w+', '', result["content"], flags=re.IGNORECASE).strip()
    
    return result

def calculate_urgency_score(todo):
    """
    Calculate urgency score (0-100) based on:
    - Frequency: How often task repeats (40% weight)
    - Deadline proximity: Days until deadline (60% weight)
    """
    frequency_score = 0
    deadline_score = 0
    
    # Frequency scoring (0-100)
    frequency_map = {
        "daily": 100,
        "every-1-day": 100,
        "every-monday": 70,
        "every-tuesday": 70,
        "every-wednesday": 70,
        "every-thursday": 70,
        "every-friday": 80,
        "every-saturday": 50,
        "every-sunday": 50,
        "every-1-week": 70,
        "every-2-week": 50,
        "every-1-month": 30,
    }
    
    if todo["type"] == "recurring" and todo["frequency"]:
        for key, score in frequency_map.items():
            if key in todo["frequency"].lower():
                frequency_score = score
                break
    
    # Deadline proximity scoring (0-100)
    if todo.get("deadline") or todo.get("next_due"):
        deadline_str = todo.get("deadline") or todo.get("next_due")
        try:
            deadline_date = datetime.datetime.fromisoformat(deadline_str).date()
            days_until = (deadline_date - datetime.date.today()).days
            
            if days_until <= 0:
                deadline_score = 100  # Overdue
            elif days_until <= 1:
                deadline_score = 95
            elif days_until <= 3:
                deadline_score = 80
            elif days_until <= 7:
                deadline_score = 60
            elif days_until <= 14:
                deadline_score = 40
            elif days_until <= 30:
                deadline_score = 20
            else:
                deadline_score = 5
        except:
            deadline_score = 0
    
    # Weighted average: 40% frequency + 60% deadline
    urgency = (frequency_score * 0.4) + (deadline_score * 0.6)
    return int(urgency)

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
        elif os.path.exists("google_credentials.json"):
            # Fall back to local file for development
            credentials = Credentials.from_service_account_file(
                "google_credentials.json", scopes=SCOPES
            )
        else:
            raise FileNotFoundError(
                "❌ GOOGLE_CREDENTIALS not set and google_credentials.json not found. "
                "Please add GOOGLE_CREDENTIALS to your environment variables or place google_credentials.json in the project root."
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

def add_calendar_event(calendar_id, title, event_datetime, duration_hours=1):
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
                "dateTime": (event_datetime + datetime.timedelta(hours=duration_hours)).isoformat(),
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
    global CALENDAR_SERVICE, sent_startup_test_calendar_reminder
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
    
    # Start tasks only if not already running
    tasks_to_start = [
        ("daily_checkin", daily_checkin),
        ("daily_reset", daily_reset),
        ("weekly_summary", weekly_summary),
        ("monthly_summary", monthly_summary),
        ("event_reminders", event_reminders),
        ("daily_calendar_notification", daily_calendar_notification),
        ("weekly_calendar_summary", weekly_calendar_summary),
    ]
    
    for task_name, task in tasks_to_start:
        try:
            if not task.is_running():
                task.start()
                print(f"✅ Started {task_name}")
        except RuntimeError as e:
            if "already launched" in str(e):
                print(f"ℹ️ {task_name} already running")
            else:
                print(f"⚠️ Error starting {task_name}: {e}")

    # Optional: send a one-time fake reminder on startup so you can preview formatting.
    # Enable by setting SEND_TEST_CALENDAR_REMINDER=1
    try:
        # enabled = os.getenv("SEND_TEST_CALENDAR_REMINDER", "").strip() in {"1", "true", "True", "yes", "YES"}
        enabled=False
        if enabled and not sent_startup_test_calendar_reminder:
            sent_startup_test_calendar_reminder = True
            channel = get_calendar_channel()
            if channel:
                now = datetime.datetime.now(TZ)
                date_str = now.strftime("%a, %b %d %Y").replace(" 0", " ")
                time_str = now.strftime("%H:%M")

                embed = discord.Embed(
                    title="Test Event (preview)",
                    url="https://calendar.google.com/calendar/u/0/r",
                )
                embed.add_field(name="Scheduled for", value=f"`{date_str} {time_str}`", inline=True)
                embed.add_field(name="Duration", value="`1 hour`", inline=True)
                await channel.send(content="Starting in 2 hours: Test Event (preview)", embed=embed)
    except Exception as e:
        print(f"⚠️ Failed to send test calendar reminder: {e}")

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
    global todo_message_map
    
    if user.bot:
        return

    if reaction.emoji in HABITS and reaction.message.channel.name == CHECKIN_CHANNEL:
        habit = HABITS[reaction.emoji]
        add_habit(today_str(), habit, True)
        await log_habit(habit, True)
    
    # Handle individual todo completion (from !todos command)
    if reaction.emoji == "✅" and reaction.message.id in todo_message_map:
        todo_content = todo_message_map[reaction.message.id]
        
        # Find the row number for this todo content
        all_todos = TODOS_SHEET.get_all_values()
        row_num = None
        for row_idx, row in enumerate(all_todos[1:], start=2):
            if len(row) > 1 and row[1] == todo_content:
                row_num = row_idx
                break
        
        if row_num:
            update_todo_status(row_num, "done", now_str())
            
            # Extract todo content from message and send confirmation
            content = reaction.message.content
            done_channel = discord.utils.get(
                bot.get_all_channels(), name=DONE_CHANNEL
            )
            if done_channel:
                await done_channel.send(f"✅ {content}")
            
            # Edit the message to show it's done (strike through)
            try:
                await reaction.message.edit(content=f"~~{content}~~ ✅ **DONE**")
            except:
                pass
        
        # Clean up the mapping
        del todo_message_map[reaction.message.id]
    
    # Legacy: Handle todo reactions in the original message (if still used)
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
last_weekly_summary_date = None

@tasks.loop(hours=1)
async def weekly_summary():
    global last_weekly_summary_date
    
    today = datetime.date.today()
    
    # Only run once on Sunday
    if today.weekday() == 6 and last_weekly_summary_date != today:
        last_weekly_summary_date = today
        
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

        # Don't delete old messages, just send a new one
        await channel.send(report)

# ---------- MONTHLY SUMMARY ----------
last_calendar_date = None
last_weekly_calendar_date = None
sent_startup_test_calendar_reminder = False

# ---------- CALENDAR EVENT REMINDERS ----------
# Track sent reminders to avoid duplicates (format: "event_id_2h" or "event_id_1h" or "event_id_5min")
sent_reminders = {}

@tasks.loop(minutes=1)
async def event_reminders():
    """Send reminders 2h, 1h, and 5min before events"""
    global sent_reminders
    
    now = datetime.datetime.now(TZ)
    channel = get_calendar_channel()
    if not channel:
        if now.minute == 0:
            print(f"❌ Channel '{CALENDAR_CHANNEL}' not found")
        return
    
    # Clean up old reminders (older than 6 hours)
    cutoff_time = now - datetime.timedelta(hours=6)
    expired_keys = [k for k, v in sent_reminders.items() if v < cutoff_time]
    for key in expired_keys:
        del sent_reminders[key]
    
    # Check personal calendar
    personal_events = get_calendar_events(PERSONAL_CALENDAR_ID, days_ahead=1)
    for event in personal_events:
        await check_and_send_reminder(event, "personal", "📅", channel, now)
    
    # Check professional calendar if configured
    if PROFESSIONAL_CALENDAR_ID:
        professional_events = get_calendar_events(PROFESSIONAL_CALENDAR_ID, days_ahead=1)
        for event in professional_events:
            await check_and_send_reminder(event, "professional", "💼", channel, now)
    
    # Check competitive (LPL/LCK) calendar if configured
    if COMPETITIVE_CALENDAR_ID:
        competitive_events = get_calendar_events(COMPETITIVE_CALENDAR_ID, days_ahead=1)
        for event in competitive_events:
            await check_and_send_reminder(event, "competitive", "🎮", channel, now)

async def check_and_send_reminder(event, calendar_type, calendar_emoji, channel, now):
    """Check if event needs a reminder and send it"""
    try:
        start = event.get("start", {})
        start_time_str = start.get("dateTime", start.get("date"))
        
        # Skip all-day events
        if "T" not in start_time_str:
            return
        
        event_time = dateutil.parser.parse(start_time_str)
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=TZ)
        else:
            event_time = event_time.astimezone(TZ)

        time_until_seconds = (event_time - now).total_seconds()
        if time_until_seconds < 0:
            return
        
        # Calculate event duration
        end = event.get("end", {})
        end_time_str = end.get("dateTime", end.get("date"))
        duration_str = "1 hour"  # default
        if end_time_str:
            try:
                end_time = dateutil.parser.parse(end_time_str)
                duration_minutes = int((end_time - event_time).total_seconds() / 60)
                if duration_minutes < 60:
                    duration_str = f"{duration_minutes} min"
                else:
                    hours = duration_minutes // 60
                    minutes = duration_minutes % 60
                    if minutes == 0:
                        duration_str = f"{hours} hour" if hours == 1 else f"{hours} hours"
                    else:
                        duration_str = f"{hours}h {minutes}min"
            except:
                pass
        
        event_id = event.get("id", "")
        title = event.get("summary", "Untitled")
        time_str = event_time.strftime("%H:%M")
        date_str = event_time.strftime("%a, %b %d %Y").replace(" 0", " ")
        
        # Define reminder triggers: (threshold_seconds, window_seconds, label, emoji)
        reminders = [
            (120 * 60, 5 * 60, "2 hours"),
            (60 * 60, 5 * 60, "1 hour"),
            (5 * 60, 2 * 60, "5 minutes"),
        ]

        for threshold_seconds, window_seconds, label in reminders:
            # Allow a small late window (task loops may drift).
            if (threshold_seconds - window_seconds) <= time_until_seconds <= threshold_seconds:
                reminder_key = f"{event_id}_{label}"
                
                # Only send if not already sent
                if reminder_key not in sent_reminders:
                    sent_reminders[reminder_key] = now

                    # Discord doesn't reliably support masked links in plain messages.
                    # Use an embed so the title is clickable.
                    event_url = event.get("htmlLink")
                    embed = discord.Embed(title=title)
                    if event_url:
                        embed.url = event_url
                    embed.add_field(name="Scheduled for", value=f"`{date_str} {time_str}`", inline=True)
                    embed.add_field(name="Duration", value=f"`{duration_str}`", inline=True)
                    await channel.send(content=f"Starting in {label}: {title}", embed=embed)
    except Exception as e:
        print(f"Error checking reminder: {e}")

@tasks.loop(minutes=1)
async def daily_calendar_notification():
    """Send daily summary of calendar events for next 2 days at 9:00 AM"""
    global last_calendar_date
    
    now = datetime.datetime.now(TZ)
    today = now.date()
    
    # Debug logging
    if now.minute == 0:
        print(f"🕐 Daily calendar check: {now.strftime('%H:%M %Z')} | Today: {today}")
    
    if (
        now.hour == CALENDAR_HOUR
        and now.minute == CALENDAR_MINUTE
        and last_calendar_date != today
    ):
        last_calendar_date = today
        print(f"✅ Sending daily calendar summary at {now.strftime('%H:%M %Z')}")
        
        channel = get_calendar_channel()
        if not channel:
            print(f"❌ Channel '{CALENDAR_CHANNEL}' not found")
            return
        
        # Get events from all configured calendars
        personal_events = get_calendar_events(PERSONAL_CALENDAR_ID, days_ahead=2)
        professional_events = []
        if PROFESSIONAL_CALENDAR_ID:
            professional_events = get_calendar_events(PROFESSIONAL_CALENDAR_ID, days_ahead=2)
        competitive_events = []
        if COMPETITIVE_CALENDAR_ID:
            competitive_events = get_calendar_events(COMPETITIVE_CALENDAR_ID, days_ahead=2)
        
        # Format messages
        personal_msg = format_events_message(personal_events, "📅 **Personal Calendar - Next 2 Days**")
        await channel.send(personal_msg)
        
        if PROFESSIONAL_CALENDAR_ID and professional_events:
            professional_msg = format_events_message(professional_events, "💼 **Professional Calendar - Next 2 Days**")
            await channel.send(professional_msg)
        
        if COMPETITIVE_CALENDAR_ID and competitive_events:
            competitive_msg = format_events_message(competitive_events, "🎮 **Competitive (LPL/LCK) - Next 2 Days**")
            await channel.send(competitive_msg)

@tasks.loop(minutes=1)
async def weekly_calendar_summary():
    """Send full calendar for next 2 weeks every Monday at 9:00 AM"""
    global last_weekly_calendar_date
    
    now = datetime.datetime.now(TZ)
    today = now.date()
    
    # Debug logging (every Monday at 8 AM)
    if today.weekday() == 0 and now.hour == 8:
        print(f"🕐 Weekly calendar check: {now.strftime('%H:%M %Z')} Monday={today.weekday()} | Last: {last_weekly_calendar_date}")
    
    # Monday = 0, check if today is Monday and time is 9:00 AM
    if (
        today.weekday() == 0  # Monday
        and now.hour == CALENDAR_HOUR
        and now.minute == CALENDAR_MINUTE
        and last_weekly_calendar_date != today
    ):
        last_weekly_calendar_date = today
        print(f"✅ Sending weekly calendar reminder at {now.strftime('%H:%M %Z')}")
        
        channel = get_calendar_channel()
        if not channel:
            print(f"❌ Channel '{CALENDAR_CHANNEL}' not found")
            return
        
        # Get events from all configured calendars for next 14 days
        personal_events = get_calendar_events(PERSONAL_CALENDAR_ID, days_ahead=14)
        professional_events = []
        if PROFESSIONAL_CALENDAR_ID:
            professional_events = get_calendar_events(PROFESSIONAL_CALENDAR_ID, days_ahead=14)
        competitive_events = []
        if COMPETITIVE_CALENDAR_ID:
            competitive_events = get_calendar_events(COMPETITIVE_CALENDAR_ID, days_ahead=14)
        
        personal_msg = format_events_message(personal_events, "📅 **Personal Calendar - Next 2 Weeks**")
        await channel.send(personal_msg)
        
        if PROFESSIONAL_CALENDAR_ID and professional_events:
            professional_msg = format_events_message(professional_events, "💼 **Professional Calendar - Next 2 Weeks**")
            await channel.send(professional_msg)
        
        if COMPETITIVE_CALENDAR_ID and competitive_events:
            competitive_msg = format_events_message(competitive_events, "🎮 **Competitive (LPL/LCK) - Next 2 Weeks**")
            await channel.send(competitive_msg)


@bot.command(name="commands")
async def commands_help(ctx):
    """Display all available commands"""
    help_msg = """
📚 **Available Commands**

**📅 Calendar Commands:**
• `!calendarnow` - Show your calendar for the next 2 days
• `!addevent <title> <date> <time>` - Add a new event to your calendar
• `!add_scrim <opponent> <date> <time>` - Add a scrim match to your calendar

**✅ Habit Commands:**
• `!habits` - Show daily habits with reaction-based tracking
• `!listhabits` - List all configured habits
• `!addhabits` - Add new habits to track
• `!removehabits` - Remove habits from tracking
• `!weeklysummary` - Show habit completion for the past 7 days
• `!monthlysummary` - Show habit completion for the past 30 days

**📋 Todo Commands:**
• `!todos` - View your todo list

**🎯 Project Management Commands:**
• `!tickets` - Show all project tickets
• `!done` - Show completed tickets
• `!ongoing` - Show ongoing tickets
• `!addticket <project> <title>` - Add a new ticket
• `!closeticket <ticket_id>` - Mark a ticket as complete

**ℹ️ Other:**
• `!commands` - Display this message
"""
    await ctx.send(help_msg)


@bot.command(name="calendarnow")
async def calendar_now(ctx):
    """Send the same message as the daily calendar summary (next 2 days), immediately."""
    personal_events = get_calendar_events(PERSONAL_CALENDAR_ID, days_ahead=2)
    professional_events = []
    if PROFESSIONAL_CALENDAR_ID:
        professional_events = get_calendar_events(PROFESSIONAL_CALENDAR_ID, days_ahead=2)

    personal_msg = format_events_message(personal_events, "📅 **Personal Calendar - Next 2 Days**")
    await ctx.send(personal_msg)

    if PROFESSIONAL_CALENDAR_ID and professional_events:
        professional_msg = format_events_message(professional_events, "💼 **Professional Calendar - Next 2 Days**")
        await ctx.send(professional_msg)



# ---------- MONTHLY SUMMARY ----------
last_monthly_summary_date = None

@tasks.loop(hours=1)
async def monthly_summary():
    global last_monthly_summary_date
    
    today_date = datetime.date.today()
    
    # Only run once on the 1st of the month
    if today_date.day == 1 and last_monthly_summary_date != today_date:
        last_monthly_summary_date = today_date
        
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

        # Don't delete old messages, just send a new one
        await channel.send(report)

# ---------- TODO SYSTEM ----------
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name == TODO_CHANNEL:
        # Parse the todo input
        parsed = parse_todo_input(message.content)
        
        add_todo(
            content=parsed["content"],
            deadline=parsed["deadline"],
            todo_type=parsed["type"],
            frequency=parsed["frequency"],
            priority=parsed["priority"],
            tags=parsed["tags"]
        )
        await message.add_reaction("⏳")

    await bot.process_commands(message)

# ---------- COMMANDS ----------

@bot.command(name="todos")
async def show_todos(ctx, tag: str = None):
    """Show all pending todos sorted by urgency (1-by-1 with reactions). Usage: !todos or !todos:perso or !todos:pro"""
    # Handle command invocation like !todos:perso
    if ctx.invoked_subcommand is None:
        # Check if tag was passed via context invoke
        if tag:
            tag = tag.lower()
    
    todos = get_todos(status="pending", tag=tag)
    
    if not todos:
        tag_str = f" with tag '{tag}'" if tag else ""
        await ctx.send(f"✅ No pending todos{tag_str}!")
        return
    
    # Calculate urgency for each todo
    todos_with_urgency = []
    for todo in todos:
        urgency = calculate_urgency_score(todo)
        todos_with_urgency.append((todo, urgency))
    
    # Sort by urgency (highest first)
    todos_with_urgency.sort(key=lambda x: x[1], reverse=True)
    
    # Send header
    tag_str = f" - {tag.upper()}" if tag else ""
    await ctx.send(f"📋 **Your Todos{tag_str} (by urgency)**")
    
    # Send each todo as a separate message with ✅ reaction
    for idx, (todo, urgency) in enumerate(todos_with_urgency, 1):
        # Urgency emoji
        if urgency >= 80:
            urgency_emoji = "🔴"
        elif urgency >= 50:
            urgency_emoji = "🟡"
        else:
            urgency_emoji = "🟢"
        
        # Format todo
        content = todo["content"]
        todo_type = f"_{todo['type']}_" if todo['type'] != "one-time" else ""
        
        if todo["frequency"]:
            freq = f"\n  ↻ {todo['frequency']}"
        else:
            freq = ""
        
        if todo["deadline"]:
            deadline = f"\n  📅 Due: {todo['deadline']}"
        else:
            deadline = ""
        
        # Format tags
        if todo["tags"]:
            tags_str = f"\n  🏷️ {', '.join([f'`{t}`' for t in todo['tags']])}"
        else:
            tags_str = ""
        
        todo_msg = f"{urgency_emoji} **{content}** `({urgency})`{todo_type}{freq}{deadline}{tags_str}"
        
        sent_msg = await ctx.send(todo_msg)
        await sent_msg.add_reaction("✅")
        
        # Store todo content for tracking in reaction handler (simpler approach)
        # We'll look up the row number when the reaction is added
        todo_message_map[sent_msg.id] = content

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


@bot.command(name="add_scrim")
async def add_scrim(ctx, *, args: str):
    """Add a 6-hour scrim event to the professional calendar.

        Usage: !add_scrim TEAM DD-MM HH.MM [AM|PM] stream|no_stream
        Examples:
            !add_scrim KC 30-12 3.30 stream
            !add_scrim KC 30-12 3.30 PM no_stream
    """
    if PROFESSIONAL_CHANNEL_ID and ctx.channel.id != PROFESSIONAL_CHANNEL_ID:
        await ctx.send(f"❌ Use this command in <#{PROFESSIONAL_CHANNEL_ID}>.")
        return

    if not PROFESSIONAL_CALENDAR_ID:
        await ctx.send("❌ Professional calendar not configured (PROFESSIONAL_CALENDAR_ID missing)")
        return

    parts = args.split()
    date_idx = None
    for i, part in enumerate(parts):
        if re.fullmatch(r"\d{1,2}-\d{1,2}", part):
            date_idx = i
            break

    if date_idx is None or date_idx == 0:
        await ctx.send("❌ Usage: !add_scrim TEAM DD-MM HH.MM [AM|PM] stream|no_stream")
        return
    if date_idx + 1 >= len(parts):
        await ctx.send("❌ Usage: !add_scrim TEAM DD-MM HH.MM [AM|PM] stream|no_stream")
        return

    team = " ".join(parts[:date_idx]).strip()
    date_token = parts[date_idx]
    time_token_raw = parts[date_idx + 1]

    # Optional AM/PM token after time (ignored)
    stream_token_idx = date_idx + 2
    if stream_token_idx < len(parts) and parts[stream_token_idx].lower() in {"am", "pm"}:
        stream_token_idx += 1

    if stream_token_idx >= len(parts):
        await ctx.send("❌ Usage: !add_scrim TEAM DD-MM HH.MM [AM|PM] stream|no_stream")
        return

    stream_token = parts[stream_token_idx].lower()
    if stream_token not in {"stream", "no_stream", "no-stream", "nostream"}:
        await ctx.send("❌ Missing stream info: add 'stream' or 'no_stream' after the time")
        return

    can_stream = stream_token == "stream"

    # Some inputs may include AM/PM attached (e.g., 3.30PM); ignore it.
    time_token = re.sub(r"\s*(am|pm)\s*$", "", time_token_raw, flags=re.IGNORECASE)

    m = re.fullmatch(r"(\d{1,2})[\.:](\d{2})", time_token)
    if not m:
        await ctx.send("❌ Time format must be HH.MM (example: 20.20)")
        return

    try:
        day_str, month_str = date_token.split("-")
        day = int(day_str)
        month = int(month_str)
        hour = int(m.group(1))
        minute = int(m.group(2))

        # Scrim start time is always between 10:00 AM and 6:00 PM.
        # Interpretation rules:
        # - 10.xx, 11.xx => AM (10:xx, 11:xx)
        # - 12.xx => noon hour (12:xx)
        # - 1.xx .. 6.xx => PM (13:xx .. 18:xx)
        # - 13.xx .. 18.xx => accepted as 24h time
        if not (0 <= minute <= 59):
            raise ValueError("Invalid minute")
        if hour in (10, 11):
            pass
        elif hour == 12:
            pass
        elif 1 <= hour <= 6:
            hour += 12
        elif 13 <= hour <= 18:
            pass
        else:
            raise ValueError("Hour must be between 10.00 AM and 6.00 PM")

        year = 2026
        target_date = datetime.date(year, month, day)

        start_dt = datetime.datetime(
            target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=TZ
        )
    except Exception:
        await ctx.send("❌ Invalid date/time. Usage: !add_scrim TEAM DD-MM HH.MM [AM|PM] stream|no_stream")
        return

    title = f"Scrim vs {team} | {'Can Stream' if can_stream else 'No Stream'}"
    result = add_calendar_event(PROFESSIONAL_CALENDAR_ID, title, start_dt, duration_hours=6)
    if not result.get("success"):
        await ctx.send(f"❌ Error adding scrim: {result.get('error', 'unknown error')}")
        return

    created = result.get("event", {})
    link = created.get("htmlLink")
    when_str = start_dt.strftime("%a, %b %d %Y at %H:%M")

    embed = discord.Embed(title=title)
    if link:
        embed.url = link
    embed.add_field(name="Scheduled for", value=f"`{when_str}`", inline=True)
    embed.add_field(name="Duration", value="`6 hours`", inline=True)
    await ctx.send(content=f"✅ Added: {title}", embed=embed)

# ---------- HABITS COMMAND ----------
@bot.command(name="habits")
async def show_habits(ctx):
    """Display today's habits with reactions to mark completion"""
    today = today_str()
    
    if not HABITS:
        await ctx.send("❌ No habits configured. Use !addhabits to add some!")
        return
    
    # Build dynamic habits message
    habits_list = "React to mark as completed:\n"
    for emoji, name in HABITS.items():
        habits_list += f"{emoji} {name.capitalize()}\n"
    
    msg = await ctx.send(
        f"🏃 **Daily Habits — {today}**\n\n{habits_list}"
    )
    
    for emoji in HABITS:
        await msg.add_reaction(emoji)

@bot.command(name="addhabits")
async def add_habits(ctx, emoji, *, name):
    """Add a new habit
    Usage: !addhabits 🎯 productivity
    """
    result = add_habit_to_config(emoji, name)
    if result["success"]:
        await ctx.send(f"✅ Added habit: {emoji} **{name}**")
    else:
        await ctx.send(f"❌ Error: {result['error']}")

@bot.command(name="removehabits")
async def remove_habits(ctx, emoji):
    """Remove a habit
    Usage: !removehabits 🎯
    """
    result = remove_habit_from_config(emoji)
    if result["success"]:
        # Reload habits from Google Sheets to ensure consistency
        load_habits_from_config()
        await ctx.send(f"✅ Removed habit: {emoji}")
    else:
        await ctx.send(f"❌ Error: {result['error']}")

@bot.command(name="listhabits")
async def list_habits(ctx):
    """Show all configured habits with reactions (one message per habit)"""
    if not HABITS:
        await ctx.send("❌ No habits configured yet. Use !addhabits to add one!")
        return
    
    for emoji, name in HABITS.items():
        msg = await ctx.send(f"{emoji} {name.capitalize()}")
        await msg.add_reaction(emoji)

# ---------- SUMMARY COMMANDS ----------
@bot.command(name="weeklysummary")
async def weekly_summary_cmd(ctx):
    """Show weekly habit summary (past 7 days)"""
    try:
        habits_data = HABITS_SHEET.get_all_values()[1:]  # Skip header
        
        if not habits_data:
            await ctx.send("📊 No habit data yet!")
            return
        
        # Get last 7 days
        today = datetime.date.today()
        week_start = today - datetime.timedelta(days=6)
        
        # Create a dict of habits completed this week (only active habits)
        habit_week_data = {}
        for row in habits_data:
            if len(row) >= 3:
                date_str, habit, completed = row[0], row[1], row[2]
                try:
                    date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                    if week_start <= date_obj <= today:
                        # Only include if habit is currently active
                        if any(habit == name for name in HABITS.values()):
                            if habit not in habit_week_data:
                                habit_week_data[habit] = []
                            habit_week_data[habit].append(int(completed))
                except:
                    pass
        
        if not habit_week_data:
            await ctx.send("📊 No habit data for this week yet!")
            return
        
        # Create summary message
        msg = "📊 **Weekly Habit Summary (Past 7 Days)**\n\n"
        
        for habit, completions in sorted(habit_week_data.items()):
            # Pad to 7 days
            while len(completions) < 7:
                completions.append(0)
            completions = completions[:7]
            
            # Create visual bar
            bar = ""
            for completed in completions:
                bar += "🟩" if completed == 1 else "⬜"
            
            # Calculate percentage
            completed_count = sum(completions)
            percentage = (completed_count / 7) * 100
            
            msg += f"{bar} {habit}: {completed_count}/7 ({percentage:.0f}%)\n"
        
        await ctx.send(msg)
    except Exception as e:
        await ctx.send(f"❌ Error generating weekly summary: {e}")

@bot.command(name="monthlysummary")
async def monthly_summary_cmd(ctx):
    """Show monthly habit summary (past 30 days)"""
    try:
        habits_data = HABITS_SHEET.get_all_values()[1:]  # Skip header
        
        if not habits_data:
            await ctx.send("📊 No habit data yet!")
            return
        
        # Get last 30 days
        today = datetime.date.today()
        month_start = today - datetime.timedelta(days=29)
        
        # Create a dict of habits completed this month (only active habits)
        habit_month_data = {}
        for row in habits_data:
            if len(row) >= 3:
                date_str, habit, completed = row[0], row[1], row[2]
                try:
                    date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                    if month_start <= date_obj <= today:
                        # Only include if habit is currently active
                        if any(habit == name for name in HABITS.values()):
                            if habit not in habit_month_data:
                                habit_month_data[habit] = []
                            habit_month_data[habit].append(int(completed))
                except:
                    pass
        
        if not habit_month_data:
            await ctx.send("📊 No habit data for this month yet!")
            return
        
        # Create summary message
        msg = "📊 **Monthly Habit Summary (Past 30 Days)**\n\n"
        
        for habit, completions in sorted(habit_month_data.items()):
            # Pad to 30 days
            while len(completions) < 30:
                completions.append(0)
            completions = completions[:30]
            
            # Create visual bar (scale to 10 blocks)
            completed_count = sum(completions)
            bar_blocks = int((completed_count / 30) * 10)
            bar = "🟩" * bar_blocks + "⬜" * (10 - bar_blocks)
            
            percentage = (completed_count / 30) * 100
            
            msg += f"{bar} {habit}: {completed_count}/30 ({percentage:.0f}%)\n"
        
        await ctx.send(msg)
    except Exception as e:
        await ctx.send(f"❌ Error generating monthly summary: {e}")

# ---------- PROJECT MANAGEMENT COMMANDS ----------
@bot.command(name="tickets")
async def show_tickets(ctx):
    """Display next steps/open tickets for the project"""
    # Find which project this command is being used in
    project_id = None
    for pid, project_info in PROJECTS.items():
        if project_info["channel_id"] == ctx.channel.id:
            project_id = pid
            break
    
    if not project_id:
        await ctx.send("❌ This command only works in project channels")
        return
    
    # Get open tickets for this project from Google Sheets
    all_tickets = TICKETS_SHEET.get_all_values()[1:]  # Skip header
    open_tickets = [row for row in all_tickets if len(row) >= 4 and row[1] == str(project_id) and row[3] == "open"]
    
    if not open_tickets:
        await ctx.send(f"✅ No open tickets for {PROJECTS[project_id]['name']}!")
        return
    
    msg = f"📋 **Open Tickets - {PROJECTS[project_id]['name']}**\n\n"
    for row in open_tickets:
        ticket_id, _, title = row[0], row[1], row[2]
        msg += f"#{ticket_id} • {title}\n"
    
    await ctx.send(msg)

@bot.command(name="done")
async def show_done(ctx):
    """Display completed tickets for the project"""
    # Find which project this command is being used in
    project_id = None
    for pid, project_info in PROJECTS.items():
        if project_info["channel_id"] == ctx.channel.id:
            project_id = pid
            break
    
    if not project_id:
        await ctx.send("❌ This command only works in project channels")
        return
    
    # Get completed tickets for this project from Google Sheets
    all_tickets = TICKETS_SHEET.get_all_values()[1:]  # Skip header
    done_tickets = [row for row in all_tickets if len(row) >= 4 and row[1] == str(project_id) and row[3] == "done"]
    
    if not done_tickets:
        await ctx.send(f"✅ No completed tickets for {PROJECTS[project_id]['name']}!")
        return
    
    msg = f"✅ **Completed Tickets - {PROJECTS[project_id]['name']}**\n\n"
    for row in done_tickets:
        ticket_id, _, title = row[0], row[1], row[2]
        msg += f"#{ticket_id} • {title}\n"
    
    await ctx.send(msg)

@bot.command(name="ongoing")
async def show_ongoing(ctx):
    """Show all open tickets across all projects"""
    all_tickets = TICKETS_SHEET.get_all_values()[1:]  # Skip header
    
    # Count open tickets per project
    project_counts = {}
    for row in all_tickets:
        if len(row) >= 4 and row[3] == "open":
            project_id = int(row[1])
            project_counts[project_id] = project_counts.get(project_id, 0) + 1
    
    if not project_counts:
        await ctx.send("✅ All projects are clear - no open tickets!")
        return
    
    msg = "🚀 **Ongoing Tickets Across All Projects**\n\n"
    for project_id, count in sorted(project_counts.items()):
        project_name = PROJECTS[project_id]["name"]
        msg += f"**{project_name}**: {count} open ticket(s)\n"
    
    await ctx.send(msg)

@bot.command(name="addticket")
async def add_ticket(ctx, *, title):
    """Add a new ticket to the project"""
    # Find which project this command is being used in
    project_id = None
    for pid, project_info in PROJECTS.items():
        if project_info["channel_id"] == ctx.channel.id:
            project_id = pid
            break
    
    if not project_id:
        await ctx.send("❌ This command only works in project channels")
        return
    
    # Get next ticket ID
    all_tickets = TICKETS_SHEET.get_all_values()[1:]  # Skip header
    ticket_id = len(all_tickets) + 1
    
    # Add ticket to Google Sheets
    TICKETS_SHEET.append_row([
        str(ticket_id),
        str(project_id),
        title,
        "open",
        datetime.datetime.now(TZ).isoformat(),
        ""  # completed_at
    ])
    
    await ctx.send(f"✅ Added ticket #{ticket_id}: **{title}** to {PROJECTS[project_id]['name']}")

@bot.command(name="closeticket")
async def close_ticket(ctx, ticket_id: int):
    """Mark a ticket as done"""
    # Find which project this command is being used in
    project_id = None
    for pid, project_info in PROJECTS.items():
        if project_info["channel_id"] == ctx.channel.id:
            project_id = pid
            break
    
    if not project_id:
        await ctx.send("❌ This command only works in project channels")
        return
    
    # Find and update the ticket in Google Sheets
    all_rows = TICKETS_SHEET.get_all_values()
    ticket_found = False
    
    for row_idx, row in enumerate(all_rows[1:], start=2):  # Start from row 2 (skip header)
        if len(row) >= 4 and row[0] == str(ticket_id) and row[1] == str(project_id):
            TICKETS_SHEET.update_cell(row_idx, 4, "done")  # Update status to done
            TICKETS_SHEET.update_cell(row_idx, 6, datetime.datetime.now(TZ).isoformat())  # Update completed_at
            ticket_found = True
            break
    
    if not ticket_found:
        await ctx.send(f"❌ Ticket #{ticket_id} not found")
        return
    
    await ctx.send(f"✅ Ticket #{ticket_id} marked as done!")

# ---------- RUN ----------

bot.run(TOKEN)
