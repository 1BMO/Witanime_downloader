import re
import time
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import base64
import os
import urllib.parse
import ctypes

# ── Focus stealing fix ──────────────────────────────────────────────────────
# Brave/Chrome grabs OS-level keyboard focus the moment a new window or tab
# opens. These helpers remember whatever window you were typing in right
# before that happens, then snap focus back to it. Requires pywin32:
#   pip install pywin32
try:
    import win32gui
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    print("Note: pywin32 not installed (pip install pywin32) -> can't stop the browser from stealing focus.")


def get_foreground_window():
    """Return whatever window currently has OS focus (e.g. your terminal/editor)."""
    if not HAS_WIN32:
        return None
    return win32gui.GetForegroundWindow()


def restore_foreground(hwnd):
    """
    Give OS-level keyboard focus back to `hwnd`. Windows normally blocks a
    background process from calling SetForegroundWindow, so we fake an ALT
    keypress first -- that satisfies the check Windows uses to decide whether
    a focus change request is "user-initiated" enough to allow.
    """
    if not HAS_WIN32 or not hwnd:
        return
    try:
        if not win32gui.IsWindow(hwnd):
            return
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)     # ALT down
        win32gui.SetForegroundWindow(hwnd)
        ctypes.windll.user32.keybd_event(0x12, 0, 0x2, 0)   # ALT up
    except Exception:
        pass  # not critical -- worst case you get a brief flash


episodes_url = {}
selected_quality = None  # once you pick a quality with multiple options, it carries over to the rest of the queue


