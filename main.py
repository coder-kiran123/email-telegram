import imaplib
import email
import html
import re
import os
import time
import requests
from email.header import decode_header
from email.utils import parseaddr

IMAP_HOST      = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT      = int(os.getenv("IMAP_PORT", "993"))
EMAIL_ADDRESS  = os.getenv("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
IMAP_FOLDER    = os.getenv("IMAP_FOLDER", "INBOX")
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", "30"))


def decode_str(value):
    if not value:
        return ""
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def clean_body(text):
    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)
    # Remove empty parentheses left by URL removal
    text = re.sub(r'\(\s*\)', '', text)
    # Strip lines that are only whitespace
    lines = [l.rstrip() for l in text.splitlines()]
    lines = [l for l in lines if l.strip()]
    # Drop footer lines (legal, unsubscribe, copyright boilerplate)
    footer_triggers = (
        "unsubscribe", "privacy policy", "all rights reserved",
        "do not reply", "please do not reply", "©", "po box",
        "fdic", "member fdic", "this email was sent",
        "questions? we're here", "💚 from", "trustpilot",
    )
    clean = []
    for line in lines:
        if any(t in line.lower() for t in footer_triggers):
            break
        clean.append(line)
    text = "\n".join(clean).strip()
    # Collapse excess blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Hard cap at 600 chars
    if len(text) > 600:
        text = text[:600].rsplit("\n", 1)[0] + "\n..."
    return text


def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                    break
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            pass
    return clean_body(body)


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    # Telegram max message length is 4096
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            requests.post(url, json={
                "chat_id": CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            print(f"Telegram error: {e}")


def check_mail():
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        mail.select(IMAP_FOLDER)

        # Only fetch unseen emails
        status, data = mail.search(None, "UNSEEN")
        if status != "OK" or not data[0]:
            mail.logout()
            return

        ids = data[0].split()
        print(f"Found {len(ids)} new email(s)")

        for uid in ids:
            status, msg_data = mail.fetch(uid, "(RFC822)")
            if status != "OK":
                continue

            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject  = decode_str(msg.get("Subject", "(no subject)"))
            from_raw = decode_str(msg.get("From", ""))
            date     = decode_str(msg.get("Date", ""))
            _, from_addr = parseaddr(from_raw)
            body = get_body(msg)

            # Truncate long bodies
            if len(body) > 3000:
                body = body[:3000] + "\n\n<i>... (truncated)</i>"

            text = (
                f"📧 <b>New Email</b>\n\n"
                f"<b>From:</b> {html.escape(from_raw)}\n"
                f"<b>Subject:</b> {html.escape(subject)}\n"
                f"<b>Date:</b> {html.escape(date)}\n\n"
                f"{html.escape(body)}"
            )

            send_telegram(text)
            print(f"Forwarded: {subject} from {from_addr}")

        mail.logout()

    except imaplib.IMAP4.error as e:
        print(f"IMAP error: {e}")
    except Exception as e:
        print(f"Error: {e}")


def main():
    print(f"Starting email monitor: {EMAIL_ADDRESS} @ {IMAP_HOST}")
    print(f"Polling every {POLL_INTERVAL} seconds...")

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        raise RuntimeError("EMAIL_ADDRESS and EMAIL_PASSWORD must be set")
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    while True:
        check_mail()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
