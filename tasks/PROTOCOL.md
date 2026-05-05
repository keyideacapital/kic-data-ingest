# Operator Protocol

*A working agreement between three parties — the operator, a chat partner, and a coding agent — for shipping production code on any software project. Project-agnostic. Living document. Read by every contributor and every AI session before work begins.*

**Version:** 1.0 · **Status:** Canonical · **Audience:** humans, chat AIs, coding agents, automation

---

## How to use this document

This protocol is read at the start of every working session on every project that adopts it. Future-you, future Claudes, and future collaborators all start here. The protocol is short on purpose — long enough to remove ambiguity, short enough to actually be read.

A project that adopts this protocol places a copy of this file at its repository root or links to a canonical copy. Project-specific extensions go in a separate document (typically `RUNTIME.md` or similar) that references this one. This document does not change to suit a project; the project either follows the protocol or doesn't.

Where the protocol is silent, fall back to the project's existing conventions. Where the protocol is explicit, it overrides convention.

---

## 1. The three roles

Three roles, always distinct, never blurred. Output flows forward only — from operator to chat partner to coding agent — and across role boundaries verification is mandatory.

### Operator

The human running the project. Owns the merge decision. Owns the visual or behavioural verification on the development environment. Owns product-level choices on scope, layout, and direction.

The operator does not review individual lines of code. Does not interpret diffs. Does not run validation scripts manually. The operator's contribution is judgement at named gates — at lower frequency than the chat partner or the coding agent, but at higher authority.

### Chat partner

An AI in a conversation interface. Produces designs, mockups, diagnostic recon, deployment artifacts, deploy prompts, and verification scripts. Surfaces risk and pushes back when scope or risk is misjudged. Speaks plainly, uses the operator's authority to override its own defaults rather than asking permission for every micro-decision.

The chat partner does not deploy code. Does not run shell commands directly on the operator's machine. Does not commit, push, or open pull requests. Its outputs are artifacts; what happens to those artifacts is the coding agent's job.

### Coding agent

An AI with file system and shell access — typically Claude Code or equivalent — running locally or in the project's CI environment. Performs mechanical deployment only: applies artifacts as written, runs verification scripts, opens pull requests, executes git operations.

The coding agent does not produce designs. Does not interpret or "improve" what was produced. Does not change scope mid-task. Receives deploy prompts; reports outcomes. If a deploy prompt is ambiguous, the coding agent stops and reports rather than guessing.

---

## 2. Working pattern

The pattern below is the default for any non-trivial change. Trivial changes (typos, lint fixes, trivial dependency bumps) can skip individual steps. The chat partner judges what counts as trivial; when in doubt, follow the full pattern.

### Repo-first session

Every working session starts with the chat partner having a fresh, complete view of the codebase. For chat interfaces that don't have direct repo access, this means the operator uploads a zip of the relevant code at the start of the session. This eliminates recon roundtrips where the chat partner asks the coding agent to paste files back. The chat partner reads the codebase once, works from real ground truth thereafter.

### Design before code

For any non-trivial change, the chat partner produces a visual or structural design first — a mockup that renders in the chat, a sketch, a structural diagram, an interface shape. The design is the specification. The operator reviews and approves the design before code is produced.

This is the highest-leverage rule in the protocol. Iteration in chat is fast; iteration through code commits is slow. Most "scope creep" bugs are actually "scope unclear at design time" bugs.

### Collision check before code

When new code interacts with shared code — shared CSS, shared utilities, shared databases, shared APIs — the chat partner has the coding agent search the existing codebase for naming collisions, schema conflicts, or interface mismatches before producing artifacts. The chat partner adjusts the artifacts to avoid collisions at design time, not deploy time.

### Namespace by default

New code uses prefixed identifiers that can't collide with existing code. Page-specific identifiers prefix with the page name. Feature-specific keys prefix with the feature name. Discipline at naming time prevents emergency renames mid-deploy.

