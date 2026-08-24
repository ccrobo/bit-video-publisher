"""通过已登录的比特浏览器窗口抓取豆包(doubao.com)聊天页中的视频与文案"""
import re
import time

from playwright.sync_api import sync_playwright

from .logs import add_log


class DoubaoScrapeError(Exception):
    pass


_VIDEO_JS = """
() => {
  const urls = [];
  const push = u => { if (u && /^https?:\\/\\//.test(u) && !urls.includes(u)) urls.push(u); };
  document.querySelectorAll('video').forEach(v => {
    push(v.currentSrc || v.src || '');
    v.querySelectorAll('source').forEach(s => push(s.src));
  });
  document.querySelectorAll('a[href]').forEach(a => { if (/\\.mp4($|\\?)/.test(a.href)) push(a.href); });
  try {
    performance.getEntriesByType('resource').forEach(e => {
      if (/\\.mp4($|\\?)/.test(e.name) || /video/.test(e.name)) push(e.name);
    });
  } catch (e) {}
  return urls;
}
"""

_TEXT_JS = "() => document.body.innerText"

# 定位页面最底部最新回复的原文作为文案材料(交AI整理, 不做二次加工)。
# 返回 {how: 策略名, text: 回复原文}, 便于日志诊断
#
# 策略更新(解决: 用户提问含【标题】等标记时被误判为最新AI回复):
# A1: 含【生成时间】的"最小气泡块" —— 只有AI回复才会带, 文档序最后 = 最新AI回复 (100%准)
# A2: AI角色气泡 (含agent/receive/bot/reply 不含 user/send/my/query) 且含【标题/描述/解答/旁白】
# A3: 旧通用 mark-block (仍排除用户气泡) 兜底
# 之后才轮到: scroller-anchor / closest
_LATEST_REPLY_TEXT_JS = """
() => {
  const GENERIC_MARK = /【(标题|描述|解答|旁白|问题)】/;
  const TS_MARK = /【生成时间】/;
  const bodyLen = (document.body.innerText || '').length;
  const maxBodyLen = t => t && t.length < Math.max(1200, bodyLen);
  // 用户气泡排除: 任一层的类/角色/属性含有用户发送侧关键词
  const USER_HINT = /(^|[-_ ])(user|my|me|owner|query|send|sender|prompt|inputs?|question|ask|qbox)([-_ ]|$)/i;
  const AI_HINT   = /(^|[-_ ])(agent|bot|reply|receive|receiver|assistant|model|answer|chatbot|message[-_]?in)([-_ ]|$)/i;
  const containerLike = c => (c || '').startsWith('container-') || /message|receive|agent|reply|chat/i.test(c);
  const hasUserMark = el => {
    let n = el; let depth = 0;
    while (n && n !== document.body && depth < 6) {
      const c = ((n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute && n.getAttribute('role') || '')).toString();
      if (USER_HINT.test(c)) return true;
      n = n.parentElement; depth++;
    }
    return false;
  };
  const hasAIMark = el => {
    let n = el; let depth = 0;
    while (n && n !== document.body && depth < 6) {
      const c = ((n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute && n.getAttribute('role') || '')).toString();
      if (AI_HINT.test(c)) return true;
      n = n.parentElement; depth++;
    }
    return false;
  };
  const collectLeaves = list => {
    return list.filter(x => !list.some(y => y !== x && x.el.contains(y.el)));
  };

  // --- A1: 【生成时间】强标记 (只有AI会写) ---
  const a1 = [];
  for (const d of document.querySelectorAll('div')) {
    if (!containerLike((d.className || '').toString())) continue;
    if (hasUserMark(d)) continue;
    const t = d.innerText || '';
    if (TS_MARK.test(t) && maxBodyLen(t)) a1.push({ el: d, t: t.trim() });
  }
  const a1Leaves = collectLeaves(a1);
  if (a1Leaves.length) {
    const last = a1Leaves[a1Leaves.length - 1];
    if ((last.t || '').length > 10) return { how: 'mark-ts(' + a1Leaves.length + ')', text: last.t };
  }

  // --- A2: AI角色气泡 + 至少有 标题/描述/解答/旁白 任一项 ---
  const a2 = [];
  for (const d of document.querySelectorAll('div')) {
    if (!containerLike((d.className || '').toString())) continue;
    if (hasUserMark(d)) continue;
    if (!hasAIMark(d)) continue;
    const t = d.innerText || '';
    if (GENERIC_MARK.test(t) && maxBodyLen(t)) a2.push({ el: d, t: t.trim() });
  }
  const a2Leaves = collectLeaves(a2);
  if (a2Leaves.length) {
    const last = a2Leaves[a2Leaves.length - 1];
    if ((last.t || '').length > 30) return { how: 'mark-ai(' + a2Leaves.length + ')', text: last.t };
  }

  // --- A3: 旧策略(兜底) —— 但先排除用户气泡 ---
  const cand = [];
  for (const d of document.querySelectorAll('div')) {
    if (!containerLike((d.className || '').toString())) continue;
    if (hasUserMark(d)) continue;
    const t = d.innerText || '';
    if (GENERIC_MARK.test(t) && maxBodyLen(t)) cand.push({ el: d, t: t.trim() });
  }
  const leaves = collectLeaves(cand);
  if (leaves.length) {
    const last = leaves[leaves.length - 1];
    if ((last.t || '').length > 30) return { how: 'mark-block(' + leaves.length + ')', text: last.t };
  }

  // 视频卡片锚点 (保持原逻辑不变)
  let cards = [...document.querySelectorAll('[class*="block-video"],[class*="image-box-grid-item"]')]
    .filter(el => el.getBoundingClientRect().width > 120);
  if (!cards.length) {
    cards = [...document.querySelectorAll('img[src*="video_dsz"], img[src*="tplv-a9rns"]')]
      .map(im => im.closest('[class*="block-video"]') || im.parentElement)
      .filter(Boolean);
  }
  const card = cards.length ? cards[cards.length - 1] : null;

  // 策略B: 最近可滚动祖先
  if (card) {
    let sc = card.parentElement;
    while (sc && sc !== document.body && !(sc.scrollHeight > sc.clientHeight + 100)) {
      sc = sc.parentElement;
    }
    if (sc && sc !== document.body) {
      let top = card;
      while (top.parentElement && top.parentElement !== sc) top = top.parentElement;
      const t = ((top.innerText) || '').trim();
      if (t && t.length > 30 && maxBodyLen(t)) return { how: 'scroller-anchor', text: t.slice(0, 6000) };
    }
    // 策略C: 特征类名兜底 + 不是用户气泡
    const msg = card.closest('[class*="message"],[class*="receive"],[class*="agent"],[class*="container-"],[class*="reply"]');
    if (msg && !hasUserMark(msg)) {
      const t = ((msg.innerText) || '').trim();
      if (t && t.length > 30) return { how: 'closest', text: t.slice(0, 6000) };
    }
  }
  return { how: 'none', text: '' };
}
"""

