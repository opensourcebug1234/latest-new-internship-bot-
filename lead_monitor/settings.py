import os
from pathlib import Path
import yaml
from dotenv import set_key
from .config import ROOT


def save_config(config):
    data = {k:v for k,v in config.items() if not k.startswith('_')}
    path = Path(config.get('_root', ROOT)) / 'config.yaml'
    temporary = path.with_suffix('.yaml.tmp')
    temporary.write_text(yaml.safe_dump(data, sort_keys=False))
    temporary.replace(path)


def save_keys(apify='', openai='', anthropic=''):
    path = ROOT / '.env'
    if path.is_symlink():
        raise ValueError('Refusing to write credentials through a symbolic link.')
    for name, value in [('APIFY_TOKEN',apify), ('OPENAI_API_KEY',openai), ('ANTHROPIC_API_KEY',anthropic)]:
        if value.strip():
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            set_key(str(path), name, value.strip())
            os.environ[name] = value.strip()
