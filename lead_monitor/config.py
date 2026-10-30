from pathlib import Path
import os
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config(path=None):
    path = Path(path or ROOT / 'config.yaml').resolve()
    load_dotenv(path.parent / '.env', override=False)
    load_dotenv(path.parent / 'apify.env', override=False)
    config = yaml.safe_load(path.read_text())
    config['_root'] = str(path.parent)
    config['llm']['provider'] = os.getenv('LLM_PROVIDER', config['llm']['provider']).lower()
    return config


def local_path(config, name):
    return Path(config['_root']) / config[name]


def live_errors(c):
    errors = []
    if not c['safety']['live_scans_enabled']:
        errors.append('Live scans are disabled. Review spend limits and enable safety.live_scans_enabled in config.yaml.')
    if not c['company']['description'] or not c['company']['ideal_customer']:
        errors.append('Fill company.description and company.ideal_customer in config.yaml.')
    if not c['queries'] and not c['subreddits']:
        errors.append('Add queries and/or subreddits in config.yaml.')
    if not (c['relevance_terms'] or c['company']['pain_phrases'] or c['company']['competitors']):
        errors.append('Add relevance_terms, pain_phrases, or competitors for the cheap relevance check.')
    provider = c['llm']['provider']
    if provider not in ('openai', 'anthropic'):
        errors.append('LLM_PROVIDER must be openai or anthropic.')
    if not os.getenv('APIFY_TOKEN'):
        errors.append('APIFY_TOKEN is missing from .env or apify.env.')
    if not os.getenv('OPENAI_API_KEY' if provider == 'openai' else 'ANTHROPIC_API_KEY'):
        errors.append(f'The {provider} API key is missing; demo scans require no keys.')
    models = [os.getenv('LLM_MODEL', c['llm']['models'].get(provider, ''))]
    if c['llm']['second_pass']:
        models.append(c['llm']['strong_models'].get(provider, ''))
    for model in models:
        price = c['llm']['prices'].get(model, {})
        if any(not isinstance(price.get(k), (int, float)) or price[k] < 0 for k in ('input', 'output')):
            errors.append(f'Set current input/output token prices for {model} in llm.prices.')
    for key in ('max_items', 'max_runs', 'max_total_charge_usd', 'timeout_seconds'):
        if not isinstance(c['apify'][key], (int, float)) or c['apify'][key] <= 0:
            errors.append(f'apify.{key} must be positive.')
    for key in ('max_output_tokens', 'max_items_per_scan', 'max_text_chars', 'max_estimated_cost_usd'):
        if c['llm'][key] <= 0:
            errors.append(f'llm.{key} must be positive.')
    if c['time_window_days'] <= 0:
        errors.append('time_window_days must be positive.')
    return errors
