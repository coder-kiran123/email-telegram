import imaplib
import email
import html
import json
import re
import os
import time
import threading
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
DATA_FILE      = os.getenv("DATA_FILE", "data.json")

REACTION_LABELS = {
    "❤️": "Love",
    "❤": "Love",
    "😂": "Haha",
    "👍": "Like",
    "🔥": "Fire",
}

data_lock = threading.Lock()


def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"totals": {"❤": 0.0, "😂": 0.0, "👍": 0.0, "🔥": 0.0}, "messages": {}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


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


def extract_amount(text):
    match = re.search(r'\$[\d,]+(?:\.\d{1,2})?', text)
    if match:
        try:
            return float(match.group().replace("$", "").replace(",", ""))
        except ValueError:
            pass
    return None


def clean_body(text):
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\(\s*\)', '', text)
    footer_triggers = (
        "unsubscribe", "privacy policy", "all rights reserved",
        "do not reply", "please do not reply", "©", "po box",
        "fdic", "member fdic", "this email was sent",
        "questions? we're here", "💚 from", "trustpilot",
    )
    lines = text.splitlines()
    trimmed = []
    for line in lines:
        if any(t in line.lower() for t in footer_triggers):
            break
        trimmed.append(line)

    paragraphs = []
    current = []
    for line in trimmed:
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                current = []
        else:
            current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))

    text = "\n\n".join(p for p in paragraphs if p.strip())
    if len(text) > 600:
        text = text[:600].rsplit(" ", 1)[0] + "..."
    return text.strip()


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

            if len(body) > 3000:
                body = body[:3000] + "\n\n<i>... (truncated)</i>"

            text = (
                f"📧 <b>New Email</b>\n\n"
                f"<b>From:</b> {html.escape(from_raw)}\n"
                f"<b>Subject:</b> {html.escape(subject)}\n"
                f"<b>Date:</b> {html.escape(date)}\n\n"
                f"{html.escape(body)}"
            )

            resp = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10
            )
            result = resp.json().get("result", {})
            message_id = result.get("message_id")

            if message_id:
                amount = extract_amount(subject)
                if amount is not None:
                    with data_lock:
                        d = load_data()
                        d["messages"][str(message_id)] = {
                            "subject": subject,
                            "amount": amount
                        }
                        save_data(d)

            print(f"Forwarded: {subject} from {from_addr}")

        mail.logout()

    except imaplib.IMAP4.error as e:
        print(f"IMAP error: {e}")
    except Exception as e:
        print(f"Error: {e}")


def handle_commands(update):
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return
    text = msg.get("text", "").strip()
    if text.lower() not in ("/total", "/total@" + "emailsender3214bot"):
        return
    with data_lock:
        d = load_data()
        totals = d["totals"]
    reply = (
        f"💰 <b>Current Totals</b>\n\n"
        f"❤️ Love total:  ${totals.get('❤', 0.0):.2f}\n"
        f"😂 Haha total:  ${totals.get('😂', 0.0):.2f}\n"
        f"👍 Like total:  ${totals.get('👍', 0.0):.2f}\n"
        f"🔥 Fire total:  ${totals.get('🔥', 0.0):.2f}"
    )
    send_telegram(reply)


def handle_reaction_count(reaction_count):
    msg_id = str(reaction_count.get("message_id", ""))
    reactions = reaction_count.get("reactions", [])
    print(f"DEBUG reaction_count: msg_id={msg_id} reactions={reactions}")

    with data_lock:
        d = load_data()
        msg_info = d["messages"].get(msg_id, {})
        subject = msg_info.get("subject", "Unknown email")
        amount = msg_info.get("amount")
        prev_counts = d.get("reaction_counts", {}).get(msg_id, {})

        # Build current counts
        curr_counts = {}
        for r in reactions:
            emoji = r.get("type", {}).get("emoji", "").replace("❤️", "❤")
            curr_counts[emoji] = r.get("total_count", 0)

        # Find which emojis changed
        changed_emoji = None
        added = True
        for emoji in REACTION_LABELS:
            prev = prev_counts.get(emoji, 0)
            curr = curr_counts.get(emoji, 0)
            if curr > prev:
                changed_emoji = emoji
                added = True
                break
            elif curr < prev:
                changed_emoji = emoji
                added = False
                break

        # Update stored counts
        if "reaction_counts" not in d:
            d["reaction_counts"] = {}
        d["reaction_counts"][msg_id] = curr_counts

        if changed_emoji and amount is not None:
            if added:
                d["totals"][changed_emoji] = round(d["totals"].get(changed_emoji, 0.0) + amount, 2)
            else:
                d["totals"][changed_emoji] = round(max(0.0, d["totals"].get(changed_emoji, 0.0) - amount), 2)

        save_data(d)
        totals = d["totals"]

    if changed_emoji:
        label = REACTION_LABELS.get(changed_emoji, changed_emoji)
        action = "New Reaction" if added else "Reaction Removed"
        icon = "👀" if added else "❌"
        amount_line = f"<b>Amount:</b> {'+'if added else '-'}${amount:.2f}\n" if amount is not None else ""
        notify = (
            f"{icon} <b>{action}</b>\n\n"
            f"<b>Reaction:</b> {changed_emoji} {label}\n"
            f"<b>Email:</b> {html.escape(subject)}\n"
            f"{amount_line}\n"
            f"❤️ Love total:  ${totals.get('❤', 0.0):.2f}\n"
            f"😂 Haha total:  ${totals.get('😂', 0.0):.2f}\n"
            f"👍 Like total:  ${totals.get('👍', 0.0):.2f}\n"
            f"🔥 Fire total:  ${totals.get('🔥', 0.0):.2f}"
        )
        send_telegram(notify)


