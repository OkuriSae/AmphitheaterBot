import asyncio
import csv
import io
import itertools
import logging
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
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
MAP_POLL_DURATION = timedelta(hours=24)
MAP_POLL_OPTIONS = [
    ("⚔️", "パンゲア"),
    ("🏖️", "パンゲアウルティマ"),
    ("⛵", "7つの海"),
    ("⛲", "湖"),
    ("⛰️", "ハイランド"),
    ("🧊", "地軸傾斜"),
]
THREAD_NAME_MAX_LENGTH = 100
TEAM_EMBED_TITLE = "チーム分け"
TEAM_1_FIELD_PREFIX = "チーム1"
TEAM_2_FIELD_PREFIX = "チーム2"
USER_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
RATINGS_SPREADSHEET_ID = "13__lGAuvm00wKJeZro8hGpy9PsulrCHvDiCVxdly7qU"
RATINGS_WORKSHEET_GID = 0
PLAYER_LIST_SHEET_TITLE = "プレイヤーリスト"
DEFAULT_RATING = "1000"
ELO_K_FACTOR = 48
TIMEZONE = ZoneInfo("Asia/Tokyo")
RATINGS_CSV_URL = os.getenv(
    "RATINGS_CSV_URL",
    f"https://docs.google.com/spreadsheets/d/{RATINGS_SPREADSHEET_ID}/export?format=csv&gid={RATINGS_WORKSHEET_GID}",
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
class PlayerResult:
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
    embed.add_field(name="メンバーリスト", value=f"[開く]({MEMBER_LIST_URL})", inline=False)


def add_result_table_link(embed: discord.Embed) -> None:
    embed.add_field(name="結果表", value=f"[開く]({RESULT_TABLE_URL})", inline=False)


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
        ("投票の作成", permissions.create_polls),
    ]
    status = " / ".join(f"{label}: {'OK' if is_allowed else 'NG'}" for label, is_allowed in permission_labels)
    channel_name = getattr(channel, "name", str(channel))
    return f"チャンネル `{channel_name}` でのBot実効権限: {status}"


def create_map_poll() -> discord.Poll:
    poll = discord.Poll(question=MAP_POLL_QUESTION, duration=MAP_POLL_DURATION, multiple=False)
    for emoji, label in MAP_POLL_OPTIONS:
        poll.add_answer(text=label, emoji=emoji)

    return poll


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
    sorted_participants = sorted(participants, key=lambda participant: (rating_value(participant), participant.user_id))
    for index, participant in enumerate(sorted_participants, start=1):
        rating = participant.rating if participant.rating else "未登録"
        lines.append(f"{index}. <@{participant.user_id}> ({rating})")

    return "\n".join(lines)


def create_team_embed_from_teams(team_1: list[Participant], team_2: list[Participant]) -> discord.Embed:
    team_1_rating = sum(rating_value(participant) for participant in team_1)
    team_2_rating = sum(rating_value(participant) for participant in team_2)
    diff = abs(team_1_rating - team_2_rating)

    embed = discord.Embed(title=TEAM_EMBED_TITLE, color=discord.Color.green())
    embed.add_field(name=f"{TEAM_1_FIELD_PREFIX} 合計: {team_1_rating}", value=format_team(team_1), inline=False)
    embed.add_field(name=f"{TEAM_2_FIELD_PREFIX} 合計: {team_2_rating}", value=format_team(team_2), inline=False)
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


def read_mentioned_user_ids(value: str) -> list[int]:
    return [int(user_id) for user_id in USER_MENTION_PATTERN.findall(value)]


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


