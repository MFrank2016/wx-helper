"""当前登录账号的数据库密钥只读获取：静态扫 DLL、只读扫内存、页 1 HMAC 验证。

三步与实测口径（微信 4.1.15.13）一致：
1. 从 `Weixin.dll` 的代码段静态扫出 32 字节辅助 key（`mov rdx, imm64` 连写 4 次）；
2. 只申请读权限枚举微信进程的私有内存，按密钥持有对象的特征定位候选指针并读 32 字节；
3. 用数据库第 1 页的 HMAC 验证「内存候选 ⊕ DLL 辅助 key」与「内存候选本身」两种形态。

密钥按账号缓存到 `.runtime/db_key.json`；缓存只写密钥与元数据，不写任何聊天数据。
微信没运行、版本变了、扫不到候选、候选全不通过等前置条件缺失时只记 WARNING 并返回
None，由调用方降级；本模块绝不写微信进程内存、不注入、不 Hook、不创建远程线程。
"""
from __future__ import annotations

import ctypes as c
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes as w
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .account import read_current_account

logger = logging.getLogger("DatabaseKey")

# 只读打开微信进程：PROCESS_QUERY_INFORMATION | PROCESS_VM_READ。
# 与 native/text_service.py 的只读核对口径一致，绝不申请写权限。
PROCESS_READ_ACCESS = 0x0410
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
# PAGE_NOACCESS=1，PAGE_GUARD=0x100：这两种保护下 ReadProcessMemory 必然失败，先跳过省时间。
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
MAX_USER_ADDRESS = 0x7FFFFFFF0000
MIN_POINTER = 0x10000

WECHAT_EXE = "weixin.exe"
CORE_DLL = "weixin.dll"

PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
# 微信 4.x 的 WCDB 页尾保留 IV + HMAC，导出明文时用零填充以保持页大小。
RESERVE_SIZE = IV_SIZE + HMAC_SIZE
# SQLCipher 的口令派生轮数；页 1 HMAC 只覆盖 page[16:4032] 加页号。
PBKDF2_ROUNDS = 256000
MAC_SALT_XOR = 0x3A
HMAC_DATA_END = PAGE_SIZE - RESERVE_SIZE + IV_SIZE

# 内存分块读：4 MiB 兼顾单次拷贝开销与进度可见性；重叠覆盖跨块的特征与指针。
READ_CHUNK = 4 * 1024 * 1024
SEARCH_OVERLAP = 64
# 密钥持有对象特征（对应参考实现的 GetKeyAddrStub）：[8 字节指针][int64 0][int64 32][int64 47]。
# 命中处往前 8 字节就是候选指针。
KEY_STUB_TAIL = b"\x00" * 8 + struct.pack("<Q", 32) + struct.pack("<Q", 47)
# 候选预筛：随机密钥的相异字节数几乎不会低于 15，可打印字符也不会超过 24；
# 这两条能一次性滤掉绝大多数普通文本块。
MIN_DISTINCT_BYTES = 15
MAX_PRINTABLE_BYTES = 24
MIN_PRINTABLE = 32
MAX_PRINTABLE = 126

# pbkdf2_hmac 在 OpenSSL 里会释放 GIL，实测 8 线程有约 4.5 倍加速；
# 用线程而不是多进程，避免 Windows spawn 重新导入主程序带来的副作用。
VERIFY_WORKERS = min(8, os.cpu_count() or 1)
# 全量内存扫描的时间预算：超时就用已扫到的候选继续验证，不让启动流程无上限等待。
SCAN_BUDGET_SECONDS = 30.0

CODE_SECTION_FLAG = 0x20000000
DLL_MATCH_LIMIT = 96

KEY_CACHE_NAME = "db_key.json"
KEY_CACHE_VERSION = 1
KEY_HEX = re.compile(r"^[0-9a-f]{64}$")
PREFERRED_PROBE_DB = ("contact/contact.db", "session/session.db", "message/message_0.db")


# --------------------------------------------------------------------------------------
# Windows 绑定
# --------------------------------------------------------------------------------------
class _ProcessEntry(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", c.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
                ("szExeFile", w.WCHAR * 260)]


