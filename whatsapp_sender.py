"""
WhatsApp Web Message Automation Script with Campaign Management

Activate Virtual Environment
    source venv/bin/activate

Deactivate Virtual Environment
    deactivate

Requirements:
    pip install selenium pandas webdriver-manager

Usage:
    python whatsapp_sender.py create <campaign> [--contacts file.csv] [--message file.md]
    python whatsapp_sender.py send <campaign>
    python whatsapp_sender.py followup <campaign>
    python whatsapp_sender.py followup2 <campaign>
    python whatsapp_sender.py followup3 <campaign>
    python whatsapp_sender.py remind <campaign>
    python whatsapp_sender.py referral <campaign>
    python whatsapp_sender.py askrefer <campaign>
    python whatsapp_sender.py status <campaign>

First run: You'll need to scan the QR code to log into WhatsApp Web.
Subsequent runs: Session persists via Chrome profile.
"""

import argparse
import contextlib
import functools
import os
import pandas as pd
import re
import shutil
import time
import urllib.parse
import signal
import sys
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager


# === GLOBAL STATE ===
shutdown_requested = False


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully."""
    global shutdown_requested
    shutdown_requested = True
    print("\n\n⚠️  Shutdown requested. Finishing current message and cleaning up...")


def request_shutdown(reason: str = "API cancel"):
    """Cooperative shutdown from outside the signal-handler path.

    Used by the Flask UI's /api/cancel/<job_id> endpoint so users can stop a
    running campaign without killing the Python process. The running send
    loop polls ``shutdown_requested`` at every safe point and exits cleanly.
    """
    global shutdown_requested
    shutdown_requested = True
    print(f"\n⚠️  Shutdown requested ({reason}). Finishing current message and cleaning up...")


def reset_shutdown():
    """Clear the shutdown flag. Call at the start of each command run so a
    previous cancel doesn't leak into the next one."""
    global shutdown_requested
    shutdown_requested = False


# === CONFIGURATION ===
CAMPAIGNS_DIR = Path("campaigns")
WAIT_BETWEEN_MESSAGES = 3      # seconds between each message
PAGE_LOAD_TIMEOUT = 60         # seconds to wait for initial WhatsApp load
MESSAGE_LOAD_TIMEOUT = 10      # seconds to wait for each message to load
CHROME_PROFILE_DIR = str(Path.home() / "whatsapp_chrome_profile")


# === CAMPAIGN LOCK ===

class CampaignBusyError(RuntimeError):
    """Raised when another process is already running a command for this campaign."""


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID is currently running.

    Cross-platform: uses os.kill(pid, 0) which raises OSError for dead PIDs
    on Unix and Windows alike.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else — still counts as alive.
        return True
    except OSError:
        # Windows: ERROR_ACCESS_DENIED means the PID exists.
        return True
    return True


@contextlib.contextmanager
def campaign_lock(campaign_dir: Path):
    """Exclusive per-campaign lock.

    Prevents two concurrent runs (CLI or Flask UI) from clobbering tracking.csv.
    Writes the current PID into ``<campaign_dir>/.lock``. Stale locks (PID no
    longer alive) are reclaimed automatically.

    Raises CampaignBusyError if another live process holds the lock.
    """
    lock_path = campaign_dir / ".lock"

    # Reclaim stale locks
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip() or "0")
        except (ValueError, OSError):
            existing_pid = 0
        if existing_pid and _pid_alive(existing_pid) and existing_pid != os.getpid():
            raise CampaignBusyError(
                f"Campaign '{campaign_dir.name}' is already running (PID {existing_pid}). "
                f"If you're sure nothing is running, delete {lock_path}"
            )
        # Stale or our own: remove and continue
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass

    # Atomic create — fails if another process created it between our check and now
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise CampaignBusyError(
            f"Campaign '{campaign_dir.name}' just became busy. Try again."
        )
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    finally:
        os.close(fd)

    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _install_signal_handler():
    """Register SIGINT handler for graceful Ctrl+C.

    Silently no-ops when called from a non-main thread (e.g. the Flask UI),
    where ``signal.signal`` raises ValueError.
    """
    try:
        signal.signal(signal.SIGINT, signal_handler)
    except ValueError:
        pass  # Not the main thread — signal handler already installed or unavailable.


class _TeeStream:
    """Duplicate writes to several streams.

    Used to mirror the command's stdout into a per-campaign log file while
    still letting the Flask UI's stdout capture (app.py ``_QueueWriter``) and
    the terminal see the output. Failures on any one stream are swallowed so
    a log-file error never takes down the send loop.
    """
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s: str) -> int:
        for s_ in self._streams:
            try:
                s_.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self) -> None:
        for s_ in self._streams:
            try:
                s_.flush()
            except Exception:
                pass


