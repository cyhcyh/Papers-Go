"""Conservative input budgets for the models used to read papers."""
from ..config import settings


def token_upper_bound(text):
    # One token per UTF-8 byte is conservative and needs no extra tokenizer.
    return len(text.encode('utf-8'))


def reading_budget(binding, level='L2'):
    if binding['kind']=='ollama':
        context=16384  # Matches the native streaming request's num_ctx.
    elif settings().reading_context_tokens:
        context=settings().reading_context_tokens
    elif (binding.get('control_capabilities') or {}).get('context_window'):
        context=int(binding['control_capabilities']['context_window'])
    elif binding['model'].lower().rsplit('/',1)[-1].startswith('deepseek-v4'):
        context=1000000
    else:
        context=32768  # Unknown cloud models can be configured explicitly.
    desired=16384 if level=='L2' else 32768
    if binding.get('thinking','auto')!='off':
        desired*=2  # The provider may count reasoning in the output budget.
    output=min(desired,context//3)
    return context,output


def prefix_bytes(text, budget):
    return text.encode('utf-8')[:max(0,budget)].decode('utf-8',errors='ignore')
