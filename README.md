# WhatsApp Web Message Automation

Send personalized WhatsApp messages to multiple contacts using WhatsApp Web, with campaign management, response tracking, multi-stage follow-ups, reminders, and referral messaging.

## Prerequisites

1. **Python 3.8+** installed
2. **Google Chrome** browser installed
3. **WhatsApp account** linked to your phone

## Setup

```bash
# Create and activate virtual environment
python -m venv venv
source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

**Important:** Always activate the virtual environment before running the script:
```bash
source venv/bin/activate
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
| `followup.md` | Follow-up 1 for interested responders |
| `followup2.md` | Follow-up 2 (after followup 1) |
| `followup3.md` | Follow-up 3 (after followup 2) |
| `reminder.md` | Reminder for non-responders |
| `referral.md` | Forwardable message for referrers |
| `ask_to_refer.md` | Ask responders to refer others |

Edit all templates before proceeding.

### 2. Send messages

```bash
python whatsapp_sender.py send my_campaign
```

- **First run:** Chrome opens WhatsApp Web → scan QR code → press Enter → messages send automatically
- **Subsequent runs:** Session persists, no QR scan needed
- **Ctrl+C:** Stops gracefully. Run `send` again to resume from where you left off.

A `tracking.csv` file is created in the campaign folder, tracking the status of each contact.

### 3. Track responses

Open `campaigns/my_campaign/tracking.csv` in Excel or Google Sheets. Update these columns manually:

| Column | When to update |
|--------|---------------|
| `responded` | Set to `yes` when contact replies |
| `interested` | Set to `yes` or `no` based on response (leave blank if unsure) |
| `referrer` | Set to `yes` for contacts willing to refer others |
| `paid` | Set to `yes` when contact has paid |

### 4. Send follow-ups, reminders, and referrals

```bash
# Follow-ups (sequential: 1 → 2 → 3)
python whatsapp_sender.py followup my_campaign     # responded=yes, interested!=no
python whatsapp_sender.py followup2 my_campaign    # after followup 1 sent
python whatsapp_sender.py followup3 my_campaign    # after followup 2 sent

# Reminder to non-responders
python whatsapp_sender.py remind my_campaign

# Ask responders to refer others
python whatsapp_sender.py askrefer my_campaign

# Send forwardable message to referrers
python whatsapp_sender.py referral my_campaign
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
| `followup <name>` | Send follow-up 1 to interested responders |
| `followup2 <name>` | Send follow-up 2 (after followup 1) |
| `followup3 <name>` | Send follow-up 3 (after followup 2) |
| `remind <name>` | Send reminder to non-responders |
| `askrefer <name>` | Ask all responders to refer others |
| `referral <name>` | Send forwardable message to referrers |
| `status <name>` | Show campaign progress |

Run `python whatsapp_sender.py` with no arguments to see help.

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
| `interested` | `yes`, `no`, blank | **You update this manually** (blank = potentially interested) |
| `followup_sent` | `yes`, `no` | Updated by `followup` command |
| `followup2_sent` | `yes`, `no` | Updated by `followup2` command |
| `followup3_sent` | `yes`, `no` | Updated by `followup3` command |
| `reminder_sent` | `yes`, `no` | Updated by `remind` command |
| `referrer` | `yes`, blank | **You update this manually** |
| `referral_sent` | `yes`, `no` | Updated by `referral` command |
| `ask_to_refer_sent` | `yes`, `no` | Updated by `askrefer` command |
| `paid` | `yes`, `no` | **You update this manually** |

**Follow-up logic:** Follow-ups are sent to contacts where `responded=yes` AND `interested` is not `no` (blank interest is included). Reminders are sent only to non-responders.

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
├── CLAUDE.md                 # AI assistant instructions
├── venv/                     # Virtual environment (activate before running)
├── campaigns/                # All campaigns live here
│   └── workshop_feb/
│       ├── contacts.csv      # Campaign contacts
│       ├── message.md        # Initial message
│       ├── followup.md       # Follow-up 1
│       ├── followup2.md      # Follow-up 2
│       ├── followup3.md      # Follow-up 3
│       ├── reminder.md       # Reminder message
│       ├── referral.md       # Forwardable referral message
│       ├── ask_to_refer.md   # Ask to refer message
│       └── tracking.csv      # Auto-generated tracking
└── contacts.csv              # Legacy/staging contact files
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: No module named 'pandas'` | Activate the virtual environment: `source venv/bin/activate` |
| QR code doesn't appear | Delete `~/whatsapp_chrome_profile` and restart |
| Messages not sending | Increase `PAGE_LOAD_TIMEOUT` |
| "Number not on WhatsApp" | Verify the phone number includes country code |
| Chrome crashes | Update Chrome and run `pip install --upgrade webdriver-manager` |
| Script stopped mid-send | Run `send` again - it resumes from where it left off |

## Disclaimer

Use responsibly. Automated messaging may violate WhatsApp's Terms of Service if used for spam. This tool is intended for legitimate use cases like sending reminders to known contacts.
