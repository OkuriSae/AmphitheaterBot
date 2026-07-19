import asyncio
import csv
import io
import itertools
import logging
import os
import random
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

import certifi
import truststore

truststore.inject_into_ssl()

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


os.environ.setdefault("SSL_CERT_FILE", certifi.where())
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

DEFAULT_BATTLE_ROLE_ID = 1507252341051490345
BATTLE_ROLE_IDS = {
    560442567674691594: 1437644534975561799,
    1504325297284317305: 1506869211501301780,
}
BATTLE_JOIN_CUSTOM_ID = "amphitheater:battle:join"
BATTLE_BALANCE_CUSTOM_ID = "amphitheater:battle:balance"
BATTLE_TOGGLE_LABEL = "👍️"
BATTLE_BALANCE_LABEL = "⚔️"
PARTICIPANTS_FIELD_NAME = "参加者"
EMPTY_PARTICIPANTS_TEXT = "まだ参加者はいません"
MAP_POLL_QUESTION = "マップ投票"
MAP_POLL_OPTIONS = [
    ("pangaea", "⚔️", "パンゲア"),
    ("ultimate", "🗡️", "ウルパン"),
    ("ultimate_nowrap", "🏟", "ウルパン（nowrap）"),
    ("seven_seas", "⛵", "7つの海"),
    ("lake", "⛲", "湖"),
    ("highland", "⛰️", "ハイランド"),
    ("tilted_axis", "🧊", "地軸傾斜"),
]
MAP_POLL_CONFIRM_EMOJI = "✅"
THREAD_NAME_MAX_LENGTH = 100
TEAM_EMBED_TITLE = "チーム分け"
TEAM_1_FIELD_PREFIX = "チーム1"
TEAM_2_FIELD_PREFIX = "チーム2"
USER_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
DISCORD_MESSAGE_URL_PATTERN = re.compile(
    r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)"
)
RATINGS_SPREADSHEET_ID = "13__lGAuvm00wKJeZro8hGpy9PsulrCHvDiCVxdly7qU"
RATINGS_WORKSHEET_GID = 0
CIV_TIER_LIST_WORKSHEET_GID = 50179013
PLAYER_LIST_SHEET_TITLE = "プレイヤーリスト"
PLAYER_PARTICIPATION_HISTORY_SHEET_TITLE = "プレイヤー参加履歴"
DEFAULT_RATING = "1000"
DRAFT_CIVS_PER_PLAYER = 6
DRAFT_MAX_PLAYERS = 12
ELO_K_FACTOR = 48
TEAM_AVERAGE_RATING_WEIGHT = 0.7
PERSONAL_RATING_WEIGHT = 0.3
TIMEZONE = ZoneInfo("Asia/Tokyo")
RATINGS_CSV_URL = os.getenv(
    "RATINGS_CSV_URL",
    f"https://docs.google.com/spreadsheets/d/{RATINGS_SPREADSHEET_ID}/export?format=csv&gid={RATINGS_WORKSHEET_GID}",
)
CIV_TIER_LIST_CSV_URL = os.getenv(
    "CIV_TIER_LIST_CSV_URL",
    f"https://docs.google.com/spreadsheets/d/{RATINGS_SPREADSHEET_ID}/export?format=csv&gid={CIV_TIER_LIST_WORKSHEET_GID}",
)
MEMBER_LIST_URL = os.getenv(
    "MEMBER_LIST_URL",
    f"https://docs.google.com/spreadsheets/d/{RATINGS_SPREADSHEET_ID}/edit#gid={RATINGS_WORKSHEET_GID}",
)
RESULT_TABLE_URL = os.getenv(
    "RESULT_TABLE_URL",
    f"https://docs.google.com/spreadsheets/d/{RATINGS_SPREADSHEET_ID}/edit?gid=510962199#gid=510962199",
)
LATEST_TEAMS_BY_CHANNEL: dict[int, tuple[list["Participant"], list["Participant"]]] = {}
BATTLE_STATES_BY_THREAD: dict[int, "BattleState"] = {}
THREAD_IDS_BY_PARENT_CHANNEL: dict[int, int] = {}
LATEST_BATTLE_MESSAGES_BY_CHANNEL: dict[int, discord.Message] = {}
LATEST_PARTICIPANTS_BY_CHANNEL: dict[int, list["Participant"]] = {}
MAP_POLL_STATES_BY_MESSAGE: dict[int, "MapPollState"] = {}
LATEST_DRAFTS_BY_CHANNEL: dict[int, "DraftState"] = {}
LOCK_FILE_PATH = ".amphitheaterbot.lock"
LOCK_FILE = None


@dataclass
class Participant:
    user_id: int
    rating: str | None = None


@dataclass
class BattleState:
    parent_channel_id: int
    map_poll_channel_id: int
    map_poll_message_id: int
    team_1: list[Participant] | None = None
    team_2: list[Participant] | None = None


@dataclass
class MapPollState:
    parent_channel_id: int
    message_id: int
    votes_by_user: dict[int, set[str]]
    selected_map: str | None = None
    finalized: bool = False


@dataclass
class PlayerResult:
    user_id: int
    player_name: str
    team_name: str
    old_rating: int
    new_rating: int


@dataclass
class FinishResult:
    winner_text: str
    map_name: str
    end_turn: str
    team_1_names: list[str]
    team_2_names: list[str]
    player_results: list[PlayerResult]


@dataclass
class RevertResult:
    finished_at: str
    rating_reverts: list[tuple[str, int, int]]


@dataclass
class DraftCivilization:
    name_en: str
    name_jp: str
    image_id: str
    tier: int


@dataclass
class DraftState:
    channel_id: int
    message: discord.Message
    slots: list[list[DraftCivilization]]
    banned_civilization_indexes: dict[int, int]


class RatingRegistrationError(Exception):
    pass


class FinishError(Exception):
    pass


def acquire_process_lock(lock_file) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except (BlockingIOError, OSError) as exc:
        raise RuntimeError("Another AmphitheaterBot process is already running.") from exc


def rating_value(participant: Participant) -> int:
    if participant.rating is None:
        return 0

    try:
        return int(participant.rating.replace(",", ""))
    except ValueError:
        return 0


def format_participants(participants: list[Participant]) -> str:
    if not participants:
        return EMPTY_PARTICIPANTS_TEXT

    lines = []
    for index, participant in enumerate(participants, start=1):
        rating = participant.rating if participant.rating else "未登録"
        lines.append(f"{index}. <@{participant.user_id}> ({rating})")

    return "\n".join(lines)


def read_participants(embed: discord.Embed) -> list[Participant]:
    for field in embed.fields:
        if field.name != PARTICIPANTS_FIELD_NAME:
            continue

        participants: list[Participant] = []
        for line in field.value.splitlines():
            marker_start = line.find("<@")
            marker_end = line.find(">", marker_start)
            if marker_start == -1 or marker_end == -1:
                continue

            raw_user_id = line[marker_start + 2 : marker_end].removeprefix("!")
            if raw_user_id.isdigit():
                rating = line[marker_end + 1 :].strip().removeprefix("(").removesuffix(")").strip() or None
                participants.append(Participant(user_id=int(raw_user_id), rating=rating))

        return participants

    return []


def read_participants_from_text(value: str) -> list[Participant]:
    participants: list[Participant] = []
    for line in value.splitlines():
        marker_start = line.find("<@")
        marker_end = line.find(">", marker_start)
        if marker_start == -1 or marker_end == -1:
            continue

        raw_user_id = line[marker_start + 2 : marker_end].removeprefix("!")
        if raw_user_id.isdigit():
            rating = line[marker_end + 1 :].strip().removeprefix("(").removesuffix(")").strip() or None
            participants.append(Participant(user_id=int(raw_user_id), rating=rating))

    return participants


def update_participants(embed: discord.Embed, participants: list[Participant]) -> None:
    value = format_participants(participants)
    for index, field in enumerate(embed.fields):
        if field.name == PARTICIPANTS_FIELD_NAME:
            embed.set_field_at(index, name=PARTICIPANTS_FIELD_NAME, value=value, inline=False)
            return

    embed.add_field(name=PARTICIPANTS_FIELD_NAME, value=value, inline=False)


def add_member_list_link(embed: discord.Embed) -> None:
    embed.add_field(name="\u200b", value=f"[メンバーリスト]({MEMBER_LIST_URL})", inline=False)


def add_result_table_link(embed: discord.Embed) -> None:
    embed.add_field(name="\u200b", value=f"[結果表]({RESULT_TABLE_URL})", inline=False)


async def edit_interaction_message(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
) -> None:
    try:
        await interaction.edit_original_response(embed=embed, view=view)
        return
    except (discord.NotFound, discord.Forbidden):
        logging.exception("Failed to edit the component message through the interaction token.")

    if interaction.message is not None:
        await interaction.message.edit(embed=embed, view=view)


def get_battle_role_id(guild_id: int | None) -> int:
    if guild_id is None:
        return DEFAULT_BATTLE_ROLE_ID

    return BATTLE_ROLE_IDS.get(guild_id, DEFAULT_BATTLE_ROLE_ID)


def describe_bot_thread_permissions(interaction: discord.Interaction) -> str:
    guild = interaction.guild
    channel = interaction.channel
    if guild is None or channel is None or guild.me is None:
        return "Botのチャンネル権限を確認できませんでした。"

    permissions = channel.permissions_for(guild.me)
    permission_labels = [
        ("チャンネルを見る", permissions.view_channel),
        ("メッセージを送信", permissions.send_messages),
        ("メッセージ履歴を読む", permissions.read_message_history),
        ("公開スレッドを作成", permissions.create_public_threads),
        ("スレッドでメッセージを送信", permissions.send_messages_in_threads),
    ]
    status = " / ".join(f"{label}: {'OK' if is_allowed else 'NG'}" for label, is_allowed in permission_labels)
    channel_name = getattr(channel, "name", str(channel))
    return f"チャンネル `{channel_name}` でのBot実効権限: {status}"


