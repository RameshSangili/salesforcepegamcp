import base64
import hashlib
import json
import os
import ssl
import urllib.parse
import urllib.request
import webbrowser

CLIENT_ID = "3MVG9nSH73I5aFNi1._.oYzqFFlQX7QCSbG7NKSXbytZQQ3gE9A.XzpOme5Luew3GXmNc9fbhZdVLGq_JyN7g"
CLIENT_SECRET = "YOUR_CLIENT_SECRET_HERE"
REDIRECT_URI = "https://login.salesforce.com/services/oauth2/success"
AUTH_URL = "https://login.salesforce.com/services/oauth2/authorize"
TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# Step 1: Generate PKCE pair and open browser
code_verifier, code_challenge = _pkce_pair()

params = urllib.parse.urlencode({
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "scope": "mcp_api refresh_token offline_access",
    "code_challenge": code_challenge,
    "code_challenge_method": "S256",
})
webbrowser.open(f"{AUTH_URL}?{params}")
print("Browser opened — log into Salesforce and approve the access request.")
print()
print("After approving, your browser will land on a success page.")
print("Copy the FULL URL from the browser address bar and paste it here:")
print()

callback_url = input("Paste the full redirect URL: ").strip()

parsed = urllib.parse.urlparse(callback_url)
params_dict = urllib.parse.parse_qs(parsed.query)
auth_code = params_dict.get("code", [None])[0]

if not auth_code:
    print("\nNo 'code' found in the URL. Make sure you copied the full URL after approving.")
    raise SystemExit(1)

# Step 2: Exchange code for tokens
data = urllib.parse.urlencode({
    "grant_type": "authorization_code",
    "code": auth_code,
    "redirect_uri": REDIRECT_URI,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code_verifier": code_verifier,
}).encode()

req = urllib.request.Request(TOKEN_URL, data=data)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
response = urllib.request.urlopen(req, context=ctx)
tokens = json.loads(response.read())

print("\n✅ SUCCESS! Your tokens:")
print(f"Access Token:  {tokens.get('access_token')}")
print(f"Refresh Token: {tokens.get('refresh_token')}")
print("\nSave the Refresh Token as SALESFORCE_REFRESH_TOKEN in your .env / Cloud Run secret!")
