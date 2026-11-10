# Insight Vault

A local-first research repository inspired by Dovetail. Paste or load interview transcripts, highlight and tag quotes, then group evidence into insights that stay linked to their source.

Built from a competitor review of Dovetail, Condens, Marvin, Aurelius, Notably and Reduct:

| Idea | From | Here |
|---|---|---|
| Highlight, tag, insight, source traceability | Dovetail | Every insight links back to the exact quote and note |
| Flat pricing, unlimited users | Aurelius | Free, no accounts, no seat limits |
| Data residency / privacy | Condens | Data lives in `data/db.json` on your machine |
| AI-native summaries | Marvin, Notably | Local extractive summary, keywords, sentiment (no cloud calls) |

## Run

Requires Node.js 18+. No dependencies to install.

    npm start        # or double-click start.bat on Windows

Opens http://localhost:3737. Set `PORT` or `DATA_FILE` to change the port or storage path.

## Test

    npm test

## Limits

- Summaries are heuristic, not an LLM.
- No audio/video transcription yet; paste text or load .txt/.md/.vtt/.srt.
- Single-machine storage (JSON file); the server binds to localhost only.
