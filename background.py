import os

from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.get("/")
def home():
    return "OK"

def _run():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

def keep_alive():
    enabled = os.getenv("KEEP_ALIVE")
    if enabled is None:
        enabled = "1" if (os.getenv("REPL_ID") or os.getenv("RENDER")) else "0"
    if enabled != "1":
        return
    t = Thread(target=_run, daemon=True)
    t.start()
