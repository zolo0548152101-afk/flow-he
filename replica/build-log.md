# Build log — Flow-HE

| date | step | status | notes |
| --- | --- | --- | --- |
| 2026-10-28 | S01 Flow Bar (idle/rec/busy/done, drag, hidden by default, tray toggle) | done | click-tier Explorer made manual launches painful; solved by self-update |
| 2026-10-28 | F01 Push-to-talk (Caps Lock, Ctrl+Win) | done | `keyboard` lib drops injected Alt and cannot suppress Caps Lock → native WH_KEYBOARD_LL hook |
| 2026-10-28 | F02 Hands-free (Ctrl+Win+Space, double-tap) | done | |
| 2026-10-28 | F04 Paste last / copy last (Alt+Shift+Z/X) | done | scan-code match; Hebrew layout reports Z as ז |
| 2026-10-28 | Text insertion | done | synthetic Ctrl+V ignored by Claude Desktop and flaky elsewhere → Unicode SendInput typing everywhere |
| 2026-10-28 | Target under mouse (I-beam / edit class) | done | only when pointing at a different window than the focused one |
| 2026-10-28 | Streaming at pauses (chunker + parallel requests) | done | 13 s recording → 2.8 s wait after release in CI |
| 2026-10-28 | Mic picker, launch at login, silent-mic warning, failed queue | done | |
| 2026-10-28 | Self-update (updates/ + manifest + sha256) | done | proven on the user's machine 20→22 |
| 2026-10-29 | .env hot reload | done | remote tools cannot write .env |
| — | S02 Home/History window, S03 Dictionary, S04 Snippets, S08–S12 Settings | not started | see working doc |
| — | Live text while speaking | planned | research in progress |
| 2026-10-29 | v25 live text in bar | done | Chunks shown in the bar as they return (RTL, grows upward, ≤~3 lines), Whisper hallucination filter (textproc.clean_chunk + tests), min chunk 3s/max 12s. E2E: live text visible while recording, wait after stop 2.8s. Not yet: tentative grey tail (LocalAgreement-2) — waits for the faster server. |
| 2026-10-29 | v28–v35 | done | quiet-mic fix; whole-utterance final pass; smart punctuation (Sonnet + guard_words: no word may change); key under Esc; new logo/DPI bar/soft sounds; update button + desktop shortcuts; lecture mode; live provisional text in the field replaced by exact backspace count. |
| 2026-10-29 | v38–v43 | done | Claude off by default (dictation/lecture/meeting); spinner fix; meeting mode: mic + system loopback, speaker separation by voice (sherpa-onnx 3D-Speaker + pyannote, local CPU), live labels, diarized second pass, rename speakers, speaker-count selector, RTL Word export; restyled windows. E2E: real 4-speaker recording -> 4 speakers. |
| 2026-10-29 | v44–v51 + server | done | Own cloud backend: Vercel project flow-he-api (server/, auto-deploy from GitHub) -> RunPod serverless endpoint flow-he-ivrit (ivrit official image, 24GB, 0 active, max 1 worker, idle 15s, FlashBoot). App reads server.env; Opus for long audio; job polling; meeting speaker separation + lecture second pass on the server. Bench: warm 6s clip 1.3s, cold 21.6s, 57s audio 4.9s, +diarize 8.3s. |
