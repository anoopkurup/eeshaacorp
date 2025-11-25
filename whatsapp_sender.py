"""
WhatsApp Web Message Automation Script

Requirements:
    pip install selenium pandas webdriver-manager

Usage:
    python whatsapp_sender.py

First run: You'll need to scan the QR code to log into WhatsApp Web.
Subsequent runs: Session persists via Chrome profile.
"""

import pandas as pd
import time
import urllib.parse
import signal
import sys
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
CONTACTS_CSV = "contacts.csv"
MESSAGE_TEMPLATE = "message_template.md"
WAIT_BETWEEN_MESSAGES = 3      # seconds between each message
PAGE_LOAD_TIMEOUT = 60         # seconds to wait for initial WhatsApp load
MESSAGE_LOAD_TIMEOUT = 10      # seconds to wait for each message to load
CHROME_PROFILE_DIR = str(Path.home() / "whatsapp_chrome_profile")


def load_contacts(csv_path: str) -> pd.DataFrame:
    """Load contacts from CSV file."""
    df = pd.read_csv(csv_path)
    
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


def main():
    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)

    print("=" * 50)
    print("WhatsApp Web Message Automation")
    print("=" * 50)
    print("(Press Ctrl+C at any time to stop gracefully)")

    # Load data
    print("\n📂 Loading contacts and message template...")
    contacts = load_contacts(CONTACTS_CSV)
    template = load_message_template(MESSAGE_TEMPLATE)
    
    print(f"   Found {len(contacts)} contacts")
    print(f"   Message template loaded ({len(template)} chars)")
    
    # Initialize browser
    print("\n🌐 Starting Chrome browser...")
    driver = create_driver()
    
    try:
        # Open WhatsApp Web
        driver.get("https://web.whatsapp.com")
        
        print("\n⏳ Waiting for WhatsApp Web to load...")
        print("   (Scan QR code if this is your first time)")
        
        wait_for_whatsapp_load(driver)
        
        # Give user time to scan QR if needed
        input("\n✅ WhatsApp loaded! Press Enter to start sending messages...")
        
        # Send messages
        print("\n📤 Sending messages...\n")
        
        successful = 0
        failed = 0
        
        for idx, row in contacts.iterrows():
            # Check if shutdown was requested
            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            first_name = row["first_name"]
            phone = row["phone_number"]

            print(f"[{idx + 1}/{len(contacts)}] Sending to {first_name} ({phone})...")

            # Personalize the message
            message = personalize_message(template, first_name)

            # Send it
            if send_message(driver, phone, message):
                print(f"   ✓ Sent successfully")
                successful += 1
            else:
                failed += 1

            # Check again after sending (in case Ctrl+C during send)
            if shutdown_requested:
                print("\n🛑 Stopping as requested...")
                break

            # Wait between messages to avoid rate limiting
            if idx < len(contacts) - 1:
                print(f"   Waiting {WAIT_BETWEEN_MESSAGES}s before next message...")
                for _ in range(WAIT_BETWEEN_MESSAGES):
                    if shutdown_requested:
                        break
                    time.sleep(1)
        
        # Summary
        print("\n" + "=" * 50)
        if shutdown_requested:
            print("📊 Summary (Stopped Early)")
        else:
            print("📊 Summary")
        print("=" * 50)
        print(f"   ✓ Successful: {successful}")
        print(f"   ✗ Failed: {failed}")
        print(f"   📝 Processed: {successful + failed}/{len(contacts)}")
        
    finally:
        print("\n🔒 Closing browser...")
        driver.quit()
        print("Done!")


if __name__ == "__main__":
    main()
