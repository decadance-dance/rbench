# Limit messages and used-share events

Fixtures for limit detection and the soft threshold (design.md D8, D14; spec `run-ledger`). Pinned
CLIs: Claude Code 2.1.316 and Codex CLI 0.171.0. The file name gives the source:

- `*.live.*`: captured from one minimal call ("Reply with exactly: OK") with the benchmark login.
- `*.stub.*`: the real pinned CLI against a local HTTP stub that answers every model request with
  HTTP 429, so no limit was approached and no login was used (Claude got a dummy OAuth token and
  `ANTHROPIC_BASE_URL`, Codex a custom `model_provider`). The CLI produced every line; the stub only
  chose the status, headers and body. Reset times come from the stub, in UTC.
- `*.code.json`: message templates and typed fields taken from the CLI's own code, read offline.

Redactions in every file: ids are the zero UUID, `cwd` is `/work` and `entrypoint` is
`<entrypoint>`. System lines of Claude's stream-json are left out because they carry host paths.
The Codex stub runs drop one warning about the stub's unknown model name.

The live files keep only the shape of the account's readings; every value that came from the
account is replaced, and the formats are kept:
- `codex/rollout-token-count.live.jsonl`:
  - `plan_type` and `credits` are null;
  - `used_percent` is synthetic: 40.0 for `primary`, 20.0 for `secondary`;
  - `resets_at` is a fake epoch: 1900000000 for `primary`, 1900500000 for `secondary`;
  - `timestamp` is one hour before the `primary` reset;
  - the token counts are those of the one-line prompt, not of the account.
- `claude/stream-json-rate-limit-event.live.jsonl`: every value in `rate_limit_info` is synthetic.
  - `utilization` is 0.25 for the 5-hour window and 0.8 for the weekly one;
  - `resetsAt` is 1900000000 and 1900500000;
  - status and threshold are consistent with those values;
  - the account's overage fields are left out (the CLI's schema marks them optional).

Reference clock for time-only resets: parser tests use each record's `timestamp` as "now" and UTC
as the CLI's time zone.
- Claude stubs: `timestamp` is 3 hours before the stub's reset epoch, so "resets 3:33am (UTC)" is
  the same day.
- Codex stubs: `timestamp` is `2026-11-21T00:32:00.000Z`, and "try again at 3:32 AM" means
  2026-11-21 03:32 UTC.
- Claude lines without a `timestamp` (`rate_limit_event`, `result`) take the clock of the
  assistant line in the same file. The Codex `exec --json` files take the clock of the matching
  rollout file.

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
