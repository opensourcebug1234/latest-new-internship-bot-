"""Apify-only ingestion. Genuine public data; no fabricated LLM classifications."""
import copy
import os
from datetime import datetime, timedelta, timezone
from filelock import FileLock
from .apify_source import ApifySource, build_input
from .config import local_path
from .mapping import map_item, parse_date
from .pipeline import safe_error, relevant
from .storage import Store


def remaining_budget(c):
    store = Store(local_path(c, 'database'))
    try:
        used = store.db.execute('SELECT COALESCE(SUM(apify_cost),0) FROM runs').fetchone()[0]
        return max(0.0, c['collection']['total_budget_usd'] - used)
    finally:
        store.close()


def collect(c, log=print, source=None):
    c = copy.deepcopy(c)
    settings = c['collection']
    if not os.getenv('APIFY_TOKEN') and source is None:
        raise ValueError('Save your Apify token in API settings first.')
    if not c['queries'] and not c['subreddits']:
        raise ValueError('Add at least one search or subreddit in Search settings.')
    n = int(settings['max_runs'])
    cap = float(settings['per_run_budget_usd'])
    if not 1 <= n <= 10 or not 0.04 <= cap <= 5 or not 1 <= settings['max_items'] <= 100:
        raise ValueError('Use 1–10 runs, $0.04–$5 per run, and 1–100 items per run.')
    c['apify'].update(max_runs=n, max_total_charge_usd=cap, max_items=settings['max_items'])
    lock = FileLock(str(local_path(c, 'database')) + '.lock', timeout=0)
    with lock:
        if n * cap > remaining_budget(c) + 1e-9:
            raise ValueError('Scan exceeds the remaining project Apify budget. Reduce runs or per-run cap.')
        store = Store(local_path(c, 'database'))
        stats = dict(items_scraped=0, items_scored=0, strong_leads=0, apify_cost=0.0, llm_cost=0.0,
                     state='running', errors=[], apify_run_ids=[], items_saved=0)
        tasks = [('search', q, q) for q in c['queries']]
        tasks += [('subreddit', f'https://www.reddit.com/r/{s.removeprefix("r/")}/new/', f'r/{s.removeprefix("r/")}/new') for s in c['subreddits']]
        run_id = store.begin_run([x[2] for x in tasks], 'apify_only')
        source = source or ApifySource(c, os.environ['APIFY_TOKEN'], log)
        cutoff = datetime.now(timezone.utc) - timedelta(days=c['time_window_days'])

        def checkpoint():
            stats['apify_cost'] = source.cost
            stats['apify_run_ids'] = source.run_ids
            store.finish_run(run_id, stats)

        def persist(raw_items, query, parent=None):
            mapped = []
            stats['items_scraped'] += len(raw_items)
            for raw in raw_items:
                item = map_item(raw, query, parent)
                if item and parse_date(item['created_at']) >= cutoff:
                    stats['items_saved'] += store.save_unscored(item)
                    mapped.append(item)
            checkpoint()
            if raw_items and not mapped:
                log('Results contained no usable recent items; inspect fields and the date window.')
            return mapped

        source.on_progress = checkpoint
        try:
            source.fetch_schema()
            for kind, value, _ in tasks:
                build_input(source.schema, kind, value, c)
            build_input(source.schema, 'thread', 'https://www.reddit.com/r/test/comments/abc/_/', c)
            # Reserve one run for comments. With two runs, use the first discovery query.
            discovery_limit = max(1, n - 1)
            posts = []
            for kind, value, query in tasks[:discovery_limit]:
                posts.extend(persist(source.scrape(kind, value), query))
            candidates = {p['url']: p for p in posts if not p['parent_url'] and relevant(p, c)}
            # Cheap, transparent prioritization for comment enrichment only, not a lead score.
            buying_phrases = ('looking for', 'recommend', 'alternative', 'struggling', 'help', 'need', '?')
            ordered = sorted(candidates.values(), key=lambda p: sum(t in (p['parent_title']+' '+p['text']).lower() for t in buying_phrases), reverse=True)
            for post in ordered:
                if source.started >= n:
                    break
                persist(source.scrape('thread', post['url']), post['query'], post)
            stats['state'] = 'completed'
        except Exception as exc:
            stats['state'] = 'partial' if stats['items_saved'] else 'failed'
            stats['errors'].append(safe_error(exc))
            log(stats['errors'][-1])
        finally:
            checkpoint()
            store.close()
        log(f'{stats["state"]}: {stats["items_scraped"]} scraped; {stats["items_saved"]} new real items saved, unscored. Apify cost: ${stats["apify_cost"]:.4f}; LLM: $0.0000.')
        return stats


def recover_run(c, local_run_id, log=print):
    """Download already-paid datasets after an interruption; never start an actor."""
    import json
    from apify_client import ApifyClient
    from .apify_source import as_dict
    from .mapping import canonical_url
    with FileLock(str(local_path(c,'database'))+'.lock', timeout=0):
        store=Store(local_path(c,'database'))
        try:
            row=store.db.execute('SELECT * FROM runs WHERE id=?',(int(local_run_id),)).fetchone()
            if not row:
                raise ValueError('Scan not found.')
            ids=json.loads(row['apify_run_ids'] or '[]')
            if not ids:
                raise ValueError('This scan has no recorded Apify run IDs.')
            client=ApifyClient(os.environ['APIFY_TOKEN'])
            total=0.0
            saved=0
            count=0
            statuses=[]
            for actor_run_id in ids:
                run=as_dict(client.run(actor_run_id).get())
                statuses.append(str(run['status']))
                cost=run.get('usageTotalUsd')
                total+=float(cost) if cost is not None else c['collection']['per_run_budget_usd']
                record=as_dict(client.key_value_store(run['defaultKeyValueStoreId']).get_record('INPUT'))
                inputs=record.get('value',{})
                query=(inputs.get('searches') or [None])[0]
                start=(inputs.get('startUrls') or [{}])[0].get('url','')
                parent=None
                if not query and canonical_url(start):
                    existing=store.db.execute('SELECT * FROM leads WHERE url=?',(canonical_url(start),)).fetchone()
                    parent=dict(existing) if existing else None
                    query=parent['query'] if parent else start
                query=query or start or json.loads(row['queries'])[0]
                raw_items=list(client.dataset(run['defaultDatasetId']).iterate_items(limit=c['collection']['max_items']))
                count+=len(raw_items)
                # Capture parent post first if this is a thread dataset.
                for raw in raw_items:
                    mapped=map_item(raw,query)
                    if mapped and not mapped['parent_url']:
                        saved+=store.save_unscored(mapped)
                        if canonical_url(start)==mapped['url']:
                            parent=mapped
                for raw in raw_items:
                    if raw.get('dataType')=='comment' and parent:
                        mapped=map_item(raw,query,parent)
                        if mapped:
                            saved+=store.save_unscored(mapped)
                if raw_items:
                    log('Actual result fields: '+', '.join(sorted(raw_items[0])))
            state='completed' if all(s=='SUCCEEDED' for s in statuses) else 'partial'
            errors=[] if state=='completed' else ['Recovered available results from an incomplete Apify run. No new scraper run started.']
            store.db.execute('UPDATE runs SET state=?,errors=?,items_scraped=?,apify_cost=? WHERE id=?',(state,json.dumps(errors),count,total,int(local_run_id)))
            store.db.commit()
            log(f'Recovered {saved} new real items from already-paid datasets. Total scan cost: ${total:.4f}.')
            return {'items_saved':saved,'items_scraped':count,'apify_cost':total,'state':state}
        finally:
            store.close()