### Bulk artifact production

The chat partner produces all artifacts for one unit of work in one bundle: implementation files, patches to existing files, server-side changes if needed, a deploy prompt, a verification script. The operator receives one bundle and one decision, not a series of incremental deliverables each requiring separate approval.

### Bulk verification

A verification script runs all checks for the unit of work in one execution: syntax, structure presence, naming hygiene, wiring integrity, integration points. The script outputs one pass/fail line per check and ends with a summary. The operator sees the summary; individual checks are the script's job, not the operator's.

### Single quality gate per unit of work

The development-environment eyeball — or behavioural test, depending on what the work changes — is the one judgement call per unit. Not the diff review. Not the test output. Not the commit message. Open the URL or run the test, observe the result, approve or reject.

If a unit of work needs more than one judgement gate to ship, the unit is too large; split it.

---

## 3. Branching and deployment

The branching model is opinionated, consistent, and non-negotiable.

A long-lived `dev` branch deploys to a development environment. A long-lived `main` branch deploys to production. Every feature branch is cut from `dev`, opens its pull request against `dev`, gets merged to `dev`, gets verified on the development environment, and only then propagates to `main` via fast-forward merge with a version tag.

**Never** commit feature work directly to `main`. **Never** merge to `main` without development-environment verification first. The verification gate is non-ceremonial — it's the only gate where "looks fine on my machine" is *not* sufficient.

A project that doesn't yet have this branching model migrates to it before adopting the protocol. The migration is a single piece of work:

1. Tag current `main` as `rollback/pre-protocol`
2. Cut a `dev` branch from current HEAD
3. Configure the development-environment deploy target to follow `dev`
4. Configure production to continue following `main`
5. Document any workflow exceptions in the project's runtime document

After migration, the operator works exclusively against `dev` until verification is green. Promotion to `main` is a deliberate operator action, not an automatic CI step.

---

## 4. Coding agent automation rules

The coding agent has an "auto-mode" or equivalent that batches mechanical decisions without prompting. The rule for when this is on or off is explicit and applies to every project.

**Auto-mode on for mechanical operations.** File replacement, git add and commit and push to `dev`, syntax checks, running verification scripts, opening pull requests. These are deterministic operations with low blast radius and predictable outcomes.

**Auto-mode off for judgement moments.** Diff review before any commit to `main`. Development-environment eyeball before merge. Any moment that requires operator approval. The deploy prompt explicitly marks every "WAIT FOR APPROVAL" gate, and the coding agent stops there even when auto-mode is otherwise on.

The cost of stopping at one extra gate is bounded — minutes of waiting. The cost of automating past a gate that mattered is unbounded — hours of recovery, or worse, silent shipping of wrong work.

Auto-mode is *never* on for any operation that touches production data, modifies external integrations (API keys, webhooks, OAuth grants), or merges to `main`.

---

## 5. Definition discipline

Words used in briefs and commit messages must have unambiguous meanings established in writing.

The original failure that produced this rule was the word "reskin" being used to mean "swap CSS class names" by some parts of a codebase and "rewrite the visible page structure" by others. Three hours of work followed the wrong meaning before the gap was caught. This is the failure mode the protocol is most designed to prevent, because it produces silent waste — the work *appears* to be progressing while the operator and the AI are working on different problems.

For any term that could be ambiguous, the brief states explicitly what is meant in writing. The commit message uses the unambiguous term. The verification standard is concrete enough that a third party reviewing screenshots or behaviour could say with confidence whether the work matches the spec.

If a term has been used inconsistently in the past, it gets retired and replaced with two distinct terms for the two distinct meanings. Old terms appear in the project's lessons document (see §7) as known-ambiguous and are flagged when they appear in any brief.

---

## 6. Risk management

### Verification standards scale with blast radius

