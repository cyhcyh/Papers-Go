"""Source-aware subscriptions shared by recommendations and trend summaries."""
from ..catalog import matches, DEFAULT_CATEGORY_WEIGHT


def category_weights(structured):
    selection = structured.get('category_selection') or {}
    keys = list(dict.fromkeys([*selection.get('categories', []),
                              *(key for key, ids in selection.get('topics', {}).items() if ids)]))
    return {key: selection.get('weights', {}).get(key, DEFAULT_CATEGORY_WEIGHT) for key in keys}


def has_selection(structured):
    selection = structured.get('category_selection')
    if selection is not None:
        return bool(selection.get('categories') or any(selection.get('topics', {}).values()))
    return bool(structured.get('topic_ids'))


def matched_keys(structured, paper, topic_ids):
    selection = structured.get('category_selection')
    if selection is None:
        return None if set(structured.get('topic_ids', [])) & set(topic_ids) else []
    keys = [key for key in selection.get('categories', []) if matches(paper, key)]
    keys.extend(key for key, ids in selection.get('topics', {}).items() if matches(paper, key) and set(ids) & set(topic_ids))
    return list(dict.fromkeys(keys))


def in_scope(structured, paper, topic_ids):
    if not has_selection(structured):
        return True
    keys = matched_keys(structured, paper, topic_ids)
    return keys is None or bool(keys)
