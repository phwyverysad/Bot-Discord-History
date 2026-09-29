from __future__ import annotations

import asyncio
import json
import logging
import os
import py_compile
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import discord
import pytz
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import myserver

# โหลด Environment Variables จากไฟล์ .env
load_dotenv()
myserver.server_on()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
THAI_TZ = pytz.timezone("Asia/Bangkok")
DATA_FILE = "guild_channels.json"
STATE_FILE = "moderation_state.json"
SCRIPT_PATH = Path(__file__).resolve()
HISTORY_CATEGORY_NAME = "ประวัติ"
CATEGORY_KEY = "_category_id"

CHANNEL_KEYS = [
    ("member_join", "สมาชิกเข้าเซิร์ฟเวอร์"),
    ("member_leave", "สมาชิกออกเซิร์ฟเวอร์"),
    ("voice_join", "เข้าห้องเสียง"),
    ("voice_leave", "ออกจากห้องเสียง"),
    ("voice_move", "ย้ายห้องเสียง"),
    ("disconnect", "ถูกตัดจากห้องเสียง"),
    ("ban", "ประวัติการแบน"),
    ("kick", "ประวัติการเตะ"),
    ("server_mute", "สถานะการปิดไมค์"),
    ("server_deafen", "สถานะการปิดหูฟัง"),
    ("timeout", "ประวัติไทม์เอาท์"),
    ("message_log", "บันทึกแก้ไขลบข้อความ"),
    ("avatar", "ประวัติรูปโปรไฟล์"),
    ("nickname", "ประวัติการเปลี่ยนชื่อ"),
    ("member_role", "ประวัติการใส่ยศ"),
    ("role_audit", "ประวัติการปรับแต่งยศ"),
]
CHANNEL_NAME_MAP = dict(CHANNEL_KEYS)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

invite_cache: dict[int, dict[str, dict[str, Any]]] = {}
vanity_cache: dict[int, Optional[int]] = {}
audit_entry_uses: dict[int, int] = {}


def load_data() -> dict[str, dict[str, Any]]:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            try:
                return json.load(file)
            except json.JSONDecodeError:
                logger.warning("guild_channels.json เสียหรืออ่านไม่ได้ ใช้ค่าเริ่มต้นแทน")
    return {}


