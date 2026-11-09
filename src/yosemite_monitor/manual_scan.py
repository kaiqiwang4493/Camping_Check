"""Plan browser-side provider reads and validate/filter their returned results."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from camply.containers.usedirect import UseDirectAvailabilityResponse

from .monitor import (
    CampgroundCandidate, Config, DISPLAY_TIMEZONE, MIN_LEAD_DAYS, Opening,
    build_reserve_california_url, build_run_report, diff_new_openings,
    filter_date_window, filter_minimum_lead_time, filter_minimum_stay,
    filter_weekend_or_holiday, load_config, month_starts_between,
    parse_recreation_openings, scan_window_for_config, select_local_candidate,
)


def browser_scan_plan(app_config, resolved: dict, today: date | None = None) -> tuple[Config, dict]:
    today = today or datetime.now(DISPLAY_TIMEZONE).date()
    settings = app_config.settings
    config = replace(
        load_config(), dry_run=False, date_mode=settings.date_mode,
        start_date=date.fromisoformat(settings.start_date) if settings.start_date else None,
        end_date=date.fromisoformat(settings.end_date) if settings.end_date else None,
        lookahead_amount=settings.lookahead_amount, lookahead_unit=settings.lookahead_unit,
        stay_nights=settings.stay_nights, require_weekend_or_holiday=settings.require_weekend_or_holiday,
        query_interval_minutes=settings.query_interval_hours * 60, schedule_enabled=settings.schedule_enabled,
    )
    start, end = scan_window_for_config(config, today)
    candidates = [
        CampgroundCandidate(provider=provider, **campground)
        for provider, field in [('Recreation.gov', 'recreation_gov_campgrounds'),
                                ('ReserveCalifornia', 'reserve_california_campgrounds')]
        for campground in resolved.get(field, [])
    ]
    requests = []
    if not app_config.watched_campgrounds:
        raise ValueError('请先添加需要查询的营地。')
    for watched in app_config.watched_campgrounds:
        if watched.provider and watched.campground_id and watched.park_name:
            campground = CampgroundCandidate(**watched.dict())
        else:
            campground = select_local_candidate(watched.campground_name, candidates)
        if campground is None or (campground.provider == 'ReserveCalifornia' and not campground.park_id):
            raise ValueError(f'无法确定营地编号，请重新搜索并添加：{watched.campground_name}')
        if campground.provider not in {'Recreation.gov', 'ReserveCalifornia'}:
            raise ValueError('不支持的营地来源。')
        for month in month_starts_between(start, end):
            item = {'key': f'{campground.provider}|{campground.campground_id}|{month}',
                    'provider': campground.provider, 'campground': campground.to_config()}
            if campground.provider == 'Recreation.gov':
                query = urlencode({'start_date': f'{month.isoformat()}T00:00:00.000Z'})
                item.update(method='GET', url=f'https://www.recreation.gov/api/camps/availability/campground/{campground.campground_id}/month?{query}')
            else:
                next_month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
                item.update(method='POST', url='https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com/rdr/search/grid',
                            body={'FacilityId': int(campground.campground_id),
                                  'StartDate': max(month, start).isoformat(),
                                  'EndDate': min(end, next_month - timedelta(days=1)).isoformat(),
                                  'WebOnly': True, 'InSeasonOnly': True, 'UnitSort': 'orderby'})
            if item['key'] not in {entry['key'] for entry in requests}:
                requests.append(item)
    plan = {'today': today.isoformat(), 'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'stay_nights': config.stay_nights, 'require_weekend_or_holiday': config.require_weekend_or_holiday,
            'requests': requests}
    plan['id'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return config, plan


def openings_from_response(item: dict, payload: dict) -> list[Opening]:
    campground = item['campground']
    if not isinstance(payload, dict):
        raise ValueError('供应商未返回有效的余位数据。')
    if item['provider'] == 'Recreation.gov':
        if not isinstance(payload.get('campsites'), dict):
            raise ValueError('Recreation.gov 未返回有效的余位数据。')
        return parse_recreation_openings(campground['park_name'], campground['campground_name'], campground['campground_id'], payload)
    grid = UseDirectAvailabilityResponse(**payload)
    if grid.Facility is None or grid.Facility.FacilityId != int(campground['campground_id']):
        raise ValueError('ReserveCalifornia 返回的营地编号不匹配。')
    openings = []
    for unit in (grid.Facility.Units or {}).values():
        for slice in (unit.Slices or {}).values():
            if slice.IsFree and not slice.IsBlocked and not slice.IsWalkin:
                openings.append(Opening(
                    provider='ReserveCalifornia', park_name=campground['park_name'],
                    campground_name=campground['campground_name'], campground_id=campground['campground_id'],
                    site=str(unit.Name), date=slice.Date.isoformat(),
                    url=build_reserve_california_url(campground['park_id'], campground['campground_id']),
                    campsite_id=str(unit.UnitId),
                ))
    return openings


def report_from_browser(config: Config, plan: dict, results: list[dict], previous: dict) -> dict:
    by_key = {result['key']: result['payload'] for result in results}
    if len(by_key) != len(results) or set(by_key) != {item['key'] for item in plan['requests']}:
        raise ValueError('查询数据不完整，请重新查询；原有结果已保留。')
    openings = []
    for item in plan['requests']:
        openings.extend(openings_from_response(item, by_key[item['key']]))
    return report_from_openings(config, plan, openings, previous)


def report_from_openings(config: Config, plan: dict, openings: list[Opening], previous: dict) -> dict:
    openings = list({opening.key: opening for opening in openings}.values())
    openings = filter_date_window(openings, date.fromisoformat(plan['start_date']), date.fromisoformat(plan['end_date']))
    openings = filter_minimum_stay(openings, config.stay_nights)
    openings = filter_minimum_lead_time(openings, date.fromisoformat(plan['today']), MIN_LEAD_DAYS)
    if config.require_weekend_or_holiday:
        openings = filter_weekend_or_holiday(openings)
    openings.sort(key=lambda item: (item.date, item.park_name, item.campground_name, item.site))
    report = build_run_report(
        config=config, current_openings=openings, new_openings=diff_new_openings(openings, previous),
        sms_status='manual_browser_scan', sms_messages_sent=0,
        email_status='manual_browser_scan', email_messages_sent=0,
    )
    report['scan_source'] = 'browser'
    return report
