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
import pandas as pd
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


# === CONFIGURATION ===
CAMPAIGNS_DIR = Path("campaigns")
WAIT_BETWEEN_MESSAGES = 3      # seconds between each message
PAGE_LOAD_TIMEOUT = 60         # seconds to wait for initial WhatsApp load
MESSAGE_LOAD_TIMEOUT = 10      # seconds to wait for each message to load
CHROME_PROFILE_DIR = str(Path.home() / "whatsapp_chrome_profile")

TRACKING_COLUMNS = [
    "first_name", "phone_number", "status", "sent_at",
    "responded", "interested", "followup_sent", "followup2_sent", "followup3_sent",
    "reminder_sent", "referrer", "referral_sent", "ask_to_refer_sent", "paid",
]


# === CONTACT & TEMPLATE LOADING ===

def load_contacts(csv_path: str) -> pd.DataFrame:
    """Load contacts from CSV file."""
    df = pd.read_csv(csv_path, encoding="utf-8-sig")

    # Validate required columns
    required_cols = ["first_name", "phone_number"]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")

    # Clean phone numbers - remove spaces, ensure string type
    df["phone_number"] = df["phone_number"].astype(str).str.replace(" ", "")

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
    df = pd.read_csv(tracking_path, encoding="utf-8-sig")
    # Ensure all expected columns exist
    for col in TRACKING_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["phone_number"] = df["phone_number"].astype(str).str.replace(" ", "")
    return df


def init_tracking(contacts: pd.DataFrame) -> pd.DataFrame:
    """Create initial tracking DataFrame from contacts list."""
    tracking = contacts[["first_name", "phone_number"]].copy()
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
    return tracking


def save_tracking(campaign_dir: Path, tracking: pd.DataFrame):
    """Save tracking DataFrame to CSV."""
    tracking_path = campaign_dir / "tracking.csv"
    tracking.to_csv(tracking_path, index=False)


def update_tracking_row(tracking: pd.DataFrame, phone_number: str, **kwargs):
    """Update a specific row in tracking by phone number."""
    mask = tracking["phone_number"].astype(str) == str(phone_number)
    for key, value in kwargs.items():
        tracking.loc[mask, key] = value


# === SELENIUM / WHATSAPP WEB ===

def create_driver() -> webdriver.Chrome:
    """Create Chrome WebDriver with persistent profile for WhatsApp session."""
    options = Options()

    # Use a persistent profile to remember WhatsApp login
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")

    # Recommended options for stability
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    # Auto-download and manage ChromeDriver
    service = Service(ChromeDriverManager().install())

    return webdriver.Chrome(service=service, options=options)


