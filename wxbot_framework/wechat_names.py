"""本机微信 contact.db 的唯一只读引擎：成员名字、群名与群昵称。

它收敛了原先 `contact_db.py` 与 `wechat_names.py` 两套实现，负责：

- 定位当前账号的 contact.db，复用 `db_key` 的公开能力取密钥（Weixin.dll 辅助 key ⊕ 内存候选、
  页 1 HMAC 校验）；AES-CBC 解密用 pycryptodome。
- 在 `<root>/.runtime/contact_cache/contact.db` 保留一份**明文快照**供复用：源库与 `-wal` 的
  (mtime, size) 都不变就不重新解密；构建失败有 60 秒退避，且不会把坏文件留在那儿。
- 解密前把 `-wal` 里**属于当前 WAL 代**的已提交帧合并进主库（帧 salt 必须等于 WAL 头 salt，
  这是 SQLite 自己的判据；checkpoint 重置 WAL 后残留的上一代帧必须跳过，否则会把库回退到旧状态）。
- `chat_room.ext_buffer` 解析兼容旧版布局。
- 查询接口：某群群名、全部群名、某群「成员 ID → 群昵称」。
- 原有的「取成员名字（群昵称 > 微信昵称 > 备注 > alias）写进成员库微信名缓存」与
  「启动即刷 + 按间隔刷新」保持不变。

机器人回复里艾特成员显示的名字来自成员库的 `wechat_names` 表
（`MemberStore.remember_wechat_name`，成员库内部的名字兜底层）。此前只有「说过话的成员」
会被原生桥接顺带写进去，群里从不发言的成员就只能显示微信 ID。本模块把缺的部分补上：
连上微信后读一次微信库，之后按固定间隔刷新，把结果写进同一个缓存，回复路径不需要任何改动。

只读与降级：

- 微信数据目录里的任何文件都只以 `rb` 打开，加密封口先复制到系统临时目录；解密与查询只动副本，
  加密中间副本每轮结束立即删除，绝不写进仓库、也不修改/移动微信数据目录里的文件。
- 唯一的持久明文是上面那份快照，位置固定、仅本机、可随时手工删除；README 里如实说明。
- 找不到库、取不到/校验不过密钥、解密失败、查询为空、密钥特征随微信升级失效等情况一律只记日志
  并保留上一轮的结果，下一轮自然重试；解析不到名字的成员直接跳过，不写占位符。
- 名字刷新的全部工作都在后台线程里做，不占用消息收发路径。

名字优先级：群昵称（ext_buffer 的 field 2）> 微信昵称（`contact.nick_name`）
> 备注（`remark`）> `alias`。四级都取不到就跳过该成员。群名取值顺序：备注 > 微信昵称 > alias > 群 ID。
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

from .db_key import (HMAC_SIZE, IV_SIZE, PAGE_SIZE, RESERVE_SIZE, SALT_SIZE,
                     find_database_key, locate_data_dir, page_hmac, resolve_key_material)

logger = logging.getLogger("WechatNames")

DEFAULT_REFRESH_SECONDS = 300
# 账号数据目录下的相对路径；账号目录名带机器/登录相关的后缀，定位交给 db_key.locate_data_dir。
CONTACT_RELATIVE = ("contact", "contact.db")
PLAINTEXT_NAME = "contact.db"
ENCRYPTED_NAME = "contact.enc"
TEMP_PREFIX = "wxbot-wechat-names-"
# 明文快照的复用目录：项目运行目录下的 `.runtime/contact_cache/`。
CACHE_SUBDIR = ("contact_cache",)

SQLITE_HEADER = b"SQLite format 3\x00"
# 明文副本不要留在 WAL 模式：只读打开时不必再依赖 -wal/-shm 旁路文件。
WAL_MODE_HEADER = b"\x02\x02"
ROLLBACK_MODE_HEADER = b"\x01\x01"

WAL_SUFFIX = "-wal"
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER = 24
WAL_VERSION = 3007000
WAL_MAGIC = (0x377F0682, 0x377F0683)

# 快照构建失败后的最小重试间隔（秒）：失败时不记 stamp，若不节流，下一轮刷新会立刻重付一次
# 复制+解密；而「微信刚启动、密钥还没就绪」是常态，所以给一个短退避而不是永久放弃。
BUILD_RETRY_SECONDS = 60.0

# 单条字符串字段上限：超过基本是二进制字段，跳过更安全。
MAX_TEXT_BYTES = 256
# 成员库的名字上限（`members.name_key` 同口径）：超过就当取不到，换下一个来源而不是丢掉这个人。
MAX_NAME_CHARS = 64
# 成员 ID 形状：wxid_/gh_ 前缀或含 @ 的直接认；其余按 6–32 位 ASCII 别名（微信 ID 规则）认。
MEMBER_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{5,31}$")
ID_PREFIXES = ("wxid_", "gh_")


# --------------------------------------------------------------------------------------
# ext_buffer（群成员与群昵称）
# --------------------------------------------------------------------------------------
def _varint(raw: bytes, offset: int) -> tuple[int | None, int]:
    value, shift, position = 0, 0, offset
    while position < len(raw):
        byte = raw[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
        if shift > 63:
            return None, len(raw)
    return None, len(raw)


def _length_fields(raw: bytes):
    """遍历 protobuf，只产出长度前缀字段的 (字段号, 内容)；结构坏了就停止。"""
    position = 0
    while position < len(raw):
        tag, next_position = _varint(raw, position)
        if tag is None or next_position <= position:
            return
        position = next_position
        field_no, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:
            _, next_position = _varint(raw, position)
            if next_position <= position:
                return
            position = next_position
        elif wire_type == 2:
            size, next_position = _varint(raw, position)
            if size is None or next_position <= position:
                return
            position = next_position
            end = position + size
            if end > len(raw):
                return
            yield field_no, raw[position:end]
            position = end
        elif wire_type == 1:
            position += 8
        elif wire_type == 5:
            position += 4
        else:
            return


def _text(value: bytes) -> str:
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def _looks_like_member_id(value: str) -> bool:
    """成员 ID 的形状判定；拿不准就当不是，宁可少一个人也不把昵称错当成 ID。"""
    text = value.strip()
    if not text:
        return False
    if text.startswith(ID_PREFIXES) or "@" in text:
        return True
    return bool(MEMBER_ID.fullmatch(text)) and not text.isdigit()


def _member_records(ext_buffer: bytes) -> list[list[tuple[int, str]]]:
    """把 ext_buffer 解成「每条成员记录 = [(字段号, 文本)]」；解不出内容返回空列表。"""
    records = []
    for _, chunk in _length_fields(ext_buffer):
        if not chunk:
            continue
        strings = []
        for field_no, value in _length_fields(chunk):
            if not value or len(value) > MAX_TEXT_BYTES:
                continue
            text = _text(value)
            if text:
                strings.append((field_no, text))
        if strings:
            records.append(strings)
    return records


def _collect_members(records: list[list[tuple[int, str]]], *, id_field: int,
                     name_field: int) -> dict[str, str]:
    """一次遍历每条记录，产出 {成员 ID: 群昵称}；没设群名片的成员以空串入表。

    空串是刻意保留的：写名字缓存的调用方拿它当「本群确实有这个成员，但没有群名片」，
    再回落到联系人表的 nick_name/remark/alias；只返回设了名片的成员会整条漏掉人。
    """
    names: dict[str, str] = {}
    for strings in records:
        display, member_ids = "", []
        for field_no, value in strings:
            if field_no == id_field and _looks_like_member_id(value):
                member_ids.append(value)
            elif field_no == name_field and value and not display:
                display = value
        for value in member_ids:
            # 同一成员出现多条记录时，保留已取到的非空群昵称。
            if value not in names or (display and not names[value]):
                names[value] = display
    return names


def parse_group_members(ext_buffer: bytes) -> dict[str, str]:
    """解析 `chat_room.ext_buffer`，返回该群**全体成员**的 {成员 ID: 群昵称}。

    没设群名片的成员也在结果里，值为空串（不是漏掉），让写名字缓存的调用方能回落到联系人表。
    只想要群昵称的调用方自己过滤空串即可（见 `group_member_names`）。

    当前布局：字段 1 = 成员 ID、字段 2 = 群昵称。遗留布局：字段 4 = 成员 ID、字段 1 = 群昵称。
    两种布局都靠形状启发式判定，一条都认不出来就返回空字典，让调用方回落到联系人名字，
    而不是猜错成员或把别名当成群昵称。
    """
    if not ext_buffer:
        return {}
    records = _member_records(ext_buffer)
    if not records:
        return {}
    current = any(field_no == 2 or (field_no == 1 and _looks_like_member_id(value))
                  for strings in records for field_no, value in strings)
    if current:
        return _collect_members(records, id_field=1, name_field=2)
    # 遗留布局只在「字段 1 全都不是成员 ID」时才认，避免把别名当成群昵称。
    legacy = [strings for strings in records
              if any(field_no == 4 and _looks_like_member_id(value) for field_no, value in strings)]
    if not legacy or any(_looks_like_member_id(value) for strings in legacy
                         for field_no, value in strings if field_no == 1):
        return {}
    return _collect_members(legacy, id_field=4, name_field=1)


# --------------------------------------------------------------------------------------
# 解密
# --------------------------------------------------------------------------------------
def _decrypt_pages(data: bytes, enc_key: bytes, mac_key: bytes) -> tuple[bytes, int, int]:
    """逐页解出明文 SQLite；返回 (明文, 未通过 HMAC 的页数, 最小的失败页号)。

    页尾的 IV 与 HMAC 用零填充，保持 4096 的页大小与页号对齐。缺 pycryptodome 时抛 ImportError，
    由调用方记日志降级（与 `db_key` 里 pefile 的处理方式一致）。第 1 页不过校验说明密钥或库不匹配，
    由调用方丢掉整份快照（页号错位会让后面的页全部对不上）。
    """
    from Crypto.Cipher import AES

    total = (len(data) + PAGE_SIZE - 1) // PAGE_SIZE
    out, failed, first_bad = bytearray(), 0, 0
    for index in range(total):
        page_no = index + 1
        page = data[index * PAGE_SIZE:(index + 1) * PAGE_SIZE]
        if len(page) < PAGE_SIZE:
            page += b"\0" * (PAGE_SIZE - len(page))
        if not hmac.compare_digest(page[PAGE_SIZE - HMAC_SIZE:], page_hmac(mac_key, page, page_no)):
            failed += 1
            first_bad = first_bad or page_no
        iv = page[PAGE_SIZE - RESERVE_SIZE:PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
        start = SALT_SIZE if page_no == 1 else 0
        plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(page[start:PAGE_SIZE - RESERVE_SIZE])
        if page_no == 1:
            out.extend(SQLITE_HEADER + plain + b"\0" * RESERVE_SIZE)
        else:
            out.extend(plain + b"\0" * RESERVE_SIZE)
    return bytes(out), failed, first_bad


def _rollback_mode(data: bytes) -> bytes:
    """把 WAL 模式的库头改成回滚模式；只读打开明文副本时就不必依赖 -wal 旁路文件。"""
    if data[18:20] == WAL_MODE_HEADER:
        return data[:18] + ROLLBACK_MODE_HEADER + data[20:]
    return data


def _material(page1: bytes, source: Path, *, root, account: str | None):
    """取数据库密钥并判定形态；拿不到返回 None。密钥缓存与 `db_key` 共用一份。"""
    key = find_database_key(root=root, account=account, probe_db=source)
    if not key:
        return None
    return resolve_key_material(bytes.fromhex(key), page1)


# --------------------------------------------------------------------------------------
# WAL 合并（只取属于当前 WAL 代的已提交帧）
# --------------------------------------------------------------------------------------
def _wal_header_checksum(wal: bytes) -> tuple[int, int]:
    """SQLite WAL 头的校验和：按 magic 决定字节序，逐 8 字节成对累加。"""
    big = struct.unpack(">I", wal[:4])[0] == 0x377F0683
    endian = ">" if big else "<"
    first = second = 0
    for low, high in struct.iter_unpack(endian + "II", wal[:24]):
        first = (first + low + second) & 0xFFFFFFFF
        second = (second + high + first) & 0xFFFFFFFF
    return first, second


def _apply_wal(database: bytes, wal: bytes, mac_key: bytes | None) -> tuple[bytes, dict]:
    """把 -wal 里属于当前 WAL 代的已提交帧合并进主库，返回 (合并后的密文, 统计)。

    帧的取舍沿用 SQLite 自己的规则：只有帧头 salt 等于 WAL 头 salt 的帧才属于当前这一代
    WAL；salt 不匹配的是 checkpoint 重置 WAL 后残留的上一代帧，必须跳过，否则会把库回退到
    旧状态（实测微信 4.1.15.13 的 contact.db-wal 就是这种全旧帧的情形）。
    在此之上再用帧页 HMAC 做加密层校验，并按帧头的库页数提交、遵守截断。
    `mac_key` 为 None 表示明文库：没有页 HMAC，只按帧头判据合并。
    """
    info = {"frames": 0, "pages": 0, "size": len(database) // PAGE_SIZE, "stale_frames": 0}
    if len(wal) < WAL_HEADER_SIZE:
        return database, info
    magic, version, page_size = struct.unpack(">III", wal[:12])
    if magic not in WAL_MAGIC or version != WAL_VERSION or page_size != PAGE_SIZE:
        logger.warning("WAL 头无效，忽略该 WAL（页大小 %s）", page_size)
        return database, info
    frame_size = WAL_FRAME_HEADER + PAGE_SIZE
    info["stale_frames"] = (len(wal) - WAL_HEADER_SIZE) // frame_size
    # WAL 头校验和覆盖 salt；头和 salt 都对得上才敢用它判定帧的代次。
    if _wal_header_checksum(wal) != struct.unpack(">II", wal[24:32]):
        logger.warning("WAL 头校验和不对（可能正被并发重写），本次忽略 WAL")
        return database, info
    salt = wal[16:24]
    base_pages = len(database) // PAGE_SIZE
    size, committed, pending = base_pages, {}, {}
    for offset in range(WAL_HEADER_SIZE, len(wal) - frame_size + 1, frame_size):
        header = wal[offset:offset + WAL_FRAME_HEADER]
        if header[8:16] != salt:
            current = (offset - WAL_HEADER_SIZE) // frame_size
            info["stale_frames"] -= current
            logger.info("WAL 共 %s 帧：前 %s 帧属于当前 WAL 代，其后 %s 帧是上一代残留，跳过",
                        current + info["stale_frames"], current, info["stale_frames"])
            break
        page_no, db_size = struct.unpack(">II", header[:8])
        if page_no == 0 or page_no > 0xFFFFFFFE:
            break
        page = wal[offset + WAL_FRAME_HEADER:offset + frame_size]
        if mac_key is not None and not hmac.compare_digest(
                page[-HMAC_SIZE:], page_hmac(mac_key, page, page_no)):
            logger.warning("WAL 第 %s 帧未通过页 HMAC，只使用已提交的帧", page_no)
            break
        pending[page_no] = page
        if db_size:
            committed.update(pending)
            pending.clear()
            committed = {no: value for no, value in committed.items() if no <= db_size}
            base_pages = min(base_pages, db_size)
            size = db_size
            info["frames"] = (offset + frame_size - WAL_HEADER_SIZE) // frame_size
    if not committed:
        return database, info
    merged = bytearray(database[:base_pages * PAGE_SIZE])
    merged.extend(b"\0" * (size * PAGE_SIZE - len(merged)))
    for page_no, page in committed.items():
        merged[(page_no - 1) * PAGE_SIZE:page_no * PAGE_SIZE] = page
    info.update(pages=len(committed), size=size)
    return bytes(merged), info


# --------------------------------------------------------------------------------------
# 明文快照
# --------------------------------------------------------------------------------------
def _remove_tree(directory: Path | None) -> None:
    """删掉本轮写出的临时目录；删不掉只记 WARNING，不影响调用方收尾。"""
    if directory is None:
        return
    try:
        shutil.rmtree(directory)
    except OSError as error:
        logger.warning("清理临时目录失败：%s", error)


def _replace_file(path: Path, data: bytes) -> bool:
    """先写同目录临时文件再原子替换，避免半个副本被当成有效快照复用。"""
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
        return True
    except OSError as error:
        logger.warning("写入明文副本失败：%s", error)
        try:
            temporary.unlink()
        except OSError:
            pass
        return False


def build_snapshot(source: Path, cache_dir: Path, *, root, account: str | None) -> Path | None:
    """复制 → 合并当前 WAL 代 → 解密，产出缓存目录里的明文快照；任何一步失败返回 None。

    加密与 WAL 的中间副本都放在系统临时目录，读完即删；缓存目录里只会留下明文快照本身。
    失败时不会写到 `PLAINTEXT_NAME`，所以不会把坏文件留在那儿给下一轮复用。
    """
    output = Path(cache_dir) / PLAINTEXT_NAME
    work = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        encrypted = work / ENCRYPTED_NAME
        wal_copy = work / (ENCRYPTED_NAME + WAL_SUFFIX)
        try:
            shutil.copyfile(source, encrypted)
            wal = Path(str(source) + WAL_SUFFIX)
            if wal.is_file():
                shutil.copyfile(wal, wal_copy)
            data = encrypted.read_bytes()
            wal_data = wal_copy.read_bytes() if wal_copy.exists() else b""
        except OSError as error:
            logger.warning("复制 contact.db 到临时目录失败：%s", error)
            return None
        if len(data) < PAGE_SIZE:
            logger.warning("contact.db 不足一页，无法解密")
            return None
        if data[:16] == SQLITE_HEADER[:16]:
            # 个别版本可能不加密：明文库在 WAL 模式下同样把已提交页留在 -wal 里，只拷主文件
            # 会得到缺最新提交的快照。这里按帧头 salt 判据合并当前代帧（明文没有页 HMAC）。
            merged, wal_info = _apply_wal(data, wal_data, None)
            if wal_info["frames"]:
                merged = merged[:28] + wal_info["size"].to_bytes(4, "big") + merged[32:]
            return output if _replace_file(output, _rollback_mode(merged)) else None
        resolved = _material(data[:PAGE_SIZE], source, root=root, account=account)
        if resolved is None:
            logger.warning("数据库密钥不可用（找不到密钥或页 1 HMAC 校验不过），本轮跳过")
            return None
        enc_key, mac_key, mode = resolved
        merged, wal_info = _apply_wal(data, wal_data, mac_key)
        try:
            plain, failed, first_bad = _decrypt_pages(merged, enc_key, mac_key)
        except ImportError as error:
            logger.warning("缺少 pycryptodome，无法解密 contact.db：%s", error)
            return None
        except (ValueError, OSError) as error:
            logger.warning("AES 解密失败：%s", error)
            return None
        if plain[:16] != SQLITE_HEADER[:16] or first_bad == 1:
            # 第 1 页不过校验 → 密钥或库不匹配，整份快照信任不了，直接丢弃。
            logger.warning("解密结果校验不过（密钥形态 %s，首个失败页 %s）", mode, first_bad)
            return None
        if wal_info["frames"]:
            # 帧提交会改变库页数（含截断），明文头里的页数必须跟着改，否则文件自相矛盾。
            plain = plain[:28] + wal_info["size"].to_bytes(4, "big") + plain[32:]
        if not _replace_file(output, _rollback_mode(plain)):
            return None
        if failed:
            logger.warning("有 %s 页未通过 HMAC 校验，可能是复制期间微信正在写库", failed)
        logger.info("明文快照已重建（密钥形态 %s，合并 WAL 当前代 %s 帧、库 %s 页）",
                    mode, wal_info["frames"], wal_info["size"] or len(data) // PAGE_SIZE)
        return output
    finally:
        _remove_tree(work)


def _source_stamp(source: Path) -> tuple:
    """源库与其 WAL 的 (mtime_ns, size)；两者都不变才允许复用上次的快照。

    微信写 WAL 一定会更新 mtime，因此这个判据足以发现「数据变了」；size 一起比对是为了
    防止同一时刻被覆盖写入而 mtime 恰好相同的情况。
    """
    stamp = []
    for target in (source, Path(str(source) + WAL_SUFFIX)):
        try:
            stat = target.stat()
            stamp += [stat.st_mtime_ns, stat.st_size]
        except OSError:
            stamp += [0, 0]
    return tuple(stamp)


# --------------------------------------------------------------------------------------
# 明文副本上的查询
# --------------------------------------------------------------------------------------
def _connect(database: Path) -> sqlite3.Connection:
    """只读打开明文副本；不写原文件，也不写副本。"""
    return sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)


def _blob(value) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return b""


def _field(value) -> str:
    """名字字段统一成 str；不同版本可能把文本存成 BLOB。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace").strip()
    return str(value).strip()


