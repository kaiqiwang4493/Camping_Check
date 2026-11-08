"""One bounded read-only probe to diagnose provider access from a runner."""
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from datetime import date, timedelta

start = date.today() + timedelta(days=7)
request = Request(
    'https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com/rdr/search/grid',
    data=json.dumps({'FacilityId': 618, 'StartDate': start.isoformat(),
                     'EndDate': (start + timedelta(days=2)).isoformat(), 'WebOnly': True}).encode(),
    headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*',
             'Origin': 'https://www.reservecalifornia.com', 'Referer': 'https://www.reservecalifornia.com/',
             'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'},
)
try:
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
        assert payload.get('Facility', {}).get('FacilityId') == 618
        print('::notice::Live availability returned HTTP 200 with the expected facility.')
except HTTPError as exc:
    print(f'::error::Provider HTTP {exc.code}; server={exc.headers.get("Server", "unknown")}')
    raise
