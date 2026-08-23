import threading
import webbrowser

import uvicorn


def _open_browser():
    webbrowser.open("http://127.0.0.1:8799/")


if __name__ == "__main__":
    threading.Timer(2.5, _open_browser).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=8799, log_level="warning")
