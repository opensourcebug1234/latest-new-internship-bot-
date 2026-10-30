import json
import time
from numbers import Number
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.request import Request, urlopen
from jsonschema import validate


def as_dict(value):
    return value.model_dump(by_alias=True) if hasattr(value, "model_dump") else value


def retry_read(fn, attempts=3):
    for attempt in range(attempts):
        try:
            return fn()
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(min(2 ** attempt, 4))


def build_input(schema, kind, value, config):
    """Only emit fields present in the fetched actor schema; fail closed on drift."""
    props = schema.get('properties', {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config['time_window_days'])).strftime('%Y-%m-%d')
    days = config['time_window_days']
    window = 'day' if days <= 1 else 'week' if days <= 7 else 'month' if days <= 30 else 'year' if days <= 365 else 'all'
    n = config['apify']['max_items']
    payload = dict(maxItems=n, maxPostCount=n, maxComments=n if kind == 'thread' else 0,
                   skipComments=kind != 'thread', skipUserPosts=True, skipCommunity=True,
                   searchPosts=True, searchComments=False, searchUsers=False, searchCommunities=False,
                   searchMedia=False, sort='new', time=window, postDateLimit=cutoff, commentDateLimit=cutoff)
    if kind == 'search':
        payload.update(searches=[value], startUrls=[])
    else:
        payload.update(startUrls=[{'url': value}], searches=[])
    missing = set(payload) - set(props)
    if missing:
        raise ValueError('Actor input schema changed; review mapping before spending: ' + ', '.join(sorted(missing)))
    validate(payload, schema)
    return payload


class ApifySource:
    def __init__(self, config, token, log=print, client=None):
        from apify_client import ApifyClient
        self.c, self.log = config, log
        self.client = client or ApifyClient(token=token, max_retries=config['apify']['read_retries'])
        # Non-idempotent run creation must not be blindly retried on an ambiguous response.
        self.starter = client or ApifyClient(token=token, max_retries=0)
        self.cost = 0.0
        self.run_ids = []
        self.started = 0
        self.schema = None
        self.build = None
        self.printed_fields = set()
        self.token = token
        self.on_progress = lambda: None

    def fetch_schema(self):
        actor = self.c['apify']['actor'].replace('/', '~')
        def fetch():
            request = Request(f'https://api.apify.com/v2/acts/{actor}/builds/default',
                              headers={'Authorization': 'Bearer ' + self.token})
            with urlopen(request, timeout=30) as response:
                return json.load(response)['data']
        data = retry_read(fetch, self.c['apify']['read_retries'] + 1)
        schema = data.get('inputSchema')
        if isinstance(schema, str):
            schema = json.loads(schema)
        if not isinstance(schema, dict) or not schema.get('properties'):
            raise ValueError('Apify default build did not expose an input schema; no run started.')
        self.schema, self.build = schema, data['buildNumber']
        self.log(f'Actor schema verified from build {self.build}: ' + ', '.join(sorted(schema['properties'])))
        return schema

    def scrape(self, kind, value):
        from .scoring import BudgetExceeded
        if self.started >= self.c['apify']['max_runs']:
            raise BudgetExceeded('Apify run limit reached; remaining queries/threads deferred.')
        if self.schema is None:
            self.fetch_schema()
        cfg = self.c['apify']
        payload = build_input(self.schema, kind, value, self.c)
        self.started += 1
        reserve = cfg['max_total_charge_usd']
        self.cost += reserve
        self.on_progress()
        try:
            run = self.starter.actor(cfg['actor']).start(run_input=payload, build=self.build,
                max_total_charge_usd=Decimal(str(reserve)), max_items=cfg['max_items'],
                run_timeout=timedelta(seconds=cfg['timeout_seconds']))
        except Exception:
            raise RuntimeError('Apify start failed. Check Apify Console before retrying: a run may have started. Reserved cost retained.') from None
        run = as_dict(run)
        self.run_ids.append(run['id'])
        self.on_progress()
        self.log(f'Apify run {run["id"]}: {kind} {value}')
        result = self.client.run(run['id']).wait_for_finish(wait_duration=timedelta(seconds=cfg['timeout_seconds'] + 30))
        result = as_dict(result)
        if result and result.get('status') not in ('SUCCEEDED', 'FAILED', 'TIMED-OUT', 'ABORTED'):
            self.client.run(run['id']).abort()
            raise RuntimeError(f'Apify run {run["id"]} exceeded the wait limit; abort requested.')
        if result and isinstance(result.get('usageTotalUsd'), Number):
            self.cost += float(result['usageTotalUsd']) - reserve
            self.on_progress()
        if not result or result.get('status') != 'SUCCEEDED':
            raise RuntimeError(f'Apify run {run["id"]} did not succeed; inspect it in Apify Console.')
        items = list(self.client.dataset(result['defaultDatasetId']).iterate_items(limit=cfg['max_items']))
        for item in items:
            if isinstance(item, dict):
                kind_name = item.get('dataType', 'unknown')
                if kind_name not in self.printed_fields:
                    self.log(f'First {kind_name} result fields: ' + ', '.join(sorted(item)))
                    self.printed_fields.add(kind_name)
        return items
