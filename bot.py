import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import aiohttp
import pytesseract
from PIL import Image, ImageFilter, ImageEnhance
import io
import re
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DB_PATH = 'config.db'

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id    INTEGER PRIMARY KEY,
                htb_channel_id INTEGER,
                category_id    INTEGER
            )
        ''')
        await db.commit()


async def get_guild_config(guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT htb_channel_id, category_id FROM guild_config WHERE guild_id = ?',
            (guild_id,)
        ) as cursor:
            return await cursor.fetchone()


async def save_guild_config(guild_id: int, htb_channel_id: int, category_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT OR REPLACE INTO guild_config (guild_id, htb_channel_id, category_id) VALUES (?, ?, ?)',
            (guild_id, htb_channel_id, category_id)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Setup wizard views
# ---------------------------------------------------------------------------

class SetupStep1(discord.ui.View):
    """Step 1: pick the HTB Updates channel."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Select the channel where HTB posts badges..."
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        htb_channel = select.values[0]
        view = SetupStep2(htb_channel_id=htb_channel.id)
        await interaction.response.edit_message(
            content=(
                f"**HTB Season Bot Setup**\n\n"
                f"HTB badge channel: {htb_channel.mention}\n\n"
                f"**Step 2 of 2:** Select the **category** where solved-machine channels will be created:"
            ),
            view=view
        )


class SetupStep2(discord.ui.View):
    """Step 2: pick the category for machine channels."""

    def __init__(self, htb_channel_id: int):
        super().__init__(timeout=120)
        self.htb_channel_id = htb_channel_id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.category],
        placeholder="Select the category for machine channels..."
    )
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        category = select.values[0]
        await save_guild_config(interaction.guild_id, self.htb_channel_id, category.id)
        await interaction.response.edit_message(
            content=(
                f"**HTB Season Bot Setup — Complete!**\n\n"
                f"Watching for HTB root badges and creating channels under **{category.name}**.\n"
                f"Everyone will see new channels, but only root solvers can read and post."
            ),
            view=None
        )


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="setup", description="Configure the HTB Season Bot for this server")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    view = SetupStep1()
    await interaction.response.send_message(
        "**HTB Season Bot Setup**\n\n**Step 1 of 2:** Select the channel where HTB posts badge notifications:",
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="invite", description="Get the link to invite this bot to another server")
async def invite(interaction: discord.Interaction):
    permissions = discord.Permissions(
        manage_channels=True,
        manage_roles=True,
        view_channel=True,
        send_messages=True,
        read_message_history=True
    )
    url = discord.utils.oauth_url(bot.user.id, permissions=permissions)
    await interaction.response.send_message(
        f"**Invite HTB Season Bot to your server:**\n{url}",
        ephemeral=True
    )


@bot.tree.command(name="htb-status", description="Show the current HTB bot configuration for this server")
@app_commands.checks.has_permissions(administrator=True)
async def htb_status(interaction: discord.Interaction):
    row = await get_guild_config(interaction.guild_id)
    if not row:
        await interaction.response.send_message(
            "This server has not been configured yet. Run `/setup` to get started.",
            ephemeral=True
        )
        return

    htb_channel_id, category_id = row
    htb_channel = interaction.guild.get_channel(htb_channel_id)
    category = interaction.guild.get_channel(category_id)

    await interaction.response.send_message(
        f"**HTB Bot Configuration**\n"
        f"Badge channel: {htb_channel.mention if htb_channel else f'Unknown ({htb_channel_id})'}\n"
        f"Machine category: **{category.name if category else f'Unknown ({category_id})'}**",
        ephemeral=True
    )


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def preprocess_image(image: Image.Image) -> Image.Image:
    """Improve OCR accuracy on the dark HTB badge."""
    image = image.convert('L')                          # greyscale
    image = ImageEnhance.Contrast(image).enhance(2.5)  # boost contrast
    image = image.filter(ImageFilter.SHARPEN)
    return image


def parse_badge(text: str):
    """
    Return (discord_username, machine_name) for a root completion,
    or (None, None) if the badge is not a root solve or couldn't be parsed.
    """
    # Must contain "root" — ignore user-flag badges
    if not re.search(r'\broot\b', text, re.IGNORECASE):
        return None, None

    machine_match = re.search(r'just got root on\s+(\S+)', text, re.IGNORECASE)
    aka_match = re.search(r'AKA\s+(\S+)', text, re.IGNORECASE)

    if not machine_match or not aka_match:
        return None, None

    machine_name = machine_match.group(1).strip().lower()
    discord_username = aka_match.group(1).strip().lower()

    # Strip any stray punctuation OCR may have added
    machine_name = re.sub(r'[^a-z0-9_-]', '', machine_name)
    discord_username = re.sub(r'[^a-z0-9_.\-]', '', discord_username)

    return discord_username, machine_name


# ---------------------------------------------------------------------------
# Badge processing
# ---------------------------------------------------------------------------

async def process_badge(message: discord.Message, attachment: discord.Attachment, category_id: int):
    # Download image
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                return
            image_data = await resp.read()

    image = Image.open(io.BytesIO(image_data))
    processed = preprocess_image(image)
    raw_text = pytesseract.image_to_string(processed)

    discord_username, machine_name = parse_badge(raw_text)
    if not discord_username or not machine_name:
        return

    guild = message.guild

    # Find member — match on name or display name
    member = discord.utils.find(
        lambda m: m.name.lower() == discord_username or m.display_name.lower() == discord_username,
        guild.members
    )
    if not member:
        return

    category = guild.get_channel(category_id)
    if not category or not isinstance(category, discord.CategoryChannel):
        return

    # Find existing channel or create it
    channel = discord.utils.find(
        lambda c: c.name == machine_name and c.category_id == category_id,
        guild.channels
    )

    solver_perms = discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=True,
        send_messages=True
    )
    everyone_perms = discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=False,
        send_messages=False
    )

    if channel is None:
        channel = await guild.create_text_channel(
            name=machine_name,
            category=category,
            overwrites={
                guild.default_role: everyone_perms,
                member: solver_perms
            }
        )
        await channel.send(
            f"{member.mention} just got root on **{machine_name.title()}** and unlocked this channel!"
        )
    else:
        await channel.set_permissions(member, overwrite=solver_perms)
        await channel.send(
            f"{member.mention} just got root on **{machine_name.title()}**!"
        )


# ---------------------------------------------------------------------------
# Event: watch for HTB badge messages
# ---------------------------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    # Only care about webhook/bot messages with image attachments
    if not message.author.bot:
        return
    if not message.attachments:
        return
    if not message.guild:
        return

    row = await get_guild_config(message.guild.id)
    if not row:
        return

    htb_channel_id, category_id = row

    if message.channel.id != htb_channel_id:
        return

    for attachment in message.attachments:
        if attachment.content_type and 'image' in attachment.content_type:
            await process_badge(message, attachment, category_id)

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print(f'Serving {len(bot.guilds)} server(s)')


bot.run(TOKEN)
