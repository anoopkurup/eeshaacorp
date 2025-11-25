# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WhatsApp Web automation script using Selenium to send personalized messages to multiple contacts. The script uses WhatsApp Web (not the official API) and maintains a persistent Chrome profile to avoid repeated QR code scanning.

## Architecture

**Single-script automation:**
- `whatsapp_sender.py` - Main script containing all logic (CSV parsing, message templating, Selenium automation)
- `contacts.csv` - Contact data with `first_name` and `phone_number` columns
- `message_template.md` - Message template using `{first_name}` placeholder for personalization

**Key workflow:**
1. Load contacts from CSV and message template from markdown
2. Initialize Chrome WebDriver with persistent profile (`~/whatsapp_chrome_profile`)
3. Navigate to WhatsApp Web and wait for QR scan (first run only)
4. For each contact: construct WhatsApp Web URL with pre-filled message, wait for send button, click to send
5. Rate limiting: 5-second delay between messages to avoid WhatsApp blocks

## Dependencies

Install via:
```bash
pip install -r requirements.txt
```

Required packages:
- selenium >=4.0.0 - Browser automation
- pandas >=1.3.0 - CSV handling
- webdriver-manager >=4.0.0 - Auto-downloads/manages ChromeDriver

## Running the Script

```bash
python whatsapp_sender.py
```

**First run:** Chrome opens WhatsApp Web → scan QR code → press Enter → messages send automatically

**Subsequent runs:** Session persists via Chrome profile, no QR scan needed

**Stopping gracefully:** Press Ctrl+C at any time. The script will finish the current message, show a summary, and close the browser cleanly. The shutdown check happens:
- Before each message
- After each message
- During the wait period (every second)

## Configuration Constants

Located at top of `whatsapp_sender.py`:

- `WAIT_BETWEEN_MESSAGES = 3` - Seconds between messages (increase if hitting rate limits)
- `PAGE_LOAD_TIMEOUT = 60` - Max seconds to wait for initial WhatsApp Web load
- `MESSAGE_LOAD_TIMEOUT = 10` - Max seconds to wait for each message chat to load
- `CHROME_PROFILE_DIR` - Session storage location (default: `~/whatsapp_chrome_profile`)

**Performance:** Each message typically takes 3-5 seconds total (2-3s to load chat + click send + 3s wait). The shorter MESSAGE_LOAD_TIMEOUT prevents long delays when contacts load quickly.

## WhatsApp Web Selectors

The script uses multiple fallback CSS selectors for robustness since WhatsApp Web frequently changes their DOM:

**Initial load detection (waits for any of these):**
- `div[contenteditable='true'][data-tab='10']` - Old message input
- `div[contenteditable='true']` - Generic message input
- `canvas` / `canvas[aria-label]` - QR code
- `[data-testid='qrcode']` - QR code container
- `[data-testid='conversation-panel-wrapper']` - Main chat panel

**Send button (tries in order):**
- `span[data-icon='send']`
- `button[aria-label*='Send']`
- `[data-testid='send']`

If all selectors fail, the script provides debugging info (URL, page title) and continues, allowing manual verification.

## Phone Number Format

- Must include country code (e.g., `+919876543210` for India)
- Script strips spaces and `+` prefix when constructing WhatsApp Web URL
- Invalid/non-WhatsApp numbers will timeout and be marked as failed

## Selenium Strategy

Uses WhatsApp Web's direct URL feature: `https://web.whatsapp.com/send?phone={number}&text={message}`

This pre-fills the message, avoiding complex element interaction. Script just waits for send button and clicks it.

## Signal Handling

The script implements graceful shutdown via SIGINT (Ctrl+C):
- `shutdown_requested` global flag set by signal handler
- Checked at multiple points: before message, after message, and during wait loops
- When triggered: finishes current message, prints summary with processed count, closes browser cleanly
- Wait periods use 1-second intervals to check shutdown flag frequently

## Chrome Profile Persistence

The script uses `--user-data-dir={CHROME_PROFILE_DIR}` to maintain WhatsApp Web login state. To force re-login, delete the profile directory:

```bash
rm -rf ~/whatsapp_chrome_profile
```

## Limitations & Considerations

- **Not official API:** Uses web scraping, fragile to WhatsApp Web UI changes
- **Rate limiting:** WhatsApp may temporarily block bulk messaging
- **Phone must stay online:** WhatsApp account's phone needs internet during sending
- **No delivery confirmation:** Script doesn't verify message delivery status
- **Chrome requirement:** Requires Google Chrome browser installed
