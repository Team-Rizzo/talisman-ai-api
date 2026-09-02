-- Mechanism profiles and controller proposals. Additive: creates two new tables,
-- alters nothing existing.
--
-- mechanism_profile is append-only by design. There is no update or delete path in
-- the application: rolling a value back means publishing the previous body under a
-- higher version, so this table stays the complete record of what the mechanism has
-- ever been. The version is the primary key, which makes a replayed publish a
-- conflict rather than an overwrite.

CREATE TABLE IF NOT EXISTS "mechanism_profile" (
    "version"          INTEGER PRIMARY KEY,
    "publish_block"    INTEGER NOT NULL,
    "activation_block" INTEGER NOT NULL,
    "schema_version"   TEXT NOT NULL,
    "body"             JSONB NOT NULL,
    "signature"        TEXT NOT NULL,
    "published_by"     TEXT,
    "published_at"     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_mechanism_profile_activation"
    ON "mechanism_profile" ("activation_block");

-- Controller steps validators observed. Informational only: nothing reads this to
-- decide capacity, which moves only when an operator publishes a profile.
CREATE TABLE IF NOT EXISTS "controller_proposal" (
    "id"               BIGSERIAL PRIMARY KEY,
    "validator_hotkey" TEXT NOT NULL,
    "epoch"            INTEGER NOT NULL,
    "roi_ema"          DOUBLE PRECISION NOT NULL,
    "direction"        INTEGER NOT NULL,
    "magnitude"        DOUBLE PRECISION NOT NULL,
    "created_at"       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_controller_proposal_epoch"
    ON "controller_proposal" ("epoch");
