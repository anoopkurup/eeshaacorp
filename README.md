# WhatsApp Web Message Automation

Send personalized WhatsApp messages to multiple contacts using WhatsApp Web.

## Prerequisites

1. **Python 3.8+** installed
2. **Google Chrome** browser installed
3. **WhatsApp account** linked to your phone

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Prepare your contacts

Edit `contacts.csv` with your contacts:

```csv
first_name,phone_number
Rahul,+919876543210
Priya,+919876543211
```

**Phone number format:** Include country code (e.g., +91 for India)

### 3. Prepare your message

Edit `message_template.md` with your message. Use `{first_name}` as a placeholder:

```markdown
Dear {first_name},

Your message content here...

Best regards,
Your Name
```

## Usage

```bash
python whatsapp_sender.py
```

### First Run

1. Chrome will open WhatsApp Web
2. Scan the QR code with your phone (WhatsApp > Linked Devices > Link a Device)
3. Press Enter when WhatsApp is loaded
4. Messages will be sent automatically

### Subsequent Runs

The script remembers your login session, so you won't need to scan the QR code again (unless you clear the profile).

## Configuration

Edit these variables in `whatsapp_sender.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WAIT_BETWEEN_MESSAGES` | 5 | Seconds to wait between messages |
| `PAGE_LOAD_TIMEOUT` | 60 | Max seconds to wait for page load |
| `CHROME_PROFILE_DIR` | `~/whatsapp_chrome_profile` | Where login session is stored |

## Important Notes

- **Rate Limiting:** WhatsApp may temporarily block you if you send too many messages too quickly. The default 5-second delay helps prevent this.
- **Number Validity:** Messages will fail for numbers not registered on WhatsApp.
- **Internet Required:** Both your computer and phone need internet access.
- **Keep Phone Connected:** Your phone must stay connected to the internet while sending.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| QR code doesn't appear | Clear Chrome profile folder and restart |
| Messages not sending | Increase `PAGE_LOAD_TIMEOUT` |
| "Number not on WhatsApp" | Verify the phone number format |
| Chrome crashes | Update Chrome and run `pip install --upgrade webdriver-manager` |

## File Structure

```
whatsapp_automation/
├── whatsapp_sender.py    # Main script
├── contacts.csv          # Your contacts list
├── message_template.md   # Message template
├── requirements.txt      # Python dependencies
└── README.md            # This file
```

## Disclaimer

Use responsibly. Automated messaging may violate WhatsApp's Terms of Service if used for spam. This tool is intended for legitimate use cases like sending reminders to known contacts.
