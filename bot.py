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
ROSTER_FILE = "campaign_roster.json"

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
    logging.info(f"Starting Health Server on port {PORT}...")
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
        # Returns an empty database dictionary designed to hold multiple events concurrently
        return {}

def save_roster_to_github(data):
    try:
        g = Github(GITHUB_TOKEN)
        repo = g.get_repo(GITHUB_REPO_NAME)
        json_content = json.dumps(data, indent=4)
        try:
            file = repo.get_contents(ROSTER_FILE)
            repo.update_file(file.path, "Update concurrent campaign rosters", json_content, file.sha)
        except Exception:
            repo.create_file(ROSTER_FILE, "Initialize concurrent campaign rosters", json_content)
    except Exception as e:
        logging.error(f"CRITICAL Roster Sync Fail: {e}")

# 1. Initialize the master configuration blueprint object
intents = discord.Intents.default()

# 2. Add individual flag properties to that blueprint object
intents.message_content = True
intents.members = True
intents.voice_states = True

# 3. Pass the completed blueprint object into the bot instance creator
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    global event_registrations
    logging.info(f"Bot online as {bot.user.name}")
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

@bot.command(name="startsignup")
@commands.has_permissions(administrator=True)
async def startsignup(ctx, days: int, *, campaign_name: str):
    """Initializes an independent, live-updating signup campaign roster."""
    global event_registrations
    import datetime
    
    clean_name = campaign_name.strip().lower()
    
    # Reload active rosters to pull other live events
    event_registrations = load_roster_from_github()
    
    if clean_name in event_registrations:
        await ctx.send(f"⚠️ A campaign named '{campaign_name}' is already running!")
        return

    # Calculate timestamps for the native Discord interface
    now = datetime.datetime.now(datetime.timezone.utc)
    future_start = now + datetime.timedelta(minutes=5)
    future_end = now + datetime.timedelta(days=days)

    # 1. Create a native Discord server calendar card event
    event_id = None
    try:
        new_event = await ctx.guild.create_scheduled_event(
            name=f"⚔️ {campaign_name}",
            description=f"Sign up using '!signdkp {campaign_name}'!",
            start_time=future_start,
            end_time=future_end,
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location="OSRS Clan Event Ground"
        )
        event_id = new_event.id
    except Exception as event_err:
        logging.warning(f"Native calendar creation skipped: {event_err}")

    # 2. Build the initial empty card embed layout
    embed = discord.Embed(title=f"📝 Campaign Roster: {campaign_name}", color=discord.Color.teal())
    embed.description = f"👉 Type **`!signdkp {campaign_name}`** in this channel to add your name!"
    embed.add_field(name="👥 Signed Up Members (0)", value="* Roster is currently empty *", inline=False)
    embed.set_footer(text=f"⏳ Collection window closes in {days} days.")
    
    sent_card = await ctx.send(embed=embed)

    # 3. Store this unique campaign nested inside the master file map structure
    event_registrations[clean_name] = {
        "campaign_display_name": campaign_name,
        "discord_event_id": event_id,
        "signup_message_id": sent_card.id,
        "signed_up_user_ids": []
    }
    save_roster_to_github(event_registrations)

