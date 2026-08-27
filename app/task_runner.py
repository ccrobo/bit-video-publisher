"""任务编排: 内容源抓取 -> AI整理发布内容 -> 多平台发布"""
import datetime as dt
import re
import threading
import uuid
from pathlib import Path

from . import platforms, sources, store
from .bitclient import BitClient, BitBrowserError
from .doubao import scrape_doubao_chat, find_doubao_replies_by_marks
from .xiaoyunque import scrape_xiaoyunque_chat, find_xiaoyunque_replies_by_marks
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


def resolve_targets_by_var(var_key, windows, wvars=None):
    """按窗口变量解析目标: 返回所有配置了该变量的窗口(与启用状态无关, 配置即生效)。

    每个返回元素附带 _var_value=变量值; 不修改传入的 windows 元素。
    """
    wvars = store.load_win_vars() if wvars is None else (wvars or {})
    out = []
    for w in windows:
        v = ((wvars.get(w["id"]) or {}).get(var_key) or "").strip()
        if v:
            ww = dict(w)
            ww["_var_value"] = v
            out.append(ww)
    return out


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


def gate_targets_by_enabled(targets, configs):
    """窗口[抓取发布]开关门禁: 未开启的窗口不参与任务执行。

    返回 (通过列表, 被跳过列表); 配置缺失或缺 enabled 字段均视为未开启。
    """
    ok = [w for w in targets if bool((configs.get(w["id"]) or {}).get("enabled"))]
    ok_ids = {w["id"] for w in ok}
    skipped = [w for w in targets if w["id"] not in ok_ids]
    return ok, skipped


def gate_targets_by_open(bit, targets, task_name):
    """窗口打开状态门禁: 窗口已处于打开状态(可能有人在用)时跳过不执行, 等下次调度再跑。

    返回 (通过列表, 被跳过列表); 查询不到状态的窗口(None)不拦截, 保持可执行。
    """
    ok, skipped = [], []
    for w in targets:
        if bit.window_status(w["id"]) is True:
            skipped.append(w)
        else:
            ok.append(w)
    if skipped:
        names = "、".join((w.get("name") or w["id"]) for w in skipped)
        add_log(f"[{task_name}] 跳过处于打开状态的窗口(等待下次执行): {names}", "warning")
    return ok, skipped


# 浏览器抓取模式注册表: source_type -> (抓取函数, 下载Referer)
_SCRAPERS = {
    "doubao_page": (scrape_doubao_chat, "https://www.doubao.com/"),
    "xiaoyunque_page": (scrape_xiaoyunque_chat, "https://xyq.jianying.com/"),
}


def get_scraper(source_type):
    """按内容源类型返回 (抓取函数, Referer); 非浏览器抓取模式返回 None"""
    return _SCRAPERS.get(str(source_type or "").strip())


# 按编号定位回复(消费提问模式): source_type -> 定位函数
_MARK_FINDERS = {
    "doubao_page": find_doubao_replies_by_marks,
    "xiaoyunque_page": find_xiaoyunque_replies_by_marks,
}

# 消费提问保护期: 提问成功至少10分钟后才会被消费(等AI生成视频完毕)
CONSUME_MIN_AGE_SECONDS = 600


def get_mark_finder(source_type):
    """返回按编号定位回复的函数; 非浏览器源不支持消费模式"""
    return _MARK_FINDERS.get(str(source_type or "").strip())


# ---------------- 提问任务编号(唯一标志) ----------------
ASK_MARK_RE = re.compile(r"ASK-\d{8}-[A-Z0-9]{4,8}")


def build_ask_mark():
    """生成提问唯一编号: ASK-YYYYMMDD-XXXX(随机4位), AI回复需原样带回"""
    day = dt.date.today().strftime("%Y%m%d")
    rand = uuid.uuid4().hex[:4].upper()
    return f"ASK-{day}-{rand}"


def find_ask_mark(text):
    """提取文本中最后一个编号(AI可能多轮回显, 最后一个即最新)"""
    ms = ASK_MARK_RE.findall(text or "")
    return ms[-1] if ms else ""


