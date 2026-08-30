"""把本地文件上传到 IMA 知识库的 COS，返回 media_id。

配合 MCP 工具使用：
  1. mcp__ima-mcp__create_media  -> 拿到 media_id + cos_credential
  2. 本脚本上传字节流到 COS
  3. mcp__ima-mcp__add_knowledge(media_id=..., knowledge_base_id=...)

用法（推荐，凭证不进 shell history）:
  # 1) 文件路径
  python ima_cos_upload.py --cred cred.json --file <本地文件>
  # 2) stdin（把 create_media 的整个返回原样喂进来）
  mcp_create_media_output | python ima_cos_upload.py --cred-stdin --file <本地文件>

兼容旧用法（不安全，会打印警告）:
  python ima_cos_upload.py --cred '<凭证 JSON 字符串>' --file <本地文件>

输出 JSON: {"ok": true, "media_id": ..., "status": 200}
仅用标准库（urllib + hmac），不依赖 requests。
"""
import argparse
import hashlib
import hmac
import json
import mimetypes
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

CT_MAP = {
    'md': 'text/markdown', 'markdown': 'text/markdown', 'txt': 'text/plain',
    'pdf': 'application/pdf', 'html': 'text/html', 'csv': 'text/csv',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'mp3': 'audio/mpeg', 'm4a': 'audio/x-m4a', 'wav': 'audio/wav',
}


def sign(cred, method, key, headers):
    key_time = f"{cred['start_time']};{cred['expired_time']}"
    sign_key = hmac.new(cred['secret_key'].encode(), key_time.encode(),
                        hashlib.sha1).hexdigest()
    pathname = '/' + key.lstrip('/')
    # HttpString: method \n pathname \n params \n headers \n
    h_str = '&'.join(
        f'{quote(k.lower(), safe="")}={quote(str(v), safe="")}'
        for k, v in sorted((k.lower(), v) for k, v in headers.items()))
    http_string = f'{method.lower()}\n{pathname}\n\n{h_str}\n'
    string_to_sign = ('sha1\n' + key_time + '\n'
                      + hashlib.sha1(http_string.encode()).hexdigest() + '\n')
    signature = hmac.new(sign_key.encode(), string_to_sign.encode(),
                         hashlib.sha1).hexdigest()
    h_list = ';'.join(sorted(k.lower() for k in headers))
    return ('q-sign-algorithm=sha1'
            f'&q-ak={cred["secret_id"]}'
            f'&q-sign-time={key_time}'
            f'&q-key-time={key_time}'
            f'&q-header-list={h_list}'
            '&q-url-param-list='
            f'&q-signature={signature}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cred', default='',
                    help='含 cos_credential JSON 的文件路径（推荐）；也接受 JSON 字符串（不安全，仅兼容旧用法）')
    ap.add_argument('--cred-stdin', action='store_true',
                    help='从 stdin 读取凭证 JSON，避免密钥进入 shell history 与进程参数')
    ap.add_argument('--file', default='')
    ap.add_argument('--content-type', default='')
    ap.add_argument('--batch', default='',
                    help='批量模式：JSON 数组文件路径，元素为 {"file","media_id","cos_credential"}，逐条上传')
    a = ap.parse_args()

    if a.batch:
        jobs = json.loads(Path(a.batch).read_text(encoding='utf-8'))
        results = []
        code = 0
        for job in jobs:
            r = upload_one(job['file'], job['cos_credential'], job.get('media_id', ''))
            results.append({'file': job['file'], **r})
            if not r['ok']:
                code = 1
        print(json.dumps(results, ensure_ascii=False, indent=2))
        sys.exit(code)

    if a.cred_stdin:
        raw = sys.stdin.read()
    else:
        raw = a.cred.strip()
        p = Path(raw)
        if p.exists():
            raw = p.read_text(encoding='utf-8')
        elif raw:
            # 凭证写进命令行会残留在 shell history，且同主机其他用户 ps 可见
            print('[警告] 通过 --cred 直接传凭证字符串不安全，'
                  '建议改用文件路径或 --cred-stdin', file=sys.stderr)
    if not raw.strip():
        print(json.dumps({'ok': False, 'error': '缺少凭证：用 --cred <文件> 或 --cred-stdin'},
                         ensure_ascii=False))
        sys.exit(1)
    cred = json.loads(raw)
    media_id = cred.get('media_id', '')
    if 'cos_credential' in cred:
        cred = cred['cos_credential']

    r = upload_one(a.file, cred, media_id)
    print(json.dumps(r, ensure_ascii=False))
    sys.exit(0 if r['ok'] else 1)


def upload_one(file_path, cred, media_id):
    """单文件 COS 上传。返回 dict；成功 2xx。"""
    src = Path(file_path)
    data = src.read_bytes()
    ct = CT_MAP.get(src.suffix.lower().lstrip('.'), '') \
        or mimetypes.guess_type(src.name)[0] or 'application/octet-stream'

    bucket = cred['bucket_name']
    appid = cred.get('appid', '')
    region = cred['region']
    hosts = [f'{bucket}.cos.{region}.myqcloud.com']
    if appid and not bucket.endswith('-' + appid):
        hosts.append(f'{bucket}-{appid}.cos.{region}.myqcloud.com')
    if cred.get('custom_domain'):
        hosts.append(cred['custom_domain'].rstrip('/')
                     .replace('https://', '').replace('http://', ''))
    pathname = '/' + cred['cos_key'].lstrip('/')

    last = None
    for host in hosts:
        url = f'https://{host}{pathname}'
        headers = {
            'Host': host,
            'Content-Type': ct,
            'Content-Length': str(len(data)),
            'x-cos-security-token': cred['token'],
        }
        headers['Authorization'] = sign(cred, 'PUT', cred['cos_key'], headers)
        req = urllib.request.Request(url, data=data, method='PUT')
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                last = (r.status, r.read().decode('utf-8', 'replace'), host)
                if 200 <= r.status < 300:
                    break
        except urllib.error.HTTPError as e:
            # HTTPError 的响应体只能读一次：先取出再复用，否则第二次读到的永远是空串
            err_body = e.read().decode('utf-8', 'replace')
            print(f'  [try] {host} -> HTTP {e.code} {err_body[:200]}', file=sys.stderr)
            last = (e.code, err_body, host)
        except urllib.error.URLError as e:
            print(f'  [try] {host} -> {e.reason}', file=sys.stderr)
            last = (0, f'{e.reason}', host)
        else:
            if not (200 <= last[0] < 300):
                print(f'  [try] {host} -> HTTP {last[0]} {last[1][:200]}',
                      file=sys.stderr)

    status, body, host = last
    ok = 200 <= status < 300
    return {'ok': ok, 'status': status, 'host': host, 'etag': body[:300],
            'media_id': media_id, 'size': len(data)}


if __name__ == '__main__':
    main()
