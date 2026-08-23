"""FastAPI 主应用: 提供 REST API 与前端页面"""
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import scheduler as scheduler_mod
from . import store
from .bitclient import BitBrowserError, BitClient
from .llm import LLMError, test_model
from .logs import add_log, get_logs, last_id
from .task_runner import run_async


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_targets()
    add_log("服务已启动，访问 http://127.0.0.1:8799/ 打开控制台")
    scheduler_mod.start()
    yield
    scheduler_mod.shutdown()


def _migrate_legacy_targets():
    """旧任务只配了分组没勾窗口:把已填聊天页链接的窗口+组内成员一次性固化到显式目标。"""
    try:
        tasks = store.list_tasks()
        wins = store._read("windows", [])
        changed = False
        for t in tasks:
            if t.get("target_window_ids"):
                continue
            linked = [
                wid for wid, v in (t.get("window_vars") or {}).items()
                if isinstance(v, dict) and (v.get("source_url") or "").strip()
            ]
            gids = set(t.get("target_group_ids") or [])
            members = [w["id"] for w in wins if w.get("group_id") in gids]
            ids = list(dict.fromkeys(linked + members))
            if ids:
                t["target_window_ids"] = ids
                changed = True
                add_log(f"任务[{t.get('name')}] 已迁移 {len(ids)} 个显式目标窗口")
        if changed:
            store.save_tasks(tasks)
    except Exception as e:
        add_log(f"目标迁移失败(忽略): {e}")


app = FastAPI(title="Bit Video Publisher", lifespan=lifespan)


def _bit() -> BitClient:
    return BitClient(store.load_settings())


def _ensure_bit(bit: BitClient):
    s = store.load_settings()
    if not bit.healthy():
        if s.get("auto_start_bitbrowser", True):
            bit.ensure_running(s.get("bitbrowser_path"))
        else:
            raise HTTPException(400, "比特浏览器未运行，且未开启【自动启动比特浏览器】")


# ---------------- 状态 / 比特浏览器 ----------------

@app.get("/api/status")
def status():
    s = store.load_settings()
    bit = _bit()
    tasks = store.list_tasks()
    return {
        "bit_running": bit.healthy(),
        "auto_start": bool(s.get("auto_start_bitbrowser", True)),
        "task_total": len(tasks),
        "task_enabled": sum(1 for t in tasks if t.get("enabled")),
        "upload_url": s.get("upload_url"),
    }


@app.post("/api/bit/start")
def bit_start():
    try:
        s = store.load_settings()
        bit = _bit()
        if not bit.healthy():
            bit.ensure_running(s.get("bitbrowser_path"))
        return {"ok": True, "running": bit.healthy()}
    except (BitBrowserError, HTTPException) as e:
        raise HTTPException(400, str(e))


@app.post("/api/bit/sync")
def bit_sync():
    bit = _bit()
    try:
        _ensure_bit(bit)
        groups = bit.list_groups()
        windows = bit.list_windows()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"同步失败: {e}")
    gmap = {g["id"]: g["name"] for g in groups}
    cfgs = store.load_window_configs()
    for w in windows:
        w["group_name"] = w.get("group_name") or gmap.get(w["group_id"], "默认分组")
        w["cfg"] = cfgs.get(w["id"]) or {"enabled": False, "task_ids": [], "note": ""}
    add_log(f"已同步比特浏览器: {len(groups)} 个分组, {len(windows)} 个窗口")
    return {"groups": groups, "windows": windows}


@app.get("/api/groups")
def groups():
    try:
        return {"groups": _bit().list_groups()}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/windows/{wid}/open")
def open_window(wid: str):
    try:
        addr = _bit().open_window(wid)
        return {"ok": True, "debug_addr": addr}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/windows/{wid}/close")
def close_window(wid: str):
    try:
        _bit().close_window(wid)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------------- 设置 ----------------

@app.get("/api/settings")
def get_settings():
    return store.load_settings()


