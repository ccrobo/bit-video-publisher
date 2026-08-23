"""比特浏览器本地 API 客户端 (默认 http://127.0.0.1:54345)"""
import os
import subprocess
import time

import httpx

from .logs import add_log


class BitBrowserError(Exception):
    pass


# 本地 API 专用客户端: 绕过系统代理，避免 127.0.0.1 请求被代理拦截
_client = httpx.Client(trust_env=False, timeout=30)


class BitClient:
    def __init__(self, settings):
        self.api = (settings.get("bitbrowser_api") or "http://127.0.0.1:54345").rstrip("/")
        self.headers = {}
        key = settings.get("bitbrowser_api_key")
        if key:
            self.headers["x-api-key"] = key

    # ---------- 基础 ----------
    def healthy(self):
        # 新版为 POST /health，旧版为 GET /health，两种都兼容
        for method in ("post", "get"):
            try:
                r = getattr(_client, method)(self.api + "/health", headers=self.headers, timeout=3)
                if r.status_code == 200 and bool(r.json().get("success")):
                    return True
            except Exception:
                continue
        return False

    def ensure_running(self, exe_path=None, timeout=120):
        if self.healthy():
            return True
        exe = exe_path
        if not exe or not os.path.exists(exe):
            raise BitBrowserError(f"未找到比特浏览器程序: {exe}，请到【系统设置】配置正确安装路径")
        add_log(f"比特浏览器未运行，正在本地启动: {exe}")
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
        except Exception as e:
            raise BitBrowserError(f"启动比特浏览器失败: {e}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            if self.healthy():
                add_log("比特浏览器已启动并就绪")
                return True
        raise BitBrowserError("比特浏览器启动超时，请确认安装路径与端口设置")

    def _post(self, path, payload):
        r = _client.post(self.api + path, json=payload, headers=self.headers)
        try:
            data = r.json()
        except Exception:
            raise BitBrowserError(f"比特浏览器接口返回异常: HTTP {r.status_code} {r.text[:200]}")
        if not data.get("success"):
            raise BitBrowserError(data.get("msg") or f"比特浏览器接口调用失败: {path}")
        return data.get("data")

    # ---------- 分组 / 窗口 ----------
    def list_groups(self):
        out = []
        page = 0
        while True:
            data = self._post("/group/list", {"page": page, "pageSize": 100}) or {}
            lst = data.get("list") or []
            for g in lst:
                # 兼容不同版本: 新版为 id/groupName, 旧版为 groupId/groupName
                gid = str(g.get("groupId") or g.get("id") or "")
                out.append({"id": gid, "name": g.get("groupName") or "未命名分组"})
            if len(lst) < 100 or page > 50:
                break
            page += 1
        return out

    def list_windows(self):
        wins = []
        page = 0
        while True:
            data = None
            last_err = None
            for pth in ("/browser/list/paged", "/browser/list"):
                try:
                    data = self._post(pth, {"page": page, "pageSize": 100})
                    break
                except BitBrowserError as e:
                    last_err = e
            if data is None:
                raise last_err or BitBrowserError("获取窗口列表失败")
            lst = data.get("list") or []
            for w in lst:
                wins.append(
                    {
                        "id": str(w.get("id")),
                        "seq": w.get("seq"),
                        "name": w.get("name") or f"窗口{w.get('seq')}",
                        "remark": w.get("remark") or "",
                        "group_id": str(w.get("groupId") or ""),
                        "group_name": str(w.get("groupName") or ""),
                    }
                )
            if len(lst) < 100 or page > 50:
                break
            page += 1
        return wins

    # ---------- 窗口控制 ----------
    def open_window(self, wid):
        data = self._post("/browser/open", {"id": wid}) or {}
        addr = data.get("ws") or ""
        if not addr:
            addr = data.get("http") or ""
        if not addr:
            raise BitBrowserError(f"打开窗口 {wid} 失败: 未返回调试地址")
        return self._normalize_cdp(addr)

    @staticmethod
    def _normalize_cdp(addr):
        """归一化为 http://host:port 形式, 兼容 ws://host:port/path 与 host:port"""
        addr = addr.strip()
        for prefix in ("ws://", "wss://", "http://", "https://"):
            if addr.startswith(prefix):
                rest = addr[len(prefix):]
                host = rest.split("/")[0]
                return "http://" + host
        return "http://" + addr.split("/")[0]

    def close_window(self, wid):
        self._post("/browser/close", {"id": wid})