@bot.command(name="signdkp")
async def signdkp(ctx, *, campaign_name: str):
    """Logs a member into a specific campaign roster and dynamically updates its channel card."""
    global event_registrations
    clean_name = campaign_name.strip().lower()
    
    event_registrations = load_roster_from_github()
    
    if clean_name not in event_registrations:
        await ctx.send(f"❌ No active campaign found named '{campaign_name}'.")
        return

    campaign_data = event_registrations[clean_name]
    u_id = str(ctx.author.id)
    
    if u_id in campaign_data["signed_up_user_ids"]:
        await ctx.send(f"⚠️ You are already signed up for the '{campaign_data['campaign_display_name']}' roster!")
        return

    # 1. Look for a matching custom tracking role on the server sidebar
    roster_role = discord.utils.get(ctx.guild.roles, name=campaign_data["campaign_display_name"])

    campaign_data["signed_up_user_ids"].append(u_id)
    save_roster_to_github(event_registrations)

    dkp_data = load_dkp_from_github()
    save_dkp_to_github(dkp_data, f"Roster Signup: {ctx.author.display_name} joined '{campaign_data['campaign_display_name']}'")

    # Rebuild roster list nicknames
    roster_names = []
    for member_id in campaign_data["signed_up_user_ids"]:
        m = ctx.guild.get_member(int(member_id))
        if m:
            roster_names.append(f"• {m.display_name}")

    # 2. Dynamically locate and edit the specific channel chat card embed
    msg_id = campaign_data.get("signup_message_id")
    if msg_id:
        try:
            target_message = await ctx.channel.fetch_message(int(msg_id))
            if target_message and target_message.embeds:
                old_embed = target_message.embeds[0]
                new_embed = discord.Embed(title=old_embed.title, color=old_embed.color, description=old_embed.description)
                new_embed.set_footer(text=old_embed.footer.text)
                
                roster_value = "\n".join(roster_names) if roster_names else "* Roster is currently empty *"
                new_embed.add_field(name=f"👥 Signed Up Members ({len(roster_names)})", value=roster_value, inline=False)
                
                await target_message.edit(embed=new_embed)
        except Exception as embed_err:
            logging.warning(f"Could not update target embed card: {embed_err}")

    # 3. Grant the role if an admin created a role with the exact name of the event
    if roster_role:
        try:
            await ctx.author.add_roles(roster_role)
        except discord.Forbidden:
            pass

    await ctx.send(f"✅ **{ctx.author.display_name}**, you signed up for **{campaign_data['campaign_display_name']}**!")

@bot.command(name="viewroster")
@commands.has_permissions(administrator=True)
async def viewroster(ctx, *, campaign_name: str):
    """Retrieves and lists every individual user registered for a targeted campaign."""
    global event_registrations
    clean_name = campaign_name.strip().lower()
    
    event_registrations = load_roster_from_github()
    
    if clean_name not in event_registrations:
        await ctx.send(f"❌ No active campaign roster found named '{campaign_name}'.")
        return

    campaign_data = event_registrations[clean_name]
    user_ids = campaign_data["signed_up_user_ids"]
    
    if not user_ids:
        await ctx.send(f"📋 The roster for **{campaign_data['campaign_display_name']}** is empty.")
        return

    embed = discord.Embed(title=f"📋 Roster Sheet: {campaign_data['campaign_display_name']}", color=discord.Color.purple())
    member_list_text = ""
    for idx, u_id in enumerate(user_ids, start=1):
        m = ctx.guild.get_member(int(u_id))
        name = m.display_name if m else f"User ID: {u_id}"
        member_list_text += f"**#{idx}** {name}\n"

    embed.description = member_list_text
    embed.set_footer(text=f"Total Registrations: {len(user_ids)} clan members")
    await ctx.send(embed=embed)

