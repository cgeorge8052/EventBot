"""
Photo contest Discord bot.

Each round runs in three phases:
  1. VOTING  - as soon as a round starts (including right when the bot
     launches, if nothing is running), the bot posts a few random candidate
     themes (recent winners excluded) and members vote by reacting with the
     matching number emoji. Voting stays open for config.VOTE_WINDOW_MINUTES
     (default 60 = "first hour").
  2. ACTIVE  - the theme with the most votes is announced and members post
     photos in the contest channel. Any message there with an image
     attachment is tracked as an entry.
  3. COMPLETE - at the end of the round (end of day or end of month,
     depending on config.CONTEST_PERIOD), the bot counts heart reactions
     on every entry (self-reacts don't count) and declares whoever has the
     most as the winner (ties become co-winners), then a new round starts
     automatically at the top of the next period.

Run: python bot.py
Configure via environment variables / a .env file - see .env.example.
"""

import logging
import random
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import contest_manager as cm
from database import Contest, Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("contest_bot")

intents = discord.Intents.default()
intents.message_content = True  # to notice image-attachment posts
intents.members = True  # to assign/remove the winner role
intents.reactions = True

bot = commands.Bot(command_prefix="!contest-", intents=intents)
db = Database(config.DATABASE_PATH)

NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_image_attachment(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
    )


