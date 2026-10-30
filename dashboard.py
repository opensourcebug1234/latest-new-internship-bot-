import copy
import json
import os
import html
import math
import hmac
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import streamlit as st
from lead_monitor.config import load_config, local_path, live_errors
from lead_monitor.models import STATUSES, INTENTS, Score
from lead_monitor.pipeline import run_pipeline, safe_error
from lead_monitor.collection import collect, remaining_budget
from lead_monitor.settings import save_config, save_keys
from lead_monitor.storage import Store

st.set_page_config(page_title='p95.ai · Lead intelligence', page_icon='🔎', layout='wide')
access_password = os.environ.get('P95_DASHBOARD_PASSWORD')
if access_password and not st.session_state.get('dashboard_authenticated', False):
    st.title('p95.ai · Sign in')
    with st.form('dashboard_login'):
        supplied_password = st.text_input('Password', type='password')
        submitted = st.form_submit_button('Sign in')
    if submitted:
        if hmac.compare_digest(supplied_password.encode(), access_password.encode()):
            st.session_state['dashboard_authenticated'] = True
            st.rerun()
        else:
            st.error('Incorrect password')
    st.stop()
st.markdown((Path(__file__).parent / 'assets/dashboard.css').read_text(), unsafe_allow_html=True)


def csv_export(frame):
    safe = frame.copy()
    for column in safe.columns:
        safe[column] = safe[column].map(lambda x: "'" + x if isinstance(x, str) and x.lstrip().startswith(('=', '+', '-', '@')) else x)
    return safe.to_csv(index=False).encode('utf-8')


def lines(text):
    return list(dict.fromkeys(x.strip() for x in text.splitlines() if x.strip()))


