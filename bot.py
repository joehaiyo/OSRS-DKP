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
PORT = int(os.getenv("PORT", 8080)) # Google Cloud Run dictates this port dynamically
DATA_FILE = "dkp_data.json"
LOG_FILE = "dkp_history.md"

EVENT_MULTIPLIERS = {
    "skilling": 1.0,
    "bossing": 1.5,
    "bingo": 2.0,
    "custom": 1.0
}

# --- HEALTH CHECK WEB SERVER (Required by Google Cloud Run) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Responds to Google Cloud's alive pings to keep the container awake."""
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive and processing events.")

    def log_message(self, format, *args):
        # Prevents flooding logs with health checks
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logging.info(f"Health check server listening globally on port {PORT}")
    server.serve_forever()

# --- GITHUB HELPER FUNCTIONS ---
def load_dkp_from_github():
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO_NAME)
        file_content = repo.get_contents(DATA_FILE)
        return json.loads(file_content.decoded_content.decode("utf-8"))
    except Exception as e:
        logging.warning(f"Starting fresh DKP database. Info: {e}")
        return {}

def save_dkp_to_github(data, log_entry=None):
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO_NAME)
        json_content = json.dumps(data, indent=4)
        
        try:
            file = repo.get_contents(DATA_FILE)
            repo.update_file(file.path, "Update DKP database balances", json_content, file.sha)
        except Exception:
            repo.create_file(DATA_FILE, "Initialize DKP database", json_content)
            
        if log_entry:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            new_log_text = f"## [{timestamp}] {log_entry}\n\n"
            try:
                log_file = repo.get_contents(LOG_FILE)
                existing_log = log_file.decoded_content.decode("utf-8")
                updated_log = new_log_text + existing_log
                repo.update_file(log_file.path, "Append DKP audit log", updated_log, log_file.sha)
            except Exception:
                repo.create_file(LOG_FILE, "Initialize DKP audit log", new_log_text)
                
        logging.info("Successfully synced data and logs to GitHub repository.")
    except Exception as e:
        logging.error(f"Failed to sync data modifications with GitHub: {e}")

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logging.info(f"Bot connected to Discord gateway successfully as {bot.user.name}")

# --- COMMANDS ---
@bot.command(name="attendance")
@commands.has_permissions(administrator=True)
async def attendance(ctx, target: Union[discord.VoiceChannel, discord.Role], event_type: str, base_points: int):
    event_type = event_type.lower()
    if event_type not in EVENT_MULTIPLIERS:
        valid_types = ", ".join(EVENT_MULTIPLIERS.keys())
        await ctx.send(f"❌ Invalid event type. Choose from: `{valid_types}`")
        return

    multiplier = EVENT_MULTIPLIERS[event_type]
    final_points = round(base_points * multiplier)
    dkp_data = load_dkp_from_github()
    rewarded = []

    # Case 1: Target is a Voice Channel
    if isinstance(target, discord.VoiceChannel):
        target_name = f"🎙️ VC: {target.name}"
        for member in target.members:
            if not member.bot:
                user_id = str(member.id)
                dkp_data[user_id] = dkp_data.get(user_id, 0) + final_points
                rewarded.append(member.display_name)
                
    # Case 2: Target is a Role
    elif isinstance(target, discord.Role):
        target_name = f"🏷️ Role: {target.name}"
        for member in target.members:
            if not member.bot:
                user_id = str(member.id)
                dkp_data[user_id] = dkp_data.get(user_id, 0) + final_points
                rewarded.append(member.display_name)

    if rewarded:
        log_msg = f"Attendance: {event_type.upper()} event (x{multiplier}) via {target_name}. Awarded {final_points} DKP to: {', '.join(rewarded)}"
        save_dkp_to_github(dkp_data, log_msg)
        
        embed = discord.Embed(title="✅ Attendance Logged Successfully", color=discord.Color.green())
        embed.add_field(name="Target Source", value=target_name, inline=True)
        embed.add_field(name="Event Type", value=f"{event_type.capitalize()} (x{multiplier})", inline=True)
        embed.add_field(name="Points Given", value=f"**{final_points} DKP**", inline=True)
        embed.add_field(name="Attendees Rewarded", value=f"{len(rewarded)} clan members", inline=False)
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"⚠️ No human users found matching {target_name}.")

@bot.command(name="award")
@commands.has_permissions(administrator=True)
async def award(ctx, member: discord.Member, points: int, *, reason: str = "Significant Event"):
    dkp_data = load_dkp_from_github()
    user_id = str(member.id)
    dkp_data[user_id] = dkp_data.get(user_id, 0) + points
    log_msg = f"Manual Award: {member.display_name} received {points} DKP for '{reason}'"
    save_dkp_to_github(dkp_data, log_msg)
    await ctx.send(f"🏆 **{member.display_name}** received **{points} DKP** for **{reason}**!")

@bot.command(name="spend")
async def spend(ctx, amount: int, *, item: str):
    if amount <= 0:
        await ctx.send("❌ You must spend an amount greater than 0.")
        return
        
    dkp_data = load_dkp_from_github()
    user_id = str(ctx.author.id)
    current_balance = dkp_data.get(user_id, 0)
    
    if current_balance < amount:
        await ctx.send(f"❌ Insufficient funds. You need `{amount} DKP` but only have `{current_balance} DKP`.")
        return
        
    dkp_data[user_id] = current_balance - amount
    log_msg = f"Purchase: {ctx.author.display_name} spent {amount} DKP on '{item}'"
    save_dkp_to_github(dkp_data, log_msg)
    
    embed = discord.Embed(title="🛍️ DKP Purchase Request", color=discord.Color.blue())
    embed.description = f"**{ctx.author.mention}** has successfully redeemed points!"
    embed.add_field(name="Item Purchased", value=item, inline=True)
    embed.add_field(name="Cost", value=f"`{amount} DKP`", inline=True)
    embed.add_field(name="Remaining Balance", value=f"`{dkp_data[user_id]} DKP`", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="dkp")
async def dkp(ctx, member: discord.Member = None):
    target = member or ctx.author
    dkp_data = load_dkp_from_github()
    balance = dkp_data.get(str(target.id), 0)
    await ctx.send(f"📊 **{target.display_name}** currently has **{balance} DKP**.")

@bot.command(name="leaderboard")
async def leaderboard(ctx):
    dkp_data = load_dkp_from_github()
    if not dkp_data:
        await ctx.send("The DKP database is currently empty.")
        return
        
    # Safe numerical sorting check (highest to lowest score)
    sorted_dkp = sorted(dkp_data.items(), key=lambda item: int(item[1]), reverse=True)
    
    embed = discord.Embed(title="🏆 Clan DKP Leaderboard", color=discord.Color.gold())
    description = ""
    for index, (user_id, points) in enumerate(sorted_df[:10], start=1):
        member = ctx.guild.get_member(int(user_id))
        name = member.display_name if member else f"User ID {user_id}"
        description += f"**#{index}** {name} — `{points} DKP`\n"
        
    embed.description = description
    await ctx.send(embed=embed)
    
if __name__ == "__main__":
    if not DISCORD_TOKEN or not GITHUB_TOKEN or not GITHUB_REPO_NAME:
        logging.critical("Missing required environment variables. Initialization halted.")
    else:
        # Start the internal Web Server thread for Google Cloud health pings
        server_thread = threading.Thread(target=run_health_server, daemon=True)
        server_thread.start()
        
        # Launch the main Discord client event loop
        bot.run(DISCORD_TOKEN)
