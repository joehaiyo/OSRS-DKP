import discord
from discord.ext import commands
import json
import os
import logging
import threading
import asyncio
import time
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

active_event = {
    "secret_code": None,
    "final_points": 0,
    "event_type": None,
    "end_timestamp": 0,
    "claimed_users": set(),
    "registered_users": []
}

# FIX: Added global campaign registration dictionary to prevent stopevent NameError crashes
event_registrations = {
    "active_campaign_name": None,
    "discord_event_id": None,
    "signed_up_user_ids": []
}

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

ROSTER_FILE = "campaign_roster.json"

def load_roster_from_github():
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO_NAME)
        file_content = repo.get_contents(ROSTER_FILE)
        return json.loads(file_content.decoded_content.decode("utf-8"))
    except Exception:
        # Returns an empty structured database if the file does not exist yet
        return {"active_campaign_name": None, "discord_event_id": None, "signed_up_user_ids": []}

def save_roster_to_github(data):
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO_NAME)
        json_content = json.dumps(data, indent=4)
        try:
            file = repo.get_contents(ROSTER_FILE)
            repo.update_file(file.path, "Update campaign roster", json_content, file.sha)
        except Exception:
            repo.create_file(ROSTER_FILE, "Initialize campaign roster", json_content)
    except Exception as e:
        logging.error(f"CRITICAL Roster Sync Fail: {e}")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    global event_registrations
    logging.info(f"Bot online as {bot.user.name}")
    # Load persistent campaign data from GitHub memory
    event_registrations = load_roster_from_github()

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
    if isinstance(target, (discord.VoiceChannel, discord.Role)):
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

@bot.command(name="startevent")
@commands.has_permissions(administrator=True)
async def startevent(ctx, event_type: str, base_points: int, duration_days: int, secret_code: str):
    global active_event
    event_type = event_type.lower()
    if event_type not in EVENT_MULTIPLIERS:
        await ctx.send("❌ Invalid event type.")
        return
    multiplier = EVENT_MULTIPLIERS[event_type]
    expiration_epoch = int(time.time()) + (duration_days * 86400)
    active_event["secret_code"] = secret_code.lower()
    active_event["final_points"] = round(base_points * multiplier)
    active_event["event_type"] = event_type
    active_event["end_timestamp"] = expiration_epoch
    active_event["claimed_users"] = set()
    active_event["registered_users"] = []
    display_time = datetime.fromtimestamp(expiration_epoch).strftime('%Y-%m-%d %H:%M UTC')
    embed = discord.Embed(title="📢 Long-Term Event Open!", color=discord.Color.orange())
    embed.description = (
        f"👉 Type **`!registerevent`** to sign up for this event list.\n"
        f"👉 Type **`!checkin {secret_code}`** to claim points.\n"
        f"🚨 **Deadline:** Active until `{display_time}`."
    )
    embed.add_field(name="Points Available", value=f"**{active_event['final_points']} DKP**")
    await ctx.send(embed=embed)

@bot.command(name="registerevent")
async def registerevent(ctx):
    """Allows members to register their intent to join the currently running active event."""
    global active_event
    if not active_event["secret_code"]:
        await ctx.send("❌ There is no active event running to register for.")
        return
    u_id = str(ctx.author.id)
    if u_id in active_event["registered_users"]:
        await ctx.send("⚠️ You are already signed up for this active event roster!")
        return
    active_event["registered_users"].append(u_id)
    await ctx.send(f"✅ **{ctx.author.display_name}**, you are signed up for the current event roster!")

@bot.command(name="vieweventmembers")
@commands.has_permissions(administrator=True)
async def vieweventmembers(ctx):
    """Lists every user who registered for the active event roster."""
    global active_event
    if not active_event["secret_code"]:
        await ctx.send("⚠️ No active event running.")
        return
    user_ids = active_event["registered_users"]
    if not user_ids:
        await ctx.send(f"📋 The registration list for code `{active_event['secret_code']}` is empty.")
        return

    embed = discord.Embed(title=f"📋 Event Roster: {active_event['secret_code'].upper()}", color=discord.Color.blue())
    member_list_text = ""
    for idx, u_id in enumerate(user_ids, start=1):
        m = ctx.guild.get_member(int(u_id))
        name = m.display_name if m else f"User ID: {u_id}"
        member_list_text += f"**#{idx}** {name}\n"

    embed.description = member_list_text
    await ctx.send(embed=embed)