def wait_for_whatsapp_load(driver: webdriver.Chrome, timeout: int = PAGE_LOAD_TIMEOUT):
    """Wait for WhatsApp Web to fully load (either QR code or main interface)."""
    wait = WebDriverWait(driver, timeout)

    # Try multiple selectors since WhatsApp Web changes frequently
    possible_selectors = [
        "div[contenteditable='true'][data-tab='10']",  # Old message input
        "div[contenteditable='true']",  # Generic message input
        "canvas",  # QR code
        "canvas[aria-label]",  # QR code with aria-label
        "[data-testid='qrcode']",  # QR code container
        "[data-testid='conversation-panel-wrapper']",  # Main chat panel
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


def send_message(driver: webdriver.Chrome, phone_number: str, message: str) -> bool:
    """
    Send a message to a specific phone number via WhatsApp Web.
    Returns True if successful, False otherwise.
    """
    # URL-encode the message to handle special characters and newlines
    encoded_message = urllib.parse.quote(message)

    # Remove any '+' prefix for the URL (WhatsApp expects numbers without '+')
    clean_number = phone_number.lstrip("+")

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

        # Click send immediately
        send_button.click()

        # Brief wait to confirm message sent
        time.sleep(0.5)

        return True

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
        with open(campaign_dir / "contacts.csv", "w") as f:
            f.write("first_name,phone_number\n")
        print(f"   Created empty contacts.csv (add your contacts)")

    # Copy message template if provided
    if message_path and Path(message_path).exists():
        shutil.copy(message_path, campaign_dir / "message.md")
        print(f"   Copied message template from {message_path}")
    else:
        with open(campaign_dir / "message.md", "w") as f:
            f.write("Hi {first_name}\n\nYour message here.\n")
        print(f"   Created template message.md (edit with your message)")

    # Create follow-up and reminder templates
    with open(campaign_dir / "followup.md", "w") as f:
        f.write("Hi {first_name}\n\nThanks for your response! Your follow-up message here.\n")
    print(f"   Created template followup.md")

    with open(campaign_dir / "reminder.md", "w") as f:
        f.write("Hi {first_name}\n\nJust following up on my earlier message. Your reminder here.\n")
    print(f"   Created template reminder.md")

    with open(campaign_dir / "followup2.md", "w") as f:
        f.write("Hi {first_name}\n\nYour second follow-up message here.\n")
    print(f"   Created template followup2.md")

    with open(campaign_dir / "followup3.md", "w") as f:
        f.write("Hi {first_name}\n\nYour third follow-up message here.\n")
    print(f"   Created template followup3.md")

    with open(campaign_dir / "referral.md", "w") as f:
        f.write("Hi {first_name}\n\nWould you mind sharing this with your network? Your referral message here.\n")
    print(f"   Created template referral.md")

    with open(campaign_dir / "ask_to_refer.md", "w") as f:
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


def cmd_send(campaign_name: str):
    """Send initial messages for a campaign."""
    global shutdown_requested
    signal.signal(signal.SIGINT, signal_handler)

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
        existing_phones = set(tracking["phone_number"].astype(str))
        new_contacts = contacts[~contacts["phone_number"].astype(str).isin(existing_phones)]
        if len(new_contacts) > 0:
            new_tracking = init_tracking(new_contacts)
            tracking = pd.concat([tracking, new_tracking], ignore_index=True)
            print(f"   Added {len(new_contacts)} new contacts to tracking")

    # Filter to only pending contacts
    pending_mask = tracking["status"] == "pending"
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

        for idx, row in tracking.iterrows():
            if row["status"] != "pending":
                continue

            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            first_name = row["first_name"]
            phone = row["phone_number"]
            sent_this_run += 1

            print(f"[{sent_this_run}/{pending_count}] Sending to {first_name} ({phone})...")

            message = personalize_message(template, first_name)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if send_message(driver, phone, message):
                print(f"   ✓ Sent successfully")
                update_tracking_row(tracking, phone, status="sent", sent_at=now)
                successful += 1
            else:
                update_tracking_row(tracking, phone, status="failed")
                failed += 1

            # Save after each message (crash-safe)
            save_tracking(campaign_dir, tracking)

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
        print("\n🔒 Closing browser...")
        driver.quit()
        save_tracking(campaign_dir, tracking)

    # Summary
    print()
    cmd_status(campaign_name)

    if pending_count - sent_this_run > 0:
        print(f"\n   {pending_count - sent_this_run} contacts remaining. Run 'send' again to resume.")


def cmd_followup(campaign_name: str):
    """Send follow-up messages to contacts who responded."""
    global shutdown_requested
    signal.signal(signal.SIGINT, signal_handler)

    campaign_dir = get_campaign_dir(campaign_name)
    tracking = load_tracking(campaign_dir)

    if tracking.empty:
        print(f"No tracking data for '{campaign_name}'. Run 'send' first.")
        return

    template = load_campaign_template(campaign_dir, "followup.md")

    # Filter: responded=yes AND interested!=no AND followup_sent!=yes
    to_followup = tracking[
        (tracking["responded"].astype(str).str.lower() == "yes") &
        (tracking["interested"].astype(str).str.lower() != "no") &
        (tracking["followup_sent"].astype(str).str.lower() != "yes")
    ]

    if len(to_followup) == 0:
        print("No contacts to follow up with.")
        print("(Contacts need interested=yes and followup_sent!=yes in tracking.csv)")
        return

    print("=" * 50)
    print(f"Follow-up: {campaign_name}")
    print("=" * 50)
    print(f"Sending follow-up to {len(to_followup)} responders...")
    print("(Press Ctrl+C at any time to stop gracefully)")

    driver = create_driver()

    try:
        open_whatsapp(driver)
        print("\n📤 Sending follow-ups...\n")

        count = 0
        for idx, row in to_followup.iterrows():
            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            first_name = row["first_name"]
            phone = row["phone_number"]
            count += 1

            print(f"[{count}/{len(to_followup)}] Follow-up to {first_name} ({phone})...")

            message = personalize_message(template, first_name)

            if send_message(driver, phone, message):
                print(f"   ✓ Sent successfully")
                update_tracking_row(tracking, phone, followup_sent="yes")
            else:
                print(f"   ✗ Failed")

            save_tracking(campaign_dir, tracking)

            if shutdown_requested:
                break

            if count < len(to_followup):
                for _ in range(WAIT_BETWEEN_MESSAGES):
                    if shutdown_requested:
                        break
                    time.sleep(1)

    finally:
        print("\n🔒 Closing browser...")
        driver.quit()
        save_tracking(campaign_dir, tracking)

    print()
    cmd_status(campaign_name)


def cmd_remind(campaign_name: str):
    """Send reminder messages to contacts who did not respond."""
    global shutdown_requested
    signal.signal(signal.SIGINT, signal_handler)

    campaign_dir = get_campaign_dir(campaign_name)
    tracking = load_tracking(campaign_dir)

    if tracking.empty:
        print(f"No tracking data for '{campaign_name}'. Run 'send' first.")
        return

    template = load_campaign_template(campaign_dir, "reminder.md")

    # Filter: status=sent AND responded=no AND reminder_sent!=yes
    to_remind = tracking[
        (tracking["status"] == "sent") &
        (tracking["responded"].astype(str).str.lower() == "no") &
        (tracking["reminder_sent"].astype(str).str.lower() != "yes")
    ]

    if len(to_remind) == 0:
        print("No contacts to remind.")
        print("(Contacts need status=sent, responded=no, and reminder_sent!=yes in tracking.csv)")
        return

    print("=" * 50)
    print(f"Reminder: {campaign_name}")
    print("=" * 50)
    print(f"Sending reminder to {len(to_remind)} non-responders...")
    print("(Press Ctrl+C at any time to stop gracefully)")

    driver = create_driver()

    try:
        open_whatsapp(driver)
        print("\n📤 Sending reminders...\n")

        count = 0
        for idx, row in to_remind.iterrows():
            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            first_name = row["first_name"]
            phone = row["phone_number"]
            count += 1

            print(f"[{count}/{len(to_remind)}] Reminder to {first_name} ({phone})...")

            message = personalize_message(template, first_name)

            if send_message(driver, phone, message):
                print(f"   ✓ Sent successfully")
                update_tracking_row(tracking, phone, reminder_sent="yes")
            else:
                print(f"   ✗ Failed")

            save_tracking(campaign_dir, tracking)

            if shutdown_requested:
                break

            if count < len(to_remind):
                for _ in range(WAIT_BETWEEN_MESSAGES):
                    if shutdown_requested:
                        break
                    time.sleep(1)

    finally:
        print("\n🔒 Closing browser...")
        driver.quit()
        save_tracking(campaign_dir, tracking)

    print()
    cmd_status(campaign_name)


def _send_targeted(campaign_name: str, template_file: str, filter_fn, tracking_col: str, label: str):
    """Generic helper: filter contacts, send a template, update a tracking column."""
    global shutdown_requested
    signal.signal(signal.SIGINT, signal_handler)

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
        for idx, row in to_send.iterrows():
            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            first_name = row["first_name"]
            phone = row["phone_number"]
            count += 1

            print(f"[{count}/{len(to_send)}] {label} to {first_name} ({phone})...")

            message = personalize_message(template, first_name)

            if send_message(driver, phone, message):
                print(f"   ✓ Sent successfully")
                update_tracking_row(tracking, phone, **{tracking_col: "yes"})
            else:
                print(f"   ✗ Failed")

            save_tracking(campaign_dir, tracking)

            if shutdown_requested:
                break

            if count < len(to_send):
                for _ in range(WAIT_BETWEEN_MESSAGES):
                    if shutdown_requested:
                        break
                    time.sleep(1)

    finally:
        print("\n🔒 Closing browser...")
        driver.quit()
        save_tracking(campaign_dir, tracking)

    print()
    cmd_status(campaign_name)


def cmd_followup2(campaign_name: str):
    """Send second follow-up to interested contacts who received first follow-up."""
    def filter_fn(tracking):
        return tracking[
            (tracking["responded"].astype(str).str.lower() == "yes") &
            (tracking["interested"].astype(str).str.lower() != "no") &
            (tracking["followup_sent"].astype(str).str.lower() == "yes") &
            (tracking["followup2_sent"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "followup2.md", filter_fn, "followup2_sent", "Follow-up 2")


def cmd_followup3(campaign_name: str):
    """Send third follow-up to interested contacts who received second follow-up."""
    def filter_fn(tracking):
        return tracking[
            (tracking["responded"].astype(str).str.lower() == "yes") &
            (tracking["interested"].astype(str).str.lower() != "no") &
            (tracking["followup2_sent"].astype(str).str.lower() == "yes") &
            (tracking["followup3_sent"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "followup3.md", filter_fn, "followup3_sent", "Follow-up 3")


def cmd_ask_to_refer(campaign_name: str):
    """Ask all responders if they'd be willing to refer others."""
    def filter_fn(tracking):
        return tracking[
            (tracking["responded"].astype(str).str.lower() == "yes") &
            (tracking["ask_to_refer_sent"].astype(str).str.lower() != "yes")
        ]
    _send_targeted(campaign_name, "ask_to_refer.md", filter_fn, "ask_to_refer_sent", "Ask to refer")


def cmd_referral(campaign_name: str):
    """Send referral messages to contacts willing to refer others."""
    global shutdown_requested
    signal.signal(signal.SIGINT, signal_handler)

    campaign_dir = get_campaign_dir(campaign_name)
    tracking = load_tracking(campaign_dir)

    if tracking.empty:
        print(f"No tracking data for '{campaign_name}'. Run 'send' first.")
        return

    template = load_campaign_template(campaign_dir, "referral.md")

    # Filter: referrer=yes AND referral_sent!=yes
    to_refer = tracking[
        (tracking["referrer"].astype(str).str.lower() == "yes") &
        (tracking["referral_sent"].astype(str).str.lower() != "yes")
    ]

    if len(to_refer) == 0:
        print("No contacts to send referral messages to.")
        print("(Contacts need referrer=yes and referral_sent!=yes in tracking.csv)")
        return

    print("=" * 50)
    print(f"Referral: {campaign_name}")
    print("=" * 50)
    print(f"Sending referral message to {len(to_refer)} referrers...")
    print("(Press Ctrl+C at any time to stop gracefully)")

    driver = create_driver()

    try:
        open_whatsapp(driver)
        print("\n📤 Sending referral messages...\n")

        count = 0
        for idx, row in to_refer.iterrows():
            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            first_name = row["first_name"]
            phone = row["phone_number"]
            count += 1

            print(f"[{count}/{len(to_refer)}] Referral to {first_name} ({phone})...")

            message = personalize_message(template, first_name)

            if send_message(driver, phone, message):
                print(f"   ✓ Sent successfully")
                update_tracking_row(tracking, phone, referral_sent="yes")
            else:
                print(f"   ✗ Failed")

            save_tracking(campaign_dir, tracking)

            if shutdown_requested:
                break

            if count < len(to_refer):
                for _ in range(WAIT_BETWEEN_MESSAGES):
                    if shutdown_requested:
                        break
                    time.sleep(1)

    finally:
        print("\n🔒 Closing browser...")
        driver.quit()
        save_tracking(campaign_dir, tracking)

    print()
    cmd_status(campaign_name)


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