# 豆包聊天区是虚拟滚动列表(v_list_scroller), 视口外的消息(含视频卡)不会渲染。
# 需要逐屏向上滚动促使历史消息挂载
_SCROLL_UP_JS = """
() => {
  const els = [...document.querySelectorAll('div')]
    .filter(d => d.scrollHeight > d.clientHeight + 300);
  let moved = false;
  els.forEach(d => {
    const step = Math.max(240, d.clientHeight * 0.8);
    const before = d.scrollTop;
    d.scrollTop = Math.max(0, before - step);
    if (d.scrollTop < before - 5) moved = true;
  });
  return moved;
}
"""

# 豆包聊天页上旧下新: 文档序最后一张视频卡片就是最新视频, 打开页面即在视口内。
# 用稳定类名前缀(block-video/image-box-grid-item)定位, 封面水印图作为兜底特征
_LATEST_CARD_INFO_JS = """
() => {
  let cards = [...document.querySelectorAll('[class*="block-video"],[class*="image-box-grid-item"]')]
    .filter(el => el.getBoundingClientRect().width > 120);
  if (!cards.length) {
    cards = [...document.querySelectorAll('img[src*="video_dsz"], img[src*="tplv-a9rns"]')]
      .map(im => im.closest('[class*="block-video"]') || im.parentElement)
      .filter(Boolean);
  }
  if (!cards.length) return null;
  const el = cards[cards.length - 1];
  el.scrollIntoView({ block: 'center', behavior: 'instant' });
  const r = el.getBoundingClientRect();
  return {
    x: r.x + r.width / 2,
    y: Math.max(20, Math.min(r.y + r.height / 2, window.innerHeight - 20)),
  };
}
"""

