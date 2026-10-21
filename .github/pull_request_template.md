<!-- Only for risk:high or risk:critical, one line here: Risk high: <what can break>. Otherwise nothing:
the risk lives in the risk:<level> label. -->
<one or two sentences: what changes and why; it stands alone. If something important was not verified, say it last>

<!-- One visual, only when the summary is not enough: What | Before | After table (max 3 rows, 12 words a cell,
no file or code names), a small Mermaid diagram for a flow, a before/after table of numbers, or a diff block
of at most 15 lines for a contract or configuration. -->
| What | Before | After |
| --- | --- | --- |
| <rule or behavior> | <before> | <after> |

<!-- Only when a release needs a real step (settings or session files, SDK dylib, download, secret, merge order):
Deploy: <the step; rollback only if it is not a plain revert>. Otherwise write nothing. -->