@bot.command(name="stopevent")
@commands.has_permissions(administrator=True)
async def stopevent(ctx):
    """Manually terminates active check-in events or signup campaigns, automatically deleting Discord calendar links."""
    global active_event, event_registrations

    # Case 1: Close active short/long-term check-in events
    if active_event["secret_code"]:
        closed_code = active_event["secret_code"]
        active_event["secret_code"] = None
        active_event["registered_users"] = []
        await ctx.send(f"🛑 Closed event code `{closed_code}`.")
        return

    # Case 2: Close active registration/signup campaigns and delete Discord Calendar Event
    if event_registrations["active_campaign_name"]:
        campaign = event_registrations["active_campaign_name"]
        event_id = event_registrations.get("discord_event_id")

        # Natively delete the scheduled calendar card from your Discord server sidebar
        if event_id:
            try:
                discord_event = ctx.guild.get_scheduled_event(int(event_id))
                if discord_event:
                    await discord_event.delete()
                    logging.info(f"Successfully deleted Discord scheduled event ID: {event_id}")
            except Exception as delete_err:
                logging.warning(f"Could not automatically delete Discord calendar card: {delete_err}")

        # Clear campaign memory variables completely clean
        event_registrations["active_campaign_name"] = None
        event_registrations["discord_event_id"] = None
        event_registrations["signed_up_user_ids"] = []
        
        await ctx.send(f"🛑 Closed signup campaign '{campaign}' and deleted its server calendar event.")
        return

    await ctx.send("⚠️ No active event or signup campaign running.")
    
@bot.command(name="checkin")
async def checkin(ctx, code: str):
    global active_event
    if not active_event["secret_code"]:
        await ctx.send("❌ No active check-in running.")
        return
    if time.time() > active_event["end_timestamp"]:
        active_event["secret_code"] = None
        active_event["registered_users"] = []
        await ctx.send("🛑 This event has expired.")
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
    save_dkp_to_github(dkp_data, f"Checkin: {ctx.author.display_name} +{active_event['final_points']}")
    await ctx.send(f"✅ Logged! **+{active_event['final_points']} DKP**")

@bot.command(name="buyrank")
async def buyrank(ctx, *, rank_keyword: str):
    rank_keyword = rank_keyword.lower().strip()
    if rank_keyword not in RANK_TIERS:
        await ctx.send("❌ Invalid rank chosen.")
        return
    rank_info = RANK_TIERS[rank_keyword]
    role = discord.utils.get(ctx.guild.roles, name=rank_info["name"])
    if not role:
        await ctx.send(f"❌ Server role '{rank_info['name']}' not found.")
        return
    if role in ctx.author.roles:
        await ctx.send("⚠️ You already have this rank!")
        return
    dkp_data = load_dkp_from_github()
    user_id = str(ctx.author.id)
    bal = dkp_data.get(user_id, 0)
    if bal < rank_info["cost"]:
        await ctx.send(f"❌ Need `{rank_info['cost']} DKP`. You have `{bal}`.")
        return
    dkp_data[user_id] = bal - rank_info["cost"]
    save_dkp_to_github(dkp_data, f"Rank Promo: {ctx.author.display_name} -> {rank_info['name']}")
    try:
        await ctx.author.add_roles(role)
        await ctx.send(f"⚔️ **{ctx.author.display_name}** promoted to **{rank_info['name']}**!")
    except discord.Forbidden:
        await ctx.send("⚠️ Hierarchy error: Drag Bot's role above scimitar roles.")

@bot.command(name="award")
@commands.has_permissions(administrator=True)
async def award(ctx, member: discord.Member, points: int, *, reason: str = "Significant Event"):
    dkp_data = load_dkp_from_github()
    dkp_data[str(member.id)] = dkp_data.get(str(member.id), 0) + points
    save_dkp_to_github(dkp_data, f"Award: {member.display_name} +{points} DKP ({reason})")
    await ctx.send(f"🏆 **{member.display_name}** received **{points} DKP**!")

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

