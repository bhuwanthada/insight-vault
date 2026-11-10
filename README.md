# Insight Vault

**A free, private research notebook for turning interview transcripts into evidence-backed insights.**

Paste a transcript, highlight the important quotes, tag them, and group them into insights. Every insight stays linked to the exact quote it came from, so you can always show your evidence.

It runs entirely on your own computer. There are no accounts, no seat limits and no cloud.

## Why this exists

Insight Vault is inspired by [Dovetail](https://dovetail.com), the best-known customer research repository. I reviewed Dovetail and its competitors (Condens, Marvin, Aurelius, Notably, Reduct) and combined the best ideas:

| Idea | Borrowed from | How it works here |
|---|---|---|
| Highlight, tag, and trace insights to the source quote | Dovetail | Each insight links back to the exact quote and note |
| Flat pricing, unlimited users | Aurelius | Free and open source, with no accounts or seats |
| Data stays in your control | Condens | Everything is stored in a local file, `data/db.json` |
| Automatic summaries | Marvin, Notably | A summary, keywords and sentiment, computed locally |

## Quick start

You need [Node.js](https://nodejs.org) 18 or newer. There is nothing else to install.

```bash
git clone https://github.com/bhuwanthada/insight-vault.git
cd insight-vault
npm start
```

On Windows you can also double-click `start.bat`.

The app opens at <http://localhost:3737>.

## Try it with the sample interview

1. Start the app (`npm start`).
2. In a second terminal, run `node scripts/demo.js`.
3. Refresh the page. You will see one interview, 7 tagged highlights and 3 insights.

The sample transcript is in [`samples/interview-priya.txt`](samples/interview-priya.txt).

## How to use it

1. **Add a note.** Click **+ Add note**, then paste a transcript or load a `.txt`, `.md`, `.vtt`, `.srt` or `.csv` file.
2. **Highlight.** Select any text in the note, type tags such as `pricing, onboarding`, and press Enter.
3. **Build insights.** Open the **Insights** tab, filter by tag, tick the quotes that belong together, give the insight a title, and create it.
4. **Search.** The **Search** tab finds text in notes, quotes and tags.
5. **Export.** Click **Export insights (.md)** to download all insights and their quotes as Markdown.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `3737` | Port the app listens on |
| `DATA_FILE` | `data/db.json` | Where your data is stored |
| `NO_OPEN` | unset | Set to `1` to stop the browser opening automatically |

## Run the tests

```bash
npm test
```

## Project layout

```text
server.js        Web server, JSON API, and the local summary logic
public/          The web interface (a single index.html, no build step)
test/            API tests
samples/         Example interview transcript
scripts/demo.js  Loads the sample and builds example insights
start.bat        One-click launcher for Windows
```

## Limitations

- Summaries and sentiment use simple word-counting, not a language model, so treat them as a first pass.
- There is no audio or video transcription. Paste text or load a transcript file.
- Data is stored in one JSON file on one machine, and the server only listens on `localhost`.

## License

[MIT](LICENSE) © 2026 Bhuwan Thada
