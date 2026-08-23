"""通过 Playwright(CDP) 接管比特浏览器窗口, 在抖音创作中心自动发布视频"""
import re
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from .logs import add_log
from .store import BASE_DIR

_publish_lock = threading.Lock()


class PublishError(Exception):
    pass


def publish_once(bitclient, settings, window, video_path, caption):
    """打开窗口 -> 进入创作中心 -> 上传 -> 填文案 -> 发布"""
    with _publish_lock:
        _publish(bitclient, settings, window, video_path, caption)


def _publish(bitclient, settings, window, video_path, caption):
    shot_dir = BASE_DIR / "data" / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    sel = settings.get("selectors") or {}
    file_sel = sel.get("file_input") or "input[type=file]"
    editor_sel = sel.get("editor") or "div[contenteditable=true]"
    pub_sel = sel.get("publish_button") or '//button[normalize-space(.)="发布"]'
    timeout = int(settings.get("publish_timeout") or 180)
    upload_url = settings.get("upload_url")
    close_after = bool(settings.get("close_window_after_publish", True))
    ts = time.strftime("%Y%m%d-%H%M%S")
    addr = None
    pw = None
    page = None
    try:
        addr = bitclient.open_window(window["id"])
        cdp = addr if addr.startswith("http") else "http://" + addr
        add_log(f"[{window['name']}] 窗口已打开，正在连接内核 {cdp}")
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto(upload_url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        if "/login" in (page.url or ""):
            raise PublishError("该窗口未登录抖音创作中心，请先在比特浏览器中手动登录一次")
        add_log(f"[{window['name']}] 已进入抖音创作中心，开始注入视频")

        file_loc = page.locator(file_sel).first
        file_loc.wait_for(state="attached", timeout=30000)
        file_loc.set_input_files(str(video_path))
        add_log(f"[{window['name']}] 已选择视频 {Path(video_path).name}，等待上传完成...")
        _wait_upload_done(page, timeout)

        editor = page.locator(editor_sel).first
        editor.wait_for(state="visible", timeout=30000)
        editor.click()
        page.keyboard.insert_text(caption)
        # 标题为尽力填写(可选成功)
        try:
            ti = page.locator('input[placeholder*="标题"]').first
            if ti.count() > 0:
                ti.fill(caption.splitlines()[0][:30])
        except Exception:
            pass

        # 等待平台内容检测(检测中)结束, 超时则尝试直接发布(会弹确认框)
        _wait_check_done(page, timeout, window["name"])

        btn = page.locator(pub_sel).first
        btn.wait_for(state="visible", timeout=30000)
        deadline = time.time() + timeout
        while time.time() < deadline and btn.is_disabled():
            time.sleep(2)
        if btn.is_disabled():
            raise PublishError("发布按钮持续不可用（视频可能仍在转码），已放弃本次发布")
        btn.click()
        add_log(f"[{window['name']}] 已点击发布按钮")

        # 处理二次确认弹窗(如: 视频检测中是否继续发布)
        _handle_confirm_dialog(page)

        # 确认真正发布成功后才算完成
        if _wait_publish_success(page, max(90, timeout)):
            add_log(f"[{window['name']}] 视频发布成功: {Path(video_path).name}")
        else:
            shot = shot_dir / f"fail-{ts}-{window['id']}.png"
            try:
                page.screenshot(path=str(shot))
            except Exception:
                pass
            raise PublishError(f"点击发布后未确认到发布成功，已截图 {shot.name} 供排查")
    except PublishError:
        # 失败时保留现场截图
        if page is not None:
            try:
                page.screenshot(path=str(shot_dir / f"fail-{ts}-{window['id']}.png"))
            except Exception:
                pass
        raise
    except Exception as e:
        if page is not None:
            try:
                page.screenshot(path=str(shot_dir / f"err-{ts}.png"))
            except Exception:
                pass
        raise PublishError(str(e)) from e
    finally:
        # 先关闭注入的上传页, 避免下次打开窗口时恢复该标签页产生干扰请求
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        if close_after and addr:
            try:
                bitclient.close_window(window["id"])
                add_log(f"[{window['name']}] 窗口已关闭")
            except Exception:
                pass


_UPLOAD_DONE_PAT = re.compile("重新上传|上传成功|上传完成|预览转码中")


def _wait_upload_done(page, timeout):
    """等待上传完成。抖音完成后会显示'重新上传'或'上传成功'/'预览转码中', 并无固定文案"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.get_by_text(_UPLOAD_DONE_PAT).count() > 0:
                return
        except Exception:
            pass
        time.sleep(2)
    raise PublishError(
        "视频上传超时: 未检测到完成标志(重新上传/上传成功/预览转码中)，"
        "请检查窗口网络或到系统设置调大【发布超时时间】"
    )


def _wait_check_done(page, timeout, win_name):
    """等待平台内容检测('检测中'进度)结束; 超时则记录警告并继续发布"""
    deadline = time.time() + min(timeout, 300)
    while time.time() < deadline:
        try:
            checking = page.get_by_text(re.compile("检测中|审核中")).count()
            passed = page.get_by_text(re.compile("检测通过|审核通过|检测完成")).count()
            if checking == 0 or passed > 0:
                return
        except Exception:
            return
        time.sleep(2)
    add_log(f"[{win_name}] 内容检测仍在进行，尝试直接发布(如弹确认框将自动确认)", "warning")


_CONFIRM_TEXTS = ("确认发布", "继续发布", "确定发布", "仍然发布", "确认", "确定")


def _handle_confirm_dialog(page, wait=10):
    """点击发布后若出现二次确认弹窗，自动点击确认类按钮"""
    deadline = time.time() + wait
    while time.time() < deadline:
        for t in _CONFIRM_TEXTS:
            try:
                btn = page.locator(f'button:has-text("{t}")')
                if btn.count() > 0 and btn.first.is_visible():
                    txt = btn.first.inner_text().strip()
                    if t in txt:
                        btn.first.click()
                        add_log(f"已点击确认弹窗按钮: {txt}")
                        time.sleep(1)
                        return True
            except Exception:
                pass
        time.sleep(1)
    return False


def _wait_publish_success(page, timeout):
    """确认发布成功: 出现'发布成功'提示 或 页面跳转离开上传页"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.get_by_text(re.compile("发布成功")).count() > 0:
                return True
        except Exception:
            pass
        try:
            u = page.url or ""
            if u and "/content/upload" not in u:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False
