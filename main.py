import json
import os
import queue
import re
import sys
import threading
import time
import random
import webbrowser
import tkinter as tk
import pystray
from PIL import Image
from tkinter import messagebox, scrolledtext

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyPKCE
from spotipy.oauth2 import SpotifyOAuth


BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
LINKS_PATH = os.path.join(BASE_DIR, "links.json")
CACHE_PATH = os.path.join(BASE_DIR, ".cache-pkce")
DASHBOARD_URL = "https://developer.spotify.com/dashboard"
DEFAULT_CLIENT_ID = ""

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = ("user-read-playback-state user-modify-playback-state ""playlist-read-private playlist-read-collaborative")

PART_RE = re.compile(
    r"^(?P<base>.+?)\s+(?:(?i:pt\.?|part)\s*)?(?P<num>[IVX]+|\d+)\s*(?:[:\-–(]|$)"
)

def base_of(title):
    m = PART_RE.match(title)
    return m.group("base").strip().lower() if m else None

def load_links(log):
    try:
        with open(LINKS_PATH, encoding="utf-8") as f:
            return {(e["artist"].lower(), e["from"].lower()): e["to"].lower()
                    for e in json.load(f)}
    except FileNotFoundError:
        with open(LINKS_PATH, "w", encoding="utf-8") as f:
            f.write("[]")
        return {}
    except Exception as e:
        log(f"Could not read links.json: {e}")
        return {}

QUEUE_WHEN_REMAINING_S = 10
RESUME_LEAD_S = 0.8
ONLY_IF_IN_PLAYLIST = True

_playlist_cache = {}


def find_next(sp, item, links):
    artist = item["artists"][0]["name"].lower()
    name = item["name"].lower()
    tracks = sp.album_tracks(item["album"]["id"], limit=50)["items"]
    ids = [t["id"] for t in tracks]
    if item["id"] not in ids:
        return None
    i = ids.index(item["id"])

    target = links.get((artist, name))
    if target:
        for t in tracks:
            if t["name"].lower() == target:
                return t

    base = base_of(item["name"])
    if base and i + 1 < len(tracks) and base_of(tracks[i + 1]["name"]) == base:
        return tracks[i + 1]
    return None


def playlist_songs(sp, ctx):
    uri = ctx["uri"]
    cached = _playlist_cache.get(uri)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    songs = set()
    try:
        page = sp.playlist_items(uri, limit=100)
        while page:
            for entry in page["items"]:
                t = entry.get("track") or entry.get("item")
                if t and t.get("name") and t.get("artists"):
                    songs.add((t["artists"][0]["name"].lower(), t["name"].lower()))
            page = sp.next(page) if page.get("next") else None
    except Exception:
        songs = None
    if not songs:
        songs = None
    _playlist_cache[uri] = (time.time(), songs)
    return songs


def resume_playlist(sp, ctx, shuffle_on, log):
    uri = ctx["uri"]
    try:
        total = sp.playlist_items(uri, limit=1)["total"]
        sp.start_playback(context_uri=uri,
                          offset={"position": random.randrange(total)})
    except Exception:
        sp.start_playback(context_uri=uri)
    if shuffle_on:
        try:
            sp.shuffle(True)
        except Exception:
            pass
    log("Chain finished: jumped to a random song in your playlist")


class Worker(threading.Thread):
    def __init__(self, log):
        super().__init__(daemon=True)
        self.log = log
        self.stop_event = threading.Event()

    def run(self):
        try:
            load_dotenv(ENV_PATH, override=True)
            cid = os.getenv("SPOTIPY_CLIENT_ID") or DEFAULT_CLIENT_ID
            if not cid:
                self.log("Enter your Client ID, then click Start. "
                         "Need one? Click 'Setup guide'.")
                return
            sp = spotipy.Spotify(auth_manager=SpotifyPKCE(
                client_id=cid, redirect_uri=REDIRECT_URI,
                scope=SCOPE, cache_path=CACHE_PATH))
            links = load_links(self.log)
            self.log("Running. Play a song in Spotify.")

            current_id = None
            is_chain = False
            next_track = None
            home_ctx = None
            shuffle_on = False
            queued_ids = set()
            handled = None

            while not self.stop_event.is_set():
                try:
                    t0 = time.monotonic()
                    pb = sp.current_playback()
                    t_mid = (t0 + time.monotonic()) / 2
                    if pb and pb["is_playing"] and pb.get("item") \
                            and pb["item"]["type"] == "track":
                        item = pb["item"]
                        ctx = pb.get("context")

                        if item["id"] != current_id:
                            current_id = item["id"]
                            is_chain = item["id"] in queued_ids
                            queued_ids.discard(item["id"])

                            if not is_chain:
                                if ctx and ctx["type"] == "playlist":
                                    home_ctx = ctx
                                    shuffle_on = pb.get("shuffle_state", False)
                                else:
                                    home_ctx = None

                            next_track = None
                            in_album = ctx and ctx["uri"] == item["album"]["uri"]
                            if is_chain or not in_album:
                                next_track = find_next(sp, item, links)
                                if next_track and ONLY_IF_IN_PLAYLIST and home_ctx:
                                    songs = playlist_songs(sp, home_ctx)
                                    key = (next_track["artists"][0]["name"].lower(),
                                           next_track["name"].lower())
                                    if songs is not None and key not in songs:
                                        self.log(f"'{next_track['name']}' is not in the playlist")
                                        next_track = None

                        remaining = (item["duration_ms"] - pb["progress_ms"]) / 1000
                        remaining -= time.monotonic() - t_mid

                        if handled != current_id:
                            if next_track:
                                if remaining <= QUEUE_WHEN_REMAINING_S:
                                    sp.add_to_queue(next_track["uri"])
                                    queued_ids.add(next_track["id"])
                                    handled = current_id
                                    self.log(f"Queued: {next_track['name']}")
                            elif is_chain and home_ctx and remaining <= 4:
                                self.stop_event.wait(max(remaining - RESUME_LEAD_S, 0))
                                resume_playlist(sp, home_ctx, shuffle_on, self.log)
                                handled = current_id
                                self.stop_event.wait(2)
                                continue
                except Exception as e:
                    self.log(f"Error: {e}")
                    self.stop_event.wait(5)
                    continue
                self.stop_event.wait(1)
        except Exception as e:
            self.log(f"Error: {e}")
        finally:
            self.log("Stopped.")
            self.log("__DONE__")


