"""任务编排: 拉取视频源 -> 下载 -> 组装文案 -> 逐窗口发布"""
import datetime as dt
import threading
from pathlib import Path

from . import sources, store
from .bitclient import BitClient, BitBrowserError
from .doubao import scrape_doubao_chat
from .logs import add_log
from .publisher import publish_once


def _today():
    return dt.date.today().isoformat()


def resolve_targets(task, windows, configs):
    # 目标以显式勾选的窗口为准(分组仅用于前端筛选候选)
    ids = set(task.get("target_window_ids") or [])
    return [
        w for w in windows
        if w["id"] in ids and configs.get(w["id"], {}).get("enabled", False)
    ]


def build_caption(task, item, caps, state, tid):
    parts = []
    if caps:
        idx = state.setdefault("caption_idx", {})
        i = idx.get(tid, 0) % len(caps)
        idx[tid] = i + 1
        parts.append(caps[i])
    tpl = task.get("caption_template") or "{title}"
    try:
        parts.append(tpl.format(title=item["title"], date=_today()))
    except Exception:
        parts.append(tpl.replace("{title}", item["title"]))
    tags = task.get("tags") or []
    if tags:
        parts.append(" ".join("#" + str(t).lstrip("#") for t in tags))
    return "\n".join([p for p in parts if p]).strip()


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

    headers = {"User-Agent": "Mozilla/5.0"}
    dl_root = Path(settings.get("download_dir"))
    limit = int(task.get("daily_limit_per_window") or 0)
    close_override = task.get("close_after_publish")
    state = store.load_state()
    ok_cnt, fail_cnt = 0, 0

    if task.get("source_type") == "doubao_page":
        # 豆包模式: 每个目标窗口各自打开自己的聊天页链接, 抓各自的最新视频并发布到自己的抖音
        headers["Referer"] = "https://www.doubao.com/"
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
            done_w = (
                state.setdefault("downloaded_by_win", {})
                .setdefault(task_id, {})
                .setdefault(w["id"], [])
            )
            fresh = [u for u in vids if u not in done_w]
            if not fresh:
                add_log(f"[{name}] 窗口[{w['name']}] 无新视频，跳过（本聊天已发布的不再重复）")
                continue
            for i, u in enumerate(fresh[:count_n]):
                item = {"title": f"豆包视频{i + 1}", "video_url": u}
                try:
                    vpath = sources.download_video(u, dl_root / task_id / w["id"], headers)
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
                caption = build_caption(task, item, caps, state, f"{task_id}|{w['id']}")
                eff_settings = dict(settings)
                if close_override is not None:
                    eff_settings["close_window_after_publish"] = bool(close_override)
                rec = {
                    "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "task_id": task_id,
                    "task_name": name,
                    "window_id": w["id"],
                    "window_name": w["name"],
                    "source_url": src,
                    "video": u,
                    "caption": caption,
                }
                try:
                    publish_once(bit, eff_settings, w, vpath, caption)
                    done_w.append(u)
                    used += 1
                    state["daily"][dkey] = used
                    rec["ok"] = True
                    ok_cnt += 1
                except Exception as e:
                    rec["ok"] = False
                    rec["error"] = str(e)[:300]
                    fail_cnt += 1
                    add_log(f"[{name}] 窗口[{w['name']}] 发布失败: {e}", "error")
                finally:
                    state.setdefault("history", []).append(rec)
                    store.save_state(state)
        add_log(f"[{name}] 本轮结束: 成功 {ok_cnt} 个窗口视频, 失败 {fail_cnt} 次")
        return

    # JSON 接口模式: 单次抓取 -> 各窗口发布
    caps = []
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
            msg = "返回内容不是JSON。若填写的是网页地址(如豆包聊天页)，请把【源类型】切换为豆包聊天页"
        add_log(f"[{name}] 视频源抓取失败: {msg}", "error")
        return

    if not items:
        add_log(f"[{name}] 视频源中没有可用视频", "error")
        return

    done = set(state.setdefault("downloaded", {}).setdefault(task_id, []))
    fresh = [it for it in items if it["video_url"] not in done]
    if not fresh:
        add_log(f"[{name}] 无新视频，跳过（共 {len(items)} 个已发布/已下载）")
        return
    count = max(1, int(task.get("fetch_count") or 1))
    batch = fresh[:count]

    if task.get("captions_url"):
        try:
            caps = sources.fetch_captions(task["captions_url"], headers)
            add_log(f"[{name}] 已抓取文案 {len(caps)} 条")
        except Exception as e:
            add_log(f"[{name}] 文案抓取失败，将使用模板: {e}", "error")

    for w in targets:
        dkey = f"{w['id']}|{_today()}"
        used = state.setdefault("daily", {}).get(dkey, 0)
        pending = list(batch)
        while pending:
            if limit > 0 and used >= limit:
                add_log(f"[{name}] 窗口[{w['name']}] 今日已达上限({limit})，跳过剩余视频")
                break
            item = pending.pop(0)
            try:
                vpath = sources.download_video(item["video_url"], dl_root / task_id, headers)
            except Exception as e:
                add_log(f"[{name}] 视频下载失败，跳过: {e}", "error")
                done.add(item["video_url"])
                continue
            ok, reason = sources.validate_mp4(vpath)
            if not ok:
                add_log(f"[{name}] 下载内容不是有效视频({reason})，跳过发布并移除: {vpath.name}", "error")
                try:
                    vpath.unlink(missing_ok=True)
                except Exception:
                    pass
                done.add(item["video_url"])
                continue
            caption = build_caption(task, item, caps, state, task_id)
            eff_settings = dict(settings)
            if close_override is not None:
                eff_settings["close_window_after_publish"] = bool(close_override)
            rec = {
                "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "task_id": task_id,
                "task_name": name,
                "window_id": w["id"],
                "window_name": w["name"],
                "video": item["video_url"],
                "caption": caption,
            }
            try:
                publish_once(bit, eff_settings, w, vpath, caption)
                done.add(item["video_url"])
                used += 1
                state["daily"][dkey] = used
                rec["ok"] = True
                ok_cnt += 1
            except Exception as e:
                rec["ok"] = False
                rec["error"] = str(e)[:300]
                fail_cnt += 1
                add_log(f"[{name}] 窗口[{w['name']}] 发布失败: {e}", "error")
            finally:
                state.setdefault("history", []).append(rec)
                state["downloaded"][task_id] = sorted(done)
                store.save_state(state)

    add_log(f"[{name}] 本轮结束: 成功 {ok_cnt} 个窗口视频, 失败 {fail_cnt} 次")


def safe_run(task_id, only_window_id=None):
    try:
        run_task(task_id, only_window_id)
    except Exception as e:
        add_log(f"任务执行异常: {e}", "error")


def run_async(task_id, only_window_id=None):
    threading.Thread(target=safe_run, args=(task_id, only_window_id), daemon=True).start()