def watch_reactions():
    offset = None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

    while True:
        try:
            params = {
                "timeout": 30,
                "allowed_updates": ["message_reaction", "message_reaction_count", "message", "channel_post"],
            }
            if offset:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=40)
            data = resp.json()

            if not data.get("ok"):
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                print(f"DEBUG update keys: {list(update.keys())}")
                handle_commands(update)

                # Handle channel reaction counts (anonymous reactions)
                reaction_count = update.get("message_reaction_count")
                if reaction_count:
                    handle_reaction_count(reaction_count)
                    continue

                reaction = update.get("message_reaction")
                if not reaction:
                    continue

                new_reactions = reaction.get("new_reaction", [])
                old_reactions = reaction.get("old_reaction", [])

                user = reaction.get("user", {})
                name = user.get("first_name", "Someone")
                last = user.get("last_name", "")
                username = user.get("username", "")
                display = f"{name} {last}".strip()
                if username:
                    display += f" (@{username})"

                msg_id = str(reaction.get("message_id", ""))
                print(f"DEBUG reaction update: msg_id={msg_id} new={new_reactions} old={old_reactions}")

                # Reaction removed
                if not new_reactions and old_reactions:
                    emoji = old_reactions[0].get("emoji", "").replace("❤️", "❤")
                    if emoji not in REACTION_LABELS:
                        continue
                    label = REACTION_LABELS[emoji]

                    with data_lock:
                        d = load_data()
                        msg_info = d["messages"].get(msg_id, {})
                        subject = msg_info.get("subject", "Unknown email")
                        amount = msg_info.get("amount")

                        if amount is not None:
                            d["totals"][emoji] = round(max(0.0, d["totals"].get(emoji, 0.0) - amount), 2)
                            save_data(d)

                        totals = d["totals"]

                    amount_line = f"<b>Amount:</b> -${amount:.2f}\n" if amount is not None else ""
                    notify = (
                        f"❌ <b>Reaction Removed</b>\n\n"
                        f"<b>Who:</b> {html.escape(display)}\n"
                        f"<b>Removed:</b> {emoji} {label}\n"
                        f"<b>Email:</b> {html.escape(subject)}\n"
                        f"{amount_line}\n"
                        f"❤️ Love total:  ${totals.get('❤', 0.0):.2f}\n"
                        f"😂 Haha total:  ${totals.get('😂', 0.0):.2f}\n"
                        f"👍 Like total:  ${totals.get('👍', 0.0):.2f}\n"
                        f"🔥 Fire total:  ${totals.get('🔥', 0.0):.2f}"
                    )
                    send_telegram(notify)
                    print(f"Reaction {emoji} removed by {display} on msg {msg_id}")
                    continue

                # Reaction added
                if not new_reactions:
                    continue

                emoji = new_reactions[0].get("emoji", "").replace("❤️", "❤")
                if emoji not in REACTION_LABELS:
                    continue

                label = REACTION_LABELS[emoji]

                with data_lock:
                    d = load_data()
                    print(f"DEBUG stored messages: {list(d['messages'].keys())}")
                    msg_info = d["messages"].get(msg_id, {})
                    subject = msg_info.get("subject", "Unknown email")
                    amount = msg_info.get("amount")
                    print(f"DEBUG msg_info={msg_info} emoji={emoji}")

                    if amount is not None:
                        d["totals"][emoji] = round(d["totals"].get(emoji, 0.0) + amount, 2)
                        save_data(d)

                    totals = d["totals"]

                amount_line = f"<b>Amount:</b> ${amount:.2f}\n" if amount is not None else ""
                notify = (
                    f"👀 <b>New Reaction</b>\n\n"
                    f"<b>Who:</b> {html.escape(display)}\n"
                    f"<b>Reaction:</b> {emoji} {label}\n"
                    f"<b>Email:</b> {html.escape(subject)}\n"
                    f"{amount_line}\n"
                    f"❤️ Love total:  ${totals.get('❤', 0.0):.2f}\n"
                    f"😂 Haha total:  ${totals.get('😂', 0.0):.2f}\n"
                    f"👍 Like total:  ${totals.get('👍', 0.0):.2f}\n"
                    f"🔥 Fire total:  ${totals.get('🔥', 0.0):.2f}"
                )
                send_telegram(notify)
                print(f"Reaction {emoji} from {display} on msg {msg_id}")

        except Exception as e:
            print(f"Reaction watcher error: {e}")
            time.sleep(5)


def main():
    print(f"Starting email monitor: {EMAIL_ADDRESS} @ {IMAP_HOST}")
    print(f"Polling every {POLL_INTERVAL} seconds...")

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        raise RuntimeError("EMAIL_ADDRESS and EMAIL_PASSWORD must be set")
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    t = threading.Thread(target=watch_reactions, daemon=True)
    t.start()

    while True:
        check_mail()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
