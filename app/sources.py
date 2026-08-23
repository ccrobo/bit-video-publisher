"""视频源抓取 / 文案抓取 / 视频下载"""
import hashlib
import re
from pathlib import Path

import httpx

from .logs import add_log


def dig(obj, path, default=None):
    """按 a.b.0.c 形式的路径取值"""
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        m = re.match(r"^([\w\-]+)\[(\d+)\]$", part)
        if m:
            if isinstance(cur, dict) and m.group(1) in cur:
                cur = cur[m.group(1)]
            else:
                return default
            idx = int(m.group(2))
            if isinstance(cur, list) and idx < len(cur):
                cur = cur[idx]
            else:
                return default
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            i = int(part)
            cur = cur[i] if i < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


def fetch_json(url, headers=None, timeout=30):
    r = httpx.get(url, headers=headers or {}, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def extract_items(payload, items_path, f_title, f_video):
    """从视频源 JSON 中提取 [{title, video_url}]"""
    arr = dig(payload, items_path or "data")
    if isinstance(arr, dict):
        for k in ("list", "items", "records", "rows", "videos"):
            if k in arr and isinstance(arr[k], list):
                arr = arr[k]
                break
    if isinstance(payload, list) and arr is None:
        arr = payload
    if not isinstance(arr, list):
        raise ValueError("视频源解析失败: 未找到视频列表，请检查【列表路径】配置")
    items = []
    for i, it in enumerate(arr):
        v = it if isinstance(it, str) else dig(it, f_video)
        t = "" if not isinstance(it, (dict, list)) else (dig(it, f_title) or "")
        if isinstance(v, str) and v.startswith("http"):
            items.append({"title": str(t) or f"视频{i + 1}", "video_url": v})
    return items


def download_video(url, dest_dir, headers=None):
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    dest = dest_dir / f"{name}.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    add_log(f"开始下载视频: {url[:80]}...")
    with httpx.stream("GET", url, headers=headers or {"User-Agent": "Mozilla/5.0"}, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(256 * 1024):
                f.write(chunk)
        tmp.replace(dest)
    size_mb = dest.stat().st_size / 1024 / 1024
    add_log(f"视频下载完成: {dest.name} ({size_mb:.1f}MB)")
    return dest


def validate_mp4(path):
    """校验下载的文件确实是视频: 大小合理且含mp4容器特征(ftyp)"""
    p = Path(path)
    if not p.exists() or p.stat().st_size < 50 * 1024:
        return False, f"文件过小({p.stat().st_size if p.exists() else 0}字节)，不是有效视频"
    try:
        with open(p, "rb") as f:
            head = f.read(64)
        if b"ftyp" not in head and b"moov" not in head and b"mdat" not in head:
            return False, "文件头缺少mp4容器特征，可能是接口返回的JSON/HTML"
    except Exception as e:
        return False, f"读取文件失败: {e}"
    return True, ""


def _to_str_list(arr):
    caps = []
    for x in arr:
        if isinstance(x, dict):
            v = x.get("text") or x.get("caption") or x.get("content") or ""
        else:
            v = x
        v = str(v).strip()
        if v:
            caps.append(v)
    return caps


def fetch_captions(url, headers=None):
    """文案源: 返回 JSON 数组的 URL"""
    data = fetch_json(url, headers)
    arr = data
    if isinstance(arr, dict):
        found = None
        for k in ("data", "list", "items", "records", "captions", "texts", "rows"):
            v = arr.get(k)
            if isinstance(v, list):
                found = v
                break
            if isinstance(v, dict):
                arr = v
        if found is None:
            for k in ("list", "items", "records", "rows"):
                if isinstance(arr.get(k), list):
                    found = arr[k]
                    break
        arr = found
    if not isinstance(arr, list):
        raise ValueError("文案源解析失败: 需要 JSON 数组格式")
    caps = _to_str_list(arr)
    if not caps:
        raise ValueError("文案源为空")
    return caps
