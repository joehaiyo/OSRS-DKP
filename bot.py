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

# --- OSRS SCIMITAR RANK TIERS CONFIGURATION ---
RANK_TIERS = {
    "bronze": {"name": "Bronze Scimitar", "cost": 150},
    "iron": {"name": "Iron Scimitar", "cost": 400},
    "steel": {"name": "Steel Scimitar", "cost": 800},
    "black": {"name": "Black Scimitar", "cost": 1500},
    "mithril": {"name": "Mithril Scimitar", "cost": 2500},
    "adamant": {"name": "Adamant Scimitar", "cost": 4000},
    "rune": {"name": "Rune Scimitar", "cost": 6000},
    "dragon": {"name": "Dragon Scimitar", "cost": 10000}
}

@bot.command(name="buyrank")
async def buyrank(ctx, *, rank_keyword: str):
    """Allows members to buy OSRS scimitar tier roles using their accumulated DKP points."""
    rank_keyword = rank_keyword.lower().strip()
    
    if rank_keyword not in RANK_TIERS:
        valid_ranks = ", ".join([f"`{k.capitalize()}`" for k in RANK_TIERS.keys()])
        await ctx.send(f"❌ Invalid rank! Choose from: {valid_ranks}\n*Example:* `!buyrank mithril`")
        return

    rank_info = RANK_TIERS[rank_keyword]
    target_role_name = rank_info["name"]
    cost = rank_info["cost"]

    # 1. Verify the role exists inside the Discord server
    role = discord.utils.get(ctx.guild.roles, name=target_role_name)
    if not role:
        await ctx.send(f"❌ Error: The server role **'{target_role_name}'** does not exist. Please ask an Admin to create it.")
        return

    # 2. Check if the user already holds this rank role
    if role in ctx.author.roles:
        await ctx.send(f"⚠️ You already hold the **{target_role_name}** rank!")
        return

    # 3. Load database balances and verify funding
    dkp_data = load_dkp_from_github()
    user_id = str(ctx.author.id)
    current_balance = dkp_data.get(user_id, 0)

    if current_balance < cost:
        await ctx.send(f"❌ Insufficient DKP! **{target_role_name}** costs `{cost} DKP`, but you only have `{current_balance} DKP`.")
        return

    # 4. Process transaction deductions
    dkp_data[user_id] = current_balance - cost
    log_msg = f"Rank Purchase: {ctx.author.display_name} spent {cost} DKP to promote to {target_role_name}"
    save_dkp_to_github(dkp_data, log_msg)

    # 5. Hand out the role natively in Discord
    try:
        await ctx.author.add_roles(role)
        
        embed = discord.Embed(title="⚔️ Clan Promotion Achieved!", color=discord.Color.dark_red())
        embed.description = f"⚔️ **{ctx.author.mention}** has advanced to a higher combat tier!"
        embed.add_field(name="Rank Unlocked", value=f"🛡️ **{target_role_name}**", inline=True)
        embed.add_field(name="DKP Spent", value=f"`{cost} DKP`", inline=True)
        embed.add_field(name="New Balance", value=f"`{dkp_data[user_id]} DKP`", inline=True)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(
            "⚠️ Points deducted, but **failed to give role due to a Discord Hierarchy error!**\n"
            "👉 Please ask an Admin to go to Server Settings -> Roles, and drag the Bot's role above the Scimitar roles."

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

# --- GLOBAL ACTIVE EVENT TRACKER ---
# The active event state will now safely fall back to checking deadlines.
active_event = {
    "secret_code": None,
    "final_points": 0,
    "event_type": None,
    "end_timestamp": 0, # Stored as a raw epoch integer
    "claimed_users": set()
}

@bot.command(name="startevent")
@commands.has_permissions(administrator=True)
async def startevent(ctx, event_type: str, base_points: int, duration_days: int, secret_code: str):
    """Starts an attendance window that persists safely across server restarts."""
    global active_event
    event_type = event_type.lower()
    
    if event_type not in EVENT_MULTIPLIERS:
        await ctx.send("❌ Invalid event type.")
        return

    import time
    multiplier = EVENT_MULTIPLIERS[event_type]
    
    # Calculate exact future expiration timestamp (Days -> Seconds)
    expiration_epoch = int(time.time()) + (duration_days * 86400)

    active_event["secret_code"] = secret_code.lower()
    active_event["final_points"] = round(base_points * multiplier)
    active_event["event_type"] = event_type
    active_event["end_timestamp"] = expiration_epoch
    active_event["claimed_users"] = set()

    # Create a human-readable format for the server confirmation
    display_time = datetime.fromtimestamp(expiration_epoch).strftime('%Y-%m-%d %H:%M UTC')

    embed = discord.Embed(title="📢 Long-Term Event Open!", color=discord.Color.orange())
    embed.description = (
        f"A multi-day check-in window has been established!\n\n"
        f"👉 Type **`!checkin {secret_code}`** to claim your reward.\n"
        f"🚨 **Deadline:** This code will remain active until `{display_time}`."
    )
    embed.add_field(name="Points Available", value=f"**{active_event['final_points']} DKP**")
    await ctx.send(embed=embed)


@bot.command(name="checkin")
async def checkin(ctx, code: str):
    """Processes a user check-in while verifying against the persistent epoch clock."""
    global active_event
    import time

    # 1. Enforce active check
    if not active_event["secret_code"]:
        await ctx.send("❌ There is no active check-in running right now.")
        return

    # 2. Check if the long-term date has expired
    if int(time.time()) > active_event["end_timestamp"]:
        expired_code = active_event["secret_code"]
        active_event["secret_code"] = None # Close the event natively
        await ctx.send(f"🛑 **Event Concluded!** The check-in window for `{expired_code}` closed automatically.")
        return

    # 3. Validate code accuracy
    if code.lower() != active_event["secret_code"]:
        await ctx.send("❌ Incorrect event secret code. Try again.")
        return
    
    u_id = str(ctx.author.id)
    if u_id in active_event["claimed_users"]:
        await ctx.send("⚠️ You have already logged your attendance for this specific code!")
        return

    # 4. Award points safely
    dkp_data = load_dkp_from_github()
    dkp_data[u_id] = dkp_data.get(u_id, 0) + active_event["final_points"]
    active_event["claimed_users"].add(u_id)
    
    save_dkp_to_github(dkp_data, f"Self-Checkin: {ctx.author.display_name} +{active_event['final_points']} DKP")
    await ctx.send(f"✅ **{ctx.author.display_name}**, your attendance has been logged! **+{active_event['final_points']} DKP**")


@bot.command(name="stopevent")
@commands.has_permissions(administrator=True)
async def stopevent(ctx):
    """Allows an administrator to manually kill the active timer early."""
    global active_event
    if not active_event["secret_code"]:
        await ctx.send("⚠️ There are no active events running to stop.")
        return
    
    closed_code = active_event["secret_code"]
    active_event["secret_code"] = None
    await ctx.send(f"🛑 Manual Override: Event code `{closed_code}` has been terminated by an administrator.")
    
@bot.command(name="helpmenu")
async def helpmenu(ctx):
    embed = discord.Embed(
        title="🤖 Clan DKP Bot Command Guide", 
        color=discord.Color.blue()
    )
    
    # Check if the user running the command is an Admin
    is_admin = ctx.author.guild_permissions.administrator

    if is_admin:
        admin_desc = (
            "**!startevent <type> <pts> <code>**\n"
            "Opens a 15-minute code check-in window.\n"
            "*Example:* `!startevent bossing 50 raid123`\n\n"
            "**!attendance <target> <type> <pts>**\n"
            "Instantly awards points to a target.\n"
            "*Targets:* @Member, @Role, or \"VC Name\"\n"
            "*Example:* `!attendance @Raiders bossing 40`\n\n"
            "**!award <@member> <pts> [reason]**\n"
            "Manually adds points to one specific player."
        )
        embed.add_field(name="🛡️ Admin Commands", value=admin_desc, inline=False)

    public_desc = (
        "**!checkin <code>**\n"
        "Claim your own points during an active event.\n"
        "*Example:* `!checkin raid123`\n\n"
        "**!spend <amount> <item>**\n"
        "Deducts points from your balance to buy a reward.\n\n"
        "**!dkp [@member]**\n"
        "Checks your point balance (or a tagged user's).\n\n"
        "**!leaderboard**\n"
        "Displays the top 10 highest-ranked clan members."
    )
    embed.add_field(name="👤 Member Commands", value=public_desc, inline=False)
    
    embed.set_footer(text="Valid event types: skilling, bossing, bingo, custom")
    await ctx.send(embed=embed)

# --- RUN ENGINE LOOP ---
if __name__ == "__main__":
    if DISCORD_TOKEN and GITHUB_TOKEN and GITHUB_REPO_NAME:
        t = threading.Thread(target=run_health_server, daemon=True)
        t.start()
        bot.run(DISCORD_TOKEN)
