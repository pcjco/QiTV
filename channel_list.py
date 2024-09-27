import asyncio
import os
import platform
import random
import re
import shutil
import string
import subprocess
from urllib.parse import urlparse

import aiohttp
import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from urlobject import URLObject

from options import OptionsDialog


class AsyncWorker(QThread):
    finished = Signal(object)

    def __init__(self, coro):
        super().__init__()
        self.coro = coro

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self.coro)
            self.finished.emit(result)
        finally:
            loop.close()


class ChannelList(QMainWindow):
    content_loaded = Signal(list)

    def __init__(self, app, player, config_manager):
        super().__init__()
        self.app = app
        self.player = player
        self.config_manager = config_manager
        self.config = self.config_manager.config
        self.config_manager.apply_window_settings("channel_list", self)

        self.setWindowTitle("QiTV Content List")

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)
        self.grid_layout = QGridLayout(self.container_widget)

        self.content_type = "channels"  # Default to channels

        self.create_upper_panel()
        self.create_left_panel()
        self.create_media_controls()
        self.link = None
        self.load_content()
        self.workers = []

    def closeEvent(self, event):
        self.app.quit()
        self.player.close()
        self.config_manager.save_window_settings(self.geometry(), "channel_list")

        # Clean up workers
        for worker in self.workers:
            worker.quit()
            worker.wait()

        event.accept()

    def create_upper_panel(self):
        self.upper_layout = QWidget(self.container_widget)
        ctl_layout = QHBoxLayout(self.upper_layout)

        self.open_button = QPushButton("Open File")
        self.open_button.clicked.connect(self.open_file)
        ctl_layout.addWidget(self.open_button)

        self.options_button = QPushButton("Options")
        self.options_button.clicked.connect(self.options_dialog)
        ctl_layout.addWidget(self.options_button)

        self.export_button = QPushButton("Export Content")
        self.export_button.clicked.connect(self.export_content)
        ctl_layout.addWidget(self.export_button)

        self.update_button = QPushButton("Update Content")
        self.update_button.clicked.connect(self.update_content)
        ctl_layout.addWidget(self.update_button)

        self.grid_layout.addWidget(self.upper_layout, 0, 0)

    def create_left_panel(self):
        self.left_panel = QWidget(self.container_widget)
        left_layout = QVBoxLayout(self.left_panel)

        self.search_box = QLineEdit(self.left_panel)
        self.search_box.setPlaceholderText("Search content...")
        self.search_box.textChanged.connect(
            lambda: self.filter_content(self.search_box.text())
        )
        left_layout.addWidget(self.search_box)

        self.content_list = QListWidget(self.left_panel)
        self.content_list.itemClicked.connect(self.item_selected)
        left_layout.addWidget(self.content_list)

        self.grid_layout.addWidget(self.left_panel, 1, 0)
        self.grid_layout.setColumnStretch(0, 1)

        # Add favorite button and action
        self.favorite_button = QPushButton("Favorite/Unfavorite")
        self.favorite_button.clicked.connect(self.toggle_favorite)
        left_layout.addWidget(self.favorite_button)

        # Add checkbox to show only favorites
        self.favorites_only_checkbox = QCheckBox("Show only favorites")
        self.favorites_only_checkbox.stateChanged.connect(
            lambda: self.filter_content(self.search_box.text())
        )
        left_layout.addWidget(self.favorites_only_checkbox)

        self.content_switch = QCheckBox("Show Movies")
        self.content_switch.stateChanged.connect(self.toggle_content_type)
        left_layout.addWidget(self.content_switch)

    def toggle_favorite(self):
        selected_item = self.content_list.currentItem()
        if selected_item:
            item_name = selected_item.text()
            is_favorite = self.check_if_favorite(item_name)
            if is_favorite:
                self.remove_from_favorites(item_name)
            else:
                self.add_to_favorites(item_name)
            self.filter_content(self.search_box.text())

    def add_to_favorites(self, item_name):
        if item_name not in self.config["favorites"]:
            self.config["favorites"].append(item_name)
            self.save_config()

    def remove_from_favorites(self, item_name):
        if item_name in self.config["favorites"]:
            self.config["favorites"].remove(item_name)
            self.save_config()

    def check_if_favorite(self, item_name):
        return item_name in self.config["favorites"]

    def toggle_content_type(self, value):
        state = Qt.CheckState(value)
        self.content_type = "movies" if state == Qt.Checked else "channels"
        self.load_content()

    def display_content(self, items):
        self.content_list.clear()
        for item in items:
            list_item = QListWidgetItem(item["name"])
            list_item.setData(31, item["cmd"])
            self.content_list.addItem(list_item)
            if self.check_if_favorite(item["name"]):
                list_item.setBackground(QColor(0, 0, 255, 20))

    def filter_content(self, text=""):
        show_favorites = self.favorites_only_checkbox.isChecked()
        search_text = text.lower() if isinstance(text, str) else ""

        for i in range(self.content_list.count()):
            item = self.content_list.item(i)
            item_name = item.text().lower()

            matches_search = search_text in item_name
            is_favorite = self.check_if_favorite(item.text())

            if show_favorites and not is_favorite:
                item.setHidden(True)
            else:
                item.setHidden(not matches_search)

    def create_media_controls(self):
        self.media_controls = QWidget(self.container_widget)
        control_layout = QHBoxLayout(self.media_controls)

        self.play_button = QPushButton("Play/Pause")
        self.play_button.clicked.connect(self.player.toggle_play_pause)
        control_layout.addWidget(self.play_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.player.stop_video)
        control_layout.addWidget(self.stop_button)

        self.vlc_button = QPushButton("Open in VLC")
        self.vlc_button.clicked.connect(self.open_in_vlc)
        control_layout.addWidget(self.vlc_button)

        self.grid_layout.addWidget(self.media_controls, 2, 0)

    def open_in_vlc(self):
        # Invoke user's VLC player to open the current stream
        if self.link:
            try:
                if platform.system() == "Windows":
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    if not vlc_path:
                        program_files = os.environ.get(
                            "ProgramFiles", "C:\\Program Files"
                        )
                        vlc_path = os.path.join(
                            program_files, "VideoLAN", "VLC", "vlc.exe"
                        )
                    subprocess.Popen([vlc_path, self.link])
                elif platform.system() == "Darwin":  # macOS
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    if not vlc_path:
                        common_paths = [
                            "/Applications/VLC.app/Contents/MacOS/VLC",
                            "~/Applications/VLC.app/Contents/MacOS/VLC",
                        ]
                        for path in common_paths:
                            expanded_path = os.path.expanduser(path)
                            if os.path.exists(expanded_path):
                                vlc_path = expanded_path
                                break
                    subprocess.Popen([vlc_path, self.link])
                else:  # Assuming Linux or other Unix-like OS
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    subprocess.Popen([vlc_path, self.link])
                # when VLC opens, stop running video on self.player
                self.player.stop_video()
            except FileNotFoundError as fnf_error:
                print("VLC not found: ", fnf_error)
            except Exception as e:
                print(f"Error opening VLC: {e}")

    def open_file(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path:
            self.player.play_video(file_path)

    def export_content(self):
        file_dialog = QFileDialog(self)
        file_dialog.setAcceptMode(QFileDialog.AcceptSave)
        file_dialog.setDefaultSuffix("m3u")
        file_path, _ = file_dialog.getSaveFileName(
            self, "Export Content", "", "M3U files (*.m3u)"
        )
        if file_path:
            provider = self.config["data"][self.config["selected"]]
            content_data = provider.get(self.content_type, [])
            base_url = provider.get("url", "")
            config_type = provider.get("type", "")
            mac = provider.get("mac", "")

            if config_type == "STB":
                self.save_stb_content(base_url, content_data, mac, file_path)
            elif config_type in ["M3UPLAYLIST", "M3USTREAM", "XTREAM"]:
                self.save_m3u_content(content_data, file_path)
            else:
                print(f"Unknown provider type: {config_type}")

    def save_m3u_content(self, content_data, file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for item in content_data:
                    name = item.get("name", "Unknown")
                    logo = item.get("logo", "")
                    cmd_url = item.get("cmd")

                    if cmd_url:
                        item_str = f'#EXTINF:-1 tvg-logo="{logo}" ,{name}\n{cmd_url}\n'
                        count += 1
                        file.write(item_str)
                print(f"Items exported: {count}")
                print(f"\nContent list has been saved to {file_path}")
        except IOError as e:
            print(f"Error saving content list: {e}")

    def save_stb_content(self, base_url, content_data, mac, file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for item in content_data:
                    name = item.get("name", "Unknown")
                    logo = item.get("logo", "")
                    cmd_url = item.get("cmd", "").replace("ffmpeg ", "")

                    # Generalized URL construction
                    if "localhost" in cmd_url:
                        id_match = re.search(r"/(ch|vod)/(\d+)_", cmd_url)
                        if id_match:
                            content_type = id_match.group(1)
                            content_id = id_match.group(2)
                            if content_type == "ch":
                                cmd_url = f"{base_url}/play/live.php?mac={mac}&stream={content_id}&extension=m3u8"
                            elif content_type == "vod":
                                cmd_url = f"{base_url}/play/vod.php?mac={mac}&stream={content_id}&extension=m3u8"

                    item_str = f'#EXTINF:-1 tvg-logo="{logo}" ,{name}\n{cmd_url}\n'
                    count += 1
                    file.write(item_str)
                print(f"Items exported: {count}")
                print(f"\nContent list has been saved to {file_path}")
        except IOError as e:
            print(f"Error saving content list: {e}")

    def save_config(self):
        self.config_manager.save_config()

    def load_content(self):
        if self.content_type == "channels":
            self.load_channels()
        else:
            self.load_movies()

    def load_channels(self):
        channels = self.config["data"][self.config["selected"]].get("channels", [])
        if channels:
            self.display_content(channels)
        else:
            self.update_content()

    def load_movies(self):
        movies = self.config["data"][self.config["selected"]].get("movies", [])
        if movies:
            self.display_content(movies)
        else:
            self.update_content()

    def update_content(self):
        selected_provider = self.config["data"][self.config["selected"]]
        config_type = selected_provider.get("type", "")
        if config_type == "M3UPLAYLIST":
            self.load_m3u_playlist(selected_provider["url"])
        elif config_type == "XTREAM":
            urlobject = URLObject(selected_provider["url"])
            if urlobject.scheme == "":
                urlobject = URLObject(f"http://{selected_provider['url']}")
            if self.content_type == "channels":
                url = (
                    f"{urlobject.scheme}://{urlobject.netloc}/get.php?"
                    f"username={selected_provider['username']}&password={selected_provider['password']}&type=m3u"
                )
            else:
                url = (
                    f"{urlobject.scheme}://{urlobject.netloc}/get.php?"
                    f"username={selected_provider['username']}&password={selected_provider['password']}&type=m3u&"
                    "contentType=vod"
                )
            self.load_m3u_playlist(url)
        elif config_type == "STB":
            worker = AsyncWorker(
                self.do_handshake(
                    selected_provider["url"], selected_provider["mac"], load=False
                )
            )
            worker.finished.connect(self.on_handshake_complete)
            worker.start()
            self.workers.append(worker)
        elif config_type == "M3USTREAM":
            self.load_stream(selected_provider["url"])

    def on_handshake_complete(self, success):
        if success:
            selected_provider = self.config["data"][self.config["selected"]]
            options = selected_provider["options"]
            self.load_stb_content(selected_provider["url"], options)
        else:
            print("Handshake failed")

    def load_m3u_playlist(self, url):
        async def fetch_m3u():
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        content = await response.text()
                        return self.parse_m3u(content)
                    else:
                        return []

        worker = AsyncWorker(fetch_m3u())
        worker.finished.connect(self.on_m3u_loaded)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_m3u_loaded(self, content):
        self.display_content(content)
        self.config["data"][self.config["selected"]][self.content_type] = content
        self.save_config()

    def load_stream(self, url):
        item = {"id": 1, "name": "Stream", "cmd": url}
        self.display_content([item])
        # Update the content in the config
        self.config["data"][self.config["selected"]][self.content_type] = [item]
        self.save_config()

    def item_selected(self, item):
        cmd = item.data(31)
        if self.config["data"][self.config["selected"]]["type"] == "STB":
            url = self.create_link(cmd)
            if url:
                self.link = url
                self.player.play_video(url)
            else:
                print("Failed to create link.")
        else:
            self.link = cmd
            self.player.play_video(cmd)

    def options_dialog(self):
        options = OptionsDialog(self)
        options.exec_()

    @staticmethod
    def parse_m3u(data):
        lines = data.split("\n")
        result = []
        item = {}
        id = 0
        for line in lines:
            if line.startswith("#EXTINF"):
                tvg_id_match = re.search(r'tvg-id="([^"]+)"', line)
                tvg_logo_match = re.search(r'tvg-logo="([^"]+)"', line)
                group_title_match = re.search(r'group-title="([^"]+)"', line)
                item_name_match = re.search(r",(.+)", line)

                tvg_id = tvg_id_match.group(1) if tvg_id_match else None
                tvg_logo = tvg_logo_match.group(1) if tvg_logo_match else None
                group_title = group_title_match.group(1) if group_title_match else None
                item_name = item_name_match.group(1) if item_name_match else None

                id += 1
                item = {
                    "id": id,
                    "name": item_name,
                    "logo": tvg_logo,
                }

            elif line.startswith("http"):
                urlobject = urlparse(line)
                item["cmd"] = urlobject.geturl()
                result.append(item)
        return result

    async def do_handshake(self, url, mac, serverload="/server/load.php", load=True):
        token = self.config.get("token") or self.random_token()
        options = self.create_options(url, mac, token)
        fetchurl = f"{url}{serverload}?type=stb&action=handshake&prehash=0&token={token}&JsHttpRequest=1-xml"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    fetchurl, headers=options["headers"]
                ) as response:
                    if response.status == 200:
                        body = await response.json()
                        token = body["js"]["token"]
                        options["headers"]["Authorization"] = f"Bearer {token}"
                        self.config["data"][self.config["selected"]][
                            "options"
                        ] = options
                        self.save_config()
                        if load:
                            await self.load_stb_content(url, options)
                        return True
                    else:
                        print(f"Handshake failed with status code: {response.status}")
                        return False
            except aiohttp.ClientError as e:
                print(f"Error in handshake: {e}")
                if serverload != "/portal.php":
                    return await self.do_handshake(url, mac, "/portal.php", load)
                return False

    def load_stb_content(self, url, options):
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}"

        async def fetch_content():
            async with aiohttp.ClientSession() as session:
                try:
                    if self.content_type == "channels":
                        fetchurl = (
                            f"{url}/server/load.php?type=itv&action=get_all_channels"
                        )
                        async with session.get(
                            fetchurl, headers=options["headers"]
                        ) as response:
                            result = await response.json()
                            return result["js"]["data"]
                    else:
                        fetchurl = (
                            f"{url}/server/load.php?type=vod&action=get_ordered_list"
                        )
                        async with session.get(
                            fetchurl, headers=options["headers"]
                        ) as response:
                            result = await response.json()
                            total_items = int(result["js"]["total_items"])
                            max_page_items = int(result["js"]["max_page_items"])
                            pages = (total_items + max_page_items - 1) // max_page_items

                            tasks = []
                            for page in range(pages):
                                page_url = f"{url}/server/load.php?type=vod&action=get_ordered_list&genre=0&category=*&p={page}&sortby=added"
                                tasks.append(
                                    session.get(page_url, headers=options["headers"])
                                )

                            responses = await asyncio.gather(*tasks)
                            items = []
                            for response in responses:
                                result = await response.json()
                                items.extend(result["js"]["data"])
                            return items

                except aiohttp.ClientError as e:
                    print(f"Error fetching content: {e}")
                    return None

        worker = AsyncWorker(fetch_content())
        worker.finished.connect(self.on_content_loaded)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_content_loaded(self, items):
        if items is None:
            print("Error loading content")
            return
        self.display_content(items)
        self.config["data"][self.config["selected"]][self.content_type] = items
        self.save_config()

    def create_link(self, cmd):
        async def fetch_link():
            try:
                selected_provider = self.config["data"][self.config["selected"]]
                url = URLObject(selected_provider["url"])
                url = f"{url.scheme}://{url.netloc}"
                options = selected_provider["options"]
                content_type = "vod" if self.content_type == "movies" else "itv"
                fetchurl = (
                    f"{url}/server/load.php?type={content_type}&action=create_link"
                    f"&cmd={requests.utils.quote(cmd)}&JsHttpRequest=1-xml"
                )
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        fetchurl, headers=options["headers"]
                    ) as response:
                        if response.status == 200:
                            result = await response.json()
                            link = result["js"]["cmd"].split(" ")[-1]
                            return link
                        else:
                            print(
                                f"Error creating link. Status code: {response.status}"
                            )
                            return None
            except Exception as e:
                print(f"Error creating link: {e}")
                return None

        worker = AsyncWorker(fetch_link())
        worker.finished.connect(self.on_link_created)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_link_created(self, link):
        if link:
            self.link = link
            self.player.play_video(link)
        else:
            print("Failed to create link.")

    @staticmethod
    def random_token():
        return "".join(random.choices(string.ascii_letters + string.digits, k=32))

    @staticmethod
    def create_options(url, mac, token):
        url = URLObject(url)
        options = {
            "headers": {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                "Accept-Charset": "UTF-8,*;q=0.8",
                "X-User-Agent": "Model: MAG200; Link: Ethernet",
                "Host": f"{url.netloc}",
                "Range": "bytes=0-",
                "Accept": "*/*",
                "Referer": f"{url}/c/" if not url.path else f"{url}/",
                "Cookie": f"mac={mac}; stb_lang=en; timezone=Europe/Kiev; PHPSESSID=null;",
                "Authorization": f"Bearer {token}",
            }
        }
        return options

    def generate_headers(self):
        selected_provider = self.config["data"][self.config["selected"]]
        return selected_provider["options"]["headers"]

    @staticmethod
    async def verify_url(url):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    return response.status != 0
        except aiohttp.ClientError as e:
            print(f"Error verifying URL: {e}")
            return False

    # To use this method, you'll need to create an AsyncWorker:
    def check_url(self, url):
        worker = AsyncWorker(self.verify_url(url))
        worker.finished.connect(self.on_url_verified)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_url_verified(self, is_valid):
        if is_valid:
            print("URL is valid")
        else:
            print("URL is invalid")