def append_default_rating(user: discord.abc.User) -> None:
    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not service_account_file:
        raise RatingRegistrationError("GOOGLE_SERVICE_ACCOUNT_FILE is not configured.")

    import gspread

    client = gspread.service_account(filename=service_account_file)
    spreadsheet = client.open_by_key(RATINGS_SPREADSHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(PLAYER_LIST_SHEET_TITLE)
    except Exception as exc:
        raise RatingRegistrationError(f"「{PLAYER_LIST_SHEET_TITLE}」シートが見つかりません。") from exc

    worksheet.append_row([user.name, DEFAULT_RATING], value_input_option="USER_ENTERED")


def append_default_ratings_for_names(names: list[str]) -> None:
    if not names:
        return

    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not service_account_file:
        raise RatingRegistrationError("GOOGLE_SERVICE_ACCOUNT_FILE is not configured.")

    import gspread

    client = gspread.service_account(filename=service_account_file)
    spreadsheet = client.open_by_key(RATINGS_SPREADSHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(PLAYER_LIST_SHEET_TITLE)
    except Exception as exc:
        raise RatingRegistrationError(f"「{PLAYER_LIST_SHEET_TITLE}」シートが見つかりません。") from exc

    worksheet.append_rows([[name, DEFAULT_RATING] for name in names], value_input_option="USER_ENTERED")


def get_spreadsheet():
    service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not service_account_file:
        raise FinishError("GOOGLE_SERVICE_ACCOUNT_FILE is not configured.")

    import gspread

    try:
        client = gspread.service_account(filename=service_account_file)
        return client.open_by_key(RATINGS_SPREADSHEET_ID)
    except Exception as exc:
        raise FinishError(f"スプレッドシートへの接続に失敗しました: {exc}") from exc


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


def update_rating_row(player_keys: list[str], new_rating: int) -> None:
    worksheet, headers, rows = load_rating_rows()
    normalized_keys = {normalize_rating_key(key) for key in player_keys}
    rating_column = headers.index("rating") + 1

    for row in rows:
        if normalize_rating_key(row.get("userid", "")) in normalized_keys:
            worksheet.update_cell(int(row["_row_number"]), rating_column, str(new_rating))
            return

    worksheet.append_row([player_keys[0], str(new_rating)], value_input_option="USER_ENTERED")


def column_label(column_number: int) -> str:
    label = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        label = chr(65 + remainder) + label
    return label


def update_rating_rows(rating_updates: list[tuple[list[str], int]]) -> None:
    if not rating_updates:
        return

    worksheet, headers, rows = load_rating_rows()
    rating_column = headers.index("rating") + 1
    rating_column_label = column_label(rating_column)
    rows_by_user_id = {normalize_rating_key(row.get("userid", "")): row for row in rows}

    batch_updates = []
    rows_to_append = []
    for player_keys, new_rating in rating_updates:
        normalized_keys = [normalize_rating_key(key) for key in player_keys]
        matched_row = next((rows_by_user_id[key] for key in normalized_keys if key in rows_by_user_id), None)
        if matched_row is None:
            rows_to_append.append([player_keys[0], str(new_rating)])
            continue

        row_number = int(matched_row["_row_number"])
        batch_updates.append(
            {
                "range": f"{rating_column_label}{row_number}",
                "values": [[str(new_rating)]],
            }
        )

    if batch_updates:
        worksheet.batch_update(batch_updates, value_input_option="USER_ENTERED")

    if rows_to_append:
        worksheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")


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


def write_finish_rows_to_sheets(
    team_row: dict[str, object],
    personal_rows: list[dict[str, object]],
    rating_updates: list[tuple[list[str], int]],
) -> None:
    try:
        append_row_by_headers("対戦結果_チーム", team_row)
        append_rows_by_headers("対戦結果_個人", personal_rows)
        update_rating_rows(rating_updates)
    except FinishError:
        raise
    except Exception as exc:
        raise FinishError(f"対戦結果のスプレッドシート更新に失敗しました: {exc}") from exc


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


def calculate_new_rating(player_rating: int, opponent_rating: int, score: float) -> int:
    return round(player_rating + ELO_K_FACTOR * (score - expected_score(player_rating, opponent_rating)))


def average_rating(participants: list[Participant]) -> int:
    if not participants:
        return 0

    return round(sum(rating_value(participant) for participant in participants) / len(participants))


def format_rating_delta(old_rating: int, new_rating: int) -> str:
    delta = new_rating - old_rating
    return f"{delta:+d}"


def format_team_result_rows(result: FinishResult, team_name: str) -> str:
    rows = [
        f"{player.player_name}: {player.new_rating} ({format_rating_delta(player.old_rating, player.new_rating)})"
        for player in sorted(result.player_results, key=lambda player: (player.old_rating, player.player_name.lower()))
        if player.team_name == team_name
    ]
    return "\n".join(rows) if rows else "なし"


def create_finish_embed(result: FinishResult) -> discord.Embed:
    embed = discord.Embed(title="対戦結果", color=discord.Color.blue())
    embed.add_field(name="勝敗", value=result.winner_text, inline=False)
    embed.add_field(name="マップ", value=result.map_name, inline=False)
    embed.add_field(name="チーム1", value=format_team_result_rows(result, "チーム1"), inline=False)
    embed.add_field(name="チーム2", value=format_team_result_rows(result, "チーム2"), inline=False)
    add_result_table_link(embed)

    return embed


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


async def fetch_map_poll_channel(interaction: discord.Interaction, state: BattleState) -> discord.abc.Messageable:
    channel = interaction.channel
    if channel is not None and interaction.channel_id == state.map_poll_channel_id:
        return channel

    guild = interaction.guild
    if guild is not None:
        channel = guild.get_channel_or_thread(state.map_poll_channel_id)
        if channel is not None:
            return channel

    fetched = await bot.fetch_channel(state.map_poll_channel_id)
    if not hasattr(fetched, "fetch_message"):
        raise FinishError("マップ投票チャンネルを取得できませんでした。")

    return fetched


async def poll_top_options(channel: discord.abc.Messageable, message_id: int) -> tuple[str | None, list[str]]:
    message = await channel.fetch_message(message_id)
    poll = message.poll
    if poll is None:
        raise FinishError("Pollメッセージを読み取れませんでした。")

    counts = [(answer.text, answer.vote_count or 0) for answer in poll.answers]
    if not counts:
        raise FinishError("Pollに選択肢がありません。")

    max_votes = max(count for _, count in counts)
    winners = [text for text, count in counts if count == max_votes]
    if len(winners) == 1:
        return winners[0], []

    return None, winners


async def collect_poll_results(
    interaction: discord.Interaction,
    state: BattleState,
) -> tuple[str | None, list[str]]:
    channel = await fetch_map_poll_channel(interaction, state)
    return await poll_top_options(channel, state.map_poll_message_id)


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
    rating_updates: list[tuple[list[str], int]] = []
    for team_name, participants, opponent_average, score in [
        ("チーム1", team_1, team_2_average, score_by_team[0]),
        ("チーム2", team_2, team_1_average, score_by_team[1]),
    ]:
        for participant in participants:
            old_rating = rating_value(participant)
            new_rating = old_rating if winner == 0 else calculate_new_rating(old_rating, opponent_average, score)
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
                    player_name=player_name,
                    team_name=team_name,
                    old_rating=old_rating,
                    new_rating=new_rating,
                )
            )
            rating_updates.append(([player_name, str(participant.user_id)], new_rating))

    await asyncio.to_thread(
        write_finish_rows_to_sheets,
        team_row,
        personal_rows,
        rating_updates if winner != 0 else [],
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
        await asyncio.to_thread(append_default_rating, user)
    except RatingRegistrationError:
        raise
    except Exception as exc:
        logging.exception("Failed to append default rating to Google Sheets.")
        raise RatingRegistrationError("ratingシートへの新規ユーザー追加に失敗しました。") from exc

    return DEFAULT_RATING


async def scan_role_members_to_player_list(role: discord.Role) -> tuple[int, int, list[str]]:
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

    try:
        await asyncio.to_thread(append_default_ratings_for_names, names_to_add)
    except RatingRegistrationError:
        raise
    except Exception as exc:
        logging.exception("Failed to append default ratings to Google Sheets.")
        raise RatingRegistrationError("ratingシートへの一括追加に失敗しました。") from exc

    return len(members), len(names_to_add), names_to_add


async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        logging.warning("Interaction expired before it could be acknowledged.")
        return False


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


class FinishSelectionView(discord.ui.View):
    def __init__(
        self,
        *,
        requester_id: int,
        winner: int,
        state: BattleState,
        map_result: str | None,
        map_ties: list[str],
    ) -> None:
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.winner = winner
        self.state = state
        self.map_result = map_result

        if map_ties:
            self.add_item(TieSelect("マップ", "map", map_ties))

        self.add_item(FinishConfirmButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("この選択は `/finish` 実行者だけが操作できます。", ephemeral=True)
            return False

        return True

    async def confirm(self, interaction: discord.Interaction) -> None:
        missing = []
        if self.map_result is None:
            missing.append("マップ")
        if missing:
            await interaction.response.send_message(f"{'、'.join(missing)}を選択してから確定してください。", ephemeral=True)
            return

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="対戦結果を記録しています...", view=self)

        try:
            result = await write_finish_results(
                winner=self.winner,
                state=self.state,
                map_name=self.map_result,
                end_turn="",
            )
        except FinishError as exc:
            await interaction.followup.send(f"対戦結果の記録に失敗しました: {exc}", ephemeral=True)
            return

        await interaction.edit_original_response(content="対戦結果を記録しました。", view=self)
        await interaction.followup.send(embed=create_finish_embed(result))


class TieSelect(discord.ui.Select):
    def __init__(self, label: str, target: str, options: list[str]) -> None:
        self.target = target
        super().__init__(
            placeholder=f"{label}を選択してください",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=option, value=option) for option in options],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FinishSelectionView):
            await interaction.response.send_message("選択UIの状態を読み取れませんでした。", ephemeral=True)
            return

        if self.target == "map":
            view.map_result = self.values[0]

        await interaction.response.defer()


class FinishConfirmButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="確定", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FinishSelectionView):
            await interaction.response.send_message("選択UIの状態を読み取れませんでした。", ephemeral=True)
            return

        await view.confirm(interaction)


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

        map_poll_message = await interaction.channel.send(poll=create_map_poll())
        if interaction.channel_id is not None:
            THREAD_IDS_BY_PARENT_CHANNEL[interaction.channel_id] = interaction.channel_id
            BATTLE_STATES_BY_THREAD[interaction.channel_id] = BattleState(
                parent_channel_id=interaction.channel_id,
                map_poll_channel_id=interaction.channel_id,
                map_poll_message_id=map_poll_message.id,
            )
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


@bot.tree.command(name="scan", description="指定ロールのメンバーをプレイヤーリストに追加します")
@app_commands.describe(role="スキャンしたいロール")
async def scan(interaction: discord.Interaction, role: discord.Role) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("このコマンドはサーバー内で実行してください。", ephemeral=True)
        return

    if not await safe_defer(interaction, ephemeral=True):
        return

    try:
        scanned_count, added_count, added_names = await scan_role_members_to_player_list(role)
    except RatingRegistrationError as exc:
        await interaction.followup.send(f"プレイヤーリストへの追加に失敗しました: {exc}", ephemeral=True)
        return

    if added_count == 0:
        await interaction.followup.send(
            f"{role.mention} のメンバー {scanned_count} 人を確認しました。新しく追加するユーザーはいませんでした。",
            ephemeral=True,
        )
        return

    preview_names = "、".join(added_names[:10])
    if len(added_names) > 10:
        preview_names += f"、ほか {len(added_names) - 10} 人"

    await interaction.followup.send(
        f"{role.mention} のメンバー {scanned_count} 人を確認し、{added_count} 人を rating {DEFAULT_RATING} で追加しました。\n"
        f"追加: {preview_names}",
        ephemeral=True,
    )