@contextlib.contextmanager
def _campaign_log_file(campaign_dir: Path, command_name: str):
    """Open a per-campaign log file and tee stdout into it for the duration.

    Log files live in ``<campaign_dir>/logs/<timestamp>-<command>.log`` and
    preserve a full record of each run so problems can be diagnosed after the
    fact. Silent-no-ops if the directory can't be created.
    """
    log_path = None
    handle = None
    try:
        logs_dir = campaign_dir / "logs"
        logs_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = logs_dir / f"{timestamp}-{command_name}.log"
        handle = open(log_path, "w", encoding="utf-8", buffering=1)  # line-buffered
        handle.write(f"# {command_name} for '{campaign_dir.name}' at {datetime.now().isoformat()}\n")
    except OSError as e:
        # Logging is best-effort; don't block the command if we can't open the file.
        print(f"   ⚠ Could not open log file: {e}")
        handle = None

    original_stdout = sys.stdout
    if handle is not None:
        sys.stdout = _TeeStream(original_stdout, handle)
    try:
        yield log_path
    finally:
        sys.stdout = original_stdout
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def _campaign_run(func):
    """Decorator for cmd_* functions that open a browser session.

    - Installs the SIGINT handler once (safely no-ops off the main thread).
    - Acquires an exclusive per-campaign lock file so two concurrent runs
      (CLI + UI, or two CLIs) can't clobber tracking.csv.
    - Opens a per-campaign log file for the duration of the command and
      tees stdout into it so every run leaves a persistent trail in
      ``campaigns/<name>/logs/``.

    The wrapped function must accept ``campaign_name`` as its first argument.
    """
    @functools.wraps(func)
    def wrapper(campaign_name: str, *args, **kwargs):
        _install_signal_handler()
        reset_shutdown()  # clear any lingering flag from a previous cancelled run
        campaign_dir = get_campaign_dir(campaign_name)
        try:
            with campaign_lock(campaign_dir):
                with _campaign_log_file(campaign_dir, func.__name__.removeprefix("cmd_")):
                    return func(campaign_name, *args, **kwargs)
        except CampaignBusyError as e:
            print(f"❌ {e}")
            return None
    return wrapper

TRACKING_COLUMNS = [
    "first_name", "last_name", "phone_number", "status", "sent_at",
    "responded", "interested", "followup_sent", "followup2_sent", "followup3_sent",
    "reminder_sent", "referrer", "referral_sent", "ask_to_refer_sent", "paid", "locked", "notes",
]


# === CONTACT & TEMPLATE LOADING ===

def normalize_phone(series: pd.Series) -> pd.Series:
    """Normalize phone numbers: remove +, spaces, parens, dashes, and .0 float suffix."""
    return (
        series.astype(str)
        .str.replace(r"\.0$", "", regex=True)   # float suffix from pandas
        .str.replace(r"[^\d]", "", regex=True)   # keep only digits
    )


def load_contacts(csv_path: str) -> pd.DataFrame:
    """Load contacts from CSV file."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype={"phone_number": str})

    # Validate required columns
    required_cols = ["first_name", "last_name", "phone_number"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")

    # Drop rows with missing first_name or phone_number
    df = df.dropna(subset=["first_name", "phone_number"])

    # Clean phone numbers - normalize to digits only
    df["phone_number"] = normalize_phone(df["phone_number"])
    # Remove rows where phone is empty after normalization
    df = df[df["phone_number"] != ""]
    # Fill missing last names with empty string
    df["last_name"] = df["last_name"].fillna("")

    return df


def load_message_template(md_path: str) -> str:
    """Load message template from markdown file."""
    with open(md_path, "r", encoding="utf-8") as f:
        return f.read()


def personalize_message(template: str, first_name: str) -> str:
    """Replace placeholders in template with actual values."""
    return template.format(first_name=first_name)


# === CAMPAIGN DIRECTORY HELPERS ===

def get_campaign_dir(campaign_name: str) -> Path:
    """Return campaign directory path, exit if it doesn't exist."""
    campaign_dir = CAMPAIGNS_DIR / campaign_name
    if not campaign_dir.exists():
        print(f"Campaign '{campaign_name}' not found in {CAMPAIGNS_DIR}/")
        available = list_campaigns()
        if available:
            print(f"Available campaigns: {', '.join(available)}")
        else:
            print("No campaigns exist yet. Create one with: python whatsapp_sender.py create <name>")
        sys.exit(1)
    return campaign_dir


def list_campaigns() -> list:
    """List all campaign directory names."""
    if not CAMPAIGNS_DIR.exists():
        return []
    return sorted([d.name for d in CAMPAIGNS_DIR.iterdir() if d.is_dir()])


def load_campaign_contacts(campaign_dir: Path) -> pd.DataFrame:
    """Load contacts.csv from a campaign directory."""
    csv_path = campaign_dir / "contacts.csv"
    if not csv_path.exists():
        print(f"No contacts.csv found in {campaign_dir}/")
        sys.exit(1)
    return load_contacts(str(csv_path))


def load_campaign_template(campaign_dir: Path, template_name: str) -> str:
    """Load a message template (message.md, followup.md, or reminder.md)."""
    md_path = campaign_dir / template_name
    if not md_path.exists():
        print(f"No {template_name} found in {campaign_dir}/")
        print(f"Create it before running this command.")
        sys.exit(1)
    return load_message_template(str(md_path))


