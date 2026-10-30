import re
from datetime import datetime, timezone
from urllib.parse import urlparse


def canonical_url(value):
    if not isinstance(value, str):
        return None
    if value.startswith('/'):
        value = 'https://www.reddit.com' + value
    p = urlparse(value)
    if p.scheme not in ('http', 'https') or p.hostname not in ('reddit.com', 'www.reddit.com', 'old.reddit.com', 'new.reddit.com'):
        return None
    # Drop title slug, tracking, fragment and host variants. Preserve comment ID.
    m = re.fullmatch(r'/r/([^/]+)/comments/([a-z0-9]+)(?:/([^/]*))?(?:/([a-z0-9]+))?/?', p.path, re.I)
    if not m:
        return None
    sub, post, _, comment = m.groups()
    return f'https://www.reddit.com/r/{sub.lower()}/comments/{post.lower()}/_/' + (f'{comment.lower()}/' if comment else '')


def parse_date(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def map_item(raw, query, parent=None):
    if not isinstance(raw, dict) or raw.get('dataType') not in (None, 'post', 'comment'):
        return None
    url = canonical_url(raw.get('url') or raw.get('permalink'))
    if not url:
        return None
    text = raw.get('body') or raw.get('selftext') or raw.get('text') or ''
    title = raw.get('title') or ''
    if text.strip() in ('[deleted]', '[removed]') or not (text.strip() or title.strip()):
        return None
    try:
        created = parse_date(raw.get('createdAt', raw.get('created_utc')))
    except (ValueError, TypeError, OverflowError):
        return None
    is_comment = raw.get('dataType') == 'comment' or len(url.rstrip('/').split('/')) == 9
    if is_comment and not parent:
        return None  # Never score a contextless comment.
    if is_comment and not url.startswith(parent['url']):
        return None
    username = raw.get('username', raw.get('author', ''))
    if not isinstance(username, str):
        username = ''
    return dict(url=url, parent_url=parent['url'] if is_comment else None,
                username=username, subreddit=(raw.get('parsedCommunityName') or raw.get('communityName') or raw.get('subreddit') or url.split('/')[4]).removeprefix('r/'),
                query=query, text=text or title, parent_title=parent['parent_title'] if is_comment else title,
                parent_body=parent['text'] if is_comment else '', created_at=created.isoformat(),
                parent_created_at=parent['created_at'] if is_comment else None)