_LATEST_CARD_SRC_JS = """
() => {
  const cards = [...document.querySelectorAll('[class*="block-video"],[class*="image-box-grid-item"]')]
    .filter(el => el.getBoundingClientRect().width > 120);
  if (!cards.length) return '';
  const el = cards[cards.length - 1];
  const v = el.querySelector('video');
  const s = v ? (v.currentSrc || v.src || '') : '';
  return /^https?:\\/\\//.test(s) ? s : '';
}
"""

_FIND_DOWNLOAD_JS = """
() => {
  const els = [...document.querySelectorAll('button, a, [role=button], div, span')]
    .filter(el => {
      const t = (el.innerText || '').trim();
      if (t !== '下载' && !t.includes('下载视频')) return false;
      const r = el.getBoundingClientRect();
      return r.width > 8 && r.width < 300 && r.height > 8;
    });
  if (!els.length) return null;
  const r = els[els.length - 1].getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
}
"""


def _click_latest_card(page):
    """直接定位文档序最底部的视频卡片(=最新视频, 打开页面即见)并点击触发播放"""
    try:
        pos = page.evaluate(_LATEST_CARD_INFO_JS)
    except Exception:
        return False
    if not pos:
        return False
    try:
        page.mouse.move(pos["x"], pos["y"])
        time.sleep(0.8)
        page.mouse.click(pos["x"], pos["y"])
        return True
    except Exception:
        return False


def _click_download_btn(page):
    """点击卡片上的下载按钮, 迫使浏览器发起真实视频请求"""
    pos = None
    try:
        pos = page.evaluate(_FIND_DOWNLOAD_JS)
    except Exception:
        return False
    if not pos:
        return False
    try:
        page.mouse.click(pos["x"], pos["y"])
        return True
    except Exception:
        return False

_BAD_WORDS = (
    "复制", "重新生成", "重新回答", "发送", "收藏", "点赞", "点踩", "举报",
    "分享", "下载视频", "暂停", "倍速", "全屏", "退出登录", "新对话",
    "开新对话", "历史对话", "深度思考", "联网搜索", "拍照答疑", "帮我",
    "写代码", "登录", "注册", "手机号", "验证码", "会员", "消息", "字",
    "Ctrl", "Shift", "Alt", "快捷键", "技能", "连接器", "伙伴", "智能体",
)

_MEDIA_HINTS = (
    ".mp4", "douyinvod", "/video/tos/", "vod.", "bytecdn", "zjcdn", "ixigua",
)

_MEDIA_BAD_HOSTS = (
    "creator.douyin.com", "sts2", "aweme/mid", "/api/", "passport",
)


def _looks_media(url):
    low = url.lower()
    if any(b in low for b in _MEDIA_BAD_HOSTS):
        return False
    return any(h in low for h in _MEDIA_HINTS)


def _media_key(url):
    """同一视频不同CDN边缘节点的归并key: 路径尾两段"""
    try:
        path = url.split("?")[0]
        return "/".join(path.split("/")[-2:])
    except Exception:
        return url


def _dedupe_media(urls):
    out, keys = [], {}
    for u in urls:
        k = _media_key(u)
        if k in keys:
            old = keys[k]
            if "douyinvod.com" in u and "douyinvod.com" not in old:
                out[out.index(old)] = u
                keys[k] = u
            continue
        keys[k] = u
        out.append(u)
    return out


_PROMPT_WORDS = (
    "定时任务", "触发", "第一步", "第二步", "第三步", "第四步", "第五步",
    "请完成以下", "请完成如下", "生成配套短视频", "视频创作需求", "拆解",
    "构思", "读取视频创作技能", "字幕", "旁白", "镜头", "模型生成",
    "注：", "seedance", "竖屏", "悬念引入", "交付给你", "已生成",
    "本次请求", "工作任务", "技能", "连接器",
)


def _clean_md(s):
    for c in ("**", "##", "`"):
        s = s.replace(c, "")
    return s.strip()


