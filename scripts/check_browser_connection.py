"""Check the provider using an ordinary Chromium browser on the hosted runner."""
import json
from datetime import date,timedelta
from playwright.sync_api import sync_playwright

start=date.today()+timedelta(days=7)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page()
    page.goto('https://www.reservecalifornia.com',wait_until='domcontentloaded',timeout=60000)
    print('Provider page title:',page.title())
    result=page.evaluate('''async ({url,body}) => {
        const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
        const text=await r.text();
        return {status:r.status, contentType:r.headers.get('content-type'),excerpt:text.slice(0,600)};
    }''',{'url':'https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com/rdr/search/grid',
          'body':{'FacilityId':618,'StartDate':start.isoformat(),'EndDate':(start+timedelta(days=2)).isoformat(),'WebOnly':True}})
    print(json.dumps(result))
    level='notice' if result['status']==200 else 'error'
    print(f"::{level}::Standard cloud Chromium provider response: HTTP {result['status']}")
    browser.close()
    if result['status']!=200: raise SystemExit(1)
