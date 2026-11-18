# Leader demo — running it from VS Code

**The story in one line:** a customer never has to repeat themselves when they move between web chat, voice and email.

## Before the meeting (10 minutes)

1. Open the folder in VS Code: **File → Open Folder… → `cx-memory-poc`**. Accept the prompt to install the recommended extensions (Python and REST Client).
2. Check that the status bar shows **Python 3.12 ('.venv')**. If it doesn't: `Cmd+Shift+P` → *Python: Select Interpreter* → `.venv`.
3. `Cmd+Shift+P` → **Tasks: Run Task** and run, in order:
   - `0 · Offline tests (21)`: everything should pass
   - `1 · Verify GCP environment`: every check should PASS (needs `gcloud auth application-default login`)
   - `2 · Ingest profiles into Memory Bank (--reset)`: gives a clean, repeatable starting state
4. Do a dry run with **▶ Demo console — OFFLINE rehearsal** (see below). It needs no GCP connection and uses a scripted model.

> **Backup plan:** if the network or GCP fails during the meeting, switch to the OFFLINE config. The flow and UI are the same, but the answers are scripted.

## During the meeting

Press `Cmd+Shift+D` (Run and Debug), choose **▶ Demo console — LIVE (Vertex + Memory Bank)** and press **F5**.
The demo console opens in your browser at http://localhost:8080. To show it inside VS Code instead, use `Cmd+Shift+P` → *Simple Browser: Show* → `http://localhost:8080`.

Run the steps in the **browser console**, which has three channels side by side plus the Memory Bank panel. If you'd rather show the raw API, open `demo/demo.http` and click **Send Request** above each step.

| # | Channel | Say / send | What to point out |
|---|---|---|---|
| 1 | Web chat | "I'm flying to Lisbon on November 10, please remember that. Will my card work there?" | The `save_customer_fact` tool fires and the fact appears in the Memory Bank panel |
| 2 | Voice | "Hi, I'm calling about my trip." | **The key moment:** the agent brings up Lisbon without being told, briefly and in a voice style |
| 3 | Email | "Any update on the hotel charge?" | Formal email style: "Dear Maya," cites DSP-5521 and is signed "Customer Care" |
| 4 | Email | "Only call me after 6pm from now on." | The preference is saved to memory |
| 5 | New chat | "What have I told you about travel and when to call me?" | Both answers come from long-term memory |
| 6 | Voice as Daniel | "Where's my replacement card?" | **Privacy:** nothing of Maya's leaks to another customer |
| 7 | (optional) | `demo/demo.http` step 7 | All three channels served at once from one memory |

Allow about 6 minutes. Pause briefly before step 5, because Memory Bank extraction is asynchronous and takes a few seconds.

## Talking points

- **One memory, three channels:** each channel resolves the customer from its own identifier (Account ID, caller number or email address) to a single customer ID.
- **Two tiers:** real-time context makes a chat turn visible to a voice call immediately, and Memory Bank holds durable facts that are deduplicated.
- **Channel-appropriate:** the same facts, phrased the right way for chat, voice or email.
- **Safe by design:** full card and account numbers and SSNs are redacted before saving. Synthetic data only.

## Evidence for the slides

- Task `4 · Storyline benchmark (7 metrics × 3 runs)` writes `eval/results/*.json`.
- Task `5 · ADK rubric evals (all 3 agents)` runs the rubric judge for each agent.

## Where things are in VS Code

| Want to show | Open |
|---|---|
| Architecture | `README.md` (`Cmd+Shift+V` for preview) |
| Routing logic | `cx_memory/gateway.py` |
| The three agents | `cx_memory/agents.py` |
| Memory tools | `cx_memory/tools.py`, `cx_memory/callbacks.py` |
| Synthetic source data | `data/source_systems/` |

**Tip:** set a breakpoint in `cx_memory/gateway.py` → `handle()`. During the LIVE run, VS Code pauses on each request so you can show identity resolution and routing step by step. Remove it before the main run-through.
