# MISHKA Spec Kit baseline

Minimal additive Spec Kit 1.4.2 shared infrastructure, copied byte-for-byte from
the reviewed official bootstrap at
`/home/khugardin/.local/share/mishka-tools/spec-kit-bootstrap/.specify`.
The upstream MIT license is in `LICENSE.spec-kit`. The project constitution
restates Roman's existing instructions; it adds no publication authority.

Current feature: `specs/001-reliable-runpod-demo`. Its local pointer is
`.specify/feature.json` (ignored by the upstream `.gitignore`). In another
checkout, set `SPECIFY_FEATURE_DIRECTORY=specs/001-reliable-runpod-demo` when
running the scripts; they persist the pointer unless read-only mode is requested.
Feature directories and Git branches are independent in this version. A script's
`BRANCH` value may be the feature identifier; verify the actual branch with
`git branch --show-current`.

The installed official skills are outside this repository at
`/home/khugardin/.local/share/mishka-tools/spec-kit-bootstrap/.agents/skills/`:
`speckit-specify`, `speckit-plan`, `speckit-tasks`, `speckit-analyze`,
`speckit-implement`, and `speckit-converge`. Follow their full `SKILL.md` workflow;
these scripts resolve templates and prerequisites, they do not generate finished
requirements automatically. Ponytail is installed at
`/home/khugardin/.agents/skills/ponytail/SKILL.md`.

```bash
bash .specify/scripts/bash/resolve-template.sh spec-template --json
bash .specify/scripts/bash/setup-plan.sh --json
bash .specify/scripts/bash/setup-tasks.sh --json
SPECIFY_FEATURE_NO_PERSIST=1 bash .specify/scripts/bash/check-prerequisites.sh \
  --json --require-spec --require-tasks --include-tasks
```

No extensions, lifecycle hooks, agent configuration, application dependencies,
branch-changing scripts, or Git hooks were installed here. Do not run
`specify init --force` over this working tree. The baseline has not run
`speckit-implement`; `speckit-converge` must wait for implementation against its
generated tasks. Specification coverage does not establish product acceptance.