def _trim_caption(s, n=240):
    """截断到句子边界, 避免文案过长被抖音截断"""
    if len(s) <= n:
        return s
    cut = s[:n]
    best = -1
    for sep in ("。", "！", "？", "；", "!", "?", ";"):
        i = cut.rfind(sep)
        if i > 60 and i > best:
            best = i
    if best > 0:
        return cut[:best + 1]
    return cut.rstrip() + "…"


def _parse_captions(text):
    """优先提取 Q:/A: 问答体的回答正文作为文案; 无则退化为通用候选行(过滤提示词)"""
    answers = []
    generic = []
    seen = set()
    q_last = ""

    def _prefix_of(s, letters):
        for ch in letters:
            for colon in ("：", ":"):
                pre = ch + colon
                if s.startswith(pre):
                    return s[len(pre):].strip()
        return None

    for raw in text.splitlines():
        s = _clean_md(raw)
        if not s:
            continue
        q_body = _prefix_of(s, "Qq")
        if q_body is not None:
            q_last = q_body
            continue
        a_body = _prefix_of(s, "Aa")
        if a_body is not None:
            if len(a_body) >= 10:
                cap = _trim_caption(f"{q_last} {a_body}" if q_last else a_body)
                k = cap[:20]
                if k not in seen:
                    seen.add(k)
                    answers.append(cap)
            q_last = ""
            continue
        if len(s) < 12 or len(s) > 300:
            continue
        if any(b in s for b in _BAD_WORDS) or any(p in s for p in _PROMPT_WORDS):
            continue
        if re.fullmatch(r"[0-9:/.、\s]+", s):
            continue
        if s.startswith(("#", "-", "*", "|", "[", "{", ">")):
            continue
        k = s[:24]
        if k in seen:
            continue
        seen.add(k)
        generic.append(_trim_caption(s))
        if len(generic) >= 30:
            break

    result = answers if answers else generic
    add_log(f"文案解析: 提取到回答体 {len(answers)} 条, 通用候选 {len(generic)} 条")
    return result[:30]