def save_data(data: dict[str, dict[str, Any]]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_state() -> dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            try:
                data = json.load(file)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                logger.warning("moderation_state.json เสียหรืออ่านไม่ได้ ใช้ค่าเริ่มต้นแทน")
    return {}


def save_state() -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(moderation_state, file, ensure_ascii=False, indent=2)


guild_channels = load_data()
moderation_state = load_state()
if not isinstance(moderation_state.get("ban_started_at"), dict):
    moderation_state["ban_started_at"] = {}


def thai_now() -> str:
    return datetime.now(THAI_TZ).strftime("%d/%m/%Y %H:%M:%S")


def to_thai(dt: Optional[datetime]) -> str:
    if dt is None:
        return "ไม่ทราบ"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(THAI_TZ).strftime("%d/%m/%Y %H:%M:%S")


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def duration_minutes_between(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    start_utc = to_utc(start)
    end_utc = to_utc(end)
    if start_utc is None or end_utc is None:
        return None
    total_seconds = max(0.0, (end_utc - start_utc).total_seconds())
    if total_seconds == 0:
        return 0
    return max(1, int((total_seconds + 59) // 60))


def format_minutes(minutes: Optional[int], *, unknown: str = "ไม่ทราบ") -> str:
    if minutes is None:
        return unknown
    if minutes <= 0:
        return "น้อยกว่า 1 นาที"
    return f"{minutes:,} นาที"


def get_ban_state_bucket(guild_id: int) -> dict[str, str]:
    guild_key = str(guild_id)
    bucket = moderation_state.setdefault("ban_started_at", {}).get(guild_key)
    if not isinstance(bucket, dict):
        bucket = {}
        moderation_state["ban_started_at"][guild_key] = bucket
    return bucket


def remember_ban_start(guild_id: int, user_id: int, started_at: datetime) -> None:
    bucket = get_ban_state_bucket(guild_id)
    started_utc = to_utc(started_at) or datetime.now(timezone.utc)
    bucket[str(user_id)] = started_utc.isoformat()
    save_state()


def pop_ban_start(guild_id: int, user_id: int) -> Optional[datetime]:
    bucket = get_ban_state_bucket(guild_id)
    raw_value = bucket.pop(str(user_id), None)
    save_state()
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def trim_text(text: Optional[str], limit: int = 1024) -> str:
    if not text:
        return "ไม่ระบุ"
    text = str(text)
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def safe_asset_url(asset: Optional[discord.Asset], *, size: int = 1024) -> Optional[str]:
    if asset is None:
        return None
    try:
        is_animated = getattr(asset, "is_animated", lambda: False)
        if is_animated():
            return asset.replace(size=size, format="gif").url
        return asset.replace(size=size, static_format="png").url
    except Exception:
        try:
            return asset.url
        except Exception:
            return None


def display_avatar_url(target: Any) -> Optional[str]:
    return safe_asset_url(getattr(target, "display_avatar", None))


def format_actor(user: Any, fallback: str = "ไม่ทราบ") -> str:
    if user is None:
        return fallback
    display_name = getattr(user, "display_name", None) or getattr(user, "name", None) or str(user)
    mention = getattr(user, "mention", None)
    if mention:
        return f"{display_name} ({mention})"
    return str(display_name)


def format_role(role: Any) -> str:
    mention = getattr(role, "mention", None)
    name = getattr(role, "name", None) or str(role)
    if mention:
        return f"{mention} (`{name}`)"
    return f"`{name}`"


def format_role_list(roles: list[discord.Role]) -> str:
    visible_roles = [role for role in roles if role.name != "@everyone"]
    if not visible_roles:
        return "ไม่มี"
    ordered = sorted(visible_roles, key=lambda item: item.position, reverse=True)
    return trim_text("\n".join(format_role(role) for role in ordered), 1024)


def format_permission_diff(before: discord.Permissions, after: discord.Permissions) -> str:
    changes: list[str] = []
    for permission_name, enabled in after:
        old_value = getattr(before, permission_name)
        if old_value == enabled:
            continue
        label = permission_name.replace("_", " ")
        state = "เปิด" if enabled else "ปิด"
        changes.append(f"- `{label}`: {state}")
    if not changes:
        return "ไม่มี"
    if len(changes) > 14:
        extra = len(changes) - 14
        changes = changes[:14] + [f"- และอีก {extra} รายการ"]
    return trim_text("\n".join(changes), 1024)


def format_reason(entry: Optional[discord.AuditLogEntry], default: str = "ไม่ระบุ") -> str:
    if entry is None:
        return default
    return trim_text(entry.reason or default, 1024)


def make_base_embed(target: Any, color: int, title: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    avatar_url = display_avatar_url(target)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    mention = getattr(target, "mention", None)
    target_label = f"{target} ({mention})" if mention else str(target)
    display_name = getattr(target, "display_name", None) or getattr(target, "name", None) or str(target)
    embed.add_field(name="เป้าหมาย", value=target_label, inline=True)
    embed.add_field(name="ชื่อที่แสดง", value=trim_text(display_name, 1024), inline=True)
    embed.add_field(name="เวลา", value=thai_now(), inline=False)
    target_id = getattr(target, "id", "ไม่ทราบ")
    embed.set_footer(text=f"ID: {target_id}")
    return embed


def make_role_embed(role: discord.Role, color: int, title: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="ยศ", value=format_role(role), inline=False)
    embed.add_field(name="เวลา", value=thai_now(), inline=False)
    embed.set_footer(text=f"Role ID: {role.id}")
    return embed


def build_history_overwrites(guild: discord.Guild) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    bot_member = guild.me
    if bot_member is None and bot.user:
        bot_member = guild.get_member(bot.user.id)

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if bot_member is not None:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
        )
    return overwrites


def get_category_id(config: Optional[dict[str, Any]]) -> Optional[int]:
    if not config:
        return None
    category_id = config.get(CATEGORY_KEY)
    if category_id is None:
        return None
    try:
        return int(category_id)
    except (TypeError, ValueError):
        return None


def audit_entry_has_change(entry: discord.AuditLogEntry, *keys: str) -> bool:
    for key in keys:
        before_val = getattr(entry.before, key, None)
        after_val = getattr(entry.after, key, None)
        if before_val != after_val:
            return True
    return False


def max_audit_uses(entry: discord.AuditLogEntry) -> int:
    count = getattr(getattr(entry, "extra", None), "count", 1)
    return count if isinstance(count, int) and count > 0 else 1


def can_use_audit_entry(entry: discord.AuditLogEntry) -> bool:
    entry_id = getattr(entry, "id", None)
    if entry_id is None:
        return True
    return audit_entry_uses.get(entry_id, 0) < max_audit_uses(entry)


def mark_audit_entry_used(entry: discord.AuditLogEntry) -> None:
    entry_id = getattr(entry, "id", None)
    if entry_id is None:
        return
    audit_entry_uses[entry_id] = audit_entry_uses.get(entry_id, 0) + 1


def snapshot_invite(invite: discord.Invite) -> dict[str, Any]:
    inviter = invite.inviter
    channel = invite.channel
    inviter_id = getattr(inviter, "id", None)
    channel_id = getattr(channel, "id", None)
    return {
        "code": invite.code,
        "uses": invite.uses or 0,
        "max_uses": invite.max_uses or 0,
        "temporary": invite.temporary,
        "inviter_id": inviter_id,
        "inviter_name": str(inviter) if inviter else None,
        "inviter_mention": f"<@{inviter_id}>" if inviter_id else None,
        "channel_id": channel_id,
        "channel_name": getattr(channel, "name", None),
        "channel_mention": f"<#{channel_id}>" if channel_id else None,
        "expires_at": to_thai(invite.expires_at) if invite.expires_at else None,
    }


def describe_role_update(before: discord.Role, after: discord.Role) -> str:
    changes: list[str] = []
    if before.name != after.name:
        changes.append(f"- ชื่อ: `{before.name}` -> `{after.name}`")
    if before.colour != after.colour:
        changes.append(f"- สี: `{before.colour}` -> `{after.colour}`")
    if before.hoist != after.hoist:
        changes.append(f"- แสดงแยกในรายชื่อสมาชิก: {'เปิด' if after.hoist else 'ปิด'}")
    if before.mentionable != after.mentionable:
        changes.append(f"- ให้แท็กยศได้: {'เปิด' if after.mentionable else 'ปิด'}")
    if before.position != after.position:
        changes.append(f"- ตำแหน่ง: `{before.position}` -> `{after.position}`")
    if before.icon != after.icon or before.unicode_emoji != after.unicode_emoji:
        changes.append("- ไอคอนหรืออีโมจิของยศถูกเปลี่ยน")

    permission_diff = format_permission_diff(before.permissions, after.permissions)
    if permission_diff != "ไม่มี":
        changes.append(f"สิทธิ์ที่เปลี่ยน:\n{permission_diff}")

    if not changes:
        return "ไม่มีข้อมูลการเปลี่ยนแปลงเพิ่มเติม"
    return trim_text("\n".join(changes), 1024)


async def get_log_channel(guild: discord.Guild, key: str) -> Optional[discord.TextChannel]:
    config = guild_channels.get(str(guild.id))
    if not config:
        return None
    channel_id = config.get(key)
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    return channel if isinstance(channel, discord.TextChannel) else None


async def find_history_category(
    guild: discord.Guild,
    config: Optional[dict[str, Any]] = None,
) -> Optional[discord.CategoryChannel]:
    category_id = get_category_id(config)
    if category_id is not None:
        category = guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            return category

    if config:
        for key in CHANNEL_NAME_MAP:
            channel_id = config.get(key)
            if not channel_id:
                continue
            channel = guild.get_channel(int(channel_id))
            if channel and channel.category:
                return channel.category

    category = discord.utils.get(guild.categories, name=HISTORY_CATEGORY_NAME)
    return category


async def ensure_history_channels(guild: discord.Guild) -> bool:
    guild_id = str(guild.id)
    config = guild_channels.get(guild_id)
    if config is None:
        return False

    changed = False
    overwrites = build_history_overwrites(guild)
    category = await find_history_category(guild, config)

    if category is None:
        category = await guild.create_category(HISTORY_CATEGORY_NAME, overwrites=overwrites)
        config[CATEGORY_KEY] = category.id
        changed = True
    else:
        category_updates: dict[str, Any] = {}
        if category.name != HISTORY_CATEGORY_NAME:
            category_updates["name"] = HISTORY_CATEGORY_NAME
        if category.overwrites != overwrites:
            category_updates["overwrites"] = overwrites
        if category_updates:
            await category.edit(reason="อัปเดตโครงสร้างห้องประวัติ", **category_updates)
        if config.get(CATEGORY_KEY) != category.id:
            config[CATEGORY_KEY] = category.id
            changed = True

    for key, channel_name in CHANNEL_KEYS:
        configured_id = config.get(key)
        channel = guild.get_channel(int(configured_id)) if configured_id else None

        if isinstance(channel, discord.TextChannel):
            channel_updates: dict[str, Any] = {}
            if channel.name != channel_name:
                channel_updates["name"] = channel_name
            if channel.category_id != category.id:
                channel_updates["category"] = category
            if channel_updates:
                await channel.edit(reason="อัปเดตห้องประวัติ", **channel_updates)
        else:
            created_channel = await category.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                reason="สร้างห้องประวัติที่ขาดหาย",
            )
            config[key] = created_channel.id
            changed = True

    if changed:
        save_data(guild_channels)

    return True

async def delete_history_setup(
    guild: discord.Guild,
    *,
    clear_config: bool = True,
) -> tuple[list[str], list[str], bool]:
    guild_id = str(guild.id)
    config = guild_channels.get(guild_id, {})
    category = await find_history_category(guild, config)
    channels_to_delete: dict[int, discord.abc.GuildChannel] = {}

    for key in CHANNEL_NAME_MAP:
        channel_id = config.get(key)
        if not channel_id:
            continue
        channel = guild.get_channel(int(channel_id))
        if channel is not None:
            channels_to_delete[channel.id] = channel

    if category is not None:
        for channel in category.channels:
            if channel.name in CHANNEL_NAME_MAP.values():
                channels_to_delete[channel.id] = channel

    deleted_channels: list[str] = []
    failed_channels: list[str] = []

    for channel in sorted(channels_to_delete.values(), key=lambda item: item.position, reverse=True):
        try:
            channel_label = f"#{channel.name}"
            await channel.delete(reason="ลบระบบประวัติผ่านคำสั่ง /delete")
            deleted_channels.append(channel_label)
        except Exception as exc:
            failed_channels.append(f"{channel.name}: {exc}")

    category_deleted = False
    if category is not None:
        fresh_category = guild.get_channel(category.id)
        if isinstance(fresh_category, discord.CategoryChannel) and not fresh_category.channels:
            try:
                await fresh_category.delete(reason="ลบหมวดประวัติที่ว่างเปล่า")
                category_deleted = True
            except Exception as exc:
                failed_channels.append(f"{fresh_category.name}: {exc}")

    if clear_config and guild_id in guild_channels:
        guild_channels.pop(guild_id, None)
        save_data(guild_channels)

    return deleted_channels, failed_channels, category_deleted


async def recreate_history_setup(guild: discord.Guild) -> None:
    guild_channels.setdefault(str(guild.id), {})
    await delete_history_setup(guild, clear_config=False)
    guild_channels[str(guild.id)] = {}
    await ensure_history_channels(guild)
    save_data(guild_channels)


async def fetch_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    *,
    target_id: Optional[int] = None,
    delay: float = 1.2,
    limit: int = 8,
    window_seconds: float = 20.0,
    predicate: Optional[Callable[[discord.AuditLogEntry], bool]] = None,
    consume: bool = False,
) -> Optional[discord.AuditLogEntry]:
    await asyncio.sleep(delay)
    now = datetime.now(timezone.utc)

    try:
        async for entry in guild.audit_logs(action=action, limit=limit):
            age = (now - entry.created_at).total_seconds()
            if age > window_seconds:
                continue
            if target_id is not None and getattr(entry.target, "id", None) != target_id:
                continue
            if predicate and not predicate(entry):
                continue
            if consume and not can_use_audit_entry(entry):
                continue
            if consume:
                mark_audit_entry_used(entry)
            return entry
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning("อ่าน audit log ไม่สำเร็จ (%s / %s): %s", guild.id, action, exc)
    return None


async def fetch_invite_snapshot(
    guild: discord.Guild,
) -> tuple[Optional[dict[str, dict[str, Any]]], Optional[int], Optional[str]]:
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        return None, None, "บอทไม่มีสิทธิ์ Manage Guild เพื่ออ่านคำเชิญ"
    except discord.HTTPException as exc:
        return None, None, f"อ่านคำเชิญไม่สำเร็จ: {exc}"

    snapshot = {invite.code: snapshot_invite(invite) for invite in invites}
    vanity_uses: Optional[int] = None

    if guild.vanity_url_code:
        try:
            vanity_invite = await guild.vanity_invite()
            vanity_uses = vanity_invite.uses or 0
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            vanity_uses = None

    return snapshot, vanity_uses, None


async def refresh_invite_cache(guild: discord.Guild) -> None:
    snapshot, vanity_uses, _ = await fetch_invite_snapshot(guild)
    if snapshot is None:
        return
    invite_cache[guild.id] = snapshot
    vanity_cache[guild.id] = vanity_uses


async def detect_member_join_source(member: discord.Member) -> dict[str, str]:
    guild = member.guild

    if member.bot:
        entry = await fetch_audit_entry(
            guild,
            discord.AuditLogAction.bot_add,
            target_id=member.id,
        )
        return {
            "inviter": format_actor(entry.user if entry else None, "ไม่ทราบ"),
            "method": "ถูกเพิ่มเข้ามาในฐานะบอท",
            "reason": format_reason(entry),
        }

    previous_snapshot = invite_cache.get(guild.id)
    previous_vanity_uses = vanity_cache.get(guild.id)

    if previous_snapshot is None:
        await refresh_invite_cache(guild)
        return {
            "inviter": "ยังระบุไม่ได้",
            "method": "ยังไม่มีฐานข้อมูลคำเชิญก่อนสมาชิกเข้ามา",
            "reason": "ไม่ระบุ",
        }

    await asyncio.sleep(1.0)
    current_snapshot, current_vanity_uses, error_message = await fetch_invite_snapshot(guild)
    if current_snapshot is None:
        return {
            "inviter": "ตรวจสอบไม่ได้",
            "method": error_message or "อ่านคำเชิญไม่สำเร็จ",
            "reason": "ไม่ระบุ",
        }

    inviter_text = "ไม่ทราบ"
    method_lines: list[str] = []
    selected_invite: Optional[dict[str, Any]] = None

    increased_invites: list[tuple[int, dict[str, Any]]] = []
    for code, invite_info in current_snapshot.items():
        previous_uses = previous_snapshot.get(code, {}).get("uses", 0)
        current_uses = invite_info.get("uses", 0)
        if current_uses > previous_uses:
            increased_invites.append((current_uses - previous_uses, invite_info))

    increased_invites.sort(key=lambda item: item[0], reverse=True)

    if increased_invites:
        if len(increased_invites) == 1 or increased_invites[0][0] > increased_invites[1][0]:
            selected_invite = increased_invites[0][1]
            method_lines.append("เข้าผ่านลิงก์เชิญปกติ")
    else:
        expired_candidates: list[dict[str, Any]] = []
        for code, invite_info in previous_snapshot.items():
            if code in current_snapshot:
                continue
            max_uses = invite_info.get("max_uses", 0)
            likely_last_use = bool(max_uses and invite_info.get("uses", 0) + 1 >= max_uses)
            if likely_last_use or invite_info.get("temporary"):
                expired_candidates.append(invite_info)

        if len(expired_candidates) == 1:
            selected_invite = expired_candidates[0]
            method_lines.append("เข้าผ่านลิงก์ที่หมดอายุหรือถูกลบทันทีหลังใช้งาน")

    if selected_invite:
        inviter_text = selected_invite.get("inviter_mention") or selected_invite.get("inviter_name") or "ไม่ทราบ"
        method_lines.append(f"โค้ดคำเชิญ: `{selected_invite.get('code', 'ไม่ทราบ')}`")
        channel_text = selected_invite.get("channel_mention") or selected_invite.get("channel_name") or "ไม่ทราบ"
        method_lines.append(f"ห้องที่สร้างลิงก์: {channel_text}")
        method_lines.append(f"ประเภทลิงก์: {'ชั่วคราว' if selected_invite.get('temporary') else 'ปกติ'}")
        expires_at = selected_invite.get("expires_at")
        if expires_at:
            method_lines.append(f"วันหมดอายุ: {expires_at}")
    elif (
        current_vanity_uses is not None
        and previous_vanity_uses is not None
        and current_vanity_uses > previous_vanity_uses
    ):
        inviter_text = "Vanity URL ของเซิร์ฟเวอร์"
        method_lines.append("เข้าผ่านลิงก์ Vanity URL ของเซิร์ฟเวอร์")
        method_lines.append(f"โค้ดคำเชิญ: `{guild.vanity_url_code}`")
    else:
        method_lines.append("ไม่พบลิงก์ที่ใช้แบบชัดเจน อาจเป็นข้อมูลมาไม่ทันหรือมีการเปลี่ยนคำเชิญพร้อมกัน")

    invite_cache[guild.id] = current_snapshot
    vanity_cache[guild.id] = current_vanity_uses

    return {
        "inviter": trim_text(inviter_text, 1024),
        "method": trim_text("\n".join(method_lines), 1024),
        "reason": "ไม่ระบุ",
    }


async def send_avatar_change_log(
    guild: discord.Guild,
    target: Any,
    *,
    title: str,
    scope_text: str,
    before_asset: Optional[discord.Asset],
    after_asset: Optional[discord.Asset],
) -> None:
    channel = await get_log_channel(guild, "avatar")
    if channel is None:
        return

    old_url = safe_asset_url(before_asset)
    new_url = safe_asset_url(after_asset)
    if not old_url and not new_url:
        return

    summary = make_base_embed(target, 0x9B59B6, title)
    summary.add_field(name="ประเภท", value=scope_text, inline=False)
    summary.add_field(
        name="รูปก่อนเปลี่ยน",
        value=f"[คลิกเพื่อดู]({old_url})" if old_url else "ไม่มีรูปเดิม",
        inline=False,
    )
    summary.add_field(
        name="รูปหลังเปลี่ยน",
        value=f"[คลิกเพื่อดู]({new_url})" if new_url else "ไม่มีรูปใหม่",
        inline=False,
    )

    embeds = [summary]
    if old_url:
        old_embed = discord.Embed(
            title="รูปก่อนเปลี่ยน",
            color=0x95A5A6,
            timestamp=datetime.now(timezone.utc),
        )
        old_embed.set_image(url=old_url)
        embeds.append(old_embed)
    if new_url:
        new_embed = discord.Embed(
            title="รูปหลังเปลี่ยน",
            color=0x9B59B6,
            timestamp=datetime.now(timezone.utc),
        )
        new_embed.set_image(url=new_url)
        embeds.append(new_embed)

    await channel.send(embeds=embeds)


async def restart_bot_process() -> None:
    await asyncio.sleep(1.5)
    logger.info("RESTART: %s", SCRIPT_PATH)
    # ใช้ Python จาก venv ถ้ามี มิเช่นนั้นใช้ sys.executable
    venv_python = SCRIPT_PATH.parent / ".venv" / "Scripts" / "python.exe"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable
    os.execv(python_exe, [python_exe, str(SCRIPT_PATH), *sys.argv[1:]])

@bot.tree.command(name="start", description="เริ่มหรือติดตั้งระบบห้องประวัติใหม่ทั้งหมด")
@app_commands.checks.has_permissions(administrator=True)
async def start(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await recreate_history_setup(interaction.guild)

    embed = discord.Embed(
        title="สำเร็จ",
        color=0x2ECC71,
        description="ตั้งค่าระบบห้องประวัติใหม่เรียบร้อยแล้ว",
    )
    embed.add_field(name="จำนวนห้อง", value=str(len(CHANNEL_KEYS)), inline=True)
    embed.add_field(name="หมวด", value=HISTORY_CATEGORY_NAME, inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="update", description="รีโหลด bothistory.py ล่าสุดแล้วรีสตาร์ตบอท")
@app_commands.checks.has_permissions(administrator=True)
async def update_bot(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)
        return

    # Defer ทันทีเพื่อป้องกัน interaction timeout
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as exc:
        logger.warning("Defer failed: %s", exc)
        return

    # ตรวจสอบ syntax โค้ด
    try:
        py_compile.compile(str(SCRIPT_PATH), doraise=True)
    except Exception as exc:
        try:
            embed = discord.Embed(
                title="อัปเดตไม่สำเร็จ",
                color=0xE74C3C,
                description="โค้ดล่าสุดยังมีข้อผิดพลาด จึงยังไม่รีสตาร์ตบอทให้",
            )
            embed.add_field(name="รายละเอียด", value=trim_text(str(exc), 1024), inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as send_exc:
            logger.error("ส่งข้อความ error กลับไม่สำเร็จ: %s", send_exc)
        return

    modified_at = datetime.fromtimestamp(SCRIPT_PATH.stat().st_mtime, tz=timezone.utc)
    embed = discord.Embed(
        title="กำลังอัปเดตบอท",
        color=0x3498DB,
        description=f"กำลังรีโหลด `{SCRIPT_PATH.name}` เวอร์ชันล่าสุด",
    )
    embed.add_field(name="แก้ไขล่าสุด", value=to_thai(modified_at), inline=False)
    embed.add_field(
        name="หมายเหตุ",
        value="หลังรีสตาร์ตแล้ว บอทจะ sync slash commands และสร้างห้องที่ขาดให้เองอัตโนมัติ",
        inline=False,
    )
    try:
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        logger.warning("ส่งข้อความอัปเดตไม่สำเร็จ: %s", exc)
    asyncio.create_task(restart_bot_process())


@bot.tree.command(name="delete", description="ลบห้องประวัติทั้งหมดตาม CHANNEL_KEYS")
@app_commands.checks.has_permissions(administrator=True)
async def delete_history(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    deleted_channels, failed_channels, category_deleted = await delete_history_setup(interaction.guild)

    embed = discord.Embed(
        title="ลบระบบประวัติแล้ว",
        color=0xE67E22 if failed_channels else 0x2ECC71,
        description="ลบห้องที่อยู่ในรายการ CHANNEL_KEYS สำหรับเซิร์ฟเวอร์นี้เรียบร้อยแล้ว",
    )
    embed.add_field(name="ลบได้", value=str(len(deleted_channels)), inline=True)
    embed.add_field(name="หมวดถูกลบ", value="ใช่" if category_deleted else "ไม่", inline=True)
    if deleted_channels:
        embed.add_field(name="รายการที่ลบ", value=trim_text("\n".join(deleted_channels), 1024), inline=False)
    if failed_channels:
        embed.add_field(name="รายการที่ลบไม่สำเร็จ", value=trim_text("\n".join(failed_channels), 1024), inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    guild = member.guild

    if before.channel is None and after.channel is not None:
        channel = await get_log_channel(guild, "voice_join")
        if channel:
            embed = make_base_embed(member, 0x2ECC71, "เข้าห้องเสียง")
            embed.add_field(name="ห้อง", value=after.channel.mention, inline=False)
            await channel.send(embed=embed)

    elif before.channel is not None and after.channel is None:
        disconnect_entry = await fetch_audit_entry(
            guild,
            discord.AuditLogAction.member_disconnect,
            predicate=lambda entry: True,
            window_seconds=4.0,
            consume=True,
        )
        if disconnect_entry:
            channel = await get_log_channel(guild, "disconnect")
            if channel:
                embed = make_base_embed(member, 0x8E44AD, "ถูกตัดออกจากห้องเสียง")
                embed.add_field(name="ห้องเดิม", value=before.channel.mention, inline=True)
                embed.add_field(name="ผู้กระทำ", value=format_actor(disconnect_entry.user), inline=True)
                embed.add_field(name="สาเหตุ", value=format_reason(disconnect_entry), inline=False)
                extra_count = getattr(getattr(disconnect_entry, "extra", None), "count", 1)
                if extra_count > 1:
                    embed.add_field(
                        name="หมายเหตุ",
                        value=f"Discord ระบุว่าเหตุการณ์นี้ตัดพร้อมกัน {extra_count} คน จึงอาจจับเป้าหมายแบบรายคนได้ไม่ครบ 100%",
                        inline=False,
                    )
                await channel.send(embed=embed)
        else:
            channel = await get_log_channel(guild, "voice_leave")
            if channel:
                embed = make_base_embed(member, 0xE74C3C, "ออกจากห้องเสียง")
                embed.add_field(name="ห้องเดิม", value=before.channel.mention, inline=False)
                await channel.send(embed=embed)

    elif before.channel and after.channel and before.channel != after.channel:
        channel = await get_log_channel(guild, "voice_move")
        if channel:
            move_entry = await fetch_audit_entry(
                guild,
                discord.AuditLogAction.member_move,
                predicate=lambda entry: getattr(getattr(entry, "extra", None), "channel", None) is not None
                and getattr(entry.extra.channel, "id", None) == after.channel.id,
                window_seconds=8.0,
                consume=True,
            )
            mover = format_actor(move_entry.user if move_entry else member, "ไม่ทราบ")
            embed = make_base_embed(member, 0xF39C12, "ย้ายห้องเสียง")
            embed.add_field(name="สมาชิกที่ถูกย้าย", value=member.mention, inline=False)
            embed.add_field(name="จากห้อง", value=before.channel.mention, inline=True)
            embed.add_field(name="ไปห้อง", value=after.channel.mention, inline=True)
            embed.add_field(
                name="ผู้ย้าย",
                value=mover if move_entry else f"{format_actor(member)} (ย้ายเอง)",
                inline=False,
            )
            embed.add_field(name="สาเหตุ", value=format_reason(move_entry), inline=False)
            extra_count = getattr(getattr(move_entry, "extra", None), "count", 1) if move_entry else 1
            if extra_count > 1:
                embed.add_field(
                    name="หมายเหตุ",
                    value=f"Discord ระบุว่าเหตุการณ์นี้ย้ายพร้อมกัน {extra_count} คน จึงอาจจับเป้าหมายแบบรายคนได้ไม่ครบ 100%",
                    inline=False,
                )
            await channel.send(embed=embed)

    if before.mute != after.mute:
        channel = await get_log_channel(guild, "server_mute")
        if channel:
            entry = await fetch_audit_entry(
                guild,
                discord.AuditLogAction.member_update,
                target_id=member.id,
                predicate=lambda audit_entry: audit_entry_has_change(audit_entry, "mute"),
            )
            embed = make_base_embed(
                member,
                0xE74C3C if after.mute else 0x2ECC71,
                "ปิดไมค์โดยเซิร์ฟเวอร์" if after.mute else "เปิดไมค์โดยเซิร์ฟเวอร์",
            )
            embed.add_field(name="ผู้สั่ง", value=format_actor(entry.user if entry else None), inline=True)
            embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=True)
            await channel.send(embed=embed)

    if before.deaf != after.deaf:
        channel = await get_log_channel(guild, "server_deafen")
        if channel:
            entry = await fetch_audit_entry(
                guild,
                discord.AuditLogAction.member_update,
                target_id=member.id,
                predicate=lambda audit_entry: audit_entry_has_change(audit_entry, "deaf"),
            )
            embed = make_base_embed(
                member,
                0xE74C3C if after.deaf else 0x2ECC71,
                "ปิดหูฟังโดยเซิร์ฟเวอร์" if after.deaf else "เปิดหูฟังโดยเซิร์ฟเวอร์",
            )
            embed.add_field(name="ผู้สั่ง", value=format_actor(entry.user if entry else None), inline=True)
            embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=True)
            await channel.send(embed=embed)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    channel = await get_log_channel(member.guild, "member_join")
    if channel is None:
        return

    join_source = await detect_member_join_source(member)
    embed = make_base_embed(member, 0x1ABC9C, "สมาชิกเข้าเซิร์ฟเวอร์")
    embed.add_field(name="สร้างบัญชีเมื่อ", value=to_thai(member.created_at), inline=False)
    embed.add_field(name="ผู้พาเข้ามา", value=join_source["inviter"], inline=False)
    embed.add_field(name="เข้ามายังไง", value=join_source["method"], inline=False)
    embed.add_field(name="สาเหตุ", value=join_source["reason"], inline=False)
    await channel.send(embed=embed)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    guild = member.guild

    kick_entry = await fetch_audit_entry(
        guild,
        discord.AuditLogAction.kick,
        target_id=member.id,
    )
    if kick_entry:
        channel = await get_log_channel(guild, "kick")
        if channel:
            embed = make_base_embed(member, 0xE67E22, "ถูกเตะออกจากเซิร์ฟเวอร์")
            embed.add_field(name="ผู้สั่ง", value=format_actor(kick_entry.user), inline=True)
            embed.add_field(name="สาเหตุ", value=format_reason(kick_entry), inline=True)
            await channel.send(embed=embed)
        return

    ban_entry = await fetch_audit_entry(
        guild,
        discord.AuditLogAction.ban,
        target_id=member.id,
        delay=0.0,
    )
    if ban_entry:
        return

    channel = await get_log_channel(guild, "member_leave")
    if channel:
        embed = make_base_embed(member, 0xE74C3C, "สมาชิกออกจากเซิร์ฟเวอร์")
        await channel.send(embed=embed)

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    channel = await get_log_channel(guild, "ban")
    if channel is None:
        return

    entry = await fetch_audit_entry(guild, discord.AuditLogAction.ban, target_id=user.id)
    ban_started_at = entry.created_at if entry else datetime.now(timezone.utc)
    remember_ban_start(guild.id, user.id, ban_started_at)
    embed = make_base_embed(user, 0xC0392B, "บันทึกการแบน")
    embed.add_field(name="ผู้สั่ง", value=format_actor(entry.user if entry else None), inline=True)
    embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=True)
    embed.add_field(name="เริ่มแบนเมื่อ", value=to_thai(ban_started_at), inline=False)
    embed.add_field(name="ระยะเวลาแบน", value="ไม่มีกำหนด (จนกว่าจะปลดแบน)", inline=False)
    await channel.send(embed=embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User) -> None:
    channel = await get_log_channel(guild, "ban")
    if channel is None:
        return

    entry = await fetch_audit_entry(guild, discord.AuditLogAction.unban, target_id=user.id)
    ban_started_at = pop_ban_start(guild.id, user.id)
    unban_at = entry.created_at if entry else datetime.now(timezone.utc)
    ban_minutes = duration_minutes_between(ban_started_at, unban_at)
    embed = make_base_embed(user, 0x27AE60, "ยกเลิกการแบน")
    embed.add_field(name="ผู้สั่ง", value=format_actor(entry.user if entry else None), inline=True)
    embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=True)
    if ban_started_at is not None:
        embed.add_field(name="เริ่มแบนเมื่อ", value=to_thai(ban_started_at), inline=True)
    embed.add_field(
        name="ถูกแบนมาแล้ว",
        value=format_minutes(ban_minutes, unknown="ไม่ทราบ (ไม่มีข้อมูลเก่าที่บอทบันทึกไว้)"),
        inline=True,
    )
    await channel.send(embed=embed)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.timed_out_until != after.timed_out_until:
        channel = await get_log_channel(after.guild, "timeout")
        if channel:
            now_utc = datetime.now(timezone.utc)
            entry = await fetch_audit_entry(
                after.guild,
                discord.AuditLogAction.member_update,
                target_id=after.id,
                predicate=lambda audit_entry: audit_entry_has_change(audit_entry, "timed_out_until"),
            )
            if after.timed_out_until and after.timed_out_until > now_utc:
                remaining_minutes = duration_minutes_between(now_utc, after.timed_out_until)
                embed = make_base_embed(after, 0xE74C3C, "ถูกจำกัดการส่งข้อความ (Timeout)")
                embed.add_field(name="สิ้นสุดเมื่อ", value=to_thai(after.timed_out_until), inline=True)
                embed.add_field(name="หมดเวลาในอีก", value=format_minutes(remaining_minutes), inline=True)
            else:
                remaining_before = duration_minutes_between(now_utc, before.timed_out_until)
                embed = make_base_embed(after, 0x2ECC71, "ยกเลิกการจำกัดการส่งข้อความ")
                if before.timed_out_until is not None:
                    embed.add_field(name="เดิมหมดเวลาเมื่อ", value=to_thai(before.timed_out_until), inline=True)
                embed.add_field(
                    name="เหลือเวลาเดิมอีก",
                    value=format_minutes(remaining_before, unknown="ไม่ทราบ"),
                    inline=True,
                )
            embed.add_field(name="ผู้สั่ง", value=format_actor(entry.user if entry else None), inline=True)
            embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=True)
            await channel.send(embed=embed)

    if before.nick != after.nick:
        channel = await get_log_channel(after.guild, "nickname")
        if channel:
            entry = await fetch_audit_entry(
                after.guild,
                discord.AuditLogAction.member_update,
                target_id=after.id,
                predicate=lambda audit_entry: audit_entry_has_change(audit_entry, "nick"),
            )
            actor_text = format_actor(entry.user if entry else after)
            if not entry:
                actor_text = f"{actor_text} (เปลี่ยนเอง)"
            embed = make_base_embed(after, 0x3498DB, "เปลี่ยนชื่อเล่น")
            embed.add_field(name="ชื่อเดิม", value=before.nick or "ไม่มี", inline=True)
            embed.add_field(name="ชื่อใหม่", value=after.nick or "ไม่มี", inline=True)
            embed.add_field(name="ผู้เปลี่ยน", value=actor_text, inline=False)
            embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=False)
            await channel.send(embed=embed)

    if before.guild_avatar != after.guild_avatar:
        await send_avatar_change_log(
            after.guild,
            after,
            title="เปลี่ยนรูปโปรไฟล์ประจำเซิร์ฟเวอร์",
            scope_text="รูปโปรไฟล์เฉพาะเซิร์ฟเวอร์",
            before_asset=before.guild_avatar,
            after_asset=after.guild_avatar,
        )

    before_roles = {role.id: role for role in before.roles if role.name != "@everyone"}
    after_roles = {role.id: role for role in after.roles if role.name != "@everyone"}
    added_roles = [after_roles[role_id] for role_id in after_roles.keys() - before_roles.keys()]
    removed_roles = [before_roles[role_id] for role_id in before_roles.keys() - after_roles.keys()]

    if added_roles or removed_roles:
        channel = await get_log_channel(after.guild, "member_role")
        if channel:
            entry = await fetch_audit_entry(
                after.guild,
                discord.AuditLogAction.member_role_update,
                target_id=after.id,
            )
            if added_roles and removed_roles:
                title = "ปรับยศสมาชิก"
                color = 0xF1C40F
            elif added_roles:
                title = "เพิ่มยศให้สมาชิก"
                color = 0x2ECC71
            else:
                title = "เอายศออกจากสมาชิก"
                color = 0xE74C3C

            embed = make_base_embed(after, color, title)
            if added_roles:
                embed.add_field(name="ยศที่เพิ่ม", value=format_role_list(added_roles), inline=False)
            if removed_roles:
                embed.add_field(name="ยศที่เอาออก", value=format_role_list(removed_roles), inline=False)
            embed.add_field(name="ผู้กระทำ", value=format_actor(entry.user if entry else None), inline=False)
            embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=False)
            await channel.send(embed=embed)