@bot.command(name="helpmenu")
async def helpmenu(ctx):
    """Displays a filtered, up-to-date guide for all active DKP system commands."""
    embed = discord.Embed(
        title="⚔️ Old School RuneScape Clan DKP Bot Guide", 
        color=discord.Color.blue()
    )
    
    is_admin = ctx.author.guild_permissions.administrator

    if is_admin:
        admin_desc = (
            "**!startevent <type> <pts> <days> <code>**\n"
            "Opens a multi-day event window for signups and check-ins.\n\n"
            "**!vieweventmembers**\n"
            "Lists all members who signed up for the active event.\n\n"
            "**!stopevent**\n"
            "Manually terminates the current long-term event early.\n\n"
            "**!startsignup <days> <campaign_name>**\n"
            "Creates a Discord event and initializes a signup roster.\n\n"
            "**!viewroster**\n"
            "Displays the list of everyone registered for the campaign.\n\n"
            "**!fullroster**\n"
            "Generates a ranked list of all server members, including 0 pointers.\n\n"
            "**!attendance <target> <type> <pts>**\n"
            "Instantly awards points to an @Member, @Role, or \"VC Name\".\n\n"
            "**!award <@member> <pts> [reason]**\n"
            "Manually adds points to one specific player."
        )
        embed.add_field(name="🛡️ Admin Commands", value=admin_desc, inline=False)

    public_desc = (
        "**!registerevent**\n"
        "Signs you up for the currently active long-term event roster.\n\n"
        "**!checkin <code>**\n"
        "Claim your own event points using an active secret code.\n\n"
        "**!signdkp**\n"
        "Signs you up for the active clan campaign roster.\n\n"
        "**!buyrank <tier>**\n"
        "Spend DKP to unlock an OSRS Clan Rank role.\n"
        "*Example:* `!buyrank rune`\n\n"
        "**!dkp [@member]**\n"
        "Checks your point balance (or a tagged user's).\n\n"
        "**!leaderboard**\n"
        "Displays the top 10 highest-ranked clan members."
    )
    embed.add_field(name="👤 Member Commands", value=public_desc, inline=False)
    
    shop_list = "\n".join([f"• **{info['name']}** — `{info['cost']} DKP`" for info in RANK_TIERS.values()])
    embed.add_field(name="⚔️ Scimitar Rank Shop Prices", value=shop_list, inline=False)
    
    embed.set_footer(text="Valid event types: skilling, bossing, bingo, custom")
    await ctx.send(embed=embed)

# --- TRACKER DICTIONARY REGISTRATION ---
# Keeps track of player user IDs signed up for active campaigns
event_registrations = {
    "active_campaign_name": None,
    "discord_event_id": None,      # FIX: Stores the unique Discord event snowflake ID
    "signed_up_user_ids": []
}

@bot.command(name="startsignup")
@commands.has_permissions(administrator=True)
async def startsignup(ctx, days: int, *, campaign_name: str):
    """Launches a native Discord Scheduled Event and initializes a tracked registration list."""
    global event_registrations
    import datetime

    clean_name = campaign_name.strip()
    
    # 1. Update the local dictionary variable
    event_registrations["active_campaign_name"] = clean_name
    event_registrations["signed_up_user_ids"] = []

    # Calculate timestamps for the native Discord interface
    now = datetime.datetime.now(datetime.timezone.utc)
    future_start = now + datetime.timedelta(minutes=5)
    future_end = now + datetime.timedelta(days=days)

    # 2. Create the native Discord server calendar card event
    try:
        new_event = await ctx.guild.create_scheduled_event(
            name=f"⚔️ {clean_name}",
            description="Sign up now using '!signdkp' to reserve your clan track profile points!",
            start_time=future_start,
            end_time=future_end,
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location="OSRS Clan Event Ground"
        )
        # Save the event ID so it can be deleted later
        event_registrations["discord_event_id"] = new_event.id
    except Exception as event_err:
        event_registrations["discord_event_id"] = None
        logging.warning(f"Native calendar creation skipped: {event_err}")

    # 3. Permanently save this new campaign structure to campaign_roster.json on GitHub
    save_roster_to_github(event_registrations)

    # 4. Post a tracking card into the text channel
    embed = discord.Embed(title="📝 Clan Registration Open!", color=discord.Color.teal())
    embed.description = (
        f"A new campaign tracking roster has been opened for: **{clean_name}**\n\n"
        f"👉 Type **`!signdkp`** in this channel to add your profile to the sign-up list!\n"
        f"⏳ **Duration:** Roster collection closes in `{days} days`."
    )
    await ctx.send(embed=embed)

    # 2. Post a tracking card into the text channel
    embed = discord.Embed(title="📝 Clan Registration Open!", color=discord.Color.teal())
    embed.description = (
        f"A new campaign tracking roster has been opened for: **{clean_name}**\n\n"
        f"👉 Type **`!signdkp`** in this channel to add your profile to the sign-up list!\n"
        f"⏳ **Duration:** Roster collection closes in `{days} days`."
    )
    await ctx.send(embed=embed)

