"""ecbwatch.py — ECB hike/cut/hold probabilities per Governing Council
meeting, derived from CME 3M €STR futures (ESR) settlements. D-006.

METHODOLOGY (research-verified 2026-09-10; full write-up in
docs/methodology/estrwatch.md):

- ESR settle = 100 − expected arithmetic-average €STR over the contract's
  Reference Quarter [third-Wednesday of the contract month, +3 months).
  NOTE the contract month names the START of accrual (verified empirically:
  JUN26 implied 2.1925 matches realized Jun-17→Sep-9 €STR 2.18-2.19).
  xccy._imm_approx is 2 days early for ESR (third MONDAY) and is NOT used
  here — see L11 in ISSUE.md D-006.
- ECB decisions take effect the first Wednesday AFTER the Thursday
  announcement (start of the new reserve-maintenance period; verified: €STR
  jumped on 2026-06-17, six days after the 2026-06-11 decision).
- 8 meetings/year vs 4 quarterly contracts → one contract spans 1-2
  meetings, so a JOINT solve is required: day-weighted least squares over
  all contracts simultaneously (generalizes the FedWatch day-weighting
  N/(M+N) to multiple jumps; the single-meeting case reduces exactly to
  the fedwatch running-rate algebra — proven by unit test).
- Probabilities use the official CME FedWatch characteristic/mantissa
  convention on a 25bp grid: expected moves E = δ/0.25, k = floor(|E|),
  m = frac(|E|) → P(k×25bp) = 1−m, P((k+1)×25bp) = m.
- Anchor = the latest €STR FIXING (ECB:ESTR), never DFR−spread: ±5bp anchor
  error moves probabilities ±20pp. The €STR−DFR basis is frozen per
  snapshot (the official CME €STRWatch convention); it cancels in rate
  CHANGES, so implied DFR after meeting j = dfr_now + Σ δ_i.
- Residual gate: RMS(A·δ̂ − b) ≤ 3bp else the snapshot is flagged degraded
  and the brief prints a ⚠ next to the number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

# (decision_day) — monetary-policy meetings only, verified against
# ecb.europa.eu/press/calendars/mgcgc (Accessed 2026-09-10). Non-monetary
# GC meetings (2026-09-30, 2026-11-25; 2027 virtual) are deliberately
# absent — that IS the filter. Implementation days are COMPUTED (first
# Wednesday strictly after the decision), never transcribed.
ECB_GC_DECISIONS = [
    date(2026, 9, 10),
    date(2026, 10, 29),
    date(2026, 12, 17),
    date(2027, 2, 4),
    date(2027, 3, 18),
    date(2027, 4, 29),
    date(2027, 6, 10),
    date(2027, 7, 22),
    date(2027, 9, 9),
    date(2027, 10, 28),
    date(2027, 12, 16),
]

_QUARTERLY = {"MAR", "JUN", "SEP", "DEC"}
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_HOLD_THRESHOLD_BP = 1.0  # |δ| below this = hold 100% (fedwatch convention)
RMS_GATE_BP = 3.0


@dataclass
class ECBMeetingProb:
    meeting_date: date  # decision day (Thursday)
    impl_date: date  # effective Wednesday
    prob_ease: float
    prob_hold: float
    prob_hike: float
    implied_rate: float | None  # implied DFR after this meeting (None if no DFR)
    expected_moves: float  # signed δ/25bp
    delta_bp: float
    exact: bool  # sole unknown in its reference quarter
    noise_amp: float | None  # 1/|w1−w2| when sharing a quarter
    sizes: dict = field(default_factory=dict)  # {move_bp: prob} on the 25bp grid


def _third_wednesday(y: int, m: int) -> date:
    """IMM Wednesday convention (CME Rulebook Ch.480: the Reference Quarter
    runs from the third Wednesday of the delivery month).

    REVIEW-CAUGHT (P1): the third Wednesday always lies in [15, 21] —
    starting the walk at day 14 returns the SECOND Wednesday whenever the
    month begins on a Thursday (JUN-2028, MAR-2029, SEP-2033... — four
    contracts in today's own strip mis-windowed, silently wrong weights
    from 2028 since the error is self-consistent). First-Wednesday + 14."""
    first = date(y, m, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7 + 14)


def reference_quarter(month_token: str) -> tuple[date, date] | None:
    """'SEP26' or 'SEP 26' (cme_settlements.month) -> (2026-09-16, 2026-12-16);
    None for non-quarterly/invalid."""
    token = "".join(month_token.split()).upper()
    if len(token) < 5 or token[:3] not in _QUARTERLY:
        return None
    try:
        y = 2000 + int(token[3:5])
    except ValueError:
        return None
    s = _third_wednesday(y, _MONTHS[token[:3]])
    e_m, e_y = _MONTHS[token[:3]] + 3, y
    if e_m > 12:
        e_m, e_y = e_m - 12, e_y + 1
    return s, _third_wednesday(e_y, e_m)


def implementation_date(decision_day: date) -> date:
    """First Wednesday strictly after the Thursday decision (RMP start)."""
    d = decision_day + timedelta(days=1)
    while d.weekday() != 2:
        d += timedelta(days=1)
    return d


def _solve_minnorm(A: list[list[float]], b: list[float]) -> tuple[list[float], int] | None:
    """Minimum-norm least squares via numpy's SVD-based pseudoinverse.

    WHY not plain OLS: the design matrix is STRUCTURALLY rank-deficient —
    8 meetings/year vs 4 quarterly contracts means two meetings inside one
    reference quarter are distinguishable only through their day-weight
    difference in that single row (verified live 2026-09-10: rank 7 of 11,
    four exact null directions; plain normal equations collapse to 1e-15
    pivots with garbage). The minimum-norm solution is the unique vector
    with ZERO null-space component: the least arbitrary choice, and its
    front meetings (the only ones the brief shows) sit in the
    well-identified subspace (top singular values).

    Returns (deltas, effective_rank); numpy is a declared dependency
    (already present transitively via pandas — made explicit for this)."""
    import numpy as np

    if not A or not A[0]:
        return None
    sol, _res, rank, _sv = np.linalg.lstsq(
        np.array(A, dtype=float), np.array(b, dtype=float), rcond=1e-6
    )
    return [float(x) for x in sol], int(rank)


def _carry_fixings(fixings: dict[date, float], s: date, a: date) -> dict[date, float]:
    """Weekend-carry: Sat/Sun take the Friday fixing (settlement averages
    over calendar days). TARGET holidays are NOT carried in v1 (<0.5bp per
    quarter — documented limitation L5)."""
    out: dict[date, float] = {}
    for d in _daterange(s, a + timedelta(days=1)):
        if d in fixings:
            out[d] = fixings[d]
        elif d.weekday() >= 5:
            # walk back to the Friday fixing — bounded by the window start;
            # a hole before the first known fixing falls back to the anchor
            g = d
            while g >= s and (g.weekday() >= 5 or g not in fixings):
                g -= timedelta(days=1)
            out[d] = fixings[g] if g >= s else None
        else:
            out[d] = None  # missing weekday fixing — caller substitutes r_a
    return out


def _daterange(s: date, e: date):
    d = s
    while d < e:
        yield d
        d += timedelta(days=1)


def compute(
    settlements: dict[str, float],
    estr: float,
    estr_asof: date,
    fixings: dict[date, float] | None = None,
    dfr: float | None = None,
    decisions: list[date] | None = None,
) -> tuple[list[ECBMeetingProb], dict]:
    """Settlements {month_token: settle} → per-meeting probabilities.

    estr/estr_asof: the latest €STR fixing (anchor, percent). fixings:
    {date: pct} for realized days inside a running front quarter (optional;
    missing weekday fixings fall back to the anchor). dfr: current deposit
    rate for display-level implied rates (probabilities live in €STR space
    and never depend on it).
    """
    diag: dict = {"convention": "joint-lstsq-minnorm-25bp-grid", "flags": []}
    if dfr is not None:
        diag["basis_bp"] = round((estr - dfr) * 100, 2)
        if abs(estr - dfr) * 100 > 15:
            diag["flags"].append("basis_wide")

    # contract windows: quarterly tokens only, drop expired (CME keeps
    # printing settlements of expired contracts — JUN26 still in the
    # 2026-09-09 strip) + light plausibility gate (review P2: a corrupt
    # settle yields a confident wrong headline the RMS gate cannot catch
    # — the system stays self-consistent around bad input)
    windows = []
    for token, settle in settlements.items():
        rq = reference_quarter(token)
        if rq is None or settle is None:
            continue
        s, e = rq
        if e <= estr_asof:
            continue
        implied = 100.0 - settle
        if not (-1.5 <= implied <= 6.5):  # €STR sanity bounds ±margin
            diag["flags"].append("outlier_dropped")
            continue
        windows.append({"token": token, "s": s, "e": e, "N": (e - s).days, "F": implied})
    windows.sort(key=lambda w: w["s"])
    diag["n_contracts"] = len(windows)
    if len(windows) < 2:
        return [], diag

    # unknowns: jumps at implementation dates strictly after the anchor
    meetings = [(d, implementation_date(d)) for d in (decisions or ECB_GC_DECISIONS)]
    unknown = [(dec, impl) for dec, impl in meetings if impl > estr_asof]
    horizon_end = max(w["e"] for w in windows)
    beyond = [dec.isoformat() for dec, impl in unknown if impl >= horizon_end]
    unknown = [(dec, impl) for dec, impl in unknown if impl < horizon_end]
    diag["beyond_horizon"] = beyond

    # horizon cap: more meetings than equations → solve the first K, flat tail
    if len(unknown) > len(windows):
        diag["flags"].append("horizon_capped")
        unknown = unknown[: len(windows)]

    # design matrix + rhs
    def _build(unk: list) -> tuple[list[list[float]], list[float]]:
        A: list[list[float]] = []
        b: list[float] = []
        carried = _carry_fixings(fixings or {}, windows[0]["s"], estr_asof) if fixings else {}
        for w in windows:
            row = []
            for _dec, impl in unk:
                # |{d : max(s, a+1) ≤ d < e, d ≥ impl}| / N  (e exclusive)
                lo = max(w["s"], estr_asof + timedelta(days=1), impl)
                n_after = max(0, (w["e"] - lo).days)
                row.append(n_after / w["N"])
            # realized-part correction for a running front quarter
            c_k = 0.0
            if w["s"] <= estr_asof:
                realized = 0.0
                n_real = 0
                for d, v in carried.items():
                    if w["s"] <= d <= estr_asof:
                        realized += v if v is not None else estr
                        n_real += 1
                if n_real:
                    c_k = (realized - n_real * estr) / w["N"]
            A.append(row)
            b.append(w["F"] - estr - c_k)
        return A, b

    # joint solve — minimum-norm (see _solve_minnorm: the system is
    # structurally rank-deficient; the min-norm convention carries zero
    # null-space component, and the 4 null directions are exactly the
    # within-quarter meeting splits the design flagged as convention-laden)
    A, b = _build(unknown)
    solved = _solve_minnorm(A, b)
    if solved is None:
        diag["flags"].append("singular")
        return [], diag
    deltas, rank = solved
    K = len(deltas)
    diag["k_solved"] = rank
    if rank < len(unknown):
        diag["flags"].append("null_space_split")  # within-quarter ambiguity

    # residual gate (on the K solved unknowns — the tail is flat by prior)
    rms = (
        sum(
            (sum(A[k][j] * deltas[j] for j in range(len(deltas)) if j < K) - b[k]) ** 2
            for k in range(len(A))
        )
        / len(A)
    ) ** 0.5 * 100  # pct→bp
    diag["rms_bp"] = round(rms, 2)
    # fitted quarterly-average levels (consistency check vs ecb_path):
    # predicted F_k = actual F_k + residual_k
    diag["fitted"] = {
        w["token"]: round(w["F"] + (sum(A[k][j] * deltas[j] for j in range(K)) - b[k]), 3)
        for k, w in enumerate(windows)
    }
    if rms > RMS_GATE_BP:
        diag["flags"].append("degraded")

    # per-meeting rows
    rows: list[ECBMeetingProb] = []
    cum = 0.0
    for j, ((dec, impl), d_pct) in enumerate(zip(unknown, deltas, strict=True)):
        delta_bp = d_pct * 100
        cum += d_pct
        abs_moves = abs(d_pct) / 0.25
        k = int(abs_moves)
        m = abs_moves - k
        if abs(delta_bp) < _HOLD_THRESHOLD_BP:
            ease, hike, expected = 0.0, 0.0, 0.0
            hold = 1.0
            sizes = {0: 1.0}
        elif delta_bp > 0:
            ease = 0.0
            hold = (1.0 - m) if k == 0 else 0.0
            hike = m if k == 0 else 1.0
            sizes = {k * 25: 1.0 - m, (k + 1) * 25: m}
            expected = abs_moves
        else:
            hike = 0.0
            hold = (1.0 - m) if k == 0 else 0.0
            ease = m if k == 0 else 1.0
            sizes = {-k * 25: 1.0 - m, -(k + 1) * 25: m}
            expected = -abs_moves
        # exactness: sole unknown in its reference quarter. A meeting
        # implemented BEFORE the front window (coefficient 1.0 everywhere)
        # rides the well-identified level direction — exact unless another
        # unknown has an identical column (only possible with two
        # pre-window meetings, impossible at ECB's 6-week cadence)
        j_window = next((w for w in windows if w["s"] <= impl < w["e"]), None)
        if j_window is not None:
            shares = sum(1 for _d2, im2 in unknown if j_window["s"] <= im2 < j_window["e"])
        else:
            col = [A[k][j] for k in range(len(A))]
            shares = 1 + sum(
                1
                for i in range(len(unknown))
                if i != j and all(abs(A[k][i] - col[k]) < 1e-9 for k in range(len(A)))
            )
        exact = shares == 1
        noise = None
        if not exact and j < K and j_window is not None:
            k_w = next(k for k, w in enumerate(windows) if w["s"] <= impl < w["e"])
            w1 = A[k_w][j]
            others = [
                A[k_w][i]
                for i, (_d2, im2) in enumerate(unknown)
                if i < K and im2 != impl and j_window["s"] <= im2 < j_window["e"]
            ]
            if others and abs(w1 - others[0]) > 1e-9:
                noise = round(1.0 / abs(w1 - others[0]), 2)
        rows.append(
            ECBMeetingProb(
                meeting_date=dec,
                impl_date=impl,
                prob_ease=round(ease, 4),
                prob_hold=round(hold, 4),
                prob_hike=round(hike, 4),
                implied_rate=(round(dfr + cum, 4) if dfr is not None else None),
                expected_moves=round(expected, 4),
                delta_bp=round(delta_bp, 2),
                exact=exact,
                noise_amp=noise,
                sizes={str(kk): round(vv, 4) for kk, vv in sizes.items()},
            )
        )
    return rows, diag


def format_brief(
    rows: list[ECBMeetingProb],
    diag: dict | None = None,
    asof: str | None = None,
    today: date | None = None,
) -> str | None:
    """'ECBWatch hike ≈85% (Sep-10, +21bp → DFR 2.46%, ESR 09-09)' — the
    NEXT meeting only; later rows ride the rank-deficient within-quarter
    split and are never surfaced.

    Decided meetings (decision day < today) stay in the SOLVE as the level
    bootstrap but never display: during the €STR fixing lag (decision →
    implementation Wednesday → first reflecting fixing) a just-decided
    meeting still counts as an unknown, and rows[0] showed a decided Sep-10
    hike as a forward 'hike ≈93%' on 09-17. `today` is injectable so tests
    pin their own clock (no time-bomb fixtures).

    Honesty markers (review round-1): '≈' when the meeting shares its
    reference quarter (min-norm split, noise_amp recorded in raw_json);
    the signed delta in bp (so 'hike 100%' cannot hide a +40bp expected
    move); the ESR strip date — decision-day prints are T−1-close pricing."""
    if not rows:
        return None
    t = today or datetime.now(UTC).date()
    r = next((x for x in rows if x.meeting_date >= t), None)
    if r is None:
        return None
    if r.prob_hike >= max(r.prob_ease, r.prob_hold):
        act, p = "hike", r.prob_hike
    elif r.prob_ease >= r.prob_hold:
        act, p = "cut", r.prob_ease
    else:
        act, p = "hold", r.prob_hold
    approx = "" if r.exact else "≈"
    delta = f"{r.delta_bp:+.0f}bp"
    implied = f" → DFR {r.implied_rate:.2f}%" if r.implied_rate is not None else ""
    tail = ""
    if asof:
        tail += f", ESR {asof[5:]}"
    # ROUND-10 outage-sim: the strip_stale flag existed but was WRITE-ONLY —
    # consume it (mirror of the FedWatch stale marker)
    if diag and "strip_stale" in diag.get("flags", []):
        tail += " ⚠strip stale"
    if diag and "degraded" in diag.get("flags", []):
        tail += f" ⚠ fit {diag.get('rms_bp', '?')}bp"
    if ECB_GC_DECISIONS and max(ECB_GC_DECISIONS) < datetime.now(UTC).date() + timedelta(days=90):
        tail += " ⚠ vendored schedule <90d — UPDATE ecbwatch.ECB_GC_DECISIONS"
    return f"ECBWatch {act} {approx}{p:.0%} ({r.meeting_date.strftime('%b-%d')}, {delta}{implied}{tail})"
