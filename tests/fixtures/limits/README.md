# Limit messages and used-share events

Fixtures for limit detection and the soft threshold (design.md D8, D14; spec `run-ledger`). Pinned
CLIs: Claude Code 2.1.316 and Codex CLI 0.171.0. The file name gives the source:

- `*.live.*`: captured from one minimal call ("Reply with exactly: OK") with the benchmark login.
- `*.stub.*`: the real pinned CLI against a local HTTP stub that answers every model request with
  HTTP 429, so no limit was approached and no login was used (Claude got a dummy OAuth token and
  `ANTHROPIC_BASE_URL`, Codex a custom `model_provider`). The CLI produced every line; the stub only
  chose the status, headers and body. Reset times come from the stub, in UTC.
- `*.code.json`: message templates and typed fields taken from the CLI's own code, read offline.

Redactions: ids are the zero UUID, timestamps are `2026-01-01T00:00:00.000Z`, `cwd` is `/work`,
`entrypoint` is `<entrypoint>`, and every live reset time is a fake epoch (`1900000000` for the
5-hour window, `1900500000` for the weekly one), with the integer format kept. System lines of
Claude's stream-json are left out because they carry host paths. The Codex stub runs drop one
warning about the stub's unknown model name.

| File | Source | What it shows |
| --- | --- | --- |
| `claude/stream-json-rate-limit-event.live.jsonl` | live | `rate_limit_event` on stdout of `claude -p --output-format stream-json --verbose`: used share per window in `unifiedWindows.*.utilization` (0 to 1) |
| `claude/stream-json-usage-limit-five-hour.stub.jsonl` | stub | 5-hour limit: `rate_limit_event` `rejected`, the synthetic assistant message and the result (`is_error`, `api_error: usage_limit_reached`); exit 1 |
| `claude/stream-json-usage-limit-weekly.stub.jsonl` | stub | the same for the weekly limit |
| `claude/stream-json-server-rate-limit.stub.jsonl` | stub | a 429 without subscription headers: `api_error` null, "not your usage limit"; exit 1 |
| `claude/transcript-*.stub.jsonl` | stub | the same three as the session transcript records them (`apiError`, `apiErrorStatus`, `quotaLimits`) |
| `claude/messages.code.json` | code | message templates, the CLI's own usage-limit prefixes, reset time format |
| `codex/rollout-token-count.live.jsonl` | live | `event_msg` `token_count` in the session rollout: `rate_limits.primary` (300 min) and `secondary` (10080 min) `used_percent` |
| `codex/exec-json-usage-limit-{plus,team,no-plan}.stub.jsonl` | stub | `codex exec --json` stdout at a usage limit, per plan type; exit 1 |
| `codex/exec-json-retry-limit-429.stub.jsonl` | stub | any other 429 after retries; exit 1 |
| `codex/rollout-*.stub.jsonl` | stub | the rollout's turn end for the same runs: `codex_error_info` `usage_limit_exceeded` or `response_too_many_failed_attempts` |
| `codex/messages.code.json` | code | message templates per plan type, retry suffixes, reset time format |