class _ModuleEntry(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD), ("th32ProcessID", w.DWORD),
                ("GlblcntUsage", w.DWORD), ("ProccntUsage", w.DWORD), ("modBaseAddr", c.c_void_p),
                ("modBaseSize", w.DWORD), ("hModule", w.HMODULE),
                ("szModule", w.WCHAR * 256), ("szExePath", w.WCHAR * 260)]


class _MemoryBasicInformation(c.Structure):
    _fields_ = [("BaseAddress", c.c_void_p), ("AllocationBase", c.c_void_p), ("AllocationProtect", w.DWORD),
                ("RegionSize", c.c_size_t), ("State", w.DWORD), ("Protect", w.DWORD), ("Type", w.DWORD)]


@lru_cache(maxsize=1)
def _kernel():
    k = c.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    k.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
    k.ReadProcessMemory.restype = w.BOOL
    k.VirtualQueryEx.argtypes = [w.HANDLE, c.c_void_p, c.POINTER(_MemoryBasicInformation), c.c_size_t]
    k.VirtualQueryEx.restype = c.c_size_t
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    k.QueryFullProcessImageNameW.restype = w.BOOL
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(_ProcessEntry)]
    k.Process32NextW.argtypes = [w.HANDLE, c.POINTER(_ProcessEntry)]
    k.Module32FirstW.argtypes = [w.HANDLE, c.POINTER(_ModuleEntry)]
    k.Module32NextW.argtypes = [w.HANDLE, c.POINTER(_ModuleEntry)]
    return k


def _checked(value, label: str):
    if not value:
        raise c.WinError(c.get_last_error(), f"{label}失败")
    return value


def _snapshot(flags: int, pid: int = 0):
    handle = _kernel().CreateToolhelp32Snapshot(flags, pid)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error(), "创建系统快照失败")
    return handle


# --------------------------------------------------------------------------------------
# 进程、模块与安装目录
# --------------------------------------------------------------------------------------
def find_wechat_processes() -> list[int]:
    """只读枚举加载了 Weixin.dll 的微信主进程 PID；枚举失败返回空列表。"""
    k, found = _kernel(), []
    try:
        handle = _snapshot(0x2)
    except OSError as error:
        logger.warning("枚举微信进程失败：%s", error)
        return []
    try:
        entry = _ProcessEntry()
        entry.dwSize = c.sizeof(entry)
        if not k.Process32FirstW(handle, c.byref(entry)):
            return []
        while True:
            # 只有加载了核心 DLL 的进程才是主进程，helper 进程没有账号数据。
            if entry.szExeFile.lower() == WECHAT_EXE and _module_info(entry.th32ProcessID, CORE_DLL):
                found.append(int(entry.th32ProcessID))
            if not k.Process32NextW(handle, c.byref(entry)):
                break
    finally:
        k.CloseHandle(handle)
    return found


def _module_info(pid: int, name: str) -> tuple[int, str] | None:
    """一次模块快照里取基址与文件路径；进程已退出或模块不存在返回 None。"""
    k = _kernel()
    try:
        handle = _snapshot(0x8 | 0x10, pid)
    except OSError:
        return None
    try:
        entry = _ModuleEntry()
        entry.dwSize = c.sizeof(entry)
        if not k.Module32FirstW(handle, c.byref(entry)):
            return None
        while True:
            if entry.szModule.lower() == name:
                return int(entry.modBaseAddr or 0), entry.szExePath
            if not k.Module32NextW(handle, c.byref(entry)):
                return None
    finally:
        k.CloseHandle(handle)


def _open_readonly(pid: int):
    return _checked(_kernel().OpenProcess(PROCESS_READ_ACCESS, False, pid), "只读打开微信进程")


def _memory_reader(handle) -> Callable[[int, int], bytes]:
    """把句柄包成 account.py 需要的 read_memory(address, size)；不完整读取按失败抛出。"""
    def read(address: int, size: int) -> bytes:
        buffer = c.create_string_buffer(size)
        count = c.c_size_t()
        if not _kernel().ReadProcessMemory(handle, c.c_void_p(address), buffer, size, c.byref(count)):
            raise OSError("ReadProcessMemory 失败")
        return buffer.raw[: count.value]
    return read


def process_image_path(pid: int) -> str | None:
    """只读取目标进程的可执行文件路径；失败返回 None。"""
    k = _kernel()
    try:
        handle = _open_readonly(pid)
    except OSError:
        return None
    try:
        size = w.DWORD(32768)
        buffer = c.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(handle, 0, buffer, c.byref(size)):
            return None
        return buffer.value or None
    finally:
        k.CloseHandle(handle)


