-- API tokens snapshot the creator's groups so group-based viewer
-- rules (privateRepoViewers) also apply to API requests.
ALTER TABLE api_tokens ADD COLUMN groups TEXT[] NOT NULL DEFAULT '{}';
