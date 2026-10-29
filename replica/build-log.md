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
