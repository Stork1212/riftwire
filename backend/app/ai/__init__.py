"""Local AI analysis (M5 preparation): providers, prompts, schemas and validation.

Nothing here is wired into the job queue yet. M5 connects it after the M4 gate; the module
exists early so the benchmark harness (``bench/llm``) exercises the same validator and
provider code that production will use.

Principles (CLAUDE.md rules 12, 15, 16): providers are analytical components with no tools;
every model output is untrusted until :mod:`app.ai.validate` accepts it; timestamps come only
from evidence ids, never from the model; loopback-only by default; no paid provider exists.
"""
