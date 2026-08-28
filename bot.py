import discord
from discord.ext import commands
import json
import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from github import Github
from datetime import datetime
from typing import Union

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- CONFIGURATION FROM ENVIRONMENT ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO_NAME = os.getenv("GITHUB_REPO")
PORT = int(os.getenv("PORT", 8080))
DATA_FILE = "dkp_data.json"
LOG_FILE = "dkp_history.md"

EVENT_MULTIPLIERS = {
    "skilling": 1.0,
    "bossing": 1.5,
    "bingo": 2.0,
    "custom": 1.0
}

# --- HEALTH CHECK SERVER (For Google Cloud Run) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive.")
    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    server.serve_forever()

# --- GITHUB STORAGE HELPER CODES ---
def load_dkp_from_github():
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO_NAME)
        file_content = repo.get_contents(DATA_FILE)
        return json.loads(file_content.decoded_content.decode("utf-8"))
    except Exception as e:
        logging.warning(f"Starting fresh database: {e}")
        return {}

def save_dkp_to_github(data, log_entry=None):
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO_NAME)
        json_content = json.dumps(data, indent=4)
        try:
            file = repo.get_contents(DATA_FILE)
            repo.update_file(file.path, "Update balances", json_content, file.sha)
        except Exception:
            repo.create_file(DATA_FILE, "Init database", json_content)
        if log_entry:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            new_log = f"## [{ts}] {log_entry}\n\n"
            try:
                l_file = repo.get_contents(LOG_FILE)
                updated_log = new_log + l_file.decoded_content.decode("utf-8")
                repo.update_file(l_file.path, "Append audit log", updated_log, l_file.sha)
            except Exception:
                repo.create_file(LOG_FILE, "Init audit log", new_log)
    except Exception as e:
        logging.error(f"CRITICAL GitHub Sync Fail: {e}")

# --- BOT CONTEXT START ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user.name}")
    
# --- COMMANDS ENGINE ---

@bot.command(name="attendance")
@commands.has_permissions(administrator=True)
async def attendance(ctx, target: Union[discord.VoiceChannel, discord.Role, discord.Member], event_type: str, base_points: int):
    event_type = event_type.lower()
    if event_type not in EVENT_MULTIPLIERS:
        await ctx.send("❌ Invalid event type.")
        return

    multiplier = EVENT_MULTIPLIERS[event_type]
    final_points = round(base_points * multiplier)
    dkp_data = load_dkp_from_github()
    rewarded = []

    if isinstance(target, discord.VoiceChannel) or isinstance(target, discord.Role):
        t_name = f"{target.name}"
        for member in target.members:
            if not member.bot:
                dkp_data[str(member.id)] = dkp_data.get(str(member.id), 0) + final_points
                rewarded.append(member.display_name)
    elif isinstance(target, discord.Member):
        t_name = f"{target.display_name}"
        if not target.bot:
            dkp_data[str(target.id)] = dkp_data.get(str(target.id), 0) + final_points
            rewarded.append(target.display_name)

    if rewarded:
        log_msg = f"Event via {t_name}. Given {final_points} DKP to: {', '.join(rewarded)}"
        save_dkp_to_github(dkp_data, log_msg)
        await ctx.send(f"✅ Points added to **{len(rewarded)}** members matching target **{t_name}**!")
    else:
        await ctx.send("⚠️ No valid members found to reward.")

@attendance.error
async def attendance_error(ctx, error):
    if isinstance(error, commands.BadUnionArgument):
        await ctx.send("❌ **Invalid Target!** Use a VC Name, @Role, or @Member.")
    else:
        await ctx.send(f"⚠️ Error: `{error}`")

@bot.command(name="award")
@commands.has_permissions(administrator=True)
async def award(ctx, member: discord.Member, points: int, *, reason: str = "Significant Event"):
    dkp_data = load_dkp_from_github()
    dkp_data[str(member.id)] = dkp_data.get(str(member.id), 0) + points
    save_dkp_to_github(dkp_data, f"Award: {member.display_name} +{points} DKP ({reason})")
    await ctx.send(f"🏆 **{member.display_name}** received **{points} DKP**!")

