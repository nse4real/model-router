# Dataset v1

153 prompts: 150 original assistant-authored examples under the repository MIT
licence, and three selected GSM8K test questions under MIT. Original prompts are
individually written fixture prompts, not generated numeric template expansions.
They are not human-rated and their tier labels are author-assigned diagnostics.
No held-out prompt has been sent to a provider by this implementation.

Frozen data: benchmark/data/prompts.jsonl. Assignment seed: 13.
SHA-256: 95833de696995a30777be087da8caea9670e566b160a44c106661dc00c3d71da.
Manifest: benchmark/data/manifest.json. Counts: dev 43, calibration 50, held-out 60.
Whitespace-normalized, case-folded prompt text and IDs are unique across splits.
The file contains the saved assignment; loading never re-randomizes it.

Public source: [GSM8K test data](https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl).
Licence checked on 2026-10-16 at the [upstream MIT notice](https://raw.githubusercontent.com/openai/grade-school-math/master/LICENSE).
Copyright 2021 OpenAI; complete notice is preserved in
benchmark/data/GSM8K_LICENSE.txt. Only the robe/fiber, James/sprints and
Claire/omelet question texts are included, with whitespace normalized. Answers
and the remainder of GSM8K were not imported. The frozen local bytes and checksum
are authoritative; the upstream links point at a moving branch.

The public base is intentionally small. This convenience fixture cannot establish
broad routing quality. No benchmark representativeness or model savings is claimed.

The 60 held-out prompt IDs for human B/A pairs and the fixed 30-prompt repeat
subset are recorded in the manifest before scoring. Calibration exports choose
25 A/B pairs from the 50 calibration prompts by seeded round-robin tier/task
strata. They produce 50 individual rating slots, blinded left/right order, and a
separate private mapping. Give only ratings-blinded.json to a rater.

The classifier diagnostic uses dev only. With frozen thresholds [2,5], the command
python -m benchmark.diagnostics produced 20/43 correct tier labels (46.51%).
This reports heuristic agreement, not answer quality. No thresholds were tuned
using calibration or held-out outputs.