# === TRACKING CSV MANAGEMENT ===

def load_tracking(campaign_dir: Path) -> pd.DataFrame:
    """Load tracking.csv from campaign. Returns empty DataFrame if not found."""
    tracking_path = campaign_dir / "tracking.csv"
    if not tracking_path.exists():
        return pd.DataFrame(columns=TRACKING_COLUMNS)
    df = pd.read_csv(tracking_path, encoding="utf-8-sig", dtype={"phone_number": str})
    # Ensure all expected columns exist
    for col in TRACKING_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["phone_number"] = normalize_phone(df["phone_number"])
    return df


def init_tracking(contacts: pd.DataFrame) -> pd.DataFrame:
    """Create initial tracking DataFrame from contacts list."""
    tracking = contacts[["first_name", "last_name", "phone_number"]].copy()
    tracking["status"] = "pending"
    tracking["sent_at"] = ""
    tracking["responded"] = "no"
    tracking["interested"] = ""
    tracking["followup_sent"] = "no"
    tracking["followup2_sent"] = "no"
    tracking["followup3_sent"] = "no"
    tracking["reminder_sent"] = "no"
    tracking["referrer"] = ""
    tracking["referral_sent"] = "no"
    tracking["ask_to_refer_sent"] = "no"
    tracking["paid"] = "no"
    tracking["locked"] = ""
    tracking["notes"] = ""
    return tracking


def save_tracking(campaign_dir: Path, tracking: pd.DataFrame):
    """Save tracking DataFrame to CSV."""
    tracking_path = campaign_dir / "tracking.csv"
    # Normalize phone numbers before saving to prevent .0 float suffix
    tracking = tracking.copy()
    tracking["phone_number"] = normalize_phone(tracking["phone_number"])
    # encoding="utf-8" is explicit so non-ASCII names/notes round-trip correctly
    # on Windows (where the default is cp1252) and any other non-UTF-8 locale.
    tracking.to_csv(tracking_path, index=False, encoding="utf-8")


def update_tracking_row(tracking: pd.DataFrame, phone_number: str, **kwargs):
    """Update a specific row in tracking by phone number."""
    import re
    normalized = re.sub(r"[^\d]", "", str(phone_number).replace(".0", ""))
    mask = normalize_phone(tracking["phone_number"]) == normalized
    for key, value in kwargs.items():
        tracking.loc[mask, key] = value


# === SELENIUM / WHATSAPP WEB ===

# Files Chrome creates in the profile directory to prevent concurrent launches.
# When Chrome crashes, these linger and block the next start-up with a cryptic
# "profile already in use" error. We sweep them if no Chrome process holds them.
_CHROME_STALE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _sweep_stale_chrome_locks(profile_dir: str) -> None:
    """Remove Chrome singleton lock files if they're stale.

    On POSIX these are symlinks of the form ``pid-hostname``; if the pid is
    dead we can safely unlink. On Windows they're regular files and always
    removable when Chrome isn't running. This is a best-effort sweep — we
    don't block the launch if removal fails for any reason.
    """
    profile_path = Path(profile_dir)
    if not profile_path.exists():
        return

    for name in _CHROME_STALE_LOCKS:
        lock_file = profile_path / name
        if not lock_file.exists() and not lock_file.is_symlink():
            continue

        should_remove = True
        # POSIX: SingletonLock is a symlink "pid-hostname" — check if pid is alive
        if lock_file.is_symlink():
            try:
                target = os.readlink(lock_file)
                pid_str = target.split("-", 1)[0]
                pid = int(pid_str)
                if _pid_alive(pid):
                    should_remove = False
            except (OSError, ValueError):
                # Unreadable target or unparseable pid — treat as stale
                pass

        if should_remove:
            try:
                lock_file.unlink()
                print(f"   Removed stale Chrome lock: {lock_file.name}")
            except OSError as e:
                # Don't block launch if we can't remove — Chrome itself may give a clearer error
                print(f"   ⚠ Could not remove {lock_file.name}: {e}")


def create_driver() -> webdriver.Chrome:
    """Create Chrome WebDriver with persistent profile for WhatsApp session."""
    # Sweep stale lock files from a prior crashed Chrome before launch.
    _sweep_stale_chrome_locks(CHROME_PROFILE_DIR)

    options = Options()

    # Use a persistent profile to remember WhatsApp login
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")

    # Recommended options for stability
    # --no-sandbox and --disable-dev-shm-usage target Linux-specific issues
    # (sandbox perms on headless Linux, /dev/shm size limits in containers).
    # They're harmless on macOS/Windows but noisy — gate them.
    if sys.platform.startswith("linux"):
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")

    # Auto-download and manage ChromeDriver
    service = Service(ChromeDriverManager().install())

    return webdriver.Chrome(service=service, options=options)