def read_process_account(pid: int) -> str | None:
    """只读读目标进程当前登录的账号 ID；未登录或读取中发生切换返回 None。"""
    info = _module_info(pid, CORE_DLL)
    if not info or not info[0]:
        return None
    try:
        handle = _open_readonly(pid)
    except OSError:
        return None
    try:
        return read_current_account(_memory_reader(handle), info[0])
    finally:
        _kernel().CloseHandle(handle)


def locate_install_dir(pid: int) -> Path | None:
    """按进程实际加载的 Weixin.dll 路径定位安装目录；定位不到返回 None。

    安装布局是两层：安装根目录放 Weixin.exe，Weixin.dll 在按版本号命名的子目录里，
    所以不能从 exe 所在目录直接推断。用模块列表里那份正在生效的 DLL 路径最准确，
    也顺带保证了后续扫描的就是当前运行版本；模块路径读不到时退回 exe 目录。
    """
    info = _module_info(pid, CORE_DLL)
    if info and info[1]:
        dll_path = Path(info[1])
        if dll_path.is_file():
            return dll_path.parent
    image = process_image_path(pid)
    if image:
        directory = Path(image).parent
        if (directory / "Weixin.dll").is_file():
            return directory
    return None


def locate_data_dir(account: str, *, search_roots: Iterable[Path] | None = None) -> Path | None:
    """按当前登录账号匹配 `xwechat_files\\<wxid>_<后缀>\\db_storage` 目录。

    微信允许把文件存储位置改到别处，所以按常见父目录逐个探测，并用目录名前缀与账号 ID
    精确匹配；不写死任何一个账号目录，匹配不到返回 None。
    """
    roots = list(search_roots) if search_roots is not None else _default_search_roots()
    for root in roots:
        base = Path(root) / "xwechat_files"
        if not base.is_dir():
            continue
        for candidate in sorted(base.glob(f"{account}_*/db_storage")):
            if candidate.is_dir():
                return candidate
        # 个别版本目录名没有后缀，允许精确同名。
        exact = base / account / "db_storage"
        if exact.is_dir():
            return exact
    return None


def _default_search_roots() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return [home / "Documents", home]


# --------------------------------------------------------------------------------------
# 第 1 步：静态扫 Weixin.dll 的辅助 key
# --------------------------------------------------------------------------------------
def scan_dll_helper_keys(dll_path: Path) -> list[bytes]:
    """从 Weixin.dll 的代码段扫出 32 字节辅助 key；pefile 缺失或解析失败返回空列表。

    用项目已声明的 pefile 定位代码段（省掉手写 PE 解析），但按段流式读文件，
    不把 200 MB 的 DLL 整体读进内存。
    """
    try:
        import pefile
    except ImportError:
        logger.warning("缺少 pefile，无法扫描 Weixin.dll 的辅助 key")
        return []
    try:
        pe = pefile.PE(str(dll_path), fast_load=True)
    except Exception as error:
        logger.warning("解析 Weixin.dll 失败：%s", error)
        return []
    sections = []
    try:
        for section in pe.sections:
            if int(section.Characteristics) & CODE_SECTION_FLAG:
                sections.append((int(section.PointerToRawData), int(section.SizeOfRawData)))
    finally:
        # pefile 会缓存整份文件映射，扫完立刻释放。
        try:
            pe.close()
        except Exception:  # noqa: BLE001 - 释放失败不影响扫描结果
            pass
    if not sections:
        return []
    # `mov rdx, imm64` 连写 4 次、中间夹 3–8 字节，紧跟 `test rax, rax`。
    pattern = re.compile(
        b"^\x48\xBA(.{8}).{3,8}?\x48\xBA(.{8}).{3,8}?\x48\xBA(.{8}).{3,8}?\x48\xBA(.{8}).{3,8}?\x48\x85\xC0",
        re.DOTALL)
    keys, seen = [], set()
    with Path(dll_path).open("rb") as stream:
        for offset, size in sections:
            stream.seek(offset)
            remaining = size
            while remaining > 0:
                block = stream.read(min(READ_CHUNK, remaining) + SEARCH_OVERLAP)
                if not block:
                    break
                cursor = 0
                while True:
                    index = block.find(b"\x48\xBA", cursor)
                    if index == -1 or index >= min(READ_CHUNK, remaining):
                        break
                    match = pattern.match(block[index:index + DLL_MATCH_LIMIT])
                    if match:
                        key = b"".join(match.groups())
                        if key not in seen:
                            seen.add(key)
                            keys.append(key)
                        cursor = index + len(match.group(0))
                    else:
                        cursor = index + 1
                consumed = min(READ_CHUNK, remaining)
                remaining -= consumed
                if remaining > 0:
                    stream.seek(offset + size - remaining)
    logger.info("Weixin.dll 辅助 key 候选：%s 个", len(keys))
    return keys