def read_contact_names(database: Path) -> dict[str, tuple[str, str, str]]:
    """{成员 ID: (微信昵称, 备注, 别名)}；读取失败返回空字典。"""
    try:
        with closing(_connect(database)) as connection:
            rows = connection.execute("SELECT username, nick_name, remark, alias FROM contact").fetchall()
    except sqlite3.Error as error:
        logger.warning("读取 contact 表失败：%s", error)
        return {}
    names = {}
    for username, nick_name, remark, alias in rows:
        member = _field(username)
        if member:
            names[member] = (_field(nick_name), _field(remark), _field(alias))
    return names


def read_group_members(database: Path, room: str) -> dict[str, str]:
    """该群全体成员的 `{成员 ID: 群昵称}`，没设群名片的人值为空串；解析不出内容返回空字典。"""
    try:
        with closing(_connect(database)) as connection:
            row = connection.execute("SELECT ext_buffer FROM chat_room WHERE username = ? LIMIT 1",
                                     (room,)).fetchone()
    except sqlite3.Error as error:
        logger.warning("读取群 %s 的成员列表失败：%s", room, error)
        return {}
    return parse_group_members(_blob(row[0])) if row else {}


def read_group_names(database: Path) -> dict[str, str]:
    """一次取出全部群名；取值顺序 remark → nick_name → alias，都没有就退回群 ID。"""
    try:
        with closing(_connect(database)) as connection:
            rows = connection.execute(
                "SELECT username, nick_name, remark, alias FROM contact WHERE username LIKE '%@chatroom'"
            ).fetchall()
    except sqlite3.Error as error:
        logger.warning("读取群名失败：%s", error)
        return {}
    names = {}
    for username, nick_name, remark, alias in rows:
        room = _field(username)
        if not room:
            continue
        names[room] = _field(remark) or _field(nick_name) or _field(alias) or room
    return names