def wait_for_whatsapp_load(driver: webdriver.Chrome, timeout: int = PAGE_LOAD_TIMEOUT):
    """Wait for WhatsApp Web to fully load (either QR code or main interface)."""
    wait = WebDriverWait(driver, timeout)

    # Try multiple selectors since WhatsApp Web changes frequently
    possible_selectors = [
        "[data-testid='chat-list']",  # Chat list panel
        "[data-testid='conversation-panel-wrapper']",  # Main chat panel
        "[data-testid='qrcode']",  # QR code container
        "div[contenteditable='true'][data-tab='10']",  # Old message input
        "div[contenteditable='true']",  # Generic message input
        "canvas[aria-label*='QR']",  # QR code canvas
        "canvas[aria-label*='qr']",  # QR code canvas (lowercase)
        "#app .two",  # Main app loaded
        "#side",  # Side panel (chat list)
    ]

    selector = ", ".join(possible_selectors)

    try:
        wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
        print("   ✓ WhatsApp Web elements detected")
    except TimeoutException:
        print("\n⚠️  Could not detect WhatsApp Web elements. Debugging info:")
        print(f"   Current URL: {driver.current_url}")
        print(f"   Page title: {driver.title}")
        print("\n   Trying to continue anyway - please manually verify WhatsApp is loaded.")
        print("   If you see WhatsApp Web in the browser, you can proceed.")


class BackoffTracker:
    """Track consecutive send failures and apply escalating backoff.

    WhatsApp Web silently throttles accounts that bulk-send; symptoms are a
    run of timeouts where the send button never becomes clickable. Pressing
    through the throttle is the fastest way to get a temporary account ban,
    so we pause on repeated failures and give up entirely after too many.

    Thresholds chosen conservatively:
      3 fails in a row  → 60s pause
      6 fails in a row  → 120s pause
      10 fails in a row → abort the whole command
    """
    FIRST_PAUSE = 3
    SECOND_PAUSE = 6
    ABORT_THRESHOLD = 10
    SHORT_BACKOFF_S = 60
    LONG_BACKOFF_S = 120

    def __init__(self):
        self.consecutive_failures = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> tuple[int, bool]:
        """Increment failure counter. Returns (backoff_seconds, should_abort)."""
        self.consecutive_failures += 1
        n = self.consecutive_failures
        if n >= self.ABORT_THRESHOLD:
            return (0, True)
        if n >= self.SECOND_PAUSE:
            return (self.LONG_BACKOFF_S, False)
        if n >= self.FIRST_PAUSE:
            return (self.SHORT_BACKOFF_S, False)
        return (0, False)


def _apply_backoff_if_needed(send_ok: bool, tracker: BackoffTracker) -> bool:
    """Update backoff tracker, print status, sleep if needed.

    Returns True to continue sending, False to abort the run (caller should
    break out of its send loop). Respects ``shutdown_requested`` during sleep
    so the user's cancel still works during a backoff window.
    """
    if send_ok:
        tracker.record_success()
        return True

    backoff, abort = tracker.record_failure()
    if abort:
        print(f"\n🚨 {tracker.consecutive_failures} consecutive failures — aborting run to "
              "protect the WhatsApp account. Check connectivity, the number list, "
              "and try again later.")
        return False
    if backoff > 0:
        print(f"   ⚠ {tracker.consecutive_failures} failures in a row — backing off {backoff}s "
              "before next send...")
        for _ in range(backoff):
            if shutdown_requested:
                break
            time.sleep(1)
    return True


# Seconds to wait for the delivery-tick (check mark) to appear after clicking Send.
# If this elapses with no tick, the message is still probably in WhatsApp's outbox
# but we can't confirm delivery — treat as best-effort success.
DELIVERY_CONFIRM_TIMEOUT = 5

# Selectors for the delivery-status icon that appears once WhatsApp has accepted
# the message for sending. These change periodically — keep multiple fallbacks.
_DELIVERY_TICK_SELECTORS = (
    "span[data-icon='msg-check']",        # single grey tick: sent from our end
    "span[data-icon='msg-dblcheck']",     # double grey tick: delivered
    "span[data-icon='msg-dblcheck-ack']", # double blue tick: read
    "span[data-icon='msg-time']",         # clock icon: still sending (counts as queued)
)


def _wait_for_delivery_tick(driver: webdriver.Chrome, timeout: int = DELIVERY_CONFIRM_TIMEOUT) -> bool:
    """Poll briefly for WhatsApp's post-send tick/clock icon.

    Returns True as soon as any delivery indicator is visible, False if the
    timeout elapses without one. This replaces a blind ``sleep(2)`` and catches
    cases where the send button was clicked but the message never left the
    outbox (network blip, account restriction, etc.).
    """
    try:
        wait = WebDriverWait(driver, timeout, poll_frequency=0.3)
        selector = ", ".join(_DELIVERY_TICK_SELECTORS)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
        return True
    except TimeoutException:
        return False