def map_option_label(option_key: str) -> str:
    for key, _, label in MAP_POLL_OPTIONS:
        if key == option_key:
            return label

    return option_key


def map_option_display(option_key: str) -> str:
    for key, emoji, label in MAP_POLL_OPTIONS:
        if key == option_key:
            return f"{emoji} {label}"

    return option_key


def map_option_key_for_emoji(emoji: str) -> str | None:
    for key, option_emoji, _ in MAP_POLL_OPTIONS:
        if emoji == option_emoji:
            return key

    return None


def map_poll_vote_count(state: MapPollState) -> int:
    return len([votes for votes in state.votes_by_user.values() if votes])


def map_poll_counts(state: MapPollState) -> dict[str, int]:
    counts = {key: 0 for key, _, _ in MAP_POLL_OPTIONS}
    for votes in state.votes_by_user.values():
        for option_key in votes:
            if option_key in counts:
                counts[option_key] += 1

    return counts


def map_poll_non_voter_mentions(state: MapPollState) -> list[str]:
    participants = LATEST_PARTICIPANTS_BY_CHANNEL.get(state.parent_channel_id, [])
    voted_user_ids = {user_id for user_id, votes in state.votes_by_user.items() if votes}
    return [
        f"<@{participant.user_id}>"
        for participant in participants
        if participant.user_id not in voted_user_ids
    ]


def create_map_poll_embed(state: MapPollState | None = None, *, non_voters: list[str] | None = None) -> discord.Embed:
    embed = discord.Embed(title=MAP_POLL_QUESTION, color=discord.Color.blurple())
    if state is None or not state.finalized:
        vote_count = 0 if state is None else map_poll_vote_count(state)
        description_lines = [
            "**対応するマップアイコンに投票してください（複数投票可）**",
            "",
            *[f"{emoji} {label}" for _, emoji, label in MAP_POLL_OPTIONS],
            "",
        ]
        embed.description = "\n".join(description_lines)
        embed.add_field(name="投票受付中", value=f"{vote_count}人が投票済み", inline=False)
        if non_voters:
            embed.add_field(name="未投票", value="\n".join(non_voters), inline=False)
        embed.add_field(name="\u200b", value=f"{MAP_POLL_CONFIRM_EMOJI} でマップ確定", inline=False)
        return embed

    counts = map_poll_counts(state)
    lines = []
    for option_key, emoji, label in MAP_POLL_OPTIONS:
        lines.append(f"{emoji} {label}: {counts.get(option_key, 0)}票")

    embed.add_field(name="投票結果", value="\n".join(lines), inline=False)
    embed.add_field(name="選出マップ", value=state.selected_map or "未確定", inline=False)
    embed.set_footer(text="/map でマップ投票やり直し")
    return embed


def finalize_map_poll(state: MapPollState) -> str:
    counts = map_poll_counts(state)
    max_votes = max(counts.values()) if counts else 0
    top_options = [key for key, count in counts.items() if count == max_votes]
    selected_key = random.choice(top_options or [MAP_POLL_OPTIONS[0][0]])
    selected_map = map_option_display(selected_key)
    state.selected_map = selected_map
    state.finalized = True
    return selected_map


async def add_map_poll_reactions(message: discord.Message) -> None:
    for _, emoji, _ in MAP_POLL_OPTIONS:
        await message.add_reaction(emoji)
    await message.add_reaction(MAP_POLL_CONFIRM_EMOJI)


async def remove_user_reaction(message: discord.Message, emoji: discord.PartialEmoji | str, user_id: int) -> None:
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            return

    try:
        await message.remove_reaction(emoji, user)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        logging.exception("Failed to remove a map poll reaction.")


async def clear_map_poll_reactions(message: discord.Message) -> None:
    try:
        await message.clear_reactions()
        return
    except (discord.Forbidden, discord.HTTPException):
        logging.exception("Failed to clear all map poll reactions. Falling back to bot reactions.")

    if bot.user is None:
        return

    for _, emoji, _ in MAP_POLL_OPTIONS:
        try:
            await message.remove_reaction(emoji, bot.user)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logging.exception("Failed to remove a bot map poll reaction.")
    try:
        await message.remove_reaction(MAP_POLL_CONFIRM_EMOJI, bot.user)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        logging.exception("Failed to remove the bot map poll confirm reaction.")


async def update_map_poll_message(channel: discord.abc.Messageable, state: MapPollState) -> None:
    message = await channel.fetch_message(state.message_id)
    await message.edit(embed=create_map_poll_embed(state, non_voters=map_poll_non_voter_mentions(state)))


async def update_map_poll_message_for_channel(channel_id: int) -> None:
    thread_id = THREAD_IDS_BY_PARENT_CHANNEL.get(channel_id)
    if thread_id is None:
        return

    state = BATTLE_STATES_BY_THREAD.get(thread_id)
    if state is None:
        return

    poll_state = MAP_POLL_STATES_BY_MESSAGE.get(state.map_poll_message_id)
    if poll_state is None or poll_state.finalized:
        return

    participant_ids = {participant.user_id for participant in LATEST_PARTICIPANTS_BY_CHANNEL.get(channel_id, [])}
    for user_id in list(poll_state.votes_by_user):
        if user_id not in participant_ids:
            del poll_state.votes_by_user[user_id]

    channel = await fetch_map_poll_channel_by_state(state)
    await update_map_poll_message(channel, poll_state)


async def create_new_map_poll(channel: discord.abc.Messageable, channel_id: int) -> discord.Message:
    initial_state = MapPollState(
        parent_channel_id=channel_id,
        message_id=0,
        votes_by_user={},
    )
    message = await channel.send(embed=create_map_poll_embed(initial_state, non_voters=map_poll_non_voter_mentions(initial_state)))
    existing_thread_id = THREAD_IDS_BY_PARENT_CHANNEL.get(channel_id)
    existing_state = BATTLE_STATES_BY_THREAD.get(existing_thread_id or channel_id)
    if existing_state is None:
        THREAD_IDS_BY_PARENT_CHANNEL[channel_id] = channel_id
        BATTLE_STATES_BY_THREAD[channel_id] = BattleState(
            parent_channel_id=channel_id,
            map_poll_channel_id=channel_id,
            map_poll_message_id=message.id,
        )
    else:
        old_poll_state = MAP_POLL_STATES_BY_MESSAGE.pop(existing_state.map_poll_message_id, None)
        if old_poll_state is not None and not old_poll_state.finalized:
            try:
                old_channel = await fetch_map_poll_channel_by_state(existing_state)
                old_message = await old_channel.fetch_message(existing_state.map_poll_message_id)
                await clear_map_poll_reactions(old_message)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException, FinishError):
                logging.exception("Failed to clear reactions from the previous map poll.")
        existing_state.map_poll_channel_id = channel_id
        existing_state.map_poll_message_id = message.id
        THREAD_IDS_BY_PARENT_CHANNEL[channel_id] = existing_thread_id or channel_id

    initial_state.message_id = message.id
    MAP_POLL_STATES_BY_MESSAGE[message.id] = initial_state
    await add_map_poll_reactions(message)
    return message


def create_battle_thread_name(title: str) -> str:
    suffix = " 投票"
    max_title_length = THREAD_NAME_MAX_LENGTH - len(suffix)
    return f"{title[:max_title_length]}{suffix}"


def balance_teams(participants: list[Participant]) -> tuple[list[Participant], list[Participant]]:
    team_size = len(participants) // 2
    total_rating = sum(rating_value(participant) for participant in participants)
    best_team_indexes: tuple[int, ...] | None = None
    best_diff: int | None = None

    for team_indexes in itertools.combinations(range(len(participants)), team_size):
        team_rating = sum(rating_value(participants[index]) for index in team_indexes)
        diff = abs(total_rating - team_rating * 2)
        if best_diff is None or diff < best_diff:
            best_team_indexes = team_indexes
            best_diff = diff

    team_1_indexes = set(best_team_indexes or ())
    team_1 = [participant for index, participant in enumerate(participants) if index in team_1_indexes]
    team_2 = [participant for index, participant in enumerate(participants) if index not in team_1_indexes]
    return team_1, team_2


def format_team(participants: list[Participant]) -> str:
    if not participants:
        return "なし"

    lines = []
    sorted_participants = sorted(participants, key=lambda participant: (-rating_value(participant), participant.user_id))
    for index, participant in enumerate(sorted_participants, start=1):
        rating = participant.rating if participant.rating else "未登録"
        lines.append(f"{index}. <@{participant.user_id}> ({rating})")

    return "\n".join(lines)


def create_team_embed_from_teams(team_1: list[Participant], team_2: list[Participant]) -> discord.Embed:
    team_1_rating = sum(rating_value(participant) for participant in team_1)
    team_2_rating = sum(rating_value(participant) for participant in team_2)
    diff = abs(team_1_rating - team_2_rating)

    embed = discord.Embed(title=TEAM_EMBED_TITLE, color=discord.Color.green())
    embed.add_field(name=f"{TEAM_1_FIELD_PREFIX} 合計: {team_1_rating}", value=format_team(team_1), inline=True)
    embed.add_field(name=f"{TEAM_2_FIELD_PREFIX} 合計: {team_2_rating}", value=format_team(team_2), inline=True)
    embed.set_footer(text=f"rating差: {diff}")
    return embed


