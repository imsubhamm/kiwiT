# Strategy candidate generator (KIW-20)

`kiwit.ml.candidates.generate_candidates(snapshot, scores, previous=(), config=None)` combines KIW-19 registry score envelopes. It returns explicit candidates, rejections, conflicts, and the original model evidence. It performs no broker, sizing, or order operations.

Scores must identify the same exact feature snapshot, model fingerprint, model version and feature version. Registry scoring now emits `snapshot_fingerprint`. Unready inputs or unavailable/uncertain regime scores yield NO_CANDIDATE. Missing specialist scores are individually rejected; valid remaining specialists may produce candidates.

The versioned default compatibility matrix allows trend and breakout in UPTREND/DOWNTREND, with direction matching the trend, and mean reversion and breakout in RANGE. HIGH_VOLATILITY allows no candidates. Regime confidence defaults to 0.6 and specialist confidence to 0.65. These are research policy defaults, not validated trading thresholds.

Confident opposing specialist directions are detected before compatibility filtering. All conflicting evidence is retained and the decision yields NO_CANDIDATE, including when one opposing strategy would otherwise be regime-incompatible. Weak signals and specialist NONE predictions cannot create candidates.

Each candidate includes a deterministic ID, strategy, direction, confidence, regime, timestamp, instrument, snapshot/model/regime fingerprints, configuration, entry thesis and reason codes. The generator is pure: identical snapshot, scores, configuration and previous emitted candidates yield the same result.

The caller supplies previously emitted candidates. Exact duplicates are suppressed, and repeated instrument/strategy/direction setups have a default 300-second cooldown; equality at the cooldown boundary permits a new setup. Future or timezone-naive history for the same instrument is rejected. Persist and serialize this history in the service that consumes candidates; this module does not provide cross-process deduplication or wire itself into the deployed engine.

Tests cover determinism, conflict retention, weak/stale/mismatched inputs, compatibility, duplicate suppression and cooldown boundaries. Deployment integration and real-market validation remain pending.
