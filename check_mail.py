import email, imaplib, json, os, re, urllib.request
from email.header import decode_header

def env(n, d=""):
    return os.environ.get(n, d).strip()

def dec(v):
    if not v:
        return ""
    out = []
    for c, e in decode_header(v):
        out.append(c.decode(e or "utf-8", errors="replace") if isinstance(c, bytes) else str(c))
    return "".join(out).strip()

def clean(t):
    t = re.sub(r"https?://\S+", "", t or "")
    return re.sub(r"\s+", " ", t).strip()

def body_of(msg):
    plain, html = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for p in parts:
        if "attachment" in str(p.get("Content-Disposition") or ""):
            continue
        payload = p.get_payload(decode=True)
        if not payload:
            continue
        txt = payload.decode(p.get_content_charset() or "utf-8", errors="replace")
        if p.get_content_type() == "text/plain":
            plain.append(txt)
        elif p.get_content_type() == "text/html":
            html.append(txt)
    if plain:
        return clean("\n".join(plain))
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", "\n".join(html), flags=re.S | re.I)
    return clean(re.sub(r"<[^>]+>", " ", h))

def esc(t):
    t = str(t or "")
    for ch in ["\\", "*", "_", "`", "["]:
        t = t.replace(ch, "\\" + ch)
    return t

def groq_summary(subject, sender, body):
    key = env("GROQ_API_KEY")
    if not key:
        return None
    prompt = "সহজ বাংলায় উত্তর দাও। ফরম্যাট:\nসারাংশ: <2-4 বাক্যে সারাংশ>\nকরণীয়: <কী করতে হবে>\n\nপাঠিয়েছেন: " + sender + "\nবিষয়: " + subject + "\nইমেইল:\n" + body[:6000]
    data = json.dumps({"model": env("GROQ_MODEL", "llama-3.1-8b-instant"), "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=data, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()

def summarize(subject, sender, body):
    try:
        ai = groq_summary(subject, sender, body)
    except Exception as e:
        print("Groq failed:", e)
        ai = None
    if ai:
        s = re.search(r"সারাংশ:\s*(.+)", ai)
        a = re.search(r"করণীয়:\s*(.+)", ai)
        return (s.group(1).strip() if s else ai), (a.group(1).strip() if a else "ইমেইলটি দেখে প্রয়োজন হলে ব্যবস্থা নিন।")
    short = body[:700] + ("..." if len(body) > 700 else "")
    return (short or "ইমেইলে কোনো লেখা পাওয়া যায়নি।"), "ইমেইলটি খুলে দেখে প্রয়োজন হলে ব্যবস্থা নিন।"

def send_tg(token, chat, text):
    for i in range(0, len(text), 3900):
        data = json.dumps({"chat_id": chat, "text": text[i:i+3900], "parse_mode": "Markdown", "disable_web_page_preview": True}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read().decode())
        if not res.get("ok"):
            raise RuntimeError(str(res))

def main():
    user, pw = env("GMAIL_ADDRESS"), env("GMAIL_APP_PASSWORD").replace(" ", "")
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    missing = [n for n, v in [("GMAIL_ADDRESS", user), ("GMAIL_APP_PASSWORD", pw), ("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)] if not v]
    if missing:
        raise SystemExit("Missing secrets: " + ", ".join(missing))
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(user, pw)
    mail.select("INBOX")
    status, data = mail.search(None, "UNSEEN")
    ids = data[0].split()[-5:] if data and data[0] else []
    print("Unseen:", len(ids))
    for mid in ids:
        st, md = mail.fetch(mid, "(RFC822)")
        if st != "OK" or not md or not isinstance(md[0], tuple):
            continue
        msg = email.message_from_bytes(md[0][1])
        subject = dec(msg.get("Subject")) or "(কোনো বিষয় নেই)"
        sender = dec(msg.get("From")) or "(অজানা)"
        summary, action = summarize(subject, sender, body_of(msg))
        text = "*📧 নতুন ইমেইল*\n\n*পাঠিয়েছেন:* " + esc(sender) + "\n*বিষয়:* " + esc(subject) + "\n\n*সারাংশ:*\n" + esc(summary) + "\n\n*আপনাকে যা করতে হবে:*\n" + esc(action)
        send_tg(token, chat, text)
        mail.store(mid, "+FLAGS", "\\Seen")
        print("Sent:", subject)
    mail.logout()

if __name__ == "__main__":
    main()
