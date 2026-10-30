#!/usr/bin/env python3
import argparse
import os
import sys
from lead_monitor.config import load_config
from lead_monitor.pipeline import run_pipeline, safe_error


def main():
    p = argparse.ArgumentParser(description='Public Reddit lead monitor. Dry-run is offline; live requires config opt-in.')
    p.add_argument('--dry-run', action='store_true', help='Use sample data and fixture scores; no API calls.')
    p.add_argument('--config', help='Path to config.yaml')
    p.add_argument('--check-apify', action='store_true', help='Read-only token and actor schema verification; no paid run.')
    p.add_argument('--max-items', type=int, help='Apify item limit per run (1–500)')
    args = p.parse_args()
    try:
        config = load_config(args.config)
        if args.check_apify:
            from lead_monitor.apify_source import ApifySource, build_input
            token = os.getenv('APIFY_TOKEN')
            if not token:
                raise ValueError('APIFY_TOKEN is missing from .env or apify.env.')
            source = ApifySource(config, token)
            if not source.client.user().get():
                raise ValueError('Apify account authentication failed.')
            schema = source.fetch_schema()
            for kind, value in [('search', 'automation'), ('subreddit', 'https://www.reddit.com/r/test/new/'), ('thread', 'https://www.reddit.com/r/test/comments/abc/_/')]:
                build_input(schema, kind, value, config)
            print('Apify token and all input mappings verified. No actor runs started; no LLM calls made.')
            return 0
        result = run_pipeline(config, dry_run=args.dry_run, max_items=args.max_items)
        return 0 if result['state'] == 'completed' else 1
    except Exception as exc:
        print('Scan could not start: ' + safe_error(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
