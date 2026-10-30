import json
import sqlite3
from datetime import datetime, timezone
from .models import STATUSES, Score


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS leads (
          url TEXT PRIMARY KEY, parent_url TEXT, username TEXT, subreddit TEXT, query TEXT,
          text TEXT, parent_title TEXT, parent_body TEXT, created_at TEXT, found_at TEXT,
          score INTEGER, intent_type TEXT, competitor TEXT, pain_point TEXT, urgency TEXT,
          reason TEXT, reply_angle TEXT, status TEXT DEFAULT 'new', notes TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS score_cache (
          url TEXT, pass TEXT, result TEXT, PRIMARY KEY(url, pass));
        CREATE TABLE IF NOT EXISTS attribution (
          url TEXT, query TEXT, PRIMARY KEY(url, query));
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, time TEXT, queries TEXT, items_scraped INTEGER DEFAULT 0,
          items_scored INTEGER DEFAULT 0, strong_leads INTEGER DEFAULT 0,
          apify_cost REAL DEFAULT 0, llm_cost REAL DEFAULT 0, state TEXT, errors TEXT,
          mode TEXT, apify_run_ids TEXT);
        CREATE INDEX IF NOT EXISTS leads_score ON leads(score, status);
        ''')

    def close(self):
        self.db.close()

    def exists(self, url):
        return self.db.execute('SELECT 1 FROM leads WHERE url=? AND score IS NOT NULL', (url,)).fetchone() is not None

    def attribute(self, url, query):
        self.db.execute('INSERT OR IGNORE INTO attribution VALUES (?,?)', (url, query))
        self.db.commit()

    def save(self, item, result):
        data = {k: v for k, v in item.items() if k != 'parent_created_at'}
        data.update(result.model_dump())
        data['competitor'] = data.pop('competitor_mentioned')
        data['found_at'] = utcnow()
        keys = list(data)
        self.db.execute(f'INSERT INTO leads ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)}) ON CONFLICT(url) DO UPDATE SET ' + ','.join(f'{k}=excluded.{k}' for k in keys if k not in ('url', 'found_at', 'query')), list(data.values()))
        self.db.commit()
        self.attribute(item['url'], item['query'])

    def save_unscored(self, item):
        data = {k: v for k, v in item.items() if k != 'parent_created_at'}
        data['found_at'] = utcnow()
        data['reason'] = 'Found by a live Reddit search. Not AI-qualified; review the original thread.'
        data['reply_angle'] = 'Read the thread, confirm relevance, and disclose your affiliation if replying.'
        keys = list(data)
        cursor = self.db.execute(f'INSERT OR IGNORE INTO leads ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})', list(data.values()))
        self.db.commit()
        self.attribute(item['url'], item['query'])
        return cursor.rowcount

    def cached_score(self, url, pass_name):
        row = self.db.execute('SELECT result FROM score_cache WHERE url=? AND pass=?', (url, pass_name)).fetchone()
        return Score.model_validate_json(row[0]) if row else None

    def cache_score(self, url, pass_name, result):
        self.db.execute('INSERT OR IGNORE INTO score_cache VALUES (?,?,?)', (url, pass_name, result.model_dump_json()))
        self.db.commit()

    def update(self, url, status, notes):
        if status not in STATUSES:
            raise ValueError('Invalid status')
        self.db.execute('UPDATE leads SET status=?,notes=? WHERE url=?', (status, str(notes), url))
        self.db.commit()

    def begin_run(self, queries, mode):
        cursor = self.db.execute('INSERT INTO runs(time,queries,state,mode) VALUES (?,?,?,?)',
                                 (utcnow(), json.dumps(queries), 'running', mode))
        self.db.commit()
        return cursor.lastrowid

    def finish_run(self, run_id, stats):
        keys = ('items_scraped', 'items_scored', 'strong_leads', 'apify_cost', 'llm_cost', 'state', 'errors', 'apify_run_ids')
        values = [json.dumps(stats[k]) if isinstance(stats[k], list) else stats[k] for k in keys]
        self.db.execute('UPDATE runs SET ' + ','.join(f'{k}=?' for k in keys) + ' WHERE id=?', values + [run_id])
        self.db.commit()