@bot.command(name="signdkp")
async def signdkp(ctx):
    """Allows a clan member to log their name onto the active event roster."""
    global event_registrations
    c_name = event_registrations["active_campaign_name"]
    
    if not c_name:
        await ctx.send("❌ There is no active sign-up campaign running right now.")
        return

    u_id = str(ctx.author.id)
    if u_id in event_registrations["signed_up_user_ids"]:
        await ctx.send("⚠️ You are already signed up on this campaign's roster!")
        return

    # 1. Add player to the live memory tracker list
    event_registrations["signed_up_user_ids"].append(u_id)

    # 2. Permanently update campaign_roster.json on GitHub with the new name array
    save_roster_to_github(event_registrations)

    # 3. Log it into your human-readable historical audit logs on GitHub too
    dkp_data = load_dkp_from_github()
    save_dkp_to_github(dkp_data, f"Roster Signup: {ctx.author.display_name} joined campaign '{c_name}'")
    
    await ctx.send(f"✅ **{ctx.author.display_name}**, you have successfully signed up for **{c_name}**!")

@bot.command(name="viewroster")
@commands.has_permissions(administrator=True)
async def viewroster(ctx):
    """Retrieves and lists every individual user who has successfully registered."""
    global event_registrations
    c_name = event_registrations["active_campaign_name"]
    
    if not c_name:
        await ctx.send("⚠️ No active campaign roster found.")
        return

    user_ids = event_registrations["signed_up_user_ids"]
    if not user_ids:
        await ctx.send(f"📋 The roster for **{c_name}** is currently empty. No users have typed `!signdkp` yet.")
        return

    embed = discord.Embed(title=f"📋 Roster Sheet: {c_name}", color=discord.Color.purple())
    member_list_text = ""
    
    for idx, u_id in enumerate(user_ids, start=1):
        m = ctx.guild.get_member(int(u_id))
        name = m.display_name if m else f"User ID: {u_id}"
        member_list_text += f"**#{idx}** {name}\n"

    embed.description = member_list_text
    embed.set_footer(text=f"Total Registrations: {len(user_ids)} clan members")
    await ctx.send(embed=embed)

@bot.command(name="fullroster")
@commands.has_permissions(administrator=True)
async def fullroster(ctx):
    """Generates a complete list of all server members ranked by points, including 0 pointers."""
    dkp_data = load_dkp_from_github()
    roster_list = []

    # 1. Loop through every single human member in the server
    for member in ctx.guild.members:
        if not member.bot:
            user_id = str(member.id)
            # Fetch points, default to 0 if they aren't in the database
            points = dkp_data.get(user_id, 0)
            roster_list.append((member.display_name, points))

    # 2. Sort the roster from highest points to lowest points
    roster_list.sort(key=lambda x: x[1], reverse=True)

    # 3. Split the list into small chunks so it doesn't break Discord text limits
    chunk_size = 15
    for i in range(0, len(roster_list), chunk_size):
        chunk = roster_list[i:i+chunk_size]
        
        embed = discord.Embed(
            title=f"📋 Full Server DKP Roster (Part {i//chunk_size + 1})", 
            color=discord.Color.dark_purple()
        )
        
        desc = ""
        for rank, (name, points) in enumerate(chunk, start=i+1):
            desc += f"**#{rank}** {name} — `{points} DKP`\n"
            
        embed.description = desc
        await ctx.send(embed=embed)

