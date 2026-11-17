## Summary

<!-- 1-3 bullet points describing what this PR does and why -->

-

## Risk tier

<!-- Highest tier of any changed file (see CLAUDE.md → Risk tiers): Critical / High / Standard / Low -->

## Test plan

- [ ] `uv run pytest` — all tests pass
- [ ] `uv run ruff check .` — lint clean
- [ ] `uv run ruff format --check .` — format clean
- [ ] `uv run mypy src/` — types clean
- [ ] Config validates (`uv run yamllint homeassistant esphome deploy`, `esphome config` if ESPHome touched)
- [ ] New tests added for new logic (written first)
- [ ] Verified on the van (describe below) — required before a PID/CAN ID is marked VERIFIED

## AI usage

- [ ] AI-assisted (tool and scope: _e.g., Claude Code for test generation_)

## Checklist

- [ ] No secrets, real GPS tracks, fill-up records or photos committed (this repo is public)
- [ ] No PID, DID or CAN ID marked VERIFIED without a scan log in `docs/pid-register.md`
- [ ] Influx schema change reflected in CLAUDE.md → Data model and `/influx-schema`
- [ ] Documentation updated (if applicable)

## Related issues

<!-- e.g. Closes #123, Fixes #456 -->
