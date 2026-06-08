// Alloy 6 model of nixbot/scheduler.py (JobScheduler.run_incremental):
// jobs arrive in batches from a still-running evaluation and are
// dispatched in dependency order; failures propagate to dependents;
// an abort cancels everything unfinished.
//
// Run headless:
//   java -Djava.awt.headless=true -jar alloy6.jar exec -c '*' scheduler.als

module scheduler

enum Status { Unarrived, Pending, Running, Succeeded, Failed, DepFailed, Cancelled }

sig Job {
  deps: set Job,
  var status: one Status
}

fact Acyclic { no j: Job | j in j.^deps }

// Whether the evaluation still produces batches.
var lone sig EvalOpen {}

// Set once the build ended abnormally (eval failure or supersedure):
// the orchestrator cancels the scheduler task and settles unfinished
// attribute rows (settle_unfinished_attributes).
var lone sig Aborted {}

pred init {
  all j: Job | j.status = Unarrived
  some EvalOpen
  no Aborted
}

fun failed: set Job { status.Failed + status.DepFailed }
fun unfinished: set Job { status.Pending + status.Running }

// Transitive pending dependents of j, reachable through pending jobs
// only (get_failed_dependents expands only through unfinished jobs).
fun pendingDependents[j: Job]: set Job {
  { k: Job | k.status = Pending and j in k.^(deps :> (status.Pending + j)) }
}

pred flagsUnchanged {
  EvalOpen' = EvalOpen
  Aborted' = Aborted
}

pred unchangedExcept[js: set Job] {
  all j: Job - js | j.status' = j.status
}

// A single eval result arrives (the implementation ingests batches;
// per-job arrival covers the same interleavings). Delivery is
// at-least-once: the orchestrator re-sends the complete eval result
// as a final batch, so any job can arrive again. _ingest drops
// already-seen attrs (seen_attrs); before that check, redelivery
// re-queued settled jobs: with the else branch set to Pending,
// TerminalImmutable, DispatchNeverRunsOnFailedDep and
// DepFailedJustified all fail - the production incident (settled
// attrs rebuilt at the eval-to-building transition).
// For new jobs, _ingest settles failed-drv dependents to a fixpoint
// over new and pending jobs.
pred ingest[j: Job] {
  some EvalOpen
  j.status = Unarrived implies {
    (some j.deps & failed) implies {
      j.status' = DepFailed
      all k: pendingDependents[j] | k.status' = DepFailed
      unchangedExcept[j + pendingDependents[j]]
    } else {
      j.status' = Pending
      unchangedExcept[j]
    }
  } else {
    // Redelivery of a seen attr: dropped, nothing changes.
    status' = status
  }
  flagsUnchanged
}

// _dispatch_ready: closure only contains unfinished jobs, so a job is
// ready once no dependency is pending or running.
pred dispatch[j: Job] {
  j.status = Pending
  no j.deps & unfinished
  j.status' = Running
  unchangedExcept[j]
  flagsUnchanged
}

pred finishOk[j: Job] {
  j.status = Running
  // Nix guarantee: success implies the whole drv closure realized,
  // impossible if a transitive dependency failed permanently.
  no j.^deps & status.Failed
  j.status' = Succeeded
  unchangedExcept[j]
  flagsUnchanged
}

// _finish_job + fail_dependents.
pred finishFail[j: Job] {
  j.status = Running
  // Nix guarantee: a drv inside an already-realized closure is in the
  // store; building it again trivially succeeds, it cannot fail.
  j not in (status.Succeeded).^deps
  j.status' = Failed
  all k: pendingDependents[j] | k.status' = DepFailed
  unchangedExcept[j + pendingDependents[j]]
  flagsUnchanged
}

// The end-of-input sentinel.
pred closeEval {
  some EvalOpen
  no EvalOpen'
  status' = status
  Aborted' = Aborted
}

// Abnormal end (eval failure, superseded build): the orchestrator
// cancels the scheduler task - which cancels in-flight executor tasks -
// and settle_unfinished_attributes flips pending rows to cancelled.
// Unarrived jobs were never recorded, they stay invisible.
pred abort {
  some EvalOpen
  no Aborted
  some Aborted'
  no EvalOpen'
  all j: Job |
    j.status in Pending + Running
      implies j.status' = Cancelled
      else j.status' = j.status
}

pred stutter {
  status' = status
  flagsUnchanged
}

fact Behavior {
  init
  always (
    (some j: Job | ingest[j] or dispatch[j] or finishOk[j] or finishFail[j])
    or closeEval
    or abort
    or stutter
  )
}

// ---------------------------------------------------------------------
// Safety

// A job must never start building while a dependency already failed.
// Caught two real _ingest bugs: dependents arriving before their
// dependency-failed sibling (same batch or a later one) dispatched.
assert DispatchNeverRunsOnFailedDep {
  always all j: Job |
    (j.status = Pending and j.status' = Running)
      implies no j.deps & failed
}
check DispatchNeverRunsOnFailedDep for 5 but 12 steps

// DepFailed is always justified by a transitively failed dependency.
assert DepFailedJustified {
  always all j: Job |
    j.status = DepFailed implies some j.^deps & failed
}
check DepFailedJustified for 5 but 12 steps

// A succeeded job never has a permanently failed drv anywhere in its
// dependency closure (follows from the nix guarantee on finishOk plus
// terminal immutability).
assert SucceededClosureRealized {
  always all j: Job |
    j.status = Succeeded implies no j.^deps & status.Failed
}
check SucceededClosureRealized for 5 but 12 steps

// Terminal statuses never change.
assert TerminalImmutable {
  always all j: Job |
    j.status in Succeeded + Failed + DepFailed + Cancelled
      implies j.status' = j.status
}
check TerminalImmutable for 5 but 12 steps

// An aborted build never shows running or pending work again
// (the stuck-rows bug: settle_unfinished_attributes was missing in
// the orchestrator's abnormal paths).
assert AbortSettlesEverything {
  always (some Aborted implies no unfinished)
}
check AbortSettlesEverything for 5 but 12 steps

// After an abort nothing is ever ingested or dispatched again.
assert AbortIsFinal {
  always (some Aborted implies always status' = status)
}
check AbortIsFinal for 5 but 12 steps

// After the sentinel, unfinished jobs always have an enabled event:
// the scheduler cannot deadlock (acyclic deps guarantee a dispatchable
// or finishable job).
assert NoDeadlock {
  always (
    (no EvalOpen and some unfinished) implies
      (some j: Job | (j.status = Pending and no j.deps & unfinished) or j.status = Running)
  )
}
check NoDeadlock for 5 but 12 steps

// ---------------------------------------------------------------------
// Liveness (under weak fairness: enabled work is eventually performed)

pred fairness {
  always (
    (some j: Job | (j.status = Pending and no j.deps & unfinished) or j.status = Running)
      implies eventually not stutter
  )
  eventually no EvalOpen
}

// Every arrived job eventually settles.
assert AllArrivedSettle {
  fairness implies
    eventually always (no unfinished)
}
check AllArrivedSettle for 4 but 14 steps

// Sanity: a full run where everything succeeds is possible.
run HappyPath {
  eventually (no EvalOpen and Job = status.Succeeded and some Job)
} for 3 but 10 steps

// Sanity: an abort can interrupt a busy scheduler.
run AbortPath {
  eventually (some status.Running and some status.Pending and after some Aborted)
} for 3 but 10 steps
