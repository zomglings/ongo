#!/usr/bin/env bash
# print-style.sh — emit optional writing-style instructions.
#
# Both Claude Code and Codex call this script explicitly after resolving the
# installed skill directory. If writing-style.md is missing or empty, every
# mode emits nothing.
#
# Modes (the first argument, default "section"):
#
#   section            Full `## Writing style` section for SKILL.md.
#                      Heading + explainer + the inlined style block +
#                      a Transitivity paragraph + the `Subagents-only
#                      for note writes` rule.
#
#   dispatch-bullet    The bullet that goes in Auto-Expansion step 4
#                      describing the writing-style block as a prompt
#                      component for subagent dispatch.
#
#   subagent-paragraph The paragraph that goes inside the verbatim
#                      subagent self-contextualization quoted block,
#                      telling the agent how to honour the embedded
#                      style guide and forward it transitively.

set -eu

MODE="${1:-section}"

# Resolve the skill root. Prefer a Claude harness override; otherwise
# fall back to this script's own parent directory (print-style.sh lives at
# <skill-root>/scripts/print-style.sh). Lets the skill load even when the
# the host does not export CLAUDE_SKILL_DIR.
SKILL_DIR="${CLAUDE_SKILL_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
STYLE_FILE="$SKILL_DIR/writing-style.md"

if [ ! -s "$STYLE_FILE" ]; then
  exit 0
fi

case "$MODE" in
  section)
    cat <<'PROSE'
## Writing style

A writing-style guide is configured beside the installed Ongo skill. Treat the block below as controlling guidance for every piece of prose produced this tick — Slack replies, status messages, spawn announcements, and sign-off relays.

The loader enforces a 4096-character ceiling.

### Style block (verbatim from writing-style.md)

PROSE

    head -c 4096 "$STYLE_FILE"

    cat <<'PROSE'

### Transitivity

The style is inherited transitively. Every prose-generating subagent dispatched this tick must receive the same block verbatim under a `## Writing style` heading in its prompt (the dispatching loop re-cats the file at dispatch time; see Auto-Expansion step 4). A subagent that itself dispatches sub-subagents must copy the block into their prompts too, so the guide propagates the entire spawn tree. Transitivity holds regardless of what the user's `writing-style.md` says about it — this script bakes the rule in.

### Subagents-only for note writes

A consequence of having a single style-enforcement point: the loop must not write new ongo notes or substantial note edits directly. Every new publication (kind `note`, `arxiv`, `web`, etc.) and every edit longer than one paragraph goes through a subagent — the subagent is the only writer that sees the style block embedded in its prompt, so writing notes inline from the loop bypasses the very mechanism this section sets up. Trivial inline ops the loop continues to do itself: renames, dedup deletions, regeneration, kendb housekeeping, slug fixes, Slack replies of any length. The rule is about prose published to the site, not about any character of prose the loop ever emits.

PROSE
    ;;

  dispatch-bullet)
    cat <<'PROSE'
- The **writing-style block**. At dispatch time, the loop re-runs this script and embeds the returned block under a `## Writing style` heading near the top of the subagent prompt. The subagent treats it as controlling guidance and forwards the same block to any sub-subagents.
PROSE
    ;;

  subagent-paragraph)
    cat <<'PROSE'
> **Writing style.** The prompt above includes a `## Writing style` section. Treat its contents as the controlling style guide for every piece of prose you produce in this run — the note body, intermediate Slack updates, and the final sign-off. The guide is more important than your default voice; defer to it on every conflict. If you spawn any sub-subagents of your own, copy the same `## Writing style` block verbatim into their prompts so the guide propagates transitively down the spawn tree.
PROSE
    ;;

  *)
    echo "print-style.sh: unknown mode $MODE" >&2
    exit 2
    ;;
esac