def scrape_doubao_chat(bitclient, settings, source_url, window, wait_seconds=15):
    """在指定窗口中打开豆包聊天页, 返回 (视频URL列表[最新在前], 候选文案列表)"""
    addr = bitclient.open_window(window["id"])
    cdp = addr if addr.startswith("http") else "http://" + addr
    pw = None
    page = None
    try:
        add_log(f"[{window['name']}] 正在打开豆包聊天页抓取: {source_url}")
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        # 监听整个上下文的所有响应(含点击封面后新开的播放标签页)
        net_urls = []

        def _on_response(resp):
            try:
                u = resp.url or ""
                ct = (resp.headers or {}).get("content-type", "") or ""
            except Exception:
                return
            if _looks_media(u) or ct.lower().startswith("video/"):
                if u not in net_urls:
                    net_urls.append(u)

        try:
            ctx.on("response", _on_response)
        except Exception:
            page.on("response", _on_response)

        page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
        cur = page.url or ""
        if "/login" in cur or "passport" in cur:
            raise DoubaoScrapeError(f"窗口[{window['name']}] 未登录豆包，请先在该窗口手动登录 doubao.com")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        # 登录态检查(页面内出现扫码/验证码登录说明未登录)
        try:
            body_txt = page.evaluate(_TEXT_JS) or ""
            if ("扫码登录" in body_txt or "验证码登录" in body_txt or "手机号登录" in body_txt):
                raise DoubaoScrapeError(f"窗口[{window['name']}] 未登录豆包，请先在该窗口手动登录 doubao.com")
        except DoubaoScrapeError:
            raise
        except Exception:
            pass

        wait_s = max(10, int(wait_seconds or 15))
        deadline = time.time() + wait_s + 30
        add_log(f"[{window['name']}] 聊天页已打开，开始检测视频链接（最多 {wait_s + 30} 秒）...")
        videos = []
        attempt = 0
        clicked_latest = False
        downloaded = False
        while time.time() < deadline:
            attempt += 1
            # 只采集自己打开的聊天页DOM: 遍历全部标签页时, 残留页面的evaluate可能无超时挂起
            try:
                dom_urls = page.evaluate(_VIDEO_JS) or []
            except Exception:
                dom_urls = []
            # 最底部最新卡片的<video>直链优先(打开页面即见, 无需滚动加载历史)
            latest_src = ""
            try:
                s = page.evaluate(_LATEST_CARD_SRC_JS)
                if s and s.startswith("http"):
                    latest_src = s
            except Exception:
                pass
            # 最新直链置顶; DOM文档序(上旧下新)为权威排序, 网络捕获仅补充DOM中没有的,
            # 同一视频的多CDN副本按key归并(优先douyinvod官方域名), 避免打乱时间顺序
            merged, key_pos = [], {}
            for u in ([latest_src] if latest_src else []) + list(dom_urls) + net_urls:
                if not u or not u.startswith("http"):
                    continue
                k = _media_key(u)
                if k in key_pos:
                    old = merged[key_pos[k]]
                    if "douyinvod.com" in u and "douyinvod.com" not in old:
                        merged[key_pos[k]] = u
                    continue
                key_pos[k] = len(merged)
                merged.append(u)
            videos = [u for u in merged if _looks_media(u)]
            if videos:
                break
            # 依次尝试: 点击最底部最新卡片播放 -> 点其"下载"按钮 -> 最后才向上滚挂载历史
            if not clicked_latest and _click_latest_card(page):
                clicked_latest = True
                add_log("已定位页面最底部最新视频卡片并点击播放...")
                time.sleep(4)
                continue
            if clicked_latest and not downloaded and _click_download_btn(page):
                downloaded = True
                add_log("已点击下载按钮获取视频直链...")
                time.sleep(5)
                continue
            try:
                page.evaluate(_SCROLL_UP_JS)
            except Exception:
                pass
            time.sleep(3)

        # 文案材料优先取"最新回复"原文(页面最底部), 直接交AI整理; 失败才退化全页解析
        reply_info = {}
        try:
            reply_info = page.evaluate(_LATEST_REPLY_TEXT_JS) or {}
        except Exception:
            reply_info = {}
        reply_text = (reply_info.get("text") or "").strip()
        how = reply_info.get("how") or "none"
        if reply_text:
            head = reply_text[:50].replace("\n", " ")
            tail = reply_text[-40:].replace("\n", " ")
            add_log(
                f"[{window['name']}] 已定位文案[{how}]({len(reply_text)}字) "
                f"开头: {head} ... 结尾: {tail}"
            )
            captions = [reply_text]
        else:
            add_log(
                f"[{window['name']}] 未定位到最新回复块[{how}]，退化为全页文案解析", "warning"
            )
            try:
                text = page.evaluate(_TEXT_JS) or ""
            except Exception:
                text = ""
            captions = _parse_captions(text)

        # 最新视频保持在列表头部(最新卡片直链优先, 其余按新->旧); 发布永远取头部
        if latest_src:
            videos = [videos[0]] + list(reversed(videos[1:]))
        else:
            videos = list(reversed(videos))
        latest = videos[0] if videos else ""
        add_log(
            f"豆包页面抓取完成: 视频 {len(videos)} 个, 候选文案 {len(captions)} 条 (尝试{attempt}轮)"
            + (f"; 最新视频: {latest[:80]}" if latest else "")
        )
        if not videos:
            raise DoubaoScrapeError(
                "未能从页面提取到视频链接。请确认该聊天已生成视频、抓取窗口已登录豆包，"
                "或适当调大【等待加载】秒数"
            )
        return videos, captions
    finally:
        if page is not None:
            try:
                page.close(timeout=5000)
            except Exception:
                pass
        # 清理残留标签页: 之前发布留下的创作中心页 / 重复打开的同一聊天页
        try:
            src = (source_url or "").rstrip("/")
            for p in list(ctx.pages):
                u = (p.url or "")
                if "creator.douyin.com" in u or (src and u.rstrip("/") == src):
                    p.close(timeout=5000)
        except Exception:
            pass
        if pw is not None:
            # 先断开CDP连接再停playwright, 避免stop()在连接未释放时挂起
            try:
                browser.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
        try:
            bitclient.close_window(window["id"])
            add_log(f"[{window['name']}] 抓取完成，窗口已关闭")
        except Exception:
            pass
