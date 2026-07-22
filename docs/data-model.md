# Data model

- **Raw Event**: append-only user, assistant, feedback or resource evidence.
- **Session Archive**: searchable original messages grouped by session.
- **Memory Unit**: an atomic candidate extracted from one raw event.
- **Claim**: sourced, temporal, correctable long-term memory.
- **Hot Memory**: bounded, frozen per-session projection of eligible claims.
- **Proposal**: a reviewable correction, replacement or other high-risk change.
- **Audience**: a user/household/person/project/device/Agent/session/event set of members.
- **Channel**: an addressable curated feed bound to one Audience.
- **Shared Activity**: a compact cross-workspace event summary with provenance and optional validity.
- **Temporal State**: one current, superseding value for a channel + subject + key, with observation and expiry time.
- **Binary Asset**: SHA-256-addressed bytes stored on disk; SQLite retains immutable base metadata, links, and separate profile/channel/workspace/Agent scope bindings.
- **Map Version**: an immutable version under a stable map id, with coordinate frame and optional asset.
- **Spatial Observation**: searchable caption/OCR/object/location semantics linked to a map and/or asset.

SQLite holds authoritative evidence, claims, queues, shared semantics, asset
metadata and indexes. Raw asset bytes live under `assets/objects`; Markdown
files are readable projections, not a reason to bypass the SQLite path.

Ordinary Claims keep their existing global/workspace/Agent visibility.
Audiences/channels do not rewrite those rules. Turns and unfinished answers are
owned by the originating Agent; shared context contains only curated activity,
current state, and bounded spatial semantics—not raw conversations or binaries.

One physical asset object may be deduplicated across uploads while each scope
keeps its own media type/metadata and visibility binding. A subject allow-list
and audience membership are separate checks: a family Agent can see only
subjects explicitly allowed by its Token and records available through the
selected audience/channel.

Spatial rows store results supplied by a robot or upstream model. The data
model does not imply built-in image understanding, OCR, object detection, SLAM,
map fusion, localization, or path planning.
