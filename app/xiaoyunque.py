"""通过已登录的比特浏览器窗口抓取小云雀(xyq.jianying.com)聊天页中的视频与文案"""
import re
import time

from playwright.sync_api import sync_playwright

from .logs import add_log


class XiaoyunqueScrapeError(Exception):
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
      if (/\\.mp4($|\\?)/.test(e.name) || /video\\/tos|jyvod|vod\\./.test(e.name)) push(e.name);
    });
  } catch (e) {}
  return urls;
}
"""

_TEXT_JS = "() => document.body.innerText"

# 定位页面最底部最新回复(AI生成视频的那条)原文作为文案材料, 交AI整理不做二次加工。
# 返回 {how: 策略名, text: 回复原文}, 便于日志诊断
# A1: 含<video>的消息块(只有AI生成的视频回复才带), 文档序最后 = 最新
# A2: AI角色气泡(类名含 assistant/agent/bot/reply/receive/answer/markdown 不含用户侧关键词)
# A3: 文档序最后一个长文本块(仍排除用户气泡)
_LATEST_REPLY_TEXT_JS = """
() => {
  const bodyLen = (document.body.innerText || '').length;
  const maxBodyLen = t => t && t.length < Math.max(1200, bodyLen);
  const USER_HINT = /(^|[-_ ])(user|my|me|owner|query|send|sender|prompt|inputs?|question|ask)([-_ ]|$)/i;
  const AI_HINT   = /(^|[-_ ])(assistant|agent|bot|reply|receive|receiver|answer|model|markdown|message[-_]?in)([-_ ]|$)/i;
  const hasUserMark = el => {
    let n = el; let depth = 0;
    while (n && n !== document.body && depth < 6) {
      const c = ((n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute && n.getAttribute('role') || '')).toString();
      if (USER_HINT.test(c)) return true;
      n = n.parentElement; depth++;
    }
    return false;
  };
  const pickBlock = el => {
    // 从叶子向上找"最小完整消息块": 到出现兄弟分支或到达聊天容器为止
    let best = el;
    let n = el; let depth = 0;
    while (n && n !== document.body && depth < 8) {
      const c = ((n.className || '') + ' ' + (n.id || '')).toString();
      if (/message|chat|conversation|session|timeline/i.test(c)) break;
      best = n;
      n = n.parentElement; depth++;
    }
    return best;
  };
  const textOf = el => (el.innerText || '').trim();

  // A1: 含<video>的块
  const vids = [...document.querySelectorAll('video')].filter(v => {
    const r = v.getBoundingClientRect();
    return r.width > 60 && r.height > 40;
  });
  for (let i = vids.length - 1; i >= 0; i--) {
    const blk = pickBlock(vids[i]);
    const t = textOf(blk);
    if (t.length > 10 && maxBodyLen(t) && !hasUserMark(blk)) {
      return { how: 'xyq-video-block', text: t };
    }
  }

  // A2: AI角色气泡
  const cands = [...document.querySelectorAll('div,section,li')]
    .filter(el => el.getBoundingClientRect().width > 200);
  const aiBlocks = cands.filter(el => {
    const c = ((el.className || '') + ' ' + (el.id || '')).toString();
    return AI_HINT.test(c) && !USER_HINT.test(c);
  });
  for (let i = aiBlocks.length - 1; i >= 0; i--) {
    const blk = pickBlock(aiBlocks[i]);
    const t = textOf(blk);
    if (t.length > 30 && maxBodyLen(t) && !hasUserMark(blk)
        && /(标题|描述|旁白|文案|视频|脚本|分镜)/.test(t)) {
      return { how: 'xyq-ai-bubble', text: t };
    }
  }

  // A3: 最后一个长文本块
  const longs = cands.filter(el => {
    const t = textOf(el);
    if (t.length < 40 || !maxBodyLen(t)) return false;
    if (hasUserMark(el)) return false;
    return el.querySelector('div,section,p') !== null || true;
  });
  for (let i = longs.length - 1; i >= 0; i--) {
    const blk = pickBlock(longs[i]);
    const t = textOf(blk);
    if (t.length >= 30 && maxBodyLen(t) && !hasUserMark(blk)) {
      return { how: 'xyq-last-long', text: t };
    }
  }
  return { how: 'none', text: '' };
}
"""

# 聊天区虚拟滚动: 逐屏向上滚促使历史消息挂载
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

# 页面上旧下新: 文档序最后一张视频卡片就是最新视频。
# 小云雀无稳定卡片类名, 用"可见<video>元素 / 视频封面图特征"通用定位
_LATEST_CARD_INFO_JS = """
() => {
  let cards = [...document.querySelectorAll('video')]
    .map(v => v.closest('[class*="card"],[class*="video"],[class*="item"],[class*="message"],[class*="agent"]') || v)
    .filter(el => el.getBoundingClientRect().width > 120);
  if (!cards.length) {
    cards = [...document.querySelectorAll('img')]
      .filter(im => /video|cover|thumb|snapshot/i.test((im.src || '')))
      .map(im => im.closest('[class*="card"],[class*="video"],[class*="item"]') || im.parentElement)
      .filter(Boolean)
      .filter(el => el.getBoundingClientRect().width > 120);
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
  const vids = [...document.querySelectorAll('video')]
    .filter(v => v.getBoundingClientRect().width > 60);
  if (!vids.length) return '';
  const v = vids[vids.length - 1];
  const s = v.currentSrc || v.src || '';
  return /^https?:\\/\\//.test(s) ? s : '';
}
"""

_FIND_DOWNLOAD_JS = """
() => {
  const els = [...document.querySelectorAll('button, a, [role=button], div, span')]
    .filter(el => {
      const t = (el.innerText || '').trim();
      if (t !== '下载' && !t.includes('下载视频') && !t.includes('下载成片')) return false;
      const r = el.getBoundingClientRect();
      return r.width > 8 && r.width < 300 && r.height > 8;
    });
  if (!els.length) return null;
  const r = els[els.length - 1].getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
}
"""


def _click_latest_card(page):
    """定位文档序最底部的视频卡片(=最新视频)并点击触发播放"""
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


_MEDIA_HINTS = (
    ".mp4", "/video/tos/", "douyinvod", "zjcdn", "ixigua",
    "aweme/v1/play", "jyvod", "vod.jianying", "bytetos.com/obj/video",
)

_MEDIA_BAD_HOSTS = (
    "passport", "/api/", "sts2",
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
    """按 media_key 去重, 同key优先保留 jianying 官方域名副本"""
    result = []
    seen = {}
    for u in urls:
        k = _media_key(u)
        if not k:
            continue
        if k in seen:
            old = result[seen[k]]
            if "jianying.com" in u.lower() and "jianying.com" not in old.lower():
                result[seen[k]] = u
            continue
        seen[k] = len(result)
        result.append(u)
    return result[:30]


def scrape_xiaoyunque_chat(bitclient, settings, source_url, window, wait_seconds=15):
    """在指定窗口中打开小云雀聊天页, 返回 (视频URL列表[最新在前], 候选文案列表)"""
    addr = bitclient.open_window(window["id"])
    cdp = addr if addr.startswith("http") else "http://" + addr
    pw = None
    page = None
    ctx = None
    browser = None
    try:
        add_log(f"[{window['name']}] 正在打开小云雀聊天页抓取: {source_url}")
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        # 监听整个上下文的所有响应捕获视频直链
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
            raise XiaoyunqueScrapeError(
                f"窗口[{window['name']}] 未登录小云雀，请先在该窗口手动登录 jianying.com"
            )
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        # 登录态检查(页面内出现扫码/验证码登录说明未登录)
        try:
            body_txt = page.evaluate(_TEXT_JS) or ""
            if ("扫码登录" in body_txt or "验证码登录" in body_txt or "手机号登录" in body_txt):
                raise XiaoyunqueScrapeError(
                    f"窗口[{window['name']}] 未登录小云雀，请先在该窗口手动登录 jianying.com"
                )
        except XiaoyunqueScrapeError:
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
            try:
                dom_urls = page.evaluate(_VIDEO_JS) or []
            except Exception:
                dom_urls = []
            latest_src = ""
            try:
                s = page.evaluate(_LATEST_CARD_SRC_JS)
                if s and s.startswith("http"):
                    latest_src = s
            except Exception:
                pass
            # 最新直链置顶; DOM文档序为权威排序, 网络捕获仅补充;
            # 同一视频的多CDN副本按key归并(优先jianying官方域名)
            merged, key_pos = [], {}
            for u in ([latest_src] if latest_src else []) + list(dom_urls) + net_urls:
                if not u or not u.startswith("http"):
                    continue
                if not _looks_media(u):
                    continue
                k = _media_key(u)
                if k in key_pos:
                    old = merged[key_pos[k]]
                    if "jianying.com" in u and "jianying.com" not in old:
                        merged[key_pos[k]] = u
                    continue
                key_pos[k] = len(merged)
                merged.append(u)
            videos = merged
            if videos:
                break
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

        # 文案材料优先取"最新回复"原文, 直接交AI整理; 失败退化全页文本
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
                f"[{window['name']}] 未定位到最新回复块[{how}]，退化为全页文本", "warning"
            )
            try:
                text = page.evaluate(_TEXT_JS) or ""
            except Exception:
                text = ""
            captions = [text.strip()] if text.strip() else []

        # 最新视频保持在列表头部; 发布永远取头部
        if latest_src:
            videos = [videos[0]] + list(reversed(videos[1:]))
        else:
            videos = list(reversed(videos))
        latest = videos[0] if videos else ""
        add_log(
            f"小云雀页面抓取完成: 视频 {len(videos)} 个, 候选文案 {len(captions)} 条 (尝试{attempt}轮)"
            + (f"; 最新视频: {latest[:80]}" if latest else "")
        )
        if not videos:
            raise XiaoyunqueScrapeError(
                "未能从页面提取到视频链接。请确认该会话已生成视频、抓取窗口已登录剪映，"
                "或适当调大【等待加载】秒数"
            )
        return videos, captions
    finally:
        if page is not None:
            try:
                page.close(timeout=5000)
            except Exception:
                pass
        # 清理残留标签页: 重复打开的同一聊天页
        try:
            src = (source_url or "").rstrip("/")
            for p in list(ctx.pages):
                u = (p.url or "")
                if src and u.rstrip("/") == src:
                    p.close(timeout=5000)
        except Exception:
            pass
        if pw is not None:
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
