CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL);
CREATE TABLE task (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  done BOOLEAN NOT NULL,
  priority INTEGER NOT NULL,
  tags JSONB,
  due TIMESTAMPTZ,
  created_at TIMESTAMP NOT NULL
);
CREATE TABLE label (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE task_label (task_id TEXT, label_id TEXT, PRIMARY KEY (task_id, label_id));