@bot.command(name="stopevent")
@commands.has_permissions(administrator=True)
async def stopevent(ctx, *, target_name: str = None):
    """Terminates a specific check-in code window, or closes a targeted multi-day campaign."""
    global active_event, event_registrations
    
    if not target_name:
        await ctx.send("❌ Please specify what you want to stop!\n*Usage:* `!stopevent <code>` or `!stopevent <campaign_name>`")
        return

    clean_target = target_name.strip().lower()

    # Case 1: Close active check-in codes matching the target input
    if active_event["secret_code"] and active_event["secret_code"] == clean_target:
        closed_code = active_event["secret_code"]
        active_event["secret_code"] = None
        active_event["registered_users"] = []
        await ctx.send(f"🛑 Closed check-in event code `{closed_code}`.")
        return

    # Case 2: Close active registration campaign matching the target input name
    event_registrations = load_roster_from_github()
    if clean_target in event_registrations:
        campaign_data = event_registrations[clean_target]
        display_name = campaign_data["campaign_display_name"]
        event_id = campaign_data.get("discord_event_id")

        # Delete native calendar card
        if event_id:
            try:
                discord_event = ctx.guild.get_scheduled_event(int(event_id))
                if discord_event:
                    await discord_event.delete()
            except Exception as delete_err:
                logging.warning(f"Could not delete calendar card: {delete_err}")

        # Strip custom matching side-panel role from participants if it exists
        roster_role = discord.utils.get(ctx.guild.roles, name=display_name)
        if roster_role:
            try:
                for member_id in campaign_data["signed_up_user_ids"]:
                    member = ctx.guild.get_member(int(member_id))
                    if member and roster_role in member.roles:
                        await member.remove_roles(roster_role)
            except Exception as role_err:
                logging.warning(f"Could not remove event roles: {role_err}")

        # Remove only this single campaign from the database and save the rest
        del event_registrations[clean_target]
        save_roster_to_github(event_registrations)
        
        await ctx.send(f"🛑 Closed signup campaign '{display_name}' and cleared its associated lists and roles.")
        return

    await ctx.send(f"⚠️ No active check-in code or campaign roster found matching '{target_name}'.")

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

@bot.command(name="fullroster")
@commands.has_permissions(administrator=True)
async def fullroster(ctx):
    dkp_data = load_dkp_from_github()
    roster_list = []
    for member in ctx.guild.members:
        if not member.bot:
            user_id = str(member.id)
            points = dkp_data.get(user_id, 0)
            roster_list.append((member.display_name, points))
    roster_list.sort(key=lambda x: x[1], reverse=True)
    chunk_size = 15
    for i in range(0, len(roster_list), chunk_size):
        chunk = roster_list[i:i+chunk_size]
        embed = discord.Embed(title=f"📋 Full Server DKP Roster (Part {i//chunk_size + 1})", color=discord.Color.dark_purple())
        desc = ""
        for rank, (name, points) in enumerate(chunk, start=i+1):
            desc += f"**#{rank}** {name} — `{points} DKP`\n"
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
            "Opens a multi-day event window for self check-ins.\n\n"
            "**!vieweventmembers**\n"
            "Lists all members who signed up for the active check-in event.\n\n"
            "**!startsignup <days> <campaign_name>**\n"
            "Launches an independent, live-updating signup card.\n"
            "*Example:* `!startsignup 7 Bingo`\n\n"
            "**!viewroster <campaign_name>**\n"
            "Displays the active signup roster for a targeted campaign.\n"
            "*Example:* `!viewroster Bingo`\n\n"
            "**!stopevent <name_or_code>**\n"
            "Closes an active check-in code OR terminates a campaign roster.\n"
            "*Example:* `!stopevent Bingo`\n\n"
            "**!fullroster**\n"
            "Generates a ranked list of all server members, including 0 pointers.\n\n"
            "**!attendance <target> <type> <pts>**\n"
            "Instantly awards points to an @Member, @Role, or \"VC Name\".\n\n"
            "**!award <@member> <pts> [reason]**\n"
            "Manually adds points to one specific player."
        )
        embed.add_field(name="🛡️ Admin Commands", value=admin_desc, inline=False)

    public_desc = (
        "**!signdkp <campaign_name>**\n"
        "Signs you up for a specific active campaign roster.\n"
        "*Example:* `!signdkp Bingo`\n\n"
        "**!registerevent**\n"
        "Signs you up for the running long-term event check-in list.\n\n"
        "**!checkin <code>**\n"
        "Claim your own event points using an active secret code.\n\n"
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
    
def start_bot_thread():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    if DISCORD_TOKEN and GITHUB_TOKEN and GITHUB_REPO_NAME:
        # 1. Start the health server IMMEDIATELY on the main thread so port 8080 opens instantly
        # 2. Push the heavy blocking Discord loop onto a safe background worker thread
        logging.info("Initializing reverse-boot architecture...")
        bot_worker = threading.Thread(target=start_bot_thread, daemon=True)
        bot_worker.start()
        
        run_health_server()
