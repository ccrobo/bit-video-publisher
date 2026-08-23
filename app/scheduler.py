"""APScheduler 定时调度: 按任务 cron 表达式周期执行"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import store
from .logs import add_log
from .task_runner import safe_run

TZ = "Asia/Shanghai"
sched = BackgroundScheduler(timezone=TZ)
_started = False


def start():
    global _started
    if not _started:
        sched.start()
        _started = True
    reload_jobs()


def reload_jobs():
    if not _started:
        return
    sched.remove_all_jobs()
    for t in store.list_tasks():
        if t.get("enabled") and t.get("cron"):
            try:
                trigger = CronTrigger.from_crontab(t["cron"], timezone=TZ)
                sched.add_job(
                    safe_run,
                    trigger,
                    args=[t["id"]],
                    id=t["id"],
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=3600,
                )
                add_log(f"定时任务[{t['name']}] 已注册 cron={t['cron']}")
            except Exception as e:
                add_log(f"任务[{t.get('name')}] cron 表达式无效({t.get('cron')}): {e}", "error")


def shutdown():
    global _started
    if _started:
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass
        _started = False
