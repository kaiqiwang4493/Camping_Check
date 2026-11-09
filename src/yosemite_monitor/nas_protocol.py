"""Typed plans and cloud-side parsing; never performs a provider HTTP request."""
from __future__ import annotations

from dataclasses import asdict
import re

from .manual_scan import browser_scan_plan, openings_from_response, report_from_openings
from .monitor import CampgroundCandidate, campground_match_score


def scan_plan(config):
    from .web import AppConfig
    for campground in config['watched_campgrounds']:
        if not re.fullmatch(r'[0-9]{1,12}', str(campground.get('campground_id', ''))):
            raise ValueError('请重新搜索并添加营地，以保存有效的营地编号。')
        if campground['provider'] == 'ReserveCalifornia' and not re.fullmatch(r'[0-9]{1,12}', str(campground.get('park_id', ''))):
            raise ValueError('ReserveCalifornia 公园编号无效。')
    return browser_scan_plan(AppConfig(**config), {})[1]


def search_plan(query):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        raise ValueError('请输入营地或公园名称。')
    return {'query': query.strip(), 'requests': [
        {'key': 'search|Recreation.gov|' + query, 'provider': 'Recreation.gov', 'operation': 'search', 'query': query},
        {'key': 'search|ReserveCalifornia|places', 'provider': 'ReserveCalifornia', 'operation': 'places'},
        {'key': 'search|ReserveCalifornia|facilities', 'provider': 'ReserveCalifornia', 'operation': 'facilities'},
    ]}


def parse_search(plan, payloads):
    recreation, places, facilities = [], {}, []
    for item, payload in payloads:
        if item['provider'] == 'Recreation.gov':
            if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
                raise ValueError('Recreation.gov 搜索响应无效。')
            for row in payload['results']:
                if row.get('entity_type') != 'campground' or not str(row.get('entity_id', '')).isdigit():
                    continue
                recreation.append(CampgroundCandidate(provider='Recreation.gov',
                    campground_name=row['name'], campground_id=str(row['entity_id']),
                    park_name=row.get('parent_name') or row['name'], park_id=row.get('parent_id')))
        elif item['operation'] == 'places':
            if not isinstance(payload, list):
                raise ValueError('ReserveCalifornia 公园目录无效。')
            places = {int(row['PlaceId']): row for row in payload if row.get('Name')}
        else:
            if not isinstance(payload, list):
                raise ValueError('ReserveCalifornia 营地目录无效。')
            facilities = payload
    candidates = list(recreation)
    for row in facilities:
        place = places.get(int(row['PlaceId']))
        if not place or not row.get('Name') or row.get('AllowWebBooking') is False:
            continue
        candidates.append(CampgroundCandidate(provider='ReserveCalifornia', campground_name=row['Name'],
                          campground_id=str(row['FacilityId']), park_name=place['Name'], park_id=row['PlaceId']))
    candidates = [c for c in candidates if campground_match_score(plan['query'], c) >= 0.35]
    candidates.sort(key=lambda c: campground_match_score(plan['query'], c), reverse=True)
    return {'query': plan['query'], 'candidates': [asdict(c) for c in candidates[:25]], 'ai_used': False}


def build_result(job, payloads, previous):
    if job['kind'] == 'search':
        return parse_search(job['plan'], payloads), None
    from .web import AppConfig
    config, _ = browser_scan_plan(AppConfig(**job['config']), {})
    openings = []
    for item, payload in payloads:
        openings.extend(openings_from_response(item, payload))
        if len(openings) > 100000:
            raise ValueError('可用日期记录超过处理上限，请减少查询范围。')
    report = report_from_openings(config, job['plan'], openings, previous)
    report.update(scan_source='nas', job_id=job['job_id'], trigger=job['trigger'],
                  config_revision=job['config_revision'], email_configured=True,
                  email_partially_configured=False, sms_status='disabled',
                  email_status=('manual_scan' if job['trigger'] == 'manual' else
                                ('queued' if report['new_openings_count'] else 'no_new_openings')))
    report['scan_start_date'], report['scan_end_date'] = job['plan']['start_date'], job['plan']['end_date']
    baseline = {'active_openings': {
        '|'.join(str(row[key]) for key in ('provider', 'campground_id', 'site', 'date', 'nights')): True
        for row in report['current_openings']}}
    return report, baseline
