# Open — Murmur

Only things that genuinely need Tommy. Everything answerable from disk was answered.

## Needs a decision

Nothing open. Both prior questions were answered by Tommy on 2026-08-29:

- **Name:** keep "Murmur".
- **Launch at login:** on by default (`autostart: true`), still toggleable from the tray.

## Answered without asking

| Question | Answer | Where it came from |
|---|---|---|
| macOS or Windows? | Windows 11 | `Wispr Flow.lnk` dated today, Ctrl+Win chord, no Swift toolchain |
| Which local LLM? | `qwen2.5:7b-instruct` | Benchmarked 4 candidates on this machine, 2026-08-29 |
| Which polish prompt? | v3, few-shot | v1 and v2 both failed measurably; see plan Task 3 |
| Need cloud API keys? | No | Ollama at `127.0.0.1:11434` covers polish; whisper is local |
| STT device | Pending measurement | `scripts/probe_stt.py`, plan Task 2 — not a question for Tommy |
