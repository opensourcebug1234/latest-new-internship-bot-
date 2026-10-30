import copy
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, create_autospec
import pytest
from apify_client import ApifyClient
from lead_monitor.config import load_config, live_errors
from lead_monitor.mapping import canonical_url, map_item
from lead_monitor.models import Score
from lead_monitor.scoring import Scorer, build_prompt, parse_score, Provider, BudgetExceeded
from lead_monitor.storage import Store
from lead_monitor.pipeline import run_pipeline
from lead_monitor.apify_source import build_input, ApifySource

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path):
    c = load_config()
    c['database'] = str(tmp_path / 'live.db')
    c['demo_database'] = str(tmp_path / 'demo.db')
    return c


@pytest.fixture
def examples():
    return json.loads((ROOT / 'data/few_shots.json').read_text())


@pytest.fixture
def raw():
    return dict(url='https://www.reddit.com/r/test/comments/abc/title/', username='reader',
                communityName='r/test', title='Need AI agent automation', body='Looking for a tool now.',
                createdAt=datetime.now(timezone.utc).isoformat(), dataType='post')


def test_canonical_dedupe():
    a = canonical_url('https://old.reddit.com/r/Test/comments/abc/title/?utm_source=x')
    assert a == canonical_url('/r/test/comments/abc/another-title/')
    assert a != canonical_url('/r/test/comments/abc/title/def/')
    assert canonical_url('https://reddit.com.evil.com/r/test/comments/abc/title/') is None
    assert canonical_url('https://www.reddit.com/user/person') is None


def test_mapping_context_and_fields(raw):
    post = map_item(raw, 'query')
    assert post['subreddit'] == 'test'
    comment = dict(raw, dataType='comment', url=raw['url']+'def/', body='same here', title='')
    assert map_item(comment, 'query') is None
    mapped = map_item(comment, 'query', post)
    assert mapped['parent_title'] == raw['title']
    assert mapped['parent_body'] == raw['body']
    assert mapped['parent_url'] == post['url']
    assert mapped['text'] == 'same here'
    assert map_item(dict(comment,url='/r/test/comments/other/title/def/'), 'q', post) is None


@pytest.mark.parametrize('item', [{}, {'body':'[deleted]'}, {'dataType':'user'}, {'url':'https://evil.com'}, {'body':''}])
def test_skips_placeholders(item):
    assert map_item(item, 'query') is None


def test_json_validation_and_fences(examples):
    valid = examples[0]['output']
    assert parse_score('```json\n'+json.dumps(valid)+'\n```').score == 5
    for changed in [dict(valid, score=6), dict(valid,score='5'), dict(valid,extra='x'), dict(valid,intent_type='buyer')]:
        with pytest.raises(ValueError):
            parse_score(json.dumps(changed))


def test_json_retry_once(config, examples, raw):
    provider = MagicMock(side_effect=['broken JSON', examples[0]['output']])
    scorer = Scorer(config, examples, provider)
    assert scorer.score(map_item(raw, 'q')).score == 5
    assert provider.call_count == 2
    assert 'previous output failed' in provider.call_args.args[1]
    provider = MagicMock(return_value='{}')
    with pytest.raises(ValueError, match='twice'):
        Scorer(config, examples, provider).score(map_item(raw, 'q'))
    assert provider.call_count == 2


def test_prompt_includes_company_and_parent(config, examples, raw):
    post = map_item(raw, 'q')
    comment = map_item(dict(raw, url=raw['url']+'def/', dataType='comment', body='same here'), 'q', post)
    system, user = build_prompt(comment, config, examples)
    payload = json.loads(user)
    assert payload['item']['parent_title'] == raw['title']
    assert payload['item']['parent_body'] == raw['body']
    assert payload['company']['name'] == 'p95.ai'
    assert 'untrusted' in system and 'spam' in system
    assert len(payload['examples']) == 8