def _usable(name: str) -> str:
    """成员库能存下的名字：1–64 字符且不含控制字符；存不下的当取不到，避免整条被丢弃。"""
    text = name.strip()
    if not text or len(text) > MAX_NAME_CHARS or any(ord(ch) < 32 for ch in text):
        return ""
    return text


def pick_name(nickname: str, contact: tuple[str, str, str] | None) -> str:
    """按 R1 的优先级取名：群昵称 > 微信昵称 > 备注 > alias；都取不到返回空串。"""
    for value in (nickname, *(contact or ())):
        usable = _usable(value)
        if usable:
            return usable
    return ""


# --------------------------------------------------------------------------------------
# 引擎：后台刷新 + 按需查询
# --------------------------------------------------------------------------------------
class WechatNameSource:
    """本机微信库的唯一只读引擎：后台把名字写进成员库，同时按需给出群名与群昵称。

    `members` 是 `MemberStore`：既提供要处理的群列表（`groups`），也是写入目标
    （`remember_wechat_name`）。任何一轮失败都只记日志并保留上一轮结果，绝不抛给调用方。
    查询接口在数据未就绪时安静返回空结果（`None` / `{}`），调用方照旧回落。
    """

    def __init__(self, *, members, root=None, account: str | None = None,
                 refresh_seconds=DEFAULT_REFRESH_SECONDS):
        self.members = members
        self.root = Path(root) if root is not None else None
        self.account = account if account is not None else getattr(members, "account", None)
        self.refresh_seconds = refresh_seconds
        self.rounds = 0
        self.last: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache_dir: Path | None = None
        self._snapshot: Path | None = None
        self._stamp: tuple | None = None
        # 快照构建失败后的退避截止时刻（time.monotonic）；到点前继续用上一份快照，不再重建。
        self._retry_after = 0.0
        # 查询结果的缓存，只对当前这一份快照有效；`_snapshot_id` 在发布新快照时自增，
        # 缓存项记下代次，代次不符即视为失效（旧快照的解析结果不能再喂给新快照）。
        self._snapshot_id = 0
        self._names_cache: dict[str, tuple[int, dict[str, str]]] = {}
        self._groups_cache: tuple[int, dict[str, str]] | None = None
        # 快照发布/读取用 `_lock`（只做字段交换），构建用 `_build_lock`（耗时长，必须在锁外）。
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        # 缓存自己的锁：只保护上面的两个字典，不覆盖 SQLite 查询，避免把读方串行化。
        self._cache_lock = threading.Lock()

    # ---- 后台刷新 ----------------------------------------------------------------
    def start(self):
        """启动后台线程（立即刷一轮）；已在运行时什么都不做。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="WechatNames", daemon=True)
        self._thread.start()

    def stop(self, timeout=10.0):
        """请求退出并等待线程结束；幂等，重复调用只等一次。

        明文快照是刻意保留的复用缓存（README 有说明），这里不删；用户可随时手工清理
        `.runtime/contact_cache/`。
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            # 每轮先做一次刷新，再等间隔；启动后的第一次因此是「连上就刷」。
            try:
                self.last = self.refresh()
            except Exception:
                # refresh 自己已经吞掉可预期错误，这里只兜住没预料到的异常。
                logger.exception("微信名刷新出现未预期异常，保留上一轮结果")
            self.rounds += 1
            if self._stop.wait(self.refresh_seconds):
                break

    def _source_path(self) -> Path | None:
        """当前账号的 contact.db；账号未知或目录/文件不存在时返回 None。"""
        if not self.account:
            logger.warning("未识别到当前登录账号，本轮跳过")
            return None
        directory = locate_data_dir(self.account)
        if directory is None:
            logger.warning("未找到账号 %s 的数据目录，本轮跳过", self.account)
            return None
        path = directory.joinpath(*CONTACT_RELATIVE)
        if not path.is_file():
            logger.warning("账号数据目录下没有 contact/contact.db，本轮跳过")
            return None
        return path

    def _cache_directory(self) -> Path | None:
        """明文快照目录：项目运行目录下的 `.runtime/contact_cache/`；未给 root 时退到系统临时目录。"""
        if self._cache_dir is None:
            if self.root is not None:
                self._cache_dir = self.root.joinpath(".runtime", *CACHE_SUBDIR)
            else:
                self._cache_dir = Path(tempfile.mkdtemp(prefix="wxbot-contact-cache-"))
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            logger.warning("无法准备明文快照目录：%s", error)
            return None
        return self._cache_dir

    def _obtain_snapshot(self) -> tuple[Path | None, bool]:
        """取可查询的明文快照，返回 (路径, 本轮是否重新解密)。

        源库与 WAL 的 (mtime, size) 都不变时复用上一次的快照，不做重复解密；构建失败后的
        退避期内也不再尝试。构建（整库复制 + 解密，可能数秒）在 `_build_lock` 下、`_lock` 外
        进行：重建期间读方继续拿上一份快照，不会阻塞消息回调。
        """
        source = self._source_path()
        if source is None:
            return None, False
        stamp = _source_stamp(source)
        with self._lock:
            if self._snapshot is not None and self._snapshot.is_file() and stamp == self._stamp:
                return self._snapshot, False
            # 退避期内沿用上一份快照（可能是 None，即首次构建还没成功过）。
            if time.monotonic() < self._retry_after:
                return self._snapshot, False
        if not self._build_lock.acquire(blocking=False):
            # 已有别的线程在重建：读方直接用上一份快照，不排队等它。
            with self._lock:
                return self._snapshot, False
        try:
            with self._lock:
                if self._snapshot is not None and self._snapshot.is_file() and stamp == self._stamp:
                    return self._snapshot, False
                if time.monotonic() < self._retry_after:
                    return self._snapshot, False
            directory = self._cache_directory()
            built = build_snapshot(source, directory, root=self.root,
                                   account=self.account) if directory is not None else None
            with self._lock:
                if built is not None:
                    self._snapshot, self._stamp, self._retry_after = built, stamp, 0.0
                    # 新快照已发布：上一份快照的解析结果与群名映射全部作废。
                    self._snapshot_id += 1
                    with self._cache_lock:
                        self._names_cache.clear()
                        self._groups_cache = None
                else:
                    # 构建失败但上一份快照文件仍有效：保留旧路径，只推迟下一次重试。
                    self._retry_after = time.monotonic() + BUILD_RETRY_SECONDS
                return self._snapshot, built is not None
        finally:
            self._build_lock.release()

    def _snapshot_view(self) -> tuple[Path | None, int]:
        """查询用的 (快照路径, 快照代次)；代次先取，重建期间读到的旧内容不会被当成新快照缓存。"""
        with self._lock:
            snapshot_id = self._snapshot_id
        snapshot, _ = self._obtain_snapshot()
        return snapshot, snapshot_id

    def refresh(self) -> dict:
        """跑一轮：定位库 → 取明文快照 → 查询 → 写入微信名缓存。任何失败都只记日志。

        返回本轮统计（成功与否、群数、成员数、解析到的名字数、写入数、是否复用快照与耗时）；
        日志同样每轮一条，成功记 INFO、失败记 WARNING，便于确认它确实在工作。
        """
        started = time.monotonic()
        summary = {"ok": False, "rooms": 0, "members": 0, "names": 0, "written": 0,
                   "reused": False, "error": ""}
        try:
            rooms = sorted(room for room in self.members.groups if room.endswith("@chatroom"))
            if not rooms:
                summary["error"] = "成员库没有启用的群"
            else:
                snapshot, rebuilt = self._obtain_snapshot()
                summary["reused"] = snapshot is not None and not rebuilt
                if snapshot is None:
                    summary["error"] = "找不到或无法解密当前账号的 contact.db"
                else:
                    summary.update(self._apply(snapshot, rooms))
                    summary["ok"] = summary["names"] > 0
                    if not summary["ok"]:
                        summary["error"] = "没有解析到任何名字"
        except Exception as error:  # noqa: BLE001 - 刷新失败只能降级，不能影响收发
            summary["error"] = f"{type(error).__name__}: {error}"
            logger.debug("微信名刷新异常", exc_info=True)
        elapsed = time.monotonic() - started
        summary["seconds"] = elapsed
        if summary["ok"]:
            logger.info("微信名已刷新：群 %s 个、成员 %s 个、解析到名字 %s 条（写入 %s 条），"
                        "明文快照%s，耗时 %.1fs",
                        summary["rooms"], summary["members"], summary["names"], summary["written"],
                        "复用" if summary["reused"] else "已重建", elapsed)
        else:
            logger.warning("微信名刷新失败（保留上一轮结果）：%s，耗时 %.1fs", summary["error"], elapsed)
        self.last = summary
        return summary

    def _apply(self, snapshot: Path, rooms: list[str]) -> dict:
        """在明文副本上查名字并写回成员库；返回本轮统计（不含耗时）。

        同一个成员可能在多个启用的群里出现，按成员 ID 去重；先解析出名字的那个群优先，
        名字为空时留给后面的群再试，最终仍取不到就跳过这个人。
        """
        contacts = read_contact_names(snapshot)
        members, resolved = set(), {}
        for room in rooms:
            for wxid, nickname in read_group_members(snapshot, room).items():
                members.add(wxid)
                if not resolved.get(wxid):
                    resolved[wxid] = pick_name(nickname, contacts.get(wxid))
        written = 0
        for wxid, name in resolved.items():
            if not name:
                # 四级都取不到：跳过这个人，不写占位符。
                continue
            try:
                self.members.remember_wechat_name(wxid, name)
                written += 1
            except Exception:  # noqa: BLE001 - 非法 ID / 名字存不下等只跳过这一条
                logger.debug("微信名写入失败：成员=%s", wxid, exc_info=True)
        return {"rooms": len(rooms), "members": len(members),
                "names": sum(1 for name in resolved.values() if name), "written": written}

    # ---- 查询接口 ----------------------------------------------------------------
    def group_names(self) -> dict[str, str]:
        """全部群名（群 ID → 显示名）；库不可用时返回空字典。"""
        snapshot, snapshot_id = self._snapshot_view()
        if not snapshot:
            return {}
        with self._cache_lock:
            cached = self._groups_cache
            if cached is not None and cached[0] == snapshot_id:
                # 返回副本：缓存结构不能被调用方改坏。
                return dict(cached[1])
        names = read_group_names(snapshot)
        if names:
            # 只缓存非空结果：瞬时读失败不能被当成「这一代就是没有群名」长期复用。
            with self._cache_lock:
                self._groups_cache = (snapshot_id, names)
        return dict(names)

    def group_name(self, room) -> str | None:
        """某群显示名；不是群、库或群不可用时返回 None（由调用方回落显示群 ID）。"""
        if not isinstance(room, str) or not room.endswith("@chatroom"):
            return None
        return self.group_names().get(room)

    def group_member_names(self, room) -> dict[str, str]:
        """该群「成员 ID → 群昵称」；只包含设置了群名片的成员，取不到返回空字典。"""
        if not isinstance(room, str) or not room.endswith("@chatroom"):
            return {}
        snapshot, snapshot_id = self._snapshot_view()
        if not snapshot:
            return {}
        with self._cache_lock:
            cached = self._names_cache.get(room)
            if cached is not None and cached[0] == snapshot_id:
                # 返回副本：同一份缓存会同时被消息回调线程和管理台线程读取。
                return dict(cached[1])
        # 上游返回的是全体成员（没名片的值为空串），对外仍只暴露群昵称；
        # 读取路径与写缓存路径同口径过滤，超长或带控制字符的名字一律跳过。
        names = {member: usable for member, nickname in read_group_members(snapshot, room).items()
                 if (usable := _usable(nickname))}
        if names:
            # 只缓存非空结果：瞬时读失败不能被当成「这个群没有群昵称」长期复用。
            with self._cache_lock:
                self._names_cache[room] = (snapshot_id, names)
        return dict(names)

    def group_member_name(self, room, member_id) -> str:
        """单个成员在某群的群昵称；没设置群名片时返回空串，不编造。"""
        if not isinstance(member_id, str):
            return ""
        return self.group_member_names(room).get(member_id, "")
