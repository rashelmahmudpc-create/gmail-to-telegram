import email, html, imaplib, json, os, re, time, urllib.request
from functools import partial

print = partial(print, flush=True)
from email.header import decode_header
from datetime import datetime, timedelta, timezone
import unicodedata

# ── START_AT & persistent duplicate tracking ──
SENT_IDS_FILE = "sent_ids.json"

def load_sent_ids():
    try:
        with open(SENT_IDS_FILE, "r") as f:
            data = json.load(f)
            return set(str(x) for x in data) if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_sent_ids(ids):
    with open(SENT_IDS_FILE, "w") as f:
        json.dump(sorted(ids, key=int), f)

def get_cutoff():
    start_at = env("START_AT")
    if start_at:
        try:
            dt = datetime.fromisoformat(start_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            print("START_AT invalid format, falling back to 24h")
    return int(time.time() - 24 * 60 * 60)

# ── helpers ──

def env(n, d=""):
    return os.environ.get(n, d).strip()

def dec(v):
    if not v:
        return ""
    out = []
    for c, e in decode_header(v):
        out.append(c.decode(e or "utf-8", errors="replace") if isinstance(c, bytes) else str(c))
    return "".join(out).strip()

def strip_html(h):
    h = h or ""
    h = re.sub(r"<!--.*?-->", " ", h, flags=re.S)
    h = re.sub(r"<!\[if.*?<!\[endif\]>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<(br|/p|/div|/tr|/li|/h[1-6])[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h)
    return h

def clean(t):
    t = str(t or "")
    for _ in range(2):
        t = html.unescape(t)
    t = strip_html(t)
    t = re.sub(r"<!--.*$", " ", t, flags=re.S)
    t = unicodedata.normalize("NFC", t)
    t = t.replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ").replace("\ufeff", "")
    t = "".join(c for c in t if c in "\n\t" or unicodedata.category(c) != "Cc")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()

def body_of(msg):
    plain, htmls = [], []
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
            htmls.append(txt)
    if plain:
        return clean("\n".join(plain))
    return clean(strip_html("\n".join(htmls)))

def md_clean(t):
    t = unicodedata.normalize("NFC", str(t or ""))
    t = "".join(c for c in t if c in "\n\t" or unicodedata.category(c) != "Cc")
    return re.sub(r"\n{3,}", "\n\n", t).strip()

def md_escape(t):
    # Escape Markdown syntax, never remove Bengali letters or vowel signs.
    return re.sub(r"([\\_*\[\]()~`>#+\-=|{}.!])", r"\\\1", md_clean(t))

def telegram_length(t):
    return len(t.encode("utf-16-le")) // 2

def build_prompt(subject, sender, account, body):
    instructions = (
        "তুমি Gmail থেকে বাংলায় স্বাভাবিক সারাংশ, বিস্তারিত, নিরাপত্তা ও করণীয় অংশ চার ভাগে লিখবে। "
        "প্রথমে সারাংশ, তারপর বিস্তারিত, নিরাপত্তা ও করণীয়। "
        "কোনো HTML, CSS, Markdown, বা কোড ব্লক লিখবে না। "
        "বাংলা অক্ষর, বোল্ড ও ইমোজি ঠিক রাখবে; বিরাম চিহ্ন সঠিকভাবে বসাবে। "
        "স্পষ্ট, স্বাভাবিক বাংলায় লিখবে:\n"
        "সারাংশ: এক-দুই বাক্যে মেইলের মূল বিষয়, প্রেরক, গুরুত্ব ও প্রয়োজনীয়তা জানাও।\n"
        "বিস্তারিত: এক-দুই অনুচ্ছেদে মেইলের বিষয়বস্তু বিস্তারিত বলো এবং প্রয়োজনীয় তথ্য দাও।\n"
        "নিরাপত্তা: ফিশিং, OTP, পেমেন্ট বা সন্দেহজনক লিংক থাকলে সতর্ক করো এবং ব্যাখ্যা দাও। "
        "কোনো পাসওয়ার্ড বা ব্যক্তিগত তথ্য চাইবে না। "
        "করণীয়: ব্যবহারকারীকে কী করতে হবে, উত্তর দিতে হবে নাকি অপেক্ষা করতে হবে তা পরামর্শ দাও।\n\n"
    )
    return instructions + json.dumps({"gmail": account, "sender": sender, "subject": subject, "email": body[:7000]}, ensure_ascii=False)

def http_json(url, data, headers, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def gemini_once(model, key, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    res = http_json(url, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1200}}, {"Content-Type": "application/json", "x-goog-api-key": key})
    parts = res["candidates"][0]["content"].get("parts", [])
    text = "".join([p.get("text", "") for p in parts]).strip()
    if not text:
        raise RuntimeError("Gemini empty text")
    return text

def call_gemini(prompt):
    key = env("GEMINI_API_KEY")
    if not key:
        return None
    return gemini_once("gemini-3.6-flash", key, prompt)


def call_chat(url, key, models, prompt, extra=None):
    if not key:
        return None
    last = None
    for model in models:
        try:
            headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
            if extra:
                headers.update(extra)
            res = http_json(url, {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}, headers)
            msg = res["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            if text:
                return text
            raise RuntimeError("empty content")
        except Exception as e:
            last = e
            print(f"Model {model} failed:", type(e).__name__)
    raise last

def call_openrouter(prompt):
    return call_chat("https://openrouter.ai/api/v1/chat/completions", env("OPENROUTER_API_KEY"), ["openrouter/free"], prompt, {"X-Title": "Gmail to Telegram"})

def call_groq(prompt):
    return call_chat("https://api.groq.com/openai/v1/chat/completions", env("GROQ_API_KEY"), ["openai/gpt-oss-20b"], prompt)

def parse_ai(text):
    text = clean(text or "")
    text = text.replace("**", "").replace("`", "")
    keys = [("সারাংশ", "summary"), ("বিস্তারিত", "detail"), ("নিরাপত্তা", "security"), ("করণীয়", "action")]
    matches = list(re.finditer(r"(?m)^\s*(?:[-*#]+\s*)?(সারাংশ|বিস্তারিত|নিরাপত্তা|করণীয়)\s*[:：]\s*", text))
    out = {key: "" for _, key in keys}
    mapping = dict(keys)
    for i, match in enumerate(matches):
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        value = clean(text[match.end():end]).strip()
        out[mapping[match.group(1)]] = value
    for key, value in out.items():
        if len(value) < 10 or not re.search(r"[\u0980-\u09ff]", value):
            raise ValueError("Incomplete or non-Bengali summary section: " + key)
        if "\ufffd" in value or "<!--" in value:
            raise ValueError("Broken text in summary")
    return out

def fallback_security(body, sender):
    low = (body or "").lower()
    bad = ["password", "otp", "verify your account", "urgent", "suspended", "click here", "login", "bank account", "send money", "lottery", "prize", "পাসওয়ার্ড", "ওটিপি"]
    hits = [w for w in bad if w in low]
    if hits:
        return "সতর্কতা! এই মেইলে পাসওয়ার্ড, OTP, বা জরুরি দাবি থাকতে পারে। কোনো লিংকে ক্লিক করবেন না, পাসওয়ার্ড বা OTP শেয়ার করবেন না। যদি সন্দেহ হয় তবে প্রেরকের সাথে সরাসরি যোগাযোগ করুন।"
    return "এই মেইলে তাৎক্ষণিক নিরাপত্তা ঝুঁকি দেখা যায়নি। তবে অজানা লিংক বা সংযুক্তি খোলার আগে সতর্ক থাকুন।"

def summarize(subject, sender, account, body):
    prompt = build_prompt(subject, sender, account, body)
    for name, fn in [("Gemini", call_gemini), ("OpenRouter", call_openrouter), ("Groq", call_groq)]:
        try:
            text = fn(prompt)
            if not text:
                print(name, "returned no summary; trying next provider")
                continue
            out = parse_ai(text)
            message = make_message(account, sender, subject, out, name)
            if telegram_length(message) > 3900:
                raise ValueError("Summary too long for one Telegram message")
            print("LLM_SUCCESS", name)
            return out, name
        except Exception as e:
            # Exception text may include API keys or URLs: never print it.
            print(name, "failed or invalid summary:", type(e).__name__, "trying next provider")
    return None, None

def make_message(account, sender, subject, out, provider):
    return (
        "*📧 নতুন মেইল পাওয়া গেছে*\n\n"
        + "*📌 বিষয়:* " + md_escape(subject) + "\n"
        + "*📤 প্রেরক:* " + md_escape(sender) + "\n"
        + "*📥 ইমেইল Gmail\\-ই:* " + md_escape(account) + "\n"
        + "*🔖 মডেল প্রদানকারী:* " + md_escape(provider) + "\n\n"
        + "*📝 সহজ সারাংশ*\n" + md_escape(out["summary"]) + "\n\n"
        + "*📋 বিস্তারিত ও প্রয়োজনীয় তথ্য*\n" + md_escape(out["detail"]) + "\n\n"
        + "*🛡️ নিরাপত্তা সতর্কতা*\n" + md_escape(out["security"]) + "\n\n"
        + "*✅ যা করতে হবে*\n" + md_escape(out["action"])
    )

def send_tg(token, chat, text):
    if telegram_length(text) > 3900:
        raise ValueError("Message too long; refusing to split Bengali text")
    data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "MarkdownV2", "disable_web_page_preview": True}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected message")

def get_accounts():
    accounts = []
    for i in range(1, 7):
        addr, pw = env(f"GMAIL_ADDRESS_{i}"), env(f"GMAIL_APP_PASSWORD_{i}").replace(" ", "")
        if addr and pw:
            accounts.append((addr, pw))
    if not accounts:
        addr, pw = env("GMAIL_ADDRESS"), env("GMAIL_APP_PASSWORD").replace(" ", "")
        if addr and pw:
            accounts.append((addr, pw))
    seen, unique = set(), []
    for addr, pw in accounts:
        if addr.lower() not in seen:
            seen.add(addr.lower())
            unique.append((addr, pw))
    return unique

def internal_timestamp(metadata):
    match = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z").timestamp()
    except (ValueError, UnicodeError):
        return None

def process_account(addr, pw, token, chat):
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    new_sent = False
    try:
        mail.login(addr, pw)
        status, _ = mail.select("INBOX")
        if status != "OK":
            raise RuntimeError("Inbox unavailable")
        cutoff = get_cutoff()
        sent_ids = load_sent_ids()
        # Server-side filtering: old mail bodies are never downloaded.
        status, data = mail.uid("search", None, "UNSEEN", "X-GM-RAW", '"after:' + str(cutoff) + '"')
        if status != "OK":
            print("SKIP recent-unread search failed; no broad search fallback")
            return
        ids = data[0].split()[-5:] if data and data[0] else []
        print(addr, "Recent unread candidates:", len(ids))
        for mid in ids:
            uid_str = mid.decode()
            # ── duplicate check ──
            if uid_str in sent_ids:
                print("SKIP already processed UID", uid_str)
                continue
            # Recheck arrival time and unread status before fetching the body.
            st, meta = mail.uid("fetch", mid, "(INTERNALDATE FLAGS)")
            metadata = b" ".join(x for x in (meta or []) if isinstance(x, bytes))
            stamp = internal_timestamp(metadata)
            if st != "OK" or stamp is None or stamp < cutoff or b"\\Seen" in metadata:
                print("SKIP old, read, or unverifiable message UID", uid_str)
                continue
            st, md = mail.uid("fetch", mid, "(BODY.PEEK[])")
            if st != "OK" or not md or not isinstance(md[0], tuple):
                print("SKIP body unavailable UID", uid_str)
                continue
            msg = email.message_from_bytes(md[0][1])
            subject = dec(msg.get("Subject")) or "(কোনো বিষয় নেই)"
            sender = dec(msg.get("From")) or "(অজানা প্রেরক)"
            out, provider = summarize(subject, sender, addr, body_of(msg))
            if not out:
                print("SKIP no valid LLM summary; not sent and kept unread UID", uid_str)
                continue
            try:
                send_tg(token, chat, make_message(addr, sender, subject, out, provider))
            except Exception as e:
                print("SKIP Telegram send failed; kept unread UID", uid_str, type(e).__name__)
                continue
            st, _ = mail.uid("store", mid, "+FLAGS.SILENT", "\\Seen")
            if st != "OK":
                print("WARNING sent but could not mark seen UID", uid_str)
            sent_ids.add(uid_str)
            new_sent = True
            print(addr, "Sent UID", uid_str, "via", provider)
    finally:
        if new_sent:
            save_sent_ids(sent_ids)
        try:
            mail.logout()
        except Exception:
            pass

def main():
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Missing secrets: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    accounts = get_accounts()
    if not accounts:
        raise SystemExit("Missing Gmail accounts")
    print("Accounts:", len(accounts))
    for addr, pw in accounts:
        try:
            process_account(addr, pw, token, chat)
        except Exception as e:
            print(f"Failed for {addr}:", type(e).__name__)

if __name__ == "__main__":
    main()