@bot.event
async def on_user_update(before: discord.User, after: discord.User) -> None:
    before_url = safe_asset_url(before.display_avatar)
    after_url = safe_asset_url(after.display_avatar)
    if before_url == after_url:
        return

    for guild in bot.guilds:
        member = guild.get_member(after.id)
        if member is None:
            continue
        await send_avatar_change_log(
            guild,
            member,
            title="เปลี่ยนรูปโปรไฟล์",
            scope_text="รูปโปรไฟล์หลักของบัญชี",
            before_asset=before.display_avatar,
            after_asset=after.display_avatar,
        )


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if not message.guild or message.author.bot:
        return

    channel = await get_log_channel(message.guild, "message_log")
    if channel is None:
        return

    entry = await fetch_audit_entry(
        message.guild,
        discord.AuditLogAction.message_delete,
        target_id=message.author.id,
        predicate=lambda audit_entry: getattr(getattr(audit_entry, "extra", None), "channel", None) is not None
        and getattr(audit_entry.extra.channel, "id", None) == message.channel.id,
        window_seconds=6.0,
        consume=True,
    )

    executor = format_actor(entry.user if entry else message.author)
    if not entry:
        executor = f"{executor} (ลบเอง)"

    content = message.content if message.content else "ไม่มีข้อความ"
    attachment_urls = "\n".join(attachment.url for attachment in message.attachments[:5])
    if attachment_urls:
        content = f"{content}\n\nไฟล์แนบ:\n{attachment_urls}"

    embed = make_base_embed(message.author, 0xE74C3C, "ข้อความถูกลบ")
    embed.add_field(name="ห้องที่เกิด", value=message.channel.mention, inline=True)
    embed.add_field(name="ผู้ลบ", value=executor, inline=True)
    embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=False)
    embed.add_field(name="เนื้อหาเดิม", value=trim_text(content, 1000), inline=False)
    await channel.send(embed=embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if not before.guild or before.author.bot or before.content == after.content:
        return

    channel = await get_log_channel(before.guild, "message_log")
    if channel is None:
        return

    embed = make_base_embed(before.author, 0xF39C12, "แก้ไขข้อความ")
    embed.add_field(name="ห้องที่เกิด", value=before.channel.mention, inline=True)
    embed.add_field(name="ลิงก์ข้อความ", value=f"[ไปที่ข้อความ]({after.jump_url})", inline=True)
    embed.add_field(name="ข้อความเดิม", value=trim_text(before.content or "ไม่มีข้อความ", 1000), inline=False)
    embed.add_field(name="ข้อความใหม่", value=trim_text(after.content or "ไม่มีข้อความ", 1000), inline=False)
    await channel.send(embed=embed)


@bot.event
async def on_guild_role_create(role: discord.Role) -> None:
    channel = await get_log_channel(role.guild, "role_audit")
    if channel is None:
        return

    entry = await fetch_audit_entry(role.guild, discord.AuditLogAction.role_create, target_id=role.id)
    embed = make_role_embed(role, 0x2ECC71, "สร้างยศใหม่")
    embed.add_field(name="ผู้สร้าง", value=format_actor(entry.user if entry else None), inline=True)
    embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=True)
    await channel.send(embed=embed)