def settings_ui(c):
    st.subheader('Search and product settings')
    st.caption('Everything needed for the next scan can be changed here. Saving settings does not start a scan.')
    with st.form('search_settings'):
        name = st.text_input('Company name', value=c['company']['name'])
        description = st.text_area('What your product does', value=c['company']['description'])
        ideal = st.text_input('Ideal customer', value=c['company']['ideal_customer'])
        competitors = st.text_area('Competitors — one per line', value='\n'.join(c['company']['competitors']))
        pain = st.text_area('Buyer pain phrases — one per line', value='\n'.join(c['company']['pain_phrases']))
        queries = st.text_area('Reddit searches — one per line', value='\n'.join(c['queries']))
        subs = st.text_area('Subreddits — one per line', value='\n'.join(c['subreddits']))
        terms = st.text_area('Relevance phrases — one per line', value='\n'.join(c['relevance_terms']))
        days = st.number_input('Look back this many days', 1, 365, int(c['time_window_days']))
        a,b = st.columns(2)
        with a:
            total = st.number_input('Total project Apify budget ($)', 0.04, 5.0, float(c['collection']['total_budget_usd']), step=0.1)
            runs = st.number_input('Maximum actor runs per scan', 1, 10, int(c['collection']['max_runs']))
        with b:
            cap = st.number_input('Maximum charge per actor run ($)', 0.04, 5.0, float(c['collection']['per_run_budget_usd']), step=0.01)
            items = st.number_input('Maximum results per actor run', 1, 100, int(c['collection']['max_items']))
        st.caption('Each search, subreddit page, or comment thread uses one actor run. One run is reserved for comments when possible. Earlier searches take priority when the run limit is small.')
        if st.form_submit_button('Save search and budget settings'):
            if not queries.strip() and not subs.strip():
                st.error('Add at least one search or subreddit.')
            elif runs * cap > total:
                st.error('The scan ceiling exceeds the project budget. Reduce runs or the per-run charge.')
            else:
                c['company'].update(name=name, description=description, ideal_customer=ideal, competitors=lines(competitors), pain_phrases=lines(pain))
                c.update(queries=lines(queries), subreddits=[s.removeprefix('r/') for s in lines(subs)], relevance_terms=lines(terms), time_window_days=int(days))
                c['collection'].update(total_budget_usd=total,max_runs=int(runs),per_run_budget_usd=cap,max_items=int(items))
                save_config(c)
                st.success('Saved. The next scan will use these settings.')
                st.rerun()
    st.subheader('API connections')
    st.caption('Your existing saved keys are never displayed. Leave a field blank to keep its current key.')
    for key, label in [('APIFY_TOKEN','Apify'), ('OPENAI_API_KEY','OpenAI'), ('ANTHROPIC_API_KEY','Anthropic')]:
        st.write(f'{label}: {"key configured" if os.getenv(key) else "not configured"}')
    with st.form('api_keys', clear_on_submit=True):
        apify = st.text_input('New Apify token', type='password')
        openai = st.text_input('New OpenAI API key (optional)', type='password')
        anthropic = st.text_input('New Anthropic API key (optional)', type='password')
        st.caption('Save writes keys to the ignored local .env file. Apify credit cannot pay for OpenAI or Anthropic usage.')
        if st.form_submit_button('Save API keys locally'):
            save_keys(apify, openai, anthropic)
            st.success('Provided keys saved locally. No paid requests were made.')
    if st.button('Check Apify connection — no paid run'):
        from lead_monitor.apify_source import ApifySource
        try:
            if not os.getenv('APIFY_TOKEN'):
                raise ValueError('Save an Apify token first.')
            source = ApifySource(c, os.environ['APIFY_TOKEN'], log=lambda message: None)
            if not source.client.user().get():
                raise ValueError('Authentication failed.')
            source.fetch_schema()
            st.success(f'Connected. Actor build {source.build} schema verified. No scraper run started.')
        except Exception as exc:
            st.error(safe_error(exc))
    with st.expander('Optional AI scoring settings'):
        st.caption('Apify-only scans work without this. Model prices must match your provider’s current rates.')
        with st.form('llm_settings'):
            provider = st.selectbox('Provider', ['openai','anthropic'], index=['openai','anthropic'].index(c['llm']['provider']))
            openai_model = st.text_input('OpenAI model', value=c['llm']['models']['openai'])
            anthropic_model = st.text_input('Anthropic model', value=c['llm']['models']['anthropic'])
            model = c['llm']['models'][c['llm']['provider']]
            prices = c['llm']['prices'].get(model, {})
            inp = st.number_input('Selected provider: input price per million tokens ($)', min_value=0.0, value=float(prices.get('input') or 0), step=0.1)
            out = st.number_input('Selected provider: output price per million tokens ($)', min_value=0.0, value=float(prices.get('output') or 0), step=0.1)
            budget = st.number_input('LLM budget per scan ($), separate from Apify credit', min_value=0.01,max_value=5.0,value=min(float(c['llm']['max_estimated_cost_usd']),5.0), step=0.05)
            limit = st.number_input('Maximum items to score', 1, 200, int(c['llm']['max_items_per_scan']))
            tokens = st.number_input('Maximum output tokens per score', 200, 4000, int(c['llm']['max_output_tokens']))
            text_limit = st.number_input('Maximum text characters per field', 500, 20000, int(c['llm']['max_text_chars']))
            refine = st.checkbox('Second pass for scores 4–5', value=c['llm']['second_pass'])
            strong_model = st.text_input('Second-pass model for selected provider', value=c['llm']['strong_models'][c['llm']['provider']])
            strong_in = st.number_input('Second-pass input price per million tokens ($)',min_value=0.0,value=0.0)
            strong_out = st.number_input('Second-pass output price per million tokens ($)',min_value=0.0,value=0.0)
            if st.form_submit_button('Save AI scoring settings'):
                if inp <= 0 or out <= 0 or (refine and (strong_in <= 0 or strong_out <= 0)):
                    st.error('Enter positive current provider prices before enabling paid scoring.')
                else:
                    c['llm'].update(provider=provider, max_estimated_cost_usd=budget, max_items_per_scan=int(limit), max_output_tokens=int(tokens), max_text_chars=int(text_limit), second_pass=refine)
                    c['llm']['models'].update(openai=openai_model,anthropic=anthropic_model)
                    c['llm']['prices'][c['llm']['models'][provider]]={'input':inp,'output':out}
                    c['llm']['strong_models'][provider]=strong_model
                    if refine:
                        c['llm']['prices'][strong_model]={'input':strong_in,'output':strong_out}
                    os.environ.pop('LLM_MODEL',None)
                    os.environ['LLM_PROVIDER']=provider
                    save_config(c)
                    st.success('AI settings saved. Saving did not call an LLM.')
        examples_path=local_path(c,'few_shot_examples')
        with st.form('examples'):
            examples=st.text_area('Scoring examples (JSON)',value=examples_path.read_text(),height=250)
            if st.form_submit_button('Save scoring examples'):
                try:
                    parsed=json.loads(examples)
                    if not 6 <= len(parsed) <= 10:
                        raise ValueError('Use 6–10 examples.')
                    for example in parsed:
                        Score.model_validate(example['output'])
                        if not isinstance(example['input'],dict):
                            raise ValueError('Each example requires an input object.')
                    examples_path.write_text(json.dumps(parsed,indent=2))
                    st.success('Examples saved.')
                except Exception as exc:
                    st.error(safe_error(exc))


