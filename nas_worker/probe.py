#!/usr/bin/env python3
"""Read-only provider connectivity probe. Each response is deleted on exit."""
import datetime
import json
from pathlib import Path
import resource
import tempfile
from urllib.parse import urlencode

from worker import download


def main():
    start = datetime.date.today() + datetime.timedelta(days=7)
    month = start.replace(day=1)
    requests = [
        {"provider": "ReserveCalifornia", "campground": {"campground_id": "618"},
         "body": {"FacilityId": 618, "StartDate": start.isoformat(),
                  "EndDate": (start + datetime.timedelta(days=2)).isoformat()}},
        {"provider": "Recreation.gov", "campground": {"campground_id": "232447"},
         "url": "https://www.recreation.gov/?" + urlencode({"start_date": month.isoformat() + "T00:00:00.000Z"})},
    ]
    with tempfile.TemporaryDirectory(prefix="camping-probe-") as temporary:
        for item in requests:
            path = Path(temporary) / "response.json"
            count = download(item, path)
            # Only this one-shot diagnostic parses raw JSON on the NAS.
            with path.open() as handle:
                payload = json.load(handle)
            if item["provider"] == "ReserveCalifornia":
                assert payload.get("Facility", {}).get("FacilityId") == 618
            else:
                assert isinstance(payload.get("campsites"), dict)
            del payload
            path.unlink()
            print(json.dumps({"provider": item["provider"], "http_status": 200, "validated": True,
                              "response_bytes": count, "peak_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))


if __name__ == "__main__":
    main()