SETUP_TEXT = (
    "One-time setup (about 2 minutes, free).\n"
    "Spotify requires Premium on the account that owns the app.\n\n"
    "1. Click 'Open dashboard' and log in.\n"
    "2. Click 'Create app' (any name and description).\n"
    "3. Click 'Copy redirect URI' here, paste it into the\n"
    "    Redirect URI box in the dashboard, and click Add.\n"
    "4. Tick 'Web API', accept the terms, and Save.\n"
    "5. Open the app's Settings and copy the Client ID.\n"
    "6. Paste it into this program, click Start, and click\n"
    "    Agree in the browser window.\n\n"
    "No client secret is needed."
)


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


class App:
    def __init__(self, root):
        self.root = root
        self.worker = None
        self.messages = queue.Queue()
        root.title("Spotify Part Queue")
        root.geometry("460x400")
        self.tray = None
        root.bind("<Unmap>", self.on_unmap)
        root.protocol("WM_DELETE_WINDOW", self.quit_app)

        load_dotenv(ENV_PATH)
        box = tk.Frame(root, padx=10, pady=10)
        box.pack(fill="x")
        tk.Label(box, text="Client ID").grid(row=0, column=0, sticky="w")
        self.cid = tk.Entry(box, width=42)
        self.cid.grid(row=0, column=1, pady=2)
        self.cid.insert(0, os.getenv("SPOTIPY_CLIENT_ID", "") or DEFAULT_CLIENT_ID)
        tk.Button(box, text="Setup guide", command=self.show_setup).grid(
            row=1, column=1, sticky="e", pady=4)

        bar = tk.Frame(root, padx=10)
        bar.pack(fill="x")
        self.btn = tk.Button(bar, text="Start", width=10, command=self.toggle)
        self.btn.pack(side="left")
        tk.Button(bar, text="Edit links.json", command=self.open_links).pack(
            side="left", padx=8)
        self.status = tk.Label(bar, text="Stopped", fg="gray")
        self.status.pack(side="right")

        self.logbox = scrolledtext.ScrolledText(root, height=14, state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=10, pady=10)
        self.poll()

    def log(self, text):
        self.messages.put(text)

    def poll(self):
        while not self.messages.empty():
            msg = self.messages.get()
            if msg == "__SHOW__":
                self.show_window()
                continue
            if msg == "__TOGGLE__":
                self.toggle()
                continue
            if msg == "__QUIT__":
                self.quit_app()
                return
            if msg == "__DONE__":
                self.btn.config(text="Start")
                self.status.config(text="Stopped", fg="gray")
                continue
            self.logbox.config(state="normal")
            self.logbox.insert("end", msg + "\n")
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        self.root.after(200, self.poll)

    def show_setup(self):
        win = tk.Toplevel(self.root)
        win.title("Setup guide")
        tk.Label(win, text=SETUP_TEXT, justify="left", padx=12, pady=12).pack()
        row = tk.Frame(win, pady=8)
        row.pack()
        tk.Button(row, text="Open dashboard",
                  command=lambda: webbrowser.open(DASHBOARD_URL)).pack(
            side="left", padx=4)

        def copy_uri():
            self.root.clipboard_clear()
            self.root.clipboard_append(REDIRECT_URI)
            messagebox.showinfo("Copied", REDIRECT_URI, parent=win)

        tk.Button(row, text="Copy redirect URI", command=copy_uri).pack(
            side="left", padx=4)



    def on_unmap(self, event):
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.after(50, self.to_tray)

    def to_tray(self):
        if self.tray is not None:
            return
        self.root.withdraw()
        image = Image.open(resource_path("icon.ico")).convert("RGBA")
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda icon, item: self.messages.put("__SHOW__"),
                             default=True),
            pystray.MenuItem("Start / Stop", lambda icon, item: self.messages.put("__TOGGLE__")),
            pystray.MenuItem("Quit", lambda icon, item: self.messages.put("__QUIT__")),
        )
        self.tray = pystray.Icon("SpotifyPartQueue", image, "Spotify Part Queue", menu)
        self.tray.run_detached()

    def show_window(self):
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def quit_app(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop_event.set()
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        self.root.destroy()

    def save_keys(self):
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(f"SPOTIPY_CLIENT_ID={self.cid.get().strip()}\n")
        self.log("Client ID saved.")

    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop_event.set()
            return
        self.save_keys()
        self.worker = Worker(self.log)
        self.worker.start()
        self.btn.config(text="Stop")
        self.status.config(text="Running", fg="green")

    def open_links(self):
        load_links(self.log)
        if sys.platform == "win32":
            os.startfile(LINKS_PATH)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        root.iconbitmap(resource_path("icon.ico"))
    except Exception:
        pass
    App(root)
    root.mainloop()