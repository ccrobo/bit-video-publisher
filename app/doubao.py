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

_FIND_COVER_JS = """
() => {
  let img = [...document.querySelectorAll('img')].find(im => {
    const r = im.getBoundingClientRect();
    const cls = (im.className || '').toString();
    return cls.includes('cover') && r.width > 120 && r.height > 100;
  });
  if (!img) {
    img = [...document.querySelectorAll('[class*=block-video] img')].find(im => {
      const r = im.getBoundingClientRect();
      return r.width > 120 && r.height > 100;
    });
  }
  if (!img) return null;
  const r = img.getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
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


_SCROLL_COVER_INTO_VIEW_JS = """
() => {
  let img = [...document.querySelectorAll('img')].find(im => {
    const cls = (im.className || '').toString();
    return cls.includes('cover') && im.getBoundingClientRect().width > 120;
  });
  if (!img) {
    img = [...document.querySelectorAll('[class*=block-video] img')].find(im =>
      im.getBoundingClientRect().width > 120);
  }
  if (!img) return false;
  img.scrollIntoView({ block: 'center', behavior: 'instant' });
  return true;
}
"""


def _hover_click_cover(page):
    """把视频封面滚入视口后悬停并点击, 触发播放器初始化"""
    try:
        if not page.evaluate(_SCROLL_COVER_INTO_VIEW_JS):
            return False
        time.sleep(1.2)
        pos = page.evaluate(_FIND_COVER_JS)
        if not pos:
            return False
        vh = page.evaluate("() => window.innerHeight") or 900
        if not (10 < pos["y"] < vh - 10):
            page.evaluate(
                "(p) => { const d = document.querySelector('[class*=v_list_scroller], [class*=scroller]');"
                " if (d) { d.scrollTop += (p.y - window.innerHeight / 2); } }",
                pos,
            )
            time.sleep(1)
            pos = page.evaluate(_FIND_COVER_JS)
            if not pos or not (10 < pos["y"] < vh - 10):
                return False
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
        browser = pw.chromium.connect_over_cdp(cdp)
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
        videos = []
        attempt = 0
        clicked = False
        downloaded = False
        while time.time() < deadline:
            attempt += 1
            dom_urls = []
            for p in list(ctx.pages):
                try:
                    dom_urls.extend(p.evaluate(_VIDEO_JS) or [])
                except Exception:
                    continue
            # 以DOM文档顺序为权威排序(聊天页上旧下新); 网络捕获仅补充DOM中没有的,
            # 同一视频的多CDN副本按key归并(优先douyinvod官方域名), 避免打乱时间顺序
            merged, key_pos = [], {}
            for u in list(dom_urls) + net_urls:
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
            # 逐屏向上滚动, 让虚拟列表挂载历史视频消息
            moved = False
            try:
                moved = page.evaluate(_SCROLL_UP_JS)
            except Exception:
                pass
            if not moved and not clicked:
                # 已滚到顶部仍无视频: 悬停+点击视频封面触发播放
                if _hover_click_cover(page):
                    clicked = True
                    add_log("已点击视频封面卡片，等待播放加载...")
                    time.sleep(4)
                    continue
            if not moved and clicked and not downloaded:
                # 仍无直链则点卡片"下载"按钮, 迫使浏览器请求真实mp4地址
                if _click_download_btn(page):
                    downloaded = True
                    add_log("已点击下载按钮获取视频直链...")
                    time.sleep(5)
                    continue
            time.sleep(3)

        text = page.evaluate(_TEXT_JS)
        captions = _parse_captions(text)

        # 聊天页文档序上旧下新, 反转使最新在前; 注入/发布永远从列表头部取(即最下方最新的视频)
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
                page.close()
            except Exception:
                pass
        # 清理残留标签页: 之前发布留下的创作中心页 / 重复打开的同一聊天页
        try:
            src = (source_url or "").rstrip("/")
            for p in list(ctx.pages):
                u = (p.url or "")
                if "creator.douyin.com" in u or (src and u.rstrip("/") == src):
                    p.close()
        except Exception:
            pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        try:
            bitclient.close_window(window["id"])
            add_log(f"[{window['name']}] 抓取完成，窗口已关闭")
        except Exception:
            pass
