"""内存日志缓冲，供前端轮询展示"""
import threading
import time
from collections import deque

_lock = threading.Lock()
_buffer = deque(maxlen=1000)
_next_id = 1


def add_log(msg, level="info"):
    global _next_id
    with _lock:
        entry = {
            "id": _next_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "msg": str(msg),
        }
        _next_id += 1
        _buffer.append(entry)
    print(f"[{entry['time']}] [{level}] {msg}", flush=True)


def get_logs(after_id=0):
    with _lock:
        return [e for e in _buffer if e["id"] > after_id]


def last_id():
    with _lock:
        return _next_id - 1
