# Timed Pool Quarantine Recovery

## Problem

Three consecutive resolve or chunk timeouts currently quarantine a pool member
for the lifetime of the process. Quarantined members have no recovery deadline,
and the existing `recover()` method is not called by the running server. A pool
can therefore remain exhausted until tgshelf is restarted.

## Scope

Quarantine health is local, transient process state. It is not stored in
PostgreSQL. Each tgshelf deployment owns an independent pool: Cloudboxes uses
its `c_*` bot family and Hetzner uses its `h_*` bot family. Health in one pool
must not affect the other.

## State Machine

A member starts healthy. Three consecutive timeout errors move it to
`quarantined` with the first configured deadline. The default retry schedule is
5, 15, 30, and 60 minutes; the final duration remains the cap for later failed
probes.

When a quarantine deadline expires, one normal download request may lease one
member in `half_open`. No background or synthetic Telegram request is sent.
Only one half-open probe may exist in a pool at a time.

- A successful chunk returns the member to healthy and resets the retry stage.
- A timeout advances the retry stage and starts the next quarantine period.
- A cancelled, flooded, or channel-ineligible probe remains quarantined without
  advancing the retry stage. It can be tried again after the applicable local
  cooldown.
- Errors from work already in flight do not advance an open quarantine.

Requests are not held open for multi-minute quarantine deadlines. If no healthy
member or due probe exists, the request fails with an explicit reason and the
status surfaces the remaining time until the next local probe.

Complete recovery timelines are provided in Appendix A.

## Configuration

`download.quarantine_backoff_seconds` is a non-empty list of positive,
non-decreasing durations. Its default is:

```yaml
download:
  quarantine_backoff_seconds: [300, 900, 1800, 3600]
```

## Observability

Pool status keeps existing fields and adds member state, half-open status,
quarantine attempt, quarantine remaining time, and the pool's next probe
countdown. Logs use `[quarantine]`, `[probe]`, and `[recover]` markers.

## Compatibility

The change requires no database migration and no Telegram session changes.
Existing configurations inherit the default schedule. FloodWait cooldown and
per-channel eligibility remain separate from timeout quarantine.

## Appendix A - Operational Timelines

### A.1 - Six healthy and six quarantined bots

**Scenario:** a 12-bot pool with healthy members `H1-H6` and newly quarantined
members `Q1-Q6`. This assumes continuous traffic, success on every first probe,
and an illustrative probe duration of 3 seconds.

| Time | Half-open probe | Available bots | Quarantined bots | Event |
|---:|:---:|:---|:---|:---|
| `t+00:00` | - | `H1-H6` (6) | `Q1-Q6` (6) | The 5-minute wait begins |
| `t+05:00` | `Q1` | `H1-H6` (6) | `Q2-Q6` (5) | The `Q1` probe starts |
| `t+05:03` | `Q2` | `H1-H6`, `Q1` (7) | `Q3-Q6` (4) | `Q1` recovered |
| `t+05:06` | `Q3` | `H1-H6`, `Q1-Q2` (8) | `Q4-Q6` (3) | `Q2` recovered |
| `t+05:09` | `Q4` | `H1-H6`, `Q1-Q3` (9) | `Q5-Q6` (2) | `Q3` recovered |
| `t+05:12` | `Q5` | `H1-H6`, `Q1-Q4` (10) | `Q6` (1) | `Q4` recovered |
| `t+05:15` | `Q6` | `H1-H6`, `Q1-Q5` (11) | - | `Q5` recovered |
| `t+05:18` | - | `H1-H6`, `Q1-Q6` (12) | - | Pool fully healthy |

The six healthy bots continue serving requests throughout the sequence. Since
only one half-open probe may run at a time, the pool returns from 6 to 12
healthy members about 18 seconds after the first deadline.

### A.2 - Timeline lanes for the six bots

**Legend:** `Q` = quarantined, `P` = half-open probe, `H` = healthy.

```text
Q1  QQQQQQQQQQQQQQQ  P--> HHHHHHHHHHH
Q2  QQQQQQQQQQQQQQQ     P--> HHHHHHHH
Q3  QQQQQQQQQQQQQQQ        P--> HHHHH
Q4  QQQQQQQQQQQQQQQ           P--> HHH
Q5  QQQQQQQQQQQQQQQ              P--> H
Q6  QQQQQQQQQQQQQQQ                 P--> H
    |                |                    |
  t+00:00          t+05:00              t+05:18

H1-H6  HHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHH
```

The probe is leased to only one expired member at a time. The `H1-H6` lane
shows that the six initially healthy members remain operational throughout
recovery.

### A.3 - Probes that keep failing

**Scenario:** one bot reaches three consecutive timeouts and then fails every
probe. The backoff remains capped at 60 minutes.

| Schematic time | Event | Next state | Next probe |
|---:|:---|:---|---:|
| `t+00m` | Third consecutive timeout | Quarantine, attempt 1 | `t+05m` |
| `t+05m` | Failed probe | Quarantine 15m, attempt 2 | `t+20m` |
| `t+20m` | Failed probe | Quarantine 30m, attempt 3 | `t+50m` |
| `t+50m` | Failed probe | Quarantine 60m, attempt 4 | `t+110m` |
| `t+110m` | Failed probe | Quarantine 60m, cap reached | `t+170m` |
| `t+170m` onward | Probe still fails | Quarantine 60m | Every 60m |
| First successful probe | Chunk retrieved | Healthy, attempt reset | - |

**Timing notes:**

- Each real deadline starts when the previous probe finishes, so request
  duration may shift the schedule.
- A FloodWait cooldown may delay the probe further without advancing the
  quarantine attempt.
- Without traffic, a deadline does not generate Telegram requests: the bot
  remains eligible until a normal request starts the probe.
- Recovery has no guaranteed maximum duration. The first successful probe
  immediately returns the bot to healthy.

### A.4 - State machine for one bot

```text
[QUARANTINE 5m]
        |
        v
   [HALF_OPEN] -- success --------------------------> [HEALTHY]
        |
        +-- timeout --> [QUARANTINE 15m]
                              |
                              v
                         [HALF_OPEN] -- success -----> [HEALTHY]
                              |
                              +-- timeout --> [QUARANTINE 30m]
                                                    |
                                                    v
                                               [HALF_OPEN]
                                                    |
                       success --> [HEALTHY] <-------+-- timeout
                                                               |
                                                               v
                                                     [QUARANTINE 60m]
                                                               |
                                                               v
                                                          [HALF_OPEN]
                                                               |
                                  success --> [HEALTHY] <-------+-- timeout
                                                               |
                                                               +--> wait another 60m
```

A cancelled, flooded, or channel-ineligible probe leaves `half_open` without
advancing the attempt. The member remains quarantined and becomes eligible
again after any applicable cooldown.