def analyze_ask_mark(material):
    """材料文本 -> (编号, 剥离编号行后的干净文案, 已发布时间或None)"""
    mark = find_ask_mark(material)
    cleaned = "\n".join(
        ln for ln in (material or "").splitlines() if not ASK_MARK_RE.search(ln)
    )
    published = None
    if mark:
        published = (store.get_ask(mark) or {}).get("published_at") or None
    return mark, cleaned, published


def ask_direct_content(material):
    """从提问回复文本中直接解析【标题】/【描述】作为发布内容。

    回复自带规范的标题和简介(含话题标签)时无需再AI整理, 直接采用;
    解析不到返回 None(调用方退回 AI 整理流程)。
    """
    text = material or ""
    tm = re.search(r"【标题】\s*([^\n\r【]+)", text)
    dm = re.search(r"【描述】\s*([\s\S]+?)(?=\n\s*【|$)", text)
    if not tm or not dm:
        return None
    title = tm.group(1).strip()
    desc = dm.group(1).strip()
    tags = [t.lstrip("#").strip() for t in re.findall(r"#[^\s#]+", desc)]
    tags = [t for t in tags if t][:10]
    if not title:
        return None
    return {"title": title, "description": desc, "tags": tags}


_APPENDIX_TMPL = (
    "\n\n——系统附加要求（必须遵守）——\n"
    "本次任务编号：{mark}\n"
    "你必须在回复正文的最前面单独一行原样输出该任务编号，格式：任务编号：{mark}。\n"
    "不得改写、翻译、增删其中任何字符，也不得省略；该编号仅用于流程归档追踪，无需做任何解释。"
)


