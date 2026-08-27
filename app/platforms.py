"""多平台发布适配层: 当前实现抖音, 小红书/B站/快手/微博 预留扩展"""
from .logs import add_log
from .publisher import PublishError, publish_once

PLATFORMS = {
    "douyin": {"label": "抖音", "ready": True},
    "xhs": {"label": "小红书", "ready": False},
    "bilibili": {"label": "B站", "ready": False},
    "kuaishou": {"label": "快手", "ready": False},
    "weibo": {"label": "微博", "ready": False},
}


def platform_label(pid):
    return (PLATFORMS.get(pid) or {}).get("label") or pid


def is_ready(pid):
    return bool((PLATFORMS.get(pid) or {}).get("ready"))


def compose_caption(platform, content):
    """结构化内容 -> 平台发布文案(仅描述+标签)。

    抖音有独立的标题输入框(由 publisher 单独填写), 标题不重复拼进描述,
    否则成品会同时出现 标题 和 描述首行的重复标题。
    """
    c = content or {}
    desc = (c.get("description") or "").strip()
    title = (c.get("title") or "").strip()
    if title and desc.startswith(title):
        # 描述若以标题开头则去掉重复
        desc = desc[len(title):].lstrip("\n：: \t").strip()
    tags = c.get("tags") or []
    if tags and ("#" not in desc):
        desc = (desc + "\n" + " ".join("#" + str(t).lstrip("#") for t in tags)).strip()
    return desc or title


def publish(platform, bitclient, eff_settings, window, video_path, content):
    """按平台分发发布; 新平台在此注册各自的实现即可"""
    if platform not in PLATFORMS:
        raise PublishError(f"不支持的平台: {platform}")
    if not is_ready(platform):
        raise PublishError(f"平台[{platform_label(platform)}]即将支持，敬请期待")
    caption = compose_caption(platform, content)
    title = ((content or {}).get("title") or "").strip()
    add_log(f"[{window['name']}] 准备发布到{platform_label(platform)}")
    return publish_once(bitclient, eff_settings, window, video_path, caption, title=title)
