# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp Web automation script using Selenium to send personalized messages to multiple contacts, with campaign management, response tracking, follow-ups, and reminders. The script uses WhatsApp Web (not the official API) and maintains a persistent Chrome profile to avoid repeated QR code scanning.

## Architecture

**Single-script automation with campaign-based organization:**
- `whatsapp_sender.py` - Main script with argparse subcommands (create, send, followup, followup2, followup3, remind, referral, askrefer, status)
- `campaigns/` - Directory containing campaign folders, each with its own contacts, templates, and tracking

**Campaign directory structure:**
```
campaigns/<campaign_name>/
  contacts.csv       # first_name, last_name, phone_number
  message.md         # Initial outreach template ({first_name} placeholder)
  followup.md        # Follow-up 1 for interested responders
  followup2.md       # Follow-up 2 (after followup 1)
  followup3.md       # Follow-up 3 (after followup 2)
  reminder.md        # Template for non-responders
  referral.md        # Forwardable message for referrers
  ask_to_refer.md    # Ask responders to refer others
  tracking.csv       # Auto-generated after send; editable in Excel
```

**Tracking CSV columns:**
`first_name, last_name, phone_number, status, sent_at, responded, interested, followup_sent, followup2_sent, followup3_sent, reminder_sent, referrer, referral_sent, ask_to_refer_sent, paid, notes`

- `status`: `pending`, `sent`, or `failed`
- `responded`: `yes` or `no` (manually updated by user in Excel/Sheets)
- `interested`: `yes`, `no`, or blank (manually updated — contacts with `interested=no` are excluded from follow-ups; blank is treated as potentially interested)
- `referrer`: `yes` or empty (manually updated — contacts willing to refer others)
- `paid`: `yes` or `no` (manually updated — contacts who have paid)
- `notes`: free-text field for user notes (not used by the script)
- `followup_sent` / `followup2_sent` / `followup3_sent`: `yes` or `no` (updated by followup commands)
- `reminder_sent` / `referral_sent` / `ask_to_refer_sent`: `yes` or `no` (updated by respective commands)

**Follow-up filter logic:**
- Follow-ups require `responded=yes` AND `interested!=no` (blank interest is included)
- Follow-up 2 requires followup 1 to be sent first; followup 3 requires followup 2 first
- Reminders go only to non-responders (`responded!=yes`)

**Key workflow:**
1. `create` - Set up campaign folder with contacts and templates
2. `send` - Send initial messages, generates tracking.csv
3. User opens tracking.csv in Excel, marks `responded=yes`, `interested=yes/no`, `referrer=yes`, `paid=yes`
4. `followup` - Send follow-up 1 to responders (where interested is not "no")
5. `followup2` - Send follow-up 2 (after followup 1 sent)
6. `followup3` - Send follow-up 3 (after followup 2 sent)
7. `remind` - Send reminder to contacts with `responded!=yes`
8. `askrefer` - Ask all responders if they'd refer others
9. `referral` - Send forwardable message to contacts with `referrer=yes`
10. `status` - View campaign progress at any time

## Dependencies

Install via:
```bash
# macOS / Linux
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

```powershell
# Windows (PowerShell)
py -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```cmd
:: Windows (cmd.exe)
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Required packages:
- selenium >=4.0.0 - Browser automation
- pandas >=1.3.0 - CSV handling
- webdriver-manager >=4.0.0 - Auto-downloads/manages ChromeDriver

**Important:** Always activate the virtual environment before running the script:
```bash
# macOS / Linux
source venv/bin/activate
# Windows (PowerShell)
venv\Scripts\Activate.ps1
# Windows (cmd.exe)
venv\Scripts\activate
```

## Running the Script

```bash
# Activate virtual environment first (macOS/Linux shown; see above for Windows)
source venv/bin/activate

# Create a campaign
python whatsapp_sender.py create workshop_feb --contacts contacts.csv --message message_template.md

# Send initial messages
python whatsapp_sender.py send workshop_feb

# Check status
python whatsapp_sender.py status workshop_feb

# Send follow-ups (sequential: 1 → 2 → 3)
python whatsapp_sender.py followup workshop_feb
python whatsapp_sender.py followup2 workshop_feb
python whatsapp_sender.py followup3 workshop_feb

