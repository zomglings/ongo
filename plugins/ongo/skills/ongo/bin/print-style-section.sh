#!/usr/bin/env bash
# print-style-section.sh — emit the SKILL.md "## Writing style" section
# iff ${CLAUDE_SKILL_DIR}/writing-style.md exists and is non-empty.
# Called from SKILL.md via the inline-command-substitution syntax:
#     !`${CLAUDE_SKILL_DIR}/bin/print-style-section.sh`
# Output is substituted verbatim into the model's system context at
# skill-load time. If the file is absent or empty, this script emits
# nothing and the entire section vanishes from the SKILL.md the model
# sees.

set -eu

STYLE_FILE="${CLAUDE_SKILL_DIR}/writing-style.md"

if [ ! -s "$STYLE_FILE" ]; then
  # File missing or empty -> emit nothing. The model's SKILL.md will
  # have no Writing-style section at all this tick.
  exit 0
fi

cat <<'PROSE'
## Writing style

A writing-style guide is configured at `${CLAUDE_SKILL_DIR}/writing-style.md`. Its contents are inlined below at skill-load time via Claude Code's `!\`command\`` inline command substitution (https://code.claude.com/docs/en/skills.md). Treat the inlined block as a *controlling style guide* for every piece of prose produced this tick — Slack replies, status messages, spawn announcements, sign-off relays. Every prose-generating subagent dispatched this tick must also receive the same block verbatim under a `## Writing style` heading in its prompt (the dispatching loop re-cats the file at dispatch time; see Auto-Expansion step 4). The style is inherited transitively: a subagent that itself dispatches sub-subagents must copy the block into their prompts too, so the guide propagates the entire spawn tree.

The 4096-char ceiling on the inlined block is enforced by `head -c 4096` in this loader script.

### Style block (verbatim from writing-style.md)

PROSE

head -c 4096 "$STYLE_FILE"

cat <<'PROSE'

### Subagents-only for note writes

A consequence of having a single style-enforcement point: the loop must not write new ongo notes or substantial note edits directly. Every new publication (kind `note`, `arxiv`, `web`, etc.) and every edit longer than one paragraph goes through a subagent — the subagent is the only writer that sees the style block embedded in its prompt, so writing notes inline from the loop bypasses the very mechanism this section sets up. Trivial inline ops the loop continues to do itself: renames, dedup deletions, regeneration, kendb housekeeping, slug fixes, Slack replies of any length. The rule is about prose published to the site, not about any character of prose the loop ever emits.

PROSE
