SCHEMA = '''
CREATE TABLE IF NOT EXISTS paper_classifications (
 paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
 standard_key TEXT, confidence REAL, reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 1,
 previous_result TEXT NOT NULL DEFAULT '{}', method_version TEXT NOT NULL DEFAULT 'medium-v1', updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_paper_classifications_status ON paper_classifications(status,paper_id);
CREATE TABLE IF NOT EXISTS alert_paper_state (
 paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL DEFAULT 1,pending INTEGER NOT NULL DEFAULT 1,cursor_user INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_alert_papers_pending ON alert_paper_state(paper_id) WHERE pending=1;
CREATE TABLE IF NOT EXISTS alert_user_state (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL DEFAULT 1,pending INTEGER NOT NULL DEFAULT 1,
 cursor_date TEXT NOT NULL DEFAULT '',cursor_id INTEGER NOT NULL DEFAULT 0,
 window_start TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_alert_users_pending ON alert_user_state(user_id) WHERE pending=1;
CREATE TABLE IF NOT EXISTS author_metrics (
 author_id TEXT PRIMARY KEY, name TEXT NOT NULL, h_index INTEGER NOT NULL,
 yearly_citations TEXT NOT NULL DEFAULT '[]', fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS author_work_matches (
 paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
 work_id TEXT, status TEXT NOT NULL, checked_at TEXT);
CREATE TABLE IF NOT EXISTS paper_author_links (
 paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
 author_id TEXT NOT NULL, name TEXT NOT NULL, confidence REAL NOT NULL,
 PRIMARY KEY(paper_id,author_id));
CREATE TABLE IF NOT EXISTS app_logs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, level TEXT NOT NULL, job TEXT, model TEXT,
 message TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', user_id INTEGER, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_app_logs_date ON app_logs(created_at);
CREATE TABLE IF NOT EXISTS topic_pending_papers (
 paper_id INTEGER PRIMARY KEY REFERENCES papers(id), topic_id INTEGER REFERENCES topics(id),
 confidence REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS arxiv_window_checks (
 category TEXT, window_start TEXT, window_end TEXT, checked_at TEXT NOT NULL, identities TEXT NOT NULL,
 PRIMARY KEY(category,window_start,window_end));
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
 is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
 telegram_chat_id TEXT, notifications_enabled INTEGER NOT NULL DEFAULT 1,
 telegram_enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invite_codes (code TEXT PRIMARY KEY, used_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refresh_tokens (jti TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS papers (
 id INTEGER PRIMARY KEY, arxiv_id TEXT UNIQUE, arxiv_version INTEGER DEFAULT 1,
 title TEXT NOT NULL, authors TEXT DEFAULT '[]', abstract TEXT DEFAULT '', venue TEXT,
 venue_rank TEXT, primary_category TEXT, published TEXT, abs_url TEXT, pdf_url TEXT,
 pdf_path TEXT, fulltext TEXT, skeleton TEXT, tldr TEXT, embedding BLOB,
 quality_score REAL DEFAULT 50, hf_upvotes INTEGER DEFAULT 0, github_stars INTEGER DEFAULT 0,
 classified INTEGER DEFAULT 0, scored INTEGER DEFAULT 0, created_at TEXT NOT NULL, ingested_date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topics (
 id INTEGER PRIMARY KEY, name_zh TEXT NOT NULL, name_en TEXT NOT NULL,
 parent_id INTEGER REFERENCES topics(id), status TEXT DEFAULT 'active', created_by TEXT DEFAULT 'user', created_at TEXT NOT NULL,
 category_keys TEXT);
CREATE TABLE IF NOT EXISTS paper_topics (
 paper_id INTEGER REFERENCES papers(id), topic_id INTEGER REFERENCES topics(id), confidence REAL,
 PRIMARY KEY (paper_id, topic_id));
CREATE TABLE IF NOT EXISTS interactions (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), paper_id INTEGER REFERENCES papers(id),
 action TEXT NOT NULL, dwell_ms INTEGER DEFAULT 0, feed_context TEXT,
 target_id INTEGER REFERENCES interactions(id), previous_state TEXT, embedding_before BLOB,
 profile_id INTEGER, created_at TEXT NOT NULL, view_rule INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_paper_state (
 user_id INTEGER REFERENCES users(id), paper_id INTEGER REFERENCES papers(id),
 liked INTEGER DEFAULT 0, saved INTEGER DEFAULT 0, seen INTEGER DEFAULT 0, updated_at TEXT,
 PRIMARY KEY(user_id, paper_id));
CREATE TABLE IF NOT EXISTS interest_profile (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), version INTEGER NOT NULL,
 content TEXT NOT NULL, structured TEXT DEFAULT '{}', embedding BLOB, embedding_parts TEXT, change_reason TEXT, created_at TEXT NOT NULL,
 UNIQUE(user_id, version));
CREATE TABLE IF NOT EXISTS reading_cards (
 paper_id INTEGER PRIMARY KEY REFERENCES papers(id), card_json TEXT,
 status TEXT NOT NULL DEFAULT 'pending', error TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watches (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), type TEXT NOT NULL,
 value TEXT NOT NULL, active INTEGER DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), type TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT, paper_id INTEGER REFERENCES papers(id), read INTEGER DEFAULT 0,
 pushed INTEGER DEFAULT 0, dedupe_key TEXT, created_at TEXT NOT NULL, UNIQUE(user_id, dedupe_key));
CREATE TABLE IF NOT EXISTS chat_sessions (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), title TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_messages (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES chat_sessions(id), role TEXT NOT NULL,
 content TEXT NOT NULL, tool_calls TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_tools (
 id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), session_id INTEGER REFERENCES chat_sessions(id),
 name TEXT NOT NULL, arguments TEXT NOT NULL, profile_version INTEGER,
 status TEXT DEFAULT 'pending', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), action TEXT, detail TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topic_daily_stats (
 date TEXT, topic_id INTEGER REFERENCES topics(id), paper_count INTEGER, avg_quality REAL,
 PRIMARY KEY(date, topic_id));
CREATE TABLE IF NOT EXISTS daily_metrics (
 date TEXT, user_id INTEGER REFERENCES users(id), shown INTEGER, skipped INTEGER, liked INTEGER,
 saved INTEGER, expanded INTEGER, like_rate REAL, quick_skip_rate REAL, considered INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(date, user_id));
CREATE TABLE IF NOT EXISTS trend_reports (
 id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), content TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS direction_trends (
 audience TEXT PRIMARY KEY, profile_version INTEGER NOT NULL, summary TEXT NOT NULL DEFAULT '',
 evidence TEXT NOT NULL DEFAULT '[]', created_at TEXT, attempted_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS trend_reference_cache (
 scope_key TEXT PRIMARY KEY, papers TEXT NOT NULL DEFAULT '[]', fetched_at TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS source_status (
 name TEXT PRIMARY KEY, last_run TEXT, last_success TEXT, added INTEGER DEFAULT 0, error TEXT, running INTEGER DEFAULT 0, progress TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS arxiv_cursors (
 category TEXT PRIMARY KEY, first_sync_from TEXT NOT NULL, synced_through TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, content TEXT, expires_at INTEGER);
CREATE TABLE IF NOT EXISTS llm_usage (id INTEGER PRIMARY KEY, model TEXT, input_tokens INTEGER, output_tokens INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS app_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_categories (
 key TEXT PRIMARY KEY,kind TEXT NOT NULL,code TEXT NOT NULL,label TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1,fetch_enabled INTEGER NOT NULL DEFAULT 0,guest_default INTEGER NOT NULL DEFAULT 0,
 sort_order INTEGER NOT NULL DEFAULT 100,feed_url TEXT,standard_system TEXT,discipline TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topic_proposals (
 id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),topic_id INTEGER REFERENCES topics(id) ON DELETE SET NULL,
 standard_key TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(user_id,standard_key));
CREATE TABLE IF NOT EXISTS pipeline_commands (
 id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS paper_sources (
 source_id TEXT PRIMARY KEY, paper_id INTEGER NOT NULL REFERENCES papers(id), venue TEXT, year INTEGER);
CREATE INDEX IF NOT EXISTS idx_paper_sources_paper ON paper_sources(paper_id);
CREATE TABLE IF NOT EXISTS paper_categories (
 paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE, category_key TEXT NOT NULL,
 PRIMARY KEY(paper_id,category_key));
CREATE INDEX IF NOT EXISTS idx_paper_categories_key ON paper_categories(category_key,paper_id);
CREATE TABLE IF NOT EXISTS conference_sync (
 venue TEXT PRIMARY KEY, etag TEXT, modified TEXT, year INTEGER, checked_at TEXT);
CREATE INDEX IF NOT EXISTS idx_interactions_user_date ON interactions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_papers_dates ON papers(ingested_date, published);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON interest_profile(user_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_user_read_id ON notifications(user_id, read, id DESC);
CREATE INDEX IF NOT EXISTS idx_state_user ON user_paper_state(user_id, seen);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_topics_links ON paper_topics(topic_id,paper_id);
'''