def create_team_embed(participants: list[Participant]) -> discord.Embed:
    team_1, team_2 = balance_teams(participants)
    return create_team_embed_from_teams(team_1, team_2)


def copy_teams(team_1: list[Participant], team_2: list[Participant]) -> tuple[list[Participant], list[Participant]]:
    return (
        [Participant(participant.user_id, participant.rating) for participant in team_1],
        [Participant(participant.user_id, participant.rating) for participant in team_2],
    )


def remember_latest_teams(channel_id: int, team_1: list[Participant], team_2: list[Participant]) -> None:
    copied_teams = copy_teams(team_1, team_2)
    LATEST_TEAMS_BY_CHANNEL[channel_id] = copied_teams

    thread_id = THREAD_IDS_BY_PARENT_CHANNEL.get(channel_id)
    if thread_id is not None:
        state = BATTLE_STATES_BY_THREAD.get(thread_id)
        if state is not None:
            state.team_1, state.team_2 = copy_teams(team_1, team_2)


def remove_participant_from_teams(participant_id: int, team_1: list[Participant], team_2: list[Participant]) -> tuple[list[Participant], list[Participant]]:
    return (
        [participant for participant in team_1 if participant.user_id != participant_id],
        [participant for participant in team_2 if participant.user_id != participant_id],
    )


def remove_participant_from_latest_teams(channel_id: int, participant_id: int) -> None:
    teams = LATEST_TEAMS_BY_CHANNEL.get(channel_id)
    if teams is not None:
        LATEST_TEAMS_BY_CHANNEL[channel_id] = remove_participant_from_teams(participant_id, *teams)

    thread_id = THREAD_IDS_BY_PARENT_CHANNEL.get(channel_id)
    if thread_id is None:
        return

    state = BATTLE_STATES_BY_THREAD.get(thread_id)
    if state is not None and state.team_1 is not None and state.team_2 is not None:
        state.team_1, state.team_2 = remove_participant_from_teams(participant_id, state.team_1, state.team_2)


def channel_lookup_ids(interaction: discord.Interaction) -> list[int]:
    ids: list[int] = []
    if interaction.channel_id is not None:
        ids.append(interaction.channel_id)

    channel = interaction.channel
    if isinstance(channel, discord.Thread) and channel.parent_id is not None:
        ids.append(channel.parent_id)

    state = get_state_for_interaction(interaction)
    if state is not None:
        ids.append(state.parent_channel_id)

    return list(dict.fromkeys(ids))


def latest_battle_message_for(interaction: discord.Interaction) -> tuple[int, discord.Message] | None:
    for channel_id in channel_lookup_ids(interaction):
        message = LATEST_BATTLE_MESSAGES_BY_CHANNEL.get(channel_id)
        if message is not None:
            return channel_id, message

    if len(LATEST_BATTLE_MESSAGES_BY_CHANNEL) == 1:
        channel_id, message = next(iter(LATEST_BATTLE_MESSAGES_BY_CHANNEL.items()))
        return channel_id, message

    return None


def draft_player_count_for(interaction: discord.Interaction, explicit_player_count: int | None) -> int | None:
    if explicit_player_count is not None:
        return explicit_player_count

    for channel_id in channel_lookup_ids(interaction):
        participants = LATEST_PARTICIPANTS_BY_CHANNEL.get(channel_id)
        if participants:
            return len(participants)

    latest_message = latest_battle_message_for(interaction)
    if latest_message is not None:
        _, message = latest_message
        if message.embeds:
            participants = read_participants(message.embeds[0])
            if participants:
                return len(participants)

    return None


def get_state_for_interaction(interaction: discord.Interaction) -> BattleState | None:
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        return BATTLE_STATES_BY_THREAD.get(channel.id)

    thread_id = THREAD_IDS_BY_PARENT_CHANNEL.get(interaction.channel_id or 0)
    if thread_id is None:
        return None

    return BATTLE_STATES_BY_THREAD.get(thread_id)


def read_teams(embed: discord.Embed) -> tuple[list[Participant], list[Participant]] | None:
    if embed.title != TEAM_EMBED_TITLE:
        return None

    team_1: list[Participant] | None = None
    team_2: list[Participant] | None = None
    for field in embed.fields:
        if field.name.startswith(TEAM_1_FIELD_PREFIX):
            team_1 = read_participants_from_text(field.value)
        elif field.name.startswith(TEAM_2_FIELD_PREFIX):
            team_2 = read_participants_from_text(field.value)

    if team_1 is None or team_2 is None:
        return None

    return team_1, team_2


def parse_discord_message_url(source: str) -> tuple[int, int]:
    match = DISCORD_MESSAGE_URL_PATTERN.fullmatch(source.strip())
    if match is None:
        raise FinishError("sourceにはチーム分けメッセージのDiscord URLを指定してください。")

    return int(match.group(2)), int(match.group(3))


async def fetch_message_from_url(source: str) -> discord.Message:
    channel_id, message_id = parse_discord_message_url(source)
    channel = bot.get_channel(channel_id)
    if channel is None:
        fetched_channel = await bot.fetch_channel(channel_id)
        if not hasattr(fetched_channel, "fetch_message"):
            raise FinishError("sourceのチャンネルからメッセージを取得できませんでした。")
        channel = fetched_channel

    if not hasattr(channel, "fetch_message"):
        raise FinishError("sourceのチャンネルからメッセージを取得できませんでした。")

    return await channel.fetch_message(message_id)


async def state_from_team_message_url(source: str) -> BattleState:
    try:
        message = await fetch_message_from_url(source)
    except discord.NotFound as exc:
        raise FinishError("sourceのメッセージが見つかりませんでした。") from exc
    except discord.Forbidden as exc:
        raise FinishError("sourceのメッセージを読む権限がありません。") from exc
    except discord.HTTPException as exc:
        raise FinishError(f"sourceのメッセージ取得に失敗しました: {exc}") from exc

    if not message.embeds:
        raise FinishError("sourceのメッセージにチーム分けの埋め込みがありません。")

    teams = read_teams(message.embeds[0])
    if teams is None:
        raise FinishError("sourceのメッセージからチーム分けを読み取れませんでした。")

    team_1, team_2 = teams
    channel_id = message.channel.id
    return BattleState(
        parent_channel_id=channel_id,
        map_poll_channel_id=channel_id,
        map_poll_message_id=0,
        team_1=team_1,
        team_2=team_2,
    )


def read_mentioned_user_ids(value: str) -> list[int]:
    return [int(user_id) for user_id in USER_MENTION_PATTERN.findall(value)]


def parse_team_mentions(value: str, team_name: str) -> list[int]:
    user_ids = read_mentioned_user_ids(value)
    if not user_ids:
        raise FinishError(f"{team_name}には1人以上のユーザーをメンションで指定してください。")

    remaining_text = USER_MENTION_PATTERN.sub("", value).strip()
    if remaining_text:
        raise FinishError(f"{team_name}はユーザーメンションだけをスペース区切りで指定してください。")

    if len(user_ids) != len(set(user_ids)):
        raise FinishError(f"{team_name}に同じユーザーが複数回指定されています。")

    return user_ids


def normalize_rating_key(value: object) -> str:
    return str(value).strip().lower()


def load_ratings() -> dict[str, str]:
    with urllib.request.urlopen(RATINGS_CSV_URL, timeout=10) as response:
        csv_text = response.read().decode("utf-8-sig")

    rows = csv.DictReader(io.StringIO(csv_text))
    ratings: dict[str, str] = {}
    for row in rows:
        normalized_row = {normalize_rating_key(key): (value or "").strip() for key, value in row.items() if key}
        user_id = normalized_row.get("userid")
        rating = normalized_row.get("rating")
        if user_id and rating:
            ratings[normalize_rating_key(user_id)] = rating

    return ratings


def parse_civilization_tier(value: object) -> int:
    raw_value = str(value or "").strip()
    if not raw_value:
        return 99

    match = re.search(r"\d+", raw_value)
    if match is None:
        return 99

    return int(match.group(0))


def load_civilization_tier_list() -> list[DraftCivilization]:
    separator = "&" if "?" in CIV_TIER_LIST_CSV_URL else "?"
    url = f"{CIV_TIER_LIST_CSV_URL}{separator}_={int(datetime.now(TIMEZONE).timestamp())}"
    with urllib.request.urlopen(url, timeout=10) as response:
        csv_text = response.read().decode("utf-8-sig")

    rows = csv.DictReader(io.StringIO(csv_text))
    civilizations: list[DraftCivilization] = []
    for row in rows:
        normalized_row = {normalize_rating_key(key): (value or "").strip() for key, value in row.items() if key}
        name_en = normalized_row.get("nameen", "")
        name_jp = normalized_row.get("namejp", "")
        image_id = normalized_row.get("imageid", "")
        if not name_en and not name_jp:
            continue

        civilizations.append(
            DraftCivilization(
                name_en=name_en,
                name_jp=name_jp or name_en,
                image_id=image_id,
                tier=parse_civilization_tier(normalized_row.get("tier")),
            )
        )

    return civilizations


def format_draft_civilization(civilization: DraftCivilization) -> str:
    if civilization.name_en and civilization.image_id:
        emoji_name = re.sub(r"[^0-9A-Za-z_]", "_", civilization.name_en)
        return f"<:{emoji_name}:{civilization.image_id}> {civilization.name_jp}"

    return civilization.name_jp


