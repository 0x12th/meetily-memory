# Product evaluation decisions

The private real-corpus evaluation keeps `context=2` available as an explicit
neighbor-expansion option, but it does not justify using that expansion as the
default context behavior. Only 2 of 10 new context tasks became practically
easier; the gate required at least 3, with no critical regressions.

The experimental task list is not a supported user workflow. A full private
review classified 15 of 72 current signals as useful tasks (20.8%), below the
80% product threshold. The extractor is not tuned against that same labeled
corpus; any future attempt requires validation on new meetings.

The question sets, labels, source references, and detailed analyses stay under
ignored `.docs/eval/` and are not published with the repository.
