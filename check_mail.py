import email, html, imaplib, json, os, re, time, urllib.request
from functools import partial

print = partial(print, flush=True)
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
    t = strip_html(t or "")
    t = re.sub(r"https?://\S+", "", t)
    t = t.replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ").replace("\ufeff", "")
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
    t = str(t or "").replace("\\", "/")
    for ch in ["*", "_", "`", "[", "]"]:
        t = t.replace(ch, "")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def build_prompt(subject, sender, account, body):
    return ("তুমি একজন সাহায্যকারী। কাজ: একটি ইমেইল পড়ে খুব সহজ বাংলায় বুঝিয়ে দাও। কঠিন টেকনোলজির ইংরেজি শব্দ থাকলে সহজ বাংলায় অর্থ বুঝিয়ে বলবে। কোনো Markdown চিহ্ন ব্যবহার করবে না। তারা, আন্ডারস্কোর, ব্যাকটিক, হ্যাশ ব্যবহার করবে না। শুধু নিচের ৪টি লেবেল দিয়ে উত্তর দাও, প্রতিটি লেবেল নতুন লাইনে শুরু করবে:\nসারাংশ: সহজ বাংলায় ২ থেকে ৪ বাক্যে মূল কথা\nবিস্তারিত: মেইলটা আসলে কী বোঝাতে চাইছে, কেন পাঠানো হয়েছে\nনিরাপত্তা: মেইলটা নিরাপদ মনে হচ্ছে কিনা, সন্দেহজনক কিছু আছে কিনা, লিংকে চাপ দেওয়া বা পাসওয়ার্ড, OTP, টাকা চাওয়া হয়েছে কিনা\nকরণীয়: ব্যবহারকারীর পরবর্তী পদক্ষেপ কী, কিছু করতে হবে কিনা পরিষ্কার করে বলো\n\n" + f"যে Gmail-এ এসেছে: {account}\nপাঠিয়েছে: {sender}\nবিষয়: {subject}\nইমেইল:\n{body[:7000]}")

def http_json(url, data, headers, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def gemini_once(model, key, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
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
            text = (msg.get("content") or msg.get("reasoning") or "").strip()
            if text:
                return text
            raise RuntimeError("empty content")
        except Exception as e:
            last = e
            print(f"Model {model} failed:", e)
    raise last

def call_openrouter(prompt):
    return call_chat("https://openrouter.ai/api/v1/chat/completions", env("OPENROUTER_API_KEY"), ["openrouter/free"], prompt, {"X-Title": "Gmail to Telegram"})

def call_groq(prompt):
    return call_chat("https://api.groq.com/openai/v1/chat/completions", env("GROQ_API_KEY"), ["openai/gpt-oss-20b"], prompt)

def parse_ai(text):
    text = md_clean(text or "")
    out = {"summary": "", "detail": "", "security": "", "action": ""}
    keys = [("সারাংশ:", "summary"), ("বিস্তারিত:", "detail"), ("নিরাপত্তা:", "security"), ("করণীয়:", "action")]
    for i, (label, name) in enumerate(keys):
        start = text.find(label)
        if start == -1:
            continue
        start += len(label)
        end = len(text)
        for next_label, _ in keys[i+1:]:
            pos = text.find(next_label, start)
            if pos != -1:
                end = min(end, pos)
        out[name] = text[start:end].strip(" \n:-।")
    if len(out["summary"]) < 10:
        out["summary"] = ""
    return out

def fallback_security(body, sender):
    low = (body or "").lower()
    bad = ["password", "otp", "verify your account", "urgent", "suspended", "click here", "login", "bank account", "send money", "lottery", "prize", "পাসওয়ার্ড", "ওটিপি"]
    hits = [w for w in bad if w in low]
    if hits:
        return "সতর্ক থাকুন। এই মেইলে পাসওয়ার্ড, OTP, লগইন বা টাকা সংক্রান্ত সন্দেহজনক কথা থাকতে পারে। কোনো লিংকে চাপ দেবেন না, পাসওয়ার্ড বা OTP দেবেন না। আগে পাঠানো ব্যক্তির ঠিকানা ভালো করে যাচাই করুন।"
    return "মেইলটা আপাতদৃষ্টিতে স্বাভাবিক মনে হচ্ছে। তারপরও অপরিচিত লিংকে চাপ দেওয়ার আগে পাঠানো ব্যক্তির ঠিকানা যাচাই করে নিন।"

def summarize(subject, sender, account, body):
    prompt = build_prompt(subject, sender, account, body)
    for name, fn in [("Gemini", call_gemini), ("OpenRouter", call_openrouter), ("Groq", call_groq)]:
        try:
            text = fn(prompt)
            if text:
                out = parse_ai(text)
                if out["summary"]:
                    if not out["detail"]:
                        out["detail"] = out["summary"]
                    if not out["security"]:
                        out["security"] = fallback_security(body, sender)
                    if not out["action"]:
                        out["action"] = "ইমেইলটি দেখে প্রয়োজন হলে ব্যবস্থা নিন।"
                    return out, name
                else:
                    print(f"{name} gave empty summary, trying next fallback")
        except Exception as e:
            print(f"{name} failed, trying next fallback:", e)
    return None, None

def make_message(account, sender, subject, out, provider):
    return ("*📧 নতুন ইমেইল এসেছে*\n\n" + f"*📌 বিষয়:* {md_clean(subject)}\n" + f"*👤 পাঠিয়েছে:* {md_clean(sender)}\n" + f"*📥 কোন Gmail-এ:* {md_clean(account)}\n" + f"*🤖 সারাংশ দিয়েছে:* {md_clean(provider)}\n\n" + "*📝 সহজ বাংলায় সারাংশ*\n" + md_clean(out['summary']) + "\n\n" + "*🔍 মেইলটা আসলে কী বোঝাতে চাইছে*\n" + md_clean(out['detail']) + "\n\n" + "*🛡️ নিরাপত্তা যাচাই*\n" + md_clean(out['security']) + "\n\n" + "*✅ আপনার পরবর্তী পদক্ষেপ*\n" + md_clean(out['action']))

def send_tg(token, chat, text):
    for i in range(0, len(text), 3900):
        data = json.dumps({"chat_id": chat, "text": text[i:i+3900], "parse_mode": "Markdown", "disable_web_page_preview": True}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage", data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read().decode())
        if not res.get("ok"):
            raise RuntimeError(str(res))

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

def process_account(addr, pw, token, chat):
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        mail.login(addr, pw)
        mail.select("INBOX")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            print(addr, "search failed")
            return
        ids = data[0].split()[-5:] if data and data[0] else []
        print(addr, "Unseen:", len(ids))
        for mid in ids:
            st, md = mail.fetch(mid, "(BODY.PEEK[])")
            if st != "OK" or not md or not isinstance(md[0], tuple):
                continue
            msg = email.message_from_bytes(md[0][1])
            subject = dec(msg.get("Subject")) or "(কোনো বিষয় নেই)"
            sender = dec(msg.get("From")) or "(অজানা)"
            out, provider = summarize(subject, sender, addr, body_of(msg))
            if not out:
                print(f"SKIP no LLM succeeded, not sending and not marking seen: {addr} | {subject}")
                continue
            send_tg(token, chat, make_message(addr, sender, subject, out, provider))
            mail.store(mid, "+FLAGS", "\\Seen")
            print(addr, "Sent:", subject, "via", provider)
            time.sleep(2)
    finally:
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
            print(f"Failed for {addr}: {e}")

if __name__ == "__main__":
    main()
