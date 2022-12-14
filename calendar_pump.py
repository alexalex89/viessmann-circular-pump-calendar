import datetime
import fhem
import json
import pathlib
import os

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account


# If modifying these scopes, delete the file service_account.json.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SUBJECT = os.environ["GOOGLE_SUBJECT"]
FHEM_IP = os.environ["FHEM_IP"]
FHEM_VITOCONNECT_OBJECT = os.environ["FHEM_VITOCONNECT_OBJECT"]
EARLY_DUTY_LABEL = os.environ["EARLY_DUTY_LABEL"]
LATE_DUTY_LABEL = os.environ["LATE_DUTY_LABEL"]
NIGHT_DUTY_LABEL = os.environ["NIGHT_DUTY_LABEL"]


def main():
    credentials = service_account.Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    try:
        service = build('calendar', 'v3', credentials=credentials)

        # Call the Calendar API
        utc_now = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        now = utc_now.isoformat() + 'Z'  # 'Z' indicates UTC time
        utc_end = (utc_now + datetime.timedelta(days=7))
        events_result = service.events().list(calendarId=SUBJECT, timeMin=now,
                                              timeMax=utc_end.isoformat() + 'Z', singleEvents=True,
                                              orderBy='startTime').execute()
        events = events_result.get('items', [])

        if not events:
            print('No upcoming events found.')
            return

        result_circular_pump, result_hot_water = calc_times(events, utc_end, utc_now)

        print("Sending new schedule to FHEM ...")

        fh = fhem.Fhem(FHEM_IP, protocol="http", port=8083)
        fh.send_cmd(f"""set {FHEM_VITOCONNECT_OBJECT} WW-Zirkulationspumpe_Zeitplan {json.dumps(result_circular_pump)}""")
        if os.environ["HOTWATER"] == "1":
            fh.send_cmd(f"""set {FHEM_VITOCONNECT_OBJECT} WW-Zeitplan {json.dumps(result_hot_water)}""")

        print("Done")
    except HttpError as error:
        print('An error occurred: %s' % error)


def calc_times(events, utc_end, utc_now):
    early_duties = []
    late_duties = []
    night_duties = []

    # valid cycles: 5/25 and 5/10
    template_single_entry = """{{"start":"{start}","position":{position},"end":"{end}","mode":"5/{cycle}-cycles"}}"""
    template_hot_water = """{{"start": "{start}", "mode": "top", "position": 0, "end": "22:00"}}"""
    times = {"early_duty_times_weekday": [
        eval(template_single_entry.format(start="04:00", end="06:30", cycle=10, position=0)),
        eval(template_single_entry.format(start="11:45", end="19:30", cycle=25, position=1))],
             "early_duty_times_weekend": [
                 eval(template_single_entry.format(start="04:00", end="05:10", cycle=10, position=0)),
                 eval(template_single_entry.format(start="08:00", end="09:30", cycle=25, position=1)),
                 eval(template_single_entry.format(start="11:40", end="19:30", cycle=25, position=2))],
             "late_duty_times_weekday": [
                 eval(template_single_entry.format(start="05:50", end="06:30", cycle=10, position=0)),
                 eval(template_single_entry.format(start="09:00", end="12:30", cycle=25, position=1)),
                 eval(template_single_entry.format(start="17:30", end="19:30", cycle=25, position=2))],
             "late_duty_times_weekend": [
                 eval(template_single_entry.format(start="08:00", end="12:30", cycle=25, position=0)),
                 eval(template_single_entry.format(start="17:30", end="19:30", cycle=25, position=1))],
             "night_duty_times_weekday": [
                 eval(template_single_entry.format(start="05:50", end="06:30", cycle=10, position=0)),
                 eval(template_single_entry.format(start="11:40", end="19:30", cycle=25, position=1))],
             "night_duty_times_weekend": [
                 eval(template_single_entry.format(start="08:00", end="09:30", cycle=10, position=0)),
                 eval(template_single_entry.format(start="11:40", end="19:30", cycle=25, position=1))],
             "weekday_times": [eval(template_single_entry.format(start="05:50", end="06:30", cycle=10, position=0)),
                               eval(template_single_entry.format(start="09:00", end="13:00", cycle=25, position=1)),
                               eval(template_single_entry.format(start="17:30", end="19:30", cycle=10, position=2))],
             "weekend_times": [eval(template_single_entry.format(start="08:00", end="19:30", cycle=25, position=0))]}
    for description in times.keys():
        override = pathlib.Path("/overrides").joinpath(description)
        if override.exists():
            print(f"Found override for {description}.")
            with override.open("r") as f_override:
                times[description] = json.load(f_override)
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))

        if EARLY_DUTY_LABEL in event['summary']:
            early_duties.append(datetime.datetime.strptime(start, "%Y-%m-%d"))
        if LATE_DUTY_LABEL in event['summary']:
            late_duties.append(datetime.datetime.strptime(start, "%Y-%m-%d"))
        if NIGHT_DUTY_LABEL in event['summary']:
            night_duties.append(datetime.datetime.strptime(start, "%Y-%m-%d"))
    utc_now = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    result_circular_pump = {}
    result_hot_water = {}
    while utc_now < utc_end:
        day = utc_now.strftime("%a").lower()

        override = pathlib.Path("/overrides").joinpath(day)
        if override.exists():
            print(f"Found override for {day}.")
            with override.open("r") as f_override:
                result_circular_pump[day] = json.load(f_override)
        else:
            if utc_now in early_duties:
                if day in ("sat", "sun"):
                    result_circular_pump[day] = times["early_duty_times_weekend"]
                else:
                    result_circular_pump[day] = times["early_duty_times_weekday"]
            elif utc_now in late_duties:
                if day in ("sat", "sun"):
                    result_circular_pump[day] = times["late_duty_times_weekend"]
                else:
                    result_circular_pump[day] = times["late_duty_times_weekday"]
            elif utc_now in night_duties:
                if day in ("sat", "sun"):
                    result_circular_pump[day] = times["night_duty_times_weekend"]
                else:
                    result_circular_pump[day] = times["night_duty_times_weekday"]
            elif day in ("sat", "sun"):
                result_circular_pump[day] = times["weekend_times"]
            else:
                result_circular_pump[day] = times["weekday_times"]

        hour, minute = list(filter(lambda x: x["position"] == 0, result_circular_pump[day]))[0]["start"].split(":")
        start_hot_water = f"{(int(hour) - 1):02d}:{minute}"
        result_hot_water[day] = [eval(template_hot_water.format(start=start_hot_water))]

        print(f"[HOT WATER] {day}: {' | '.join(entry['start'] + '-' + entry['end'] for entry in result_hot_water[day])}")
        print(f"[CIRCULAR PUMP] {day}: {' | '.join(entry['start'] + '-' + entry['end'] for entry in result_circular_pump[day])}")
        utc_now += datetime.timedelta(days=1)
    return result_circular_pump, result_hot_water


if __name__ == '__main__':
    main()