# Send reminder to non-responders
python whatsapp_sender.py remind workshop_feb

# Ask responders to refer others
python whatsapp_sender.py askrefer workshop_feb

# Send forwardable message to referrers (after marking referrer=yes in tracking.csv)
python whatsapp_sender.py referral workshop_feb
```

**First run:** Chrome opens WhatsApp Web → scan QR code → press Enter → messages send automatically

**Subsequent runs:** Session persists via Chrome profile, no QR scan needed

**Stopping gracefully:** Press Ctrl+C at any time. The script will finish the current message, save tracking, and close the browser cleanly. Run `send` again to resume from where you left off.

## Configuration Constants

Located at top of `whatsapp_sender.py`:

- `CAMPAIGNS_DIR = Path("campaigns")` - Root directory for all campaigns
- `WAIT_BETWEEN_MESSAGES = 3` - Seconds between messages (increase if hitting rate limits)
- `PAGE_LOAD_TIMEOUT = 60` - Max seconds to wait for initial WhatsApp Web load
- `MESSAGE_LOAD_TIMEOUT = 10` - Max seconds to wait for each message chat to load
- `CHROME_PROFILE_DIR` - Session storage location (default: `~/whatsapp_chrome_profile`)

## WhatsApp Web Selectors

The script uses multiple fallback CSS selectors for robustness since WhatsApp Web frequently changes their DOM:

**Initial load detection (waits for any of these):**
- `[data-testid='chat-list']` - Chat list panel
- `[data-testid='conversation-panel-wrapper']` - Main chat panel
- `[data-testid='qrcode']` - QR code container
- `div[contenteditable='true'][data-tab='10']` - Old message input
- `div[contenteditable='true']` - Generic message input
- `canvas[aria-label*='QR']` / `canvas[aria-label*='qr']` - QR code canvas
- `#app .two` / `#side` - Structural selectors

**Send button (tries in order):**
- `span[data-icon='send']`
- `button[aria-label*='Send']`
- `[data-testid='send']`

If all selectors fail, the script provides debugging info (URL, page title) and continues.

## Phone Number Handling

- Must include country code (e.g., `919876543210` for India, `18583493572` for US)
- `normalize_phone()` helper strips `.0` float suffixes and all non-digit characters for consistent comparisons
- CSVs are read with `dtype={"phone_number": str}` to prevent pandas float conversion
- `save_tracking()` normalizes phone numbers before writing to prevent `.0` corruption
- `load_contacts()` drops rows with missing name or empty phone number
- Invalid/non-WhatsApp numbers will timeout and be marked as failed in tracking.csv

## Selenium Strategy

Uses WhatsApp Web's direct URL feature: `https://web.whatsapp.com/send?phone={number}&text={message}`

This pre-fills the message, avoiding complex element interaction. Script just waits for send button and clicks it.

## Signal Handling

The script implements graceful shutdown via SIGINT (Ctrl+C):
- `shutdown_requested` global flag set by signal handler
- Checked at multiple points: before message, after message, and during wait loops
- When triggered: finishes current message, saves tracking.csv, closes browser cleanly
- Wait periods use 1-second intervals to check shutdown flag frequently
- Run `send` again to resume - already-sent contacts are skipped automatically

## Chrome Profile Persistence

The script uses `--user-data-dir={CHROME_PROFILE_DIR}` with `--profile-directory=Default` and `--remote-debugging-port=9222` to maintain WhatsApp Web login state. If Chrome crashes or leaves stale lock files (`SingletonLock`, `SingletonSocket`, `SingletonCookie`), delete them before retrying. To force re-login, delete the profile directory:

```bash
rm -rf ~/whatsapp_chrome_profile
```

## Limitations & Considerations

- **Not official API:** Uses web scraping, fragile to WhatsApp Web UI changes
- **Rate limiting:** WhatsApp may temporarily block bulk messaging
- **Phone must stay online:** WhatsApp account's phone needs internet during sending
- **No delivery confirmation:** Script doesn't verify message delivery status
- **Chrome requirement:** Requires Google Chrome browser installed
- **Manual response tracking:** User must manually mark responders in tracking.csv (open in Excel/Sheets)
- **Browser close timing:** A 5-second delay before `driver.quit()` ensures the last message is delivered