@bot.event
async def on_guild_role_delete(role: discord.Role) -> None:
    channel = await get_log_channel(role.guild, "role_audit")
    if channel is None:
        return

    entry = await fetch_audit_entry(role.guild, discord.AuditLogAction.role_delete, target_id=role.id)
    embed = make_role_embed(role, 0xE74C3C, "ลบยศ")
    embed.add_field(name="ผู้ลบ", value=format_actor(entry.user if entry else None), inline=True)
    embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=True)
    await channel.send(embed=embed)


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role) -> None:
    if (
        before.name == after.name
        and before.colour == after.colour
        and before.hoist == after.hoist
        and before.mentionable == after.mentionable
        and before.position == after.position
        and before.permissions == after.permissions
        and before.icon == after.icon
        and before.unicode_emoji == after.unicode_emoji
    ):
        return

    channel = await get_log_channel(after.guild, "role_audit")
    if channel is None:
        return

    entry = await fetch_audit_entry(after.guild, discord.AuditLogAction.role_update, target_id=after.id)
    embed = make_role_embed(after, 0x3498DB, "แก้ไขยศ")
    embed.add_field(name="ผู้แก้ไข", value=format_actor(entry.user if entry else None), inline=True)
    embed.add_field(name="สาเหตุ", value=format_reason(entry), inline=True)
    embed.add_field(name="รายละเอียดที่เปลี่ยน", value=describe_role_update(before, after), inline=False)
    await channel.send(embed=embed)

