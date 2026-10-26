# Free Pilot Final Verification Deadline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enforce the documented 75-second final read-only verification deadline, including blocked or continuously dripping HTTP responses, without accepting late success or repeating mutations.

**Architecture:** Keep every mutation and the durable journal in the parent process. Run the complete final convergence loop in a separate Python subprocess, pass its PAT and expected state through a private stdin pipe, and expose only its exit status. The parent owns the monotonic deadline and kills/reaps the reader on expiry; the reader permits GET requests only and cannot inherit the journal descriptor.

**Tech Stack:** Python standard library subprocess, urllib, unittest, and the existing in-process fake HTTP server.

---

### Task 1: Reproduce a final read exceeding the deadline

**Files:**
- Modify: `scripts/tests/test_free_pilot.py`

**Step 1:** Add a regression that gives the final phase a short deadline while the fake HTTP server stalls a response longer than that budget. Assert a stopped journal, preserved attempted/confirmed steps, no mutation retry, and elapsed time bounded below the server delay. Use the normal tool flow and real local HTTP.

**Step 2:** Run `python3 -m unittest discover -s scripts/tests -p test_free_pilot.py -v` and confirm the old implementation fails the new deadline behavior.

### Task 2: Isolate and bound the final verification phase

**Files:**
- Modify: `scripts/free_pilot.py`
- Modify: `scripts/tests/test_free_pilot.py`

**Step 1:** Extract the existing final convergence checks into a reader function retaining exact options, channel inventory and paid pricing validation, and only the existing `pricing_version` exception.

**Step 2:** Add a private subprocess entry point that reads its JSON input from stdin, constructs a PAT-only GET-only client, runs the convergence reader, and exits 0 on success or nonzero on any failure. Suppress stdout/stderr and raw exceptions.

**Step 3:** Wrap that subprocess with the parent deadline. The key sequence is `deadline = time.monotonic() + PRICING_WAIT_SECONDS`, `Popen(..., stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL, close_fds=True)`, `communicate(input=payload, timeout=deadline-time.monotonic())`, and reject `time.monotonic() >= deadline` before accepting exit 0. Kill and reap on timeout or any parent exception. The parent alone writes the existing stopped/success journal records.

**Step 4:** Move fake cache and drift controls to the fake HTTP server so they apply to the independent reader process. Preserve success/no-op, exact drift checks, timeout journal, and no-extra-writes assertions.

**Step 5:** Add real stalled/slow-response coverage and verify worker cleanup, redaction, and no late success; cover reader startup/failure using narrowly scoped process seams where needed.

**Step 6:** Run `python3 -m unittest discover -s scripts/tests -p test_free_pilot.py -v` and expect all focused tests to pass.

### Task 3: Document and verify the final implementation

**Files:**
- Modify: `docs/FREE-PILOT.md`

**Step 1:** Explain that the parent bounds the full reader process, kills/reaps it at expiry, and retains sole ownership of mutations and journaling. Preserve all existing scope limits.

**Step 2:** Run `python3 -m unittest discover -s scripts/tests -v` and expect the original 229 tests plus new regressions to pass. Run the repository contract self-check command if present, `python3 -m py_compile scripts/free_pilot.py`, and `git diff --check`.

**Step 3:** Report implementation, observed test counts, and limitations for independent review. Do not commit or push; the coordinating agent handles the follow-up PR after the already-merged #40.