def send_message(driver: webdriver.Chrome, phone_number: str, message: str) -> bool:
    """
    Send a message to a specific phone number via WhatsApp Web.
    Returns True if the delivery tick was observed, False otherwise.
    """
    # URL-encode the message to handle special characters and newlines
    encoded_message = urllib.parse.quote(message)

    # Remove non-digit characters for the URL (WhatsApp expects digits only)
    clean_number = re.sub(r"[^\d]", "", str(phone_number))

    # Navigate to WhatsApp Web with pre-filled message
    url = f"https://web.whatsapp.com/send?phone={clean_number}&text={encoded_message}"
    driver.get(url)

    try:
        # Use shorter timeout for individual messages
        wait = WebDriverWait(driver, MESSAGE_LOAD_TIMEOUT)

        # Try multiple selectors for the send button
        send_button_selectors = [
            "span[data-icon='send']",  # Common selector
            "button[aria-label*='Send']",  # Aria label
            "[data-testid='send']",  # Test ID
        ]

        send_button = None
        for selector in send_button_selectors:
            try:
                send_button = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                break
            except TimeoutException:
                continue

        if not send_button:
            raise TimeoutException("Could not find send button with any known selector")

        # Click send
        send_button.click()

        # Verify delivery by waiting for the tick/clock icon. Falling back to a
        # short sleep on timeout keeps behaviour close to the old blind-sleep
        # path — the message likely still went out, we just can't confirm.
        if _wait_for_delivery_tick(driver):
            return True
        print("  ⚠ Send clicked but no delivery tick observed within "
              f"{DELIVERY_CONFIRM_TIMEOUT}s — treating as failed.")
        return False

    except TimeoutException:
        print(f"  ⚠ Could not load chat. Number may be invalid or not on WhatsApp.")
        return False
    except Exception as e:
        print(f"  ⚠ Error sending: {e}")
        return False


def open_whatsapp(driver: webdriver.Chrome):
    """Open WhatsApp Web and wait for it to load."""
    driver.get("https://web.whatsapp.com")
    print("\n⏳ Waiting for WhatsApp Web to load...")
    print("   (Scan QR code if this is your first time)")
    wait_for_whatsapp_load(driver)
    input("\n✅ WhatsApp loaded! Press Enter to start...")


# === COMMANDS ===

def cmd_create(campaign_name: str, contacts_path: str = None, message_path: str = None):
    """Create a new campaign directory with templates."""
    campaign_dir = CAMPAIGNS_DIR / campaign_name

    if campaign_dir.exists():
        print(f"Campaign '{campaign_name}' already exists at {campaign_dir}/")
        sys.exit(1)

    campaign_dir.mkdir(parents=True)

    # Copy contacts if provided, otherwise create template
    if contacts_path and Path(contacts_path).exists():
        shutil.copy(contacts_path, campaign_dir / "contacts.csv")
        df = load_contacts(contacts_path)
        print(f"   Copied contacts from {contacts_path} ({len(df)} contacts)")
    else:
        with open(campaign_dir / "contacts.csv", "w", encoding="utf-8") as f:
            f.write("first_name,last_name,phone_number\n")
        print(f"   Created empty contacts.csv (add your contacts)")

    # Copy message template if provided
    if message_path and Path(message_path).exists():
        shutil.copy(message_path, campaign_dir / "message.md")
        print(f"   Copied message template from {message_path}")
    else:
        with open(campaign_dir / "message.md", "w", encoding="utf-8") as f:
            f.write("Hi {first_name}\n\nYour message here.\n")
        print(f"   Created template message.md (edit with your message)")

    # Create follow-up and reminder templates
    with open(campaign_dir / "followup.md", "w", encoding="utf-8") as f:
        f.write("Hi {first_name}\n\nThanks for your response! Your follow-up message here.\n")
    print(f"   Created template followup.md")

    with open(campaign_dir / "reminder.md", "w", encoding="utf-8") as f:
        f.write("Hi {first_name}\n\nJust following up on my earlier message. Your reminder here.\n")
    print(f"   Created template reminder.md")

    with open(campaign_dir / "followup2.md", "w", encoding="utf-8") as f:
        f.write("Hi {first_name}\n\nYour second follow-up message here.\n")
    print(f"   Created template followup2.md")

    with open(campaign_dir / "followup3.md", "w", encoding="utf-8") as f:
        f.write("Hi {first_name}\n\nYour third follow-up message here.\n")
    print(f"   Created template followup3.md")

    with open(campaign_dir / "referral.md", "w", encoding="utf-8") as f:
        f.write("Hi {first_name}\n\nWould you mind sharing this with your network? Your referral message here.\n")
    print(f"   Created template referral.md")

    with open(campaign_dir / "ask_to_refer.md", "w", encoding="utf-8") as f:
        f.write("Hi {first_name}\n\nWould you be open to sharing this with anyone who might benefit? Your ask-to-refer message here.\n")
    print(f"   Created template ask_to_refer.md")

    print(f"\n✅ Campaign '{campaign_name}' created at {campaign_dir}/")
    print(f"\nNext steps:")
    print(f"  1. Edit {campaign_dir}/contacts.csv with your contacts")
    print(f"  2. Edit {campaign_dir}/message.md with your outreach message")
    print(f"  3. Edit {campaign_dir}/followup.md, followup2.md, followup3.md")
    print(f"  4. Edit {campaign_dir}/reminder.md with your reminder message")
    print(f"  5. Edit {campaign_dir}/referral.md and ask_to_refer.md")
    print(f"  6. Run: python whatsapp_sender.py send {campaign_name}")


