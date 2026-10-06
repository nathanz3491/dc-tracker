-- 0033_watch_project: watch one exact project, by the button on its row.
--
-- A watch used to be text only: "xAI" for a company, "xAI | Colossus" for one of
-- its projects, the project part matched as a substring in either direction. That
-- looseness is right for something typed by hand — the database holds whatever the
-- first article called the campus — and wrong for a button on one row of the
-- Projects table, because a watch on "Colossus" also matches "Colossus 2". A star
-- that lights up three rows when you press one is a broken star.
--
-- So a watch may now name a project by id. `project_id` set means exactly that
-- project and nothing else. NULL keeps the old meaning, untouched.
--
-- **`project_key` holds "#<id>" on these rows**, and that is the existing UNIQUE
-- constraint's doing. `uq_watch_entity` is (account_id, company_key, project_key),
-- and an exact watch on Colossus would otherwise carry the same key as the typed
-- watch "xAI | Colossus" and the two could not coexist. A lowercased project name
-- never starts with "#", so the spaces cannot collide, and the partial index below
-- is what actually guarantees one exact watch per account per project.
--
-- **ON DELETE CASCADE, and merges do not rely on it.** A project deleted outright
-- takes its exact watches with it, which is right: there is nothing left to watch.
-- A merge deletes the folded row too, and there the watch has to survive, so
-- `merge.merge_projects` moves it to the surviving project before the delete.
--
-- ADD COLUMN rather than a rebuild: SQLite allows a REFERENCES clause on an added
-- column whose default is NULL, and nothing else about the table changes.

ALTER TABLE watch ADD COLUMN project_id INTEGER REFERENCES project (id) ON DELETE CASCADE;

CREATE UNIQUE INDEX uq_watch_project ON watch (account_id, project_id)
    WHERE project_id IS NOT NULL;
