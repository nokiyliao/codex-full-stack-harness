# Control Deck public-reference reuse matrix

| Reference | Disposition | Bounded use | Rejected surface |
| --- | --- | --- | --- |
| OpenAI Symphony | ADAPT | Single orchestrator ownership, issue/task lifecycle vocabulary, reconciliation loop | Its daemon and external issue tracker are not installed or copied |
| JSCOP ATC Kanban | CONCEPT_ONLY | Dependency DAG and attention-first board presentation | Separate SQLite, MCP server, lock TTL, auto-merge, PID liveness inference |
| codex-agents | CONCEPT_ONLY | Compact terminal/session status presentation | Transcript scraping and terminal-dashboard ownership |
| multiagents | REJECT | None | No repository-declared license at the frozen commit; no code or schema reuse |

The candidate uses only Tura's existing Session DB, Router, Gateway and lifecycle
sidecar. It starts no third-party daemon, adds no task write API, and does not
copy public-source implementation bytes.