@_campaign_run
def cmd_send(campaign_name: str):
    """Send initial messages for a campaign."""
    global shutdown_requested

    campaign_dir = get_campaign_dir(campaign_name)
    contacts = load_campaign_contacts(campaign_dir)
    template = load_campaign_template(campaign_dir, "message.md")

    print("=" * 50)
    print(f"Sending: {campaign_name}")
    print("=" * 50)
    print("(Press Ctrl+C at any time to stop gracefully)")

    # Load or initialize tracking
    tracking = load_tracking(campaign_dir)
    if tracking.empty:
        tracking = init_tracking(contacts)
    else:
        # Merge: add any new contacts not yet in tracking
        existing_phones = set(normalize_phone(tracking["phone_number"]))
        new_contacts = contacts[~normalize_phone(contacts["phone_number"]).isin(existing_phones)]
        if len(new_contacts) > 0:
            new_tracking = init_tracking(new_contacts)
            tracking = pd.concat([tracking, new_tracking], ignore_index=True)
            print(f"   Added {len(new_contacts)} new contacts to tracking")

    # Normalize blank status to "pending" (handles manually edited tracking.csv)
    tracking["status"] = tracking["status"].fillna("").replace("", "pending")

    # Filter to only pending, non-locked contacts
    pending_mask = (tracking["status"] == "pending") & (tracking["locked"].astype(str).str.lower() != "yes")
    pending_count = pending_mask.sum()
    already_done = len(tracking) - pending_count

    if pending_count == 0:
        print(f"\nAll {len(tracking)} contacts have already been processed. Nothing to send.")
        cmd_status(campaign_name)
        return

    if already_done > 0:
        print(f"\n   Resuming: {already_done} already processed, {pending_count} remaining")
    else:
        print(f"\n   {pending_count} contacts to send")

    print(f"   Message template: {len(template)} chars")

    # Start browser
    print("\n🌐 Starting Chrome browser...")
    driver = create_driver()

    try:
        open_whatsapp(driver)

        print("\n📤 Sending messages...\n")

        successful = 0
        failed = 0
        sent_this_run = 0
        tracker = BackoffTracker()

        for idx, row in tracking.iterrows():
            if row["status"] != "pending":
                continue
            if str(row.get("locked", "")).lower() == "yes":
                continue

            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            first_name = row["first_name"]
            last_name = row.get("last_name", "")
            phone = row["phone_number"]
            sent_this_run += 1

            display_name = f"{first_name} {last_name}".strip()
            print(f"[{sent_this_run}/{pending_count}] Sending to {display_name} ({phone})...")

            message = personalize_message(template, first_name)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ok = send_message(driver, phone, message)
            if ok:
                print(f"   ✓ Sent successfully")
                update_tracking_row(tracking, phone, status="sent", sent_at=now)
                successful += 1
            else:
                update_tracking_row(tracking, phone, status="failed")
                failed += 1

            # Save after each message (crash-safe)
            save_tracking(campaign_dir, tracking)

            if not _apply_backoff_if_needed(ok, tracker):
                break
            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            # Wait between messages to avoid rate limiting
            if sent_this_run < pending_count:
                print(f"   Waiting {WAIT_BETWEEN_MESSAGES}s before next message...")
                for _ in range(WAIT_BETWEEN_MESSAGES):
                    if shutdown_requested:
                        break
                    time.sleep(1)

    finally:
        print("\n⏳ Waiting 5s for last message to deliver...")
        time.sleep(5)
        print("🔒 Closing browser...")
        driver.quit()
        save_tracking(campaign_dir, tracking)

    # Summary
    print()
    cmd_status(campaign_name)

    if pending_count - sent_this_run > 0:
        print(f"\n   {pending_count - sent_this_run} contacts remaining. Run 'send' again to resume.")


