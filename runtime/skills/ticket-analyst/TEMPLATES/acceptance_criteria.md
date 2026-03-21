# Acceptance Criteria Template

When generating acceptance criteria, follow these guidelines:

## Format

Each criterion should be a single, testable statement:
- Use active voice: "User can..." / "System returns..." / "Page displays..."
- Be specific about inputs, outputs, and conditions
- Include boundary conditions where relevant
- Avoid vague terms like "appropriate," "proper," "correctly" — state what correct means

## Examples

**Good:**
- "User can upload an avatar image up to 5MB in PNG, JPG, or GIF format"
- "System returns HTTP 413 when uploaded file exceeds 5MB"
- "Profile page displays the uploaded avatar at 256x256 resolution"

**Bad:**
- "Avatar upload works correctly" (what does "correctly" mean?)
- "Handle errors properly" (what errors? what does "properly" look like?)
- "Good performance" (what is the threshold?)

## Coverage Checklist

For each feature area in the ticket, ensure criteria cover:
1. **Happy path** — The main success scenario
2. **Validation** — What happens with invalid inputs
3. **Error states** — What the user sees when something fails
4. **Edge cases** — Boundary values, empty states, concurrent access
