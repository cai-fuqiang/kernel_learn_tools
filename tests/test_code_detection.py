#!/usr/bin/env python3
"""_is_code_or_data_line() regression tests with real misdetection samples."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from translate_context import _is_code_or_data_line

# Should be detected as code/data (NOT translated)
POSITIVE_SAMPLES = [
    "        _raw_spin_lock_nested+0x44/0x5c",
    "        raw_spin_rq_lock_nested+0x24/0x40",
    "        task_fork_fair+0x3c/0xac",
    "        place_entity+0x199/0x1b0",
    "(&rq->__lock){-.-.}-{2:2}",
    "(console_owner){..-.}-{0:0}",
    " kernel/sched/fair.c |    4 +++-",
    " 1 file changed, 3 insertions(+), 1 deletion(-)",
    "static void place_entity(struct cfs_rq *cfs_rq, struct sched_entity *se, int flags)",
    "#define ENQUEUE_INITIAL\t\t0x80",
    "if (sched_feat(PLACE_DEADLINE_INITIAL) && (flags & ENQUEUE_INITIAL))",
    "WARNING: CPU: 29 PID: 593474 at kernel/sched/fair.c:5250 place_entity+0x199/0x1b0",
    "Call Trace:",
    "<TASK>",
    "RIP: 0010:pick_next_task_fair+0x28c/0x498",
    "CPU: 1 PID: 1187 Comm: systemd-udevd Not tainted 6.6.0-rc4+ #7222",
    "kernel/sched/fair.c",
    "include/linux/rbtree_augmented.h",
    "[    0.000000] Booting Linux on physical CPU 0x0",
    "[12345.678901] BUG: unable to handle kernel NULL pointer",
    "  5.23%  swapper  [kernel.kallsyms]  [k] intel_idle",
    "1,234,567,890  cycles  (66.67%)",
    "sched                    1,243,911        3,947,251",
    "tick_check_preempts     12,899,049",
    "   |*--------|---------|---------|---------|----",
    "   |---*-----|---------|---------|---------|----",
    "t=0 V=1",
    "d = 6",
    "q = 2",
    "CONFIG_SMP=y",
    "CONFIG_FAIR_GROUP_SCHED=y",
    "commit 561c58efd239",
    "> +\t\tplace_entity(cfs_rq, se, flags);",
    "> -\t\tplace_entity(cfs_rq, se, 0);",
    "-> #4 (&rq->__lock){-.-.}-{2:2}:",
    "-> #3 (&p->pi_lock){-.-.}-{2:2}:",
    "RBX: 0000000000000000 RCX: ffffffff81234567",
    "EFLAGS: 00010046",
    "======================================================",
    "------------------------------------------------------",
    "   lock(&rq->__lock);",
    "Chain exists of:",
    "3 locks held by systemd-udevd/1187:",
    "#0: ffff5535ffdd2b18 (&rq->__lock){-.-.}-{2:2}, at: __schedule+0xe0/0xc40",
    "t=10 V=4",
    "./eevdf -e \"0,1024,6\" -e \"1,1024,2\"",
    "---|---------|-------*-|---------|---------|----",
    "0xffffffff81234567",
    "ffffbcc2be0c4de0",
]

# Should NOT be detected as code (should be translated)
NEGATIVE_SAMPLES = [
    "Similar, yes, but not quite the same in two ways:",
    "it's sometimes off by one entry due to ordering of operations -- this is probably fixable.",
    "Ah yes, we are still using the the scaled down value for computation",
    "You're quite right -- and I *SHOULD* have double checked my decade old patches, but alas.",
    "Well, that is embarrassing :-(",
    "I believe EEVDF will still have those issues",
    "Whether EEVDF helps us improve our CFS latency issues or not, I do like the merits of this diffstat alone",
    "We're seeing regressions from EEVDF with SPEC CPU, a database workload, and a Java workload.",
    "The much bigger problem with those bounds is this little caveat: 'in a steady state'.",
    "This allows place_entity() to consider ENQUEUE_WAKEUP and ENQUEUE_MIGRATED.",
    "At a quick glance, the EVDF scheduler organizes tasks by deadline.",
    "Since the removal of sysctl_sched_child_runs_first there is no user of this anymore.",
    "Tested for a 3 days in ~1k hosts and the warning is gone.",
]


def test_positive_samples():
    failures = []
    for i, sample in enumerate(POSITIVE_SAMPLES):
        if not _is_code_or_data_line(sample):
            failures.append(f"  MISS #{i}: {sample!r}")
    if failures:
        print(f"FAIL: {len(failures)}/{len(POSITIVE_SAMPLES)} positive samples missed:")
        for f in failures:
            print(f)
        return False
    print(f"PASS: all {len(POSITIVE_SAMPLES)} positive samples detected as code/data")
    return True


def test_negative_samples():
    failures = []
    for i, sample in enumerate(NEGATIVE_SAMPLES):
        if _is_code_or_data_line(sample):
            failures.append(f"  FALSE #{i}: {sample!r}")
    if failures:
        print(f"FAIL: {len(failures)}/{len(NEGATIVE_SAMPLES)} negative samples falsely detected:")
        for f in failures:
            print(f)
        return False
    print(f"PASS: all {len(NEGATIVE_SAMPLES)} negative samples passed through")
    return True


if __name__ == "__main__":
    ok1 = test_positive_samples()
    ok2 = test_negative_samples()
    sys.exit(0 if (ok1 and ok2) else 1)