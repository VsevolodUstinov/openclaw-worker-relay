# Architect Prompts

Prompts for the competing-architects pattern. Include these in the external worker task prompt for L-complexity designs.

## Preamble (always include)

```text
You are designing a module/feature. You have gathered codebase context already (see above).

Your job: produce 2-3 competing architecture options, each optimizing for a DIFFERENT goal.
The options must be genuinely different approaches, not the same idea with minor variations.

For each option, you MUST provide:
1. Approach — how it works (3-5 sentences)
2. Interfaces — concrete inputs, outputs, data models
3. Files to create/modify — exact paths
4. Effort — S / M / L (relative complexity, not calendar time)
5. Risks — what could go wrong with THIS approach specifically
6. Trade-off — what you gain vs what you give up

After all options, produce:
- A comparison table (effort, risk, maintainability, reuse, extensibility, complexity)
- A recommendation with the deciding factor named explicitly
- Fill the "Chosen Approach" section with the recommended option — this is autonomous, no human gate

Write the full result as a DESIGN.md following the template provided.
```

## Option A: Minimal Architect

```text
OPTION A — MINIMAL:
You MUST optimize for the smallest possible change that fulfills the requirements.
- Maximize reuse of existing code, patterns, and conventions in this codebase
- Minimize new files, new abstractions, and new dependencies
- Prefer extending existing modules over creating new ones
- Accept ugliness, coupling, or tech debt if it keeps the change small
- If the feature can be a 20-line addition to an existing file, that's better than a new module
Your goal: "what's the least work that gets this done correctly?"
```

## Option B: Clean Architect

```text
OPTION B — CLEAN:
You MUST optimize for maintainability, clarity, and clean architecture.
- Design proper abstractions and separation of concerns
- Create clear interfaces that could be tested independently
- Follow established architectural patterns (even if the codebase doesn't always)
- Prefer correctness and readability over implementation speed
- Accept that this will take more initial effort
Your goal: "what would a senior engineer be proud to maintain for 2 years?"
```

## Option C: Pragmatic Architect

```text
OPTION C — PRAGMATIC (include only if Options A and B are meaningfully different):
You MUST balance implementation speed with code quality.
- Find the 80/20 point: good enough architecture without over-engineering
- Abstract where it matters (public interfaces, data models), keep it simple elsewhere
- Prefer convention over configuration
- Accept small trade-offs on both purity and speed
Your goal: "what ships reliably this week and doesn't create regret next month?"
```

## Self-Verification (always append)

```text
SELF-VERIFICATION — After writing the DESIGN.md:
1. Re-read it from the top
2. Check: are the options genuinely different approaches, or the same idea restated?
3. Check: are interfaces concrete (data types, field names, formats), not vague ("takes input and produces output")?
4. Check: are DoD items testable by running a command or inspecting a file?
5. Check: is the rollback plan specific enough to execute without thinking?
6. Check: is the document shorter than the code it will describe?
If any check fails, fix it before finishing.
```

## Single-Architect Prompt (for M-complexity)

For M-complexity tasks, use this instead of the competing options:

```text
You are designing a module/feature. You have gathered codebase context already (see above).

Your job: produce ONE recommended architecture approach.

Balance implementation speed with code quality — find the pragmatic middle ground.
Maximize reuse of existing patterns in this codebase.

You MUST provide:
1. Approach — how it works (3-5 sentences)
2. Interfaces — concrete inputs, outputs, data models
3. Files to create/modify — exact paths
4. Effort — S / M / L (relative complexity, not calendar time)
5. Risks — what could go wrong, with mitigations
6. Definition of Done — concrete, testable items
7. Implementation stages with gates
8. Rollback plan — specific steps
9. Validation steps — how to verify correct implementation

Write the full result as a DESIGN.md following the template provided.
Skip the "Architecture Options" comparison table — go directly to "Chosen Approach."
```

## Assembling the Full Prompt

When building the external worker task prompt for design:

```
1. Research opener (if context gathering needed)
2. Context: what module, what it does, constraints
3. Instruction: "Read the following codebase locations to understand existing patterns: <paths>"
4. If local instructions define environment-specific workspace/bootstrap rules, include them here (monorepo map, repo topology, department rules, secrets policy, git workflow). If no such local workspace exists, skip this step.
5. Preamble (above)
6. Option A + Option B + Option C prompts (L-complexity) OR Single-Architect prompt (M-complexity)
7. Self-verification (above)
8. DESIGN.md template (from references/design-template.md)
9. Output instruction: "Write the complete DESIGN.md to <target-directory>/DESIGN.md"
```
