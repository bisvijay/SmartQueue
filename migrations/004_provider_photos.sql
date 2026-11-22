-- Provider photos move from local disk files to database rows so uploads
-- survive hosts with ephemeral filesystems (e.g. Render free services,
-- where every deploy or restart wipes local files). The image bytes are
-- stored base64-encoded in the data column (JPEG, at most 1600px).
CREATE TABLE IF NOT EXISTS provider_photos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    data text NOT NULL,
    content_type text NOT NULL DEFAULT 'image/jpeg',
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS provider_photos_provider_idx
    ON provider_photos (provider_id);
