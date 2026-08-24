"""任务编排: 内容源抓取 -> AI整理发布内容 -> 多平台发布"""
import datetime as dt
import threading
from pathlib import Path

from . import platforms, sources, store
from .bitclient import BitClient, BitBrowserError
from .doubao import scrape_doubao_chat
from .llm import refine_content
from .logs import add_log


def _today():
    return dt.date.today().isoformat()


def resolve_targets(task, windows, configs):
    # 目标以显式勾选的窗口为准(分组仅用于前端筛选候选)
    ids = set(task.get("target_window_ids") or [])
    return [
        w for w in windows
        if w["id"] in ids and configs.get(w["id"], {}).get("enabled", False)
    ]


def _fallback_content(material):
    """AI不可用时兜底: 取材料中最长的一段文字作为描述"""
    first = ""
    for ln in (material or "").splitlines():
        s = ln.strip()
        if len(s) > len(first):
            first = s
    return {
        "title": first[:20] or "今日分享",
        "description": first[:240],
        "tags": [],
    }


def _pick_model(task):
    mid = (task.get("ai_model_id") or "").strip()
    if not mid:
        add_log("任务未选择AI模型，将使用原始文案兜底", "warning")
        return None
    m = store.get_model(mid)
    if not m:
        add_log(f"AI模型不存在({mid})，将使用原始文案兜底", "warning")
        return None
    if not m.get("enabled", True):
        add_log(f"AI模型[{m['name']}]已停用，将使用原始文案兜底", "warning")
        return None
    if not (m.get("api_key") or "").strip():
        add_log(f"AI模型[{m['name']}]未配置API Key，将使用原始文案兜底", "warning")
        return None
    return m


def prepare_content(task, settings, material, win_name):
    """材料 -> 结构化发布内容 {title, description, tags}; AI失败时原文兜底"""
    model = _pick_model(task)
    if not model:
        return _fallback_content(material)
    try:
        content = refine_content(
            model,
            task.get("platform") or "douyin",
            material,
            (task.get("ai_prompt") or "").strip(),
        )
    except Exception as e:
        add_log(f"[{win_name}] AI整理失败({e})，使用原始文案兜底", "warning")
        return _fallback_content(material)
    add_log(f"[{win_name}] AI整理完成: {content['title']} | 标签{len(content['tags'])}个")
    return content


