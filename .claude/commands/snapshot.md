---
description: End session - Create snapshot, update logs, commit and push to GitHub
---

# End Session Workflow

This command does EVERYTHING to properly end a session following the "one problem, one session, one update" discipline.

## Steps:

1. **Create local snapshot** (for recovery):
   - Review this entire session from end to end
   - Generate 3-word description for filename
   - Create `.claude/snapshots/YYYY-MM-DD-HHMMSS_<description>.md` with:
     - Problem solved
     - Metrics/validation
     - Modified files
     - Next steps

2. **Update SESSIONS.md** (for LLM resume):
   - Add new entry at TOP of file (most recent first)
   - Format (exactly 5 lines):
     ```
     ## YYYY-MM-DD: <Problem Title>
     **Status**: ✅ Complete
     **Commit**: (pending)
     **What**: <one sentence what was done>
     **Result**: <one sentence outcome/metrics>
     **Next**: <one sentence next steps>
     ```

3. **Update TODO.md** (for LLM next action):
   - Determine smartly from session: "Which TODOs were completed in this session?"
   - Determine smartly from session: "Any new TODOs?"
   - Update the file:
     - Mark completed items as done
     - Add new items to appropriate priority section
     - Update "Last updated" date

4. **Execute git commit and push**:
   - Show summary of changes (`git diff --stat`)
   - Stage all changes: `git add .`
   - Generate commit message with format:
     ```
     feat/fix/docs: <title>

     <description>

     🤖 Generated with [Claude Code](https://claude.com/claude-code)

     Co-Authored-By: Claude <noreply@anthropic.com>
     ```
   - Commit the changes
   - Push to remote: `git push`
   - If push fails:
     - Show error message
     - Ask user: "Push failed. Would you like to retry?"
     - If yes, retry `git push`
     - If no, continue (mark session complete but note push failed)
   - After successful push:
     - Update SESSIONS.md with commit hash
     - Commit and push the hash update: `git add .claude/SESSIONS.md && git commit -m "Update session log with commit hash" && git push`

5. **Remind**:
   - "✅ Session complete and pushed to GitHub! Type `/clear` for fresh context"
   - "Next session: Start with `/session-start` to resume from where you left off"

---

**Keep snapshots ≤300 tokens. Focus on scannability.**