def is_admin(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions if interaction.guild else None
    return bool(perms and (perms.administrator or perms.manage_guild))


async def get_contest_channel() -> discord.TextChannel | None:
    channel = bot.get_channel(config.CONTEST_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(config.CONTEST_CHANNEL_ID)
        except discord.DiscordException:
            log.error("Could not find contest channel %s", config.CONTEST_CHANNEL_ID)
            return None
    return channel


async def compute_entry_heart_counts(contest_id: int) -> dict[str, tuple[str, int]]:
    """message_id -> (author_user_id, heart_count), self-reacts excluded."""
    results: dict[str, tuple[str, int]] = {}
    entries = db.get_entries_for_contest(contest_id)
    for entry in entries:
        channel = bot.get_channel(int(entry.channel_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(entry.channel_id))
            except discord.DiscordException:
                continue
        try:
            message = await channel.fetch_message(int(entry.message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

        hearted_by: set[int] = set()
        for reaction in message.reactions:
            emoji_str = str(reaction.emoji)
            if emoji_str not in config.HEART_EMOJIS:
                continue
            async for user in reaction.users():
                if user.id == message.author.id:
                    continue  # no self-voting
                if user.bot:
                    continue
                hearted_by.add(user.id)

        results[entry.message_id] = (entry.user_id, len(hearted_by))
    return results


# ---------------------------------------------------------------------------
# Round lifecycle
# ---------------------------------------------------------------------------

async def start_voting_phase(now: datetime) -> discord.Embed | str:
    channel = await get_contest_channel()
    if channel is None:
        return "Could not find the configured contest channel."

    pkey = cm.period_key(now, config.CONTEST_PERIOD)
    start, end = cm.period_bounds(now, config.CONTEST_PERIOD)
    vote_end = cm.vote_window_end(start, end, config.VOTE_WINDOW_MINUTES)

    recent_themes = db.get_recent_winning_themes(config.THEME_COOLDOWN)
    candidates = cm.pick_candidate_themes(config.CONTEST_THEMES, recent_themes, config.VOTE_CANDIDATE_COUNT)

    contest = db.create_voting_contest(
        pkey, config.CONTEST_PERIOD, candidates, start.isoformat(), vote_end.isoformat(), end.isoformat()
    )

    lines = [f"{NUMBER_EMOJIS[i]} **{theme}**" for i, theme in enumerate(candidates)]
    embed = discord.Embed(
        title="🗳️ Vote for this round's contest theme!",
        description=(
            "\n".join(lines)
            + f"\n\nReact with the number of your favorite. Voting closes "
            f"<t:{int(vote_end.timestamp())}:R> (<t:{int(vote_end.timestamp())}:t>)."
        ),
        color=discord.Color(0x1D003A),
    )
    embed.set_footer(
        text=f"Round #{contest.id} • entries open once voting closes, through "
        f"{end.strftime('%Y-%m-%d %H:%M %Z')}"
    )
    message = await channel.send(embed=embed)
    for i in range(len(candidates)):
        await message.add_reaction(NUMBER_EMOJIS[i])
    db.set_poll_message(contest.id, str(message.id), str(channel.id))
    log.info("Started round #%s (period %s) with candidates %s", contest.id, pkey, candidates)
    return embed


async def finalize_theme_for_contest(contest: Contest) -> discord.Embed | str:
    channel = bot.get_channel(int(contest.poll_channel_id)) if contest.poll_channel_id else None
    if channel is None and contest.poll_channel_id:
        try:
            channel = await bot.fetch_channel(int(contest.poll_channel_id))
        except discord.DiscordException:
            channel = None
    if channel is None:
        channel = await get_contest_channel()
    if channel is None:
        return "Could not find the configured contest channel."

    vote_counts: dict[str, int] = {theme: 0 for theme in contest.candidate_themes}
    message = None
    if contest.poll_message_id:
        try:
            message = await channel.fetch_message(int(contest.poll_message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    if message is not None:
        for i, theme in enumerate(contest.candidate_themes):
            reaction = discord.utils.get(message.reactions, emoji=NUMBER_EMOJIS[i])
            if reaction is None:
                continue
            count = 0
            async for user in reaction.users():
                if not user.bot:
                    count += 1
            vote_counts[theme] = count

    if message is not None and any(vote_counts.values()):
        winning_theme = cm.tally_theme_votes(vote_counts)
    else:
        # No poll message / nobody voted - just pick randomly so the round
        # still moves forward.
        winning_theme = random.choice(contest.candidate_themes)

    db.finalize_theme(contest.id, winning_theme)

    results_lines = [f"{NUMBER_EMOJIS[i]} {t} — {vote_counts.get(t, 0)} vote(s)" for i, t in enumerate(contest.candidate_themes)]
    end_at = datetime.fromisoformat(contest.end_at)
    embed = discord.Embed(
        title=f"📸 This round's theme: {winning_theme}!",
        description=(
            "Voting results:\n" + "\n".join(results_lines) + "\n\n"
            f"Post your best photo for **{winning_theme}** right here in "
            f"<#{channel.id}> before <t:{int(end_at.timestamp())}:F> — "
            f"react with ❤️ on your favorites!"
        ),
        color=discord.Color(0x1D003A),
    )
    await channel.send(embed=embed)
    log.info("Round #%s theme finalized: %r (votes=%s)", contest.id, winning_theme, vote_counts)
    return embed


async def end_contest_and_tally(contest: Contest) -> discord.Embed | str:
    channel = await get_contest_channel()
    if channel is None:
        return "Could not find the configured contest channel."

    heart_counts = await compute_entry_heart_counts(contest.id)
    winner_ids, winning_message_id, top_count = cm.tally_winners(heart_counts)

    # Handle the reward role: remove from last round's winners, add to this one's.
    if config.WINNER_ROLE_ID and channel.guild:
        role = channel.guild.get_role(config.WINNER_ROLE_ID)
        if role:
            for member in list(role.members):
                if str(member.id) not in winner_ids:
                    try:
                        await member.remove_roles(role, reason="Contest winner rotation")
                    except discord.DiscordException:
                        log.warning("Could not remove winner role from %s", member.id)
            for user_id in winner_ids:
                member = channel.guild.get_member(int(user_id))
                if member:
                    try:
                        await member.add_roles(role, reason="Contest winner")
                    except discord.DiscordException:
                        log.warning("Could not add winner role to %s", user_id)

    db.complete_contest(contest.id, winner_ids, winning_message_id, top_count)

    theme = contest.theme or "(no theme)"
    if not winner_ids:
        embed = discord.Embed(
            title=f"🏁 Round Over: {theme}",
            description="Nobody submitted an entry (or nothing got any ❤️ reactions) "
            "this round, so there's no winner this time. A new round starts automatically!",
            color=discord.Color(0x1D003A),
        )
    else:
        mentions = ", ".join(f"<@{uid}>" for uid in winner_ids)
        tie_note = " (it's a tie!)" if len(winner_ids) > 1 else ""
        embed = discord.Embed(
            title=f"🏆 Round Winner: {theme}",
            description=(
                f"Congratulations {mentions}{tie_note} with **{top_count}** ❤️ reactions!\n\n"
                f"**Reward:** {config.REWARD_DESCRIPTION}"
            ),
            color=discord.Color(0x1D003A),
        )
        if winning_message_id:
            embed.add_field(
                name="Winning entry",
                value=f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{winning_message_id}",
                inline=False,
            )
        top3 = db.get_leaderboard(limit=3)
        if top3:
            embed.add_field(
                name="🏆 Leaderboard (top 3 all-time)",
                value="\n".join(format_leaderboard_lines(top3)),
                inline=False,
            )
    await channel.send(embed=embed)
    log.info(
        "Ended round #%s (theme %r): winners=%s hearts=%s",
        contest.id, contest.theme, winner_ids, top_count,
    )
    return embed


# ---------------------------------------------------------------------------
# State machine - checks periodically whether a phase transition is due.
# Runs immediately on startup (so the bot always has a round going) and
# then every config.CHECK_INTERVAL_SECONDS.
# ---------------------------------------------------------------------------

@tasks.loop(seconds=config.CHECK_INTERVAL_SECONDS)
async def contest_state_machine():
    now = datetime.now(config.CONTEST_TIMEZONE)
    pkey_now = cm.period_key(now, config.CONTEST_PERIOD)
    try:
        current = db.get_latest_contest()
        if current is None:
            await start_voting_phase(now)
        elif current.status == "completed":
            if current.period_key != pkey_now:
                await start_voting_phase(now)
            # else: already ran this period, wait for the next one.
        elif current.status == "voting":
            vote_end = datetime.fromisoformat(current.vote_ends_at)
            if now >= vote_end:
                await finalize_theme_for_contest(current)
        elif current.status == "active":
            end_at = datetime.fromisoformat(current.end_at)
            if now >= end_at:
                await end_contest_and_tally(current)
    except Exception:
        log.exception("contest_state_machine tick failed")


@contest_state_machine.before_loop
async def before_state_machine():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
    except discord.DiscordException:
        log.exception("Failed to sync slash commands")
    if not contest_state_machine.is_running():
        contest_state_machine.start()  # fires immediately, then every CHECK_INTERVAL_SECONDS


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id != config.CONTEST_CHANNEL_ID:
        return
    contest = db.get_latest_contest()
    if contest and contest.status == "active" and any(is_image_attachment(a) for a in message.attachments):
        if db.user_has_entry(contest.id, str(message.author.id)):
            # One entry per person per round - remove the extra post and let them know.
            try:
                await message.delete()
            except discord.DiscordException:
                log.warning("Could not delete extra entry message %s from %s", message.id, message.author.id)
            warning = (
                f"{message.author.mention} you can only submit **one photo per round** — "
                "your extra post was removed. Your first entry is still in the running!"
            )
            try:
                await message.channel.send(warning, delete_after=15)
            except discord.DiscordException:
                pass
            return
        db.add_entry(
            contest_id=contest.id,
            message_id=str(message.id),
            channel_id=str(message.channel.id),
            user_id=str(message.author.id),
            created_at=message.created_at.isoformat(),
        )
        try:
            await message.add_reaction("❤️")
        except discord.DiscordException:
            pass
    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(description="Show this round's contest status.")
async def currenttheme(interaction: discord.Interaction):
    contest = db.get_latest_contest()
    if contest is None or contest.status == "completed":
        await interaction.response.send_message("No round is running right now — a new one starts automatically.")
        return
    if contest.status == "voting":
        vote_end = datetime.fromisoformat(contest.vote_ends_at)
        options = ", ".join(contest.candidate_themes)
        await interaction.response.send_message(
            f"Theme voting is open! Candidates: {options}. Voting closes <t:{int(vote_end.timestamp())}:R>."
        )
        return
    entries = db.get_entries_for_contest(contest.id)
    end_at = datetime.fromisoformat(contest.end_at)
    await interaction.response.send_message(
        f"This round's theme is **{contest.theme}**. {len(entries)} photo(s) submitted so far — "
        f"entries close <t:{int(end_at.timestamp())}:R>."
    )


MEDALS = ["🥇", "🥈", "🥉"]


def format_leaderboard_lines(top: list[tuple[str, int]]) -> list[str]:
    """Render (user_id, wins) pairs as display lines, with medals for the
    top 3 and plain numbering after that."""
    lines = []
    for i, (uid, wins) in enumerate(top):
        rank = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        win_word = "win" if wins == 1 else "wins"
        lines.append(f"{rank} <@{uid}> — **{wins}** {win_word}")
    return lines


@bot.tree.command(description="Show the all-time leaderboard.")
async def leaderboard(interaction: discord.Interaction):
    top = db.get_leaderboard(limit=10)
    if not top:
        await interaction.response.send_message("No rounds have finished yet.")
        return
    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="\n".join(format_leaderboard_lines(top)),
        color=discord.Color(0x1D003A),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(description="[Admin] Clear the all-time leaderboard. Does not affect the current round.")
@app_commands.describe(confirm="Set to True to actually clear the leaderboard - this can't be undone.")
async def reset(interaction: discord.Interaction, confirm: bool = False):
    if not is_admin(interaction):
        await interaction.response.send_message("You need Manage Server permission to do that.", ephemeral=True)
        return
    if not confirm:
        await interaction.response.send_message(
            "This will permanently clear everyone's win counts on the leaderboard "
            "(round history, themes, and the current round are unaffected). "
            "Run `/reset confirm:True` to go ahead.",
            ephemeral=True,
        )
        return
    cleared = db.reset_leaderboard()
    log.info("Leaderboard reset by %s (id=%s): %d round(s) cleared", interaction.user, interaction.user.id, cleared)
    await interaction.response.send_message(
        f"🧹 Leaderboard cleared — win counts removed from {cleared} past round(s)."
    )


@bot.tree.command(description="[Admin] Start a new round right now, if none is running.")
async def force_start_contest(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You need Manage Server permission to do that.", ephemeral=True)
        return
    current = db.get_latest_contest()
    if current is not None and current.status != "completed":
        await interaction.response.send_message(
            f"A round is already {current.status} (round #{current.id}). "
            f"Use /force_finalize_vote or /force_end_contest instead.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    result = await start_voting_phase(datetime.now(config.CONTEST_TIMEZONE))
    await interaction.followup.send(result if isinstance(result, str) else "New round started - voting is open.")


@bot.tree.command(description="[Admin] Close theme voting immediately and lock in the winning theme.")
async def force_finalize_vote(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You need Manage Server permission to do that.", ephemeral=True)
        return
    current = db.get_latest_contest()
    if current is None or current.status != "voting":
        await interaction.response.send_message("There's no theme vote in progress right now.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    result = await finalize_theme_for_contest(current)
    await interaction.followup.send(result if isinstance(result, str) else "Voting closed - theme locked in.")


@bot.tree.command(description="[Admin] End the active round and declare a winner right now.")
async def force_end_contest(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You need Manage Server permission to do that.", ephemeral=True)
        return
    current = db.get_latest_contest()
    if current is None or current.status != "active":
        await interaction.response.send_message("There's no active round to end right now.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    result = await end_contest_and_tally(current)
    await interaction.followup.send(result if isinstance(result, str) else "Round ended.")


def main():
    bot.run(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
