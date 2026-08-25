"""AI 提问任务: 在比特浏览器窗口中打开指定平台的对话框URL, 自动输入提示词并发送"""
import datetime as dt
import time

from playwright.sync_api import sync_playwright

from .logs import add_log

ASK_PLATFORMS = [
    {"id": "doubao", "name": "豆包", "ready": True},
    {"id": "xiaoyunque", "name": "小云雀", "ready": True},
    {"id": "workbuddy", "name": "WorkBuddy", "ready": False},
    {"id": "qianwen", "name": "通义千问", "ready": False},
    {"id": "yuanbao", "name": "腾讯元宝", "ready": False},
    {"id": "yuque", "name": "语雀", "ready": False},
    {"id": "keling", "name": "可灵", "ready": False},
    {"id": "jimeng", "name": "即梦", "ready": False},
]

# 定位聊天输入框: 优先 textarea, 其次 contenteditable 编辑区(豆包为 tiptap ProseMirror, 高度约24px)
_FIND_INPUT_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 80 && r.height >= 20; };
  const tas = [...document.querySelectorAll('textarea')].filter(vis);
  if (tas.length) {
    const t = tas[tas.length - 1];
    const r = t.getBoundingClientRect();
    return { type: 'textarea', x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }
  const eds = [...document.querySelectorAll('[contenteditable="true"]')]
    .filter(e => e.getBoundingClientRect().height >= 20);
  if (eds.length) {
    const e = eds[eds.length - 1];
    const r = e.getBoundingClientRect();
    return { type: 'editor', x: r.x + r.width / 2, y: r.y + r.height / 2 };
  }
  return null;
}
"""

# 输入框是否已有内容(用于判断提示词是否注入成功)
_INPUT_HAS_TEXT_JS = """
() => {
  const tas = [...document.querySelectorAll('textarea')].filter(t => t.getBoundingClientRect().height >= 20);
  if (tas.length) return (tas[tas.length - 1].value || '').trim().length > 0;
  const eds = [...document.querySelectorAll('[contenteditable="true"]')].filter(e => e.getBoundingClientRect().height >= 20);
  if (eds.length) return (eds[eds.length - 1].innerText || '').trim().length > 0;
  return null;
}
"""

# 发送按钮(兜底: Enter 无效时点击); 兼容图标按钮的 aria-label/id 含 send
_FIND_SEND_JS = """
() => {
  const btns = [...document.querySelectorAll('button, [role=button], div[class*=send], span')]
    .filter(b => {
      const t = ((b.getAttribute('aria-label') || '') + ' ' + (b.id || '') + ' ' + (b.innerText || '')).trim();
      if (!/发送|send/i.test(t)) return false;
      const r = b.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });
  if (!btns.length) return null;
  const b = btns[btns.length - 1];
  const r = b.getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
}
"""

# 定位豆包「视频生成」Tab/模式切换按钮: 优先返回其可点击的包裹元素坐标
_FIND_VIDEO_MODE_JS = """
() => {
  const keywords = ['视频生成', '生成视频', '视频创作'];
  const vis = el => {
    if (!el || el.nodeType !== 1) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || 1) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 10 && r.height > 10 && r.left < window.innerWidth && r.top < window.innerHeight;
  };
  const hasText = (el, kws) => {
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, '').trim();
    if (!t) return false;
    for (const k of kws) if (t.includes(k)) return true;
    return false;
  };
  // 候选: 带文字匹配的所有可见元素
  const candidates = [...document.querySelectorAll('div, button, a, span, [role=tab], [role=button], li')]
    .filter(el => vis(el) && hasText(el, keywords));
  if (!candidates.length) return null;
  // 找最小文本承载元素（叶子元素更精准），再往上找第一个可点击容器
  candidates.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  let el = candidates[0];
  let anchor = el;
  for (let depth = 0; depth < 10 && el && el !== document.body; depth++) {
    const r = el.getBoundingClientRect();
    if (r.width > 20 && r.height > 20) { anchor = el; }
    const cursor = window.getComputedStyle(el).cursor;
    const role = el.getAttribute && (el.getAttribute('role') || '');
    if (cursor === 'pointer' || el.tagName === 'BUTTON' || el.tagName === 'A'
        || role === 'button' || role === 'tab' || (el.onclick != null)) {
      const r2 = el.getBoundingClientRect();
      if (r2.width > 20 && r2.height > 20) anchor = el;
      break;
    }
    el = el.parentElement;
  }
  const r = anchor.getBoundingClientRect();
  if (!(r.width > 10 && r.height > 10)) return null;
  return { x: r.x + r.width / 2, y: r.y + r.height / 2, label: (anchor.innerText || '').trim().slice(0, 20) };
}
"""


# ---------------- 提示词变量渲染 ----------------

def render_prompt_vars(text, ask_vars, now=None, window=None):
    """根据 ask_vars（开关列表）对提示词中的 {当前时间}/{当前日期}/{窗口ID}/{窗口名} 做替换。

    未启用的变量不替换，保留占位符，避免意外覆盖用户字面文案。
    now/dt 为注入的当前时间，不传则取 now=datetime.now()。
    """
    if not text:
        return text or ""
    ask_vars = set(ask_vars or [])
    if not ask_vars:
        return text
    if now is None:
        now = dt.datetime.now()
    w = window or {}
    mapping = {
        "current_time": ("{当前时间}", now.strftime("%Y-%m-%d %H:%M:%S")),
        "current_date": ("{当前日期}", now.strftime("%Y-%m-%d")),
        "window_id": ("{窗口ID}", str(w.get("id") or "")),
        "window_name": ("{窗口名}", str(w.get("name") or "")),
    }
    out = text
    for key, (placeholder, value) in mapping.items():
        if key in ask_vars and placeholder in out:
            out = out.replace(placeholder, value)
    return out


def read_chat_text(bitclient, chat_url, window, settle_seconds=6):
    """打开窗口的对话框URL读取页面可见文本(不发送任何内容)。

    用于AI提问任务在发送前让推理模型判断今日已提问次数。"""
    addr = bitclient.open_window(window["id"])
    cdp = addr if addr.startswith("http") else "http://" + addr
    pw = None
    page = None
    browser = None
    ctx = None
    try:
        add_log(f"[{window['name']}] 正在打开对话页检查今日提问情况: {chat_url}")
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        cur = page.url or ""
        if "/login" in cur or "passport" in cur:
            raise RuntimeError(f"窗口[{window['name']}] 未登录该平台，请先在该窗口手动登录")
        try:
            body_txt = page.evaluate("() => document.body.innerText") or ""
            if ("扫码登录" in body_txt or "验证码登录" in body_txt or "手机号登录" in body_txt):
                raise RuntimeError(f"窗口[{window['name']}] 未登录该平台，请先在该窗口手动登录")
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(max(3, int(settle_seconds)))
        try:
            return page.evaluate("() => document.body.innerText") or ""
        except Exception:
            return ""
    finally:
        if page is not None:
            try:
                page.close(timeout=5000)
            except Exception:
                pass
        try:
            src = (chat_url or "").rstrip("/")
            for p in list(ctx.pages):
                u = (p.url or "")
                if src and u.rstrip("/") == src:
                    p.close(timeout=5000)
        except Exception:
            pass
        if pw is not None:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
        try:
            bitclient.close_window(window["id"])
            add_log(f"[{window['name']}] 对话页读取完成，窗口已关闭")
        except Exception:
            pass


def ask_in_chat(bitclient, settings, chat_url, window, prompt, wait_seconds=30,
                ask_vars=None, video_mode=False):
    """打开窗口的对话框URL并发送提示词; 返回 True 表示已发出

    - ask_vars: 开关列表, 支持 current_time / current_date / window_id / window_name
      将把提示词里的 {当前时间}/{当前日期}/{窗口ID}/{窗口名} 替换为实际值后再发送
    - video_mode: 豆包专用, 进入页面后先尝试点击 "视频生成" Tab/模式切换, 再注入提示词
    """
    addr = bitclient.open_window(window["id"])
    cdp = addr if addr.startswith("http") else "http://" + addr
    pw = None
    page = None
    browser = None
    try:
        add_log(f"[{window['name']}] 正在打开对话框提问: {chat_url}")
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp, timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        cur = page.url or ""
        if "/login" in cur or "passport" in cur:
            raise RuntimeError(f"窗口[{window['name']}] 未登录该平台，请先在该窗口手动登录")
        try:
            body_txt = page.evaluate("() => document.body.innerText") or ""
            if ("扫码登录" in body_txt or "验证码登录" in body_txt or "手机号登录" in body_txt):
                raise RuntimeError(f"窗口[{window['name']}] 未登录该平台，请先在该窗口手动登录")
        except RuntimeError:
            raise
        except Exception:
            pass

        # [需求2] 豆包视频生成模式: 注入提示词前先切到 Tab
        if video_mode:
            vpos = None
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    vpos = page.evaluate(_FIND_VIDEO_MODE_JS)
                except Exception:
                    vpos = None
                if vpos:
                    break
                time.sleep(1)
            if vpos:
                try:
                    page.mouse.click(vpos["x"], vpos["y"])
                    time.sleep(1.2)
                    add_log(f"[{window['name']}] 已切换到豆包「{vpos.get('label') or '视频生成'}」模式")
                except Exception as e:
                    add_log(f"[{window['name']}] 切换视频生成模式失败({e})，继续按普通对话执行", "warning")
            else:
                add_log(f"[{window['name']}] 未找到豆包「视频生成」Tab，按普通对话执行（如首次使用可先手动切一次）", "warning")

        # 等待输入框出现
        pos = None
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                pos = page.evaluate(_FIND_INPUT_JS)
            except Exception:
                pos = None
            if pos:
                break
            time.sleep(1.5)
        if not pos:
            raise RuntimeError(f"窗口[{window['name']}] 页面未找到聊天输入框，请确认链接是平台对话页")

        # [需求1] 提示词变量渲染（仅替换勾选的占位符）
        final_prompt = render_prompt_vars(prompt, ask_vars or [], window=window)
        if final_prompt != prompt:
            add_log(f"[{window['name']}] 提示词已补充变量（启用项: {', '.join(sorted(set(ask_vars or [])))}），共 {len(final_prompt)} 字")

        # 注入提示词
        page.mouse.click(pos["x"], pos["y"])
        time.sleep(0.6)
        page.keyboard.insert_text(final_prompt)
        time.sleep(0.5)
        has_text = None
        try:
            has_text = page.evaluate(_INPUT_HAS_TEXT_JS)
        except Exception:
            pass

        # 回车发送; 未清空则点发送按钮兜底
        sent = False
        try:
            page.keyboard.press("Enter")
            time.sleep(1.5)
            has_text2 = None
            try:
                has_text2 = page.evaluate(_INPUT_HAS_TEXT_JS)
            except Exception:
                pass
            sent = (has_text is True and has_text2 is False) or (has_text2 is False and has_text is None)
        except Exception:
            pass
        if not sent:
            spos = None
            try:
                spos = page.evaluate(_FIND_SEND_JS)
            except Exception:
                spos = None
            if spos:
                try:
                    page.mouse.click(spos["x"], spos["y"])
                    sent = True
                    add_log(f"[{window['name']}] 已通过发送按钮提交")
                except Exception:
                    pass
        if not sent:
            # 无法确认时以输入框内容已注入为准, 不中断流程
            if has_text:
                add_log(f"[{window['name']}] 提示词已注入，回车/发送按钮结果未确认，按已发送处理", "warning")
            else:
                raise RuntimeError(f"窗口[{window['name']}] 提示词发送失败，请检查页面状态")

        wait_s = max(5, int(wait_seconds or 30))
        add_log(f"[{window['name']}] 已发送提示词（{len(final_prompt)}字），等待生成 {wait_s} 秒...")
        time.sleep(wait_s)
        return True
    finally:
        if page is not None:
            try:
                page.close(timeout=5000)
            except Exception:
                pass
        try:
            src = (chat_url or "").rstrip("/")
            for p in list(ctx.pages):
                u = (p.url or "")
                if src and u.rstrip("/") == src:
                    p.close(timeout=5000)
        except Exception:
            pass
        if pw is not None:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
        try:
            bitclient.close_window(window["id"])
            add_log(f"[{window['name']}] 提问完成，窗口已关闭")
        except Exception:
            pass