@bot.event
async def on_invite_create(invite: discord.Invite) -> None:
    await refresh_invite_cache(invite.guild)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    if str(guild.id) in guild_channels:
        await ensure_history_channels(guild)
    await refresh_invite_cache(guild)

@bot.event
async def on_ready() -> None:
    logger.info("START: %s", bot.user)

    try:
        await bot.tree.sync()
        logger.info("SYNC DONE")
    except Exception as exc:
        logger.error("SYNC FAILED: %s", exc)

    for guild in bot.guilds:
        if str(guild.id) in guild_channels:
            try:
                await ensure_history_channels(guild)
            except Exception as exc:
                logger.error("ensure_history_channels failed for %s: %s", guild.id, exc)
        await refresh_invite_cache(guild)

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="ประวัติเซิร์ฟเวอร์")
    )


@bot.tree.command(name="exit", description="สั่งให้บอทออกจากเซิร์ฟเวอร์นี้ (ต้องมีสิทธิ์ผู้ดูแล)")
@app_commands.default_permissions(administrator=True)
async def exit_server(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์เท่านั้น", ephemeral=True)
        return
    await interaction.response.send_message("กำลังออกจากเซิร์ฟเวอร์... ลาก่อน!", ephemeral=True)
    try:
        await guild.leave()
    except Exception as exc:
        logger.error("Failed to leave guild %s: %s", guild.id, exc)

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "ต้องมีสิทธิ์ผู้ดูแลระบบจึงจะใช้คำสั่งนี้ได้"
    else:
        logger.exception("APP COMMAND ERROR", exc_info=error)
        message = f"เกิดข้อผิดพลาดระหว่างทำงาน: {trim_text(str(error), 1000)}"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        logger.error("ส่งข้อความ error กลับไม่สำเร็จ")


if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN.strip() == "":
        logger.critical("DISCORD_TOKEN is missing! Please configure .env file.")
        print("[!] ERROR: DISCORD_TOKEN is missing. Please create a .env file with DISCORD_TOKEN=your_token")
        sys.exit(1)
    try:
        bot.run(BOT_TOKEN, log_handler=None)
    except Exception as exc:
        logger.critical("FATAL ERROR: %s", exc)
        raise