@app.put("/api/settings")
def put_settings(patch: dict = Body(...)):
    patch = {k: v for k, v in patch.items() if k in store.DEFAULT_SETTINGS}
    if not isinstance(patch.get("selectors", {}), dict):
        raise HTTPException(400, "selectors 格式错误")
    s = store.save_settings(patch)
    add_log("系统设置已更新")
    return s


# ---------------- 任务 ----------------

TASK_EDITABLE = set(store.TASK_DEFAULTS.keys())


def _clean_task(patch: dict) -> dict:
    data = {k: v for k, v in (patch or {}).items() if k in TASK_EDITABLE}
    if "fetch_count" in data:
        data["fetch_count"] = max(1, int(data["fetch_count"] or 1))
    if "daily_limit_per_window" in data:
        data["daily_limit_per_window"] = max(0, int(data["daily_limit_per_window"] or 0))
    if "window_vars" in data:
        wv = {}
        if isinstance(data["window_vars"], dict):
            for wid, var in data["window_vars"].items():
                if isinstance(var, dict):
                    url = str(var.get("source_url") or "").strip()
                    remark = str(var.get("remark") or "").strip()
                    if url or remark:
                        entry = {}
                        if url:
                            entry["source_url"] = url
                        if remark:
                            entry["remark"] = remark
                        wv[str(wid)] = entry
        data["window_vars"] = wv
    return data


@app.get("/api/tasks")
def get_tasks():
    return {"tasks": store.list_tasks()}


@app.post("/api/tasks")
def post_task(patch: dict = Body(...)):
    data = _clean_task(patch)
    if not data.get("name"):
        raise HTTPException(400, "任务名称不能为空")
    if data.get("source_type") == "doubao_page":
        if not any(v.get("source_url") for v in (data.get("window_vars") or {}).values()):
            raise HTTPException(400, "豆包模式需至少为一个目标窗口配置聊天页链接")
    elif not data.get("source_url"):
        raise HTTPException(400, "视频源URL不能为空")
    task = store.create_task(data)
    scheduler_mod.reload_jobs()
    add_log(f"任务[{task['name']}] 已创建")
    return task


@app.put("/api/tasks/{tid}")
def put_task(tid: str, patch: dict = Body(...)):
    data = _clean_task(patch)
    task = store.update_task(tid, data)
    if not task:
        raise HTTPException(404, "任务不存在")
    scheduler_mod.reload_jobs()
    add_log(f"任务[{task['name']}] 已更新")
    return task


@app.delete("/api/tasks/{tid}")
def del_task(tid: str):
    t = store.get_task(tid)
    store.delete_task(tid)
    scheduler_mod.reload_jobs()
    if t:
        add_log(f"任务[{t['name']}] 已删除")
    return {"ok": True}


@app.post("/api/tasks/{tid}/toggle")
def toggle_task(tid: str, body: dict = Body(...)):
    enabled = bool(body.get("enabled"))
    task = store.update_task(tid, {"enabled": enabled})
    if not task:
        raise HTTPException(404, "任务不存在")
    scheduler_mod.reload_jobs()
    add_log(f"任务[{task['name']}] 已{'启用' if enabled else '停用'}")
    return task


@app.post("/api/tasks/{tid}/run")
def run_task_now(tid: str, body: Optional[dict] = Body(default=None)):
    if not store.get_task(tid):
        raise HTTPException(404, "任务不存在")
    only_window = (body or {}).get("window_id")
    run_async(tid, only_window)
    return {"ok": True, "msg": "已在后台开始执行，请到【运行日志】查看进展"}


# ---------------- AI 模型 ----------------

MODEL_EDITABLE = {"name", "provider", "api_base", "model", "api_key", "enabled"}


def _mask_model(m: dict) -> dict:
    """API Key 不回传明文, 仅返回是否已配置"""
    out = {k: v for k, v in m.items() if k != "api_key"}
    out["has_key"] = bool((m.get("api_key") or "").strip())
    return out


@app.get("/api/models")
def get_models():
    return {"models": [_mask_model(m) for m in store.list_models()]}


