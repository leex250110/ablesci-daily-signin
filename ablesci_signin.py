"""
AbleSci.com Daily Sign-In Script
Designed to run in GitHub Actions
"""
import os, re, sys, time
import requests

EMAIL = os.environ.get("ABLESCI_EMAIL")
PASSWORD = os.environ.get("ABLESCI_PASSWORD")

if not EMAIL or not PASSWORD:
    print("Error: ABLESCI_EMAIL and ABLESCI_PASSWORD must be set")
    sys.exit(1)

session = requests.Session()
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Step 1: Get CSRF token from login page
print("1. Fetching login page...")
r = session.get("https://www.ablesci.com/site/login", headers=headers)
match = re.search(r'id="csrf-val" value="([^"]+)"', r.text)
if not match:
    print("Error: Could not find CSRF token")
    sys.exit(1)
csrf = match.group(1)
print(f"   CSRF token obtained")

# Step 2: Login
print("2. Logging in...")
login_data = {"_csrf": csrf, "email": EMAIL, "password": PASSWORD}
h2 = dict(headers)
h2["X-Requested-With"] = "XMLHttpRequest"
h2["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
r2 = session.post("https://www.ablesci.com/site/login", data=login_data, headers=h2)
result = r2.json()
if result.get("code") != 0:
    print(f"   Login failed: {result.get('msg', 'unknown error')}")
    sys.exit(1)
print("   Login successful!")

# Step 3: Visit homepage to sync session
print("3. Syncing session...")
session.get("https://www.ablesci.com/", headers=headers)

# Step 4: Attempt sign-in
print("4. Attempting daily sign-in...")
r3 = session.get("https://www.ablesci.com/user/sign", headers=headers)
try:
    sign_result = r3.json()
    code = sign_result.get("code")
    msg = sign_result.get("msg", "")
    if code == 0:
        print(f"   ✅ Sign-in successful! {msg}")
    elif code == 1:
        if "已" in msg:
            print(f"   ℹ️  Already signed in today: {msg}")
        else:
            print(f"   ❌ Sign-in failed: {msg}")
    else:
        print(f"   ❓ Unknown response: {sign_result}")
except Exception:
    print(f"   ❓ Raw response: {r3.text[:200]}")

# Step 5: Verify
print("5. Verifying sign-in status...")
r4 = session.get("https://www.ablesci.com/", headers=headers)
m = re.search(r"当前拥有<[^>]+>(\d+)</cite>积分", r4.text)
if m:
    print(f"   Current points: {m.group(1)}")
m2 = re.search(r"已连续签到<[^>]+>(\d+)</cite>", r4.text)
if m2:
    print(f"   Consecutive days: {m2.group(1)}")
