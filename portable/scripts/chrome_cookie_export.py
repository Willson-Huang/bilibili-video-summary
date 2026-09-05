#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从本机 Chrome 读取 B站登录 Cookie 并写入 skill 的 cookie 文件。

Chrome 的 cookie 是加密存储的（Windows 上用 DPAPI 保护主密钥，再用 AES-256-GCM 加密每条值），
F12 手动复制很麻烦，本脚本自动解密，零手工操作。

用法:
  python chrome_cookie_export.py                      # 自动扫描所有 Chrome profile
  python chrome_cookie_export.py --profile Default    # 指定 profile
  python chrome_cookie_export.py --dry-run            # 只看命中情况，不写文件
  python chrome_cookie_export.py --show               # 打印完整 cookie（谨慎，会暴露凭据）

输出目标（与 bili_asr.py 的 COOKIE_FILE 一致）:
  ~/.cache/bilibili-video-summary/.bilibili_cookie
  （可用 BILI_COOKIE / BILI_CACHE 环境变量覆盖）

依赖: cryptography（pip install cryptography）
限制: 仅支持 v10/v11 加密格式；Chrome 127+ 的 v20（App-Bound Encryption）无法解密，会跳过。
"""
import argparse
import base64
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.exit('缺少 cryptography：pip install cryptography -i https://mirrors.cloud.tencent.com/pypi/simple')

CHROME_DIR = Path.home() / 'AppData' / 'Local' / 'Google' / 'Chrome' / 'User Data'
# 便携版：凭据路径与 bili_asr.py 的 BILI_COOKIE / BILI_CACHE 保持一致
CACHE_DIR = Path(os.environ.get('BILI_CACHE', str(Path.home() / '.cache' / 'bilibili-video-summary')))
OUT_FILE = Path(os.environ.get('BILI_COOKIE', str(CACHE_DIR / '.bilibili_cookie')))
WANT = ['SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5', 'buvid4']

sys.stdout.reconfigure(encoding='utf-8')


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def dpapi_decrypt(data: bytes) -> bytes:
    """用当前 Windows 用户凭据解密 DPAPI 数据。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise RuntimeError('CryptUnprotectData 失败')
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def master_key(user_data: Path) -> bytes:
    ls = json.loads((user_data / 'Local State').read_text(encoding='utf-8'))
    enc = base64.b64decode(ls['os_crypt']['encrypted_key'])
    if enc[:5] != b'DPAPI':
        raise RuntimeError(f'unknown key format: {enc[:5]!r}')
    return dpapi_decrypt(enc[5:])


def decrypt_value(blob: bytes, key: bytes):
    """返回 (明文, 状态)。状态: ok / plaintext / v20_unsupported / none"""
    if not blob:
        return None, 'none'
    if blob[:3] in (b'v10', b'v11'):
        try:
            return AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode('utf-8', 'replace'), 'ok'
        except Exception:
            return None, 'decrypt_failed'
    if blob[:3] == b'v20':
        return None, 'v20_unsupported'
    try:
        return blob.decode('utf-8'), 'plaintext'
    except Exception:
        return None, 'unknown'


def profiles(user_data: Path, only=None):
    if only:
        return [user_data / only]
    return sorted(p for p in user_data.iterdir()
                  if p.is_dir() and (p / 'Network' / 'Cookies').exists())


def read_cookies(prof: Path, key: bytes):
    """Chrome 运行时 Cookies 被锁，先复制再读。"""
    src = prof / 'Network' / 'Cookies'
    tmp = Path(tempfile.gettempdir()) / f'_chrome_ck_{prof.name}.db'
    shutil.copy2(src, tmp)
    out = []
    try:
        con = sqlite3.connect(str(tmp))
        try:
            rows = con.execute(
                "SELECT name, value, encrypted_value FROM cookies "
                "WHERE host_key LIKE '%bilibili%'").fetchall()
        finally:
            con.close()
        for name, plain, enc in rows:
            val, st = decrypt_value(enc, key) if enc else (plain, 'plaintext')
            if val is not None:
                out.append((name, val, st))
    finally:
        tmp.unlink(missing_ok=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', help='指定 Chrome profile 目录名，如 Default')
    ap.add_argument('--dry-run', action='store_true', help='只检查，不写文件')
    ap.add_argument('--show', action='store_true', help='打印完整 cookie 值（会暴露凭据）')
    a = ap.parse_args()

    if not CHROME_DIR.exists():
        sys.exit(f'未找到 Chrome 目录: {CHROME_DIR}')
    try:
        key = master_key(CHROME_DIR)
    except Exception as e:
        sys.exit(f'取主密钥失败: {e}')

    picked, v20 = {}, 0
    for prof in profiles(CHROME_DIR, a.profile):
        try:
            for name, val, st in read_cookies(prof, key):
                if st == 'v20_unsupported':
                    v20 += 1
                if name in WANT and name not in picked:
                    picked[name] = val
        except Exception as e:
            print(f'[跳过] {prof.name}: {e}')

    print('=== 命中字段 ===')
    for n in WANT:
        v = picked.get(n)
        mark = '✓' if v else '✗'
        print(f'  {mark} {n}' + (f'  ({len(v)} 字符)' if v else ''))
    if v20:
        print(f'\n[提示] {v20} 条为 v20 加密（Chrome 127+ App-Bound），本脚本不支持')

    if not picked.get('SESSDATA'):
        sys.exit('\n失败：未取到 SESSDATA。可能未登录、profile 不对，或 cookie 为 v20 加密。'
                 '\n回退方案：Chrome F12 → Application → Storage → Cookies → bilibili.com → 复制 SESSDATA')

    cookie = '; '.join(f'{k}={picked[k]}' for k in WANT if picked.get(k))
    if a.show:
        print('\n=== Cookie 全文 ===\n' + cookie)

    if a.dry_run:
        print('\n[dry-run] 未写文件')
        return
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(cookie, encoding='utf-8')
    print(f'\n已写入: {OUT_FILE}  ({len(cookie)} 字符)')
    print('验证: 跑一个视频看 route 是否变为 subtitle:*，或素材包不再出现「未配置登录凭据」')


if __name__ == '__main__':
    main()
