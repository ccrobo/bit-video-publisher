"""基于 JSON 文件的持久化存储: 设置 / 任务 / 窗口配置 / AI模型 / 提示词库 / 运行状态"""
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .llm import DEFAULT_PROMPT

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
    "default_ai_prompt": DEFAULT_PROMPT,
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
    "task_type": "video_publish",  # video_publish=抓视频发布 | ai_ask=到对话框自动提问
    "platform": "douyin",
    "enabled": False,
    "cron": "0 9 * * *",
    "ask_platform": "doubao",  # ai_ask: 提问平台
    "prompt_text": "",  # ai_ask: 要发送的提示词
    "ask_wait": 30,  # ai_ask: 发送后等待生成秒数
    "ask_daily_limit": 1,  # ai_ask: 每窗口每日提问上限, 0=不限制(AI根据回复判断今日已问次数)
    "ask_vars": [],  # ai_ask: 提示词中补充的变量开关: current_time/current_date/window_id/window_name
    "ask_video_mode": False,  # ai_ask: 豆包平台: 注入前切换到"视频生成"模式
    "source_type": "json",
    "source_url": "",
    "source_window_id": "",
    "source_wait": 15,
    "items_path": "data",
    "field_title": "title",
    "field_video": "video_url",
    "fetch_count": 1,
    "ai_model_id": "",
    "ai_prompt": "",
    "allow_repeat": True,
    "target_group_ids": [],
    "target_window_ids": [],
    "window_vars": {},
    "url_var": "",  # ai_ask: 引用的窗口变量名(如"对话框URL"), 设置后按变量自动圈定目标窗口并取各窗口URL
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


# ---------------- AI 模型 ----------------

