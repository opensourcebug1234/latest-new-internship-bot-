import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from filelock import FileLock, Timeout
from .config import live_errors, local_path
from .mapping import map_item, parse_date
from .scoring import Scorer, Provider, DemoProvider, BudgetExceeded
from .storage import Store


def safe_error(exc):
    message = str(exc)
    for name in ('APIFY_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
        value = os.getenv(name)
        if value:
            message = message.replace(value, '[REDACTED]')
    return re.sub(r'(?:apify_api_|sk-)[A-Za-z0-9_-]+', '[REDACTED]', message)[:1500]


def relevant(post, config):
    text = (post['parent_title'] + ' ' + post['text']).casefold()
    terms = config['relevance_terms'] + config['company']['pain_phrases'] + config['company']['competitors']
    return any(t.casefold() in text for t in terms if t.strip())


def run_pipeline(config, dry_run=False, max_items=None, log=print, source=None, provider=None):
    c = copy.deepcopy(config)
    if max_items is not None:
        if not 1 <= max_items <= 500:
            raise ValueError('Max items must be between 1 and 500.')
        c['apify']['max_items'] = max_items
    if not dry_run:
        errors = live_errors(c)
        if errors:
            raise ValueError('\n'.join(errors))
    db_path = local_path(c, 'demo_database' if dry_run else 'database')
    try:
        with FileLock(str(db_path) + '.lock', timeout=0):
            if not dry_run and 'collection' in c:
                from .collection import remaining_budget
                if c['apify']['max_runs'] * c['apify']['max_total_charge_usd'] > remaining_budget(c) + 1e-9:
                    raise ValueError('Scan exceeds the remaining project Apify budget.')
            return _run(c, dry_run, db_path, log, source, provider)
    except Timeout:
        raise RuntimeError('A scan is already running for this database; wait for it to finish.') from None


def _run(c, dry_run, db_path, log, source, provider):
    store = Store(db_path)
    stats = dict(items_scraped=0, items_scored=0, strong_leads=0, apify_cost=0.0, llm_cost=0.0,
                 state='running', errors=[], apify_run_ids=[])
    tasks = [('search', q, q) for q in c['queries']]
    tasks += [('subreddit', f'https://www.reddit.com/r/{s.removeprefix("r/")}/new/', f'r/{s.removeprefix("r/")}/new') for s in c['subreddits']]
    run_id = store.begin_run([t[2] for t in tasks] if not dry_run else ['demo'], 'demo' if dry_run else 'live')
    seen = set()

    def checkpoint():
        stats['llm_cost'] = getattr(provider, 'cost', 0.0)
        stats['apify_cost'] = getattr(source, 'cost', 0.0)
        stats['apify_run_ids'] = getattr(source, 'run_ids', [])
        store.finish_run(run_id, stats)

    def accept(item):
        if not item:
            return
        if store.exists(item['url']):
            store.attribute(item['url'], item['query'])
            return
        if item['url'] in seen:
            return
        if stats['items_scored'] >= c['llm']['max_items_per_scan']:
            raise BudgetExceeded('Scoring item limit reached; remaining items deferred.')
        result = store.cached_score(item['url'], 'bulk')
        if result is None:
            result = scorer.score(item)
            store.cache_score(item['url'], 'bulk', result)
        if c['llm']['second_pass'] and result.score >= 4 and not dry_run:
            refined = store.cached_score(item['url'], 'strong')
            if refined is None:
                refined = scorer.score(item, strong=True)
                store.cache_score(item['url'], 'strong', refined)
            result = refined
        store.save(item, result)
        seen.add(item['url'])
        stats['items_scored'] += 1
        stats['strong_leads'] += int(result.score >= 4)
        checkpoint()

    try:
        examples = json.loads(local_path(c, 'few_shot_examples').read_text())
        if dry_run:
            samples = json.loads(local_path(c, 'sample_file').read_text())
            provider = provider or DemoProvider({i['body']: i['demo_score'] for i in samples if 'demo_score' in i})
            scorer = Scorer(c, examples, provider)
            parents = {}
            for raw in samples:
                stats['items_scraped'] += 1
                raw = dict(raw, createdAt=(datetime.now(timezone.utc) - timedelta(hours=raw.get('age_hours', 0))).isoformat())
                parent = parents.get(raw.get('parent_demo_id'))
                item = map_item(raw, 'demo', parent)
                if item and raw.get('dataType') == 'post':
                    parents[raw['id']] = item
                accept(item)
        else:
            from .apify_source import ApifySource
            source = source or ApifySource(c, os.environ['APIFY_TOKEN'], log)
            provider = provider or Provider(c)
            scorer = Scorer(c, examples, provider)
            source.on_progress = checkpoint
            source.fetch_schema()
            # Validate every run shape against current schema BEFORE the first charge.
            from .apify_source import build_input
            for kind, value, _ in tasks:
                build_input(source.schema, kind, value, c)
            build_input(source.schema, 'thread', 'https://www.reddit.com/r/test/comments/abc/_/', c)
            posts = {}
            cutoff = datetime.now(timezone.utc) - timedelta(days=c['time_window_days'])
            for kind, value, query in tasks:
                try:
                    raw_items = source.scrape(kind, value)
                    stats['items_scraped'] += len(raw_items)
                    mapped = 0
                    for raw in raw_items:
                        post = map_item(raw, query)
                        if not post:
                            continue
                        mapped += 1
                        if parse_date(post['created_at']) < cutoff or not relevant(post, c):
                            continue
                        if post['url'] in posts:
                            posts[post['url']][1].add(query)
                        else:
                            posts[post['url']] = (post, {query})
                        accept(post)
                        store.attribute(post['url'], query)
                    if raw_items and not mapped:
                        raise ValueError('No usable posts mapped. Inspect printed output field names and lead_monitor/mapping.py.')
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    stats['errors'].append(f'{query}: {safe_error(exc)}')
                    log(stats['errors'][-1])
                finally:
                    checkpoint()
            # Revisit already-scored posts to discover NEW comments, while scoring URLs only once.
            for post, queries in posts.values():
                try:
                    raw_items = source.scrape('thread', post['url'])
                    stats['items_scraped'] += len(raw_items)
                    for raw in raw_items:
                        comment = map_item(raw, post['query'], post)
                        if not comment or not comment['parent_url'] or parse_date(comment['created_at']) < cutoff:
                            continue
                        accept(comment)
                        for query in queries:
                            store.attribute(comment['url'], query)
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    stats['errors'].append(f'{post["url"]}: {safe_error(exc)}')
                    log(stats['errors'][-1])
                finally:
                    checkpoint()
        stats['state'] = 'partial' if stats['errors'] else 'completed'
    except BudgetExceeded as exc:
        stats['state'] = 'limited'
        stats['errors'].append(safe_error(exc))
        log(stats['errors'][-1])
    except Exception as exc:
        stats['state'] = 'failed'
        stats['errors'].append(safe_error(exc))
        log(stats['errors'][-1])
    finally:
        checkpoint()
        store.close()
    log(f'{stats["state"]}: {stats["items_scraped"]} scraped, {stats["items_scored"]} scored, {stats["strong_leads"]} strong leads. '
        f'Estimated costs: Apify ${stats["apify_cost"]:.4f}; LLM ${stats["llm_cost"]:.4f}.')
    return stats
