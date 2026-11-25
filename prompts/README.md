# Prompt registry

Versioned prompts for local AI analysis (`backend/app/ai/prompts.py`). Each file has TOML
front matter between `+++` lines (`id`, `version`, `schema`, generation defaults) and exactly
one `## system` and one `## user` section. Placeholders are `{{name}}`.

- A changed prompt is a **new version** (new file or bumped `version`); results record the
  prompt id, version and SHA-256.
- Game, category and channel instructions live here as data, never in code.
- Transcript, OCR and metadata are inserted only inside the evidence block and are treated
  as untrusted data by the system rules.

| id | version | file | status |
|---|---|---|---|
| `gaming.gta.v1` | 1 | `gaming/gta/highlight.v1.md` | initial; not yet evaluated on real footage |
