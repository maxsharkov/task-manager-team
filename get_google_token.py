"""
Запусти один раз локально, чтобы получить refresh token для Railway.

Шаги:
1. Открой https://console.cloud.google.com/
2. Создай проект → APIs & Services → Enable API → Google Calendar API
3. Credentials → Create Credentials → OAuth client ID → Desktop App
4. Скачай JSON → скопируй client_id и client_secret ниже или передай как аргументы
5. Запусти: python get_google_token.py
6. Браузер откроется → авторизуйся → разреши доступ
7. Скопируй значения в Railway Variables
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]

client_id = input("Google Client ID: ").strip()
client_secret = input("Google Client Secret: ").strip()

flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    },
    scopes=SCOPES,
)

creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

print("\n" + "=" * 60)
print("Добавь в Railway Variables:")
print("=" * 60)
print(f"GOOGLE_CLIENT_ID     = {creds.client_id}")
print(f"GOOGLE_CLIENT_SECRET = {creds.client_secret}")
print(f"GOOGLE_REFRESH_TOKEN = {creds.refresh_token}")
print("GOOGLE_CALENDAR_ID   = primary  (или ID конкретного календаря)")
print("=" * 60)