def test_stale_and_bot_penalties(config, examples, raw):
    raw['createdAt'] = (datetime.now(timezone.utc)-timedelta(days=50)).isoformat()
    s = Scorer(config, examples, MagicMock(return_value=examples[0]['output']))
    assert s.score(map_item(raw, 'q')).score == 2
    raw.update(createdAt=datetime.now(timezone.utc).isoformat(), username='AutoModeratorBot')
    assert s.score(map_item(raw, 'q')).score == 2


def test_dry_run_dedupe_and_edit_persistence(config):
    first = run_pipeline(config, dry_run=True)
    assert first['items_scored'] == 8 and first['strong_leads'] == 3
    store = Store(config['demo_database'])
    url = store.db.execute('SELECT url FROM leads LIMIT 1').fetchone()[0]
    store.update(url, 'replied', 'Asked about their workflow')
    with pytest.raises(ValueError):
        store.update(url, 'bad-status', '')
    store.close()
    second = run_pipeline(config, dry_run=True)
    assert second['items_scored'] == 0
    store = Store(config['demo_database'])
    assert store.db.execute('SELECT count(*) FROM leads').fetchone()[0] == 8
    assert tuple(store.db.execute('SELECT status,notes FROM leads WHERE url=?',(url,)).fetchone()) == ('replied','Asked about their workflow')
    store.close()


def test_actor_real_schema(config):
    schema = json.loads((ROOT / 'reference/apify-input-schema.json').read_text())
    search = build_input(schema, 'search', 'automation', config)
    assert search['searches'] == ['automation'] and search['sort'] == 'new'
    assert search['skipComments'] and search['maxItems'] == 50
    thread = build_input(schema, 'thread', 'https://www.reddit.com/r/test/comments/abc/title/', config)
    assert not thread['skipComments'] and thread['maxComments'] == 50
    del schema['properties']['searches']
    with pytest.raises(ValueError, match='schema changed'):
        build_input(schema, 'search', 'q', config)


def test_mock_apify_sdk_contract(config, raw):
    real = ApifyClient('fake')
    client = MagicMock()
    actor = create_autospec(real.actor('trudax/reddit-scraper-lite'), instance=True)
    run = create_autospec(real.run('run1'), instance=True)
    dataset = create_autospec(real.dataset('ds1'), instance=True)
    client.actor.return_value = actor
    client.run.return_value = run
    client.dataset.return_value = dataset
    actor.start.return_value = {'id':'run1'}
    run.wait_for_finish.return_value = {'id':'run1','status':'SUCCEEDED','defaultDatasetId':'ds1','usageTotalUsd':0.01}
    dataset.iterate_items.return_value = iter([raw])
    messages = []
    source = ApifySource(config, 'fake', messages.append, client)
    source.schema = json.loads((ROOT / 'reference/apify-input-schema.json').read_text())
    source.build = '5.8.7'
    assert source.scrape('search','q') == [raw]
    assert source.cost == pytest.approx(0.01)
    assert actor.start.call_args.kwargs['max_items'] == 50
    assert 'max_total_charge_usd' in actor.start.call_args.kwargs
    assert any('First post result fields' in m for m in messages)
    assert source.run_ids == ['run1']


def test_live_blocked_before_network(config):
    source = MagicMock()
    with pytest.raises(ValueError, match='Live scans are disabled'):
        run_pipeline(config, source=source)
    source.scrape.assert_not_called()


def live_config(c, monkeypatch):
    c['safety']['live_scans_enabled'] = True
    c['company']['ideal_customer'] = 'Test customer'
    c['queries'] = ['automation', 'AI agent']
    c['subreddits'] = ['test']
    c['llm']['prices']['gpt-4.1-mini'] = {'input':0.4,'output':1.6}
    monkeypatch.setenv('APIFY_TOKEN','test')
    monkeypatch.setenv('OPENAI_API_KEY','test')
    monkeypatch.setenv('LLM_PROVIDER','openai')
    monkeypatch.delenv('LLM_MODEL',raising=False)