@_campaign_run
def cmd_followup(campaign_name: str):
    """Send follow-up messages to contacts who responded."""
    def filter_fn(tracking):
        return tracking[
            (tracking["responded"].astype(str).str.lower() == "yes") &
            (tracking["interested"].astype(str).str.lower() != "no") &
            (tracking["followup_sent"].astype(str).str.lower() != "yes") &
            (tracking["locked"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "followup.md", filter_fn, "followup_sent", "Follow-up")


@_campaign_run
def cmd_remind(campaign_name: str):
    """Send reminder messages to contacts who did not respond."""
    def filter_fn(tracking):
        return tracking[
            (tracking["status"] == "sent") &
            (tracking["responded"].astype(str).str.lower() == "no") &
            (tracking["reminder_sent"].astype(str).str.lower() != "yes") &
            (tracking["locked"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "reminder.md", filter_fn, "reminder_sent", "Reminder")


def _send_targeted(campaign_name: str, template_file: str, filter_fn, tracking_col: str, label: str):
    """Generic helper: filter contacts, send a template, update a tracking column.

    Does NOT acquire the campaign lock — callers must be decorated with
    @_campaign_run so the lock is held at the outer command boundary.
    """
    global shutdown_requested

    campaign_dir = get_campaign_dir(campaign_name)
    tracking = load_tracking(campaign_dir)

    if tracking.empty:
        print(f"No tracking data for '{campaign_name}'. Run 'send' first.")
        return

    template = load_campaign_template(campaign_dir, template_file)
    to_send = filter_fn(tracking)

    if len(to_send) == 0:
        print(f"No contacts to send {label} to.")
        return

    print("=" * 50)
    print(f"{label}: {campaign_name}")
    print("=" * 50)
    print(f"Sending {label} to {len(to_send)} contacts...")
    print("(Press Ctrl+C at any time to stop gracefully)")

    driver = create_driver()

    try:
        open_whatsapp(driver)
        print(f"\n📤 Sending {label}...\n")

        count = 0
        tracker = BackoffTracker()
        for idx, row in to_send.iterrows():
            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            first_name = row["first_name"]
            last_name = row.get("last_name", "")
            phone = row["phone_number"]
            count += 1

            display_name = f"{first_name} {last_name}".strip()
            print(f"[{count}/{len(to_send)}] {label} to {display_name} ({phone})...")

            message = personalize_message(template, first_name)

            ok = send_message(driver, phone, message)
            if ok:
                print(f"   ✓ Sent successfully")
                update_tracking_row(tracking, phone, **{tracking_col: "yes"})
            else:
                print(f"   ✗ Failed")

            save_tracking(campaign_dir, tracking)

            if not _apply_backoff_if_needed(ok, tracker):
                break
            if shutdown_requested:
                break

            if count < len(to_send):
                for _ in range(WAIT_BETWEEN_MESSAGES):
                    if shutdown_requested:
                        break
                    time.sleep(1)

    finally:
        print("\n⏳ Waiting 5s for last message to deliver...")
        time.sleep(5)
        print("🔒 Closing browser...")
        driver.quit()
        save_tracking(campaign_dir, tracking)

    print()
    cmd_status(campaign_name)


@_campaign_run
def cmd_followup2(campaign_name: str):
    """Send second follow-up to interested contacts who received first follow-up."""
    def filter_fn(tracking):
        return tracking[
            (tracking["responded"].astype(str).str.lower() == "yes") &
            (tracking["interested"].astype(str).str.lower() != "no") &
            (tracking["followup_sent"].astype(str).str.lower() == "yes") &
            (tracking["followup2_sent"].astype(str).str.lower() != "yes") &
            (tracking["locked"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "followup2.md", filter_fn, "followup2_sent", "Follow-up 2")


@_campaign_run
def cmd_followup3(campaign_name: str):
    """Send third follow-up to interested contacts who received second follow-up."""
    def filter_fn(tracking):
        return tracking[
            (tracking["responded"].astype(str).str.lower() == "yes") &
            (tracking["interested"].astype(str).str.lower() != "no") &
            (tracking["followup2_sent"].astype(str).str.lower() == "yes") &
            (tracking["followup3_sent"].astype(str).str.lower() != "yes") &
            (tracking["locked"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "followup3.md", filter_fn, "followup3_sent", "Follow-up 3")


@_campaign_run
def cmd_ask_to_refer(campaign_name: str):
    """Ask all responders if they'd be willing to refer others."""
    def filter_fn(tracking):
        return tracking[
            (tracking["responded"].astype(str).str.lower() == "yes") &
            (tracking["ask_to_refer_sent"].astype(str).str.lower() != "yes") &
            (tracking["locked"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "ask_to_refer.md", filter_fn, "ask_to_refer_sent", "Ask to refer")


@_campaign_run
def cmd_referral(campaign_name: str):
    """Send referral messages to contacts willing to refer others."""
    def filter_fn(tracking):
        return tracking[
            (tracking["referrer"].astype(str).str.lower() == "yes") &
            (tracking["referral_sent"].astype(str).str.lower() != "yes") &
            (tracking["locked"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "referral.md", filter_fn, "referral_sent", "Referral")


def cmd_status(campaign_name: str):
    """Show campaign status summary."""
    campaign_dir = get_campaign_dir(campaign_name)
    tracking = load_tracking(campaign_dir)

    if tracking.empty:
        print(f"Campaign '{campaign_name}' has no tracking data yet. Run 'send' first.")
        return

    total = len(tracking)
    sent = len(tracking[tracking["status"] == "sent"])
    failed = len(tracking[tracking["status"] == "failed"])
    pending = len(tracking[tracking["status"] == "pending"])
    responded = len(tracking[tracking["responded"].astype(str).str.lower() == "yes"])
    interested = len(tracking[tracking["interested"].astype(str).str.lower() == "yes"])
    not_interested = len(tracking[tracking["interested"].astype(str).str.lower() == "no"])
    followup_done = len(tracking[tracking["followup_sent"].astype(str).str.lower() == "yes"])
    followup2_done = len(tracking[tracking["followup2_sent"].astype(str).str.lower() == "yes"])
    followup3_done = len(tracking[tracking["followup3_sent"].astype(str).str.lower() == "yes"])
    reminder_done = len(tracking[tracking["reminder_sent"].astype(str).str.lower() == "yes"])
    referrers = len(tracking[tracking["referrer"].astype(str).str.lower() == "yes"])
    referral_done = len(tracking[tracking["referral_sent"].astype(str).str.lower() == "yes"])
    ask_to_refer_done = len(tracking[tracking["ask_to_refer_sent"].astype(str).str.lower() == "yes"])
    paid = len(tracking[tracking["paid"].astype(str).str.lower() == "yes"])

    print("=" * 50)
    print(f"Campaign: {campaign_name}")
    print("=" * 50)
    print(f"  Total contacts:      {total}")
    print(f"  Sent:                {sent}")
    print(f"  Failed:              {failed}")
    print(f"  Pending:             {pending}")
    print(f"  Responded:           {responded} / {sent}")
    print(f"  Interested:          {interested}")
    print(f"  Not interested:      {not_interested}")
    print(f"  Paid:                {paid}")
    print(f"  Follow-up 1 sent:    {followup_done}")
    print(f"  Follow-up 2 sent:    {followup2_done}")
    print(f"  Follow-up 3 sent:    {followup3_done}")
    print(f"  Reminders sent:      {reminder_done}")
    print(f"  Referrers:           {referrers}")
    print(f"  Referrals sent:      {referral_done}")
    print(f"  Ask to refer sent:   {ask_to_refer_done}")
    print(f"  Awaiting follow-up:  {max(0, interested - followup_done)}")
    print(f"  Awaiting follow-up2: {max(0, followup_done - followup2_done)}")
    print(f"  Awaiting follow-up3: {max(0, followup2_done - followup3_done)}")
    print(f"  Awaiting reminder:   {max(0, sent - responded - reminder_done)}")
    print(f"  Awaiting referral:   {max(0, referrers - referral_done)}")
    print(f"  Awaiting ask-refer:  {max(0, responded - ask_to_refer_done)}")
    print("=" * 50)
    print(f"  Tracking file: {campaign_dir / 'tracking.csv'}")


# === MAIN ENTRY POINT ===

def main():
    parser = argparse.ArgumentParser(
        description="WhatsApp Web Message Automation with Campaign Management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Workflow:
  1. create   - Set up a new campaign with contacts and message templates
  2. send     - Send initial messages (creates tracking.csv)
  3.          - Open tracking.csv in Excel, mark responded/interested/referrer
  4. status   - View campaign progress
  5. followup  - Send follow-up 1 to interested contacts
  6. followup2 - Send follow-up 2 (after followup 1)
  7. followup3 - Send follow-up 3 (after followup 2)
  8. remind    - Send reminder to non-responders
  9. referral  - Send forwardable message to referrers
 10. askrefer  - Ask all responders to refer others
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # create
    create_parser = subparsers.add_parser("create", help="Create a new campaign")
    create_parser.add_argument("campaign", help="Campaign name (becomes folder name)")
    create_parser.add_argument("--contacts", help="Path to contacts CSV to copy in")
    create_parser.add_argument("--message", help="Path to message template to copy in")

    # send
    send_parser = subparsers.add_parser("send", help="Send initial messages for a campaign")
    send_parser.add_argument("campaign", help="Campaign name")

    # followup
    followup_parser = subparsers.add_parser("followup", help="Send follow-up 1 to interested contacts")
    followup_parser.add_argument("campaign", help="Campaign name")

    # followup2
    followup2_parser = subparsers.add_parser("followup2", help="Send follow-up 2 (after followup 1)")
    followup2_parser.add_argument("campaign", help="Campaign name")

    # followup3
    followup3_parser = subparsers.add_parser("followup3", help="Send follow-up 3 (after followup 2)")
    followup3_parser.add_argument("campaign", help="Campaign name")

    # remind
    remind_parser = subparsers.add_parser("remind", help="Send reminder to non-responders")
    remind_parser.add_argument("campaign", help="Campaign name")

    # referral
    referral_parser = subparsers.add_parser("referral", help="Send forwardable message to referrers")
    referral_parser.add_argument("campaign", help="Campaign name")

    # askrefer
    askrefer_parser = subparsers.add_parser("askrefer", help="Ask all responders to refer others")
    askrefer_parser.add_argument("campaign", help="Campaign name")

    # status
    status_parser = subparsers.add_parser("status", help="Show campaign status summary")
    status_parser.add_argument("campaign", help="Campaign name")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        campaigns = list_campaigns()
        if campaigns:
            print(f"\nExisting campaigns: {', '.join(campaigns)}")
        return

    if args.command == "create":
        cmd_create(args.campaign, args.contacts, args.message)
    elif args.command == "send":
        cmd_send(args.campaign)
    elif args.command == "followup":
        cmd_followup(args.campaign)
    elif args.command == "followup2":
        cmd_followup2(args.campaign)
    elif args.command == "followup3":
        cmd_followup3(args.campaign)
    elif args.command == "remind":
        cmd_remind(args.campaign)
    elif args.command == "referral":
        cmd_referral(args.campaign)
    elif args.command == "askrefer":
        cmd_ask_to_refer(args.campaign)
    elif args.command == "status":
        cmd_status(args.campaign)


if __name__ == "__main__":
    main()
