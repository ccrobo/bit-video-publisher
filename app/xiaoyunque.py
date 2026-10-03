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

# 聊天区为虚拟滚动列表: 打开页面后必须先滚到底部, 最新的消息块才会挂载进DOM
_SCROLL_BOTTOM_JS = """
() => {
  let moved = false;
  const els = [document.scrollingElement || document.documentElement,
               ...document.querySelectorAll('div')]
    .filter(d => d.scrollHeight > d.clientHeight + 50);
  els.forEach(d => {
    const before = d.scrollTop;
    d.scrollTop = d.scrollHeight;
    if (d.scrollTop > before + 5) moved = true;
  });
  return moved;
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
  // 选视口最靠下(=最新消息)的可见卡片
  let el = null, bestB = -1;
  for (const c of cards) {
    const r0 = c.getBoundingClientRect();
    if (r0.width <= 120 || r0.bottom <= 0 || r0.top >= window.innerHeight) continue;
    if (r0.bottom > bestB) { bestB = r0.bottom; el = c; }
  }
  if (!el) return null;
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
  const vis = [...document.querySelectorAll('video')].filter(v => {
    const r = v.getBoundingClientRect();
    return r.width > 60 && r.height > 40 && r.bottom > 0 && r.top < window.innerHeight;
  });
  if (!vis.length) return '';
  // 滚动到底后, 视口最靠下(文档序也最靠后)的视频就是最新消息里的; 排除顶部悬浮播放器
  let best = vis[0], bestBottom = -1e9;
  for (const v of vis) {
    const b = v.getBoundingClientRect().bottom;
    if (b > bestBottom) { bestBottom = b; best = v; }
  }
  const s = best.currentSrc || best.src || '';
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


# ---------------- 按任务编号定位指定回复(消费提问模式) ----------------
# 与豆包同构: 扫描含 任务编号ASK-xxx 的最小块并提取视频/文案。
# 视频可能在本块内, 也可能在【后续相邻兄弟气泡】(脚本气泡与视频气泡分离的场景)

# 编号块搜索通用过滤: 排除 fixed/sticky 悬浮区(侧边栏"最近对话"摘要也含编号,
# 会让编号定位/卡序判定全部歪掉), 只认主消息流内的块
_MARK_SCOPE_JS = """
  const inFixed = el => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const ps = getComputedStyle(n).position;
      if (ps === 'fixed' || ps === 'sticky') return true;
    }
    return false;
  };