if __name__ == "__main__":
    if DISCORD_TOKEN and GITHUB_TOKEN and GITHUB_REPO_NAME:
        t = threading.Thread(target=run_health_server, daemon=True)
        t.start()
        bot.run(DISCORD_TOKEN)
         user_ids = active_event["registered_users"]
    if not user_ids:
        await ctx.send(f"📋 The registration list for code `{active_event['secret_code']}` is empty.")
        return

    embed = discord.Embed(title=f"📋 Event Roster: {active_event['secret_code'].upper()}", color=discord.Color.blue())
    member_list_text = ""
    for idx, u_id in enumerate(user_ids, start=1):
        m = ctx.guild.get_member(int(u_id))
        name = m.display_name if m else f"User ID: {u_id}"
        member_list_text += f"**#{idx}** {name}\n"
    embed.description = member_list_text
    await ctx.send(embed=embed)

# --- COMMANDS ENGINE CONTINUED ---

@bot.command(name="stopevent")
@commands.has_permissions(administrator=True)
async def stopevent(ctx):
    """Manually terminates active check-in events or signup campaigns, automatically deleting Discord calendar links."""
    global active_event, event_registrations

    # Case 1: Close active short/long-term check-in events
    if active_event["secret_code"]:
        closed_code = active_event["secret_code"]
        active_event["secret_code"] = None
        active_event["registered_users"] = []
        await ctx.send(f"🛑 Closed event code `{closed_code}`.")
        return

    # Case 2: Close active registration/signup campaigns and delete Discord Calendar Event
    if event_registrations["active_campaign_name"]:
        campaign = event_registrations["active_campaign_name"]
        event_id = event_registrations.get("discord_event_id")

        # Natively delete the scheduled calendar card from your Discord server sidebar
        if event_id:
            try:
                discord_event = ctx.guild.get_scheduled_event(int(event_id))
                if discord_event:
                    await discord_event.delete()
                    logging.info(f"Successfully deleted Discord scheduled event ID: {event_id}")
            except Exception as delete_err:
                logging.warning(f"Could not automatically delete Discord calendar card: {delete_err}")

        # WIPE THE STORAGE: Reset memory variables clean
        event_registrations["active_campaign_name"] = None
        event_registrations["discord_event_id"] = None
        event_registrations["signed_up_user_ids"] = []
        
        # WIPE GITHUB FILE: Force-save the blank structured dict back onto campaign_roster.json on GitHub
        save_roster_to_github(event_registrations)
        
        await ctx.send(f"🛑 Closed signup campaign '{campaign}' and cleared your GitHub roster database.")
        return

    await ctx.send("⚠️ No active event or signup campaign running.")
    
@bot.command(name="checkin")
async def checkin(ctx, code: str):
    global active_event
    if not active_event["secret_code"]:
        await ctx.send("❌ No active check-in running.")
        return
    if time.time() > active_event["end_timestamp"]:
        active_event["secret_code"] = None
        active_event["registered_users"] = []
        await ctx.send("🛑 This event has expired.")
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
    save_dkp_to_github(dkp_data, f"Checkin: {ctx.author.display_name} +{active_event['final_points']}")
    await ctx.send(f"✅ Logged! **+{active_event['final_points']} DKP**")

@bot.command(name="buyrank")
async def buyrank(ctx, *, rank_keyword: str):
    rank_keyword = rank_keyword.lower().strip()
    if rank_keyword not in RANK_TIERS:
        await ctx.send("❌ Invalid rank chosen.")
        return
    rank_info = RANK_TIERS[rank_keyword]
    role = discord.utils.get(ctx.guild.roles, name=rank_info["name"])
    if not role:
        await ctx.send(f"❌ Server role '{rank_info['name']}' not found.")
        return
    if role in ctx.author.roles:
        await ctx.send("⚠️ You already have this rank!")
        return

    dkp_data = load_dkp_from_github()
    user_id = str(ctx.author.id)
    bal = dkp_data.get(user_id, 0)
    if bal < rank_info["cost"]:
        await ctx.send(f"❌ Need `{rank_info['cost']} DKP`. You have `{bal}`.")
        return

    dkp_data[user_id] = bal - rank_info["cost"]
    save_dkp_to_github(dkp_data, f"Rank Promo: {ctx.author.display_name} -> {rank_info['name']}")
    try:
        await ctx.author.add_roles(role)
        await ctx.send(f"⚔️ **{ctx.author.display_name}** promoted to **{rank_info['name']}**!")
    except discord.Forbidden:
        await ctx.send("⚠️ Hierarchy error: Drag Bot's role above scimitar roles.")