@bot.tree.command(name="finish", description="対戦結果を記録し、レートを更新します")
@app_commands.describe(winner="勝利チーム。0は引き分けです")
@app_commands.choices(
    winner=[
        app_commands.Choice(name="0: 引き分け", value=0),
        app_commands.Choice(name="1: チーム1の勝利", value=1),
        app_commands.Choice(name="2: チーム2の勝利", value=2),
    ]
)
async def finish(interaction: discord.Interaction, winner: int) -> None:
    winner_value = winner
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
        map_result, map_ties = await collect_poll_results(interaction, state)
    except (discord.Forbidden, discord.NotFound, FinishError) as exc:
        await interaction.followup.send(f"投票結果の取得に失敗しました: {exc}", ephemeral=True)
        return

    if map_ties:
        view = FinishSelectionView(
            requester_id=interaction.user.id,
            winner=winner_value,
            state=state,
            map_result=map_result,
            map_ties=map_ties,
        )
        await interaction.followup.send("同票の投票項目があります。記録に使う項目を選択してください。", view=view, ephemeral=True)
        return

    try:
        result = await write_finish_results(
            winner=winner_value,
            state=state,
            map_name=map_result or "",
            end_turn="",
        )
    except FinishError as exc:
        await interaction.followup.send(f"対戦結果の記録に失敗しました: {exc}", ephemeral=True)
        return

    await interaction.followup.send("対戦結果を記録しました。", ephemeral=True)
    await interaction.followup.send(embed=create_finish_embed(result))


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