def distribute_draft_civilizations(civilizations: list[DraftCivilization], player_count: int) -> list[list[DraftCivilization]]:
    total_required = player_count * DRAFT_CIVS_PER_PLAYER
    if len(civilizations) < total_required:
        raise FinishError(
            f"文明Tierリストの文明数が不足しています。必要: {total_required} / 読み込み: {len(civilizations)}"
        )

    high_tier_pool = [civilization for civilization in civilizations if civilization.tier in (1, 2)]
    other_pool = [civilization for civilization in civilizations if civilization.tier not in (1, 2)]
    for pool in (high_tier_pool, other_pool):
        random.shuffle(pool)

    minimum_high_tier_count = max(0, total_required - len(other_pool))
    high_tier_count = max(min(player_count, len(high_tier_pool), total_required), minimum_high_tier_count)
    selected_high_tier = high_tier_pool[:high_tier_count]
    selected_others = random.sample(other_pool, total_required - high_tier_count)
    tier_1 = [civilization for civilization in selected_high_tier if civilization.tier == 1]
    tier_2 = [civilization for civilization in selected_high_tier if civilization.tier == 2]
    others = selected_others
    for pool in (tier_1, tier_2, others):
        random.shuffle(pool)

    slots: list[list[DraftCivilization]] = [[] for _ in range(player_count)]

    def place(civilization: DraftCivilization, *, high_tier: bool) -> None:
        candidates = [index for index, slot in enumerate(slots) if len(slot) < DRAFT_CIVS_PER_PLAYER]
        if not candidates:
            return

        if high_tier:
            min_high_count = min(
                len([item for item in slots[index] if item.tier in (1, 2)])
                for index in candidates
            )
            candidates = [
                index
                for index in candidates
                if len([item for item in slots[index] if item.tier in (1, 2)]) == min_high_count
            ]

        min_size = min(len(slots[index]) for index in candidates)
        candidates = [index for index in candidates if len(slots[index]) == min_size]
        slots[random.choice(candidates)].append(civilization)

    for civilization in [*tier_1, *tier_2]:
        place(civilization, high_tier=True)
    for civilization in others:
        place(civilization, high_tier=False)

    for slot in slots:
        random.shuffle(slot)

    return slots


def draft_slot_label(slot_index: int) -> str:
    team_name = "チーム1" if slot_index % 2 == 0 else "チーム2"
    player_number = slot_index // 2 + 1
    return f"{team_name} Player {player_number}"


def format_draft_civilization_line(
    civilization: DraftCivilization,
    *,
    number: int,
    banned: bool = False,
) -> str:
    line = f"{number} {format_draft_civilization(civilization)}"
    return f"~~{line}~~" if banned else line


def format_draft_slot(
    civilizations: list[DraftCivilization],
    *,
    slot_index: int,
    banned_civilization_indexes: dict[int, int],
) -> str:
    banned_index = banned_civilization_indexes.get(slot_index)
    team_player_index = slot_index // 2
    return "\n".join(
        format_draft_civilization_line(
            civilization,
            number=team_player_index * DRAFT_CIVS_PER_PLAYER + index + 1,
            banned=index == banned_index,
        )
        for index, civilization in enumerate(civilizations)
    )


