-- Advertisement module: ads, their media, and basic delivery events.
-- Ad configuration lives in app_data (like provider_reviews); media bytes
-- are stored base64-encoded in ad_media.data because the production host's
-- filesystem is ephemeral (the same reason provider photos live in the
-- database). Uploads are size-capped by the application (images 5 MB,
-- videos 10 MB) so no large files are stored here.
CREATE TABLE IF NOT EXISTS advertisements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS ad_media (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ad_id uuid NOT NULL REFERENCES advertisements(id) ON DELETE CASCADE,
    data text NOT NULL,
    content_type text NOT NULL DEFAULT 'application/octet-stream',
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ad_media_ad_idx
    ON ad_media (ad_id);
CREATE TABLE IF NOT EXISTS ad_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ad_id uuid NOT NULL REFERENCES advertisements(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ad_events_ad_idx
    ON ad_events (ad_id, created_at);