A page reskin needs a visual eyeball. A backend API change needs API testing. A change to a function that places real trades, executes real payments, or touches production user data needs real production-like signal flow before merging to `main` — not just smoke tests. Smoke tests prove the code compiles. Real flow proves the code does the right thing.

The chat partner's job is to name the appropriate verification standard for each unit of work and write the verification script accordingly. The operator's job is to confirm the standard is appropriate before authorising the work to begin.

### Wrapper-only instrumentation for high-stakes code

When a critical function needs new behaviour — timing, metrics, logging, audit trails — wrap the existing function at one site rather than modifying it in place. The wrapped function stays untouched. The wrapper preserves error propagation through inner try/catch with explicit re-throw. The blast radius collapses from "every adapter, every code path" to "one wrapper, one site."

### Backward compatibility on shared interfaces

When extending an API response or a shared schema, existing fields stay in place. New fields are additive. Other consumers of the same interface don't break when one consumer evolves. This rule is non-negotiable for any interface with multiple readers.

### Single-purpose commits

Feature commits don't include `.gitignore` changes, infrastructure tweaks, or unrelated chores. Bisect history stays clean. If something breaks, the commit that introduced the break is unambiguous.

### Cost preview before paid action

Any operation that consumes paid API calls, paid compute, or paid storage shows the operator an estimate before triggering. The operator never discovers a charge in retrospect. This applies equally to code generation runs, AI audits, image generation, and any third-party service the project routes through.

When estimates can vary by a tier or model selection (cheap vs expensive provider), the choice is presented with the cost delta visible. Escalation between tiers is **never automatic** — it always requires explicit operator consent.

---

## 7. Lessons capture

The project's `lessons.md` (or equivalent) lives in version control. Every working session that surfaces something worth remembering appends to it. "Worth remembering" includes:

- Bugs whose root cause was non-obvious
- Workflow patterns that worked or failed
- Definitions that were ambiguous and got resolved
- Tools or commands that turned out to behave unexpectedly
- Decisions that were made and the reasoning behind them
- Things future-you would want to know but won't remember

Lessons are short — one to four lines per entry — and dated. The chat partner reads `lessons.md` at the start of every session. Lessons that contradict newer lessons are not deleted; they're superseded with a note ("see entry of YYYY-MM-DD"). Future archaeology often turns up these contradictions as load-bearing context.

`lessons.md` is the institutional memory the operator's brain shouldn't have to be. If a lesson is relevant to multiple projects, it is duplicated; the protocol does not depend on cross-project sync to function.

---

## 8. Pace and capacity

Pace targets are realistic, not aspirational.

A unit of work — one page, one feature, one focused refactor — targets ninety minutes from start to merged-to-`main`, including verification on the development environment. Two units per half-day session is a reasonable target.

The pre-build collision check, the bulk verification script, and the auto-mode automation collectively make this pace possible. Skipping any of them looks faster in the moment and is slower over the session.

If a unit takes longer than ninety minutes, the chat partner stops and reports — does not push past the gate to "save time." The cause is usually scope creep or an unsurfaced collision. Naming the cause is faster than absorbing it.

If a unit consistently takes longer than ninety minutes across multiple sessions, the unit shape is wrong; revisit how units are scoped before continuing.

---

## 9. Vocabulary

Sessions establish project-specific vocabulary that all three roles use consistently. The terms below are the protocol's canonical terms; project-specific terms extend this list in the project's runtime document.

| Term | Definition |
|---|---|
| **Mockup** | A visual design produced in chat before code is written. Renders in the chat interface so the operator sees it without leaving the conversation. |
| **Artifact** | A deploy-ready file produced by the chat partner. The coding agent applies it without modification. |
| **Deploy prompt** | The written instructions the operator pastes into the coding agent. Specifies what files to apply, what verification to run, where to stop. |
| **Staging directory** | A gitignored directory inside the repository where artifacts land before being applied. Decouples download-and-extract from apply-and-commit. |
| **Verification script** | A shell script that runs all checks for a unit of work and reports pass or fail per check. |
| **Collision matrix** | A structured report from the coding agent listing which new identifiers collide with which existing code. Produced before artifacts are written. |
| **Unit of work** | A single coherent change with one verification gate. Roughly ninety minutes start to merge. |
| **Verification gate** | A named moment where work pauses for operator judgement before continuing. |
| **Rollback tag** | A git tag created at a known-good state, pushed to the remote, used as the recovery anchor if a unit fails verification. |