# --------------------------------------------------------------------------------------
# 第 2 步：只读扫微信进程内存取候选
# --------------------------------------------------------------------------------------
def scan_process_candidates(pid: int, *, budget: float = SCAN_BUDGET_SECONDS) -> list[bytes]:
    """只读扫描微信进程内存，返回过预筛的 32 字节候选；失败返回空列表。"""
    try:
        handle = _open_readonly(pid)
    except OSError as error:
        logger.warning("只读打开微信进程失败：%s", error)
        return []
    try:
        reader = _memory_reader(handle)
        pointers = _stub_pointers(reader, _private_regions(handle), budget)
        blobs, seen = [], set()
        for pointer in pointers:
            blob = _read_blob(reader, pointer)
            if blob is None or blob in seen:
                continue
            seen.add(blob)
            if _looks_like_key(blob):
                blobs.append(blob)
        logger.info("内存候选：指针 %s 个，过预筛 %s 个", len(pointers), len(blobs))
        return blobs
    finally:
        _kernel().CloseHandle(handle)


def _private_regions(handle) -> list[tuple[int, int]]:
    """枚举 MEM_COMMIT|MEM_PRIVATE 且可读的区域，大的优先，尽早扫到密钥。"""
    k = _kernel()
    regions, address = [], 0
    info = _MemoryBasicInformation()
    while address < MAX_USER_ADDRESS:
        if not k.VirtualQueryEx(handle, c.c_void_p(address), c.byref(info), c.sizeof(info)):
            break
        size = int(info.RegionSize)
        if not size:
            break
        if (info.State == MEM_COMMIT and info.Type == MEM_PRIVATE
                and info.Protect != PAGE_NOACCESS and not (info.Protect & PAGE_GUARD)):
            regions.append((int(info.BaseAddress), size))
        address += size
    regions.sort(key=lambda region: -region[1])
    return regions


def _stub_pointers(reader: Callable[[int, int], bytes], regions: Sequence[tuple[int, int]],
                   budget: float) -> list[int]:
    """按特征定位候选指针；超预算就用已扫到的部分继续，不让扫描无上限。"""
    pointers, deadline = [], time.monotonic() + budget
    for base, size in regions:
        if time.monotonic() >= deadline:
            logger.warning("内存扫描超出 %s 秒预算，用已扫到的候选继续", budget)
            break
        position = 0
        while position < size:
            take = min(READ_CHUNK, size - position)
            block = _read_region(reader, base + position, take, size - position)
            if block is None:
                position += take
                continue
            cursor = 0
            while True:
                index = block.find(KEY_STUB_TAIL, cursor)
                if index == -1 or index >= take:
                    break
                if index >= 8:
                    pointer = struct.unpack_from("<Q", block, index - 8)[0]
                    if MIN_POINTER <= pointer < MAX_USER_ADDRESS:
                        pointers.append(pointer)
                cursor = index + KEY_SIZE
            position += take
    return list(dict.fromkeys(pointers))


def _read_region(reader: Callable[[int, int], bytes], address: int, take: int, remaining: int) -> bytes | None:
    """读一块区域；末尾不够重叠长度时只读到区域边界，避免越界读取。"""
    length = take + SEARCH_OVERLAP if remaining - take >= SEARCH_OVERLAP else take
    try:
        value = reader(address, length)
    except OSError:
        return None
    return value if len(value) == length else None


def _read_blob(reader: Callable[[int, int], bytes], address: int) -> bytes | None:
    try:
        value = reader(address, KEY_SIZE)
    except OSError:
        return None
    return value if len(value) == KEY_SIZE else None


