# WhatsApp Web Message Automation

Send personalized WhatsApp messages to multiple contacts using WhatsApp Web, with campaign management, response tracking, follow-ups, and reminders.

## Prerequisites

1. **Python 3.8+** installed
2. **Google Chrome** browser installed
3. **WhatsApp account** linked to your phone

## Setup

```bash
pip install -r requirements.txt
```

## Quick Start

### 1. Create a campaign

```bash
python whatsapp_sender.py create my_campaign --contacts contacts.csv --message message_template.md
```

This creates a `campaigns/my_campaign/` folder with:

| File | Purpose |
|------|---------|
| `contacts.csv` | Your contacts (first_name, phone_number) |
| `message.md` | Initial outreach message |
| `followup.md` | Follow-up message for responders |
| `reminder.md` | Reminder message for non-responders |

Edit the follow-up and reminder templates before proceeding.

### 2. Send messages

```bash
python whatsapp_sender.py send my_campaign
```

- **First run:** Chrome opens WhatsApp Web → scan QR code → press Enter → messages send automatically
- **Subsequent runs:** Session persists, no QR scan needed
- **Ctrl+C:** Stops gracefully. Run `send` again to resume from where you left off.

A `tracking.csv` file is created in the campaign folder, tracking the status of each contact.

### 3. Track responses

Open `campaigns/my_campaign/tracking.csv` in Excel or Google Sheets. For contacts who responded, change the `responded` column from `no` to `yes`. Save the file.

### 4. Send follow-ups and reminders

```bash
# Send follow-up to people who responded
python whatsapp_sender.py followup my_campaign

# Send reminder to people who did not respond
python whatsapp_sender.py remind my_campaign
```

### 5. Check campaign status

```bash
python whatsapp_sender.py status my_campaign
```

## Commands

| Command | Description |
|---------|-------------|
| `create <name> [--contacts file] [--message file]` | Create a new campaign |
| `send <name>` | Send initial messages |
| `followup <name>` | Send follow-up to responders |
| `remind <name>` | Send reminder to non-responders |
| `status <name>` | Show campaign progress |

Run `python whatsapp_sender.py` with no arguments to see help and a list of existing campaigns.

## Contacts CSV Format

```csv
first_name,phone_number
Rahul,+919876543210
Priya,+919876543211
```

**Phone number format:** Include country code (e.g., +91 for India, +1 for US).

## Message Templates

All templates use `{first_name}` as a placeholder for personalization:

```markdown
Hi {first_name}

Your message content here...
```

## Tracking CSV

After `send` runs, `tracking.csv` is generated in the campaign folder:

| Column | Values | Description |
|--------|--------|-------------|
| `first_name` | | Contact's name |
| `phone_number` | | Contact's phone |
| `status` | `pending`, `sent`, `failed` | Send status |
| `sent_at` | timestamp | When the message was sent |
| `responded` | `yes`, `no` | **You update this manually** |
| `followup_sent` | `yes`, `no` | Updated by `followup` command |
| `reminder_sent` | `yes`, `no` | Updated by `remind` command |

## Configuration

Edit these variables at the top of `whatsapp_sender.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WAIT_BETWEEN_MESSAGES` | 3 | Seconds to wait between messages |
| `PAGE_LOAD_TIMEOUT` | 60 | Max seconds to wait for page load |
| `MESSAGE_LOAD_TIMEOUT` | 10 | Max seconds to wait for each chat to load |
| `CHROME_PROFILE_DIR` | `~/whatsapp_chrome_profile` | Where login session is stored |

To force a fresh WhatsApp login, delete the profile:

```bash
rm -rf ~/whatsapp_chrome_profile
```

## File Structure

```
WhatsApp-Automation/
├── whatsapp_sender.py        # Main script
├── requirements.txt          # Python dependencies
├── README.md                 # This file
├── campaigns/                # All campaigns live here
│   ├── workshop_feb/
│   │   ├── contacts.csv      # Campaign contacts
│   │   ├── message.md        # Initial message
│   │   ├── followup.md       # Follow-up message
│   │   ├── reminder.md       # Reminder message
│   │   └── tracking.csv      # Auto-generated tracking
│   └── linkedin_ai/
│       └── ...
├── contacts.csv              # Legacy/staging contact files
└── message_template.md       # Legacy/staging template
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| QR code doesn't appear | Delete `~/whatsapp_chrome_profile` and restart |
| Messages not sending | Increase `PAGE_LOAD_TIMEOUT` |
| "Number not on WhatsApp" | Verify the phone number includes country code |
| Chrome crashes | Update Chrome and run `pip install --upgrade webdriver-manager` |
| Script stopped mid-send | Run `send` again - it resumes from where it left off |

## Disclaimer

Use responsibly. Automated messaging may violate WhatsApp's Terms of Service if used for spam. This tool is intended for legitimate use cases like sending reminders to known contacts.