def create_draft_embed(
    slots: list[list[DraftCivilization]],
    banned_civilization_indexes: dict[int, int] | None = None,
) -> discord.Embed:
    banned_civilization_indexes = banned_civilization_indexes or {}
    embed = discord.Embed(title="文明ドラフト", color=discord.Color.dark_teal())

    team_1_slots = slots[::2]
    team_2_slots = slots[1::2]
    row_count = max(len(team_1_slots), len(team_2_slots))

    for index in range(row_count):
        team_1_slot_index = index * 2
        team_1_value = format_draft_slot(
            team_1_slots[index],
            slot_index=team_1_slot_index,
            banned_civilization_indexes=banned_civilization_indexes,
        )
        embed.add_field(
            name=f"{'チーム1' if index == 0 else ' '}\nPlayer {index + 1}",
            value=team_1_value,
            inline=True,
        )

        if index < len(team_2_slots):
            team_2_slot_index = index * 2 + 1
            team_2_value = format_draft_slot(
                team_2_slots[index],
                slot_index=team_2_slot_index,
                banned_civilization_indexes=banned_civilization_indexes,
            )
        else:
            team_2_value = "-"

        embed.add_field(
            name=f"{'チーム2' if index == 0 else ' '}\nPlayer {index + 1}",
            value=team_2_value,
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    return embed


def parse_ban_numbers(value: str) -> list[int]:
    numbers: list[int] = []
    invalid_tokens: list[str] = []
    for token in re.split(r"[\s,、]+", value.strip()):
        if not token:
            continue
        if not token.isdigit():
            invalid_tokens.append(token)
            continue
        numbers.append(int(token))

    if invalid_tokens:
        raise FinishError(f"番号はカンマまたは半角スペース区切りの数字で指定してください: {', '.join(invalid_tokens)}")
    if not numbers:
        raise FinishError("Banする文明番号を1つ以上指定してください。")

    return numbers


def draft_slot_and_civilization_index(team_index: int, number: int, slots: list[list[DraftCivilization]]) -> tuple[int, int]:
    if number < 1:
        raise FinishError("文明番号は1以上で指定してください。")

    player_offset, civilization_index = divmod(number - 1, DRAFT_CIVS_PER_PLAYER)
    slot_index = player_offset * 2 + team_index
    if slot_index >= len(slots):
        team_name = f"チーム{team_index + 1}"
        max_number = len(slots[team_index::2]) * DRAFT_CIVS_PER_PLAYER
        raise FinishError(f"{team_name} の文明番号は1〜{max_number}で指定してください。")

    if civilization_index >= len(slots[slot_index]):
        raise FinishError(f"文明番号 {number} に対応する文明が見つかりません。")

    return slot_index, civilization_index


async def apply_draft_bans(interaction: discord.Interaction, *, team_index: int, no: str) -> None:
    draft_state = next(
        (
            LATEST_DRAFTS_BY_CHANNEL[channel_id]
            for channel_id in channel_lookup_ids(interaction)
            if channel_id in LATEST_DRAFTS_BY_CHANNEL
        ),
        None,
    )
    if draft_state is None:
        await interaction.response.send_message("このチャンネルにドラフト表が見つかりません。先に `/draft` を実行してください。", ephemeral=True)
        return

    try:
        numbers = parse_ban_numbers(no)
        for number in numbers:
            slot_index, civilization_index = draft_slot_and_civilization_index(team_index, number, draft_state.slots)
            draft_state.banned_civilization_indexes[slot_index] = civilization_index
    except FinishError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    try:
        await draft_state.message.edit(
            embed=create_draft_embed(draft_state.slots, draft_state.banned_civilization_indexes)
        )
    except discord.HTTPException as exc:
        logging.exception("Failed to update the draft embed.")
        await interaction.response.send_message(f"ドラフト表の更新に失敗しました: {exc}", ephemeral=True)
        return

    team_name = f"チーム{team_index + 1}"
    await interaction.response.send_message(f"{team_name} の {', '.join(str(number) for number in numbers)} 番をBanしました。", ephemeral=True)


def rating_lookup_candidates(user: discord.abc.User) -> list[str]:
    candidates = [
        str(user.id),
        user.name,
        user.display_name,
    ]

    global_name = getattr(user, "global_name", None)
    if global_name:
        candidates.append(global_name)

    return candidates


def get_spreadsheet():
    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not service_account_file:
        raise FinishError("GOOGLE_SERVICE_ACCOUNT_FILE is not configured.")

    configured_path = Path(service_account_file).expanduser()
    if not configured_path.is_file():
        project_path = Path(__file__).resolve().parent / configured_path.name
        if project_path.is_file():
            configured_path = project_path

    import gspread

    try:
        client = gspread.service_account(filename=str(configured_path))
        return client.open_by_key(RATINGS_SPREADSHEET_ID)
    except Exception as exc:
        logging.exception("Failed to connect to the ratings spreadsheet.")
        raise FinishError(
            "スプレッドシートへの接続に失敗しました。"
            "Botの管理者に連絡してください。"
        ) from exc


def get_worksheet_by_title(title: str):
    spreadsheet = get_spreadsheet()
    try:
        return spreadsheet.worksheet(title)
    except Exception as exc:
        raise FinishError(f"スプレッドシートに「{title}」シートが見つかりません。") from exc


def load_rating_rows() -> tuple[object, list[str], list[dict[str, str]]]:
    worksheet = get_worksheet_by_title(PLAYER_LIST_SHEET_TITLE)
    values = worksheet.get_all_values()
    if not values:
        raise FinishError("ratingシートが空です。")

    headers = values[0]
    rows: list[dict[str, str]] = []
    for index, values_row in enumerate(values[1:], start=2):
        row = {header: values_row[column] if column < len(values_row) else "" for column, header in enumerate(headers)}
        row["_row_number"] = str(index)
        rows.append(row)

    return worksheet, headers, rows


def load_rows_by_headers(sheet_title: str) -> tuple[object, list[str], list[dict[str, str]]]:
    worksheet = get_worksheet_by_title(sheet_title)
    values = worksheet.get_all_values()
    if not values:
        raise FinishError(f"「{sheet_title}」シートが空です。")

    headers = values[0]
    rows: list[dict[str, str]] = []
    for index, values_row in enumerate(values[1:], start=2):
        if not any((value or "").strip() for value in values_row):
            continue

        row = {header: values_row[column] if column < len(values_row) else "" for column, header in enumerate(headers)}
        row["_row_number"] = str(index)
        rows.append(row)

    return worksheet, headers, rows


def column_label(column_number: int) -> str:
    label = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        label = chr(65 + remainder) + label
    return label


def append_row_by_headers(sheet_title: str, row: dict[str, object]) -> None:
    append_rows_by_headers(sheet_title, [row])


def append_rows_by_headers(sheet_title: str, rows: list[dict[str, object]]) -> None:
    if not rows:
        return

    worksheet = get_worksheet_by_title(sheet_title)
    values = worksheet.get_all_values()
    if not values:
        raise FinishError(f"「{sheet_title}」シートにヘッダー行がありません。")

    headers = values[0]
    worksheet.append_rows(
        [[str(row.get(header, "")) for header in headers] for row in rows],
        value_input_option="USER_ENTERED",
    )


def append_player_participation_history(user: discord.abc.User) -> None:
    worksheet = get_worksheet_by_title(PLAYER_PARTICIPATION_HISTORY_SHEET_TITLE)
    joined_at = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    worksheet.append_row(
        [
            joined_at,
            str(user.id),
            "-",
            "新規",
            "-",
            DEFAULT_RATING,
            "0",
            "",
        ],
        value_input_option="USER_ENTERED",
    )


def write_finish_rows_to_sheets(
    team_row: dict[str, object],
    personal_rows: list[dict[str, object]],
) -> None:
    try:
        append_row_by_headers("対戦結果_チーム", team_row)
        append_rows_by_headers("対戦結果_個人", personal_rows)
    except FinishError:
        raise
    except Exception as exc:
        raise FinishError(f"対戦結果のスプレッドシート更新に失敗しました: {exc}") from exc


def parse_sheet_rating(value: object) -> int:
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError as exc:
        raise FinishError(f"レート値を読み取れませんでした: {value}") from exc


def revert_latest_finish_from_sheets() -> RevertResult:
    try:
        team_worksheet, _, team_rows = load_rows_by_headers("対戦結果_チーム")
        if not team_rows:
            raise FinishError("削除できる対戦結果がありません。")

        latest_team_row = team_rows[-1]
        finished_at = latest_team_row.get("対戦日（決着日時）", "").strip()
        if not finished_at:
            raise FinishError("直前の対戦結果の決着日時を読み取れませんでした。")

        personal_worksheet, _, personal_rows = load_rows_by_headers("対戦結果_個人")
        matched_personal_rows = [
            row
            for row in personal_rows
            if row.get("対戦日（決着日時）", "").strip() == finished_at
        ]
        if not matched_personal_rows:
            raise FinishError(f"決着日時「{finished_at}」に対応する個人結果が見つかりません。")

        rating_reverts: list[tuple[str, int, int]] = []
        for row in matched_personal_rows:
            player_name = row.get("プレイヤー", "").strip()
            old_rating = parse_sheet_rating(row.get("元レート", ""))
            new_rating = parse_sheet_rating(row.get("新レート", ""))
            if not player_name:
                raise FinishError("個人結果のプレイヤー名を読み取れませんでした。")

            rating_reverts.append((player_name, new_rating, old_rating))

        for row in sorted(matched_personal_rows, key=lambda item: int(item["_row_number"]), reverse=True):
            personal_worksheet.delete_rows(int(row["_row_number"]))

        team_worksheet.delete_rows(int(latest_team_row["_row_number"]))
        return RevertResult(finished_at=finished_at, rating_reverts=rating_reverts)
    except FinishError:
        raise
    except Exception as exc:
        raise FinishError(f"直前の対戦結果の巻き戻しに失敗しました: {exc}") from exc


def winning_text(winner: int) -> str:
    if winner == 1:
        return "チーム1の勝利"
    if winner == 2:
        return "チーム2の勝利"
    return "引き分け"


def personal_result_text(winner: int, team_name: str) -> str:
    if winner == 0:
        return "引き分け"
    if (winner == 1 and team_name == "チーム1") or (winner == 2 and team_name == "チーム2"):
        return "勝利"
    return "敗北"


def expected_score(player_rating: int, opponent_rating: int) -> float:
    return 1 / (1 + 10 ** ((opponent_rating - player_rating) / 400))


def calculate_blended_rating_delta(player_rating: int, team_rating: int, opponent_rating: int, score: float) -> int:
    team_delta = ELO_K_FACTOR * (score - expected_score(team_rating, opponent_rating))
    personal_delta = ELO_K_FACTOR * (score - expected_score(player_rating, opponent_rating))
    return round(team_delta * TEAM_AVERAGE_RATING_WEIGHT + personal_delta * PERSONAL_RATING_WEIGHT)


def average_rating(participants: list[Participant]) -> int:
    if not participants:
        return 0

    return round(sum(rating_value(participant) for participant in participants) / len(participants))


def format_rating_delta(old_rating: int, new_rating: int) -> str:
    delta = new_rating - old_rating
    return f"{delta:+d}"


def format_team_result_rows(result: FinishResult, team_name: str) -> str:
    rows = [
        f"<@{player.user_id}>: {player.new_rating} ({format_rating_delta(player.old_rating, player.new_rating)})"
        for player in sorted(result.player_results, key=lambda player: (-player.old_rating, player.player_name.lower()))
        if player.team_name == team_name
    ]
    return "\n".join(rows) if rows else "なし"


def create_finish_embed(result: FinishResult) -> discord.Embed:
    embed = discord.Embed(title="対戦結果", color=discord.Color.blue())
    embed.add_field(name="勝敗", value=result.winner_text, inline=True)
    embed.add_field(name="マップ", value=result.map_name, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="チーム1", value=format_team_result_rows(result, "チーム1"), inline=True)
    embed.add_field(name="チーム2", value=format_team_result_rows(result, "チーム2"), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    add_result_table_link(embed)

    return embed


def create_revert_embed(result: RevertResult) -> discord.Embed:
    embed = discord.Embed(title="対戦結果を巻き戻しました", color=discord.Color.orange())
    embed.add_field(name="削除した対戦日（決着日時）", value=result.finished_at, inline=False)

    rows = [
        f"{player_name}: {new_rating} → {old_rating}"
        for player_name, new_rating, old_rating in result.rating_reverts
    ]
    embed.add_field(name="戻したレート", value="\n".join(rows) if rows else "なし", inline=False)
    return embed


async def send_finish_embed(interaction: discord.Interaction, result: FinishResult) -> None:
    embed = create_finish_embed(result)
    if interaction.channel is not None:
        await interaction.channel.send(embed=embed)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            logging.exception("Failed to delete the deferred finish interaction response.")
        return

    await interaction.followup.send(embed=embed)


async def send_revert_embed(interaction: discord.Interaction, result: RevertResult) -> None:
    embed = create_revert_embed(result)
    if interaction.channel is not None:
        await interaction.channel.send(embed=embed)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            logging.exception("Failed to delete the deferred revert interaction response.")
        return

    await interaction.followup.send(embed=embed)


async def participant_names(participants: list[Participant]) -> dict[int, str]:
    names: dict[int, str] = {}
    for participant in participants:
        user = bot.get_user(participant.user_id)
        if user is None:
            try:
                user = await bot.fetch_user(participant.user_id)
            except discord.HTTPException:
                user = None

        names[participant.user_id] = user.name if user is not None else str(participant.user_id)

    return names


async def fetch_map_poll_channel_by_state(state: BattleState) -> discord.abc.Messageable:
    fetched = await bot.fetch_channel(state.map_poll_channel_id)
    if not hasattr(fetched, "fetch_message"):
        raise FinishError("マップ投票チャンネルを取得できませんでした。")

    return fetched


async def fetch_map_poll_channel(interaction: discord.Interaction, state: BattleState) -> discord.abc.Messageable:
    channel = interaction.channel
    if channel is not None and interaction.channel_id == state.map_poll_channel_id:
        return channel

    guild = interaction.guild
    if guild is not None:
        channel = guild.get_channel_or_thread(state.map_poll_channel_id)
        if channel is not None:
            return channel

    return await fetch_map_poll_channel_by_state(state)


async def collect_map_result(state: BattleState) -> str:
    poll_state = MAP_POLL_STATES_BY_MESSAGE.get(state.map_poll_message_id)
    if poll_state is None:
        raise FinishError("マップ投票の状態を取得できませんでした。")
    if not poll_state.finalized or not poll_state.selected_map:
        raise FinishError("マップ投票がまだ確定されていません。")

    return poll_state.selected_map


async def write_finish_results(
    *,
    winner: int,
    state: BattleState,
    map_name: str,
    end_turn: str,
) -> FinishResult:
    if state.team_1 is None or state.team_2 is None:
        raise FinishError("直前のチーム分けが見つかりません。先に `⚔️` ボタンでチーム分けを作成してください。")

    team_1, team_2 = copy_teams(state.team_1, state.team_2)
    all_names = await participant_names([*team_1, *team_2])
    finished_at = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    result_text = winning_text(winner)
    team_1_name_list = [all_names[participant.user_id] for participant in team_1]
    team_2_name_list = [all_names[participant.user_id] for participant in team_2]
    team_1_names = "/".join(team_1_name_list)
    team_2_names = "/".join(team_2_name_list)
    team_row = {
        "対戦日（決着日時）": finished_at,
        "マップ": map_name,
        "終了ターン": end_turn,
        "チーム１": team_1_names,
        "チーム２": team_2_names,
        "結果": result_text,
    }

    team_1_average = average_rating(team_1)
    team_2_average = average_rating(team_2)
    score_by_team = {0: (0.5, 0.5), 1: (1.0, 0.0), 2: (0.0, 1.0)}[winner]

    personal_rows = []
    player_results: list[PlayerResult] = []
    for team_name, participants, team_average, opponent_average, score in [
        ("チーム1", team_1, team_1_average, team_2_average, score_by_team[0]),
        ("チーム2", team_2, team_2_average, team_1_average, score_by_team[1]),
    ]:
        for participant in participants:
            old_rating = rating_value(participant)
            rating_delta = calculate_blended_rating_delta(old_rating, team_average, opponent_average, score)
            new_rating = old_rating if winner == 0 else old_rating + rating_delta
            player_name = all_names[participant.user_id]
            personal_rows.append(
                {
                    "対戦日（決着日時）": finished_at,
                    "プレイヤー": player_name,
                    "チーム": team_name,
                    "勝敗": personal_result_text(winner, team_name),
                    "元レート": old_rating,
                    "新レート": new_rating,
                }
            )
            player_results.append(
                PlayerResult(
                    user_id=participant.user_id,
                    player_name=player_name,
                    team_name=team_name,
                    old_rating=old_rating,
                    new_rating=new_rating,
                )
            )

    await asyncio.to_thread(
        write_finish_rows_to_sheets,
        team_row,
        personal_rows,
    )

    return FinishResult(
        winner_text=result_text,
        map_name=map_name,
        end_turn=end_turn,
        team_1_names=team_1_name_list,
        team_2_names=team_2_name_list,
        player_results=player_results,
    )


async def find_or_register_rating_for_user(user: discord.abc.User) -> str:
    try:
        ratings = await asyncio.to_thread(load_ratings)
    except Exception as exc:
        logging.exception("Failed to load ratings from Google Sheets.")
        raise RatingRegistrationError("ratingシートの読み込みに失敗しました。") from exc

    candidates = rating_lookup_candidates(user)
    for candidate in candidates:
        rating = ratings.get(normalize_rating_key(candidate))
        if rating:
            return rating

    try:
        await asyncio.to_thread(append_player_participation_history, user)
    except FinishError as exc:
        raise RatingRegistrationError(str(exc)) from exc
    except Exception as exc:
        logging.exception("Failed to append player participation history to Google Sheets.")
        raise RatingRegistrationError("プレイヤー参加履歴への新規ユーザー追加に失敗しました。") from exc

    return DEFAULT_RATING


async def participants_with_current_ratings(
    interaction: discord.Interaction,
    user_ids: list[int],
    ratings: dict[str, str],
) -> list[Participant]:
    participants: list[Participant] = []
    missing_users: list[str] = []

    for user_id in user_ids:
        user = interaction.guild.get_member(user_id) if interaction.guild is not None else None
        if user is None:
            user = bot.get_user(user_id)
        if user is None:
            try:
                user = await bot.fetch_user(user_id)
            except discord.HTTPException as exc:
                raise FinishError(f"<@{user_id}> のDiscordユーザー情報を取得できませんでした。") from exc

        rating = next(
            (
                ratings[normalize_rating_key(candidate)]
                for candidate in rating_lookup_candidates(user)
                if normalize_rating_key(candidate) in ratings
            ),
            None,
        )
        if rating is None:
            try:
                await asyncio.to_thread(append_player_participation_history, user)
            except Exception:
                logging.exception("Failed to append player participation history for /record.")
                missing_users.append(user.mention)
                continue
            rating = DEFAULT_RATING

        participants.append(Participant(user_id=user_id, rating=rating))

    if missing_users:
        raise FinishError(f"プレイヤー参加履歴への追加に失敗したユーザーがいます: {' '.join(missing_users)}")

    return participants


async def scan_role_members_to_player_list(role: discord.Role) -> tuple[int, list[str]]:
    guild = role.guild
    try:
        await asyncio.wait_for(guild.chunk(cache=True), timeout=30)
    except asyncio.TimeoutError as exc:
        raise RatingRegistrationError("サーバーメンバーの取得がタイムアウトしました。") from exc
    except discord.HTTPException as exc:
        raise RatingRegistrationError("サーバーメンバーの取得に失敗しました。") from exc

    members = sorted((member for member in role.members if not member.bot), key=lambda member: member.name.lower())
    try:
        ratings = await asyncio.to_thread(load_ratings)
    except Exception as exc:
        logging.exception("Failed to load ratings from Google Sheets.")
        raise RatingRegistrationError("ratingシートの読み込みに失敗しました。") from exc

    names_to_add: list[str] = []
    for member in members:
        if any(ratings.get(normalize_rating_key(candidate)) for candidate in rating_lookup_candidates(member)):
            continue

        names_to_add.append(member.name)

    return len(members), names_to_add


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        logging.warning("Interaction expired before it could be acknowledged.")
        return False


class FinishMapSelectionView(discord.ui.View):
    def __init__(self, *, requester_id: int, winner: int, state: BattleState) -> None:
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.winner = winner
        self.state = state
        self.add_item(FinishMapSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("このマップ選択はコマンド実行者だけが操作できます。", ephemeral=True)
            return False

        return True

    async def record(self, interaction: discord.Interaction, map_key: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        map_name = map_option_display(map_key)
        try:
            result = await write_finish_results(
                winner=self.winner,
                state=self.state,
                map_name=map_name,
                end_turn="",
            )
        except FinishError as exc:
            await interaction.followup.send(f"対戦結果の記録に失敗しました: {exc}", ephemeral=True)
            return

        try:
            await interaction.message.delete()
        except discord.HTTPException:
            logging.exception("Failed to delete the finish map selection message.")

        await send_finish_embed(interaction, result)
        self.stop()


class FinishMapSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="記録するマップを選択してください",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=key, emoji=emoji)
                for key, emoji, label in MAP_POLL_OPTIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FinishMapSelectionView):
            await interaction.response.send_message("マップ選択UIの状態を読み取れませんでした。", ephemeral=True)
            return

        await view.record(interaction, self.values[0])


class BattleView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label=BATTLE_TOGGLE_LABEL, style=discord.ButtonStyle.primary, custom_id=BATTLE_JOIN_CUSTOM_ID)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.message is None or not interaction.message.embeds:
            await interaction.response.send_message("募集メッセージが見つかりませんでした。", ephemeral=True)
            return

        if interaction.channel_id is not None:
            LATEST_BATTLE_MESSAGES_BY_CHANNEL[interaction.channel_id] = interaction.message

        if not await safe_defer(interaction):
            return

        embed = interaction.message.embeds[0]
        participants = read_participants(embed)
        existing_participant = next(
            (participant for participant in participants if participant.user_id == interaction.user.id),
            None,
        )
        if existing_participant is not None:
            participants.remove(existing_participant)
            update_participants(embed, participants)
            if interaction.channel_id is not None:
                LATEST_PARTICIPANTS_BY_CHANNEL[interaction.channel_id] = [
                    Participant(participant.user_id, participant.rating) for participant in participants
                ]
                await update_map_poll_message_for_channel(interaction.channel_id)
            await edit_interaction_message(interaction, embed=embed, view=self)
            return

        try:
            rating = await find_or_register_rating_for_user(interaction.user)
        except RatingRegistrationError as exc:
            await interaction.followup.send(
                f"ratingの取得または登録に失敗しました: {exc}",
                ephemeral=True,
            )
            return

        participants.append(Participant(user_id=interaction.user.id, rating=rating))
        update_participants(embed, participants)
        if interaction.channel_id is not None:
            LATEST_PARTICIPANTS_BY_CHANNEL[interaction.channel_id] = [
                Participant(participant.user_id, participant.rating) for participant in participants
            ]
            await update_map_poll_message_for_channel(interaction.channel_id)
        await edit_interaction_message(interaction, embed=embed, view=self)

    @discord.ui.button(label=BATTLE_BALANCE_LABEL, style=discord.ButtonStyle.success, custom_id=BATTLE_BALANCE_CUSTOM_ID)
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.message is None or not interaction.message.embeds:
            await interaction.response.send_message("募集メッセージが見つかりませんでした。", ephemeral=True)
            return

        if not await safe_defer(interaction):
            return
        participants = read_participants(interaction.message.embeds[0])
        if len(participants) < 2:
            await interaction.followup.send("チーム分けには2人以上の参加者が必要です。", ephemeral=True)
            return

        team_1, team_2 = balance_teams(participants)
        if interaction.channel_id is not None:
            remember_latest_teams(interaction.channel_id, team_1, team_2)

        await interaction.followup.send(embed=create_team_embed_from_teams(team_1, team_2))


class AmphitheaterBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.guild_id = self._read_guild_id()

    async def setup_hook(self) -> None:
        self.add_view(BattleView())

        guild_ids = set(BATTLE_ROLE_IDS)
        if self.guild_id is not None:
            guild_ids.add(self.guild_id)

        if not guild_ids:
            try:
                synced = await asyncio.wait_for(self.tree.sync(), timeout=20)
                logging.info("Synced %s global command(s).", len(synced))
            except (asyncio.TimeoutError, discord.HTTPException):
                logging.exception("Failed to sync global commands. Continuing with the existing registered commands.")
            return

        for guild_id in guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await asyncio.wait_for(self.tree.sync(guild=guild), timeout=20)
                logging.info("Synced %s command(s) to guild %s.", len(synced), guild_id)
            except (asyncio.TimeoutError, discord.HTTPException):
                logging.exception("Failed to sync commands to guild %s. Continuing with the existing registered commands.", guild_id)

    async def on_ready(self) -> None:
        if self.user is None:
            return

        logging.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        if not self.guilds:
            logging.info("Not connected to any guilds yet. Invite the bot to a server to use slash commands.")

        for guild in self.guilds:
            logging.info("Connected guild: %s (ID: %s)", guild.name, guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logging.info("Joined %s (ID: %s) and synced %s command(s).", guild.name, guild.id, len(synced))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user is not None and payload.user_id == self.user.id:
            return

        poll_state = MAP_POLL_STATES_BY_MESSAGE.get(payload.message_id)
        if poll_state is None:
            return

        emoji = str(payload.emoji)
        option_key = map_option_key_for_emoji(emoji)
        is_confirm = emoji == MAP_POLL_CONFIRM_EMOJI
        if option_key is None and not is_confirm:
            return

        channel = self.get_channel(payload.channel_id)
        if channel is None:
            fetched = await self.fetch_channel(payload.channel_id)
            if not hasattr(fetched, "fetch_message"):
                return
            channel = fetched

        message = await channel.fetch_message(payload.message_id)
        await remove_user_reaction(message, payload.emoji, payload.user_id)

        if poll_state.finalized:
            return

        participant_ids = {
            participant.user_id
            for participant in LATEST_PARTICIPANTS_BY_CHANNEL.get(poll_state.parent_channel_id, [])
        }
        if payload.user_id not in participant_ids:
            return

        if is_confirm:
            for user_id in list(poll_state.votes_by_user):
                if user_id not in participant_ids:
                    del poll_state.votes_by_user[user_id]

            finalize_map_poll(poll_state)
            await message.edit(embed=create_map_poll_embed(poll_state))
            await clear_map_poll_reactions(message)
            return

        votes = poll_state.votes_by_user.setdefault(payload.user_id, set())
        if option_key in votes:
            votes.remove(option_key)
        else:
            votes.add(option_key)

        await message.edit(embed=create_map_poll_embed(poll_state, non_voters=map_poll_non_voter_mentions(poll_state)))

    @staticmethod
    def _read_guild_id() -> int | None:
        raw_guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if not raw_guild_id:
            return None

        try:
            return int(raw_guild_id)
        except ValueError as exc:
            raise RuntimeError("DISCORD_GUILD_ID must be a numeric Discord server ID.") from exc


bot = AmphitheaterBot()


@bot.tree.command(name="battle", description="Civilization6のマルチプレイ卓を募集します")
@app_commands.describe(name="募集タイトル")
async def battle(interaction: discord.Interaction, name: str | None = None) -> None:
    title = name.strip() if name and name.strip() else "募集"
    embed = discord.Embed(title=title, color=discord.Color.gold())
    try:
        owner_rating = await find_or_register_rating_for_user(interaction.user)
    except RatingRegistrationError as exc:
        await interaction.response.send_message(
            f"ratingの取得または登録に失敗しました: {exc}",
            ephemeral=True,
        )
        return

    participants = [Participant(user_id=interaction.user.id, rating=owner_rating)]
    update_participants(embed, participants)
    add_member_list_link(embed)
    role_id = get_battle_role_id(interaction.guild_id)
    role = interaction.guild.get_role(role_id) if interaction.guild is not None else None
    if role is None:
        await interaction.response.send_message(
            f"募集通知ロール `<@&{role_id}>` がこのサーバーに見つかりません。ロールID設定を確認してください。",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        content=role.mention,
        embed=embed,
        view=BattleView(),
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
    )

    try:
        battle_message = await interaction.original_response()
        if interaction.channel_id is not None:
            LATEST_BATTLE_MESSAGES_BY_CHANNEL[interaction.channel_id] = battle_message
            LATEST_PARTICIPANTS_BY_CHANNEL[interaction.channel_id] = [
                Participant(participant.user_id, participant.rating) for participant in participants
            ]

        if interaction.channel is None:
            await interaction.followup.send("マップ投票を投稿するチャンネルを取得できませんでした。", ephemeral=True)
            return

        if interaction.channel_id is not None:
            THREAD_IDS_BY_PARENT_CHANNEL[interaction.channel_id] = interaction.channel_id
            BATTLE_STATES_BY_THREAD[interaction.channel_id] = BattleState(
                parent_channel_id=interaction.channel_id,
                map_poll_channel_id=interaction.channel_id,
                map_poll_message_id=0,
            )
            await create_new_map_poll(interaction.channel, interaction.channel_id)
    except discord.Forbidden:
        permission_summary = describe_bot_thread_permissions(interaction)
        await interaction.followup.send(
            "マップ投票の投稿に必要な権限がありません。\n"
            f"{permission_summary}",
            ephemeral=True,
        )
    except discord.HTTPException:
        logging.exception("Failed to create a battle thread or send the map poll.")
        await interaction.followup.send("マップ投票の投稿に失敗しました。", ephemeral=True)


@bot.tree.command(name="map", description="現在の募集のマップ投票をやり直します")
async def map(interaction: discord.Interaction) -> None:
    if interaction.channel_id is None or interaction.channel is None:
        await interaction.response.send_message("マップ投票を投稿するチャンネルを取得できませんでした。", ephemeral=True)
        return

    state = get_state_for_interaction(interaction)
    if state is None:
        await interaction.response.send_message(
            "このチャンネルに対応する募集が見つかりません。先に `/battle` で募集を作成してください。",
            ephemeral=True,
        )
        return

    if not await safe_defer(interaction, ephemeral=True):
        return

    try:
        await create_new_map_poll(interaction.channel, state.parent_channel_id)
    except discord.Forbidden:
        permission_summary = describe_bot_thread_permissions(interaction)
        await interaction.followup.send(
            "マップ投票の投稿に必要な権限がありません。\n"
            f"{permission_summary}",
            ephemeral=True,
        )
        return
    except discord.HTTPException as exc:
        logging.exception("Failed to recreate the map poll.")
        await interaction.followup.send(f"マップ投票のやり直しに失敗しました: {exc}", ephemeral=True)
        return

    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        logging.exception("Failed to delete the deferred map interaction response.")

@bot.tree.command(name="remove", description="募集参加者から指定ユーザーを除外します")
@app_commands.describe(user="除外したいユーザー")
async def remove(interaction: discord.Interaction, user: discord.User) -> None:
    latest_message = latest_battle_message_for(interaction)
    if latest_message is None:
        await interaction.response.send_message("直近の募集メッセージが見つかりません。先に `/battle` で募集を作成してください。", ephemeral=True)
        return

    channel_id, battle_message = latest_message
    if not battle_message.embeds:
        await interaction.response.send_message("募集メッセージの参加者欄を読み取れませんでした。", ephemeral=True)
        return

    embed = battle_message.embeds[0]
    participants = LATEST_PARTICIPANTS_BY_CHANNEL.get(channel_id)
    if participants is None:
        participants = read_participants(embed)
    else:
        participants = [Participant(participant.user_id, participant.rating) for participant in participants]

    logging.info("Remove requested for %s. Current participants: %s", user.id, [participant.user_id for participant in participants])
    if not any(participant.user_id == user.id for participant in participants):
        await interaction.response.send_message(f"{user.mention} は現在の募集に参加していません。", ephemeral=True)
        return

    participants = [participant for participant in participants if participant.user_id != user.id]
    LATEST_PARTICIPANTS_BY_CHANNEL[channel_id] = [Participant(participant.user_id, participant.rating) for participant in participants]
    update_participants(embed, participants)
    remove_participant_from_latest_teams(channel_id, user.id)
    await update_map_poll_message_for_channel(channel_id)

    try:
        await battle_message.edit(embed=embed, view=BattleView())
    except discord.HTTPException as exc:
        logging.exception("Failed to remove a participant from the battle message.")
        await interaction.response.send_message(f"参加者の除外に失敗しました: {exc}", ephemeral=True)
        return

    await interaction.response.send_message(f"{user.mention} を募集参加者から除外しました。", ephemeral=True)


@bot.tree.command(name="swap", description="直前のチーム分けで2人のチームを入れ替えます")
@app_commands.describe(id="入れ替えたい2人をメンションで指定します。例: @okurisae @hakoeda")
async def swap(interaction: discord.Interaction, id: str) -> None:
    user_ids = read_mentioned_user_ids(id)
    if len(user_ids) != 2:
        await interaction.response.send_message("入れ替えたい2人をメンションで指定してください。例: `/swap id: @okurisae @hakoeda`", ephemeral=True)
        return

    user1_id, user2_id = user_ids
    if user1_id == user2_id:
        await interaction.response.send_message("同じユーザー同士は入れ替えできません。", ephemeral=True)
        return

    if not await safe_defer(interaction, ephemeral=True):
        return

    state = get_state_for_interaction(interaction)
    teams = None
    if state is not None and state.team_1 is not None and state.team_2 is not None:
        teams = (state.team_1, state.team_2)
    if teams is None:
        teams = LATEST_TEAMS_BY_CHANNEL.get(interaction.channel_id or 0)
    if teams is None:
        await interaction.followup.send("直前のチーム分けが見つかりませんでした。先に `⚔️` ボタンでチーム分けを作成してください。", ephemeral=True)
        return

    team_1, team_2 = copy_teams(*teams)
    user1_team = 1 if any(participant.user_id == user1_id for participant in team_1) else 2 if any(
        participant.user_id == user1_id for participant in team_2
    ) else None
    user2_team = 1 if any(participant.user_id == user2_id for participant in team_1) else 2 if any(
        participant.user_id == user2_id for participant in team_2
    ) else None

    missing_users = []
    if user1_team is None:
        missing_users.append(f"<@{user1_id}>")
    if user2_team is None:
        missing_users.append(f"<@{user2_id}>")
    if missing_users:
        await interaction.followup.send(
            f"{'、'.join(missing_users)} は直前のチーム分けに含まれていません。",
            ephemeral=True,
        )
        return

    if user1_team == user2_team:
        await interaction.followup.send("指定された2人は同じチームにいるため、入れ替えできません。", ephemeral=True)
        return

    source_team = team_1 if user1_team == 1 else team_2
    target_team = team_2 if user1_team == 1 else team_1
    source_index = next(index for index, participant in enumerate(source_team) if participant.user_id == user1_id)
    target_index = next(index for index, participant in enumerate(target_team) if participant.user_id == user2_id)
    source_team[source_index], target_team[target_index] = target_team[target_index], source_team[source_index]

    if state is not None:
        state.team_1, state.team_2 = copy_teams(team_1, team_2)
        remember_latest_teams(state.parent_channel_id, team_1, team_2)
    elif interaction.channel_id is not None:
        remember_latest_teams(interaction.channel_id, team_1, team_2)

    await interaction.followup.send(f"<@{user1_id}> と <@{user2_id}> のチームを入れ替えました。", ephemeral=True)
    await interaction.followup.send(embed=create_team_embed_from_teams(team_1, team_2))


@bot.tree.command(name="scan", description="指定ロールのメンバーがプレイヤーリストに登録済みか確認します")
@app_commands.describe(role="スキャンしたいロール")
async def scan(interaction: discord.Interaction, role: discord.Role) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("このコマンドはサーバー内で実行してください。", ephemeral=True)
        return

    if not await safe_defer(interaction, ephemeral=True):
        return

    try:
        scanned_count, missing_names = await scan_role_members_to_player_list(role)
    except RatingRegistrationError as exc:
        await interaction.followup.send(f"プレイヤーリストの確認に失敗しました: {exc}", ephemeral=True)
        return

    if not missing_names:
        await interaction.followup.send(
            f"{role.mention} のメンバー {scanned_count} 人を確認しました。未登録ユーザーはいませんでした。",
            ephemeral=True,
        )
        return

    preview_names = "、".join(missing_names[:10])
    if len(missing_names) > 10:
        preview_names += f"、ほか {len(missing_names) - 10} 人"

    await interaction.followup.send(
        f"{role.mention} のメンバー {scanned_count} 人を確認しました。未登録ユーザーが {len(missing_names)} 人います。\n"
        f"未登録: {preview_names}",
        ephemeral=True,
    )


@bot.tree.command(name="draft", description="参加人数分の文明ドラフト候補を作成します")
@app_commands.describe(player_count="参加人数。省略時は現在の募集参加者数を使います")
async def draft(interaction: discord.Interaction, player_count: int | None = None) -> None:
    resolved_player_count = draft_player_count_for(interaction, player_count)
    if resolved_player_count is None:
        await interaction.response.send_message(
            "現在の募集参加者数を取得できませんでした。例: `/draft player_count:8` のように参加人数を指定してください。",
            ephemeral=True,
        )
        return

    if resolved_player_count < 1 or resolved_player_count > DRAFT_MAX_PLAYERS:
        await interaction.response.send_message(
            f"参加人数は1〜{DRAFT_MAX_PLAYERS}人で指定してください。",
            ephemeral=True,
        )
        return

    if not await safe_defer(interaction):
        return

    try:
        civilizations = await asyncio.to_thread(load_civilization_tier_list)
        slots = distribute_draft_civilizations(civilizations, resolved_player_count)
    except Exception as exc:
        logging.exception("Failed to create a civilization draft.")
        await interaction.followup.send(f"文明ドラフトの作成に失敗しました: {exc}", ephemeral=True)
        return

    draft_message = await interaction.followup.send(embed=create_draft_embed(slots), wait=True)
    if interaction.channel_id is not None:
        LATEST_DRAFTS_BY_CHANNEL[interaction.channel_id] = DraftState(
            channel_id=interaction.channel_id,
            message=draft_message,
            slots=slots,
            banned_civilization_indexes={},
        )


@bot.tree.command(name="ban1", description="直近のドラフト表でチーム1の文明をBanします")
@app_commands.describe(no="Banする文明番号。例: 1 6 9 / 1,6,9")
async def ban1(interaction: discord.Interaction, no: str) -> None:
    await apply_draft_bans(interaction, team_index=0, no=no)


@bot.tree.command(name="ban2", description="直近のドラフト表でチーム2の文明をBanします")
@app_commands.describe(no="Banする文明番号。例: 1 6 9 / 1,6,9")
async def ban2(interaction: discord.Interaction, no: str) -> None:
    await apply_draft_bans(interaction, team_index=1, no=no)


@bot.tree.command(name="record", description="指定したチームの対戦結果とレート変動を記録します")
@app_commands.describe(
    team1="チーム1のユーザーをメンションでスペース区切り指定します",
    team2="チーム2のユーザーをメンションでスペース区切り指定します",
    winner="勝利チーム。0は引き分けです",
)
@app_commands.choices(
    winner=[
        app_commands.Choice(name="0: 引き分け", value=0),
        app_commands.Choice(name="1: チーム1の勝利", value=1),
        app_commands.Choice(name="2: チーム2の勝利", value=2),
    ]
)
async def record(interaction: discord.Interaction, team1: str, team2: str, winner: int) -> None:
    try:
        team_1_user_ids = parse_team_mentions(team1, "team1")
        team_2_user_ids = parse_team_mentions(team2, "team2")
    except FinishError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    overlapping_user_ids = set(team_1_user_ids) & set(team_2_user_ids)
    if overlapping_user_ids:
        mentions = " ".join(f"<@{user_id}>" for user_id in sorted(overlapping_user_ids))
        await interaction.response.send_message(
            f"同じユーザーを両方のチームに指定することはできません: {mentions}",
            ephemeral=True,
        )
        return

    if not await safe_defer(interaction, ephemeral=True):
        return

    try:
        ratings = await asyncio.to_thread(load_ratings)
        team_1 = await participants_with_current_ratings(interaction, team_1_user_ids, ratings)
        team_2 = await participants_with_current_ratings(interaction, team_2_user_ids, ratings)
    except FinishError as exc:
        await interaction.edit_original_response(content=f"対戦メンバーの読み取りに失敗しました: {exc}", view=None)
        return
    except Exception as exc:
        logging.exception("Failed to prepare players for /record.")
        await interaction.edit_original_response(
            content=f"プレイヤーリストの読み込みに失敗しました: {exc}",
            view=None,
        )
        return

    channel_id = interaction.channel_id or 0
    state = BattleState(
        parent_channel_id=channel_id,
        map_poll_channel_id=channel_id,
        map_poll_message_id=0,
        team_1=team_1,
        team_2=team_2,
    )
    await interaction.edit_original_response(
        content="記録するマップを選択してください。",
        view=FinishMapSelectionView(requester_id=interaction.user.id, winner=winner, state=state),
    )


@bot.tree.command(name="finish", description="対戦結果とレート変動を記録します")
@app_commands.describe(
    winner="勝利チーム。0は引き分けです",
    source="セッション切れ時に使うチーム分けメッセージのDiscord URL",
)
@app_commands.choices(
    winner=[
        app_commands.Choice(name="0: 引き分け", value=0),
        app_commands.Choice(name="1: チーム1の勝利", value=1),
        app_commands.Choice(name="2: チーム2の勝利", value=2),
    ]
)
async def finish(interaction: discord.Interaction, winner: int, source: str | None = None) -> None:
    winner_value = winner
    if source:
        if not await safe_defer(interaction, ephemeral=True):
            return

        try:
            state = await state_from_team_message_url(source)
        except FinishError as exc:
            await interaction.followup.send(f"チーム分けメッセージの読み取りに失敗しました: {exc}", ephemeral=True)
            return

        await interaction.edit_original_response(
            content="記録するマップを選択してください。",
            view=FinishMapSelectionView(requester_id=interaction.user.id, winner=winner_value, state=state),
        )
        return

    state = get_state_for_interaction(interaction)
    if state is None:
        await interaction.response.send_message(
            "このチャンネルに対応する募集スレッドが見つかりません。`/battle` で作成されたスレッド内、または募集チャンネルで実行してください。",
            ephemeral=True,
        )
        return

    if state.team_1 is None or state.team_2 is None:
        await interaction.response.send_message("直前のチーム分けが見つかりません。先に `⚔️` ボタンでチーム分けを作成してください。", ephemeral=True)
        return

    if not await safe_defer(interaction, ephemeral=True):
        return

    try:
        map_result = await collect_map_result(state)
    except (discord.Forbidden, discord.NotFound, FinishError) as exc:
        await interaction.followup.send(f"投票結果の取得に失敗しました: {exc}", ephemeral=True)
        return

    try:
        result = await write_finish_results(
            winner=winner_value,
            state=state,
            map_name=map_result,
            end_turn="",
        )
    except FinishError as exc:
        await interaction.followup.send(f"対戦結果の記録に失敗しました: {exc}", ephemeral=True)
        return

    await send_finish_embed(interaction, result)


@bot.tree.command(name="revert", description="直前の一試合の対戦ログを削除し、レートを戻します")
async def revert(interaction: discord.Interaction) -> None:
    if not await safe_defer(interaction, ephemeral=True):
        return

    try:
        result = await asyncio.to_thread(revert_latest_finish_from_sheets)
    except FinishError as exc:
        await interaction.followup.send(f"対戦結果の巻き戻しに失敗しました: {exc}", ephemeral=True)
        return

    await send_revert_embed(interaction, result)


def main() -> None:
    global LOCK_FILE

    LOCK_FILE = open(LOCK_FILE_PATH, "w")
    acquire_process_lock(LOCK_FILE)

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and set your bot token.")

    bot.run(token)


if __name__ == "__main__":
    main()
