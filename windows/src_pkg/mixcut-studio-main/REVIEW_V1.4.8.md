# V1.4.8 Review

Planning no longer calls precise_music. Decoder errors are mapped by FFmpeg input index so source-video/encoder/disk errors do not randomly change music. Music replacement is serialized with store updates, keeps output paths stable, updates song metadata/counts, and respects dismissed/cancelled tasks. Later tasks avoid known bad songs within the batch.

Tests cover no decode/hash during planning, margin feasibility, error input mapping, successful replacement, exhaustion, and actual FFmpeg rendering of a deliberately damaged AAC packet followed by replacement and validated export.

A read-only snapshot of the live library (375 videos, 514 songs) was planned in an isolated temporary store: 30 tasks of 10-15 minutes, 0.331 seconds, full audio decode and file hashing forbidden. This is not a benchmark using all original user settings.

Limitations: metadata cannot guarantee successful playback. Only identifiable music errors trigger replacement; others remain explicit task failures. Three render attempts maximum; insufficient replacement music fails visibly. Output validation remains. Fixed mode retains full-set duration rules. Windows source and tests are synchronized but Windows installation is not verified on macOS.

Validation completed: 187 root tests and 185 Windows-mirror tests passed on macOS. JS syntax and diff checks passed. Frozen application reported version 1.4.8 / protocol 7; task/cache/review API build checks and native WebKit smoke passed.
