# Specifications

This directory is the **normative** layer of the repository: what the service must do, in
numbered requirements that name the test proving each one.

It does not replace the design docs. The split is deliberate:

| Where | Answers | Changes when |
| --- | --- | --- |
| [`00-plan.md`](../00-plan.md) | *Why are we building this, and in what order?* | Strategy changes |
| [`01-hld.md`](../01-hld.md) | *How does it fit together?* | Architecture changes |
| [`02-lld.md`](../02-lld.md) | *How is it built?* | Implementation changes |
| **`specs/`** | *What must be true, and what proves it?* | Behaviour changes |
| [`constitution.md`](constitution.md) | *What must never drift?* | Rarely, by amendment |

A spec cites the HLD/LLD section it derives from rather than restating it. Where a spec and a
design doc disagree about **behaviour**, the spec wins; where they disagree about
**rationale**, the design doc wins.

---

## What spec-driven development actually is

The short version: **the specification is the artifact you maintain, and code is what you
generate from it.** Not "write a doc, then code." The doc stays authoritative after the code
exists, and the link between them is mechanically checked.

Three properties separate SDD from ordinary documentation:

1. **Specs are normative, not descriptive.** A spec says *must*, not *does*. If code and spec
   disagree, one of them is a bug — and you have to decide which, deliberately.
2. **Requirements are addressable.** Every requirement has a stable id (`REQ-MUT-004`). Tests
   cite it, commits cite it, pull requests cite it. This is what makes "is this covered?" a
   query instead of an argument.
3. **Traceability is enforced.** `make trace` checks that every requirement names a real test
   and every named test exists. A requirement with no test must be explicitly declared a
   `GAP` — the honesty is compulsory, which is the whole point.

### The loop

```
constitution  →  specify  →  plan  →  tasks  →  implement  →  trace
   (rules)      (what/why)  (how)   (steps)     (code)      (proof)
```

- **constitution** — the invariants every spec is checked against. Written once, amended rarely.
- **specify** — *what* must be true and *why*, with acceptance criteria. Contains no
  implementation detail. If you can't state how you'd falsify a requirement, it isn't one yet.
- **plan** — *how*, against this codebase specifically: modules touched, contracts changed,
  alternatives rejected. Checked against the constitution before any code is written.
- **tasks** — the plan cut into independently reviewable steps, each naming the requirement it
  advances and the test it adds.
- **implement** — write the code and the tests together; the test names are already decided.
- **trace** — `make trace` proves the loop closed.

The discipline that makes it work: **the spec is written before the code and updated before
the code changes.** A spec edited to match code already written is just a changelog.

### Why it's worth the ceremony here

This service is a security boundary. "Prose that reads as consent executes nothing" is not a
feature, it is the property the propose→confirm design exists to guarantee. Properties like
that need to survive refactors by people who weren't there — which means they need to be
written down as requirements and pinned to tests, not left as tribal knowledge in a docstring.

The first reverse-engineered spec ([0001](0001-mutation-gate/spec.md)) found two real gaps
within an hour of being written. That is the return on the ceremony.

---

## Layout

```
docs/specs/
  constitution.md          the invariants; amended, not edited
  templates/               copy these to start
    spec.md  plan.md  tasks.md
  NNNN-slug/
    spec.md                required — requirements + traceability
    plan.md                required before implementing
    tasks.md               required before implementing
```

Specs are numbered in creation order and never renumbered. A superseded spec keeps its number
and gains `**Status:** Superseded by NNNN` — the id is a permanent address.

## Requirement ids

`REQ-<AREA>-<NNN>` — area is a short uppercase tag, unique per spec, stable forever.

| Area | Spec |
| --- | --- |
| `MUT` | [0001 — mutation gate](0001-mutation-gate/spec.md) |
| `SESS` | [0002 — durable sessions](0002-durable-sessions/spec.md) |
| `MGW` | [0003 — air-model-gateway integration](0003-model-gateway/spec.md) |

Ids are never reused or renumbered. A withdrawn requirement stays in the table marked
`Withdrawn`, because commit messages and test docstrings still point at it.

Each requirement carries one of:

- **MUST** / **MUST NOT** — a violation is a defect, and for Articles I, II and IV a security defect.
- **SHOULD** — deviation needs a recorded reason.
- **MAY** — genuinely optional.

## Status vocabulary

| Status | Meaning |
| --- | --- |
| `Draft` | Under discussion; not binding |
| `Accepted` | Binding; implementation may start |
| `Implemented` | Code exists and every requirement traces to a passing test |
| `Partial` | Implemented with declared `GAP`s |
| `Superseded` | Replaced; the successor is named |

## Traceability

Every spec ends with a table mapping each `REQ-` id to the test that proves it:

```
| REQ-MUT-004 | `tests/unit/test_turns.py::test_prose_that_reads_as_consent_executes_nothing` |
| REQ-MUT-009 | GAP — no behavioural test drives an expired proposal |
```

Run `make trace` (included in `make check`) to verify. It fails on a named test that does not
exist, and on a requirement that neither names a test nor declares `GAP`/`DEFERRED`. It reports
declared gaps without failing — they are tracked debt, not broken links.

## Working on a spec with Claude Code

Four slash commands drive the loop; each one re-reads the constitution first:

| Command | Does |
| --- | --- |
| `/specify <what>` | Creates `NNNN-slug/spec.md` from the template, checked against the constitution |
| `/plan <NNNN>` | Writes `plan.md` for an accepted spec |
| `/tasks <NNNN>` | Cuts the plan into reviewable steps |
| `/trace [NNNN]` | Runs the traceability check and explains failures |

## Starting a spec by hand

```bash
n=$(printf '%04d' $(( $(ls -d docs/specs/[0-9]* 2>/dev/null | wc -l) + 1 )))
mkdir -p "docs/specs/$n-my-feature"
cp docs/specs/templates/spec.md "docs/specs/$n-my-feature/spec.md"
```

Then fill it in, run `make trace`, and open it for review **before** writing code.
