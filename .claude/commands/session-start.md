---
description: Resume from last session - Show context and next actions
---

# Start Session Workflow

Read the latest session state and help user pick next task.

## Steps:

1. **Show last session** (from SESSIONS.md):
   - Read the TOP entry only (most recent 5 lines)
   - Display:
     ```
     📋 Last Session: YYYY-MM-DD

     What: <what was done>
     Result: <outcome>
     Status: ✅ Complete
     ```

2. **Show current priorities** (from TODO.md):
   - Read all High Priority items
   - Display numbered list:
     ```
     🎯 Current Priorities:

     High:
     1. [ ] <task 1>
     2. [ ] <task 2>
     3. [ ] <task 3>

     Medium: <count> items
     Low: <count> items
     Blocked: <count> items
     ```

3. **Prompt for action**:
   ```
   Which task would you like to work on?
   - Enter number (1, 2, 3...)
   - Or describe a different task
   - Or ask me to recommend based on priority
   ```

4. **Prepare to work**:
   - Once user picks a task, acknowledge and say:
     "Let's work on: <task description>"
     "I'm ready when you are. What's the first step?"

---

**Purpose**: Minimal context load (15 lines) for LLM to resume efficiently.