@bot.command(name="award")
@commands.has_permissions(administrator=True)
async def award(ctx, member: discord.Member, points: int, *, reason: str = "Significant Event"):
    dkp_data = load_dkp_from_github()
    dkp_data[str(member.id)] = dkp_data.get(str(member.id), 0) + points
    save_dkp_to_github(dkp_data, f"Award: {member.display_name} +{points} DKP ({reason})")
    await ctx.send(f"🏆 **{member.display_name}** received **{points} DKP**!")

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

@bot.command(name="helpmenu")
async def helpmenu(ctx):
    """Displays a filtered, up-to-date guide for all active DKP system commands."""
    embed = discord.Embed(title="⚔️ Old School RuneScape Clan DKP Bot Guide", color=discord.Color.blue())
    is_admin = ctx.author.guild_permissions.administrator
    if is_admin:
        admin_desc = (
            "**!startevent <type> <pts> <days> <code>**\nOpens a multi-day event window.\n\n"
            "**!vieweventmembers**\nLists members who signed up for the active event.\n\n"
            "**!stopevent**\nTermulates the current event/campaign early.\n\n"
            "**!startsignup <days> <campaign_name>**\nInitializes a signup roster.\n\n"
            "**!viewroster**\nDisplays everyone registered for the campaign.\n\n"
            "**!fullroster**\nGenerates a ranked list of all members, including 0 pointers.\n\n"
            "**!attendance <target> <type> <pts>**\nAwards points to an @Member, @Role, or \"VC Name\".\n\n"
            "**!award <@member> <pts> [reason]**\nManually adds points to a specific player."
        )
        embed.add_field(name="🛡️ Admin Commands", value=admin_desc, inline=False)
    public_desc = (
        "**!registerevent**\nSigns you up for the running long-term event roster.\n\n"
        "**!checkin <code>**\nClaim event points using an active secret code.\n\n"
        "**!signdkp**\nSigns you up for the active clan campaign roster.\n\n"
        "**!buyrank <tier>**\nSpend DKP to unlock an OSRS Clan Rank role.\n\n"
        "**!dkp [@member]**\nChecks point balances.\n\n"
        "**!leaderboard**\nDisplays the top 10 clan point earners."
    )
    embed.add_field(name="👤 Member Commands", value=public_desc, inline=False)
    shop_list = "\n".join([f"• **{info['name']}** — `{info['cost']} DKP`" for info in RANK_TIERS.values()])
    embed.add_field(name="⚔️ Scimitar Rank Shop Prices", value=shop_list, inline=False)
    embed.set_footer(text="Valid event types: skilling, bossing, bingo, custom")
    await ctx.send(embed=embed)
@bot.command(name="startsignup")
@commands.has_permissions(administrator=True)
async def startsignup(ctx, days: int, *, campaign_name: str):
    """Launches a native Discord Scheduled Event and initializes a tracked registration list."""
    global event_registrations
    import datetime

    clean_name = campaign_name.strip()
    event_registrations["active_campaign_name"] = clean_name
    event_registrations["signed_up_user_ids"] = []

    # Calculate timestamps for the native Discord interface
    now = datetime.datetime.now(datetime.timezone.utc)
    future_start = now + datetime.timedelta(minutes=5)
    future_end = now + datetime.timedelta(days=days)

    # 1. Create a native Discord server calendar card event
    try:
        new_event = await ctx.guild.create_scheduled_event(
            name=f"⚔️ {clean_name}",
            description="Sign up now using '!signdkp' to reserve your clan track profile points!",
            start_time=future_start,
            end_time=future_end,
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location="OSRS Clan Event Ground"
        )
        # Save the event ID so it can be deleted by !stopevent later
        event_registrations["discord_event_id"] = new_event.id
    except Exception as event_err:
        event_registrations["discord_event_id"] = None
        logging.warning(f"Native calendar creation skipped: {event_err}")

    # 2. Post a tracking card into the text channel
    embed = discord.Embed(title="📝 Clan Registration Open!", color=discord.Color.teal())
    embed.description = (
        f"A new campaign tracking roster has been opened for: **{clean_name}**\n\n"
        f"👉 Type **`!signdkp`** in this channel to add your profile to the sign-up list!\n"
        f"⏳ **Duration:** Roster collection closes in `{days} days`."
    )
    await ctx.send(embed=embed)