@bot.command(name="spend")
async def spend(ctx, amount: int, *, item: str):
    if amount <= 0: return
    dkp_data = load_dkp_from_github()
    bal = dkp_data.get(str(ctx.author.id), 0)
    if bal < amount:
        await ctx.send("❌ Insufficient funds.")
        return
    dkp_data[str(ctx.author.id)] = bal - amount
    save_dkp_to_github(dkp_data, f"Spent: {ctx.author.display_name} -{amount} DKP on {item}")
    await ctx.send(f"🛍️ Purchase logged! Remaining balance: `{bal - amount} DKP`")

@bot.command(name="dkp")
async def dkp(ctx, member: discord.Member = None):
    t = member or ctx.author
    dkp_data = load_dkp_from_github()
    await ctx.send(f"📊 **{t.display_name}** has **{dkp_data.get(str(t.id), 0)} DKP**.")

@bot.command(name="leaderboard")
async def leaderboard(ctx):
    dkp_data = load_dkp_from_github()
    if not dkp_data:
        await ctx.send("Database is empty.")
        return
    sorted_dkp = sorted(dkp_data.items(), key=lambda item: int(item[1]), reverse=True)
    embed = discord.Embed(title="🏆 Leaderboard", color=discord.Color.gold())
    desc = ""
    for idx, (u_id, pts) in enumerate(sorted_dkp[:10], start=1):
        m = ctx.guild.get_member(int(u_id))
        name = m.display_name if m else f"ID {u_id}"
        desc += f"**#{idx}** {name} — `{pts} DKP`\n"
    embed.description = desc
    await ctx.send(embed=embed)
    
# --- GLOBAL ACTIVE EVENT TRACKER ---
active_event = {
    "secret_code": None,
    "final_points": 0,
    "event_type": None,
    "claimed_users": set()
}

@bot.command(name="startevent")
@commands.has_permissions(administrator=True)
async def startevent(ctx, event_type: str, base_points: int, secret_code: str):

    global active_event
    
    event_type = event_type.lower()
    if event_type not in EVENT_MULTIPLIERS:
        await ctx.send("❌ Invalid event type.")
        return

    import asyncio
    multiplier = EVENT_MULTIPLIERS[event_type]
    active_event["secret_code"] = secret_code.lower()
    active_event["final_points"] = round(base_points * multiplier)
    active_event["event_type"] = event_type
    active_event["claimed_users"] = set()

    embed = discord.Embed(title="📢 Self-Checkin Started!", color=discord.Color.orange())
    embed.description = f"Claim your points for the next **15 minutes**!\n\n👉 Type **`!checkin {secret_code}`**"
    embed.add_field(name="Points", value=f"**{active_event['final_points']} DKP**")
    await ctx.send(embed=embed)

    await asyncio.sleep(900)
    if active_event["secret_code"] == secret_code.lower():
        active_event["secret_code"] = None
        await ctx.send("🛑 The check-in window has closed.")

@bot.command(name="checkin")
async def checkin(ctx, code: str):
    if not active_event["secret_code"]:
        await ctx.send("❌ No active check-in event running.")
        return
    if code.lower() != active_event["secret_code"]:
        await ctx.send("❌ Incorrect code.")
        return
    
    u_id = str(ctx.author.id)
    if u_id in active_event["claimed_users"]:
        await ctx.send("⚠️ Already checked in!")
        return

    dkp_data = load_dkp_from_github()
    dkp_data[u_id] = dkp_data.get(u_id, 0) + active_event["final_points"]
    active_event["claimed_users"].add(u_id)
    
    save_dkp_to_github(dkp_data, f"Self-Checkin: {ctx.author.display_name} +{active_event['final_points']} DKP")
    await ctx.send(f"✅ logged! **+{active_event['final_points']} DKP**")
    
# --- RUN ENGINE LOOP ---
if __name__ == "__main__":
    if DISCORD_TOKEN and GITHUB_TOKEN and GITHUB_REPO_NAME:
        t = threading.Thread(target=run_health_server, daemon=True)
        t.start()
        bot.run(DISCORD_TOKEN)
