# IMAP IDLE daemon — keeps a persistent connection to Gmail.
# When a new mail arrives, runs check_mail.py immediately.
# Usage: python3 idle_daemon.py
# Requirements: pip install -r requirements.txt (or just stdlib)

import imaplib, os, subprocess, sys, time

CHECK_INTERVAL = 120  # seconds between IDLE renewals

def env(n, d=""):
    return os.environ.get(n, d).strip()

def main():
    accounts = []
    for i in range(1, 7):
        addr, pw = env(f"GMAIL_ADDRESS_{i}"), env(f"GMAIL_APP_PASSWORD_{i}").replace(" ", "")
        if addr and pw:
            accounts.append((addr, pw))
    if not accounts:
        addr, pw = env("GMAIL_ADDRESS"), env("GMAIL_APP_PASSWORD").replace(" ", "")
        if addr and pw:
            accounts.append((addr, pw))
    # dedupe
    seen, unique = set(), []
    for a, p in accounts:
        if a.lower() not in seen:
            seen.add(a.lower())
            unique.append((a, p))
    if not unique:
        print("No Gmail accounts configured. Exiting.")
        sys.exit(1)
    print(f"IMAP IDLE daemon started for {len(unique)} account(s)")

    idle_time = 9 * 60  # 9 minutes before Gmail disconnects
    while True:
        for addr, pw in unique:
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
                mail.login(addr, pw)
                mail.select("INBOX")
                print(f"{addr}: IDLE started, waiting for new mail...")

                # IDLE loop
                while True:
                    tag = mail._new_tag()
                    mail.send(f"{tag} IDLE\r\n".encode())
                    start = time.time()
                    while time.time() - start < idle_time:
                        line = mail.readline()
                        if not line:
                            continue
                        line_str = line.decode("utf-8", errors="replace").strip()
                        if line_str.startswith("*") and ("EXISTS" in line_str or "RECENT" in line_str):
                            # New mail detected!
                            print(f"{addr}: NEW MAIL detected — running check_mail.py")
                            mail.send("DONE\r\n".encode())
                            mail.readline()  # consume DONE response
                            try:
                                subprocess.run([sys.executable, "check_mail.py"],
                                               env={**os.environ, "IDLE_ACCOUNT": addr},
                                               timeout=300)
                            except Exception as e:
                                print(f"check_mail.py failed: {type(e).__name__}")
                            # Re-select and restart IDLE
                            mail.select("INBOX")
                            break  # restart IDLE loop
                    else:
                        # Timeout: renew IDLE
                        mail.send("DONE\r\n".encode())
                        mail.readline()
                        print(f"{addr}: IDLE renewed")
                        continue
                    break
            except Exception as e:
                print(f"{addr} connection error: {type(e).__name__}, retrying in {CHECK_INTERVAL}s")
                time.sleep(CHECK_INTERVAL)
                continue
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        time.sleep(5)

if __name__ == "__main__":
    main()
