# V1.4.8

- Plan directly from library durations; no full candidate audio decoding.
- Prefer audio-track duration for music, with container duration as fallback.
- Pool allocation reserves 0.5% of video duration, capped at 3 seconds.
- Scheduled scans also use lightweight audio identification.
- Identified music decoder failures and short output audio trigger bounded music replacement using batch usage counts. Keep task/output identity, persist replacement history, exclude known bad music from later tasks.
- Maximum three rendering attempts. Insufficient eligible music produces an explicit error.
- API protocol 7. Fixed song-set mode retains its existing timeline rules.