def main():
    c = load_config()
    st.markdown('<div class="hero"><div><div class="eyebrow">p95.ai / Lead intelligence</div>'
                '<h1>Find your next conversation.</h1>'
                '<p>Discover relevant Reddit discussions, understand the context, and keep every follow-up in one place.</p>'
                '</div><div class="hero-tag">● &nbsp; Real Reddit data</div></div>', unsafe_allow_html=True)
    with st.sidebar:
        st.markdown('<div class="brand"><div class="brand-icon">p95</div><div><div class="brand-name">p95.ai</div>'
                    '<div class="brand-sub">Lead workspace</div></div></div>', unsafe_allow_html=True)
        st.header('Collection controls')
        balance=remaining_budget(c)
        ceiling=c['collection']['max_runs']*c['collection']['per_run_budget_usd']
        st.metric('Apify project budget remaining',f'${balance:.2f}')
        st.progress(min(1.0, max(0.0, balance / c['collection']['total_budget_usd'])))
        st.caption('Project budget remaining · excludes other account usage.')
        st.write(f'Next scan: up to {c["collection"]["max_runs"]} runs, {c["collection"]["max_items"]} results/run, ${ceiling:.2f} Apify maximum.')
        scoring=st.checkbox('Also use paid AI scoring',value=False)
        if not scoring:
            st.caption('Apify only. Real results remain unscored for manual review.')
        scan_config=copy.deepcopy(c)
        scan_config['apify'].update(max_runs=c['collection']['max_runs'],max_items=c['collection']['max_items'],max_total_charge_usd=c['collection']['per_run_budget_usd'])
        scan_config['safety']['live_scans_enabled']=True
        errors=live_errors(scan_config) if scoring else ([] if os.getenv('APIFY_TOKEN') else ['Add an Apify token in Settings.'])
        for error in errors:
            st.warning(error)
        if balance + 1e-9 < ceiling:
            st.warning('Reduce scan limits in Settings to stay within your remaining budget.')
        approve=st.checkbox(f'I approve this scan’s maximum ${ceiling:.2f} Apify cost' + (f' + ${c["llm"]["max_estimated_cost_usd"]:.2f} estimated LLM cost' if scoring else ''),value=False)
        clicked=st.button('Run real scan now',type='primary',disabled=bool(errors) or not approve or balance + 1e-9 < ceiling,width='stretch')
        st.caption('Browsing saved leads is free. Only an approved scan uses credit.')
        st.button('Refresh saved leads')
    if clicked:
        with st.status('Collecting real Reddit threads…',expanded=True) as status:
            try:
                if scoring:
                    # Pipeline locks the database; the collection path checks its budget inside its own lock.
                    result=run_pipeline(scan_config,log=st.write)
                else:
                    result=collect(c,log=st.write)
                status.update(label=f'Scan {result["state"]}',state='complete' if result['state']=='completed' else 'error')
                st.session_state['last_scan']=result
            except Exception as exc:
                status.update(label='Scan failed',state='error')
                st.error(safe_error(exc))
    store=Store(local_path(c,'database'))
    try:
        leads=pd.read_sql_query('SELECT * FROM leads ORDER BY score DESC, created_at DESC',store.db)
        leads['score']=pd.to_numeric(leads['score'],errors='coerce')
        runs=pd.read_sql_query('SELECT * FROM runs ORDER BY time DESC',store.db)
        attribution=pd.read_sql_query('SELECT * FROM attribution',store.db)
        cost=runs[['apify_cost','llm_cost']].sum().sum() if not runs.empty else 0.0
        metrics=[len(leads),int(leads['score'].notna().sum()),int((leads['score']>=4).sum()),int((leads['status']=='new').sum()),int((leads['status']=='converted').sum()),f'${cost:.4f}']
        for col,label,value in zip(st.columns(6),['Real items','AI scored','Strong leads','To review','Converted','Project spend'],metrics):
            col.metric(label,value)
        month=datetime.now(timezone.utc).strftime('%Y-%m')
        monthly=runs[runs['time'].str.startswith(month)] if not runs.empty else runs
        monthly_cost=monthly[['apify_cost','llm_cost']].sum().sum() if not monthly.empty else 0
        st.caption(f'Replied: {int((leads["status"]=="replied").sum())} · Cost this month (UTC): ${monthly_cost:.4f} · Unscored: {int(leads["score"].isna().sum())}. Unscored results are candidates, not qualified buyers.')
        with st.sidebar:
            st.divider()
            st.header('Refine your inbox')
            minimum=st.slider('Minimum AI score',1,5,1)
            unscored=st.checkbox('Include unscored results',value=True)
            statuses=st.multiselect('Status',STATUSES)
            subs=st.multiselect('Subreddit',sorted(leads['subreddit'].dropna().unique()))
            intents=st.multiselect('Intent type',INTENTS)
            search=st.text_input('Text search')
            dates=st.date_input('Created date range',value=())
        mask=(leads['score']>=minimum) | (leads['score'].isna() & unscored)
        filtered=leads[mask].copy()
        for field,selected in [('status',statuses),('subreddit',subs),('intent_type',intents)]:
            if selected:
                filtered=filtered[filtered[field].isin(selected)]
        if search and not filtered.empty:
            text=filtered[['text','parent_title','parent_body','username','competitor']].fillna('').agg(' '.join,axis=1)
            filtered=filtered[text.str.contains(search,case=False,regex=False)]
        if isinstance(dates,(tuple,list)) and len(dates)==2:
            days=pd.to_datetime(filtered['created_at'],utc=True).dt.date
            filtered=filtered[(days>=dates[0]) & (days<=dates[1])]
        inbox,insights,history,settings=st.tabs(['Lead inbox','Insights','Scan history','Settings'])
        with inbox:
            heading, sorting = st.columns([2, 1])
            with heading:
                st.subheader('Your review queue')
                st.caption(f"{len(filtered):,} matching results · Review a thread before reaching out.")
            with sorting:
                sort_order = st.selectbox('Sort review queue', ['Highest score', 'Newest first', 'Oldest first'])
            top = filtered[filtered['status']=='new'].copy()
            if sort_order != 'Highest score':
                top = top.sort_values('created_at', ascending=sort_order == 'Oldest first')
            pages = max(1, math.ceil(len(top) / 6))
            page = st.number_input('Review page', min_value=1, max_value=pages, value=1, help='Six threads per page. The full table below includes every matching lead.')
            st.caption(f'Page {page} of {pages} · {len(top)} new threads')
            if top.empty:
                st.info('Your review queue is clear. Adjust filters or collect new threads using the sidebar.')
            visible = top.iloc[(int(page)-1)*6:int(page)*6]
            card_columns = st.columns(2, gap='medium')
            for position, (_, lead) in enumerate(visible.iterrows()):
                with card_columns[position % 2]:
                    with st.container(border=True):
                        scored = pd.notna(lead['score'])
                        badge = f"Score {int(lead['score'])} / 5" if scored else 'Needs review'
                        badge_style = 'good' if scored and lead['score'] >= 4 else 'pending' if not scored else ''
                        subreddit = html.escape(str(lead['subreddit']))
                        title = html.escape(html.unescape(lead['parent_title'] or 'Reddit comment'))
                        date = pd.to_datetime(lead['created_at'], utc=True).strftime('%d %b · %H:%M UTC')
                        st.markdown(f'<div class="lead-meta"><span class="pill {badge_style}">{badge}</span>'
                                    f'<span>r/{subreddit}</span><span>· {date}</span></div>'
                                    f'<div class="lead-title">{title}</div>', unsafe_allow_html=True)
                        body = html.unescape(lead['text'] or '')
                        snippet = body[:360].rsplit(' ', 1)[0] + '…' if len(body) > 360 else body
                        st.markdown(f'<div class="lead-excerpt">{html.escape(snippet)}</div>', unsafe_allow_html=True)
                        with st.expander('Read full thread & context'):
                            st.text(body)
                            if lead['parent_url']:
                                st.markdown('**Parent post**')
                                st.text(html.unescape(lead['parent_title'] or ''))
                                st.text(html.unescape(lead['parent_body'] or ''))
                        with st.expander('Qualification & reply guidance'):
                            st.caption('Search that found this thread')
                            st.text(lead['query'])
                            st.text(lead['reason'] or 'Not AI-scored. Confirm fit before replying.')
                            st.text(lead['reply_angle'] or 'Read the thread before deciding whether to reply.')
                        st.link_button('Open Reddit thread ↗', lead['url'], width='stretch')
                        replied, ignored = st.columns(2)
                        with replied:
                            if st.button('Mark replied', key=f"replied_{lead['url']}", width='stretch'):
                                store.update(lead['url'], 'replied', lead['notes'] or '')
                                st.rerun()
                        with ignored:
                            if st.button('Ignore', key=f"ignore_{lead['url']}", width='stretch'):
                                store.update(lead['url'], 'ignored', lead['notes'] or '')
                                st.rerun()
            st.divider()
            st.subheader('All matching leads')
            st.caption('Update status and notes below, then save your changes. Ignored threads remain available through the status filter.')
            columns=['url','score','status','notes','subreddit','intent_type','competitor','text','created_at']
            editor=st.data_editor(filtered[columns],key='live_lead_editor',hide_index=True,width='stretch',
                disabled=[x for x in columns if x not in ('status','notes')],
                column_config={'status':st.column_config.SelectboxColumn('Status',options=STATUSES,required=True),'url':st.column_config.LinkColumn('Reddit thread')})
            if st.button('Save status and notes',disabled=filtered.empty):
                for _,row in editor.iterrows():
                    store.update(row['url'],row['status'],'' if pd.isna(row['notes']) else row['notes'])
                st.session_state['saved']=True
                st.rerun()
            if st.session_state.pop('saved',False):
                st.success('Status and notes saved.')
            st.download_button('Export filtered leads as CSV',csv_export(filtered),'p95-real-leads.csv','text/csv')
        with insights:
            if filtered.empty:
                st.info('Saved real results will populate these charts.')
            else:
                a,b=st.columns(2)
                with a:
                    st.subheader('Real results per day')
                    chart=filtered.copy()
                    chart['day']=pd.to_datetime(chart['created_at'],utc=True).dt.date
                    st.bar_chart(chart.groupby('day').size().rename('Results'))
                    st.subheader('Strong leads per day')
                    strong=chart[chart['score']>=4]
                    if not strong.empty:
                        st.bar_chart(strong.groupby('day').size())
                    else:
                        st.caption('No AI-qualified strong leads yet.')
                    st.subheader('Competitors mentioned')
                    counts=filtered['competitor'].replace('',pd.NA).dropna().value_counts()
                    if not counts.empty:
                        st.bar_chart(counts)
                    else:
                        st.caption('Competitor classifications require scoring.')
                with b:
                    st.subheader('Top subreddits')
                    st.bar_chart(filtered['subreddit'].value_counts().head(10))
                    st.subheader('Intent types')
                    st.bar_chart(filtered['intent_type'].fillna('unscored').value_counts())
                st.subheader('Query performance')
                matched=attribution.merge(filtered[['url','score']],on='url')
                if not matched.empty:
                    perf=matched.groupby('query').agg(items=('url','nunique'),scored=('score','count'),average_score=('score','mean'),hit_rate=('score',lambda x:(x.dropna()>=4).mean() if x.notna().any() else float('nan')))
                    st.dataframe(perf,width='stretch')
                    st.caption('Average score and hit rate use scored items only. Blank means not scored, not poor quality.')
        with history:
            st.subheader('Collection activity')
            st.caption('Review previous runs, costs, and any results that need attention.')
            st.dataframe(runs,hide_index=True,width='stretch')
            for _,run in runs.iterrows():
                if run['state']!='completed':
                    st.warning(f'{run["time"]}: {run["state"]} — {run["errors"] or "Scan running or interrupted."}')
                for run_id in json.loads(run['apify_run_ids'] or '[]'):
                    st.link_button(f'Apify run {run_id}',f'https://console.apify.com/actors/runs/{run_id}')
        with settings:
            settings_ui(c)
    finally:
        store.close()


try:
    main()
except Exception as exc:
    st.error('Dashboard could not load: '+safe_error(exc))
    st.info('Reload the page. Existing saved leads are retained.')
