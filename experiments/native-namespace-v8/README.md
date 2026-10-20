# Native namespace v8 - unfinished experiment

This source-only migration draft preserves the latest local native transport
experiment for contributor review. It is not production routing code, a service,
a deployment recipe approved for execution, or a completed cancellation test.
The package and hosted application are unchanged.

The fixed client talks to IPv4 loopback port 8002 inside one disposable native
router. A bounded host stdio adapter supports six fixed commands, strict resource
identities, finite request/cell budgets and owned-resource cleanup. Fixture
coordinates are an invented grid, not personal locations or a national dataset.

Safe local checks, with no container or network execution:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B native_review.py
```

The default command verifies the source manifest and reports proposal-only mode.
Do not run either preparation/runtime script with `--execute` under this PR.

Known gaps and limits:

- The code retains ARM64/rootless-Docker and existing-engine assumptions from
  the original experiment. Those assumptions are not a portable cloud deployment.
- Independent re-review of cleanup absence, diagnostic deadlines and protocol
  counter/identity handling is outstanding. Earlier validation used 91 mocked
  or local-pipe tests; Python 3.12 evidence was AST-only, not runtime execution.
- This v8 revision has not been staged or executed on a native host. Old fixture
  receipts cannot be reused. Any preparation/runtime test needs a new reviewed
  source-specific receipt and separate host/resource approval.
- A successful smoke run would establish only synthetic own-namespace HTTP,
  matrices and observed recovery. It would not prove production connectivity,
  host forwarding, native cancellation, queue admission, worker reclamation,
  latency or national capacity. The code explicitly keeps those conclusions
  unproved.
- No host/network change, container launch, fixture transfer, image download,
  production integration or activation is part of this preservation branch.

The ten Python/config/test files are byte-identical to the latest local v8
source. This README replaces historical local operational narrative; the
original remains untouched. `SHA256SUMS` identifies this published source tree,
including this new status document. The original local manifest SHA256 was
`518cdc2c9b6de35f6c71fb1ec13ac96875b9f0f6b5d8a00d795fd4bf6d651b18`.
Both manifests concern source provenance, not execution approval or readiness.

No private host receipts, captured data, credentials, databases, large graphs,
earlier duplicate variants or benchmark results are included. Further review
and code work should use the shared cloud development environment.