MODEL_PRESETS = [
    {"id": "deepseek", "name": "DeepSeek", "provider": "deepseek",
     "api_base": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"id": "hunyuan", "name": "腾讯混元(HY)", "provider": "hunyuan",
     "api_base": "https://api.hunyuan.cloud.tencent.com/v1", "model": "hunyuan-turbos-latest"},
    {"id": "glm", "name": "智谱 GLM", "provider": "zhipu",
     "api_base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-plus"},
    {"id": "kimi", "name": "Kimi(Moonshot)", "provider": "moonshot",
     "api_base": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"id": "minimax", "name": "MiniMax", "provider": "minimax",
     "api_base": "https://api.minimax.chat/v1", "model": "abab6.5s-chat"},
    {"id": "qwen", "name": "通义千问 Qwen", "provider": "dashscope",
     "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
]


def list_models():
    """AI模型配置列表; 首次访问时种入内置预置(仅名称/接口地址, Key留待用户填写)"""
    ms = _read("ai_models.json", [])
    if not ms:
        ms = [dict(m, api_key="", enabled=True, builtin=True) for m in MODEL_PRESETS]
        _write("ai_models.json", ms)
    return ms


def save_models(ms):
    _write("ai_models.json", ms)


def get_model(mid):
    for m in list_models():
        if m.get("id") == mid:
            return copy.deepcopy(m)
    return None


def upsert_model(patch):
    ms = list_models()
    mid = patch.get("id") or ""
    for i, m in enumerate(ms):
        if m.get("id") == mid:
            ms[i].update({k: v for k, v in patch.items() if k != "id"})
            save_models(ms)
            return copy.deepcopy(ms[i])
    m = {k: v for k, v in MODEL_DEFAULTS.items()}
    m.update(patch or {})
    m["id"] = patch.get("id") or new_id()
    m["builtin"] = False
    ms.append(m)
    save_models(ms)
    return copy.deepcopy(m)


MODEL_DEFAULTS = {
    "name": "",
    "provider": "custom",
    "api_base": "",
    "model": "",
    "api_key": "",
    "enabled": True,
}


def delete_model(mid):
    ms = [m for m in list_models() if m.get("id") != mid]
    save_models(ms)
    return True


# ---------------- 提示词库 ----------------

def list_prompts():
    return _read("prompts.json", [])


def get_prompt(pid):
    for p in list_prompts():
        if p.get("id") == pid:
            return copy.deepcopy(p)
    return None


def upsert_prompt(patch):
    ps = list_prompts()
    pid = patch.get("id") or ""
    for i, p in enumerate(ps):
        if p.get("id") == pid:
            ps[i].update({k: v for k, v in patch.items() if k != "id"})
            _write("prompts.json", ps)
            return copy.deepcopy(ps[i])
    p = {"name": "", "category": "", "content": ""}
    p.update({k: v for k, v in (patch or {}).items() if k != "id"})
    p["id"] = new_id()
    p["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ps.append(p)
    _write("prompts.json", ps)
    return copy.deepcopy(p)


def delete_prompt(pid):
    ps = [p for p in list_prompts() if p.get("id") != pid]
    _write("prompts.json", ps)
    return True


# ---------------- window configs ----------------

def load_window_configs():
    return _read("window_configs.json", {})


def save_window_configs(cfgs):
    _write("window_configs.json", cfgs)


# ---------------- 窗口变量(全局复用) ----------------
# 结构: {window_id: {变量名: 值}}; 例如每个窗口配置"对话框URL"后, AI提问任务直接引用该变量

def load_win_vars():
    return _read("win_vars.json", {})


def set_win_var(window_id, key, value):
    """设置/删除(空值即删除)某窗口的一个变量; 删除最后一个变量时清理窗口条目"""
    if not window_id or not key:
        return load_win_vars()
    d = load_win_vars()
    wv = dict(d.get(window_id) or {})
    v = str(value if value is not None else "").strip()
    if v:
        wv[key] = v
    else:
        wv.pop(key, None)
    if wv:
        d[window_id] = wv
    else:
        d.pop(window_id, None)
    save_win_vars(d)
    return d


def set_window_vars(window_id, vars_map):
    """整表替换某窗口的全部变量(键值均去首尾空白, 空值/空键忽略); 全空则清理窗口条目"""
    if not window_id:
        return load_win_vars()
    d = load_win_vars()
    clean = {}
    for k, v in (vars_map or {}).items():
        k = str(k).strip()
        v = str(v if v is not None else "").strip()
        if k and v:
            clean[k] = v
    if clean:
        d[window_id] = clean
    else:
        d.pop(window_id, None)
    save_win_vars(d)
    return d


def save_win_vars(d):
    _write("win_vars.json", d)


# ---------------- 变量定义(公共) ----------------
# 结构: [{"key": 变量名, "note": 说明}, ...]; 窗口配置时只能从这些key中选择并填值

def load_var_defs():
    return _read("var_defs.json", [])


def save_var_defs(defs):
    _write("var_defs.json", defs)


def upsert_var_def(key, note="", new_key=None):
    """新增/更新变量定义; 重命名时同步迁移各窗口已配置的同名键。返回 (defs, 错误信息)"""
    key = str(key or "").strip()
    target = str(new_key or key or "").strip()
    if not key or not target:
        return load_var_defs(), "变量名不能为空"
    defs = load_var_defs()
    if new_key:
        # 编辑模式: 目标名与其他定义冲突则报错
        if target != key and any(d.get("key") == target for d in defs):
            return defs, f"变量[{target}]已存在"
    else:
        # 新增模式: key 已存在报错
        if any(d.get("key") == key for d in defs):
            return defs, f"变量[{key}]已存在"
    found = False
    for d in defs:
        if d.get("key") == key:
            d["key"] = target
            d["note"] = str(note or "").strip()
            found = True
            break
    if not found:
        defs.append({"key": target, "note": str(note or "").strip()})
        defs.sort(key=lambda x: x.get("key", ""))
    save_var_defs(defs)
    if target != key:
        wv = load_win_vars()
        changed = False
        for m in wv.values():
            if key in m:
                m[target] = m.pop(key)
                changed = True
        if changed:
            save_win_vars(wv)
    return defs, ""


def delete_var_def(key):
    """删除变量定义(不影响各窗口已配置的值)"""
    key = str(key or "").strip()
    defs = [d for d in load_var_defs() if d.get("key") != key]
    save_var_defs(defs)
    return defs


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
