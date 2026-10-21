# Verification — 2026-10-21

The current-user Windows installation was tested against real filesystem notifications, not only a mock UI.

- Unit/integration command: `python -m unittest discover -s tests -v` (24 tests).
- Live fixture: a 119-byte text file appeared in the real collector index in **281 ms**.
- Live authenticated state query: **140 ms** in the recorded run. Latency varies with hardware and scan load; this is a measurement, not a guarantee.
- OpenCode Go / `deepseek-flash` generated an actual structured summary for that fixture. SHA-256 of the file before and after analysis was identical.
- Initial inventory creates no AI jobs. Automatic AI waits for the first full scan; manual requests were explicitly tested.
- Windows create/grow/move/delete events for small files, all-file indexing and interrupted scan queues are exercised by the integration harness.
- Custom date boundaries include 9/10 and 9/29, exclude 9/30; timestamp ranges use `[start,end)`.
- Missing historical samples and unknown prior sizes are shown as unknown, not made-up byte changes.
- Delete, edit and command-execution API paths return 404; token/Host/Origin checks reject foreign origins.
- Candidate rules preserve secrets, system files, Git/worktrees, model weights and session history as protected/review-required categories.
- SQLite runtime is application-local **3.53.1**, SHA-256 `C145910DB0EE9AE8C9EE836D64FA8B891EC290642903505D85F6694F9CE15E73`.
- Real index `PRAGMA integrity_check` returned `ok` after concurrent scan, HTTP polling and an AI job.
- Ego Browser exercised date presets, custom datetime inputs, RAM view, candidates, map, settings and file details. Its pixel capture returned an unusable frame on this host, so an isolated local Chromium process supplied visual QA.
- Restored the nested pastel treemap as the default view, with cache hatching, a period-growth map and visual candidate ring. Directory byte remainders, multiple roots and live-growth reconciliation are covered by tests. Source evidence is persisted for later historical queries.
- Local Chromium desktop and 390-pixel mobile views had no uncaught JavaScript errors and no horizontal overflow.
- The desktop and previous “磁盘哨兵” shortcuts target the new desktop repository.
- The current-user Task Scheduler watchdog and login entry were installed without elevation.

## Remaining practical boundaries

First whole-computer inventory runs in the background and may take time for millions of files. Protected paths are skipped and reported. No past unobserved history can be reconstructed reliably. Agent attribution is evidence correlation, not kernel-level write auditing; OpenCode's non-JSONL database is not parsed into individual conversations in this version. RAM is sampled, not every allocation. No automatic deletion or modification capability exists.
