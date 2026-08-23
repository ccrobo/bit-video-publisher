"""基于 JSON 文件的持久化存储: 设置 / 任务 / 窗口配置 / 运行状态"""
import copy
import json
import os
import threading
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"

_lock = threading.RLock()

_OLD_DEFAULT_PATH = r"C:\Program Files\BitBrowser\比特浏览器.exe"


def default_bitbrowser_path():
    """比特浏览器默认安装路径: %LOCALAPPDATA%\\Programs\\bitbrowser\\比特浏览器.exe"""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return str(Path(local) / "Programs" / "bitbrowser" / "比特浏览器.exe")
    return r"C:\Program Files\BitBrowser\比特浏览器.exe"


DEFAULT_SETTINGS = {
    "bitbrowser_api": "http://127.0.0.1:54345",
    "bitbrowser_path": default_bitbrowser_path(),
    "bitbrowser_api_key": "",
    "auto_start_bitbrowser": True,
    "download_dir": str(BASE_DIR / "downloads"),
    "upload_url": "https://creator.douyin.com/creator-micro/content/upload",
    "publish_timeout": 180,
    "close_window_after_publish": True,
    "selectors": {
        "file_input": "input[type=file]",
        "editor": "div[contenteditable=true]",
        "publish_button": '//button[normalize-space(.)="发布"]',
    },
}


def new_id():
    return uuid.uuid4().hex[:12]


def _path(name):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / name


def _read(name, default):
    with _lock:
        p = _path(name)
        if not p.exists():
            return copy.deepcopy(default)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return copy.deepcopy(default)


def _write(name, obj):
    with _lock:
        p = _path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)


# ---------------- settings ----------------

def load_settings():
    s = _read("settings.json", {})
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in s.items() if k != "selectors"})
    merged["selectors"] = {**DEFAULT_SETTINGS["selectors"], **(s.get("selectors") or {})}
    # 旧默认路径或空路径时自动纠正为当前默认安装位置
    if not merged.get("bitbrowser_path") or merged["bitbrowser_path"] == _OLD_DEFAULT_PATH:
        merged["bitbrowser_path"] = default_bitbrowser_path()
    return merged


def save_settings(patch):
    s = _read("settings.json", {})
    sel_patch = patch.pop("selectors", None) or {}
    s.update(patch)
    s.setdefault("selectors", {})
    s["selectors"].update(sel_patch)
    _write("settings.json", s)
    return load_settings()


# ---------------- tasks ----------------

TASK_DEFAULTS = {
    "name": "",
    "platform": "douyin",
    "enabled": False,
    "cron": "0 9 * * *",
    "source_type": "json",
    "source_url": "",
    "source_window_id": "",
    "source_wait": 15,
    "items_path": "data",
    "field_title": "title",
    "field_video": "video_url",
    "fetch_count": 1,
    "captions_url": "",
    "caption_template": "{title}",
    "tags": [],
    "target_group_ids": [],
    "target_window_ids": [],
    "window_vars": {},
    "daily_limit_per_window": 1,
    "close_after_publish": None,
}


def list_tasks():
    return _read("tasks.json", [])


def save_tasks(tasks):
    _write("tasks.json", tasks)


def get_task(tid):
    for t in list_tasks():
        if t.get("id") == tid:
            return copy.deepcopy(t)
    return None


def create_task(patch):
    tasks = list_tasks()
    task = copy.deepcopy(TASK_DEFAULTS)
    task.update(patch or {})
    task["id"] = new_id()
    task["created_at"] = uuid.uuid4().hex[:0] or __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    tasks.append(task)
    save_tasks(tasks)
    return task


def update_task(tid, patch):
    tasks = list_tasks()
    for i, t in enumerate(tasks):
        if t.get("id") == tid:
            patch = {k: v for k, v in (patch or {}).items() if k != "id"}
            tasks[i].update(patch)
            save_tasks(tasks)
            return copy.deepcopy(tasks[i])
    return None


def delete_task(tid):
    tasks = [t for t in list_tasks() if t.get("id") != tid]
    save_tasks(tasks)
    return True


# ---------------- window configs ----------------

def load_window_configs():
    return _read("window_configs.json", {})


def save_window_configs(cfgs):
    _write("window_configs.json", cfgs)


def upsert_window_config(window_id, patch):
    cfgs = load_window_configs()
    c = cfgs.get(window_id) or {"enabled": False, "task_ids": [], "note": ""}
    c.update(patch or {})
    cfgs[window_id] = c
    save_window_configs(cfgs)
    return c


def bulk_configure(window_ids, enabled=None, task_mode=None, task_ids=None):
    cfgs = load_window_configs()
    for wid in window_ids:
        c = cfgs.get(wid) or {"enabled": False, "task_ids": [], "note": ""}
        if enabled is not None:
            c["enabled"] = bool(enabled)
        if task_mode == "overwrite" and task_ids is not None:
            c["task_ids"] = list(dict.fromkeys(task_ids))
        elif task_mode == "append" and task_ids:
            c["task_ids"] = list(dict.fromkeys(list(c.get("task_ids") or []) + list(task_ids)))
        cfgs[wid] = c
    save_window_configs(cfgs)
    return cfgs


# ---------------- runtime state ----------------

def load_state():
    return _read(
        "state.json",
        {"downloaded": {}, "daily": {}, "caption_idx": {}, "history": []},
    )


def save_state(state):
    hist = state.get("history") or []
    state["history"] = hist[-500:]
    _write("state.json", state)
