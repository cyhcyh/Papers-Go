FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY VERSION.txt /VERSION.txt
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
ADD https://www.sqlite.org/2026/sqlite-amalgamation-3510300.zip /tmp/sqlite.zip
# Verify the exact upstream source used in the compatibility tests.
RUN python -c "import zipfile,hashlib,pathlib; src=zipfile.ZipFile('/tmp/sqlite.zip').read('sqlite-amalgamation-3510300/sqlite3.c'); assert hashlib.sha3_256(src).hexdigest()=='32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e'; pathlib.Path('/tmp/sqlite3.c').write_bytes(src)"
RUN mkdir -p /opt/sqlite && gcc -O2 -fPIC -shared -Wl,-soname,libsqlite3.so.0 \
    -DSQLITE_THREADSAFE=1 -DSQLITE_USE_URI=1 \
    -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_DBSTAT_VTAB \
    -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS3_PARENTHESIS -DSQLITE_ENABLE_FTS3_TOKENIZER \
    -DSQLITE_ENABLE_FTS4 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_MATH_FUNCTIONS \
    -DSQLITE_ENABLE_PREUPDATE_HOOK -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_SESSION \
    -DSQLITE_ENABLE_STMTVTAB -DSQLITE_ENABLE_UNLOCK_NOTIFY \
    -DSQLITE_LIKE_DOESNT_MATCH_BLOBS -DSQLITE_OMIT_LOOKASIDE -DSQLITE_SECURE_DELETE -DSQLITE_SOUNDEX \
    -DSQLITE_DEFAULT_AUTOVACUUM=1 -DSQLITE_DEFAULT_RECURSIVE_TRIGGERS=1 \
    -DSQLITE_MAX_DEFAULT_PAGE_SIZE=32768 -DSQLITE_MAX_FUNCTION_ARG=127 \
    -DSQLITE_MAX_VARIABLE_NUMBER=250000 -DSQLITE_MAX_PAGE_COUNT=1073741823 \
    -DSQLITE_MAX_VDBE_OP=250000000 -DSQLITE_MAX_WORKER_THREADS=8 \
    /tmp/sqlite3.c -o /opt/sqlite/libsqlite3.so.0 -ldl -lm -pthread

FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend DATA_DIR=/data FRONTEND_DIR=/app/frontend/dist
COPY backend/requirements.txt ./backend/requirements.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY --from=sqlite-build /opt/sqlite /opt/sqlite
ENV LD_LIBRARY_PATH=/opt/sqlite
# Fail the build if Python loads the wrong engine or either index is unusable.
RUN python -c "import sqlite3,sqlite_vec; \
    assert sqlite3.sqlite_version=='3.51.3',sqlite3.sqlite_version; \
    db=sqlite3.connect(':memory:'); \
    db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False); \
    db.execute(\"CREATE VIRTUAL TABLE smoke_fts USING fts5(text,tokenize='trigram',detail='none')\"); \
    db.execute('INSERT INTO smoke_fts VALUES(?)',('graph research',)); \
    assert db.execute('SELECT COUNT(*) FROM smoke_fts WHERE smoke_fts MATCH ?',('gra',)).fetchone()[0]==1; \
    db.execute('CREATE VIRTUAL TABLE smoke_vec USING vec0(embedding float[4] distance_metric=cosine)'); \
    vector=sqlite_vec.serialize_float32([1.,0.,0.,0.]); \
    db.execute('INSERT INTO smoke_vec(rowid,embedding) VALUES(1,?)',(vector,)); \
    match=db.execute('SELECT rowid,distance FROM smoke_vec WHERE embedding MATCH ? AND k=1',(vector,)).fetchone(); \
    assert match[0]==1 and abs(match[1])<0.000001; \
    assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]; \
    db.close(); print('SQLite 3.51.3, sqlite-vec and FTS5 checks passed')"
COPY LICENSE ./LICENSE
COPY VERSION.txt CHANGELOG ./
COPY backend/ ./backend/
COPY --from=web /web/dist ./frontend/dist
RUN mkdir -p /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