@bot.command(name="signdkp")
async def signdkp(ctx):
    """Allows a clan member to log their name onto the active event roster."""
    global event_registrations
    c_name = event_registrations["active_campaign_name"]
    if not c_name:
        await ctx.send("❌ There is no active sign-up campaign running right now.")
        return

    u_id = str(ctx.author.id)
    if u_id in event_registrations["signed_up_user_ids"]:
        await ctx.send("⚠️ You are already signed up on this campaign's roster!")
        return

    # Add to temporary storage list
    event_registrations["signed_up_user_ids"].append(u_id)

    # Save the roster array modification down to a clean line in your log tracking histories
    dkp_data = load_dkp_from_github()
    save_dkp_to_github(dkp_data, f"Roster Signup: {ctx.author.display_name} joined campaign '{c_name}'")
    await ctx.send(f"✅ **{ctx.author.display_name}**, you have successfully signed up for **{c_name}**!")

@bot.command(name="viewroster")
@commands.has_permissions(administrator=True)
async def viewroster(ctx):
    """Retrieves and lists every individual user who has successfully registered."""
    c_name = event_registrations["active_campaign_name"]
    if not c_name:
        await ctx.send("⚠️ No active campaign roster found.")
        return

    user_ids = event_registrations["signed_up_user_ids"]
    if not user_ids:
        await ctx.send(f"📋 The roster for **{c_name}** is currently empty. No users have typed `!signdkp` yet.")
        return

    embed = discord.Embed(title=f"📋 Roster Sheet: {c_name}", color=discord.Color.purple())
    member_list_text = ""
    # Parse numerical handles into display profiles
    for idx, u_id in enumerate(user_ids, start=1):
        m = ctx.guild.get_member(int(u_id))
        name = m.display_name if m else f"User ID: {u_id}"
        member_list_text += f"**#{idx}** {name}\n"
    embed.description = member_list_text
    embed.set_footer(text=f"Total Registrations: {len(user_ids)} clan members")
    await ctx.send(embed=embed)

@bot.command(name="fullroster")
@commands.has_permissions(administrator=True)
async def fullroster(ctx):
    """Generates a complete list of all server members ranked by points, including 0 pointers."""
    dkp_data = load_dkp_from_github()
    roster_list = []

    # 1. Loop through every single human member in the server
    for member in ctx.guild.members:
        if not member.bot:
            user_id = str(member.id)
            points = dkp_data.get(user_id, 0)
            roster_list.append((member.display_name, points))

    # 2. Sort the roster from highest points to lowest points
    roster_list.sort(key=lambda x: x[1], reverse=True)

    # 3. Split the list into small chunks so it doesn't break Discord text limits
    chunk_size = 15
    for i in range(0, len(roster_list), chunk_size):
        chunk = roster_list[i:i+chunk_size]
        embed = discord.Embed(
            title=f"📋 Full Server DKP Roster (Part {i//chunk_size + 1})",
            color=discord.Color.dark_purple()
        )
        desc = ""
        for rank, (name, points) in enumerate(chunk, start=i+1):
            desc += f"**#{rank}** {name} — `{points} DKP`\n"
        embed.description = desc
        await ctx.send(embed=embed)

# --- ENGINE SHUTDOWN RUN CODES ---
if __name__ == "__main__":
    if DISCORD_TOKEN and GITHUB_TOKEN and GITHUB_REPO_NAME:
        t = threading.Thread(target=run_health_server, daemon=True)
        t.start()
        bot.run(DISCORD_TOKEN)
