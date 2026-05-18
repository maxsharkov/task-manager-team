import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

# Google Calendar color IDs: 11=Tomato(red), 5=Banana(yellow), 2=Sage(green)
_PRIORITY_COLOR = {"Высокий": "11", "Средний": "5", "Низкий": "2"}


def _get_service():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        return None
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
    )
    return build("calendar", "v3", credentials=creds)


_RRULE = {
    "Ежедневно":   "RRULE:FREQ=DAILY",
    "Еженедельно": "RRULE:FREQ=WEEKLY",
    "Ежемесячно":  "RRULE:FREQ=MONTHLY",
}


def _build_body(title, deadline, priority=None, project=None, assignee=None, recurrence=None):
    parts = []
    if assignee:
        parts.append(f"Исполнитель: {assignee}")
    if priority:
        parts.append(f"Приоритет: {priority}")
    date_str = str(deadline)
    body = {
        "summary": title,
        "description": "\n".join(parts),
        "start": {"date": date_str},
        "end": {"date": date_str},
        "colorId": _PRIORITY_COLOR.get(priority, "0"),
    }
    if recurrence and recurrence in _RRULE:
        body["recurrence"] = [_RRULE[recurrence]]
    return body


def create_event(title, deadline, priority=None, project=None, assignee=None, recurrence=None):
    service = _get_service()
    if not service or not deadline:
        return None
    try:
        event = service.events().insert(
            calendarId=CALENDAR_ID,
            body=_build_body(title, deadline, priority, project, assignee, recurrence),
        ).execute()
        return event.get("id")
    except HttpError as e:
        print(f"GCal create error: {e}")
        return None


def update_event(event_id, title, deadline, priority=None, project=None, assignee=None, recurrence=None):
    service = _get_service()
    if not service or not event_id or not deadline:
        return False
    try:
        service.events().update(
            calendarId=CALENDAR_ID,
            eventId=event_id,
            body=_build_body(title, deadline, priority, project, assignee, recurrence),
        ).execute()
        return True
    except HttpError as e:
        print(f"GCal update error: {e}")
        return False


def delete_event(event_id):
    service = _get_service()
    if not service or not event_id:
        return False
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return True
    except HttpError as e:
        print(f"GCal delete error: {e}")
        return False