def _looks_like_key(blob: bytes) -> bool:
    """随机密钥的字节分布特征；普通文本块会被这两条挡掉。"""
    if len(set(blob)) < MIN_DISTINCT_BYTES:
        return False
    printable = sum(MIN_PRINTABLE <= byte <= MAX_PRINTABLE for byte in blob)
    return printable <= MAX_PRINTABLE_BYTES


# --------------------------------------------------------------------------------------
# 第 3 步：页 1 HMAC 验证
# --------------------------------------------------------------------------------------
def read_page1(db_path) -> bytes | None:
    """只读读数据库文件第 1 页；不足一页或读失败返回 None。"""
    try:
        with Path(db_path).open("rb") as stream:
            page = stream.read(PAGE_SIZE)
    except OSError:
        return None
    return page if len(page) == PAGE_SIZE else None


def page_hmac(mac_key: bytes, page: bytes, page_num: int) -> bytes:
    """页 HMAC：第 1 页跳过 16 字节 salt，覆盖到 IV 起点，再加页号小端。"""
    digest = hmac.new(mac_key, digestmod=hashlib.sha512)
    digest.update(page[SALT_SIZE if page_num == 1 else 0:HMAC_DATA_END])
    digest.update(page_num.to_bytes(4, "little"))
    return digest.digest()


def resolve_key_material(material: bytes, page1: bytes) -> tuple[bytes, bytes, str] | None:
    """按页 1 HMAC 判定密钥形态，返回 (enc_key, mac_key, mode)；不匹配返回 None。

    口令态是实测形态：enc_key 由密钥材料经 256000 轮 PBKDF2 派生。
    原始态（内存里直接是 AES key）先试，因为它只要 2 轮 PBKDF2，几乎不花时间。
    """
    if not material or len(material) != KEY_SIZE or len(page1) != PAGE_SIZE:
        return None
    salt = page1[:SALT_SIZE]
    stored = page1[PAGE_SIZE - HMAC_SIZE:]
    mac_salt = bytes(byte ^ MAC_SALT_XOR for byte in salt)
    raw_mac = hashlib.pbkdf2_hmac("sha512", material, mac_salt, 2, dklen=KEY_SIZE)
    if hmac.compare_digest(stored, page_hmac(raw_mac, page1, 1)):
        return material, raw_mac, "raw_key"
    enc_key = hashlib.pbkdf2_hmac("sha512", material, salt, PBKDF2_ROUNDS, dklen=KEY_SIZE)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)
    if hmac.compare_digest(stored, page_hmac(mac_key, page1, 1)):
        return enc_key, mac_key, "passphrase"
    return None


def verify_page1_hmac(material: bytes, page1: bytes) -> bool:
    """只要页 1 HMAC 能对上就算密钥可用，不关心它属于哪种形态。"""
    return resolve_key_material(material, page1) is not None


def find_database_key(*, root=None, account: str | None = None, probe_db=None,
                      refresh: bool = False) -> str | None:
    """取当前账号的数据库密钥（64 位小写十六进制）；失败返回 None 并记 WARNING。

    先试 `.runtime/db_key.json` 缓存；给了 probe_db（或能定位到账号目录）时顺手用页 1
    HMAC 核对缓存，不通过就整条重扫。任何一步失败都降级，绝不抛给调用方。
    """
    cached = None
    try:
        if account is None:
            account = _first_account()
        if root is not None and account:
            cached = load_cached_key(root, account)
        if probe_db is None:
            probe_db = _default_probe_db(account)
        page1 = read_page1(probe_db) if probe_db else None
        if not refresh and cached:
            if page1 is None or verify_page1_hmac(bytes.fromhex(cached), page1):
                logger.info("本次使用已缓存的数据库密钥（账号 %s）", account or "未知")
                return cached
            logger.warning("缓存的数据库密钥已失效，重新获取")

        pids = find_wechat_processes()
        if not pids:
            logger.warning("未找到运行中的微信进程，跳过数据库密钥获取")
            return cached
        pid = pids[0]
        if account is None:
            account = read_process_account(pid)
        if page1 is None:
            page1 = read_page1(probe_db or _default_probe_db(account))
        if page1 is None:
            logger.warning("找不到可用于校验密钥的数据库文件，跳过数据库密钥获取")
            return cached
        install = locate_install_dir(pid)
        if install is None:
            logger.warning("未定位到微信安装目录，跳过数据库密钥获取")
            return cached
        helper_keys = scan_dll_helper_keys(install / "Weixin.dll")
        candidates = scan_process_candidates(pid)
        plan = _key_plan(candidates, helper_keys)
        if not plan:
            logger.warning("内存中未扫到密钥候选（微信版本可能已变化），跳过数据库密钥获取")
            return cached
        found = _find_material(plan, page1)
        if found is None:
            logger.warning("内存候选与 DLL 辅助 key 的 %s 种组合都不通过页 1 HMAC，"
                           "密钥获取失败（微信版本可能已变化）", len(plan))
            return cached
        label, material = found
        key = material.hex()
        if root is not None and account:
            store_cached_key(root, account, key)
        logger.info("已获取数据库密钥（%s；%s 字节，账号 %s）", label, KEY_SIZE, account or "未知")
        return key
    except Exception as error:  # noqa: BLE001 - 密钥缺失只能降级，不能拖垮主流程
        logger.warning("数据库密钥获取失败：%s: %s", type(error).__name__, error)
        return cached