def parse_size_to_bytes(text):
    """Pulls a size like '117.24MB' out of MediaFire's button label and converts to bytes."""
    match = re.search(r"([\d.]+)\s*(KB|MB|GB)", text, re.IGNORECASE)
    if not match:
        return None
    size = float(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[unit]
    return int(size * multiplier)


def make_driver(download_dir):
    options = Options()
    options.binary_location = "C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe"
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    # Push the window off your visible desktop so it's out of the way.
    # If you have a second monitor positioned at negative coordinates this
    # could land there instead -- just adjust the numbers, or comment this
    # line out if you want to actually watch it work.
    options.add_argument("--window-position=-2400,-2400")
    options.add_argument("--disable-blink-features=AutomationControlled")

    # tell Brave to auto-download to our folder instead of asking
    prefs = {
        "download.default_directory": os.path.abspath(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": False,
        "safebrowsing.disable_download_protection": True,
    }
    options.add_experimental_option("prefs", prefs)
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)


def get_episodes_url(anime_url, driver):
    """Accepts either the anime main page or any episode URL and finds all episodes.

    Dedupes by decoded URL (witanime renders two <a> tags per episode card,
    both pointing to the same link) and reads the real episode number from
    the URL slug instead of trusting a running counter.
    """
    if "/episode/" in anime_url:
        driver.get(anime_url)
        time.sleep(3)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        anime_link = None
        for a in soup.find_all("a", href=True):
            if "/anime/" in a["href"] and a["href"] != "#":
                anime_link = a["href"]
                break
        if anime_link:
            print(f"Detected anime page: {anime_link}")
            anime_url = anime_link
        else:
            print("Could not find anime main page, trying episode links directly...")

    driver.get(anime_url)
    time.sleep(3)

    soup = BeautifulSoup(driver.page_source, "html.parser")

    seen_urls = set()
    fallback_counter = 1

    for link in soup.find_all("a"):
        onclick = link.get("onclick")
        if not onclick:
            continue

        result = re.search(r"\('([^']+)'\)", onclick)
        if not result:
            continue

        try:
            decoded_url = base64.b64decode(result.group(1)).decode("utf-8")
        except Exception:
            continue

        if decoded_url in seen_urls:
            continue
        seen_urls.add(decoded_url)

        match = re.search(r"-(\d+)/?$", decoded_url.rstrip("/"))
        ep_num = int(match.group(1)) if match else fallback_counter

        episodes_url[ep_num] = decoded_url
        fallback_counter += 1

    print(f"Found {len(episodes_url)} episodes.")


def scan_available_qualities(episode_url, driver):
    driver.get(episode_url)
    time.sleep(3)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    qualities = {}

    for ul in soup.find_all("ul", class_="quality-list"):
        first_li = ul.find("li")
        if not first_li:
            continue

        label_text = first_li.get_text(strip=True).upper()

        if "FHD" in label_text:
            quality_key = "FHD"
        elif "HD" in label_text:
            quality_key = "HD"
        elif "SD" in label_text:
            quality_key = "SD"
        else:
            quality_key = label_text

        for a in ul.find_all("a", class_="download-link"):
            span = a.find("span", class_="notice")
            if span and "mediafire" in span.get_text(strip=True).lower():
                qualities[quality_key] = a.get("data-index")
                break

    return qualities


def download_via_browser(episode_url, driver, data_index, download_dir):
    """
    Clicks the MediaFire button, waits for MediaFire page to load in new tab,
    then clicks the download button directly in the browser so Brave handles
    the download. Re-grabs your OS focus right after any new tab opens.
    """
    driver.get(episode_url)
    time.sleep(3)

    original_windows = set(driver.window_handles)

    your_hwnd = get_foreground_window()  # remember what you're working in
    btn = driver.find_element(By.CSS_SELECTOR, f'a[data-index="{data_index}"]')
    btn.click()

    # wait for new tab
    timeout = 10
    while timeout > 0 and set(driver.window_handles) == original_windows:
        time.sleep(0.5)
        timeout -= 0.5

    restore_foreground(your_hwnd)  # the new tab likely just stole OS focus -- take it back

    new_windows = set(driver.window_handles) - original_windows
    if not new_windows:
        print("  No new tab opened.")
        return False

    new_handle = list(new_windows)[0]
    driver.switch_to.window(new_handle)

    print("  Waiting for MediaFire redirect...")
    timeout = 15
    while timeout > 0:
        if "mediafire.com" in driver.current_url:
            break
        time.sleep(1)
        timeout -= 1

    if "mediafire.com" not in driver.current_url:
        print(f"  Timed out. Current URL: {driver.current_url}")
        driver.close()
        driver.switch_to.window(list(original_windows)[0])
        return False

    print(f"  MediaFire page: {driver.current_url}")

    time.sleep(2)
    try:
        dl_btn = driver.find_element(By.ID, "downloadButton")
        filename = dl_btn.text.strip()
        print(f"  Clicking download: {filename}")
        total_bytes = parse_size_to_bytes(filename)
        your_hwnd = get_foreground_window()
        dl_btn.click()
        restore_foreground(your_hwnd)
    except Exception as e:
        print(f"  Could not click downloadButton: {e}")
        driver.close()
        driver.switch_to.window(list(original_windows)[0])
        return False

    print("  Waiting for download to complete...")
    abs_dir = os.path.abspath(download_dir)

    def get_downloading():
        return [f for f in os.listdir(abs_dir) if f.endswith(".crdownload")]

    timeout = 15
    while timeout > 0 and not get_downloading():
        time.sleep(0.5)
        timeout -= 0.5

    timeout = 300  # 5 minutes max
    while timeout > 0:
        current = get_downloading()
        if not current:
            break

        current_path = os.path.join(abs_dir, current[0])
        try:
            current_size = os.path.getsize(current_path)
        except OSError:
            current_size = 0

        if total_bytes:
            pct = min(100, current_size / total_bytes * 100)
            print(f"\r  Downloading... {pct:5.1f}% ({current[0]})", end="", flush=True)
        else:
            mb = current_size / (1024 ** 2)
            print(f"\r  Downloading... {mb:.1f}MB ({current[0]})", end="", flush=True)

        time.sleep(1)
        timeout -= 1

    print("\n  Download complete.")

    driver.close()
    driver.switch_to.window(list(original_windows)[0])
    return True


def ask_yes_no(prompt):
    """Ask a yes/no question; just pressing Enter answers No (default)."""
    answer = input(f"{prompt} (y/N): ").strip().lower()
    return answer in ("y", "yes")


def collect_requests():
    """
    Ask for every anime up front, before any browser/downloading starts.
    Keeps asking "Is there another anime?" (default No) until you say no,
    so you can queue a whole batch in one go and then walk away.
    """
    queue = []
    while True:
        anime_url = input("\nEnter the anime or episode url: ").strip()
        from_ep   = int(input("Start episode: "))
        to_ep     = int(input("End episode:   "))
        queue.append((anime_url, from_ep, to_ep))

        if not ask_yes_no("Is there another anime?"):
            break

    return queue


def process_anime(driver, out_dir, anime_url, from_ep, to_ep):
    """Handles one already-queued anime: scans episodes/qualities and downloads."""
    global selected_quality
    episodes_url.clear()

    print(f"\n=== {anime_url}  (episodes {from_ep}-{to_ep}) ===")
    get_episodes_url(anime_url, driver)

    if not episodes_url:
        print("No episodes found. Make sure you entered a valid witanime URL.")
        return

    print(f"\nScanning available qualities starting from episode {from_ep}...")
    available = {}
    quality_source_ep = None
    for ep_num in range(from_ep, to_ep + 1):
        ep_url = episodes_url.get(ep_num)
        if not ep_url:
            continue
        available = scan_available_qualities(ep_url, driver)
        if available:
            quality_source_ep = ep_num
            break
        print(f"  Episode {ep_num}: no MediaFire qualities found, trying next episode...")

    if not available:
        print("No MediaFire download qualities found for any episode in this range, skipping this anime.")
        return

    print(f"\nAvailable qualities: {', '.join(available.keys())}")

    if selected_quality and selected_quality in available:
        quality = selected_quality
        print(f"Using quality from earlier selection: {quality}")
    elif len(available) == 1:
        quality = list(available.keys())[0]
        print(f"Only one quality available, using: {quality}")
    else:
        quality = input(f"Choose quality ({' / '.join(available.keys())}): ").strip().upper()
        while quality not in available:
            print(f"Invalid. Pick from: {', '.join(available.keys())}")
            quality = input(f"Choose quality ({' / '.join(available.keys())}): ").strip().upper()
        selected_quality = quality  # carry this choice forward to the rest of the queue

    print(f"\nDownloading {quality} quality to: {os.path.abspath(out_dir)}\n")

    for ep_num in range(from_ep, to_ep + 1):
        if ep_num not in episodes_url:
            print(f"Episode {ep_num} not found, skipping.")
            continue

        print(f"── Episode {ep_num} ──")

        if ep_num == quality_source_ep:
            ep_qualities = available
        else:
            ep_qualities = scan_available_qualities(episodes_url[ep_num], driver)

        if quality not in ep_qualities:
            if ep_qualities:
                fallback = list(ep_qualities.keys())[0]
                print(f"  {quality} not available, falling back to {fallback}")
                data_index = ep_qualities[fallback]
            else:
                print(f"  No MediaFire qualities found, skipping.")
                continue
        else:
            data_index = ep_qualities[quality]

        success = download_via_browser(episodes_url[ep_num], driver, data_index, out_dir)
        if not success:
            print(f"  Failed to download episode {ep_num}.")


# ── Main ──────────────────────────────────────────────────────────────────────
out_dir = input("Output folder (leave blank for current dir): ").strip() or "."
os.makedirs(out_dir, exist_ok=True)

queue = collect_requests()

your_hwnd = get_foreground_window()
driver = make_driver(out_dir)
restore_foreground(your_hwnd)  # take focus back right after Brave opens

try:
    for anime_url, from_ep, to_ep in queue:
        try:
            process_anime(driver, out_dir, anime_url, from_ep, to_ep)
        except Exception as e:
            print(f"\n  Unexpected error on this anime, skipping it and moving on: {e}")
finally:
    driver.quit()