---

## 10. What this protocol rejects

These behaviours violate the protocol regardless of how convenient they feel in the moment:

- Approving merges on the coding agent's self-report instead of producing evidence
- Recon roundtrips where the chat partner repeatedly asks the coding agent to paste the same files
- Auto-mode papering over verification gates that matter
- Conflating different scopes of work in shared briefs
- Skipping collision checks because "it'll probably be fine"
- Bundling unrelated commits to save time
- Merging high-stakes changes without real-world verification
- Committing feature work directly to `main`
- Silent escalation to a more expensive AI tier without operator consent
- Producing or accepting work without naming what "done" means

---

## 11. What this protocol rewards

These behaviours are the working pattern. They are *not* exceptional; they are the floor:

- Designs visible to the operator before code is written
- Namespaced identifiers that can't collide with shared code
- Verification scripts that catch issues before the operator sees them
- Bulk operations that minimise the operator's decision cycles
- The development environment as the single source of truth for visual or behavioural quality
- The chat partner overriding its own defaults on the operator's authority rather than pre-emptively asking permission for every micro-decision
- Lessons captured in writing the moment they're learned
- Deploy prompts that mark every WAIT-FOR-APPROVAL gate explicitly
- Cost estimates surfaced before any paid action
- Explicit naming of what "done" means at the start of each unit, used as the verification standard at the end

---

## 12. Adoption

To adopt this protocol on a new project:

1. **Establish the three roles explicitly with the operator and the AI partners.** Write down what each role does and does not do in the project's runtime document. The chat partner produces but does not deploy. The coding agent deploys but does not produce. The operator approves at named gates.

2. **Establish the branching model.** Long-lived `dev` and `main`. Development environment deploys from `dev`. Production deploys from `main`. Feature work cuts branches from `dev`.

3. **Write a session-zero document for the project.** Vocabulary, current state, what's shipped, what's pending, what's in flight. Update at the end of every session. Future sessions read it before starting.

4. **Establish `lessons.md` (or equivalent) in the repository.** Capture mistakes and their corrections in writing as they happen. Future sessions read it before starting.

5. **Run the first unit of work end-to-end with auto-mode off.** Verify every gate. Once the operator and the AI partners have a shared rhythm, turn auto-mode on for mechanical operations. Keep it off for judgement moments forever.

6. **Reference this protocol from the project's runtime document.** The runtime document extends but does not contradict this protocol. If conflict arises, the protocol wins by default; explicit overrides must be documented and justified in the runtime document.

---

## 13. The protocol's own update rule

This document is canonical and stable. Changes are deliberate.

A change to this protocol requires:

1. A specific lesson from at least two projects suggesting the current rule is wrong or incomplete
2. A proposed revision that names what changes and why
3. A version bump and a note in the version history below

A change to this protocol does not retroactively invalidate work done under the previous version. Projects update to the new version on their next session-zero, not in the middle of a unit.

---

## Version history

| Version | Date | Notes |
|---|---|---|
| 1.0 | 2026-05-04 | Canonicalised from working pattern across BCC, tradingview-claude, L&L ops, and the lessons of Command Centre. |

---

*This document captures patterns for AI-assisted production development that prioritise operator time, prevent scope drift, and maintain code quality. The patterns work because they treat each role's contribution as authoritative within its domain and verify across domain boundaries. The protocol is the floor, not the ceiling. Projects can do more; they cannot do less.*