def _first_account() -> str | None:
    """未指定账号时读第一个能读到账号的微信进程；多账号场景应由调用方显式指定。"""
    for pid in find_wechat_processes():
        account = read_process_account(pid)
        if account:
            return account
    return None


def _key_plan(candidates: Sequence[bytes], helper_keys: Sequence[bytes]) -> list[tuple[str, bytes]]:
    """候选的两种形态：实测形态（内存值 ⊕ DLL 辅助 key）优先，其后是内存值本身。"""
    plan, seen = [], set()
    for index, helper in enumerate(helper_keys, 1):
        for candidate in candidates:
            material = bytes(a ^ b for a, b in zip(candidate, helper))
            if material not in seen:
                seen.add(material)
                plan.append((f"内存候选 ⊕ DLL 辅助 key #{index}", material))
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            plan.append(("内存候选本身", candidate))
    return plan


def _find_material(plan: Sequence[tuple[str, bytes]], page1: bytes) -> tuple[str, bytes] | None:
    """并行验证候选；命中一个就置位让后面的任务立即跳过。"""
    stop = threading.Event()

    def probe(item):
        label, material = item
        if stop.is_set():
            return None
        if verify_page1_hmac(material, page1):
            stop.set()
            return label, material
        return None

    with ThreadPoolExecutor(max_workers=min(VERIFY_WORKERS, max(1, len(plan)))) as pool:
        for result in pool.map(probe, plan):
            if result is not None:
                return result
    return None


def _default_probe_db(account: str | None) -> Path | None:
    """挑一个用于校验的账号数据库；密钥是账号级的，任意一个库都能验证。"""
    if not account:
        return None
    directory = locate_data_dir(account)
    if directory is None:
        return None
    for relative in PREFERRED_PROBE_DB:
        candidate = directory / relative
        if candidate.is_file():
            return candidate
    for candidate in sorted(directory.rglob("*.db")):
        if candidate.name.lower() != "key_info.db" and candidate.stat().st_size >= PAGE_SIZE:
            return candidate
    return None


# --------------------------------------------------------------------------------------
# 密钥缓存
# --------------------------------------------------------------------------------------
def cache_path(root) -> Path:
    """缓存位置与其它运行期数据一致：`<root>/.runtime/db_key.json`。"""
    return Path(root) / ".runtime" / KEY_CACHE_NAME


def load_cached_key(root, account: str) -> str | None:
    """读账号对应的缓存密钥；文件缺失、损坏或账号不匹配都返回 None。"""
    try:
        data = json.loads(cache_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != KEY_CACHE_VERSION:
        return None
    if data.get("account") != account:
        return None
    key = str(data.get("key") or "").lower()
    return key if KEY_HEX.fullmatch(key) else None


def store_cached_key(root, account: str, key_hex: str) -> None:
    """写缓存密钥；只写密钥与元数据，不含任何聊天数据。先写临时文件再替换，避免半截文件。"""
    path = cache_path(root)
    payload = {"version": KEY_CACHE_VERSION, "account": account, "key": key_hex.lower(),
               "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        # 缓存写不进去不影响本次使用，只是下次还要重新扫一遍。
        logger.warning("写入数据库密钥缓存失败：%s", error)
