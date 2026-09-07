---
name: github-ai-benchmark
description: Discover GitHub AI Skill and Agent candidates, freeze repository provenance, plan isolated reproduction, validate real-run evidence, and render a narrow adoption report.
---

# GitHub AI benchmark

Use this skill when a user wants to discover, install, reproduce, compare, or recommend GitHub AI Skills or Agents.

## Required order

1. Collect a timestamped discovery snapshot from `workflow_asset/discovery-config.json`.
2. Describe momentum only when two comparable snapshots exist.
3. Apply the shortlist hard gates; a shortlist is not an adoption result.
4. Write the ordinary-user task and desired delivery before installation.
5. Freeze the official repository, detected license, README, and a 40-character commit SHA.
6. Create an isolated run package from `workflow_asset/benchmark-case.json` or another fully frozen case.
7. Run untrusted code only in a disposable container, VM, or Windows Sandbox with no personal data or secrets.
8. Preserve environment, install log, frozen input, raw run log, actual result, and human verdict.
9. Validate the same-run chain before reporting or comparison.
10. State only the narrow claim allowed by the receipt.
11. Export a read-only FLOW handoff; do not write existing topic tables, approvals, or production state.
12. Build one public repository bundle per evaluated work from owned original code and sanitized examples only. Do not copy upstream source or installed skills.

## Stop

Stop at unknown license, wrong repository identity, missing immutable commit, host-machine install, real credentials, personal data, admin privileges, payment, external sending, deletion, or permission changes. Remote publication additionally requires the exact GitHub owner/repository and current upload authorization; without them, keep the bundle at `draft_ready`.

Never translate stars into quality, an official demo into a local run, one success into general reliability, or one failure into a repository-wide defect.