def test_two_stage_attribution_and_cached_comments(config, examples, raw, monkeypatch):
    live_config(config, monkeypatch)
    source = MagicMock()
    source.cost = 0.05
    source.run_ids = ['r1']
    source.schema = json.loads((ROOT / 'reference/apify-input-schema.json').read_text())
    comment = dict(raw, dataType='comment',url=raw['url']+'def/',body='same here')
    source.scrape.side_effect = lambda kind, value: [comment] if kind=='thread' else [raw]
    provider = MagicMock(return_value=examples[0]['output'])
    provider.cost = 0.01
    first = run_pipeline(config, source=source, provider=provider)
    assert first['items_scored'] == 2
    assert source.scrape.call_count == 4  # Two keyword runs, subreddit run, one thread run.
    store = Store(config['database'])
    assert store.db.execute('SELECT count(*) FROM attribution').fetchone()[0] == 6
    store.close()
    run_pipeline(config, source=source, provider=provider)
    assert provider.call_count == 2  # Cached post AND comment never re-scored.
    assert source.scrape.call_count == 8  # Still discovers new comments in existing threads.


def test_llm_budget_no_request(config, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fake')
    config['llm']['max_estimated_cost_usd'] = 0.00000001
    config['llm']['prices']['gpt-4.1-mini'] = {'input':1,'output':1}
    p = Provider(config)
    p.client = MagicMock()
    with pytest.raises(BudgetExceeded):
        p('system','user','gpt-4.1-mini')
    p.client.chat.completions.create.assert_not_called()


def test_live_dashboard_does_not_scan_without_approval(config, monkeypatch):
    import lead_monitor.config as config_module
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(config_module, 'load_config', lambda: config)
    app = AppTest.from_file(str(ROOT / 'dashboard.py')).run(timeout=30)
    assert not app.exception and not app.error
    assert any(m.label == 'Real items' for m in app.metric)
    assert next(b for b in app.button if b.label == 'Run real scan now').disabled
    assert not app.toggle
    assert any(t.label == 'Settings' for t in app.tabs)


def test_bulk_cache_survives_refinement_failure(config, examples, raw, monkeypatch):
    live_config(config, monkeypatch)
    config['queries'] = ['automation']
    config['subreddits'] = []
    config['llm']['second_pass'] = True
    config['llm']['prices']['gpt-4.1'] = {'input': 2, 'output': 8}
    source = MagicMock(cost=0.02, run_ids=[])
    source.schema = json.loads((ROOT / 'reference/apify-input-schema.json').read_text())
    source.scrape.side_effect = lambda kind, value: [] if kind == 'thread' else [raw]
    provider = MagicMock(side_effect=[examples[0]['output'], RuntimeError('Temporary provider failure')])
    provider.cost = 0.001
    first = run_pipeline(config, source=source, provider=provider)
    assert first['state'] == 'partial'
    assert provider.call_count == 2
    provider.side_effect = None
    provider.return_value = examples[2]['output']
    second = run_pipeline(config, source=source, provider=provider)
    assert second['items_scored'] == 1
    assert provider.call_count == 3  # One refinement retry; no repeated bulk request.
    assert provider.call_args.args[2] == 'gpt-4.1'


def test_ambiguous_actor_start_does_not_retry(config):
    client = MagicMock()
    client.actor.return_value.start.side_effect = RuntimeError('transport error')
    source = ApifySource(config, 'fake', client=client)
    source.schema = json.loads((ROOT / 'reference/apify-input-schema.json').read_text())
    source.build = '5.8.7'
    with pytest.raises(RuntimeError, match='may have started'):
        source.scrape('search', 'q')
    assert client.actor.return_value.start.call_count == 1
    assert source.cost == config['apify']['max_total_charge_usd']


def test_both_provider_adapters(config, examples, monkeypatch):
    from types import SimpleNamespace as N
    monkeypatch.setenv('OPENAI_API_KEY', 'fake')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fake')
    for name, model in config['llm']['models'].items():
        config['llm']['provider'] = name
        config['llm']['prices'][model] = {'input':1, 'output':2}
        p = Provider(config)
        p.client = MagicMock()
        if name == 'openai':
            p.client.chat.completions.create.return_value = N(choices=[N(message=N(content=json.dumps(examples[0]['output'])))], usage=N(prompt_tokens=100, completion_tokens=50))
        else:
            p.client.messages.create.return_value = N(content=[N(type='tool_use',input=examples[0]['output'])],usage=N(input_tokens=100,output_tokens=50))
        assert parse_score(p('system','user',model)).score == 5
        assert p.cost == pytest.approx(0.0002)


def test_empty_live_dashboard_and_filters(config, monkeypatch):
    import lead_monitor.config as config_module
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(config_module, 'load_config', lambda: config)
    app = AppTest.from_file(str(ROOT / 'dashboard.py')).run(timeout=30)
    app.slider[0].set_value(5).run()
    assert not app.error and not app.exception
    next(x for x in app.text_input if x.label == 'Text search').set_value('no-such-phrase-839302').run()
    assert not app.error and not app.exception
    assert next(b for b in app.button if b.label=='Run real scan now').disabled


def test_dashboard_save_edits(config, monkeypatch):
    # Use an isolated DB and patch config import so the UI test doesn't alter the user's inbox.
    import lead_monitor.config as config_module
    from streamlit.testing.v1 import AppTest
    run_pipeline(config, dry_run=True)
    config['database'] = config['demo_database']  # Isolated fixture data for UI edits.
    monkeypatch.setattr(config_module, 'load_config', lambda: config)
    app = AppTest.from_file(str(ROOT / 'dashboard.py')).run(timeout=30)
    app.session_state['live_lead_editor'] = {'edited_rows':{0:{'status':'replied','notes':'Test follow-up'}}, 'added_rows':[], 'deleted_rows':[]}
    next(b for b in app.button if b.label=='Save status and notes').click().run(timeout=30)
    assert not app.error and not app.exception
    store = Store(config['demo_database'])
    row = store.db.execute("SELECT status FROM leads WHERE notes='Test follow-up'").fetchone()
    store.close()
    assert row and row[0] == 'replied'


def test_unscored_storage_and_later_scoring(config, raw, examples):
    store=Store(config['database'])
    item=map_item(raw,'q')
    assert store.save_unscored(item)==1
    assert store.save_unscored(item)==0
    assert not store.exists(item['url'])  # exists means already scored, so pending items can be scored.
    store.update(item['url'],'replied','Keep my note')
    store.save(item,Score.model_validate(examples[0]['output']))
    assert store.exists(item['url'])
    row=store.db.execute('SELECT score,status,notes FROM leads').fetchone()
    assert tuple(row)==(5,'replied','Keep my note')
    store.close()


def test_sdk_model_normalization():
    from lead_monitor.apify_source import as_dict
    from pydantic import BaseModel, Field
    from decimal import Decimal
    class RunResponse(BaseModel):
        id: str
        usage_total_usd: Decimal = Field(alias='usageTotalUsd')
    response=RunResponse(id='real',usageTotalUsd=Decimal('0.08'))
    assert as_dict(response)['usageTotalUsd']==Decimal('0.08')


def test_apify_only_collection_and_budget(config, raw, monkeypatch):
    from lead_monitor.collection import collect, remaining_budget
    config['queries']=['AI agent']
    config['subreddits']=[]
    source=MagicMock(cost=0.08,run_ids=['mock1'],started=1)
    source.schema=json.loads((ROOT/'reference/apify-input-schema.json').read_text())
    comment=dict(raw,dataType='comment',url=raw['url']+'def/',body='I also need this.')
    source.scrape.side_effect=lambda kind,value:[comment] if kind=='thread' else [raw]
    result=collect(config,source=source)
    assert result['items_saved']==2 and result['items_scored']==0
    assert result['llm_cost']==0
    assert remaining_budget(config)==pytest.approx(4.92)
    store=Store(config['database'])
    assert store.db.execute('SELECT count(*) FROM leads WHERE score IS NULL').fetchone()[0]==2
    store.close()
    config['collection']['total_budget_usd']=0.1
    with pytest.raises(ValueError,match='remaining project'):
        collect(config,source=source)
    assert source.scrape.call_count==2
