# KIW-37 shadow environment prototype

Status: **partial research implementation; not operationally complete**.

`kiwit.rl.shadow.ShadowEnvironment` implements reset/observation/step/report without broker references. It accepts a supplied chronological single-session quote tape. Each row carries aware `at` and `available_at`, bid/ask, numeric features and externally generated LONG/SHORT permissions. The adapter supplying that tape must verify calendar boundaries, data provenance, quote freshness, independent decision/risk approvals and feature availability. A timestamp alone cannot prove provenance.

Only the current feature row and account state are passed to a policy. Action requests execute on the next supplied quote, with adverse slippage and fees; entry permission is rechecked at that quote. One unlevered unit, at most one position and no reversal/pyramiding are supported. Invalid actions become NO_TRADE and are counted. The final supplied executable quote settles any open position. Therefore input must contain a valid terminal settlement quote: this prototype does not yet implement missing-settlement recovery or exchange calendar validation. Never use a partial session as if it were complete.

Rewards follow the KIW-36 net-equity-change, maximum-drawdown increment and per-entry penalty formula. Closed episodes reconcile to net P&L less penalties. Resetting unfinished episodes is rejected. Reports include costs through net P&L, action/reward history, drawdown and invalid action counts.

`compare_shadow` runs supplied fixed candidate/champion policies and a mandatory NO_TRADE baseline on identical copied tape, after checking strict training/evaluation date separation. It does not train a policy or establish that an operator has kept the test set untouched. Policy artifacts, hyperparameter provenance and data embargo enforcement need a frozen experiment manifest before genuine learned-policy evaluation. Arbitrary Python policies are trusted local code, not sandboxed adversaries; they must not be given external tape or broker access.

This is a simplified simulator, not the existing production paper ledger. Pending KIW-37 work includes integrating the actual risk/paper simulator and session calendar, unresolved-close/restart behavior, complete immutable training/evaluation manifests, trained policy/champion artifact binding, and real held-out comparisons. Earlier operational gates remain pending. No learned policy has been trained, promoted or deployed.

Four synthetic tests cover observation isolation, temporal rejection, execution costs, reward reconciliation, invalid actions and repeatable comparison. These tests do not validate investment performance.