def run_task(task_id, only_window_id=None, force=False):
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

    # 变量模式(所有任务类型统一): 任务只选变量名, 执行时自动找到配置了该变量的窗口并取值
    url_var = (task.get("url_var") or "").strip()
    if url_var:
        targets = resolve_targets_by_var(url_var, windows)
        targets = [w for w in targets if (not only_window_id or w["id"] == only_window_id)]
        if not targets:
            add_log(
                f"[{name}] 没有窗口配置了变量[{url_var}]，"
                f"请到【窗口管理→配置】为各窗口填写该变量后重试",
                "error",
            )
            return
    else:
        targets = [w for w in resolve_targets(task, windows, configs) if (not only_window_id or w["id"] == only_window_id)]

    # 窗口[抓取发布]开关门禁: 未开启的窗口不执行任务
    if targets:
        targets, skipped = gate_targets_by_enabled(targets, configs)
        if skipped:
            names = "、".join((w.get("name") or w["id"]) for w in skipped)
            add_log(f"[{name}] 跳过未开启[抓取发布]的窗口: {names}", "warning")
        if only_window_id and not targets:
            add_log(f"[{name}] 指定的窗口未开启[抓取发布]，已取消执行（请在【窗口管理】开启后再试）", "error")
            return

    # 窗口打开状态门禁: 窗口已打开(可能有人在用)时不打扰, 跳过等下次调度; 强制执行时不拦截
    if targets and not force:
        targets, open_skipped = gate_targets_by_open(bit, targets, name)
        if only_window_id and not targets and open_skipped:
            add_log(f"[{name}] 指定的窗口处于打开状态，已跳过本次执行（关闭窗口后重试）", "error")
            return

    # AI提问任务: 独立流程(打开对话框URL发送提示词)
    if task.get("task_type") == "ai_ask":
        if not targets:
            add_log(f"[{name}] 没有可执行的目标窗口（请选择URL变量或在窗口管理中开启并绑定）", "error")
            return
        run_ai_ask(task, bit, settings, targets)
        return

    if not targets:
        add_log(f"[{name}] 没有可执行的目标窗口（请先在【窗口管理】中开启抓取并绑定该任务）", "error")
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

    # ---------------- 消费AI提问模式: 按任务编号定位回复并发布其视频 ----------------
    finder = get_mark_finder(task.get("source_type")) if task.get("consume_ask") else None
    if task.get("consume_ask") and not finder:
        add_log(f"[{name}] 消费提问模式仅支持内容源类型=豆包/小云雀聊天页", "error")
        return
    if finder:
        referer = (_SCRAPERS.get(str(task.get("source_type")).strip()) or ("", ""))[1]
        ask_task_id_cfg = (task.get("consume_ask_task_id") or "").strip()
        ask_task_id = ask_task_id_cfg or None
        # 消费保护期(秒)取自绑定的AI提问任务设置[ask_min_age分钟]; 未绑定用默认10分钟; 强制执行时归零立即消费
        min_age = CONSUME_MIN_AGE_SECONDS
        if ask_task_id_cfg:
            atask = store.get_task(ask_task_id_cfg)
            if atask:
                min_age = max(0, int(atask.get("ask_min_age") or 0)) * 60
            else:
                add_log(f"[{name}] 绑定的提问任务不存在({ask_task_id_cfg})，使用默认{CONSUME_MIN_AGE_SECONDS // 60}分钟等待", "warning")
        if force:
            min_age = 0
        wait_s = int(task.get("source_wait") or 15)
        wvars = task.get("window_vars") or {}
        add_log(
            f"[{name}] 消费提问模式: 仅消费提问成功≥{min_age // 60}分钟的未发布编号"
            + (f", 绑定提问任务[{ask_task_id}]" if ask_task_id else ", 不限提问任务")
        )
        for w in targets:
            src = (w.get("_var_value") or ((wvars.get(w["id"]) or {}).get("source_url") or "")).strip()
            if not src:
                add_log(f"[{name}] 窗口[{w['name']}] 未配置聊天页链接(变量)，跳过", "error")
                continue
            pend = store.pending_asks(min_age_seconds=min_age, window_id=w["id"], task_id=ask_task_id)
            if not pend:
                add_log(f"[{name}] 窗口[{w['name']}] 无待消费编号（需提问成功≥{min_age // 60}分钟且未发布）")
                continue
            dkey = f"{w['id']}|{_today()}"
            used = state.setdefault("daily", {}).get(dkey, 0)
            marks = [a["mark"] for a in pend]
            try:
                replies = finder(bit, settings, src, w, marks, wait_s)
            except Exception as e:
                add_log(f"[{name}] 窗口[{w['name']}] 编号定位失败: {e}", "error")
                continue
            got = {r["mark"]: r for r in replies}
            for a in pend:
                mk = a["mark"]
                if limit > 0 and used >= limit:
                    add_log(f"[{name}] 窗口[{w['name']}] 今日已达上限({limit})，剩余编号下轮消费")
                    break
                r = got.get(mk)
                if not r:
                    add_log(f"[{name}] 窗口[{w['name']}] 编号[{mk}] 回复未找到(可能历史过深)，下轮继续")
                    continue
                vurl = (r.get("videos") or [""])[0]
                if not vurl:
                    add_log(f"[{name}] 窗口[{w['name']}] 编号[{mk}] 暂无视频(可能仍在生成)，下轮再试")
                    continue
                _, material, _pub = analyze_ask_mark(r.get("text") or "")
                # 回复自带【标题】/【描述】时直接采用(与提问内容严格对应); 缺字段才退回AI整理
                content = ask_direct_content(material)
                if content:
                    add_log(f"[{w['name']}] 编号[{mk}] 使用回复自带标题描述: {content['title']}")
                else:
                    content = prepare_content(task, settings, material or f"任务编号{mk}", w["name"])
                try:
                    vpath = sources.download_video(
                        vurl, dl_root / task_id / w["id"],
                        {"User-Agent": "Mozilla/5.0", "Referer": referer},
                    )
                except Exception as e:
                    add_log(f"[{name}] 窗口[{w['name']}] 编号[{mk}] 视频下载失败: {e}", "error")
                    continue
                okv, reason = sources.validate_mp4(vpath)
                if not okv:
                    add_log(
                        f"[{name}] 窗口[{w['name']}] 编号[{mk}] 不是有效视频({reason})，保留待下轮",
                        "error",
                    )
                    try:
                        vpath.unlink(missing_ok=True)
                    except Exception:
                        pass
                    continue
                rec = _record(
                    state,
                    task_id=task_id, task_name=name,
                    window_id=w["id"], window_name=w["name"],
                    source_url=src, video=vurl,
                    content=dict(content),
                    platform=task.get("platform"),
                    ask_mark=mk,
                )
                try:
                    platforms.publish(task.get("platform"), bit, eff(), w, vpath, content)
                    used += 1
                    state["daily"][dkey] = used
                    store.save_state(state)
                    store.mark_ask_published(mk, w["id"])
                    _finish_rec(state, rec, True)
                    ok_cnt += 1
                    add_log(f"[{name}] 窗口[{w['name']}] 编号[{mk}] 已发布并标记，不再重复消费")
                except Exception as e:
                    _finish_rec(state, rec, False, e)
                    fail_cnt += 1
                    add_log(f"[{name}] 窗口[{w['name']}] 编号[{mk}] 发布失败: {e}", "error")
        add_log(f"[{name}] 本轮结束: 成功 {ok_cnt} 个视频, 失败 {fail_cnt} 次")
        return

    scraper = get_scraper(task.get("source_type"))
    if scraper:
        scrape_fn, referer = scraper
        # 浏览器抓取模式(豆包/小云雀): 每个目标窗口各自打开自己的聊天页链接, 抓材料 -> AI整理 -> 发布到所选平台
        wvars = task.get("window_vars") or {}
        wait_s = int(task.get("source_wait") or 15)
        count_n = max(1, int(task.get("fetch_count") or 1))
        for w in targets:
            src = (w.get("_var_value") or ((wvars.get(w["id"]) or {}).get("source_url") or "")).strip()
            if not src:
                add_log(f"[{name}] 窗口[{w['name']}] 未配置聊天页链接(变量)，跳过", "error")
                continue
            dkey = f"{w['id']}|{_today()}"
            used = state.setdefault("daily", {}).get(dkey, 0)
            if limit > 0 and used >= limit:
                add_log(f"[{name}] 窗口[{w['name']}] 今日已达上限({limit})，跳过")
                continue
            try:
                vids, caps = scrape_fn(bit, settings, src, w, wait_s)
            except Exception as e:
                add_log(f"[{name}] 窗口[{w['name']}] 抓取失败: {e}", "error")
                continue

            # 页面关键文字 -> AI整理为结构化发布内容(每窗口一次, 复用于本批视频)
            # 提问编号门禁: 最新回复若带编号且已发布过 -> 不重复发布; 编号行从AI材料中剥离
            ask_mark, material, ask_published = analyze_ask_mark("\n".join(caps))
            if ask_published:
                add_log(
                    f"[{name}] 窗口[{w['name']}] 最新回复已发布过(任务编号[{ask_mark}])，"
                    f"跳过等待新提问（重复发布需重新提问获取新编号）",
                )
                continue
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
            mark_flagged = False
            for i, u in enumerate(batch):
                try:
                    vpath = sources.download_video(
                        u, dl_root / task_id / w["id"],
                        {"User-Agent": "Mozilla/5.0", "Referer": referer},
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
                    ask_mark=ask_mark or None,
                )
                try:
                    platforms.publish(task.get("platform"), bit, eff(), w, vpath, content)
                    done_w.append(u)
                    used += 1
                    state["daily"][dkey] = used
                    if ask_mark and not mark_flagged:
                        store.mark_ask_published(ask_mark, w["id"])
                        mark_flagged = True
                        add_log(f"[{name}] 窗口[{w['name']}] 任务编号[{ask_mark}] 已标记发布，该回复不会再重复发布")
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
    """AI提问任务: 逐窗口打开各自的对话框URL发送提示词(可按日限次, 按成功发送记录计数)"""
    import datetime as dt

    from .asker import ask_in_chat

    name = task.get("name") or task.get("id")
    prompt = (task.get("prompt_text") or "").strip()
    if not prompt:
        add_log(f"[{name}] 未配置提示词，任务结束", "error")
        return
    platform = task.get("ask_platform") or "doubao"
    url_var = (task.get("url_var") or "").strip()
    wvars = task.get("window_vars") or {}
    wait_s = int(task.get("ask_wait") or 30)
    ask_vars = list(task.get("ask_vars") or [])
    video_mode = bool(task.get("ask_video_mode") or False)
    wait_consume = task.get("ask_wait_consume", True)
    limit = int(task.get("ask_daily_limit") or 0)
    limit_mode = (task.get("ask_limit_mode") or "record").strip() or "record"
    today = dt.date.today().isoformat()
    ok_cnt, fail_cnt = 0, 0
    add_log(
        f"[{name}] AI提问模式: 平台[{platform}], 目标 {len(targets)} 个窗口"
        + (f", URL变量[{url_var}]" if url_var else "")
        + (f", 每日上限 {limit} 次/窗口" if limit > 0 else "")
        + ("(忽略已达次数,直接提问)" if limit > 0 and limit_mode == "force" else "")
        + (", 豆包视频生成模式" if video_mode else "")
        + (f", 变量补充: {ask_vars}" if ask_vars else "")
    )
    for w in targets:
        # 变量模式: URL来自窗口变量(resolve_targets_by_var 附带的 _var_value); 否则用任务内逐窗配置
        src = (w.get("_var_value") if url_var else "") or ((wvars.get(w["id"]) or {}).get("source_url") or "").strip()
        if not src:
            add_log(f"[{name}] 窗口[{w['name']}] 未配置对话框URL，跳过（每个目标窗口必须有自己的链接）", "error")
            continue
        # 防重复提问门禁: 上一个编号还没被视频发布任务消费(未发布)时不再提问
        if wait_consume and store.has_pending_ask(w["id"], task.get("id") or ""):
            add_log(f"[{name}] 窗口[{w['name']}] 存在尚未被消费的提问编号，等待消费后再提问（防止重复提问堆叠）", "warning")
            continue
        if limit > 0 and limit_mode != "force":
            # record模式(默认): 按本任务的成功发送记录计数(待发布+已发布均计入), 不开窗查询
            asked = store.count_asks_on_date(today, window_id=w["id"], task_id=task.get("id") or "")
            add_log(f"[{name}] 窗口[{w['name']}] 本任务今日已成功提问 {asked}/{limit} 次(含待发布/已发布)")
            if asked >= limit:
                add_log(f"[{name}] 窗口[{w['name']}] 今日已达提问上限({limit})，跳过")
                continue
        elif limit_mode == "force":
            add_log(f"[{name}] 窗口[{w['name']}] 强制提问模式: 忽略今日次数判断, 直接开窗提问")
        try:
            # 提问唯一编号: 每窗口每次提问生成独立编号, 附加到提示词并登记,
            # AI回复须原样带回; 发布侧凭该编号判断"已提问未发布/已发布"避免重复发布
            ask_mark = build_ask_mark()
            win_prompt = prompt + _APPENDIX_TMPL.format(mark=ask_mark)
            add_log(f"[{name}] 窗口[{w['name']}] 本次任务编号: {ask_mark}")
            ask_in_chat(bit, settings, src, w, win_prompt, wait_s, ask_vars=ask_vars, video_mode=video_mode)
            store.record_ask(ask_mark, w["id"], task.get("id") or "")
            ok_cnt += 1
        except Exception as e:
            fail_cnt += 1
            add_log(f"[{name}] 窗口[{w['name']}] 提问失败: {e}", "error")
    add_log(f"[{name}] 本轮结束: 成功提问 {ok_cnt} 个窗口, 失败 {fail_cnt} 次")


def safe_run(task_id, only_window_id=None, force=False):
    try:
        run_task(task_id, only_window_id, force)
    except Exception as e:
        add_log(f"任务执行异常: {e}", "error")


def run_async(task_id, only_window_id=None, force=False):
    threading.Thread(target=safe_run, args=(task_id, only_window_id, force), daemon=True).start()
