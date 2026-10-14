# v0.2.0 — policy and journal source milestone

This release publishes the reviewed Stage B/E **source foundations**, not a production rollout or completed client-isolation/promotion system. `VERSION` is the repository release version; no package/version shim is introduced.

## Included
- Deterministic common → role → client policy compilation, narrowing/revocation and protected-path coverage, explicit offset-bearing approvals, canonical snapshots and report-only drift.
- The existing deploy surface's read-only roster/policy check: exact roster selection, stable identity/role admission, ambiguous/duplicate-claim refusal and non-vacuous coverage.
- Tenant-local journal, real OS locking, atomic fsynced publication, read-time schema/envelope/approval uniqueness, commit-sequence readiness and reference-based attestations. Duplicate JSON keys and malformed/unencodable values refuse rather than silently alter reports.
- Portable NFC/case admission refuses all conflicting legacy siblings, even exact matches on sensitive volumes; conflict-free exact reopen is retained. Lock-file opening has contextual path refusal and same-object recovery.

## Verified source gates
Both requested reviewers returned SHIP with complete, unclipped evidence and reported-match routes: Opus 5.5 + GLM 5.3 for B595 and E649. Reviewed compiler/consumer/journal source hashes match the combined tree.

`python -m pytest tests/ -q -rs`: **649 collected on each native OS**.
- macOS / Python 3.11.15: `643 passed, 6 skipped in 40.56s`, exit 0.
- Windows / Python 3.11.16: `644 passed, 5 skipped in 38.61s`, exit 0.
- Additional real case-sensitive APFS matrix: `26 passed, 2 skipped in 0.86s`; skips concern NFC/NFD behavior, not the case-conflict branch.
- Unchanged source-only estate leak guard passed on both OSes.
- Actual pre-change CLI writes a legacy snapshot file with naive timestamps; current load/drift preserves interpretation and bytes, reports expiry without stored-hash mismatch. Six future-window offset/seam probes pass on each OS.

Red/green regressions and historical failed candidates remain preserved by the operator. Examples: parser-limit matrix 31 failures → 31 passes; duplicate-key matrix 7 failures/1 positive → 8 passes; real directory-loss lock-open matrix 1 failure/1 volume skip → 1 pass/1 volume skip.

## Boundaries and retained follow-ups
- Policy ALLOW is identity/role/capability-presence reporting, not path-specific write permission or runtime enforcement. Successful drift execution does not mean absence of drift.
- Declared VM identities do not prove memory/credential isolation. Content hashes are not signatures; editing both data and hash is not detected. Signed policy/key lifecycle and actual isolation remain held.
- Names and `verified` flags are caller attestations, not authenticated human identity or independently contacted verification. Recorded promotion completeness authorises no execution or self-editing.
- Journal admission/lock-opening path errors are contextual; some library publication errors retain native OS exception types. CLI refuses those errors. Private publication-hook wording, diagnostic fidelity and contention-budget attribution are minor follow-ups, not newly claimed guarantees.
- Cross-process/open-handle tests are in the full suite; focused review packets do not contain every retained test. No Windows case-sensitive-directory or ACL-equivalence proof is claimed from APFS/chmod tests.
- Directory fsync/power-loss durability, trusted verification/promotion consumers, live canaries and deployment are not completed by source publication.

The feature branch and tag may be published as a **prerelease**, with a **draft PR**. Default-branch merge and live cutover remain held.