def _record(state, **kw):
    rec = {"time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **kw}
    state.setdefault("history", []).append(rec)
    return rec


def _finish_rec(state, rec, ok, err=None):
    rec["ok"] = ok
    if err:
        rec["error"] = str(err)[:300]
    store.save_state(state)


def run_task(task_id, only_window_id=None):
    task = store.get_task(task_id)
    if not task:
        add_log(f"任务不存在: {task_id}", "error")
        return
    name = task.get("name") or task_id
    add_log(f"[{name}] 开始执行任务" + ("（手动指定窗口）" if only_window_id else ""))

    settings = store.load_settings()
    bit = BitClient(settings)
    try:
        if not bit.healthy():
            if settings.get("auto_start_bitbrowser", True):
                bit.ensure_running(settings.get("bitbrowser_path"))
            else:
                raise BitBrowserError("比特浏览器未运行，且未开启自动启动")
        windows = bit.list_windows()
        gmap = {g["id"]: g["name"] for g in bit.list_groups()}
        for w in windows:
            w["group_name"] = w.get("group_name") or gmap.get(w["group_id"], "默认分组")
    except Exception as e:
        add_log(f"[{name}] 比特浏览器不可用: {e}", "error")
        return

    configs = store.load_window_configs()
    targets = [w for w in resolve_targets(task, windows, configs) if (not only_window_id or w["id"] == only_window_id)]
    if not targets:
        add_log(f"[{name}] 没有可执行的目标窗口（请先在【窗口管理】中开启抓取并绑定该任务）", "error")
        return

    # AI提问任务: 独立流程(打开对话框URL发送提示词)
    if task.get("task_type") == "ai_ask":
        run_ai_ask(task, bit, settings, targets)
        return

    headers = {"User-Agent": "Mozilla/5.0"}
    dl_root = Path(settings.get("download_dir"))
    limit = int(task.get("daily_limit_per_window") or 0)
    close_override = task.get("close_after_publish")
    state = store.load_state()

    def eff():
        s = dict(settings)
        if close_override is not None:
            s["close_window_after_publish"] = bool(close_override)
        return s

    ok_cnt, fail_cnt = 0, 0

    if task.get("source_type") == "doubao_page":
        # 豆包模式: 每个目标窗口各自打开自己的聊天页链接, 抓材料 -> AI整理 -> 发布到所选平台
        wvars = task.get("window_vars") or {}
        wait_s = int(task.get("source_wait") or 15)
        count_n = max(1, int(task.get("fetch_count") or 1))
        for w in targets:
            src = ((wvars.get(w["id"]) or {}).get("source_url") or "").strip()
            if not src:
                add_log(f"[{name}] 窗口[{w['name']}] 未配置聊天页链接，跳过（每个目标窗口必须有自己的链接）", "error")
                continue
            dkey = f"{w['id']}|{_today()}"
            used = state.setdefault("daily", {}).get(dkey, 0)
            if limit > 0 and used >= limit:
                add_log(f"[{name}] 窗口[{w['name']}] 今日已达上限({limit})，跳过")
                continue
            try:
                vids, caps = scrape_doubao_chat(bit, settings, src, w, wait_s)
            except Exception as e:
                add_log(f"[{name}] 窗口[{w['name']}] 抓取失败: {e}", "error")
                continue

            # 页面关键文字 -> AI整理为结构化发布内容(每窗口一次, 复用于本批视频)
            material = "\n".join(caps)
            content = prepare_content(task, settings, material, w["name"])

            done_w = (
                state.setdefault("downloaded_by_win", {})
                .setdefault(task_id, {})
                .setdefault(w["id"], [])
            )
            if task.get("allow_repeat", True):
                # 允许重复: 不做已发布过滤, 每轮取最新视频
                batch = vids[:count_n]
                if not batch:
                    add_log(f"[{name}] 窗口[{w['name']}] 未抓取到视频，跳过")
                    continue
            else:
                batch = [u for u in vids if u not in done_w]
                if not batch:
                    add_log(f"[{name}] 窗口[{w['name']}] 无新视频，跳过（本聊天已发布的不再重复，可在任务中开启「允许重复发布」）")
                    continue
            for i, u in enumerate(batch):
                try:
                    vpath = sources.download_video(
                        u, dl_root / task_id / w["id"],
                        {"User-Agent": "Mozilla/5.0", "Referer": "https://www.doubao.com/"},
                    )
                except Exception as e:
                    add_log(f"[{name}] 窗口[{w['name']}] 视频下载失败，跳过: {e}", "error")
                    done_w.append(u)
                    continue
                ok, reason = sources.validate_mp4(vpath)
                if not ok:
                    add_log(
                        f"[{name}] 窗口[{w['name']}] 下载内容不是有效视频({reason})，跳过发布并移除",
                        "error",
                    )
                    try:
                        vpath.unlink(missing_ok=True)
                    except Exception:
                        pass
                    done_w.append(u)
                    continue
                rec = _record(
                    state,
                    task_id=task_id, task_name=name,
                    window_id=w["id"], window_name=w["name"],
                    source_url=src, video=u,
                    content=dict(content),
                    platform=task.get("platform"),
                )
                try:
                    platforms.publish(task.get("platform"), bit, eff(), w, vpath, content)
                    done_w.append(u)
                    used += 1
                    state["daily"][dkey] = used
                    _finish_rec(state, rec, True)
                    ok_cnt += 1
                except Exception as e:
                    _finish_rec(state, rec, False, e)
                    fail_cnt += 1
                    add_log(f"[{name}] 窗口[{w['name']}] 发布失败: {e}", "error")

    else:
        # JSON 接口模式: 单次抓取 -> 各窗口发布(标题作为AI材料)
        try:
            payload = sources.fetch_json(task["source_url"], headers)
            items = sources.extract_items(
                payload,
                task.get("items_path", "data"),
                task.get("field_title", "title"),
                task.get("field_video", "video_url"),
            )
        except ValueError as e:
            add_log(f"[{name}] 视频源解析失败: {e}", "error")
            return
        except Exception as e:
            msg = str(e)
            if "Expecting value" in msg or "json" in msg.lower():
                msg = "返回内容不是JSON。若填写的是网页地址(如豆包聊天页)，请把【内容源】切换为豆包聊天页"
            add_log(f"[{name}] 视频源抓取失败: {msg}", "error")
            return

        if not items:
            add_log(f"[{name}] 视频源中没有可用视频", "error")
            return

        done = set(state.setdefault("downloaded", {}).setdefault(task_id, []))
        count = max(1, int(task.get("fetch_count") or 1))
        if task.get("allow_repeat", True):
            batch = items[:count]
        else:
            batch = [it for it in items if it["video_url"] not in done]
            if not batch:
                add_log(f"[{name}] 无新视频，跳过（共 {len(items)} 个已发布/已下载，可在任务中开启「允许重复发布」）")
                return

        for it in batch:
            content = prepare_content(task, settings, it.get("title") or "", name)
            for w in targets:
                dkey = f"{w['id']}|{_today()}"
                used = state.setdefault("daily", {}).get(dkey, 0)
                if limit > 0 and used >= limit:
                    add_log(f"[{name}] 窗口[{w['name']}] 今日已达上限({limit})，跳过剩余视频")
                    break
                try:
                    vpath = sources.download_video(it["video_url"], dl_root / task_id, headers)
                except Exception as e:
                    add_log(f"[{name}] 视频下载失败，跳过: {e}", "error")
                    done.add(it["video_url"])
                    continue
                ok, reason = sources.validate_mp4(vpath)
                if not ok:
                    add_log(f"[{name}] 下载内容不是有效视频({reason})，跳过发布并移除: {vpath.name}", "error")
                    try:
                        vpath.unlink(missing_ok=True)
                    except Exception:
                        pass
                    done.add(it["video_url"])
                    continue
                rec = _record(
                    state,
                    task_id=task_id, task_name=name,
                    window_id=w["id"], window_name=w["name"],
                    video=it["video_url"],
                    content=dict(content),
                    platform=task.get("platform"),
                )
                try:
                    platforms.publish(task.get("platform"), bit, eff(), w, vpath, content)
                    done.add(it["video_url"])
                    used += 1
                    state["daily"][dkey] = used
                    _finish_rec(state, rec, True)
                    ok_cnt += 1
                except Exception as e:
                    _finish_rec(state, rec, False, e)
                    fail_cnt += 1
                    add_log(f"[{name}] 窗口[{w['name']}] 发布失败: {e}", "error")
            state["downloaded"][task_id] = sorted(done)
            store.save_state(state)

    add_log(f"[{name}] 本轮结束: 成功 {ok_cnt} 个视频, 失败 {fail_cnt} 次")


def run_ai_ask(task, bit, settings, targets):
    """AI提问任务: 逐窗口打开各自的对话框URL发送提示词(可按日限次, AI判断今日已问次数)"""
    import datetime as dt

    from .asker import ask_in_chat, read_chat_text
    from .llm import count_replies_on_date

    name = task.get("name") or task.get("id")
    prompt = (task.get("prompt_text") or "").strip()
    if not prompt:
        add_log(f"[{name}] 未配置提示词，任务结束", "error")
        return
    platform = task.get("ask_platform") or "doubao"
    wvars = task.get("window_vars") or {}
    wait_s = int(task.get("ask_wait") or 30)
    limit = int(task.get("ask_daily_limit") or 0)
    model = None
    if limit > 0:
        model = _pick_model(task)
        if not model:
            add_log(f"[{name}] 已设每日提问上限({limit})但无可用推理模型，本次不做次数限制", "warning")
    today = dt.date.today().isoformat()
    ok_cnt, fail_cnt = 0, 0
    add_log(
        f"[{name}] AI提问模式: 平台[{platform}], 目标 {len(targets)} 个窗口"
        + (f", 每日上限 {limit} 次/窗口" if limit > 0 else "")
    )
    for w in targets:
        src = ((wvars.get(w["id"]) or {}).get("source_url") or "").strip()
        if not src:
            add_log(f"[{name}] 窗口[{w['name']}] 未配置对话框URL，跳过（每个目标窗口必须有自己的链接）", "error")
            continue
        if limit > 0 and model:
            # 发送前打开对话页取文本, 由推理模型判断今日已提问次数
            try:
                page_text = read_chat_text(bit, src, w)
            except Exception as e:
                add_log(f"[{name}] 窗口[{w['name']}] 读取对话页失败({e})，本次不做次数检查", "warning")
                page_text = ""
            asked = 0
            if page_text:
                try:
                    asked = count_replies_on_date(model, page_text, today)
                except Exception as e:
                    add_log(f"[{name}] 窗口[{w['name']}] AI计数失败({e})，按0次处理", "warning")
            add_log(f"[{name}] 窗口[{w['name']}] 今日已提问 {asked}/{limit} 次")
            if asked >= limit:
                add_log(f"[{name}] 窗口[{w['name']}] 今日已达提问上限({limit})，跳过")
                continue
        try:
            ask_in_chat(bit, settings, src, w, prompt, wait_s)
            ok_cnt += 1
        except Exception as e:
            fail_cnt += 1
            add_log(f"[{name}] 窗口[{w['name']}] 提问失败: {e}", "error")
    add_log(f"[{name}] 本轮结束: 成功提问 {ok_cnt} 个窗口, 失败 {fail_cnt} 次")


def safe_run(task_id, only_window_id=None):
    try:
        run_task(task_id, only_window_id)
    except Exception as e:
        add_log(f"任务执行异常: {e}", "error")


def run_async(task_id, only_window_id=None):
    threading.Thread(target=safe_run, args=(task_id, only_window_id), daemon=True).start()
