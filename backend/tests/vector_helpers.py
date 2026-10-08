"""Storage assertions use the active independent space, not legacy tables."""
from app import vector_store


def vector_rows(sql='SELECT * FROM papers_vec',args=(),*,staged=False):
    if staged:
        from app.llm.vector_rebuild import pending
        name=pending()['vector_file']
        sql=sql.replace('embedding_rebuild_papers','paper_vectors').replace('embedding_rebuild_profiles','profile_vectors')
    else:
        name=vector_store.active()['name']
    with vector_store.reader(name) as db:
        return [dict(row) for row in db.execute(sql,args)]


def embedding(paper_id):
    return vector_store.get(paper_id)