@app.post("/api/models")
def add_model(body: dict = Body(...)):
    data = {k: body.get(k) for k in MODEL_EDITABLE if k in body}
    if not (data.get("name") or "").strip():
        raise HTTPException(400, "模型名称不能为空")
    if not (data.get("api_base") or "").strip() or not (data.get("model") or "").strip():
        raise HTTPException(400, "接口地址与模型名称不能为空")
    data["api_key"] = str(data.get("api_key") or "").strip()
    m = store.upsert_model(data)
    add_log(f"已添加AI模型[{m['name']}]")
    return _mask_model(m)


@app.put("/api/models/{mid}")
def update_model(mid: str, body: dict = Body(...)):
    cur = store.get_model(mid)
    if not cur:
        raise HTTPException(404, "模型不存在")
    data = {k: v for k, v in body.items() if k in MODEL_EDITABLE and k != "id"}
    # api_key 语义: 字段缺失=保持不变; 空串=清除; 非空=更新
    if "api_key" in data:
        data["api_key"] = str(data.get("api_key") or "").strip()
    m = store.upsert_model({"id": mid, **data})
    add_log(f"AI模型[{m['name']}] 已更新" + ("，API Key 已变更" if "api_key" in data else ""))
    return _mask_model(m)


@app.delete("/api/models/{mid}")
def remove_model(mid: str):
    m = store.get_model(mid)
    store.delete_model(mid)
    if m:
        add_log(f"AI模型[{m['name']}] 已删除")
    return {"ok": True}


@app.post("/api/models/{mid}/test")
def model_test(mid: str):
    m = store.get_model(mid)
    if not m:
        raise HTTPException(404, "模型不存在")
    if not (m.get("api_key") or "").strip():
        raise HTTPException(400, "请先填写并保存该模型的 API Key")
    try:
        reply = test_model(m)
    except LLMError as e:
        add_log(f"模型[{m['name']}]联通测试失败: {e}", "error")
        raise HTTPException(400, f"联通失败: {e}")
    add_log(f"模型[{m['name']}]联通测试成功: {reply}")
    return {"ok": True, "reply": reply}


# ---------------- 窗口配置 ----------------

@app.get("/api/window-configs")
def get_window_configs():
    return {"configs": store.load_window_configs()}


@app.put("/api/window-configs")
def put_window_config(body: dict = Body(...)):
    wid = str(body.get("window_id") or "")
    if not wid:
        raise HTTPException(400, "缺少 window_id")
    patch = {}
    if "enabled" in body:
        patch["enabled"] = bool(body["enabled"])
    if "task_ids" in body:
        patch["task_ids"] = [str(x) for x in (body["task_ids"] or [])]
    if "note" in body:
        patch["note"] = str(body["note"] or "")
    c = store.upsert_window_config(wid, patch)
    add_log(f"窗口 {wid} 配置已保存: {'启用' if c['enabled'] else '停用'}, 绑定 {len(c['task_ids'])} 个任务")
    return c


@app.post("/api/batch/configure")
def batch_configure(body: dict = Body(...)):
    window_ids = [str(x) for x in (body.get("window_ids") or [])]
    if not window_ids:
        raise HTTPException(400, "请先勾选窗口")
    enabled = body.get("enabled")
    task_mode = body.get("task_mode")
    task_ids = [str(x) for x in (body.get("task_ids") or [])] or None
    if task_mode in ("append", "overwrite") and not task_ids:
        raise HTTPException(400, "请选择要绑定的任务")
    cfgs = store.bulk_configure(
        window_ids,
        None if enabled is None else bool(enabled),
        task_mode,
        task_ids,
    )
    add_log(f"批量配置完成: 影响 {len(window_ids)} 个窗口")
    return {"ok": True, "configs": cfgs}


# ---------------- 日志 ----------------

@app.get("/api/logs")
def logs(after: int = 0):
    return {"entries": get_logs(after), "last_id": last_id()}


# ---------------- 前端页面 ----------------

@app.get("/")
def index():
    f = store.WEB_DIR / "index.html"
    if not f.exists():
        raise HTTPException(500, "前端文件缺失: web/index.html")
    return FileResponse(f)