"""

_MARK_SCAN_JS = """
() => {
  const RE = /ASK-\\d{8}-[A-Z0-9]{4,8}/g;
""" + _MARK_SCOPE_JS + """
  const hits = [];
  for (const el of document.querySelectorAll('div')) {
    const t = el.innerText || '';
    if (!t || t.length > 30000) continue;
    if (inFixed(el)) continue;
    RE.lastIndex = 0;
    const ms = t.match(RE);
    if (ms && ms.length) hits.push({el, ms});
  }
  // 只留最小块: 不再包含其他命中块的
  // 防御: 同时含多个不同编号的leaf是"消息列表大容器"塌缩产物, 内部消息级候选全被吞掉
  // -> 把leaf内每个编号的最小命中块补回候选; 塌缩leaf本身丢弃(全文混合多轮回复, 不能作文案)。
  // 注意不能全局按"每编号最小块"砍候选: 输入框草稿模板块比AI回复气泡更小, 会挤掉真回复
  const leaves = hits.filter(h => !hits.some(o => o !== h && h.el.contains(o.el)));
  const multi = leaves.filter(h => new Set(h.ms).size > 1);
  let scanList = leaves.filter(h => !multi.includes(h));
  if (multi.length) {
    const byMark = new Map();
    for (const h of hits) {
      if (!multi.some(m => m.el.contains(h.el) && m.el !== h.el)) continue;
      for (const mk of h.ms) {
        const cur = byMark.get(mk);
        if (!cur || cur.el.contains(h.el)) byMark.set(mk, h);
      }
    }
    for (const h of byMark.values()) if (!scanList.includes(h)) scanList.push(h);
  }
  const out = [];
  for (const h of scanList) {
    const videos = [];
    const push = u => { if (u && /^https?:\\/\\//.test(u) && !videos.includes(u)) videos.push(u); };
    const grab = root => {
      if (!root) return;
      root.querySelectorAll('video').forEach(v => {
        push(v.currentSrc || v.src || '');
        v.querySelectorAll('source').forEach(s => push(s.src));
      });
      root.querySelectorAll('a[href]').forEach(a => { if (/\\.mp4($|\\?)/.test(a.href)) push(a.href); });
    };
    grab(h.el);
    if (!videos.length) {
      // 向上爬到"消息级"容器: 逐层上升直到某层的下一个兄弟包含视频
      let node = h.el;
      for (let up = 0; up < 8 && !videos.length && node.parentElement; up++) {
        let sib = node.nextElementSibling;
        let hops = 0;
        while (sib && hops < 3 && !videos.length) {
          grab(sib);
          sib = sib.nextElementSibling;
          hops++;
        }
        if (!videos.length) node = node.parentElement;
      }
    }
    out.push({
      mark: h.ms[h.ms.length - 1],
      text: (() => {
        // 文本扩展: 最小编号块只有编号行, 向上爬到"消息级"取完整回复文案。
        // 爬升边界: 文本>8000字符 或 引入了第二个不同编号(混入其他回复) 即停
        let el = h.el, best = h.el.innerText || '';
        for (let up = 0; up < 12; up++) {
          const p = el.parentElement;
          if (!p || p === document.body) break;
          const t = p.innerText || '';
          if (!t || t.length > 8000) break;
          RE.lastIndex = 0;
          const uniq = [...new Set(t.match(RE) || [])];
          if (uniq.length > 1) break;
          if (t.length >= best.length) { if (t.length > best.length) best = t; el = p; } else break;
        }
        // 清洗: 爬升可能把用户提问(含提示词模板)也包进来。
        // 提示词要求AI回复首行原样输出"任务编号：ASK-XXXX", 用行首匹配定位AI回复起点,
        // 丢弃前面的用户提问与提示词模板(模板里的"格式：任务编号："在行中, 不匹配行首)。
        const headRe = /^\\s*(?:任务编号[:：]\\s*)?ASK-\\d{8}-[A-Z0-9]{4,8}/m;
        const hm = best.match(headRe);
        let s;
        if (hm) {
          s = best.slice(hm.index).trim();
        } else {
          // 兜底: AI未按要求首行输出编号时, 取最后一组结构化字段
          // (用户提示词模板在前, AI真实回复在后, 最后一组字段才是真实产出)
          s = best.trim();
          const fieldMarks = ['【标题】', '【描述】', '【新闻事件】', '【热点解读】', '【完整视频脚本】', '【旁白】', '【生成时间】', '【AI 任务产出'];
          let lastIdx = -1;
          for (const mk of fieldMarks) {
            const idx = s.lastIndexOf(mk);
            if (idx > lastIdx) lastIdx = idx;
          }
          if (lastIdx > 0) s = s.slice(lastIdx).trim();
        }
        return s;
      })(),
      videos,
    });
  }
  return out;
}
"""

# 视频可能是独立懒挂载卡(与编号文本气泡不同容器): 与豆包同构, 点击卡组+网络增量归属。

# 返回扫描到的编号按文档序(=时间序)排列的列表, 用于把第k个编号对应到第k组视频卡
_MARKS_ORDERED_JS = """
() => {
  const RE = /ASK-\\d{8}-[A-Z0-9]{4,8}/g;
""" + _MARK_SCOPE_JS + """
  const hits = [];
  for (const el of document.querySelectorAll('div')) {
    const t = el.innerText || '';
    if (!t || t.length > 30000) continue;
    if (inFixed(el)) continue;
    RE.lastIndex = 0;
    const ms = t.match(RE);
    if (ms && ms.length) hits.push({el});
  }
  // 每个编号取最小命中块: contains 剪枝会把"同时含多个编号的大容器"塌缩成
  // 唯一叶子, 导致其余编号全部丢失
  const byMark = new Map();
  for (const h of hits) {
    RE.lastIndex = 0;
    const ms = h.el.innerText.match(RE) || [];
    for (const mk of ms) {
      const cur = byMark.get(mk);
      if (!cur || cur.el.contains(h.el)) byMark.set(mk, {mark: mk, el: h.el});
    }
  }
  const uniq = [...byMark.values()];
  uniq.sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  return uniq.map(u => u.mark);
}
"""

# 按编号定位其对应的视频卡并返回点击坐标。
# 优先: 找到该编号文档序最后一次出现的块, 取其之后最近的一张视频卡
# (编号最后出现处的下方即本轮视频); 其他编号(如已消费任务)的视频卡不参与,
# 避免虚拟列表部分挂载时"第k张卡"错位。编号块未挂载或其后无卡时,
# 回退为按全局卡序取第k张。小云雀无稳定卡片类名: 可见<video>包裹容器优先, 其次视频特征封面图
_MARK_CARD_POINT_JS = """
([mk, k]) => {
""" + _MARK_SCOPE_JS + """
  const TIMEVAL = /【生成时间】[^【\\r\\n]*\\d{1,2}:\\d{2}/;
  const nFields = t => (t.match(/【(?:标题|描述|新闻事件|热点解读|完整视频脚本|旁白)】/g) || []).length;
  const hits = [];
  for (const el of document.querySelectorAll('div')) {
    const t = el.innerText || '';
    if (!t || t.length > 30000) continue;
    if (t.indexOf(mk) < 0) continue;
    if (inFixed(el)) continue;
    hits.push(el);
  }
  // 锚点 = 文档序最后一个"AI回复样"命中块(含真实生成时间或>=2个字段标记)。
  // 不做 contains 剪枝(大容器塌缩会让锚点失效), 也不用"最后出现"——
  // 输入框草稿/提问回显(模板文本)也含编号且位置最靠下, 会把锚点带进无卡区域
  let anchor = null;
  const replies = hits.filter(el => TIMEVAL.test(el.innerText) || nFields(el.innerText) >= 2);
  const pool = replies.length ? replies : hits;
  if (pool.length) {
    anchor = pool[0];
    for (const l of pool) {
      if (anchor.compareDocumentPosition(l) & Node.DOCUMENT_POSITION_FOLLOWING) anchor = l;
    }
  }
  let cards = [...document.querySelectorAll('video')]
    .map(v => v.closest('[class*="card"],[class*="video"],[class*="item"],[class*="message"],[class*="agent"]') || v)
    .filter(el => el.getBoundingClientRect().width > 120);
  if (!cards.length) {
    cards = [...document.querySelectorAll('img')]
      .filter(im => /video|cover|thumb|snapshot/i.test(im.src || ''))
      .map(im => im.closest('[class*="card"],[class*="video"],[class*="item"]') || im.parentElement)
      .filter(Boolean)
      .filter(el => el.getBoundingClientRect().width > 120);
  }
  if (!cards.length) return null;
  let el = null;
  if (anchor) {
    const after = cards
      .filter(c => anchor.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (after.length) el = after[0];
  }
  if (!el) {
    el = (k >= 0 && k < cards.length) ? cards[k] : null;
  }
  if (!el) return null;
  el.scrollIntoView({block: 'center', behavior: 'instant'});
  const r = el.getBoundingClientRect();
  if (r.width < 40) return null;
  return {x: r.x + r.width / 2,
          y: Math.max(20, Math.min(r.y + r.height / 2, window.innerHeight - 20)),
          w: r.width | 0};
}
"""

# 从指定坐标处的视频卡DOM直取视频直链: elementFromPoint -> 最近卡片容器 -> video/source/a.mp4。
# 卡是按编号锚定后选中的, 归属明确; 网络监听会把同页其他卡促发的请求也记进来, 只作兜底
_CARD_SRC_AT_POINT_JS = """
([x, y]) => {
  const urls = [];
  const push = u => { if (u && /^https?:\\/\\//.test(u) && !urls.includes(u)) urls.push(u); };
  const grab = root => {
    if (!root) return;
    if (root.tagName === 'VIDEO') push(root.currentSrc || root.src || '');
    root.querySelectorAll('video').forEach(v => {
      push(v.currentSrc || v.src || '');
      v.querySelectorAll('source').forEach(s => push(s.src));
    });
    root.querySelectorAll('a[href]').forEach(a => { if (/\\.mp4($|\\?)/.test(a.href)) push(a.href); });
  };
  const ep = document.elementFromPoint(x, y);
  if (!ep) return '';
  if (ep.tagName === 'VIDEO') { grab(ep); return urls[0] || ''; }
  grab(ep.closest('[class*="card"],[class*="video"],[class*="item"],[class*="message"],[class*="agent"]'));
  return urls[0] || '';
}
"""


def _merge_mark_replies(items):
    """同编号多条扫描结果归并:
    编号会同时出现在[用户提问回显]和[AI回复]中, 结构化字段分高的(AI回复)优先,
    平分时才取文本更长的版本"""
    merged = {}
    order = []
    for it in items or []:
        mk = it.get("mark")
        if not mk:
            continue
        if mk not in merged:
            merged[mk] = {"mark": mk, "text": it.get("text") or "", "videos": list(it.get("videos") or [])}
            order.append(mk)
            continue
        cur = merged[mk]
        if len(it.get("videos") or []) > len(cur["videos"]):
            cur["videos"] = list(it.get("videos") or [])
        nt = it.get("text") or ""
        ct = cur.get("text") or ""
        if _reply_score(nt) > _reply_score(ct) or (
            _reply_score(nt) == _reply_score(ct) and len(nt) > len(ct)
        ):
            cur["text"] = nt
    return [merged[k] for k in order]


# 提问回复的结构化字段特征: 含这些标记越多越可能是"AI回复"(而非回显的提问原文)
_REPLY_FIELD_MARKS = (
    "【标题】", "【描述】", "【新闻事件】", "【热点解读】",
    "【完整视频脚本】", "【旁白】",
)

# AI回复专属强特征: 首行原样输出任务编号(提示词强制要求); 生成时间为真实时间值而非说明文字
_REPLY_HEAD_RE = re.compile(r"^\s*(?:任务编号[:：]\s*)?ASK-\d{8}-[A-Z0-9]{4,8}", re.M)
_REPLY_TIMEVAL_RE = re.compile(r"【生成时间】[^【\r\n]*\d{1,2}:\d{2}")


def _reply_score(text):
    """区分AI回复与提问回显: 回显也会含全部字段名(模板), 因此用AI回复专属特征加权"""
    t = text or ""
    s = sum(1 for m in _REPLY_FIELD_MARKS if m in t)
    if _REPLY_HEAD_RE.search(t):
        s += 10
    if _REPLY_TIMEVAL_RE.search(t):
        s += 5
    return s


_ASK_LINE_RE = re.compile(r"ASK-\d{8}-[A-Z0-9]{4,8}")


# 提问模板特征(输入框草稿/提问回显都会带, 真实AI回复不含)
_THIN_TEMPLATE_HINTS = ("系统附加要求", "归档追踪", "必须在回复正文的最前面")


def _thin_text(text, min_len=30):
    """回复文本是否单薄: 剥离含任务编号的行后有效内容过短。

    场景: 编号所在叶子块向上爬升时被其他编号/超长文本阻断, 只定位到编号行
    而没拿到完整回复(带【标题】【描述】的正文可能在未挂载的历史消息里)。
    另: 输入框草稿/提问回显的模板文本(系统附加要求等)视为单薄, 不算有效回复。
    """
    body = "\n".join(
        ln for ln in (text or "").splitlines() if not _ASK_LINE_RE.search(ln)
    )
    if len(body.strip()) < min_len:
        return True
    return "【标题】" not in text and any(h in text for h in _THIN_TEMPLATE_HINTS)


def find_xiaoyunque_replies_by_marks(bitclient, settings, source_url, window, marks, wait_seconds=15, keep_open=False):
    """打开小云雀聊天页, 定位包含指定任务编号的回复块。

    返回 [{"mark", "text", "videos"}](仅含找到的编号; 未找到/暂无视频的不返回)。
    keep_open=True: 成功时保留窗口并在 window["_cdp_addr"] 附带调试地址, 供发布阶段复用。
    """
    addr = bitclient.open_window(window["id"])
    cdp = addr if addr.startswith("http") else "http://" + addr
    ok = False
    pw = None
    page = None
    ctx = None
    browser = None
    try:
        add_log(f"[{window['name']}] 打开小云雀聊天页消费提问(查找 {len(marks)} 个编号): {source_url}")
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        # 监听网络响应: 视频卡是懒挂载占位卡, 点击播放/下载后直链才流经网络
        net_urls = []  # [(time, url)]

        def _on_response(resp):
            try:
                u = resp.url or ""
                ct = (resp.headers or {}).get("content-type", "") or ""
            except Exception:
                return
            if _looks_media(u) or ct.lower().startswith("video/"):
                net_urls.append((time.time(), u))

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
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        want = [m for m in marks if m]
        found = {}
        deadline = time.time() + max(20, int(wait_seconds or 15)) + 30
        rounds = 0

        def _complete(m):
            f = found.get(m) or {}
            return bool(f.get("videos")) and not _thin_text(f.get("text"))

        while time.time() < deadline and not all(_complete(m) for m in want):
            rounds += 1
            # 虚拟列表: 先滚到底让最新消息挂载
            try:
                page.evaluate(_SCROLL_BOTTOM_JS)
            except Exception:
                pass
            time.sleep(1)
            try:
                items = page.evaluate(_MARK_SCAN_JS) or []
            except Exception:
                items = []
            for it in _merge_mark_replies(items):
                mk = it["mark"]
                if mk not in want:
                    continue
                old = found.get(mk)
                if not old:
                    found[mk] = dict(it)
                elif _reply_score(it["text"]) > _reply_score(old["text"]) or (
                    _reply_score(it["text"]) == _reply_score(old["text"])
                    and (len(it["videos"]) > len(old["videos"])
                         or len(it["text"]) > len(old["text"]))
                ):
                    src_reply = it if _reply_score(it["text"]) >= _reply_score(old["text"]) else old
                    found[mk] = {
                        "mark": mk,
                        "text": src_reply["text"],
                        "videos": it["videos"] or old["videos"],
                    }
            remaining = [m for m in want if not _complete(m)]
            if not remaining:
                break
            # 已定位文本但缺视频: 视频卡是独立懒挂载卡, 与编号文本气泡不属同一容器。
            # 编号时间序与页面视频卡组顺序一一对应 -> 按文档序把第k个待消费编号对到第k张视频卡,
            # 真实鼠标点击促发挂载/播放, 网络监听捕获的增量直链归属该编号。
            # 缺完整文本(仅拿到编号行)时: 滚动加载历史消息, 重扫后由归并逻辑补齐文本。
            woke = False
            for m in remaining:
                if m not in found:
                    continue
                if found[m]["videos"]:
                    continue
                try:
                    ordered = page.evaluate(_MARKS_ORDERED_JS) or []
                except Exception:
                    ordered = []
                idx = ordered.index(m) if m in ordered else 0
                try:
                    pos = page.evaluate(_MARK_CARD_POINT_JS, [m, idx])
                except Exception:
                    pos = None
                if not pos:
                    continue
                try:
                    # hover促发懒挂载后从卡DOM直取直链: 卡按编号锚定, 归属明确
                    page.mouse.move(pos["x"], pos["y"])
                    time.sleep(0.8)
                    card_src = page.evaluate(_CARD_SRC_AT_POINT_JS, [pos["x"], pos["y"]])
                    if not card_src:
                        time.sleep(1.5)
                        card_src = page.evaluate(_CARD_SRC_AT_POINT_JS, [pos["x"], pos["y"]])
                    if card_src:
                        found[m]["videos"] = [card_src]
                        add_log(f"[{window['name']}] 编号[{m}] 从编号下方视频卡直取得直链(卡内匹配)")
                        woke = True
                        continue
                except Exception:
                    pass
                cursor = time.time()
                try:
                    page.mouse.click(pos["x"], pos["y"])
                    woke = True
                    time.sleep(5)
                    delta = [u for t, u in net_urls if t >= cursor]
                    if delta and not found[m]["videos"]:
                        seen_ = set()
                        uniq = []
                        for u in delta:
                            k_ = _media_key(u)
                            if k_ not in seen_:
                                seen_.add(k_)
                                uniq.append(u)
                        found[m]["videos"] = uniq[:2]
                        add_log(f"[{window['name']}] 点击编号[{m}]的视频卡后捕获到 {len(found[m]['videos'])} 个视频直链")
                    elif rounds >= 2 and not found[m]["videos"]:
                        dl = page.evaluate(_FIND_DOWNLOAD_JS)
                        if dl:
                            cursor2 = time.time()
                            page.mouse.click(dl["x"], dl["y"])
                            add_log(f"[{window['name']}] 已点击下载按钮获取编号[{m}]的视频直链...")
                            time.sleep(5)
                            delta = [u for t, u in net_urls if t >= cursor2]
                            if delta and not found[m]["videos"]:
                                found[m]["videos"] = delta[:2]
                except Exception:
                    pass
            if woke:
                continue
            # 更早的历史编号需要向上滚加载
            try:
                page.evaluate(_SCROLL_UP_JS)
            except Exception:
                pass
            time.sleep(3)

        out = [found[m] for m in want if m in found]
        with_vid = sum(1 for m in want if (found.get(m) or {}).get("videos"))
        add_log(f"[{window['name']}] 编号定位完成: 找到 {len(out)}/{len(want)} 个(带视频 {with_vid} 个)")
        ok = True
        return out
    finally:
        if page is not None:
            try:
                page.close(timeout=5000)
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
        if keep_open and ok:
            # 保留窗口, 附带调试地址供发布阶段直接复用(省一次关开窗)
            window["_cdp_addr"] = cdp
            add_log(f"[{window['name']}] 编号定位完成，窗口保持打开等待发布阶段复用")
        else:
            try:
                bitclient.close_window(window["id"])
            except Exception:
                pass


_MEDIA_HINTS = (
    ".mp4", "/video/tos/", "douyinvod", "zjcdn", "ixigua",
    "aweme/v1/play", "jyvod", "vod.jianying", "bytetos.com/obj/video",
    # 小云雀真实直链无.mp4后缀: v26-xyq-video.jianying.com/<sign>/<ts>/video/n/everphoto-jianying-assets/<id>/?a=...
    # 注意: 不能用 everphoto-jianying-assets 作特征——douyinpic 封面图路径同样含它
    "video.jianying.com", "/video/n/",
    "download=true",
)

_MEDIA_BAD_HOSTS = (
    "passport", "/api/", "sts2",
    # 抖音图床CDN: 小云雀视频封面图走此域名, 路径同样含 everphoto-jianying-assets
    "douyinpic",
)


def _looks_media(url):
    low = url.lower()
    # 强特征(.mp4/显式下载参数)优先于坏名单——小云雀下载接口形如 /api/material/<id>?download=true&filename=x.mp4
    if ".mp4" in low or "download=true" in low:
        return True
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


def scrape_xiaoyunque_chat(bitclient, settings, source_url, window, wait_seconds=15, keep_open=False):
    """在指定窗口中打开小云雀聊天页, 返回 (视频URL列表[最新在前], 候选文案列表)。

    keep_open=True: 成功时保留窗口并在 window["_cdp_addr"] 附带调试地址, 供发布阶段复用。
    """
    addr = bitclient.open_window(window["id"])
    cdp = addr if addr.startswith("http") else "http://" + addr
    ok = False
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
            # 虚拟列表: 每轮先滚到底, 促使最新消息块挂载进DOM
            try:
                page.evaluate(_SCROLL_BOTTOM_JS)
            except Exception:
                pass
            time.sleep(1)
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
        ok = True
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
        if keep_open and ok:
            # 保留窗口, 附带调试地址供发布阶段直接复用(省一次关开窗)
            window["_cdp_addr"] = cdp
            add_log(f"[{window['name']}] 抓取完成，窗口保持打开等待发布阶段复用")
        else:
            try:
                bitclient.close_window(window["id"])
                add_log(f"[{window['name']}] 抓取完成，窗口已关闭")
            except Exception:
                pass
